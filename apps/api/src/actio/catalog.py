"""The models this installation offers, and where each one runs.

They are read from a TOML file, `config/models.toml`. Everything else in Actio asks for
a model by the name it has there, such as `standard`, and gets back an `OfferedModel`:
what to call it at its provider, its limits, and the client to stream an answer through.

- `read_file` reads and checks the file. A mistake raises `CatalogError`, which says
  where the file is and what is wrong in it.
- `open_catalog` builds the clients for the providers it names, and closes them again.
  Nothing connects until a model is used.
- `Catalog.status` says whether a model's provider has it, and what the model can do.
- `Catalog.chain` gives a model and those that stand in for it, which are asked in its
  place when it can't answer before the first word, and `Catalog.chain_status` says
  which of them are ready (ADR 0028). `Chain.asked_with` says which of those are asked
  for a request of so many tokens, and in which order: a model whose room is smaller is
  passed over while another has room (`OfferedModel.room`, ADR 0029), and then a model
  whose provider's plan has nothing left for the request, by what `Chain.look` read of
  its budget, is asked after those that have some (`OfferedModel.budget`, ADR 0030).
  A model whose provider said to wait, longer than the walk waits out itself, counts as
  one with no budget left until that wait is over (`OfferedModel.own_wait`,
  `Chain.with_own_waits`). When none of those with room has any, the request is
  refused for now. Within each of those two groups, a model that keeps failing, by what
  `Chain.failing` read of its breaker, is asked after those that don't
  (`OfferedModel.breaker`, ADR 0031): that never refuses anything.
- What a model's provider said of it is reused for `STATUS_LIFETIME`, and asked again
  and waited for after that: `Catalog.chain_status` for a chain, `Catalog.status` for a
  model alone, and `Catalog.chains` for every model's chain at once, as the list of
  models needs. A turn's start alone goes by what is known (`Catalog.chain_as_known`):
  an answer younger than `STATUS_USED_FOR` is used without waiting while a model of
  its chain is ready by what is known, and the provider is asked again behind it, for
  the requests after. Nothing is refused by an old answer. One ask about a model is
  under way at a time, which every request that needs it shares, either way.
  `Catalog.stop_tasks` ends the asks under way, and the loads.
- `asked_through` gives the client a model is asked through: one that records each try
  in the model's breaker, and notes the wait its provider names (`OwnWait`), around
  the one `counted` gives, which counts in its budget what each request takes of it.
- `Catalog.load_ahead` has a model's server load it into memory before the first
  question to it, where the server can be asked to (ADR 0026): the model itself, never
  one that stands in for it.

A server writes only so many answers at once: a local one, such as Ollama, one. Each
provider that has such a limit gets a `Gate`, shared by every model it runs, at which
the answers that would be too many wait their turn.

Nothing here knows about HTTP: the routes are in `actio.api.catalog`.
"""

import asyncio
import logging
import math
import os
from collections import deque
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Callable,
    Hashable,
    Iterator,
    Mapping,
    Sequence,
)
from contextlib import AsyncExitStack, aclosing, asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Annotated, Literal, Self

import anyio
import httpx2
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    model_validator,
)

from actio.core import config_file
from actio.core.budget import Bucket, Budgets, BudgetsUnavailableError, Counts, Period
from actio.core.config_file import Name
from actio.core.settings import Settings
from actio_llm import (
    FOR_NOW,
    Breaker,
    Change,
    ChatProvider,
    ChatRequest,
    ConnectionFailedError,
    Failing,
    Finish,
    LLMError,
    LoadsAhead,
    ModelNotFoundError,
    Ollama,
    OpenAICompatible,
    RateLimitedError,
    ReasoningDelta,
    StreamEvent,
    TextDelta,
    Timeouts,
    TookTooLongError,
    hands_on,
    tokens_in,
)
from actio_llm.breaker import Closed, Opened, OpenedAgain
from actio_llm.openai_compatible import OWN_MEMBERS
from actio_llm.router import WAITS_AT_MOST_S

logger = logging.getLogger(__name__)

STATUS_LIFETIME = timedelta(seconds=15)
"""How long an answer about a model's status is reused before its provider is asked
again. Long enough that a page which shows the models doesn't question the provider on
every look; short enough that a model pulled a moment ago shows up."""

STATUS_USED_FOR = timedelta(minutes=5)
"""How long an answer about a model's status, once it is older than `STATUS_LIFETIME`,
is still used by a turn's start without waiting, while its provider is asked again
behind the request (`Catalog.chain_as_known`): only while a model of the chain is ready
by what is known, since nothing is refused by such an answer. Long enough for a
conversation's pace, an answer read and a question written; as long as Ollama keeps a
model loaded after it was last asked (ADR 0026). Older than this, an answer is asked
again and waited for, as one nothing is known of. Nothing else uses an old answer."""

TIMEOUTS = Timeouts(connect_s=2.0, request_s=3.0)
"""Asking for a status is a small call: a provider that takes longer than this counts as
away. Waiting for an answer to start, between its pieces and for the whole of it keeps
the adapters' own, much longer, patience, and so does a model's load, which takes as
long as the first word of an answer to a model that isn't loaded. A provider may say
itself how long it is given to start an answer (`ProviderEntry.begins_within_seconds`)
and to write a whole one (`ProviderEntry.answers_within_seconds`): those values are
replaced for its models."""

KEPT_PERCENT = 90
"""How much of each figure the models file states of a provider's limits Actio keeps to,
rounded down, and 1 at the least (`kept`): 27 of 30 requests a minute, 7,200 of 8,000
tokens. The tenth left is for what Actio can't count: its guess at a request's prompt
may be under what the provider counts, and its clock isn't the provider's (ADR 0030)."""

LIMITS: dict[str, tuple[Counts, Period]] = {
    "requests_a_minute": ("requests", "minute"),
    "requests_a_day": ("requests", "day"),
    "tokens_a_minute": ("tokens", "minute"),
    "tokens_a_day": ("tokens", "day"),
}
"""The keys of a model's table that state its provider's limits for it, each with what it
counts, and over which period: its budget's buckets, in this order."""

BELIEVED_AT_MOST = timedelta(days=1)
"""The longest wait a provider may name that Actio keeps as it was named (`believed`). A
provider's longest period is a day, of requests or of tokens: a wait named longer is
taken for a day, after which one request finds out what the provider says then."""

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
    answers_at_once: int | None = Field(default=None, ge=1)
    """How many answers the server writes at the same time, across all the models it
    runs. Left out: see `at_once`."""
    begins_within_seconds: float | None = Field(
        default=None, gt=0, strict=True, allow_inf_nan=False
    )
    """How many seconds the server is given, from the moment a request is sent, to send
    the first line of its answer: after that it counts as away, and the next model of a
    chain is asked. It bounds a load ahead too, which waits as an answer's first line is
    waited for. A number greater than 0, whole or not: a string that holds one is a
    mistake. Left out: the client's own (`Timeouts.first_event_s`), 90 seconds, which
    covers a model that is loaded cold."""
    answers_within_seconds: float | None = Field(
        default=None, gt=0, strict=True, allow_inf_nan=False
    )
    """How many seconds the server is given, from the moment a request is sent, to write
    a whole answer: after that the answer is cut where it stands. A turn records it as
    one that reached a limit when something of it was handed on; with nothing handed on,
    the next model of a chain is asked, as after any failure. A number greater than 0,
    whole or not, as above, and nothing ties it to `begins_within_seconds`: the first
    line, and a load ahead, have to come within the shorter of the two. Left out: the
    client's own (`Timeouts.answer_s`), 300 seconds."""

    @property
    def timeouts(self) -> Timeouts:
        """How long the clients of the server's models wait: the catalog's, with the
        server's own time to begin an answer, and to write a whole one, each where the
        file gives one."""
        given = TIMEOUTS
        if self.begins_within_seconds is not None:
            given = replace(given, first_event_s=self.begins_within_seconds)
        if self.answers_within_seconds is not None:
            given = replace(given, answer_s=self.answers_within_seconds)
        return given

    @model_validator(mode="after")
    def _fits_its_kind(self) -> Self:
        if self.kind == "openai-compatible" and self.url is None:
            raise ValueError('an "openai-compatible" provider needs a url')
        if self.kind == "ollama" and self.api_key_env is not None:
            raise ValueError("Ollama takes no key")
        return self

    @property
    def at_once(self) -> int | None:
        """How many answers the server is given at a time. `None`: as many as come.

        Ollama writes one answer at a time, unless the file says otherwise. A server
        that speaks OpenAI's API may write many at once, as vLLM does, so it is given
        every answer at once unless the file says how many it takes."""
        if self.answers_at_once is None and self.kind == "ollama":
            return 1
        return self.answers_at_once


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
    thinking_off: dict[str, JsonValue] | None = None
    """What a request to the model holds when a turn asks for no reasoning, in place of
    the client's own `reasoning_effort = "none"`: an empty table holds nothing. Only for
    a model of an "openai-compatible" server whose `thinking` is true. Only what JSON
    can hold: a TOML date in it would fail every request while its body is written."""
    thinking_on: dict[str, JsonValue] | None = None
    """What a request to the model holds when a turn asks for reasoning, in place of
    nothing. Only where `thinking_off` may be, and as it may be."""
    then: tuple[Name, ...] = ()
    """Other models of the file, asked in this one's place, in this order, when it can't
    answer before the first word (ADR 0028). Their own `then` isn't followed."""
    tokens_a_minute: int | None = Field(default=None, ge=1, strict=True)
    """How many tokens the model's server takes from this installation's account in a
    minute, as a cloud provider's plan says: the provider's own figure, for this model.
    A request larger than that fits in no minute there, and Groq's free plan was seen to
    refuse one whole, so it is part of the model's room (`OfferedModel.room`, ADR 0029),
    whatever the server does with such a request. It is also one of the limits of the
    model's budget, as `requests_a_minute` is. A whole number of 1 or more: a string, a
    fraction, `8000.0` and `true` are mistakes. Nothing ties it to `context_window` or
    `max_output_tokens`: either may be the smaller. Left out: the server has no such
    limit."""
    requests_a_minute: int | None = Field(default=None, ge=1, strict=True)
    """How many requests the model's server takes from this installation's account in a
    minute, as a cloud provider's plan says: the provider's own figure, for this model.
    Actio counts what it sends to the model, keeps to `KEPT_PERCENT` of the figure, and
    asks a ready model that has none left for a request after those of its chain that
    have some, or refuses the request for now when none of those with room for it has
    any (`OfferedModel.budget`, ADR 0030). A whole number of 1 or more, as
    `tokens_a_minute` is. Left out: the server has no such limit."""
    requests_a_day: int | None = Field(default=None, ge=1, strict=True)
    """The same, of the requests the server takes in a day."""
    tokens_a_day: int | None = Field(default=None, ge=1, strict=True)
    """The same, of the tokens the server takes in a day: a request's prompt and its
    answer."""

    @property
    def stated(self) -> dict[str, int | None]:
        """What the table states of its provider's limits for the model, by key: each of
        `LIMITS`, `None` where it is left out."""
        return {key: getattr(self, key) for key in LIMITS}

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
    titles: Name | None = None
    """The model that names a conversation after its first answer (`actio.chat.titles`).
    Left out, none does, and a conversation keeps its first question's first line."""
    providers: dict[Name, ProviderEntry] = Field(min_length=1)
    models: dict[Name, ModelEntry] = Field(min_length=1)

    @model_validator(mode="after")
    def _names_what_exists(self) -> Self:
        if self.default not in self.models:
            raise ValueError(f'default = "{self.default}" is not one of the models')
        if self.titles is not None and self.titles not in self.models:
            raise ValueError(f'titles = "{self.titles}" is not one of the models')
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
            for told in ("thinking_off", "thinking_on"):
                members = getattr(model, told)
                if members is None:
                    continue
                if provider.kind == "ollama":
                    raise ValueError(
                        f"models.{name}: leave `{told}` out for a model on Ollama, "
                        "which is told whether to reason in its own way"
                    )
                if model.thinking is not True:
                    raise ValueError(
                        f"models.{name}: `{told}` is for a model that can reason: "
                        "it needs `thinking = true`"
                    )
                for member in members:
                    if member in OWN_MEMBERS:
                        raise ValueError(
                            f"models.{name}: `{told}` can't hold `{member}`, "
                            "which Actio writes into a request itself"
                        )
            for stand_in in model.then:
                if stand_in == name:
                    raise ValueError(f"models.{name}: `then` can't name the model itself")
                if stand_in not in self.models:
                    raise ValueError(
                        f'models.{name}: `then` names "{stand_in}", which is not one of the models'
                    )
                if model.then.count(stand_in) > 1:
                    raise ValueError(f'models.{name}: `then` names "{stand_in}" twice')
        self._share_budgets_alike()
        return self

    def _share_budgets_alike(self) -> None:
        """Two models that name the same provider and the same model on it share one
        budget (ADR 0030), so they state the same figures of its limits: a key left out
        by one and given by the other differs too."""
        first_of: dict[tuple[str, str], str] = {}
        for name, model in self.models.items():
            first = first_of.setdefault((model.provider, model.model), name)
            if first == name:
                continue
            for key, theirs in self.models[first].stated.items():
                mine = getattr(model, key)
                if mine != theirs:
                    raise ValueError(
                        f"models.{name}: `{key}` is {_said(mine)} here and {_said(theirs)} in"
                        f" models.{first}, the same model on the same provider: the two share"
                        " one budget, and state the same figures of it"
                    )


def _said(figure: int | None) -> str:
    return "left out" if figure is None else str(figure)


def read_file(path: Path) -> CatalogFile:
    """Read and check the models file. Raises `CatalogError` naming the file and the fault."""
    return config_file.read(
        path,
        CatalogFile,
        what="models",
        elsewhere=(
            "Set ACTIO_MODELS_FILE to where it is; the repository has one at config/models.toml"
        ),
        failure=CatalogError,
    )


# --- What the rest of Actio uses ------------------------------------------------------


@dataclass(eq=False, slots=True)
class _Waiting:
    """An answer that waits at a gate: its number in the order the waiting answers came,
    and the future the gate resolves when it hands the answer a place."""

    came: int
    passed: asyncio.Future[None]


class Gate:
    """The way in to a server that writes only so many answers at once.

    An answer passes while the server has room for it, and holds its place until its
    stream is closed. One that comes while the server is full waits here, inside Actio,
    and its server hears of it only once it has passed: a server that is asked anyway
    keeps the request waiting inside itself, while the client's patience for the first
    word runs out.

    Each answer passes as an account's: the one it is for, as a key that the gate only
    compares and hashes. A place that is given back while answers wait goes on at once,
    to the waiting answer of the account that holds the fewest places here; of those
    that hold as few, the one given a place here the longest ago, an account of which
    nothing is remembered before any that is; and of those, the one whose answer came
    first. One account's own answers pass in the order they came. "Given a place" is
    counted, not timed: the gate numbers the places it gives, and reads no clock (ADR
    0023, revision "a place that comes free goes to the account that has had the least").

    An account's last place is remembered while it holds a place or has an answer
    waiting here, and beyond that until nobody waits. Whenever nobody waits, the gate
    keeps nothing of an account that holds no place: guests are accounts, and there is
    no end of them.

    An answer that gives up waiting, by being cancelled, leaves, and changes nothing for
    the others. One that was handed a place and was cancelled before it could go on
    with it counts as given that place, and gives it back at once, as an answer that
    ended at once would: the place goes on by the same rule, and is never lost, and the
    account's next answer waits as that of an account given a place just then.

    `async with gate.passing(account):` waits, and gives the place back on the way out,
    however that is, in a step that awaits nothing.
    """

    def __init__(self, at_once: int) -> None:
        self.at_once = at_once
        """How many answers pass at the same time."""
        self._taken = 0
        """How many places are held, one handed on and not yet gone on with included."""
        self._held: dict[Hashable, int] = {}
        """How many places each account holds. An account that holds none isn't in it."""
        self._lines: dict[Hashable, deque[_Waiting]] = {}
        """Each account's waiting answers, in the order they came. No line is empty."""
        self._waiting = 0
        """How many answers are in the lines."""
        self._last: dict[Hashable, int] = {}
        """The number of each remembered account's last place."""
        self._given = 0
        """How many places the gate has given: the number of the last one, from 1."""
        self._came = 0
        """How many answers have come to wait: the number of the next one."""

    @property
    def full(self) -> bool:
        """Whether an answer that comes now has to wait."""
        return self._taken >= self.at_once

    @property
    def waiting(self) -> int:
        """How many answers wait to pass."""
        return self._waiting

    @property
    def accounts(self) -> int:
        """How many accounts the gate keeps anything of: those that hold a place or have
        an answer waiting, and those it remembers while somebody waits. Once nobody
        waits, as many as hold a place. For tests."""
        return len(self._held.keys() | self._lines.keys() | self._last.keys())

    @asynccontextmanager
    async def passing(self, account: Hashable) -> AsyncIterator[None]:
        """Pass as `account`'s answer, waiting as long as it takes, and hold the place
        until the end of the block. At once, without waiting for anything, while a place
        is free."""
        await self._pass(account)
        try:
            yield
        finally:
            self._give_back(account)

    async def _pass(self, account: Hashable) -> None:
        if self._taken < self.at_once:
            self._give(account)
            return
        waiting = _Waiting(self._came, asyncio.get_running_loop().create_future())
        self._came += 1
        self._lines.setdefault(account, deque()).append(waiting)
        self._waiting += 1
        try:
            await waiting.passed
        except BaseException:
            if waiting.passed.cancelled() or not waiting.passed.done():
                # It gave up waiting.
                self._out_of_line(account, waiting)
                self._forget_the_absent()
            else:
                # It was handed a place in the very step in which it was cancelled.
                self._give_back(account)
            raise

    def _give(self, account: Hashable) -> None:
        """A place, to `account`: the next number, and remembered as its last."""
        self._taken += 1
        self._given += 1
        self._last[account] = self._given
        self._held[account] = self._held.get(account, 0) + 1

    def _give_back(self, account: Hashable) -> None:
        """`account` gives one of its places back: it goes on at once to the waiting
        answer the rule chooses, and is free when none waits. Awaits nothing."""
        left = self._held[account] - 1
        if left:
            self._held[account] = left
        else:
            del self._held[account]
        self._taken -= 1
        chosen = self._next()
        if chosen is not None:
            to, waiting = chosen
            self._out_of_line(to, waiting)
            self._give(to)
            waiting.passed.set_result(None)
        self._forget_the_absent()

    def _next(self) -> tuple[Hashable, _Waiting] | None:
        """The waiting answer a place goes to, with its account: of the account that
        holds the fewest places, then the one given a place the longest ago, nothing
        remembered counting as longest, then the one whose answer came first. `None`
        when nobody waits. An answer that was cancelled while it waited, and hasn't left
        its line yet, waits no more."""
        chosen: tuple[Hashable, _Waiting] | None = None
        best: tuple[int, int, int] | None = None
        for account, line in self._lines.items():
            first = next((each for each in line if not each.passed.done()), None)
            if first is None:
                continue
            # Places are numbered from 1: an account of which nothing is remembered is 0.
            rank = (self._held.get(account, 0), self._last.get(account, 0), first.came)
            if best is None or rank < best:
                chosen, best = (account, first), rank
        return chosen

    def _out_of_line(self, account: Hashable, waiting: _Waiting) -> None:
        line = self._lines[account]
        line.remove(waiting)
        if not line:
            del self._lines[account]
        self._waiting -= 1

    def _forget_the_absent(self) -> None:
        """Whenever nobody waits: forget each account that holds no place."""
        if not self._waiting:
            self._last = {
                account: last for account, last in self._last.items() if account in self._held
            }


@dataclass(frozen=True, slots=True)
class Budget:
    """What a provider's plan lets this installation send to one of its models, as Actio
    keeps to it (ADR 0030): a bucket for each of its limits that the models file states,
    the size of each what is kept of the figure (`kept`). Two models of the file that are
    one model on one provider have the same buckets: they share one budget."""

    buckets: tuple[Bucket, ...]

    def takes(self, *, requests: int, tokens: int) -> list[tuple[Bucket, int]]:
        """Each bucket with an amount: `requests` for one of requests, `tokens` for one
        of tokens."""
        return [
            (bucket, requests if bucket.counts == "requests" else tokens) for bucket in self.buckets
        ]


def kept(figure: int) -> int:
    """What Actio keeps to of a figure a provider states: `KEPT_PERCENT` of it, rounded
    down, and 1 at the least."""
    return max(1, figure * KEPT_PERCENT // 100)


def budget_of(provider: str, model: str, stated: Mapping[str, int | None]) -> Budget | None:
    """The budget of the model called `model` on the provider called `provider`, from the
    figures the models file states of its limits, by their keys (`LIMITS`, as
    `ModelEntry.stated` gives them): `None` when it states none."""
    unknown = set(stated) - set(LIMITS)
    if unknown:
        raise ValueError(f"not a limit of a model: {', '.join(sorted(unknown))}")
    buckets = tuple(
        Bucket(counts, period, provider, model, kept(figure))
        for key, (counts, period) in LIMITS.items()
        if (figure := stated.get(key)) is not None
    )
    return Budget(buckets) if buckets else None


def believed(retry_after_s: float | None) -> timedelta | None:
    """How long Actio takes a model's provider to refuse its requests, from the wait in
    seconds that a 429 of it named (`RateLimitedError.retry_after_s`): kept until it is
    over (`OwnWait`), and meanwhile the model counts as having no budget left.

    `None`, nothing kept, when it named none, `nan` or `inf`, and for a wait of
    `WAITS_AT_MOST_S` or less, negative ones among them: the walk waits those out itself
    and asks the model once more (`actio_llm.router`), so nothing is left of them to
    keep. Otherwise the wait, and `BELIEVED_AT_MOST` when it is that or more.

    Numbers alone are compared before a `timedelta` is made, which a wait as long as
    `1e308` seconds can't be: it never raises, as the end of a try, where it is asked,
    must not."""
    # `nan` compares false with everything, and `inf` is no moment to wait for.
    if retry_after_s is None or not WAITS_AT_MOST_S < retry_after_s < math.inf:
        return None
    if retry_after_s >= BELIEVED_AT_MOST.total_seconds():
        return BELIEVED_AT_MOST
    return timedelta(seconds=retry_after_s)


class OwnWait:
    """What a model's provider last said of when it takes requests again: the moment the
    wait its 429 named is over, when Actio keeps it (`believed`). Until then the model
    counts as having no budget left (`Chain.with_own_waits`).

    Nothing here reads a clock or logs: every moment is given."""

    def __init__(self) -> None:
        self._until: datetime | None = None
        self._words = 0

    @property
    def words(self) -> int:
        """How many waits of the provider's have been noted here so far (`told`). A try
        reads it when it begins, and gives it to `over` when it ends answered."""
        return self._words

    def told(self, until: datetime) -> None:
        """The provider's newest word: its wait is over at `until`. It replaces what was
        kept, whether that was later or sooner."""
        self._until = until
        self._words += 1

    def left(self, now: datetime) -> timedelta | None:
        """How long until the wait is over, at `now`. `None` when nothing is kept, and
        from the moment it is over on."""
        if self._until is None or now >= self._until:
            return None
        return self._until - now

    def over(self, now: datetime, *, words: int) -> bool:
        """The model answered a try that began when `words` waits had been noted
        (`words`, read then): forget the wait, unless another has been noted since. An
        answer to a request that was sent before a refusal was heard says nothing of
        that refusal's wait, however late it ends. Whether a wait that was still running
        at `now` was forgotten."""
        if words != self._words:
            return False
        running = self.left(now) is not None
        self._until = None
        return running


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
    gate: Gate | None = None
    """Where an answer waits until its server has room for it: the same for every model
    on that server. `None`: the server is given every answer at once."""
    then: tuple[str, ...] = ()
    """The names of the models asked in its place, in this order, when it can't answer
    before the first word: see `Catalog.chain`."""
    tokens_a_minute: int | None = None
    """How many tokens its server takes from this installation's account in a minute.
    `None`: no such limit."""
    budget: Budget | None = None
    """What its provider's plan lets this installation send to it, as Actio keeps to it:
    a ready model with nothing left for a request is asked after those of its chain
    that have budget, and when none of those with room for the request has any, the
    request is refused for now (`Chain.look`, `Chain.asked_with`). What each request to
    it takes is counted there (`counted`). `None`: the models file states none of its
    limits, and nothing is counted."""
    breaker: Breaker = field(default_factory=Breaker, compare=False, repr=False)
    """Whether it keeps failing, from each try of it that ended (ADR 0031): a ready model
    whose breaker says so is asked after the models of its chain that aren't failing,
    within each group of the budget's rule, and never refused for it (`Chain.failing`,
    `Chain.asked_with`). Each try of it is recorded there (`asked_through`). Kept in the
    process, and shared by two models of the file that are one model on one provider,
    as a budget is. A new one for each model on offer unless given."""
    own_wait: OwnWait = field(default_factory=OwnWait, compare=False, repr=False)
    """The wait its provider last named, in a 429, when that is longer than the walk
    waits out itself (`believed`, ADR 0030): until it is over, a ready model counts as
    having no budget left, whether or not it has a budget, by the budget's rule
    (`Chain.with_own_waits`, `Chain.asked_with`). Each try of it notes it there, and an
    answer of the model to a request sent after it was noted ends it (`asked_through`).
    Kept in the process, and shared by
    two models of the file that are one model on one provider, as its breaker is. A new
    one for each model on offer unless given."""

    @property
    def room(self) -> int:
        """How many tokens of a request the model takes here: its context window, or
        what its server takes in a minute when that is less. A model whose room is
        smaller than a request is passed over while another of its chain has room
        (`Chain.asked_with`). A request to it still carries its `context_window`: Ollama
        loads the model by it, and which limit an answer reached is told by it."""
        if self.tokens_a_minute is None:
            return self.context_window
        return min(self.context_window, self.tokens_a_minute)


@dataclass(frozen=True, slots=True)
class ModelStatus:
    """What a provider said about a model, when it was last asked."""

    state: Literal["ready", "missing", "unreachable"]
    """`ready`: the server has it. `missing`: the server answers, without this model.
    `unreachable`: the server doesn't answer, or answers nonsense."""
    thinking: bool
    """Whether the model can reason before it answers. False when that isn't known."""


@dataclass(frozen=True, slots=True)
class ProvidersWait:
    """The limit in the way of a model whose provider said to wait (`OwnWait`): no
    bucket of Actio's count, but the provider's own word. In words, as a bucket is."""

    def __str__(self) -> str:
        return "its provider's own wait"


PROVIDERS_WAIT = ProvidersWait()
"""The limit a `Spent` names when a provider's own wait is in the way."""


@dataclass(frozen=True, slots=True)
class Spent:
    """What is known of a model that has no budget left for a request now: how long
    until it has, and the limit in the way. By a look at its budget (`Chain.look`), the
    one of its buckets that takes the longest to have room for it; or by its provider's
    own wait (`PROVIDERS_WAIT`, `Chain.with_own_waits`). Either limit reads as words:
    `tokens a minute`, `its provider's own wait`."""

    wait: timedelta
    limit: Bucket | ProvidersWait

    @property
    def seconds(self) -> int:
        """The wait in whole seconds, rounded up, and 1 at the least: as the log says
        it."""
        return _whole_seconds(self.wait)


def _whole_seconds(wait: timedelta) -> int:
    """A wait in whole seconds, rounded up, and 1 at the least: as the log says it."""
    return max(1, math.ceil(wait.total_seconds()))


@dataclass(frozen=True, slots=True)
class Asked:
    """The models of a chain that a request is asked of, in which order, and those
    passed over for it; or that it is refused for now (`Chain.asked_with`)."""

    models: tuple[tuple[OfferedModel, ModelStatus], ...]
    """Those to ask, in this order, each with its status: ready, all of them. Those
    with budget first, then those without; in each of the two, those that aren't
    failing first and then those that are, each in the chain's order. Empty when the
    request is refused (`refused`), and when no model of the chain is ready."""
    too_small: tuple[OfferedModel, ...]
    """Those that are ready and are passed over, their room smaller than the request,
    in the chain's order."""
    spent: tuple[tuple[OfferedModel, Spent], ...] = ()
    """Those that are ready, are not passed over for their room, and whose budget has
    nothing left for the request now, in the chain's order, each with what was said of
    it, by the look or by its provider: asked after those that have budget, or, when
    the request is refused, the reason it is. Empty when no ready model has room and
    none has budget: every one is asked then, and none is named."""
    refused: Spent | None = None
    """When the request is refused for now: some ready models have room for it, and
    none of those has budget left for it. The shortest wait among them, with its limit:
    of two as short, the first in the chain's order. `None` when it is asked, which it
    is too when no ready model has room for it, and also when no model of the chain is
    ready, which refuses nothing here."""
    failing: tuple[tuple[OfferedModel, Failing], ...] = ()
    """The models that are asked later than they would be if no model were failing, in
    the order they are asked, each with what its breaker said: each failing model asked
    after a model that the order without breakers has after it. A failing model left
    where it was isn't named: a chain's only model, one of a chain whose every model
    asked is failing, one that was last already, or one that its budget alone put last,
    which `spent` names. Empty when the request is refused."""


@dataclass(frozen=True, slots=True)
class Chain:
    """A model and the models asked in its place, in order, each with its status."""

    models: tuple[tuple[OfferedModel, ModelStatus], ...]
    """The model itself first."""

    @property
    def status(self) -> ModelStatus:
        """The model's own status, whatever its stand-ins'."""
        return self.models[0][1]

    @property
    def ready(self) -> list[tuple[OfferedModel, ModelStatus]]:
        """Those that can be asked now, in order: a model that isn't `ready` is passed
        over, `missing` or `unreachable` alike."""
        return [(offered, status) for offered, status in self.models if status.state == "ready"]

    @property
    def passed_over(self) -> list[tuple[OfferedModel, ModelStatus]]:
        """Those that aren't `ready`, in order."""
        return [(offered, status) for offered, status in self.models if status.state != "ready"]

    async def look(self, budgets: Budgets, tokens: int, *, now: datetime) -> dict[str, Spent]:
        """Whether one request, with a prompt of about `tokens` by Actio's guess, fits
        the budget of each ready model of the chain that has one: those it doesn't fit
        now, by their names, each with how long until it does and the limit in the way.
        A model that has no budget, or isn't ready, is never among them.

        One question to Valkey for the whole chain, and none when no ready model has a
        budget. It writes nothing. Raises `BudgetsUnavailableError` when Valkey doesn't
        answer."""
        each = [
            (offered, offered.budget.takes(requests=1, tokens=tokens))
            for offered, _ in self.ready
            if offered.budget is not None
        ]
        waits = iter(await budgets.look(*(take for _, takes in each for take in takes), now=now))
        spent: dict[str, Spent] = {}
        for offered, takes in each:
            # The longest wait is the limit in the way: of two as long, the first.
            wait, limit = max(
                ((next(waits), bucket) for bucket, _ in takes), key=lambda found: found[0]
            )
            if wait > timedelta(0):
                spent[offered.name] = Spent(wait, limit)
        return spent

    def with_own_waits(self, spent: Mapping[str, Spent], now: datetime) -> dict[str, Spent]:
        """What a look said (`spent`, by name), with each ready model of the chain whose
        provider's own wait isn't over at `now` (`OfferedModel.own_wait`): that model
        has no budget left until then, whether or not it has a budget, the limit in the
        way being `PROVIDERS_WAIT`. Of a model that both name, the longer wait; of two as
        long, the provider's own. A model that isn't ready is never added. Asks nobody
        anything."""
        found = dict(spent)
        for offered, _ in self.ready:
            left = offered.own_wait.left(now)
            if left is None:
                continue
            counted = found.get(offered.name)
            if counted is None or left >= counted.wait:
                found[offered.name] = Spent(left, PROVIDERS_WAIT)
        return found

    def failing(self, now: datetime) -> dict[str, Failing]:
        """The ready models of the chain whose breaker says that they are failing at
        `now`, by their names, each with what it says (ADR 0031). A model that isn't
        ready is never among them. Asks nobody anything."""
        found: dict[str, Failing] = {}
        for offered, _ in self.ready:
            said = offered.breaker.failing(now)
            if said is not None:
                found[offered.name] = said
        return found

    def asked_with(
        self, tokens: int, spent: Mapping[str, Spent], failing: Mapping[str, Failing]
    ) -> Asked:
        """Which of the ready models are asked for a request of about `tokens`, a guess
        at its prompt, in which order, and which are passed over for their room (ADR
        0029); or that the request is refused for now, when no model with room for it
        has budget left for it, which `spent` says (`look`, ADR 0030). `failing` says
        which models keep failing (`failing`, ADR 0031), and changes only the order.

        The room first. A model has room when `tokens` is no larger than its `room`.
        While at least one ready model has room, those that haven't are passed over;
        when none has, every ready model is left, and none is passed over for its room.

        Then the budget, of those left. A model has budget when `spent` doesn't name it:
        it has no budget, or the look said that the request fits it now, or the look
        couldn't be had, and its provider didn't say to wait (`with_own_waits`).

        - While at least one has budget, those are asked first, in the chain's order,
          and then those without, in the chain's order: Actio keeps a tenth of each
          figure back, and a model its count calls spent may answer all the same.
        - When none has, and at least one ready model has room, nobody is asked: the
          request is refused for now, with the shortest wait among those left.
        - When none has, and no ready model has room, every ready model is asked, in
          the chain's order, and none is said to be without budget: a request too large
          by the guess is asked all the same, and its model's own answer about its size
          is truer than a wait. A guess never refuses a request.

        Then the breakers, within each group the budget's rule made: those with budget,
        those without, or every ready model when none has room nor budget. Of each, the
        models that `failing` doesn't name are asked first, and those it names after
        them, each in the chain's order. A failing model with budget is asked before a
        model without, whatever that one's breaker says. It never refuses a request,
        and never keeps one from being refused: a failing model counts for the budget's
        rule as any other does. `Asked.failing` names those it moved.

        A model passed over for its room is not also said to be without budget, and its
        wait is never the refusal's. A model that isn't ready is passed over whatever its
        room or its budget, and never counts as one that has either, nor as one whose
        wait is the refusal's. A chain with no ready model has none to ask, and is no
        refusal: its request is nobody's to refuse here."""
        ready = self.ready
        roomy = [(offered, status) for offered, status in ready if tokens <= offered.room]
        left = roomy or ready
        too_small = tuple(offered for offered, _ in ready if tokens > offered.room) if roomy else ()
        budgeted = tuple((offered, status) for offered, status in left if offered.name not in spent)
        unbudgeted = tuple((offered, status) for offered, status in left if offered.name in spent)
        over = tuple((offered, spent[offered.name]) for offered, _ in unbudgeted)
        if budgeted:
            unmoved = (*budgeted, *unbudgeted)
            asked = (*_failing_last(budgeted, failing), *_failing_last(unbudgeted, failing))
            return Asked(asked, too_small, over, failing=_moved(unmoved, asked, failing))
        if roomy:
            # Of two as short, the first in the chain's order: `min` keeps the first.
            shortest = min((short for _, short in over), key=lambda short: short.wait)
            return Asked((), too_small, over, refused=shortest)
        asked = _failing_last(left, failing)
        return Asked(asked, too_small, failing=_moved(tuple(left), asked, failing))


def _failing_last(
    models: Sequence[tuple[OfferedModel, ModelStatus]], failing: Mapping[str, Failing]
) -> tuple[tuple[OfferedModel, ModelStatus], ...]:
    """`models` in their order, those that `failing` names moved after the others."""
    return (
        *(each for each in models if each[0].name not in failing),
        *(each for each in models if each[0].name in failing),
    )


def _moved(
    unmoved: Sequence[tuple[OfferedModel, ModelStatus]],
    asked: Sequence[tuple[OfferedModel, ModelStatus]],
    failing: Mapping[str, Failing],
) -> tuple[tuple[OfferedModel, Failing], ...]:
    """Each failing model of `asked`, in its order, that is asked after a model that
    `unmoved`, the order without breakers, has after it: those the breakers moved."""
    place = {offered.name: number for number, (offered, _) in enumerate(unmoved)}
    return tuple(
        (offered, failing[offered.name])
        for number, (offered, _) in enumerate(asked)
        if offered.name in failing
        and any(place[before.name] > place[offered.name] for before, _ in asked[:number])
    )


class UnknownModelError(Exception):
    """No model on offer has this name."""


@dataclass(frozen=True, slots=True)
class _Mistake:
    """A mistake of Actio's own that an ask about a model's status raised, with its trace
    as it was when the ask caught it: each request that waits for the ask is raised the
    error with that trace, as `Future.result()` raises, and never with the frames of
    another request that was raised it before."""

    error: Exception
    trace: TracebackType | None


@dataclass(frozen=True, slots=True, eq=False)
class _Asking:
    """An ask about one model's status, in a task of its own: its server's answer, or the
    mistake of Actio's own that it raised, which the task returns and never raises.
    Those that need its answer wait for it (`Catalog._answers`), each through a future of
    its own, kept in `waiting` while it waits; those that don't need it don't wait, and
    its answer is kept for the requests after."""

    task: asyncio.Task[ModelStatus | _Mistake]
    waiting: set[asyncio.Future[ModelStatus | _Mistake]] = field(default_factory=set)

    @property
    def waited_for(self) -> bool:
        """Whether a request waits for its answer now. A waiter that was cancelled has
        stopped waiting at once, whenever it goes on."""
        return any(not each.done() for each in self.waiting)


class Catalog:
    """The models on offer. Iterating gives them in the file's order."""

    def __init__(
        self,
        models: Mapping[str, OfferedModel],
        *,
        default: str,
        thinking: Mapping[str, bool],
        titles: str | None = None,
    ) -> None:
        self._models = dict(models)
        self._declared_thinking = dict(thinking)
        self._statuses: dict[str, tuple[datetime, ModelStatus]] = {}
        """What each model's server last said of it, by the model's name, dated by the
        `now` of the request that had it asked, also when the answer came later."""
        self._asking: dict[str, _Asking] = {}
        """Each model's last ask about its status, by its name: under way, or ended. One
        at a time for a model: the next is started only once the last has ended. One
        that failed in Actio's own code takes itself off (`_asked`)."""
        self._loads: dict[str, asyncio.Task[None]] = {}
        """Each model's last load, by its name: under way, or ended."""
        self.default = default
        self.titles = titles
        """The name of the model that names conversations. `None`: none does."""

    def __iter__(self) -> Iterator[OfferedModel]:
        return iter(self._models.values())

    def get(self, name: str | None = None) -> OfferedModel:
        """The model called `name`, or the default one. Raises `UnknownModelError`."""
        try:
            return self._models[name or self.default]
        except KeyError:
            raise UnknownModelError(name) from None

    async def status(self, name: str, *, now: datetime) -> ModelStatus:
        """Whether the model's provider has it, and what the model can do: as
        `chain_status` says it, for the model by itself. An answer is reused for
        `STATUS_LIFETIME`, so asking often is cheap. Raises `UnknownModelError`."""
        [status] = await self._known([self.get(name)], now, uses_old=False)
        return status

    def chain(self, name: str | None = None) -> list[OfferedModel]:
        """The model called `name`, or the default one, then the models its `then` names,
        in order: those asked in its place when it can't answer before the first word.
        A stand-in's own `then` isn't followed. Raises `UnknownModelError`."""
        offered = self.get(name)
        return [offered, *(self.get(stand_in) for stand_in in offered.then)]

    async def chain_status(self, name: str | None = None, *, now: datetime) -> Chain:
        """The model's chain, each with what its provider says of it now: an answer
        younger than `STATUS_LIFETIME` at `now` is reused, by the `now` of the request
        that had it asked, and every other one is asked and waited for, all at the same
        time. For whoever says or does something by what a server says now: the list of
        models, the warm-up, the title job, and any new caller, unless it is a turn's
        start (`chain_as_known`).

        One ask about a model is under way at a time: a request that needs a status
        whose ask is under way starts none, and waits for that same ask. A request that
        stops waiting, by being cancelled, leaves the ask to go on, for whoever else
        waits and for the requests after. An ask that fails in Actio's own code, by
        raising something that isn't an `LLMError`, raises it to each request that
        waits for it; with none waiting, the log says it, with its trace, and what was
        known stays.

        Raises `UnknownModelError`."""
        chain = self.chain(name)
        statuses = await self._known(chain, now, uses_old=False)
        return Chain(tuple(zip(chain, statuses, strict=True)))

    async def chain_as_known(self, name: str | None = None, *, now: datetime) -> Chain:
        """The model's chain, each with its status, by what is known of it where that
        will do: only for a request that a person waits for, whose models are then asked
        through the walk, which moves on to the next model when an old `ready` was
        wrong. Today a turn's start, its one caller. The name says what sets it apart
        from `chain_status`, which asks.

        By the age of what each model's provider said at `now`, by the `now` of the
        request that had it asked:

        1. **Younger than `STATUS_LIFETIME`:** reused, and nobody is asked.
        2. **Older, and younger than `STATUS_USED_FOR`:** used without waiting, and its
           provider is asked again behind the request. The answer replaces what is
           known when it comes, for the requests after.
        3. **But nothing is refused by an old status:** when, by what is known and what
           the request waited for anyway, no model of the chain is ready, those whose
           status was old are waited for, at the same time, and the chain is as their
           answers say.
        4. **Nothing known, or `STATUS_USED_FOR` old or older:** asked and waited for.

        Every status that isn't fresh is asked for at once, when the request comes,
        those waited for and those not. The asks are those of `chain_status`, shared
        with it, and so is what they do when they fail.

        Raises `UnknownModelError`."""
        chain = self.chain(name)
        statuses = await self._known(chain, now, uses_old=True)
        return Chain(tuple(zip(chain, statuses, strict=True)))

    async def chains(self, *, now: datetime) -> dict[str, Chain]:
        """Every model's chain, by the model's name, in the file's order: each as
        `chain_status` gives it, all at the same time, so that a model in several chains
        is asked about once."""
        names = list(self._models)
        found = await asyncio.gather(*(self.chain_status(name, now=now) for name in names))
        return dict(zip(names, found, strict=True))

    async def _known(
        self, chain: Sequence[OfferedModel], now: datetime, *, uses_old: bool
    ) -> list[ModelStatus]:
        """The status of each model of `chain`, in its order: the one place where it is
        decided, for both ways. `uses_old` is a turn's start's (`chain_as_known`): an old
        status is used while a model of the chain is ready by what is known. Otherwise
        every status that isn't fresh is waited for (`chain_status`)."""
        old: dict[str, _Asking] = {}
        needed: dict[str, _Asking] = {}
        for offered in chain:
            said = self._statuses.get(offered.name)
            age = None if said is None else now - said[0]
            if age is not None and age < STATUS_LIFETIME:
                continue  # reused, and nobody is asked
            # Every status that isn't fresh is asked for now, whether or not it is waited
            # for: a request that turns out to need the old ones too has lost no time.
            asking = self._asked_about(offered, now)
            if uses_old and age is not None and age < STATUS_USED_FOR:
                old[offered.name] = asking
            else:
                needed[offered.name] = asking
        answers = await self._answers(needed)

        def known() -> list[ModelStatus]:
            # The answers waited for, and what is known of the others: an old status
            # whose ask has answered meanwhile is that answer by now.
            return [
                answers[offered.name]
                if offered.name in answers
                else self._statuses[offered.name][1]
                for offered in chain
            ]

        found = known()
        if old and not any(status.state == "ready" for status in found):
            # Nothing is refused by an old status: those are waited for.
            answers |= await self._answers(old)
            found = known()
        return found

    def _asked_about(self, offered: OfferedModel, now: datetime) -> _Asking:
        """The ask about `offered` that is under way, or one started now, its answer
        dated `now`: one at a time for a model, and once the last has ended, however it
        ended, the next request that needs one starts another."""
        asking = self._asking.get(offered.name)
        if asking is None or asking.task.done():
            task = asyncio.create_task(self._asked(offered, now), name=f"status-{offered.name}")
            asking = self._asking[offered.name] = _Asking(task)
        return asking

    async def _answers(self, asking: Mapping[str, _Asking]) -> dict[str, ModelStatus]:
        """The answer of each ask, by the model's name, all waited for at the same time.

        A waiter's cancellation ends its own wait, never an ask: others may wait for it,
        and its answer is worth having (awaiting a task would pass the cancellation into
        it, so each is waited for through a shield). Once every ask has ended, raises the
        first mistake of Actio's own that one of them raised, in the order given, with
        the trace it had then. A request is raised one error: the log says each other
        mistake, with its trace, as it says one that nobody waited for."""
        if not asking:
            return {}
        waits = {name: asyncio.shield(each.task) for name, each in asking.items()}
        for name, wait in waits.items():
            asking[name].waiting.add(wait)
        try:
            said = await asyncio.gather(*waits.values())
        finally:
            for name, wait in waits.items():
                asking[name].waiting.discard(wait)
        answers: dict[str, ModelStatus] = {}
        mistakes: list[tuple[str, _Mistake]] = []
        for name, answer in zip(waits, said, strict=True):
            if isinstance(answer, _Mistake):
                mistakes.append((name, answer))
            else:
                answers[name] = answer
        if mistakes:
            (_, first), *others = mistakes
            for name, other in others:
                logger.error(
                    "model %s: asking its server about it failed",
                    name,
                    exc_info=(type(other.error), other.error, other.trace),
                )
            raise first.error.with_traceback(first.trace)
        return answers

    async def _asked(self, offered: OfferedModel, dated: datetime) -> ModelStatus | _Mistake:
        """Ask `offered`'s server about it, and keep its answer, dated `dated`. Nobody may
        be waiting: what goes wrong ends here, and never escapes the task. A mistake of
        Actio's own is returned, with its trace, for whoever waits to raise; with none
        waiting, the log says it, and what was known stays. A cancellation gets out as
        it came, and keeps nothing either."""
        try:
            status = await self._ask(offered)
        except Exception as error:
            # An ask that failed so isn't kept as the model's last one: each request
            # that is raised the error adds its own frames to it, and those would stay,
            # with what they hold of that request, until the model's next ask.
            failed = self._asking.pop(offered.name)
            if not failed.waited_for:
                # Something of Actio's own, with its trace. An ask carries nobody's words.
                logger.exception("model %s: asking its server about it failed", offered.name)
            return _Mistake(error, error.__traceback__)
        self._statuses[offered.name] = (dated, status)
        return status

    def load_ahead(self, name: str) -> bool:
        """Start having the model called `name` loaded into its server's memory, ahead of
        the first question to it, and return at once. Raises `UnknownModelError`.

        Says whether a load was started, or is under way already: one at a time for a
        model, and once it has ended, well or not, the next call starts another. False
        when there is nothing to do: the model's server can't be asked to load it, or it
        is writing as many answers as it takes. The model that answers there is in
        memory already. A load of another one waited for the answer to end, when it
        was measured on Ollama, and then had the answer's model unloaded (ADR 0026).

        A load takes no place at the model's gate, and never waits there: a question to
        the same model asked meanwhile is asked at once, and waits inside the server for
        that load. It is asked with the model's context window, as a turn is. A load
        that fails is said in the log, and nowhere else.
        """
        offered = self.get(name)
        if not isinstance(offered.chat, LoadsAhead):
            return False
        last = self._loads.get(offered.name)
        if last is not None and not last.done():
            return True  # under way, whatever its server does meanwhile
        if offered.gate is not None and offered.gate.full:
            return False
        self._loads[offered.name] = asyncio.create_task(
            self._load(offered, offered.chat), name=f"load-{offered.name}"
        )
        return True

    async def stop_tasks(self) -> None:
        """Cancel every task the catalog has under way by itself, apart from the
        requests, and wait until each has ended: the loads ahead (`stop_loading`), and
        the asks about a model's status. For whoever closes the providers' clients, or
        stops using the catalog: nothing is left pending, and nothing is left to a
        closed client. One call for every kind, so that no place forgets one. A request
        that still waits for an ask is cancelled with it."""
        asking = [each.task for each in self._asking.values() if not each.task.done()]
        for task in asking:
            task.cancel()
        await asyncio.gather(self.stop_loading(), *asking, return_exceptions=True)

    async def stop_loading(self) -> None:
        """Cancel the loads under way, and wait until each has ended: the part of
        `stop_tasks` that stops loads."""
        loading = [task for task in self._loads.values() if not task.done()]
        for task in loading:
            task.cancel()
        await asyncio.gather(*loading, return_exceptions=True)

    async def _load(self, offered: OfferedModel, chat: LoadsAhead) -> None:
        """Nobody waits for this: what goes wrong ends here, and only the log hears of
        it. A cancellation gets out as it came."""
        try:
            await chat.load(offered.model, context_window=offered.context_window)
        except LLMError as error:
            # Its kind, never its text: that is the server's, and no log line needs it.
            logger.warning(
                "model %s: it wasn't loaded ahead (%s)", offered.name, type(error).__name__
            )
        except Exception:
            # Something of Actio's own, with its trace. A load carries nobody's words.
            logger.exception("model %s: loading it ahead failed", offered.name)

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


def counted(
    offered: OfferedModel, budgets: Budgets, tokens: int, clock: Callable[[], datetime]
) -> ChatProvider:
    """The client to ask `offered` through, for requests whose prompt is about `tokens`
    by Actio's guess, which its caller made once (ADR 0030). The model's own client when
    it has no budget, and Valkey is asked nothing for it. Otherwise one around it, which
    counts in the model's budget what each request takes:

    - when its `stream` is called, which the walk does once the model's gate is passed,
      and once for each try: one request from each bucket of requests, and the guess
      from each bucket of tokens;
    - when the stream is over, however it ended, the tokens are put right. After a
      `Finish` with the provider's count, by that count, the prompt's cached part and
      the answer included. After a `ConnectionFailedError` nothing was sent, and the
      request and the guess are given back. After anything else, another error, a
      `Finish` without a count, or a stream closed before its end by a stop, a cut or a
      cancellation, the guess stays, and a guess at what was handed on of the answer,
      its text and its reasoning, is added.

    Each is dated by `clock`, when it happens. The model's events are handed on as they
    come, and its errors as they came, the same objects: the walk sorts them by kind.
    It never fails an answer: when Valkey doesn't answer, the log says so, by the
    error's kind, and the stream goes on as if the model had no budget. After a take
    that couldn't be counted, nothing is put right. A stream that a stop or a
    cancellation closes puts its tokens right all the same.

    `OfferedModel.chat` stays the bare client: whatever else is asked of a model's
    server, whether it has the model, or a load ahead, isn't counted."""
    if offered.budget is None:
        return offered.chat
    return _Counted(offered.name, offered.chat, offered.budget, budgets, tokens, clock)


class _Counted:
    """A model's client that counts what each request takes of its budget: see
    `counted`."""

    def __init__(
        self,
        name: str,
        chat: ChatProvider,
        budget: Budget,
        budgets: Budgets,
        tokens: int,
        clock: Callable[[], datetime],
    ) -> None:
        self._name = name
        self._chat = chat
        self._budget = budget
        self._budgets = budgets
        self._tokens = tokens
        self._clock = clock

    async def stream(self, request: ChatRequest) -> AsyncGenerator[StreamEvent]:
        counting = await self._add(requests=1, tokens=self._tokens)
        written: list[str] = []
        finish: Finish | None = None
        sent = True
        try:
            async with aclosing(self._chat.stream(request)) as events:
                async for event in events:
                    if isinstance(event, TextDelta | ReasoningDelta):
                        written.append(event.text)
                    elif isinstance(event, Finish):
                        finish = event
                    yield event
        except ConnectionFailedError:
            sent = False
            raise
        finally:
            if counting:
                # Shielded: a halted turn cancels whatever its stream waits for, and an
                # unshielded wait for Valkey would be cancelled with it.
                with anyio.CancelScope(shield=True):
                    if not sent:  # nothing reached the server
                        await self._add(requests=-1, tokens=-self._tokens)
                    else:
                        await self._add(requests=0, tokens=self._more(finish, written))

    def _more(self, finish: Finish | None, written: list[str]) -> int:
        """How many tokens a request that was sent took beyond the guess that was taken
        for it, or fewer when negative."""
        if finish is not None and finish.usage is not None:
            return finish.usage.input_tokens + finish.usage.output_tokens - self._tokens
        return tokens_in("".join(written))

    async def _add(self, *, requests: int, tokens: int) -> bool:
        """Add `requests` to the budget's buckets of requests and `tokens` to those of
        tokens, now: taken, or given back when negative. Whether it was counted: not
        when Valkey didn't answer, which the log says."""
        amounts = [
            (bucket, amount)
            for bucket, amount in self._budget.takes(requests=requests, tokens=tokens)
            if amount
        ]
        if not amounts:
            return True
        try:
            await self._budgets.add(*amounts, now=self._clock())
        except BudgetsUnavailableError as error:
            logger.warning(
                "model %s: what a request to it took of its budget couldn't be counted (%s)",
                self._name,
                type(error.__cause__).__name__,
            )
            return False
        return True


def asked_through(
    offered: OfferedModel, budgets: Budgets, tokens: int, clock: Callable[[], datetime]
) -> ChatProvider:
    """The client a turn or a title asks `offered` through, in a link of its walk: one
    that records each try of it in its breaker (ADR 0031), around the one `counted`
    gives, which counts what each request takes of its budget, from `tokens`, the guess
    its caller made once (ADR 0030). One call gives both, so that no caller has one
    without the other.

    A try begins when its `stream` is called, which the walk does once the model's gate
    is passed, and once for each try; and it ends when that stream is over, however it
    ended, each by `clock`. It ends:

    - **failed,** when the stream raised an error that another try may not meet again
      (`FOR_NOW`), before anything was handed on or after it; or ended without its
      `Finish`, whether or not anything was handed on;
    - **answered,** otherwise, when something was handed on (`actio_llm.hands_on`): it
      finished, or was closed before its end by a stop, a cut or a cancellation; and
      when its answer was cut at its server's time (`TookTooLongError`) after something
      was handed on, which anyone can ask for on purpose;
    - **with nothing to say of the model,** otherwise: it was closed or cancelled before
      anything was handed on, it raised an `LLMError` that is the request's own, such as
      a request too large or refused, a model its server hasn't got or a server too old,
      or it raised anything else.

    The same end of a try also says what its provider said of when it takes requests
    again (`OfferedModel.own_wait`), dated when the try ended:

    - **a 429 whose wait is kept** (`RateLimitedError`, `believed`): the wait, until
      it is over, whatever was kept before it. Meanwhile the model counts as having no
      budget left (`Chain.with_own_waits`). It is still a failure of the breaker's;
    - **a try that ended answered,** by the rule above, while a wait was still running:
      the wait is over, unless it was noted after that try began. An answer to a
      request sent before a refusal was heard says nothing of the refusal's wait;
    - **nothing else changes it:** a 429 that named no wait, or one the walk waits out
      itself, any other error, a try closed before anything was handed on.

    The model's events are handed on as they come, and its errors as they came, the
    same objects: the walk sorts them by kind, and the counted client inside reads them
    too. What the end of a try changed of the breaker, and then of the provider's wait,
    is said in the log, a warning each, by the model's name and numbers alone.
    `OfferedModel.chat` stays the bare client: whether a server has a model, and a load
    ahead, are recorded nowhere."""
    return _Recorded(
        offered.name,
        counted(offered, budgets, tokens, clock),
        offered.breaker,
        offered.own_wait,
        clock,
    )


class _Recorded:
    """A model's client that records each try of it in its breaker, and notes the wait
    its provider names: see `asked_through`."""

    def __init__(
        self,
        name: str,
        chat: ChatProvider,
        breaker: Breaker,
        own_wait: OwnWait,
        clock: Callable[[], datetime],
    ) -> None:
        self._name = name
        self._chat = chat
        self._breaker = breaker
        self._own_wait = own_wait
        self._clock = clock

    async def stream(self, request: ChatRequest) -> AsyncGenerator[StreamEvent]:
        # How many of the provider's waits had been noted when this try began: its
        # answer ends no wait that was noted after that.
        words = self._own_wait.words
        call = self._breaker.begins(self._clock())
        handed_on = False
        finished = False
        outcome: Literal["answered", "failed"] | None = None
        wait: timedelta | None = None
        try:
            async with aclosing(self._chat.stream(request)) as events:
                async for event in events:
                    handed_on = handed_on or hands_on(event)
                    finished = finished or isinstance(event, Finish)
                    yield event
            # An answer that ended without saying why is a failure, words or not.
            outcome = "answered" if finished else "failed"
        except FOR_NOW as error:
            # Cut at its time after something was handed on, it is an answer.
            cut = handed_on and isinstance(error, TookTooLongError)
            outcome = "answered" if cut else "failed"
            if isinstance(error, RateLimitedError):
                wait = believed(error.retry_after_s)
            raise
        except BaseException:
            # A stop, a cut, a cancellation, the request's own refusal, or anything else:
            # an answer if something of it was handed on, and otherwise nothing to say.
            outcome = "answered" if handed_on else None
            raise
        finally:
            # Every ending, once, by one moment, and nothing awaited: a stop or a
            # cancellation that closes the stream records it too.
            ended = self._clock()
            self._said(call.ends(outcome, ended))
            self._heard(wait, outcome, ended, words)

    def _said(self, change: Change | None) -> None:
        """Say in the log what the end of a try changed of the breaker: names and
        numbers alone."""
        match change:
            case Opened(failed=failed, calls=calls):
                logger.warning(
                    "model %s: %d of its last %d requests failed, and for %d s it is asked"
                    " after models that aren't failing",
                    self._name,
                    failed,
                    calls,
                    change.seconds,
                )
            case OpenedAgain():
                logger.warning(
                    "model %s: it failed again, and for %d s more it is asked after models"
                    " that aren't failing",
                    self._name,
                    change.seconds,
                )
            case Closed():
                logger.warning("model %s: it answered, and is asked in its place again", self._name)
            case None:
                pass

    def _heard(
        self,
        wait: timedelta | None,
        outcome: Literal["answered", "failed"] | None,
        ended: datetime,
        words: int,
    ) -> None:
        """Note what the end of a try said of the provider's own wait, at `ended`, and
        say it in the log: names and numbers alone. `words` is how many of the provider's
        waits had been noted when the try began (`OwnWait.words`)."""
        if wait is not None:
            self._own_wait.told(ended + wait)
            logger.warning(
                "model %s: its provider said to wait %d s, and until then it counts as having"
                " no budget left",
                self._name,
                _whole_seconds(wait),
            )
        elif outcome == "answered" and self._own_wait.over(ended, words=words):
            logger.warning(
                "model %s: it answered before its provider's wait was over, and counts as"
                " having budget again",
                self._name,
            )


@asynccontextmanager
async def build(
    file: CatalogFile,
    *,
    ollama_url: str,
    transports: Mapping[str, httpx2.AsyncBaseTransport] | None = None,
) -> AsyncIterator[Catalog]:
    """The catalog a checked file describes. On the way out, the loads and the asks about
    a status under way are cancelled (`Catalog.stop_tasks`), and then its providers'
    clients are closed.

    `ollama_url` is where Ollama providers are, unless the file gives one its own `url`.
    `transports` replaces how a provider is reached, by the provider's name: tests pass
    a fake server there.
    """
    transports = transports or {}
    async with AsyncExitStack() as stack:
        ollamas: dict[str, Ollama] = {}
        clients: dict[str, httpx2.AsyncClient] = {}
        gates = {
            name: Gate(provider.at_once) if provider.at_once is not None else None
            for name, provider in file.providers.items()
        }
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
                ollamas[name] = Ollama(client, timeouts=provider.timeouts)
            else:
                clients[name] = client

        def chat_of(entry: ModelEntry) -> ChatProvider:
            """The one client of an Ollama, for every model on it. For a server that
            speaks OpenAI's API, a model's own, over its provider's HTTP client: what
            the model is told about reasoning is its own."""
            if entry.provider in ollamas:
                return ollamas[entry.provider]
            return OpenAICompatible(
                clients[entry.provider],
                provider=entry.provider,
                timeouts=file.providers[entry.provider].timeouts,
                thinking_off=entry.thinking_off,
                thinking_on=entry.thinking_on,
            )

        # One for each model on a provider: two models of the file that are one model on
        # one provider fail together, and are told to wait together, as they share a
        # budget.
        breakers = {(entry.provider, entry.model): Breaker() for entry in file.models.values()}
        own_waits = {(entry.provider, entry.model): OwnWait() for entry in file.models.values()}
        models = {
            name: OfferedModel(
                name=name,
                title=entry.title,
                kind=file.providers[entry.provider].kind,
                model=entry.model,
                context_window=entry.context_window,
                max_output_tokens=entry.max_output_tokens,
                chat=chat_of(entry),
                gate=gates[entry.provider],
                then=entry.then,
                tokens_a_minute=entry.tokens_a_minute,
                budget=budget_of(entry.provider, entry.model, entry.stated),
                breaker=breakers[entry.provider, entry.model],
                own_wait=own_waits[entry.provider, entry.model],
            )
            for name, entry in file.models.items()
        }
        declared = {name: bool(entry.thinking) for name, entry in file.models.items()}
        catalog = Catalog(models, default=file.default, thinking=declared, titles=file.titles)
        # After the clients, so it is done before they are closed: a load or an ask about
        # a status under way is given up on while its connection is still there.
        stack.push_async_callback(catalog.stop_tasks)
        yield catalog


@asynccontextmanager
async def open_catalog(settings: Settings) -> AsyncIterator[Catalog]:
    """The catalog that `settings.models_file` describes."""
    file = read_file(settings.models_file)
    async with build(file, ollama_url=settings.ollama_url) as catalog:
        yield catalog
