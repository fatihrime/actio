"""The models on offer: reading the file, building the providers, asking about a model.

No model server runs here. Ollama is stood in for by a function that answers its
`/api/show` the way the real server was recorded to (see `packages/llm/tests/fixtures`),
and an OpenAI-compatible server by fakellm.
"""

import json
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest

from actio import catalog as catalogs
from actio.catalog import CatalogError, CatalogFile, ModelStatus, UnknownModelError, read_file
from actio_fakellm.app import create_app as create_fake_llm
from actio_llm import Ollama, OpenAICompatible

pytestmark = pytest.mark.anyio

ROOT = Path(__file__).resolve().parents[3]
NOON = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)

GOOD = """
default = "standard"

[providers.ollama]
kind = "ollama"

[providers.lab]
kind = "openai-compatible"
url = "http://lab.internal:8000/v1"

[models.standard]
title = "Qwen 3.5 4B"
provider = "ollama"
model = "qwen3.5:4b-q4_K_M"
context_window = 8192
max_output_tokens = 2048

[models.big]
title = "The lab's large model"
provider = "lab"
model = "fake-chat"
context_window = 32768
max_output_tokens = 4096
thinking = true
"""


def written(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "models.toml"
    path.write_text(text)
    return path


def parsed(text: str) -> CatalogFile:
    return CatalogFile.model_validate(tomllib.loads(text))


class FakeOllama:
    """Answers `POST /api/show` like an Ollama server that has `models`: each a name and
    what it can do. Remembers what it was asked, and at which address."""

    def __init__(self, models: dict[str, list[str]]) -> None:
        self.models = models
        self.asked: list[str] = []
        self.urls: list[str] = []
        self.transport = httpx2.MockTransport(self._answer)

    def _answer(self, request: httpx2.Request) -> httpx2.Response:
        name = json.loads(request.content)["model"]
        self.asked.append(name)
        self.urls.append(str(request.url))
        if name not in self.models:
            return httpx2.Response(404, json={"error": f"model '{name}' not found"})
        return httpx2.Response(
            200,
            json={
                "capabilities": self.models[name],
                "details": {"parameter_size": "4.0B", "quantization_level": "Q4_K_M"},
                "model_info": {"qwen35.context_length": 262144},
            },
        )


def unreachable() -> httpx2.MockTransport:
    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused", request=request)

    return httpx2.MockTransport(refuse)


# --- The file -------------------------------------------------------------------------


def test_the_file_in_the_repository_is_valid() -> None:
    """What `make up` and `make dev` start with."""
    file = read_file(ROOT / "config" / "models.toml")

    assert file.default in file.models
    standard = file.models["standard"]
    assert file.providers[standard.provider].kind == "ollama"
    assert standard.model == "qwen3.5:4b-q4_K_M"


def test_a_file_says_which_models_run_where(tmp_path: Path) -> None:
    file = read_file(written(tmp_path, GOOD))

    assert file.default == "standard"
    assert list(file.models) == ["standard", "big"]
    assert file.models["big"].thinking is True
    assert file.providers["lab"].url == "http://lab.internal:8000/v1"


def test_a_missing_file_says_where_it_looked_and_how_to_point_elsewhere(tmp_path: Path) -> None:
    missing = tmp_path / "nowhere" / "models.toml"

    with pytest.raises(CatalogError) as failure:
        read_file(missing)

    assert str(missing) in str(failure.value)
    assert "ACTIO_MODELS_FILE" in str(failure.value)


# Each mistake is the good file with one piece replaced, and what the error must mention.
MISTAKES = {
    "a default that isn't a model": (
        'default = "standard"',
        'default = "best"',
        "is not one of the models",
    ),
    "a model on a provider that isn't there": (
        'provider = "lab"',
        'provider = "cloud"',
        "models.big: provider",
    ),
    "an openai-compatible provider without an address": (
        'url = "http://lab.internal:8000/v1"\n',
        "",
        "needs a url",
    ),
    "an answer as large as the whole window": (
        "max_output_tokens = 2048",
        "max_output_tokens = 8192",
        "smaller than",
    ),
    "a misspelt setting": ("context_window = 8192", "contxt_window = 8192", "contxt_window"),
    "a name with a capital letter": ("[models.big]", "[models.Big]", "models.Big"),
    "thinking declared for a model on Ollama": (
        "max_output_tokens = 2048",
        "max_output_tokens = 2048\nthinking = true",
        "leave `thinking` out",
    ),
    "a key for Ollama": (
        'kind = "ollama"',
        'kind = "ollama"\napi_key_env = "KEY"',
        "takes no key",
    ),
    "a kind of provider nobody wrote": (
        'kind = "ollama"',
        'kind = "llamafile"',
        "providers.ollama.kind",
    ),
    "an address that isn't one": ('url = "http://lab', 'url = "lab', "providers.lab.url"),
    "a number written as a word": (
        "context_window = 32768",
        'context_window = "large"',
        "models.big.context_window",
    ),
    "the same model twice": (
        "[models.big]",
        "[models.standard]",
        "valid TOML",
    ),
    "a line that stops half-way": ('default = "standard"', "default = ", "valid TOML"),
}


@pytest.mark.parametrize("mistake", MISTAKES)
def test_a_mistake_in_the_file_is_named(tmp_path: Path, mistake: str) -> None:
    right, wrong, fault = MISTAKES[mistake]
    assert right in GOOD
    path = written(tmp_path, GOOD.replace(right, wrong))

    with pytest.raises(CatalogError) as failure:
        read_file(path)

    assert str(path) in str(failure.value)
    assert fault in str(failure.value)


def test_a_file_without_models_offers_nothing_and_says_so(tmp_path: Path) -> None:
    path = written(tmp_path, GOOD.split("[models.standard]")[0])

    with pytest.raises(CatalogError, match="models"):
        read_file(path)


# --- Finding a model ------------------------------------------------------------------


async def test_a_model_is_found_by_its_name_and_the_default_when_none_is_named() -> None:
    async with catalogs.build(parsed(GOOD), ollama_url="http://ollama.internal:11434") as catalog:
        standard, big = catalog.get("standard"), catalog.get("big")

        assert catalog.default == "standard"
        assert catalog.get() is catalog.get(None) is standard
        assert [offered.name for offered in catalog] == ["standard", "big"]  # the file's order
        assert (standard.title, standard.kind, standard.model) == (
            "Qwen 3.5 4B",
            "ollama",
            "qwen3.5:4b-q4_K_M",
        )
        assert (standard.context_window, standard.max_output_tokens) == (8192, 2048)
        assert isinstance(standard.chat, Ollama)
        assert isinstance(big.chat, OpenAICompatible)
        with pytest.raises(UnknownModelError):
            catalog.get("largest")


# --- Reaching the providers -----------------------------------------------------------


async def test_ollama_is_where_the_setting_says_unless_the_file_gives_it_an_address() -> None:
    here, elsewhere = FakeOllama({"qwen3.5:4b-q4_K_M": []}), FakeOllama({"qwen3.5:9b": []})
    file = parsed(
        GOOD
        + """
[providers.workstation]
kind = "ollama"
url = "http://workstation.internal:11434"

[models.smart]
title = "Qwen 3.5 9B"
provider = "workstation"
model = "qwen3.5:9b"
context_window = 8192
max_output_tokens = 2048
"""
    )
    transports = {"ollama": here.transport, "workstation": elsewhere.transport}

    async with catalogs.build(
        file, ollama_url="http://host.docker.internal:11434", transports=transports
    ) as catalog:
        await catalog.status("standard", now=NOON)
        await catalog.status("smart", now=NOON)

    assert here.urls == ["http://host.docker.internal:11434/api/show"]
    assert elsewhere.urls == ["http://workstation.internal:11434/api/show"]


async def test_a_providers_key_comes_from_the_environment_variable_the_file_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LAB_API_KEY", "a-made-up-key")
    seen: list[str | None] = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.headers.get("authorization"))
        return httpx2.Response(200, json={"object": "list", "data": [{"id": "fake-chat"}]})

    file = parsed(
        GOOD.replace('url = "http://lab', 'api_key_env = "LAB_API_KEY"\nurl = "http://lab')
    )

    async with catalogs.build(
        file, ollama_url="http://127.0.0.1:1", transports={"lab": httpx2.MockTransport(answer)}
    ) as catalog:
        status = await catalog.status("big", now=NOON)

    assert seen == ["Bearer a-made-up-key"]
    assert status.state == "ready"


async def test_a_key_that_is_not_in_the_environment_stops_the_catalog_from_opening(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LAB_API_KEY", raising=False)
    file = parsed(
        GOOD.replace('url = "http://lab', 'api_key_env = "LAB_API_KEY"\nurl = "http://lab')
    )

    with pytest.raises(CatalogError) as failure:
        async with catalogs.build(file, ollama_url="http://127.0.0.1:1"):
            pytest.fail("it opened")

    assert "providers.lab: the environment variable LAB_API_KEY" in str(failure.value)


# --- Asking about a model -------------------------------------------------------------


@pytest.mark.parametrize(
    ("has", "expected"),
    [
        ({"qwen3.5:4b-q4_K_M": ["completion", "tools", "thinking"]}, ModelStatus("ready", True)),
        ({"qwen3.5:4b-q4_K_M": ["completion", "tools"]}, ModelStatus("ready", False)),
        ({"granite4:micro": ["completion"]}, ModelStatus("missing", False)),  # not pulled
    ],
)
async def test_ollama_says_whether_it_has_a_model_and_whether_the_model_can_reason(
    has: dict[str, list[str]], expected: ModelStatus
) -> None:
    ollama = FakeOllama(has)

    async with catalogs.build(
        parsed(GOOD), ollama_url="http://ollama", transports={"ollama": ollama.transport}
    ) as catalog:
        status = await catalog.status("standard", now=NOON)

    assert status == expected
    assert ollama.asked == ["qwen3.5:4b-q4_K_M"]


async def test_a_server_that_does_not_answer_is_unreachable_not_an_error() -> None:
    transports = {"ollama": unreachable(), "lab": unreachable()}

    async with catalogs.build(
        parsed(GOOD), ollama_url="http://ollama", transports=transports
    ) as catalog:
        statuses = await catalog.statuses(now=NOON)

    assert statuses == {
        "standard": ModelStatus("unreachable", False),
        "big": ModelStatus("unreachable", False),
    }


@pytest.mark.parametrize(
    ("served", "expected"),
    [
        (["fake-chat", "fake-embed"], ModelStatus("ready", True)),  # `thinking = true` in the file
        (["some-other-model"], ModelStatus("missing", True)),
    ],
)
async def test_an_openai_compatible_server_is_asked_for_its_list_of_models(
    served: list[str], expected: ModelStatus
) -> None:
    fake = httpx2.ASGITransport(app=create_fake_llm(models=served))

    async with catalogs.build(
        parsed(GOOD), ollama_url="http://ollama", transports={"lab": fake}
    ) as catalog:
        assert await catalog.status("big", now=NOON) == expected


async def test_an_answer_about_a_model_is_reused_for_a_quarter_of_a_minute() -> None:
    ollama = FakeOllama({})

    async with catalogs.build(
        parsed(GOOD), ollama_url="http://ollama", transports={"ollama": ollama.transport}
    ) as catalog:
        first = await catalog.status("standard", now=NOON)
        ollama.models["qwen3.5:4b-q4_K_M"] = ["completion", "thinking"]  # `ollama pull` finished
        soon = await catalog.status("standard", now=NOON + timedelta(seconds=14.9))
        later = await catalog.status("standard", now=NOON + timedelta(seconds=15))
        again = await catalog.status("standard", now=NOON + timedelta(seconds=20))

    assert (first.state, soon.state, later.state, again.state) == (
        "missing",
        "missing",
        "ready",
        "ready",
    )
    assert len(ollama.asked) == 2


async def test_every_model_is_asked_about_at_once() -> None:
    ollama = FakeOllama({"qwen3.5:4b-q4_K_M": ["completion", "thinking"]})
    transports: dict[str, Any] = {
        "ollama": ollama.transport,
        "lab": httpx2.ASGITransport(app=create_fake_llm()),
    }

    async with catalogs.build(
        parsed(GOOD), ollama_url="http://ollama", transports=transports
    ) as catalog:
        statuses = await catalog.statuses(now=NOON)

    assert statuses == {
        "standard": ModelStatus("ready", True),
        "big": ModelStatus("ready", True),
    }
