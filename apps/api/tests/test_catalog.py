"""The models on offer: reading the file, building the providers, asking about a model.

No model server runs here. Ollama is stood in for by a function that answers its
`/api/show` the way the real server was recorded to (see `packages/llm/tests/fixtures`),
and an OpenAI-compatible server by fakellm.
"""

import asyncio
import json
import tomllib
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import aclosing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import anyio
import httpx2
import pytest
from valkey.asyncio import Valkey
from valkey.exceptions import ValkeyError

from actio import catalog as catalogs
from actio.catalog import (
    LIMITS,
    Asked,
    Budget,
    CatalogError,
    CatalogFile,
    Chain,
    Gate,
    ModelStatus,
    OfferedModel,
    Spent,
    UnknownModelError,
    budget_of,
    counted,
    kept,
    read_file,
)
from actio.core.budget import Bucket, Budgets, BudgetsUnavailableError
from actio_fakellm.app import DEFAULT_MODELS as FAKE_MODELS
from actio_fakellm.app import QUIET
from actio_fakellm.app import create_app as create_fake_llm
from actio_llm import (
    ChatProvider,
    ChatRequest,
    Finish,
    FinishReason,
    Ollama,
    OpenAICompatible,
    ProviderUnavailableError,
    ReasoningDelta,
    StreamEvent,
    TextDelta,
    Timeouts,
    TookTooLongError,
    Usage,
    User,
    tokens_in,
)

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
    """What `make up` and `make dev` start with. The default model names conversations
    too: on a laptop it is the one that is loaded already."""
    file = read_file(ROOT / "config" / "models.toml")

    assert file.default in file.models
    standard = file.models["standard"]
    assert file.providers[standard.provider].kind == "ollama"
    assert file.providers[standard.provider].at_once == 1  # one answer at a time
    assert standard.model == "qwen3.5:4b-q4_K_M"
    assert file.titles == "standard"
    # Its server keeps the client's own time to begin an answer, which covers a cold load,
    # and its own time for a whole answer, five minutes.
    assert [provider.begins_within_seconds for provider in file.providers.values()] == [None]
    assert [provider.answers_within_seconds for provider in file.providers.values()] == [None]
    assert [provider.timeouts.answer_s for provider in file.providers.values()] == [300]
    # A local server takes as many tokens as the model holds: its room is its window. And
    # as many requests as come: it has no budget.
    assert [model.tokens_a_minute for model in file.models.values()] == [None]
    assert [model.stated for model in file.models.values()] == [dict.fromkeys(LIMITS)]


def test_the_file_of_the_end_to_end_stack_offers_the_fake_under_the_same_name() -> None:
    """What `make e2e` starts with. The web app asks for the default model, and must get
    one there too: the fake, which the tests script."""
    file = read_file(ROOT / "deploy" / "e2e" / "config" / "models.toml")

    assert file.default == read_file(ROOT / "config" / "models.toml").default
    standard = file.models[file.default]
    provider = file.providers[standard.provider]
    assert provider.kind == "openai-compatible"
    assert provider.url == "http://fakellm:8100/v1"  # the container's name and port
    assert standard.model in FAKE_MODELS
    assert standard.thinking is True  # so a test can ask for reasoning
    # Every answer is asked at once: the tests run side by side, on one fake.
    assert provider.at_once is None
    # The tests find a conversation in the list by its first question's words. The
    # model that names conversations there is the fake's quiet one, which says nothing
    # unless the question tells it what to: then they keep them.
    assert file.titles is not None
    assert file.models[file.titles].model == QUIET
    # The fake once more, behind a server given 3 seconds for a whole answer: a test
    # sends its question there to see an answer cut at its time (apps/web/e2e). The
    # other models keep the client's five minutes.
    hurried = file.models["hurried"]
    assert (hurried.model, hurried.thinking) == (standard.model, True)
    assert file.providers[hurried.provider].url == provider.url
    # And a model whose server isn't there: the fake's container at a port where nothing
    # listens, which refuses the connection at once. The fake under the default name
    # stands in for it, so that a test sees another model answer in its place.
    away = file.models["away"]
    nowhere = file.providers[away.provider]
    assert away.then == (file.default,)
    assert away.title not in {model.title for name, model in file.models.items() if name != "away"}
    # A window of its own, so that the test tells whose window the page divides by.
    assert away.context_window != standard.context_window
    assert (nowhere.kind, nowhere.url) == ("openai-compatible", "http://fakellm:8101/v1")
    # And the fake once more, with a budget of one request a minute, so that a test sees a
    # question refused for now (apps/web/e2e/no-budget.spec.ts). On a provider of its own
    # at the fake's address: on the fake's, it would share a budget with the models that
    # are the same model there, and they would have to state the same figures.
    rationed = file.models["rationed"]
    assert rationed.model == standard.model
    assert file.providers[rationed.provider].url == provider.url
    assert rationed.provider not in {
        model.provider for name, model in file.models.items() if name != "rationed"
    }
    assert rationed.stated == {
        "requests_a_minute": 1,
        "requests_a_day": None,
        "tokens_a_minute": None,
        "tokens_a_day": None,
    }
    assert {
        name: (entry.answers_within_seconds, entry.timeouts.answer_s)
        for name, entry in file.providers.items()
    } == {
        standard.provider: (None, 300),
        hurried.provider: (3, 3),
        away.provider: (None, 300),
        rationed.provider: (None, 300),
    }
    # The fake takes any number of tokens a minute: no model's room is less than its window.
    # Nor does it limit anything else: no other model has a budget.
    assert {model.tokens_a_minute for model in file.models.values()} == {None}
    assert {
        tuple(model.stated.values()) for name, model in file.models.items() if name != "rationed"
    } == {(None,) * 4}


async def test_the_example_that_offers_free_cloud_models_opens_with_their_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """config/examples/free-cloud, a pair of files to copy over config/'s: the shipped
    model, and Groq's and the Gemini API's beside it. Checked as the repository's own
    pair is, so that it can't rot. Its plans file is checked in test_plans.py."""
    monkeypatch.setenv("GROQ_API_KEY", "a-placeholder")
    monkeypatch.setenv("GEMINI_API_KEY", "a-placeholder")
    file = read_file(ROOT / "config" / "examples" / "free-cloud" / "models.toml")
    shipped = read_file(ROOT / "config" / "models.toml")

    async with catalogs.build(file, ollama_url="http://127.0.0.1:1") as catalog:
        offered = [(model.name, model.kind) for model in catalog]
        stand_ins = {model.name: model.then for model in catalog}
        chain = [model.name for model in catalog.chain("groq-large")]
        rooms = {model.name: model.room for model in catalog}
        kept_to = {
            model.name: None
            if model.budget is None
            else {
                str(bucket): (bucket.provider, bucket.model, bucket.size)
                for bucket in model.budget.buckets
            }
            for model in catalog
        }

    assert (file.default, file.titles) == (shipped.default, shipped.titles)
    assert file.models[file.default] == shipped.models[shipped.default]
    assert offered == [
        ("standard", "ollama"),
        ("groq-large", "openai-compatible"),
        ("groq-small", "openai-compatible"),
        ("gemini-flash", "openai-compatible"),
        ("gemini-lite", "openai-compatible"),
    ]
    # Groq's free plan refuses a request over its tokens a minute, and limits requests
    # a minute: then flash-lite answers, and the local model when that is too busy.
    assert chain == ["groq-large", "gemini-lite", "standard"]
    assert {name: then for name, then in stand_ins.items() if then} == {
        "groq-large": ("gemini-lite", "standard")
    }
    # A cloud server that hasn't begun an answer in half a minute has more likely hung
    # than is still loading: the model that stands in is asked sooner.
    assert {name: provider.begins_within_seconds for name, provider in file.providers.items()} == {
        "ollama": None,
        "groq": 30,
        "gemini": 30,
    }
    # Nothing measured says that a cloud server needs another time for a whole answer
    # than the client's five minutes.
    assert {
        name: (provider.answers_within_seconds, provider.timeouts.answer_s)
        for name, provider in file.providers.items()
    } == {"ollama": (None, 300), "groq": (None, 300), "gemini": (None, 300)}
    # Each cloud model's own window, and the tokens a minute its free plan takes: a
    # request larger than those isn't sent there while another model has room for it.
    assert {
        name: (model.context_window, model.tokens_a_minute) for name, model in file.models.items()
    } == {
        "standard": (8192, None),
        "groq-large": (131_072, 8_000),
        "groq-small": (131_072, 8_000),
        "gemini-flash": (1_048_576, 250_000),
        "gemini-lite": (1_048_576, 250_000),
    }
    assert rooms == {
        "standard": 8192,
        "groq-large": 8_000,
        "groq-small": 8_000,
        "gemini-flash": 250_000,
        "gemini-lite": 250_000,
    }
    # What each free plan takes in a minute and a day, as Groq's answers and its
    # documentation, and one free account's page of the Gemini API's, said on 2026-11-25:
    # a model with none left for a request is asked after those that have some.
    groq = {
        "requests_a_minute": 30,
        "requests_a_day": 1000,
        "tokens_a_minute": 8000,
        "tokens_a_day": 200_000,
    }
    assert {name: model.stated for name, model in file.models.items()} == {
        "standard": dict.fromkeys(LIMITS),
        "groq-large": groq,
        "groq-small": groq,
        "gemini-flash": {
            "requests_a_minute": 5,
            "requests_a_day": 20,
            "tokens_a_minute": 250_000,
            "tokens_a_day": None,
        },
        "gemini-lite": {
            "requests_a_minute": 15,
            "requests_a_day": 500,
            "tokens_a_minute": 250_000,
            "tokens_a_day": None,
        },
    }
    # Nine tenths of each, under each model's own name on its server.
    assert kept_to == {
        "standard": None,
        "groq-large": {
            "requests a minute": ("groq", "openai/gpt-oss-120b", 27),
            "requests a day": ("groq", "openai/gpt-oss-120b", 900),
            "tokens a minute": ("groq", "openai/gpt-oss-120b", 7_200),
            "tokens a day": ("groq", "openai/gpt-oss-120b", 180_000),
        },
        "groq-small": {
            "requests a minute": ("groq", "openai/gpt-oss-20b", 27),
            "requests a day": ("groq", "openai/gpt-oss-20b", 900),
            "tokens a minute": ("groq", "openai/gpt-oss-20b", 7_200),
            "tokens a day": ("groq", "openai/gpt-oss-20b", 180_000),
        },
        "gemini-flash": {
            "requests a minute": ("gemini", "gemini-3.6-flash", 4),
            "requests a day": ("gemini", "gemini-3.6-flash", 18),
            "tokens a minute": ("gemini", "gemini-3.6-flash", 225_000),
        },
        "gemini-lite": {
            "requests a minute": ("gemini", "gemini-3.5-flash-lite", 13),
            "requests a day": ("gemini", "gemini-3.5-flash-lite", 450),
            "tokens a minute": ("gemini", "gemini-3.5-flash-lite", 225_000),
        },
    }


def test_a_file_says_which_models_run_where(tmp_path: Path) -> None:
    file = read_file(written(tmp_path, GOOD))

    assert file.default == "standard"
    assert list(file.models) == ["standard", "big"]
    assert file.models["big"].thinking is True
    assert file.providers["lab"].url == "http://lab.internal:8000/v1"


async def test_a_file_names_the_model_that_writes_titles_or_none(tmp_path: Path) -> None:
    """Left out, no model does, and a conversation keeps its first question's first
    line."""
    named = parsed(GOOD.replace('default = "standard"', 'default = "standard"\ntitles = "big"'))

    async with (
        catalogs.build(named, ollama_url="http://ollama.internal:11434") as with_titles,
        catalogs.build(parsed(GOOD), ollama_url="http://ollama.internal:11434") as without,
    ):
        assert named.titles == "big"
        assert with_titles.titles == "big"
        assert read_file(written(tmp_path, GOOD)).titles is None
        assert without.titles is None


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
    "a model for titles that isn't one": (
        'default = "standard"',
        'default = "standard"\ntitles = "tiny"',
        'titles = "tiny" is not one of the models',
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
    "a server that writes no answers": (
        'url = "http://lab.internal:8000/v1"',
        'url = "http://lab.internal:8000/v1"\nanswers_at_once = 0',
        "providers.lab.answers_at_once",
    ),
    "a server that writes fewer than none": (
        'kind = "ollama"',
        'kind = "ollama"\nanswers_at_once = -1',
        "providers.ollama.answers_at_once",
    ),
    "a server given no time to begin an answer": (
        'url = "http://lab.internal:8000/v1"',
        'url = "http://lab.internal:8000/v1"\nbegins_within_seconds = 0',
        "providers.lab.begins_within_seconds",
    ),
    "a server given less than none": (
        'kind = "ollama"',
        'kind = "ollama"\nbegins_within_seconds = -0.5',
        "providers.ollama.begins_within_seconds",
    ),
    "a time to begin written as a word": (
        'url = "http://lab.internal:8000/v1"',
        'url = "http://lab.internal:8000/v1"\nbegins_within_seconds = "30"',
        "providers.lab.begins_within_seconds",
    ),
    "a time to begin that never ends": (
        'kind = "ollama"',
        'kind = "ollama"\nbegins_within_seconds = inf',
        "providers.ollama.begins_within_seconds",
    ),
    "a server given no time for an answer": (
        'url = "http://lab.internal:8000/v1"',
        'url = "http://lab.internal:8000/v1"\nanswers_within_seconds = 0',
        "providers.lab.answers_within_seconds",
    ),
    "a server given less than none for an answer": (
        'kind = "ollama"',
        'kind = "ollama"\nanswers_within_seconds = -300',
        "providers.ollama.answers_within_seconds",
    ),
    "a time for an answer written as a word": (
        'url = "http://lab.internal:8000/v1"',
        'url = "http://lab.internal:8000/v1"\nanswers_within_seconds = "300"',
        "providers.lab.answers_within_seconds",
    ),
    "a time for an answer that never ends": (
        'kind = "ollama"',
        'kind = "ollama"\nanswers_within_seconds = inf',
        "providers.ollama.answers_within_seconds",
    ),
    "a time for an answer that isn't a number": (
        'url = "http://lab.internal:8000/v1"',
        'url = "http://lab.internal:8000/v1"\nanswers_within_seconds = nan',
        "providers.lab.answers_within_seconds",
    ),
    "how to tell a model not to reason, for one that can't reason": (
        "thinking = true",
        'thinking = false\nthinking_off = { reasoning_effort = "low" }',
        "models.big: `thinking_off` is for a model that can reason",
    ),
    "how to tell a model to reason, for one that isn't said to": (
        "thinking = true",
        "thinking_on = { extra_body = { think = true } }",
        "models.big: `thinking_on` is for a model that can reason",
    ),
    "how to tell a model on Ollama to reason": (
        "max_output_tokens = 2048",
        'max_output_tokens = 2048\nthinking_on = { reasoning_effort = "high" }',
        "models.standard: leave `thinking_on` out for a model on Ollama",
    ),
    "something Actio writes into a request itself": (
        "thinking = true",
        "thinking = true\nthinking_off = { max_tokens = 16 }",
        "models.big: `thinking_off` can't hold `max_tokens`",
    ),
    "how to tell a model to reason, written as a word": (
        "thinking = true",
        'thinking = true\nthinking_on = "high"',
        "models.big.thinking_on",
    ),
    "how to tell a model to reason, with what JSON can't hold": (
        "thinking = true",
        "thinking = true\nthinking_on = { extra_body = { at = 1979-05-27 } }",
        "models.big.thinking_on",
    ),
    "a stand-in that isn't one of the models": (
        "thinking = true",
        'thinking = true\nthen = ["tiny"]',
        'models.big: `then` names "tiny", which is not one of the models',
    ),
    "a model that stands in for itself": (
        "thinking = true",
        'thinking = true\nthen = ["standard", "big"]',
        "models.big: `then` can't name the model itself",
    ),
    "a stand-in named twice": (
        "thinking = true",
        'thinking = true\nthen = ["standard", "standard"]',
        'models.big: `then` names "standard" twice',
    ),
    "tokens a minute written as a word": (
        "thinking = true",
        'thinking = true\ntokens_a_minute = "8000"',
        "models.big.tokens_a_minute",
    ),
    "tokens a minute that aren't whole": (
        "thinking = true",
        "thinking = true\ntokens_a_minute = 8000.5",
        "models.big.tokens_a_minute",
    ),
    "tokens a minute written as a fraction": (
        "max_output_tokens = 2048",
        "max_output_tokens = 2048\ntokens_a_minute = 8000.0",
        "models.standard.tokens_a_minute",
    ),
    "tokens a minute that are a yes": (
        "thinking = true",
        "thinking = true\ntokens_a_minute = true",
        "models.big.tokens_a_minute",
    ),
    "no tokens a minute": (
        "max_output_tokens = 2048",
        "max_output_tokens = 2048\ntokens_a_minute = 0",
        "models.standard.tokens_a_minute",
    ),
    "fewer tokens a minute than none": (
        "thinking = true",
        "thinking = true\ntokens_a_minute = -8000",
        "models.big.tokens_a_minute",
    ),
}
# The three other limits of a model's budget, each with the same six mistakes, on a model
# of either kind of server.
for _key in ("requests_a_minute", "requests_a_day", "tokens_a_day"):
    MISTAKES |= {
        f"{_key} written as a word": (
            "thinking = true",
            f'thinking = true\n{_key} = "30"',
            f"models.big.{_key}",
        ),
        f"{_key} that aren't whole": (
            "thinking = true",
            f"thinking = true\n{_key} = 30.5",
            f"models.big.{_key}",
        ),
        f"{_key} written as a fraction": (
            "max_output_tokens = 2048",
            f"max_output_tokens = 2048\n{_key} = 30.0",
            f"models.standard.{_key}",
        ),
        f"{_key} that are a yes": (
            "thinking = true",
            f"thinking = true\n{_key} = true",
            f"models.big.{_key}",
        ),
        f"no {_key}": (
            "max_output_tokens = 2048",
            f"max_output_tokens = 2048\n{_key} = 0",
            f"models.standard.{_key}",
        ),
        f"fewer {_key} than none": (
            "thinking = true",
            f"thinking = true\n{_key} = -30",
            f"models.big.{_key}",
        ),
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


# --- Models that stand in for another --------------------------------------------------

WITH_STAND_INS = (
    GOOD.replace(
        "max_output_tokens = 2048", 'max_output_tokens = 2048\nthen = ["big", "small"]'
    ).replace("thinking = true", 'thinking = true\nthen = ["standard"]')
    + """
[models.small]
title = "Granite 4 Micro"
provider = "ollama"
model = "granite4:micro"
context_window = 4096
max_output_tokens = 1024
then = []
"""
)
"""`standard` and `big` stand in for each other, and `small` for nobody, with an empty
list where it could have left `then` out."""


async def test_a_model_is_followed_by_those_its_file_names_and_never_by_theirs() -> None:
    """Only the asked model's list counts. So two models may name each other, and a
    model is never asked twice."""
    file = parsed(WITH_STAND_INS)

    async with catalogs.build(file, ollama_url="http://ollama") as catalog:
        chains = {
            offered.name: [model.name for model in catalog.chain(offered.name)]
            for offered in catalog
        }
        default = [model.name for model in catalog.chain()]
        then = catalog.get("standard").then

    assert chains == {
        "standard": ["standard", "big", "small"],
        "big": ["big", "standard"],  # not `small`, which only `standard` names
        "small": ["small"],
    }
    assert default == chains["standard"]
    assert then == ("big", "small")


async def test_a_model_without_stand_ins_is_its_own_chain() -> None:
    async with catalogs.build(parsed(GOOD), ollama_url="http://ollama") as catalog:
        chain = catalog.chain("big")

    assert chain == [catalog.get("big")]
    assert catalog.get("big").then == ()


@pytest.mark.parametrize(
    ("ollama_has", "lab_answers", "ready", "passed_over", "own"),
    [
        ({}, True, ["big"], ["standard", "small"], "missing"),
        (
            {"qwen3.5:4b-q4_K_M": ["completion"], "granite4:micro": ["completion", "thinking"]},
            False,
            ["standard", "small"],
            ["big"],
            "ready",
        ),
        (None, False, [], ["standard", "big", "small"], "unreachable"),
    ],
    ids=["the model missing", "a stand-in away", "every server away"],
)
async def test_a_chain_says_which_of_its_models_are_ready_in_order_with_their_status(
    ollama_has: dict[str, list[str]] | None,
    lab_answers: bool,
    ready: list[str],
    passed_over: list[str],
    own: str,
) -> None:
    """A model that isn't ready is passed over, missing or unreachable alike. What it can
    do is each one's own."""
    ollama = unreachable() if ollama_has is None else FakeOllama(ollama_has).transport
    lab = httpx2.ASGITransport(app=create_fake_llm()) if lab_answers else unreachable()

    async with catalogs.build(
        parsed(WITH_STAND_INS),
        ollama_url="http://ollama",
        transports={"ollama": ollama, "lab": lab},
    ) as catalog:
        chain = await catalog.chain_status("standard", now=NOON)

    can_reason = {"standard": False, "big": True, "small": True}
    assert [model.name for model, _ in chain.models] == ["standard", "big", "small"]
    assert [(model.name, status.thinking) for model, status in chain.ready] == [
        (name, can_reason[name]) for name in ready
    ]
    assert [model.name for model, _ in chain.passed_over] == passed_over
    assert chain.status.state == own


# --- A model's room, and who is asked for a request -----------------------------------

READY = ModelStatus("ready", thinking=False)
REASONS = ModelStatus("ready", thinking=True)
AWAY = ModelStatus("unreachable", thinking=False)
GONE = ModelStatus("missing", thinking=False)
NONE_SPENT: dict[str, Spent] = {}
"""What a look said when no model's budget is spent for a request: every one has budget."""


def offered(
    name: str,
    *,
    context_window: int = 8192,
    tokens_a_minute: int | None = None,
    budget: dict[str, int] | None = None,
    chat: Any = None,
) -> OfferedModel:
    """A model on offer whose server is never asked, unless it is given a `chat`: for the
    rules that only read it. `budget` is what a models file would state of its provider's
    limits, on a provider called `cloud`."""
    return OfferedModel(
        name=name,
        title=name.capitalize(),
        kind="openai-compatible",
        model=f"model-{name}",
        context_window=context_window,
        max_output_tokens=256,
        chat=chat,
        tokens_a_minute=tokens_a_minute,
        budget=None if budget is None else budget_of("cloud", f"model-{name}", budget),
    )


async def test_a_model_of_either_kind_of_server_may_say_how_many_tokens_it_takes_a_minute() -> None:
    """The figure reaches the model on offer. Nothing ties it to the window or the
    longest answer: either may be the smaller. Left out, the server has no such limit,
    and the model's room is its window."""
    file = parsed(
        GOOD.replace(
            "max_output_tokens = 2048", "max_output_tokens = 2048\ntokens_a_minute = 100000"
        ).replace("thinking = true", "thinking = true\ntokens_a_minute = 1000")
    )

    async with (
        catalogs.build(file, ollama_url="http://ollama") as catalog,
        catalogs.build(parsed(GOOD), ollama_url="http://ollama") as without,
    ):
        standard, big = catalog.get("standard"), catalog.get("big")
        left_out = [(model.tokens_a_minute, model.room) for model in without]

    assert {name: model.tokens_a_minute for name, model in file.models.items()} == {
        "standard": 100_000,
        "big": 1000,
    }
    assert (standard.kind, standard.tokens_a_minute, standard.room) == ("ollama", 100_000, 8192)
    assert (big.kind, big.tokens_a_minute, big.room) == ("openai-compatible", 1000, 1000)
    assert (big.context_window, big.max_output_tokens) == (32768, 4096)
    assert left_out == [(None, 8192), (None, 32768)]


@pytest.mark.parametrize(
    ("context_window", "tokens_a_minute", "room"),
    [(8192, 6000, 6000), (8192, 100_000, 8192), (8000, 8000, 8000), (8192, None, 8192)],
    ids=["smaller by its minute", "smaller by its window", "the two equal", "its window alone"],
)
def test_a_models_room_is_the_smaller_of_its_window_and_its_tokens_a_minute(
    context_window: int, tokens_a_minute: int | None, room: int
) -> None:
    model = offered("standard", context_window=context_window, tokens_a_minute=tokens_a_minute)

    assert model.room == room
    assert model.context_window == context_window  # a request still carries its window


def test_a_request_as_large_as_a_models_room_is_asked_of_it_and_one_token_more_is_not() -> None:
    small, large = offered("small", tokens_a_minute=8000), offered("large", context_window=131_072)
    chain = Chain(((small, READY), (large, READY)))

    assert chain.asked_with(8000, NONE_SPENT) == Asked(((small, READY), (large, READY)), ())
    assert chain.asked_with(8001, NONE_SPENT) == Asked(((large, READY),), (small,))


def test_while_some_ready_models_have_room_only_those_are_asked_in_the_chains_order() -> None:
    """Each with its own status. The others are passed over for their room, and listed
    in the chain's order too."""
    first = offered("first", tokens_a_minute=1000)
    second = offered("second", context_window=9000)
    third = offered("third", context_window=500)
    fourth = offered("fourth", context_window=1_048_576, tokens_a_minute=250_000)
    chain = Chain(((first, READY), (second, REASONS), (third, READY), (fourth, READY)))

    assert chain.asked_with(5000, NONE_SPENT) == Asked(
        ((second, REASONS), (fourth, READY)), (first, third)
    )


def test_when_no_ready_model_has_room_each_is_asked_and_none_is_passed_over() -> None:
    """A guess never refuses a request: it is asked as if no model had a room. A model
    alone is its own chain, and is asked whatever the request."""
    first, second = offered("first", tokens_a_minute=1000), offered("second", context_window=2000)

    assert Chain(((first, READY), (second, REASONS))).asked_with(5000, NONE_SPENT) == Asked(
        ((first, READY), (second, REASONS)), ()
    )
    assert Chain(((first, READY),)).asked_with(5000, NONE_SPENT) == Asked(((first, READY),), ())


def test_a_stand_in_without_room_is_passed_over_while_the_asked_model_is_asked() -> None:
    asked, stand_in = offered("standard"), offered("second", tokens_a_minute=1000)

    assert Chain(((asked, READY), (stand_in, READY))).asked_with(5000, NONE_SPENT) == Asked(
        ((asked, READY),), (stand_in,)
    )


def test_a_model_that_isnt_ready_never_counts_as_one_with_room() -> None:
    """Away or missing with the only room large enough, it doesn't keep the ready ones
    from all being asked. And with too little room, it is passed over for being away,
    not for its room: it isn't listed as too small."""
    small, smaller = offered("small", tokens_a_minute=1000), offered("smaller", context_window=2000)
    away, gone = offered("away", context_window=1_048_576), offered("gone", context_window=131_072)
    tiny, large = offered("tiny", context_window=1000), offered("large", context_window=131_072)

    roomy_but_not_ready = Chain(((small, READY), (away, AWAY), (smaller, READY), (gone, GONE)))
    tiny_and_away = Chain(((tiny, AWAY), (large, READY)))

    assert roomy_but_not_ready.asked_with(5000, NONE_SPENT) == Asked(
        ((small, READY), (smaller, READY)), ()
    )
    assert tiny_and_away.asked_with(5000, NONE_SPENT) == Asked(((large, READY),), ())


# --- A model's budget, and who is asked for it ----------------------------------------


async def test_each_limit_of_a_budget_is_read_for_a_model_of_either_kind_of_server() -> None:
    """Each the provider's own figure, of which a bucket keeps nine tenths, under the
    provider's name and the model's on its server. A model that states none has no
    budget, and one may state some and leave others out."""
    file = parsed(
        GOOD.replace(
            "max_output_tokens = 2048",
            "max_output_tokens = 2048\nrequests_a_minute = 30\ntokens_a_day = 200000",
        ).replace(
            "thinking = true", "thinking = true\nrequests_a_day = 1000\ntokens_a_minute = 8000"
        )
    )

    async with (
        catalogs.build(file, ollama_url="http://ollama") as catalog,
        catalogs.build(parsed(GOOD), ollama_url="http://ollama") as without,
    ):
        standard, big = catalog.get("standard").budget, catalog.get("big").budget
        left_out = [model.budget for model in without]

    assert standard is not None
    assert standard.buckets == (
        Bucket("requests", "minute", "ollama", "qwen3.5:4b-q4_K_M", 27),
        Bucket("tokens", "day", "ollama", "qwen3.5:4b-q4_K_M", 180_000),
    )
    assert big is not None
    assert big.buckets == (
        Bucket("requests", "day", "lab", "fake-chat", 900),
        Bucket("tokens", "minute", "lab", "fake-chat", 7_200),
    )
    assert left_out == [None, None]
    # The tokens a minute are the room's too, whole.
    assert catalog.get("big").room == 8_000


@pytest.mark.parametrize(
    ("figure", "kept_to"),
    [
        (30, 27),
        (1000, 900),
        (8000, 7_200),
        (200_000, 180_000),
        (5, 4),
        (20, 18),
        (15, 13),
        (500, 450),
        (250_000, 225_000),
        (2, 1),
        (1, 1),
    ],
)
def test_actio_keeps_to_nine_tenths_of_each_figure_rounded_down_and_one_at_the_least(
    figure: int, kept_to: int
) -> None:
    assert kept(figure) == kept_to
    budget = budget_of("cloud", "a-model", {"requests_a_day": figure})
    assert budget is not None
    assert [bucket.size for bucket in budget.buckets] == [kept_to]


def test_a_budget_is_of_the_limits_a_models_file_names_and_none_of_none() -> None:
    assert budget_of("cloud", "a-model", {}) is None
    assert budget_of("cloud", "a-model", dict.fromkeys(LIMITS)) is None
    with pytest.raises(ValueError, match="requests_a_week"):
        budget_of("cloud", "a-model", {"requests_a_week": 10})


AGAIN = """
[models.again]
title = "The lab's large model, once more"
provider = "lab"
model = "fake-chat"
context_window = 32768
max_output_tokens = 4096
"""
"""A third model: the same as `big`, on the same provider."""


@pytest.mark.parametrize("key", list(LIMITS))
@pytest.mark.parametrize(
    ("big_states", "again_states", "said"),
    [
        (30, 31, "is 31 here and 30 in models.big"),
        (30, None, "is left out here and 30 in models.big"),
    ],
    ids=["two figures", "one left out"],
)
def test_two_models_that_are_one_model_on_one_provider_state_the_same_budget(
    key: str, big_states: int, again_states: int | None, said: str, tmp_path: Path
) -> None:
    """They share one budget, so a difference is a mistake of the file, in one line that
    names the two models and the key."""
    text = GOOD.replace("thinking = true", f"thinking = true\n{key} = {big_states}") + AGAIN
    if again_states is not None:
        text += f"{key} = {again_states}\n"
    path = written(tmp_path, text)

    with pytest.raises(CatalogError) as failure:
        read_file(path)

    assert f"models.again: `{key}` {said}" in str(failure.value)
    assert "share one budget" in str(failure.value)


async def test_two_models_that_state_the_same_budget_share_it_and_two_providers_never_do() -> None:
    """The same model's name on two providers is two budgets, whatever each states."""
    text = (
        GOOD.replace("thinking = true", "thinking = true\nrequests_a_day = 1000")
        + AGAIN
        + "requests_a_day = 1000\n"
        + """
[providers.lab2]
kind = "openai-compatible"
url = "http://lab2.internal:8000/v1"

[models.elsewhere]
title = "The other lab's large model"
provider = "lab2"
model = "fake-chat"
context_window = 32768
max_output_tokens = 4096
requests_a_day = 20
"""
    )

    async with catalogs.build(parsed(text), ollama_url="http://ollama") as catalog:
        big, again, elsewhere = (catalog.get(name).budget for name in ("big", "again", "elsewhere"))

    assert big is not None
    assert elsewhere is not None
    assert big == again
    assert big.buckets == (Bucket("requests", "day", "lab", "fake-chat", 900),)
    assert elsewhere.buckets == (Bucket("requests", "day", "lab2", "fake-chat", 18),)


SPENT = Spent(timedelta(seconds=12), Bucket("tokens", "minute", "cloud", "model-x", 7_200))
"""What a look says of a model whose budget has nothing left for a request."""
SOONER = Spent(timedelta(seconds=5), Bucket("requests", "minute", "cloud", "model-y", 27))
"""The same, of a model whose budget has room for the request sooner."""
LATER = Spent(timedelta(hours=2), Bucket("requests", "day", "cloud", "model-z", 900))
"""The same, of a model whose budget has room for the request later."""


def test_a_model_with_no_budget_left_is_asked_after_those_that_have_some() -> None:
    """Each group in the chain's order: those with budget first, then those without, the
    asked model among them, each named with what the look said of it."""
    first, second = offered("first"), offered("second")
    third, fourth = offered("third"), offered("fourth")
    chain = Chain(((first, READY), (second, REASONS), (third, READY), (fourth, READY)))

    assert chain.asked_with(1000, {"first": SPENT, "third": LATER}) == Asked(
        ((second, REASONS), (fourth, READY), (first, READY), (third, READY)),
        (),
        ((first, SPENT), (third, LATER)),
    )


def test_when_no_model_with_room_has_budget_nobody_is_asked_and_the_request_is_refused() -> None:
    """For the shortest wait of theirs, whichever comes first in the chain, and each is
    named in the chain's order. A model alone is refused by its own wait."""
    first, second, third = offered("first"), offered("second"), offered("third")
    chain = Chain(((first, READY), (second, REASONS), (third, READY)))

    assert chain.asked_with(1000, {"first": LATER, "second": SOONER, "third": SPENT}) == Asked(
        (), (), ((first, LATER), (second, SOONER), (third, SPENT)), refused=SOONER
    )
    assert Chain(((first, READY),)).asked_with(1000, {"first": SPENT}) == Asked(
        (), (), ((first, SPENT),), refused=SPENT
    )


def test_of_two_waits_as_short_the_refusal_names_the_first_in_the_chain() -> None:
    first, second = offered("first"), offered("second")
    as_soon = Spent(SPENT.wait, SOONER.limit)
    chain = Chain(((first, READY), (second, READY)))

    asked = chain.asked_with(1000, {"first": SPENT, "second": as_soon})

    assert asked.refused == SPENT
    assert chain.asked_with(1000, {"first": as_soon, "second": SPENT}).refused == as_soon


def test_a_model_without_room_neither_shortens_the_wait_nor_keeps_the_refusal_away() -> None:
    """Passed over for its room, it is named for its room alone: with budget, it keeps
    nobody from being refused, and with a shorter wait, it doesn't shorten the one the
    request is refused for."""
    small, spent = offered("small", tokens_a_minute=500), offered("spent")
    chain = Chain(((small, READY), (spent, READY)))

    with_budget = chain.asked_with(1000, {"spent": SPENT})
    sooner_than_it = chain.asked_with(1000, {"small": SOONER, "spent": SPENT})

    assert with_budget == Asked((), (small,), ((spent, SPENT),), refused=SPENT)
    assert sooner_than_it == Asked((), (small,), ((spent, SPENT),), refused=SPENT)


def test_the_room_comes_first_and_a_model_without_room_is_said_for_its_room_alone() -> None:
    small, spent, fits = offered("small", tokens_a_minute=500), offered("spent"), offered("fits")
    chain = Chain(((small, READY), (spent, READY), (fits, READY)))

    assert chain.asked_with(1000, {"small": SPENT, "spent": SPENT}) == Asked(
        ((fits, READY), (spent, READY)), (small,), ((spent, SPENT),)
    )


def test_when_no_model_has_room_nor_budget_every_ready_one_is_asked_and_none_is_named() -> None:
    """A request too large by the guess is asked all the same, and nothing is refused:
    the model's own answer about its size is truer than a wait."""
    first, second = offered("first", context_window=500), offered("second", tokens_a_minute=500)
    away = offered("away", context_window=500)
    chain = Chain(((first, READY), (away, AWAY), (second, REASONS)))

    assert chain.asked_with(1000, {"first": SPENT, "second": SOONER, "away": SOONER}) == Asked(
        ((first, READY), (second, REASONS)), ()
    )


def test_when_no_model_has_room_the_one_with_budget_is_asked_first_and_the_others_after() -> None:
    first, second = offered("first", context_window=500), offered("second", tokens_a_minute=500)
    third = offered("third", context_window=500)
    chain = Chain(((first, READY), (second, READY), (third, READY)))

    assert chain.asked_with(1000, {"first": SPENT, "third": SOONER}) == Asked(
        ((second, READY), (first, READY), (third, READY)), (), ((first, SPENT), (third, SOONER))
    )


def test_a_model_that_isnt_ready_never_counts_as_one_with_budget_nor_for_its_wait() -> None:
    """With the only budget left, it doesn't keep the ready one that has none from being
    refused, it isn't named, and its shorter wait isn't the refusal's. A chain with no
    ready model has nobody to ask, and that is no refusal."""
    spent, away, gone = offered("spent"), offered("away"), offered("gone")
    chain = Chain(((spent, READY), (away, AWAY)))

    assert chain.asked_with(1000, {"spent": SPENT}) == Asked(
        (), (), ((spent, SPENT),), refused=SPENT
    )
    assert chain.asked_with(1000, {"spent": SPENT, "away": SOONER}) == Asked(
        (), (), ((spent, SPENT),), refused=SPENT
    )
    assert Chain(((away, AWAY), (gone, GONE))).asked_with(
        1000, {"away": SPENT, "gone": SOONER}
    ) == Asked((), ())


def test_a_look_that_said_nothing_refuses_nothing() -> None:
    """As when it couldn't be had: every model is taken to have budget, and is asked as
    by its room alone."""
    first = offered("first", budget={"requests_a_day": 1})
    second = offered("second", budget={"requests_a_minute": 1})
    chain = Chain(((first, READY), (second, READY)))

    asked = chain.asked_with(1000, NONE_SPENT)

    assert asked == Asked(((first, READY), (second, READY)), ())
    assert asked.refused is None


def model_budget(model: OfferedModel) -> Budget:
    assert model.budget is not None
    return model.budget


@pytest.fixture
async def valkey(valkey_url: str) -> AsyncIterator[Valkey]:
    async with Valkey.from_url(valkey_url) as client:
        yield client


@pytest.fixture
def budgets(valkey: Valkey) -> Budgets:
    """Budgets on the session's Valkey, whose keys no other test shares."""
    return Budgets(valkey, prefix=f"test-{uuid.uuid4().hex}")


@pytest.fixture
async def budgets_away() -> AsyncIterator[Budgets]:
    """Budgets on an address where nothing listens."""
    async with Valkey.from_url("valkey://127.0.0.1:1/0") as nowhere:
        yield Budgets(nowhere)


async def test_a_look_says_of_each_ready_model_with_a_budget_whether_a_request_fits(
    budgets: Budgets,
) -> None:
    """One request from each bucket of requests and the guess from each of tokens: of
    a model it doesn't fit, how long until it does, and the limit in the way, the one
    that takes longest. A model without a budget, one whose budget the request fits, and
    one that isn't ready, are not among them."""
    tokens_first = offered("tokens", budget={"requests_a_minute": 30, "tokens_a_minute": 8000})
    requests_first = offered("requests", budget={"requests_a_minute": 30, "tokens_a_minute": 8000})
    fits = offered("fits", budget={"tokens_a_day": 200_000})
    without, away = offered("without"), offered("away", budget={"requests_a_day": 1})
    rpm, tpm = model_budget(tokens_first).buckets
    other_rpm, other_tpm = model_budget(requests_first).buckets
    # 500 tokens are 4,166 ms of 7,200 a minute, and a request 2,222 of 27.
    await budgets.add((rpm, 27), (tpm, 7_000), (other_rpm, 27), (other_tpm, 6_900), now=NOON)
    await budgets.add((model_budget(away).buckets[0], 1), now=NOON)
    chain = Chain(
        (
            (tokens_first, READY),
            (requests_first, READY),
            (fits, READY),
            (without, READY),
            (away, AWAY),
        )
    )

    found = await chain.look(budgets, 500, now=NOON)

    assert found == {
        "tokens": Spent(timedelta(milliseconds=2_499), tpm),
        "requests": Spent(timedelta(milliseconds=2_222), other_rpm),
    }
    assert found["requests"].seconds == 3
    assert Spent(timedelta(milliseconds=1), tpm).seconds == 1


async def test_a_look_asks_valkey_nothing_when_no_ready_model_has_a_budget(
    budgets_away: Budgets,
) -> None:
    """And says it can't be had when one has."""
    plain, away = offered("plain"), offered("away", budget={"requests_a_day": 1})

    assert await Chain(((plain, READY), (away, GONE))).look(budgets_away, 10, now=NOON) == {}
    with pytest.raises(BudgetsUnavailableError):
        await Chain(((away, READY),)).look(budgets_away, 10, now=NOON)


# --- What a request takes -------------------------------------------------------------


class Says:
    """A model that says what it is told to, an error included, and remembers whether
    its stream was closed."""

    def __init__(self, *script: StreamEvent | Exception) -> None:
        self.script = script
        self.closed = False

    async def stream(self, request: ChatRequest) -> AsyncGenerator[StreamEvent]:
        assert request.messages
        try:
            for item in self.script:
                if isinstance(item, Exception):
                    raise item
                yield item
        finally:
            self.closed = True


REQUEST = ChatRequest(model="model-counted", messages=[User("What moves the chain?")])
GUESS = 120
"""The guess at the request's prompt, as its caller made it."""


async def taken(budgets: Budgets, budget: Budget) -> dict[str, int]:
    """What each bucket of the budget holds taken, by its limit in words: read back
    through a look, from how long until it is full again."""
    waits = await budgets.look(*((b, b.size) for b in budget.buckets), now=NOON)
    return {
        str(b): round(wait / timedelta(milliseconds=1) * b.size / b.period_ms)
        for b, wait in zip(budget.buckets, waits, strict=True)
    }


def counted_model(chat: Says) -> OfferedModel:
    """A model whose budget keeps 9 requests and 900 tokens a minute: a token is worth
    66 ms, so that a bucket a request took from lives for seconds, past the test's look."""
    return offered("counted", chat=chat, budget={"requests_a_minute": 10, "tokens_a_minute": 1_000})


async def read(chat: ChatProvider, *, into: list[StreamEvent]) -> None:
    """Ask `chat` the request, and read its stream to its end or its error, keeping each
    event in `into` as it comes."""
    async with aclosing(chat.stream(REQUEST)) as events:
        async for event in events:
            into.append(event)  # noqa: PERF401 - one by one: the stream may raise after them


async def test_a_model_without_a_budget_is_asked_through_its_own_client(budgets: Budgets) -> None:
    plain = offered("plain", chat=Says())

    assert counted(plain, budgets, GUESS, lambda: NOON) is plain.chat


async def test_the_counted_client_hands_on_the_models_events_and_its_error_as_they_came(
    budgets: Budgets,
) -> None:
    """The same objects: the walk sorts the error by its kind."""
    thought, words, error = (
        ReasoningDelta("Hm."),
        TextDelta("A chain"),
        ProviderUnavailableError("busy"),
    )
    model = counted_model(Says(thought, words, error))
    received: list[StreamEvent] = []

    with pytest.raises(ProviderUnavailableError) as raised:
        await read(counted(model, budgets, GUESS, lambda: NOON), into=received)

    assert [a is b for a, b in zip(received, (thought, words), strict=True)] == [True, True]
    assert raised.value is error
    assert await taken(budgets, model_budget(model)) == {
        "requests a minute": 1,
        "tokens a minute": GUESS + tokens_in("Hm." + "A chain"),
    }


async def test_a_stream_closed_before_its_end_takes_the_guess_and_what_was_handed_on(
    budgets: Budgets,
) -> None:
    """As a title's reader closes it once it has the finish, or a turn's walk when it
    is done with it: what was handed on, and nothing after it, counted by guess."""
    chat = Says(TextDelta("A derailleur"), TextDelta(", mostly"), Finish(FinishReason.STOP, None))
    model = counted_model(chat)

    async with aclosing(counted(model, budgets, GUESS, lambda: NOON).stream(REQUEST)) as events:
        first = await anext(events)

    assert first == TextDelta("A derailleur")
    assert chat.closed
    assert await taken(budgets, model_budget(model)) == {
        "requests a minute": 1,
        "tokens a minute": GUESS + tokens_in("A derailleur"),
    }


class AfterAMoment(Budgets):
    """Budgets that count only after the event loop has turned twice, as when a
    connection to Valkey has to be made first: long enough for a cancellation that is
    under way to reach whoever waits for them."""

    def __init__(self, client: Valkey) -> None:
        super().__init__(client, prefix=f"test-{uuid.uuid4().hex}")

    async def add(self, *amounts: tuple[Bucket, int], now: datetime) -> None:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await super().add(*amounts, now=now)


class HeldAfter:
    """A model that says a few words, and then waits until its stream is closed."""

    def __init__(self, *words: StreamEvent) -> None:
        self.words = words
        self.waiting = asyncio.Event()

    async def stream(self, request: ChatRequest) -> AsyncGenerator[StreamEvent]:
        assert request.messages
        for event in self.words:
            yield event
        self.waiting.set()
        await asyncio.Event().wait()  # nothing sets it


async def test_a_stream_that_a_stop_closes_has_its_tokens_put_right_though_the_count_has_to_wait(
    valkey: Valkey,
) -> None:
    """A turn is stopped through a cancel scope, inside which whatever waits is cancelled
    again at every turn of the event loop (`answering.Halt`). What puts the tokens right
    is shielded from it: without the shield, a count that has to wait a moment is
    cancelled before it is sent, and the bucket keeps the guess alone."""
    budgets = AfterAMoment(valkey)
    chat = HeldAfter(TextDelta("Half an "))
    model = offered(
        "counted", chat=chat, budget={"requests_a_minute": 10, "tokens_a_minute": 1_000}
    )
    received: list[StreamEvent] = []
    stop = anyio.CancelScope()

    async def turn() -> None:
        with stop:
            await read(counted(model, budgets, GUESS, lambda: NOON), into=received)

    running = asyncio.create_task(turn())
    await asyncio.wait_for(chat.waiting.wait(), 5)
    stop.cancel()
    await asyncio.wait_for(running, 5)

    assert received == [TextDelta("Half an ")]
    assert await taken(budgets, model_budget(model)) == {
        "requests a minute": 1,
        "tokens a minute": GUESS + tokens_in("Half an "),
    }


async def test_each_part_of_what_a_request_takes_is_dated_when_it_happens(
    budgets: Budgets,
) -> None:
    """The take when the request is sent, and the provider's count when the stream is
    over: a minute after the take, the take has come back, and the count is all there
    is."""
    times = iter([NOON, NOON + timedelta(minutes=1)])
    model = counted_model(Says(TextDelta("Fine."), Finish(FinishReason.STOP, Usage(300, 20))))

    async with aclosing(
        counted(model, budgets, GUESS, lambda: next(times)).stream(REQUEST)
    ) as events:
        async for _ in events:
            pass

    budget = model_budget(model)
    waits = await budgets.look(
        *((b, b.size) for b in budget.buckets), now=NOON + timedelta(minutes=1)
    )
    # Nothing of the request is left a minute after it was taken: only the count's
    # difference, 200 tokens, taken a minute later.
    assert waits == [timedelta(0), timedelta(milliseconds=200 * 60_000 // 900)]


async def test_a_take_that_cant_be_counted_fails_nothing_and_nothing_is_put_right(
    budgets_away: Budgets, caplog: pytest.LogCaptureFixture
) -> None:
    model = counted_model(Says(TextDelta("A chain."), Finish(FinishReason.STOP, Usage(30, 3))))

    async with aclosing(
        counted(model, budgets_away, GUESS, lambda: NOON).stream(REQUEST)
    ) as events:
        received = [event async for event in events]

    assert received == [TextDelta("A chain."), Finish(FinishReason.STOP, Usage(30, 3))]
    assert caplog.messages == [
        "model counted: what a request to it took of its budget couldn't be counted"
        " (ConnectionError)"
    ]


class AwayAfterTheTake(Budgets):
    """Budgets that take a request, and then can't be reached."""

    def __init__(self, client: Valkey) -> None:
        super().__init__(client, prefix=f"test-{uuid.uuid4().hex}")
        self.adds = 0

    async def add(self, *amounts: tuple[Bucket, int], now: datetime) -> None:
        self.adds += 1
        if self.adds > 1:
            raise BudgetsUnavailableError from ValkeyError("away")
        await super().add(*amounts, now=now)


async def test_a_stream_whose_tokens_cant_be_put_right_fails_nothing(
    valkey: Valkey, caplog: pytest.LogCaptureFixture
) -> None:
    """The take stays as it was: the guess."""
    away_after = AwayAfterTheTake(valkey)
    model = counted_model(Says(TextDelta("A chain."), Finish(FinishReason.STOP, Usage(30, 3))))

    async with aclosing(counted(model, away_after, GUESS, lambda: NOON).stream(REQUEST)) as events:
        received = [event async for event in events]

    assert len(received) == 2
    assert away_after.adds == 2
    assert caplog.messages == [
        "model counted: what a request to it took of its budget couldn't be counted (ValkeyError)"
    ]
    assert await taken(away_after, model_budget(model)) == {
        "requests a minute": 1,
        "tokens a minute": GUESS,
    }


# --- How many answers a server writes at once -----------------------------------------


async def test_ollama_writes_one_answer_at_a_time_and_another_server_any_number() -> None:
    """Unless the file says otherwise."""
    async with catalogs.build(parsed(GOOD), ollama_url="http://ollama") as catalog:
        standard, big = catalog.get("standard"), catalog.get("big")

    assert standard.gate is not None
    assert standard.gate.at_once == 1
    assert big.gate is None


async def test_a_server_writes_as_many_answers_at_once_as_the_file_says() -> None:
    file = parsed(
        GOOD.replace('kind = "ollama"', 'kind = "ollama"\nanswers_at_once = 4').replace(
            'url = "http://lab', 'answers_at_once = 2\nurl = "http://lab'
        )
    )

    async with catalogs.build(file, ollama_url="http://ollama") as catalog:
        standard, big = catalog.get("standard"), catalog.get("big")

    assert file.providers["ollama"].answers_at_once == 4
    assert (standard.gate and standard.gate.at_once, big.gate and big.gate.at_once) == (4, 2)


async def test_the_models_of_one_server_share_its_gate_and_another_servers_have_their_own() -> None:
    file = parsed(
        GOOD.replace('url = "http://lab', 'answers_at_once = 1\nurl = "http://lab')
        + """
[models.small]
title = "Granite 4 Micro"
provider = "ollama"
model = "granite4:micro"
context_window = 4096
max_output_tokens = 1024

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

    async with catalogs.build(file, ollama_url="http://ollama") as catalog:
        gates = {offered.name: offered.gate for offered in catalog}

    assert gates["standard"] is gates["small"]  # one Ollama, writing for both
    assert len({id(gates["standard"]), id(gates["big"]), id(gates["smart"])}) == 3
    assert None not in gates.values()


# --- How long a server is given to begin an answer ------------------------------------

BEGINS_WITHIN = GOOD.replace('kind = "ollama"', 'kind = "ollama"\nbegins_within_seconds = 0.05')
"""The good file, its Ollama given a twentieth of a second to begin an answer."""


def test_a_server_of_either_kind_may_say_how_long_it_is_given_to_begin_an_answer() -> None:
    """A number of seconds, whole or not."""
    file = parsed(
        GOOD.replace('kind = "ollama"', 'kind = "ollama"\nbegins_within_seconds = 45').replace(
            'url = "http://lab', 'begins_within_seconds = 2.5\nurl = "http://lab'
        )
    )

    assert file.providers["ollama"].begins_within_seconds == 45
    assert file.providers["lab"].begins_within_seconds == 2.5
    assert parsed(GOOD).providers["lab"].begins_within_seconds is None


class NeverBegins(httpx2.AsyncBaseTransport):
    """A server that takes a question, and never begins its answer. As an Ollama, it says
    its version first."""

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/api/version":
            return httpx2.Response(200, json={"version": "0.40.16"})
        await asyncio.Event().wait()  # nothing sets it: only a cancellation ends this
        return httpx2.Response(500)


async def answered(offered: OfferedModel) -> list[StreamEvent]:
    """Every event of the model's answer to a short question, asked as a turn asks it."""
    request = ChatRequest(offered.model, [User("hi")], context_window=offered.context_window)
    async with aclosing(offered.chat.stream(request)) as events:
        return [event async for event in events]


@pytest.mark.parametrize("provider", ["ollama", "lab"])
async def test_a_server_that_hasnt_begun_within_its_own_time_is_away(provider: str) -> None:
    """The file's value reaches the client that asks, for either kind of server: a
    twentieth of a second here. The time limit fails a test that waits the client's own
    90 seconds."""
    file = parsed(
        BEGINS_WITHIN.replace(
            'url = "http://lab', 'begins_within_seconds = 0.05\nurl = "http://lab'
        )
    )

    async with (
        asyncio.timeout(1),
        catalogs.build(
            file, ollama_url="http://ollama", transports={provider: NeverBegins()}
        ) as catalog,
    ):
        [offered] = [model for model in catalog if file.models[model.name].provider == provider]
        with pytest.raises(
            ProviderUnavailableError, match=r"start answering within 0\.05 s"
        ) as caught:
            await answered(offered)

    assert type(caught.value) is ProviderUnavailableError


async def test_a_server_that_says_nothing_is_given_the_clients_own_ninety_seconds() -> None:
    """Without waiting them: a client says how long it waits. Only the time to begin an
    answer is the file's: the small calls, such as asking for a model's status, keep the
    catalog's short limits."""
    async with catalogs.build(parsed(BEGINS_WITHIN), ollama_url="http://ollama") as catalog:
        standard, big = catalog.get("standard").chat, catalog.get("big").chat

    assert isinstance(standard, Ollama)
    assert isinstance(big, OpenAICompatible)
    assert big.timeouts == Timeouts(connect_s=2.0, first_event_s=90.0, idle_s=20.0, request_s=3.0)
    assert standard.timeouts == Timeouts(
        connect_s=2.0, first_event_s=0.05, idle_s=20.0, request_s=3.0
    )


# --- How long a server is given for a whole answer ------------------------------------


def test_a_server_of_either_kind_may_say_how_long_it_is_given_for_a_whole_answer() -> None:
    """A number of seconds, whole or not, alone or beside its time to begin: nothing ties
    the two together, and less for the whole than to begin is no mistake."""
    file = parsed(
        GOOD.replace('kind = "ollama"', 'kind = "ollama"\nanswers_within_seconds = 600').replace(
            'url = "http://lab',
            'begins_within_seconds = 30\nanswers_within_seconds = 12.5\nurl = "http://lab',
        )
    )
    shorter = parsed(
        GOOD.replace(
            'kind = "ollama"',
            'kind = "ollama"\nbegins_within_seconds = 45\nanswers_within_seconds = 20',
        )
    )

    ollama, lab = file.providers["ollama"], file.providers["lab"]
    assert (ollama.begins_within_seconds, ollama.answers_within_seconds) == (None, 600)
    assert (lab.begins_within_seconds, lab.answers_within_seconds) == (30, 12.5)
    assert parsed(GOOD).providers["lab"].answers_within_seconds is None
    assert shorter.providers["ollama"].timeouts == Timeouts(
        connect_s=2.0, first_event_s=45, idle_s=20.0, answer_s=20, request_s=3.0
    )


async def test_each_value_the_file_gives_reaches_the_clients_of_its_servers_models() -> None:
    """Each replaces the client's own where it is given, and only there: the small calls
    keep the catalog's short limits."""
    file = parsed(
        GOOD.replace('kind = "ollama"', 'kind = "ollama"\nanswers_within_seconds = 600').replace(
            'url = "http://lab',
            'begins_within_seconds = 30\nanswers_within_seconds = 12.5\nurl = "http://lab',
        )
    )

    async with catalogs.build(file, ollama_url="http://ollama") as catalog:
        standard, big = catalog.get("standard").chat, catalog.get("big").chat

    assert isinstance(standard, Ollama)
    assert isinstance(big, OpenAICompatible)
    assert standard.timeouts == Timeouts(
        connect_s=2.0, first_event_s=90.0, idle_s=20.0, answer_s=600, request_s=3.0
    )
    assert big.timeouts == Timeouts(
        connect_s=2.0, first_event_s=30, idle_s=20.0, answer_s=12.5, request_s=3.0
    )


class Words(httpx2.AsyncByteStream):
    """The body of an answer that never ends: `line` every hundredth of a second."""

    def __init__(self, line: bytes) -> None:
        self._line = line
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        while True:
            yield self._line
            await asyncio.sleep(0.01)

    async def aclose(self) -> None:
        self.closed = True


class NeverEnds(httpx2.AsyncBaseTransport):
    """A server that answers a question with a word every hundredth of a second, and never
    ends, in its own kind's format: Ollama's lines of JSON, or OpenAI's events. As an
    Ollama, it says its version first."""

    def __init__(self) -> None:
        self.bodies: list[Words] = []

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/api/version":
            return httpx2.Response(200, json={"version": "0.40.16"})
        if request.url.path == "/api/chat":
            chunk: dict[str, Any] = {
                "message": {"role": "assistant", "content": "again "},
                "done": False,
            }
            line = json.dumps(chunk).encode() + b"\n"
        else:
            chunk = {"choices": [{"index": 0, "delta": {"content": "again "}}]}
            line = b"data: " + json.dumps(chunk).encode() + b"\n\n"
        self.bodies.append(Words(line))
        return httpx2.Response(200, stream=self.bodies[-1])


@pytest.mark.parametrize("provider", ["ollama", "lab"])
async def test_a_server_still_writing_after_its_own_time_for_an_answer_is_cut(
    provider: str,
) -> None:
    """The file's value reaches the client that asks, for either kind of server: two
    tenths of a second here. The time limit fails a test that waits the client's own
    five minutes."""
    file = parsed(
        GOOD.replace('kind = "ollama"', 'kind = "ollama"\nanswers_within_seconds = 0.2').replace(
            'url = "http://lab', 'answers_within_seconds = 0.2\nurl = "http://lab'
        )
    )
    server = NeverEnds()

    async with (
        asyncio.timeout(2),
        catalogs.build(file, ollama_url="http://ollama", transports={provider: server}) as catalog,
    ):
        [offered] = [model for model in catalog if file.models[model.name].provider == provider]
        with pytest.raises(TookTooLongError, match=r"still answering after 0\.2 s"):
            await answered(offered)

    [body] = server.bodies
    assert body.closed


# --- The gate by itself ---------------------------------------------------------------


class Answering:
    """An answer that passes a gate, and holds its place there until it is let go."""

    def __init__(self, gate: Gate, passed: list[str], name: str) -> None:
        self.done = asyncio.Event()
        self.task = asyncio.create_task(self._pass(gate, passed, name))

    async def _pass(self, gate: Gate, passed: list[str], name: str) -> None:
        async with gate:
            passed.append(name)
            await self.done.wait()


async def settled() -> None:
    """Let every task that can go on do so."""
    for _ in range(5):
        await asyncio.sleep(0)


async def test_answers_pass_in_the_order_they_came_as_many_at_a_time_as_the_server_takes() -> None:
    gate, passed = Gate(2), list[str]()
    first, second, third, fourth = (Answering(gate, passed, name) for name in "abcd")
    await settled()
    at_first, waiting = list(passed), gate.waiting

    second.done.set()
    await settled()
    then = list(passed)
    first.done.set()
    third.done.set()
    fourth.done.set()
    await asyncio.wait_for(asyncio.gather(first.task, second.task, third.task, fourth.task), 1)

    assert (at_first, waiting) == (["a", "b"], 2)
    assert then == ["a", "b", "c"]
    assert passed == ["a", "b", "c", "d"]
    assert (gate.full, gate.waiting) == (False, 0)


async def test_an_answer_that_gives_up_waiting_leaves_the_order_of_the_others_as_it_was() -> None:
    gate, passed = Gate(1), list[str]()
    first, second, third = (Answering(gate, passed, name) for name in "abc")
    await settled()

    second.task.cancel()  # stopped while it waits
    await settled()
    still_waiting = gate.waiting
    first.done.set()
    third.done.set()
    await asyncio.wait_for(asyncio.gather(first.task, third.task), 1)

    assert still_waiting == 1
    assert passed == ["a", "c"]
    assert second.task.cancelled()
    assert not gate.full


async def test_a_place_at_the_gate_is_given_back_however_the_answer_ended() -> None:
    gate = Gate(1)

    with pytest.raises(RuntimeError):
        async with gate:
            raise RuntimeError("the model failed")
    after_a_failure = gate.full

    async def holds_its_place() -> None:
        async with gate:
            await asyncio.Event().wait()

    holding = asyncio.create_task(holds_its_place())
    await settled()
    while_held = gate.full
    holding.cancel()  # from outside, as a process going down does
    await asyncio.gather(holding, return_exceptions=True)

    assert (after_a_failure, while_held, gate.full) == (False, True, False)


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


async def test_each_model_of_a_server_is_told_about_reasoning_as_the_file_says() -> None:
    """Three models of one server that speaks OpenAI's API: the one the file says nothing
    more of is told the client's own "none" when a turn asks for no reasoning, and
    nothing when it asks for some; the others what the file says, an empty table being
    nothing at all. They share the server's gate, and the way to it that a test gives by
    the provider's name."""
    file = parsed(
        GOOD.replace('url = "http://lab', 'answers_at_once = 2\nurl = "http://lab')
        + """
[models.small]
title = "The lab's small model"
provider = "lab"
model = "fake-quiet"
context_window = 8192
max_output_tokens = 1024
thinking = true
thinking_off = { reasoning_effort = "low" }
thinking_on = { extra_body = { google = { thinking_config = { include_thoughts = true } } } }

[models.lite]
title = "A model that refuses to be told none"
provider = "lab"
model = "fake-lite"
context_window = 8192
max_output_tokens = 1024
thinking = true
thinking_off = {}
"""
    )
    sent: list[dict[str, Any]] = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        sent.append(json.loads(request.content))
        return httpx2.Response(
            200, content=b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
        )

    async with catalogs.build(
        file, ollama_url="http://ollama", transports={"lab": httpx2.MockTransport(answer)}
    ) as catalog:
        big, small, lite = catalog.get("big"), catalog.get("small"), catalog.get("lite")
        for offered in (big, small, lite):
            for thinking in (False, True):
                request = ChatRequest(offered.model, [User("hi")], thinking=thinking)
                async with aclosing(offered.chat.stream(request)) as events:
                    async for _ in events:
                        pass

    told = [{name: body.get(name) for name in ("reasoning_effort", "extra_body")} for body in sent]
    assert [body["model"] for body in sent] == (
        ["fake-chat"] * 2 + ["fake-quiet"] * 2 + ["fake-lite"] * 2
    )
    assert told == [
        {"reasoning_effort": "none", "extra_body": None},
        {"reasoning_effort": None, "extra_body": None},
        {"reasoning_effort": "low", "extra_body": None},
        {
            "reasoning_effort": None,
            "extra_body": {"google": {"thinking_config": {"include_thoughts": True}}},
        },
        {"reasoning_effort": None, "extra_body": None},  # `thinking_off = {}`: nothing
        {"reasoning_effort": None, "extra_body": None},
    ]
    assert big.gate is not None
    assert big.gate is small.gate is lite.gate


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


class NeverGzip(httpx2.AsyncByteStream):
    """A body as it comes from the network, which a server said was compressed."""

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"this was never gzip"


async def test_a_server_whose_answers_cant_be_unpacked_is_unreachable_not_an_error() -> None:
    """Its answer about a model is nonsense. The HTTP client's error for that is none
    of its errors of the connection: let out as it was, it failed the list of models."""

    def packed_badly(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, headers={"content-encoding": "gzip"}, stream=NeverGzip())

    nonsense = httpx2.MockTransport(packed_badly)
    transports = {"ollama": nonsense, "lab": nonsense}

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


async def test_a_model_named_by_a_path_is_found_by_that_path() -> None:
    """A server may name a model `models/qwen`, and take exactly that, as vLLM started
    with a path does. (The Gemini API lists `models/<name>` and takes `<name>`: that
    spelling is found too, as packages/llm's tests show.)"""
    file = parsed(GOOD.replace('model = "fake-chat"', 'model = "models/qwen"'))

    def lists(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"object": "list", "data": [{"id": "models/qwen"}]})

    async with catalogs.build(
        file, ollama_url="http://ollama", transports={"lab": httpx2.MockTransport(lists)}
    ) as catalog:
        assert await catalog.status("big", now=NOON) == ModelStatus("ready", True)


class AnswersLate(httpx2.AsyncBaseTransport):
    """A server that lists its models after `late_s` seconds. Like a real connection, it
    is given up on when the request's own time to read runs out first."""

    def __init__(self, late_s: float) -> None:
        self.late_s = late_s

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        patience = request.extensions["timeout"]["read"]
        if patience is not None and patience < self.late_s:
            await asyncio.sleep(patience)
            raise httpx2.ReadTimeout("timed out", request=request)
        await asyncio.sleep(self.late_s)
        return httpx2.Response(200, json={"object": "list", "data": [{"id": "fake-chat"}]})


async def test_a_cloud_models_status_is_asked_with_the_catalogs_short_patience(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A server that takes longer than the catalog gives a small call is away, whatever
    the client's own patience for one. The time limit fails a test that waits on."""
    monkeypatch.setattr(catalogs, "TIMEOUTS", Timeouts(connect_s=0.05, request_s=0.05))

    async with (
        asyncio.timeout(5),
        catalogs.build(
            parsed(GOOD), ollama_url="http://ollama", transports={"lab": AnswersLate(1.0)}
        ) as catalog,
    ):
        status = await catalog.status("big", now=NOON)

    assert status == ModelStatus("unreachable", False)


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


class LoadsForEver(httpx2.AsyncBaseTransport):
    """An Ollama server that never answers a load. Says whether it had been closed by
    the time the load was given up on."""

    def __init__(self) -> None:
        self.asked = asyncio.Event()
        self.closed = False
        self.closed_before_given_up: bool | None = None
        """`None` while the load hasn't been given up on."""

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/api/version":
            return httpx2.Response(200, json={"version": "0.40.16"})
        self.asked.set()
        try:
            await asyncio.Event().wait()  # nothing sets it: only a cancellation ends this
        finally:
            self.closed_before_given_up = self.closed
        return httpx2.Response(500)

    async def aclose(self) -> None:
        self.closed = True


async def test_a_load_under_way_is_given_up_on_before_its_servers_client_is_closed() -> None:
    """None is left pending once the catalog is closed, and none is left to a closed
    client."""
    server = LoadsForEver()

    # With a time limit: a catalog that waited for the load would never close.
    async with (
        asyncio.timeout(5),
        catalogs.build(
            parsed(GOOD), ollama_url="http://ollama", transports={"ollama": server}
        ) as catalog,
    ):
        started = catalog.load_ahead("standard")
        await asyncio.wait_for(server.asked.wait(), 5)

    assert started
    assert server.closed
    assert server.closed_before_given_up is False
    assert [task for task in asyncio.all_tasks() if task.get_name().startswith("load-")] == []


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
