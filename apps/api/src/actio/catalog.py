"""The models this installation offers, and where each one runs.

They are read from a TOML file, `config/models.toml`. Everything else in Actio asks for
a model by the name it has there, such as `standard`, and gets back an `OfferedModel`:
what to call it at its provider, its limits, and the client to stream an answer through.

- `read_file` reads and checks the file. A mistake raises `CatalogError`, which says
  where the file is and what is wrong in it.
- `open_catalog` builds the clients for the providers it names, and closes them again.
  Nothing connects until a model is used.
- `Catalog.status` asks a provider whether it has a model, and what the model can do.

Nothing here knows about HTTP: the route is in `actio.api.models`.
"""

import asyncio
import os
import tomllib
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal, Self

import httpx2
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from actio.core.settings import Settings
from actio_llm import (
    ChatProvider,
    LLMError,
    ModelNotFoundError,
    Ollama,
    OpenAICompatible,
    Timeouts,
)

STATUS_LIFETIME = timedelta(seconds=15)
"""How long an answer about a model's status is reused before its provider is asked
again. Long enough that a page which shows the models doesn't question the provider on
every look; short enough that a model pulled a moment ago shows up."""

TIMEOUTS = Timeouts(connect_s=2.0, request_s=3.0)
"""Asking for a status is a small call: a provider that takes longer than this counts as
away. Waiting for an answer to start, and between its pieces, keeps the adapters' own,
much longer, patience."""

type Name = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{0,31}$")]
"""The name of a model or a provider in the file: short, lower-case, safe in a URL."""

type Kind = Literal["ollama", "openai-compatible"]


class CatalogError(Exception):
    """The models file is missing, unreadable, or says something that can't be right."""


# --- The file -------------------------------------------------------------------------


class ProviderEntry(BaseModel):
    """One `[providers.<name>]` table: a server that runs models."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Kind
    url: str | None = Field(default=None, pattern=r"^https?://\S+$")
    """Where the server is. For Ollama, left out: `ACTIO_OLLAMA_URL` says."""
    api_key_env: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]*$")
    """The name of the environment variable that holds the server's key. The key itself
    never goes in the file."""

    @model_validator(mode="after")
    def _fits_its_kind(self) -> Self:
        if self.kind == "openai-compatible" and self.url is None:
            raise ValueError('an "openai-compatible" provider needs a url')
        if self.kind == "ollama" and self.api_key_env is not None:
            raise ValueError("Ollama takes no key")
        return self


class ModelEntry(BaseModel):
    """One `[models.<name>]` table: a model on offer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    provider: Name
    model: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    context_window: int = Field(ge=256)
    max_output_tokens: int = Field(ge=1)
    thinking: bool | None = None
    """Whether the model can reason before it answers. Only for providers that can't be
    asked; an Ollama server says so itself."""

    @model_validator(mode="after")
    def _leaves_room_for_the_prompt(self) -> Self:
        if self.max_output_tokens >= self.context_window:
            raise ValueError(
                "max_output_tokens must be smaller than context_window: "
                "the answer and what it answers share the window"
            )
        return self


class CatalogFile(BaseModel):
    """The whole file."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    default: Name
    providers: dict[Name, ProviderEntry] = Field(min_length=1)
    models: dict[Name, ModelEntry] = Field(min_length=1)

    @model_validator(mode="after")
    def _names_what_exists(self) -> Self:
        if self.default not in self.models:
            raise ValueError(f'default = "{self.default}" is not one of the models')
        for name, model in self.models.items():
            provider = self.providers.get(model.provider)
            if provider is None:
                raise ValueError(
                    f'models.{name}: provider = "{model.provider}" is not one of the providers'
                )
            if provider.kind == "ollama" and model.thinking is not None:
                raise ValueError(
                    f"models.{name}: leave `thinking` out for a model on Ollama, "
                    "which says itself what its models can do"
                )
        return self


def read_file(path: Path) -> CatalogFile:
    """Read and check the models file. Raises `CatalogError` naming the file and the fault."""
    try:
        with path.open("rb") as file:
            data = tomllib.load(file)
    except FileNotFoundError:
        raise CatalogError(
            f"the models file {str(path)!r} doesn't exist. Set ACTIO_MODELS_FILE to where "
            "it is; the repository has one at config/models.toml"
        ) from None
    except OSError as error:
        raise CatalogError(f"can't read the models file {str(path)!r}: {error}") from error
    except tomllib.TOMLDecodeError as error:
        raise CatalogError(f"{path} isn't valid TOML: {error}") from error
    try:
        return CatalogFile.model_validate(data)
    except ValidationError as error:
        raise CatalogError(f"{path}: {'; '.join(map(_in_words, error.errors()))}") from None


def _in_words(fault: Mapping[str, Any]) -> str:
    """One fault as `models.big.context_window: what is wrong with it`. A fault of the
    file as a whole, such as a default that isn't a model, names its place itself."""
    where = ".".join(str(part) for part in fault["loc"])
    what = str(fault["msg"]).removeprefix("Value error, ")
    return f"{where}: {what}" if where else what


# --- What the rest of Actio uses ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OfferedModel:
    """A model on offer: everything needed to ask it something."""

    name: str
    """Its name in Actio, such as `standard`."""
    title: str
    kind: Kind
    """What kind of server runs it."""
    model: str
    """Its name on that server."""
    context_window: int
    max_output_tokens: int
    chat: ChatProvider
    """Stream an answer with `chat.stream(ChatRequest(model=model, ...))`."""


@dataclass(frozen=True, slots=True)
class ModelStatus:
    """What a provider said about a model, when it was last asked."""

    state: Literal["ready", "missing", "unreachable"]
    """`ready`: the server has it. `missing`: the server answers, without this model.
    `unreachable`: the server doesn't answer, or answers nonsense."""
    thinking: bool
    """Whether the model can reason before it answers. False when that isn't known."""


class UnknownModelError(Exception):
    """No model on offer has this name."""


class Catalog:
    """The models on offer. Iterating gives them in the file's order."""

    def __init__(
        self, models: Mapping[str, OfferedModel], *, default: str, thinking: Mapping[str, bool]
    ) -> None:
        self._models = dict(models)
        self._declared_thinking = dict(thinking)
        self._statuses: dict[str, tuple[datetime, ModelStatus]] = {}
        self.default = default

    def __iter__(self) -> Iterator[OfferedModel]:
        return iter(self._models.values())

    def get(self, name: str | None = None) -> OfferedModel:
        """The model called `name`, or the default one. Raises `UnknownModelError`."""
        try:
            return self._models[name or self.default]
        except KeyError:
            raise UnknownModelError(name) from None

    async def status(self, name: str, *, now: datetime) -> ModelStatus:
        """Whether the model's provider has it, and what the model can do.

        An answer is reused for `STATUS_LIFETIME`, so asking often is cheap.
        """
        known = self._statuses.get(name)
        if known is not None and now - known[0] < STATUS_LIFETIME:
            return known[1]
        status = await self._ask(self.get(name))
        self._statuses[name] = (now, status)
        return status

    async def statuses(self, *, now: datetime) -> dict[str, ModelStatus]:
        """The status of every model, asked for at the same time."""
        names = list(self._models)
        found = await asyncio.gather(*(self.status(name, now=now) for name in names))
        return dict(zip(names, found, strict=True))

    async def _ask(self, offered: OfferedModel) -> ModelStatus:
        declared = self._declared_thinking.get(offered.name, False)
        try:
            if isinstance(offered.chat, Ollama):
                described = await offered.chat.describe(offered.model)
                return ModelStatus("ready", thinking=described.can("thinking"))
            if isinstance(offered.chat, OpenAICompatible):
                served = await offered.chat.models()
                return ModelStatus("ready" if offered.model in served else "missing", declared)
        except ModelNotFoundError:
            return ModelStatus("missing", thinking=False)
        except LLMError:
            return ModelStatus("unreachable", thinking=False)
        return ModelStatus("ready", declared)  # a provider that can't be asked: take its word


@asynccontextmanager
async def build(
    file: CatalogFile,
    *,
    ollama_url: str,
    transports: Mapping[str, httpx2.AsyncBaseTransport] | None = None,
) -> AsyncIterator[Catalog]:
    """The catalog a checked file describes. Its providers' clients are closed on the
    way out.

    `ollama_url` is where Ollama providers are, unless the file gives one its own `url`.
    `transports` replaces how a provider is reached, by the provider's name: tests pass
    a fake server there.
    """
    transports = transports or {}
    async with AsyncExitStack() as stack:
        chats: dict[str, ChatProvider] = {}
        for name, provider in file.providers.items():
            headers = {}
            if provider.api_key_env is not None:
                key = os.environ.get(provider.api_key_env)
                if not key:
                    raise CatalogError(
                        f"providers.{name}: the environment variable {provider.api_key_env}, "
                        "which should hold its key, is not set"
                    )
                headers["Authorization"] = f"Bearer {key}"
            client = await stack.enter_async_context(
                httpx2.AsyncClient(
                    base_url=provider.url or ollama_url,
                    headers=headers,
                    transport=transports.get(name),
                )
            )
            if provider.kind == "ollama":
                chats[name] = Ollama(client, timeouts=TIMEOUTS)
            else:
                chats[name] = OpenAICompatible(client, provider=name, timeouts=TIMEOUTS)
        models = {
            name: OfferedModel(
                name=name,
                title=entry.title,
                kind=file.providers[entry.provider].kind,
                model=entry.model,
                context_window=entry.context_window,
                max_output_tokens=entry.max_output_tokens,
                chat=chats[entry.provider],
            )
            for name, entry in file.models.items()
        }
        declared = {name: bool(entry.thinking) for name, entry in file.models.items()}
        yield Catalog(models, default=file.default, thinking=declared)


@asynccontextmanager
async def open_catalog(settings: Settings) -> AsyncIterator[Catalog]:
    """The catalog that `settings.models_file` describes."""
    file = read_file(settings.models_file)
    async with build(file, ollama_url=settings.ollama_url) as catalog:
        yield catalog
