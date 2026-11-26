"""The models on offer: reading the file, building the providers, asking about a model.

No model server runs here. Ollama is stood in for by a function that answers its
`/api/show` the way the real server was recorded to (see `packages/llm/tests/fixtures`),
and an OpenAI-compatible server by fakellm.
"""

import asyncio
import gc
import json
import logging
import math
import sys
import tomllib
import traceback
import uuid
import weakref
from collections import Counter
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import aclosing, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import anyio
import httpx2
import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st
from valkey.asyncio import Valkey
from valkey.exceptions import ValkeyError

from actio import catalog as catalogs
from actio.catalog import (
    BELIEVED_AT_MOST,
    LIMITS,
    PROVIDERS_WAIT,
    Asked,
    Budget,
    Catalog,
    CatalogError,
    CatalogFile,
    Chain,
    Gate,
    ModelStatus,
    OfferedModel,
    OwnWait,
    Spent,
    UnknownModelError,
    asked_through,
    believed,
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
    ConnectionFailedError,
    ContextOverflowError,
    Failing,
    Finish,
    FinishReason,
    Link,
    LLMError,
    ModelNotFoundError,
    Ollama,
    OpenAICompatible,
    ProviderError,
    ProviderUnavailableError,
    RateLimitedError,
    ReasoningDelta,
    RequestRejectedError,
    StreamEvent,
    StreamInterruptedError,
    TextDelta,
    Timeouts,
    TookTooLongError,
    UnsupportedFeatureError,
    UnsupportedServerError,
    Usage,
    User,
    Walk,
    tokens_in,
)
from actio_llm.breaker import Opened, Outcome

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
    # And the fake once more, behind a gate of one, so that a test sees an answer wait its
    # turn (apps/web/e2e/waits.spec.ts). On a provider of its own at the fake's address:
    # the others take every answer as it comes, and the tests run side by side.
    queued = file.models["queued"]
    assert (queued.model, queued.thinking) == (standard.model, False)
    assert file.providers[queued.provider].url == provider.url
    assert {name: entry.at_once for name, entry in file.providers.items()} == {
        standard.provider: None,
        hurried.provider: None,
        away.provider: None,
        rationed.provider: None,
        queued.provider: 1,
    }
    assert queued.provider not in {
        model.provider for name, model in file.models.items() if name != "queued"
    }
    assert {
        name: (entry.answers_within_seconds, entry.timeouts.answer_s)
        for name, entry in file.providers.items()
    } == {
        standard.provider: (None, 300),
        hurried.provider: (3, 3),
        away.provider: (None, 300),
        rationed.provider: (None, 300),
        queued.provider: (None, 300),
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


def test_a_directory_in_the_files_place_says_so_and_how_to_point_elsewhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(CatalogError) as failure:
        read_file(tmp_path)

    assert str(failure.value).startswith(f"the models file '{tmp_path}' is a directory")
    assert "ACTIO_MODELS_FILE" in str(failure.value)

    # A variable that is set and empty names the directory the process runs in.
    monkeypatch.chdir(tmp_path)
    with pytest.raises(CatalogError) as failure:
        read_file(Path())

    assert str(failure.value).startswith("the models file '.' is a directory")
    assert "ACTIO_MODELS_FILE" in str(failure.value)


def test_a_path_that_cant_be_looked_at_is_one_line_too(tmp_path: Path) -> None:
    # A name longer than a file's may be: asking whether it is a directory fails as
    # opening it does.
    unreadable = tmp_path / ("a" * 300)

    with pytest.raises(CatalogError) as failure:
        read_file(unreadable)

    assert str(failure.value).startswith(f"can't read the models file '{unreadable}'")
    assert "\n" not in str(failure.value)


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


def test_a_mistake_in_toml_says_where_in_the_file_it_is(tmp_path: Path) -> None:
    path = written(tmp_path, GOOD.replace('default = "standard"', "default = "))
    [line] = [
        number
        for number, text in enumerate(path.read_text().splitlines(), start=1)
        if text == "default = "
    ]

    with pytest.raises(CatalogError) as failure:
        read_file(path)

    assert f"line {line}, column" in str(failure.value)


# What the TOML parser gives up on in a way of its own, and not as a mistake in TOML:
# each was let out as the parser raised it, and stopped the API with a trace.
GIVEN_UP_ON = {
    "bytes that aren't UTF-8": ("# caf\xe9\n".encode("latin-1") + GOOD.encode(), "UTF-8"),
    "a whole number of 5,000 digits": (
        GOOD.replace("context_window = 32768", f"context_window = {'9' * 5000}").encode(),
        "value has 5000 digits",
    ),
    "lists nested 3,000 deep": (
        (GOOD + "\ndeep = " + "[" * 3000 + "]" * 3000 + "\n").encode(),
        "nested too deep",
    ),
}


@pytest.mark.parametrize("fault", GIVEN_UP_ON)
def test_a_file_the_toml_parser_gives_up_on_is_named_in_one_line(
    tmp_path: Path, fault: str
) -> None:
    content, said = GIVEN_UP_ON[fault]
    path = tmp_path / "models.toml"
    path.write_bytes(content)

    with pytest.raises(CatalogError) as failure:
        read_file(path)

    assert str(path) in str(failure.value)
    assert said in str(failure.value)
    assert "\n" not in str(failure.value)
    # Python's own advice for a number too long is no help with a file.
    assert "sys." not in str(failure.value)


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
NONE_FAILING: dict[str, Failing] = {}
"""What the breakers said when no model is failing: the order is the room's and the
budget's alone."""


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

    assert chain.asked_with(8000, NONE_SPENT, NONE_FAILING) == Asked(
        ((small, READY), (large, READY)), ()
    )
    assert chain.asked_with(8001, NONE_SPENT, NONE_FAILING) == Asked(((large, READY),), (small,))


def test_while_some_ready_models_have_room_only_those_are_asked_in_the_chains_order() -> None:
    """Each with its own status. The others are passed over for their room, and listed
    in the chain's order too."""
    first = offered("first", tokens_a_minute=1000)
    second = offered("second", context_window=9000)
    third = offered("third", context_window=500)
    fourth = offered("fourth", context_window=1_048_576, tokens_a_minute=250_000)
    chain = Chain(((first, READY), (second, REASONS), (third, READY), (fourth, READY)))

    assert chain.asked_with(5000, NONE_SPENT, NONE_FAILING) == Asked(
        ((second, REASONS), (fourth, READY)), (first, third)
    )


def test_when_no_ready_model_has_room_each_is_asked_and_none_is_passed_over() -> None:
    """A guess never refuses a request: it is asked as if no model had a room. A model
    alone is its own chain, and is asked whatever the request."""
    first, second = offered("first", tokens_a_minute=1000), offered("second", context_window=2000)

    assert Chain(((first, READY), (second, REASONS))).asked_with(
        5000, NONE_SPENT, NONE_FAILING
    ) == Asked(((first, READY), (second, REASONS)), ())
    assert Chain(((first, READY),)).asked_with(5000, NONE_SPENT, NONE_FAILING) == Asked(
        ((first, READY),), ()
    )


def test_a_stand_in_without_room_is_passed_over_while_the_asked_model_is_asked() -> None:
    asked, stand_in = offered("standard"), offered("second", tokens_a_minute=1000)

    assert Chain(((asked, READY), (stand_in, READY))).asked_with(
        5000, NONE_SPENT, NONE_FAILING
    ) == Asked(((asked, READY),), (stand_in,))


def test_a_model_that_isnt_ready_never_counts_as_one_with_room() -> None:
    """Away or missing with the only room large enough, it doesn't keep the ready ones
    from all being asked. And with too little room, it is passed over for being away,
    not for its room: it isn't listed as too small."""
    small, smaller = offered("small", tokens_a_minute=1000), offered("smaller", context_window=2000)
    away, gone = offered("away", context_window=1_048_576), offered("gone", context_window=131_072)
    tiny, large = offered("tiny", context_window=1000), offered("large", context_window=131_072)

    roomy_but_not_ready = Chain(((small, READY), (away, AWAY), (smaller, READY), (gone, GONE)))
    tiny_and_away = Chain(((tiny, AWAY), (large, READY)))

    assert roomy_but_not_ready.asked_with(5000, NONE_SPENT, NONE_FAILING) == Asked(
        ((small, READY), (smaller, READY)), ()
    )
    assert tiny_and_away.asked_with(5000, NONE_SPENT, NONE_FAILING) == Asked(((large, READY),), ())


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

    assert chain.asked_with(1000, {"first": SPENT, "third": LATER}, NONE_FAILING) == Asked(
        ((second, REASONS), (fourth, READY), (first, READY), (third, READY)),
        (),
        ((first, SPENT), (third, LATER)),
    )


def test_when_no_model_with_room_has_budget_nobody_is_asked_and_the_request_is_refused() -> None:
    """For the shortest wait of theirs, whichever comes first in the chain, and each is
    named in the chain's order. A model alone is refused by its own wait."""
    first, second, third = offered("first"), offered("second"), offered("third")
    chain = Chain(((first, READY), (second, REASONS), (third, READY)))

    assert chain.asked_with(
        1000, {"first": LATER, "second": SOONER, "third": SPENT}, NONE_FAILING
    ) == Asked((), (), ((first, LATER), (second, SOONER), (third, SPENT)), refused=SOONER)
    assert Chain(((first, READY),)).asked_with(1000, {"first": SPENT}, NONE_FAILING) == Asked(
        (), (), ((first, SPENT),), refused=SPENT
    )


def test_of_two_waits_as_short_the_refusal_names_the_first_in_the_chain() -> None:
    first, second = offered("first"), offered("second")
    as_soon = Spent(SPENT.wait, SOONER.limit)
    chain = Chain(((first, READY), (second, READY)))

    asked = chain.asked_with(1000, {"first": SPENT, "second": as_soon}, NONE_FAILING)

    assert asked.refused == SPENT
    assert (
        chain.asked_with(1000, {"first": as_soon, "second": SPENT}, NONE_FAILING).refused == as_soon
    )


def test_a_model_without_room_neither_shortens_the_wait_nor_keeps_the_refusal_away() -> None:
    """Passed over for its room, it is named for its room alone: with budget, it keeps
    nobody from being refused, and with a shorter wait, it doesn't shorten the one the
    request is refused for."""
    small, spent = offered("small", tokens_a_minute=500), offered("spent")
    chain = Chain(((small, READY), (spent, READY)))

    with_budget = chain.asked_with(1000, {"spent": SPENT}, NONE_FAILING)
    sooner_than_it = chain.asked_with(1000, {"small": SOONER, "spent": SPENT}, NONE_FAILING)

    assert with_budget == Asked((), (small,), ((spent, SPENT),), refused=SPENT)
    assert sooner_than_it == Asked((), (small,), ((spent, SPENT),), refused=SPENT)


def test_the_room_comes_first_and_a_model_without_room_is_said_for_its_room_alone() -> None:
    small, spent, fits = offered("small", tokens_a_minute=500), offered("spent"), offered("fits")
    chain = Chain(((small, READY), (spent, READY), (fits, READY)))

    assert chain.asked_with(1000, {"small": SPENT, "spent": SPENT}, NONE_FAILING) == Asked(
        ((fits, READY), (spent, READY)), (small,), ((spent, SPENT),)
    )


def test_when_no_model_has_room_nor_budget_every_ready_one_is_asked_and_none_is_named() -> None:
    """A request too large by the guess is asked all the same, and nothing is refused:
    the model's own answer about its size is truer than a wait."""
    first, second = offered("first", context_window=500), offered("second", tokens_a_minute=500)
    away = offered("away", context_window=500)
    chain = Chain(((first, READY), (away, AWAY), (second, REASONS)))

    assert chain.asked_with(
        1000, {"first": SPENT, "second": SOONER, "away": SOONER}, NONE_FAILING
    ) == Asked(((first, READY), (second, REASONS)), ())


def test_when_no_model_has_room_the_one_with_budget_is_asked_first_and_the_others_after() -> None:
    first, second = offered("first", context_window=500), offered("second", tokens_a_minute=500)
    third = offered("third", context_window=500)
    chain = Chain(((first, READY), (second, READY), (third, READY)))

    assert chain.asked_with(1000, {"first": SPENT, "third": SOONER}, NONE_FAILING) == Asked(
        ((second, READY), (first, READY), (third, READY)), (), ((first, SPENT), (third, SOONER))
    )


def test_a_model_that_isnt_ready_never_counts_as_one_with_budget_nor_for_its_wait() -> None:
    """With the only budget left, it doesn't keep the ready one that has none from being
    refused, it isn't named, and its shorter wait isn't the refusal's. A chain with no
    ready model has nobody to ask, and that is no refusal."""
    spent, away, gone = offered("spent"), offered("away"), offered("gone")
    chain = Chain(((spent, READY), (away, AWAY)))

    assert chain.asked_with(1000, {"spent": SPENT}, NONE_FAILING) == Asked(
        (), (), ((spent, SPENT),), refused=SPENT
    )
    assert chain.asked_with(1000, {"spent": SPENT, "away": SOONER}, NONE_FAILING) == Asked(
        (), (), ((spent, SPENT),), refused=SPENT
    )
    assert Chain(((away, AWAY), (gone, GONE))).asked_with(
        1000, {"away": SPENT, "gone": SOONER}, NONE_FAILING
    ) == Asked((), ())


def test_a_look_that_said_nothing_refuses_nothing() -> None:
    """As when it couldn't be had: every model is taken to have budget, and is asked as
    by its room alone."""
    first = offered("first", budget={"requests_a_day": 1})
    second = offered("second", budget={"requests_a_minute": 1})
    chain = Chain(((first, READY), (second, READY)))

    asked = chain.asked_with(1000, NONE_SPENT, NONE_FAILING)

    assert asked == Asked(((first, READY), (second, READY)), ())
    assert asked.refused is None


# --- A model that keeps failing, and who is asked then --------------------------------

FAILING = Failing(timedelta(seconds=20))
"""What a breaker says of a model that is failing, for 20 seconds more."""
TRIED = Failing(None)
"""What it says once the wait is over, while a request tries the model in its place."""


def failing_now(model: OfferedModel, now: datetime = NOON) -> OfferedModel:
    """`model`, with its breaker opened at `now`, for 30 seconds, by five failures."""
    for _ in range(5):
        model.breaker.begins(now).ends("failed", now)
    assert model.breaker.failing(now) == Failing(timedelta(seconds=30))
    return model


def test_a_failing_model_is_asked_after_the_models_of_its_chain_that_arent() -> None:
    """Each group in the chain's order: those that aren't failing, then those that are.
    Each that was moved is named, in the order they are asked, with what its breaker
    said."""
    first, second = offered("first"), offered("second")
    third, fourth = offered("third"), offered("fourth")
    chain = Chain(((first, READY), (second, REASONS), (third, READY), (fourth, READY)))

    assert chain.asked_with(1000, NONE_SPENT, {"first": FAILING, "third": TRIED}) == Asked(
        ((second, REASONS), (fourth, READY), (first, READY), (third, READY)),
        (),
        failing=((first, FAILING), (third, TRIED)),
    )


def test_the_breakers_order_each_of_the_budgets_two_groups_and_never_mix_them() -> None:
    """With budget and not failing, with budget and failing, without and not failing,
    without and failing."""
    a, b, c, d = offered("a"), offered("b"), offered("c"), offered("d")
    chain = Chain(((a, READY), (b, READY), (c, READY), (d, READY)))

    asked = chain.asked_with(1000, {"a": SPENT, "b": SOONER}, {"a": FAILING, "c": FAILING})

    assert asked == Asked(
        ((d, READY), (c, READY), (b, READY), (a, READY)),
        (),
        ((a, SPENT), (b, SOONER)),
        failing=((c, FAILING), (a, FAILING)),
    )


def test_a_failing_model_with_budget_is_asked_before_one_without_whatever_its_breaker() -> None:
    """And isn't named: the breaker didn't move it. Nor is one that its budget alone put
    last, which `spent` names."""
    failing, spent = offered("failing"), offered("spent")

    assert Chain(((spent, READY), (failing, READY))).asked_with(
        1000, {"spent": SPENT}, {"failing": FAILING}
    ) == Asked(((failing, READY), (spent, READY)), (), ((spent, SPENT),))
    assert Chain(((failing, READY), (spent, READY))).asked_with(
        1000, {"failing": SPENT}, {"failing": FAILING}
    ) == Asked(((spent, READY), (failing, READY)), (), ((failing, SPENT),))


def test_a_failing_model_the_breakers_left_where_it_was_is_not_named() -> None:
    """A chain's only model, a chain whose every model is failing, and one that was last
    already: each asked in the chain's order, as if nobody were failing."""
    alone, first, second = offered("alone"), offered("first"), offered("second")
    both = Chain(((first, READY), (second, READY)))

    assert Chain(((alone, READY),)).asked_with(1000, NONE_SPENT, {"alone": FAILING}) == Asked(
        ((alone, READY),), ()
    )
    assert both.asked_with(1000, NONE_SPENT, {"first": FAILING, "second": TRIED}) == Asked(
        ((first, READY), (second, READY)), ()
    )
    assert both.asked_with(1000, NONE_SPENT, {"second": FAILING}) == Asked(
        ((first, READY), (second, READY)), ()
    )


def test_when_no_ready_model_has_room_every_one_is_asked_and_the_failing_ones_after() -> None:
    """With budget or without: a guess never refuses, and the breakers only order."""
    first, second = offered("first", context_window=500), offered("second", tokens_a_minute=500)
    chain = Chain(((first, READY), (second, REASONS)))
    moved = Asked(((second, REASONS), (first, READY)), (), failing=((first, FAILING),))

    assert chain.asked_with(1000, NONE_SPENT, {"first": FAILING}) == moved
    assert chain.asked_with(1000, {"first": SPENT, "second": SOONER}, {"first": FAILING}) == moved


def test_a_breaker_never_refuses_a_request_nor_keeps_one_from_being_refused() -> None:
    """The refusal for now is the budget's alone: a failing model with budget keeps it
    away, and one without counts for its wait as any other does."""
    failing, spent = offered("failing"), offered("spent")
    chain = Chain(((failing, READY), (spent, READY)))

    kept_away = chain.asked_with(1000, {"spent": SPENT}, {"failing": FAILING})
    refused = chain.asked_with(1000, {"failing": SOONER, "spent": SPENT}, {"failing": FAILING})

    assert kept_away == Asked(((failing, READY), (spent, READY)), (), ((spent, SPENT),))
    assert refused == Asked((), (), ((failing, SOONER), (spent, SPENT)), refused=SOONER)


def test_a_failing_model_passed_over_for_its_room_is_neither_asked_nor_named() -> None:
    small, large, other = offered("small", tokens_a_minute=500), offered("large"), offered("other")
    chain = Chain(((small, READY), (large, READY), (other, READY)))

    assert chain.asked_with(1000, NONE_SPENT, {"small": FAILING, "large": FAILING}) == Asked(
        ((other, READY), (large, READY)), (small,), failing=((large, FAILING),)
    )


ROOMS = (500, 1000, 4000)
"""A model's room in the made-up chains, against requests of 1 to 4,500 tokens."""

MADE_UP = st.lists(
    st.tuples(
        st.sampled_from(ROOMS),
        st.sampled_from([READY, READY, REASONS, AWAY, GONE]),
        st.sampled_from([None, SPENT, SOONER, LATER]),
        st.sampled_from([None, None, FAILING, TRIED]),
    ),
    min_size=1,
    max_size=6,
)
"""Chains of models, each with its room, its status, what a look said of its budget, and
what its breaker said."""


@given(made_up=MADE_UP, tokens=st.integers(1, 4_500))
def test_the_breakers_change_who_is_asked_only_in_order(
    made_up: list[tuple[int, ModelStatus, Spent | None, Failing | None]], tokens: int
) -> None:
    """Whatever the chain: the same models asked as with no model failing, the same
    passed over, said without budget and refused; within each of the budget's groups the
    models that aren't failing first; and each failing model named that a model which
    isn't failing, of its group, followed in the chain's order."""
    models = [
        offered(f"m{number}", context_window=room) for number, (room, *_) in enumerate(made_up)
    ]
    each = list(zip(models, made_up, strict=True))
    chain = Chain(tuple((model, status) for model, (_, status, _, _) in each))
    spent = {model.name: said for model, (_, _, said, _) in each if said is not None}
    failing = {model.name: said for model, (_, _, _, said) in each if said is not None}

    plain = chain.asked_with(tokens, spent, NONE_FAILING)
    broken = chain.asked_with(tokens, spent, failing)

    assert (broken.too_small, broken.spent, broken.refused) == (
        plain.too_small,
        plain.spent,
        plain.refused,
    )
    assert plain.failing == ()
    # Stable: the chain's order, and the budget's, kept within each kind.
    assert broken.models == tuple(
        sorted(plain.models, key=lambda each: (each[0].name in spent, each[0].name in failing))
    )
    groups = [model.name in spent for model, _ in plain.models]
    moved = {
        model.name
        for number, (model, _) in enumerate(plain.models)
        if model.name in failing
        and any(
            later.name not in failing and groups[number + 1 + after] == groups[number]
            for after, (later, _) in enumerate(plain.models[number + 1 :])
        )
    }
    assert [model.name for model, _ in broken.failing] == [
        model.name for model, _ in broken.models if model.name in moved
    ]
    assert all(said is failing[model.name] for model, said in broken.failing)


def test_a_chain_says_which_of_its_ready_models_are_failing_and_for_how_long() -> None:
    """By each model's breaker, at the moment it is given. A model that isn't ready is
    never named, failing or not."""
    away, failing, fine = (
        failing_now(offered("away")),
        failing_now(offered("failing")),
        offered("fine"),
    )
    chain = Chain(((away, AWAY), (failing, READY), (fine, READY)))

    assert chain.failing(NOON) == {"failing": Failing(timedelta(seconds=30))}
    assert chain.failing(NOON + timedelta(seconds=10)) == {
        "failing": Failing(timedelta(seconds=20))
    }
    assert chain.failing(NOON + timedelta(seconds=30)) == {}


async def test_two_models_that_are_one_model_on_one_provider_share_a_breaker() -> None:
    """Failures through one name have the other asked last too. The same model's name on
    another provider has a breaker of its own, and so has each other model."""
    text = (
        GOOD
        + AGAIN
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
"""
    )

    async with catalogs.build(parsed(text), ollama_url="http://ollama") as catalog:
        standard, big, again, elsewhere = (
            catalog.get(name) for name in ("standard", "big", "again", "elsewhere")
        )

    assert big.breaker is again.breaker
    assert elsewhere.breaker is not big.breaker
    assert standard.breaker is not big.breaker
    assert standard.breaker is not elsewhere.breaker
    failing_now(big)
    chain = Chain(((again, READY), (elsewhere, READY), (standard, READY)))
    assert chain.failing(NOON) == {"again": Failing(timedelta(seconds=30))}
    assert chain.asked_with(10, NONE_SPENT, chain.failing(NOON)).models == (
        (elsewhere, READY),
        (standard, READY),
        (again, READY),
    )


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


# --- Each try, recorded in the model's breaker ----------------------------------------

SECRET = "my door code is 4471"
"""What an error's text may quote: never in the log."""
WORDS = TextDelta("A chain")
THOUGHT = ReasoningDelta("Hm.")
FINISHED = Finish(FinishReason.STOP, Usage(30, 3))


async def to_its_end(chat: ChatProvider) -> None:
    """Ask `chat` the request, and read its stream to its end or its error."""
    with suppress(LLMError, RuntimeError):
        await read(chat, into=[])


async def closed_after_one(chat: ChatProvider) -> None:
    """Ask `chat` the request, take its first event, and close its stream, as a reader
    that is done with it does."""
    async with aclosing(chat.stream(REQUEST)) as events:
        await anext(events)


async def ends_as(
    chat: ChatProvider,
    budgets: Budgets,
    reading: Callable[[ChatProvider], Awaitable[None]] = to_its_end,
) -> Outcome:
    """What one try of a model whose client is `chat` ends as, through the client a link
    asks it through, read from its breaker: one with four failures kept, which a failure
    opens. Not opened by the try, it is opened by one failure more, of 6 calls when the
    try was kept as an answer, and of 5 when it had nothing to say."""
    model = offered("watched", chat=chat)
    for _ in range(4):
        model.breaker.begins(NOON).ends("failed", NOON)

    await reading(asked_through(model, budgets, GUESS, lambda: NOON))

    if model.breaker.failing(NOON) is not None:
        return "failed"
    opened = model.breaker.begins(NOON).ends("failed", NOON)
    assert isinstance(opened, Opened)
    assert model.breaker.trying == 0
    return "answered" if opened.calls == 6 else None


@pytest.mark.parametrize(
    ("script", "outcome"),
    [
        pytest.param(lambda: Says(WORDS, FINISHED), "answered", id="it finished"),
        pytest.param(lambda: Says(FINISHED), "answered", id="it finished with no words"),
        pytest.param(lambda: Says(THOUGHT, FINISHED), "answered", id="it finished a thought"),
        pytest.param(
            lambda: Says(ProviderUnavailableError(SECRET)), "failed", id="too busy before a word"
        ),
        pytest.param(
            lambda: Says(WORDS, ProviderUnavailableError(SECRET)), "failed", id="too busy after one"
        ),
        pytest.param(lambda: Says(ConnectionFailedError(SECRET)), "failed", id="refused"),
        pytest.param(lambda: Says(RateLimitedError(SECRET, retry_after_s=1)), "failed", id="429"),
        pytest.param(
            lambda: Says(THOUGHT, StreamInterruptedError(SECRET)), "failed", id="broke off"
        ),
        pytest.param(lambda: Says(ProviderError(SECRET)), "failed", id="its provider failed"),
        pytest.param(
            lambda: Says(TextDelta(""), TookTooLongError(SECRET)),
            "failed",
            id="out of time with nothing handed on",
        ),
        pytest.param(
            lambda: Says(WORDS, TookTooLongError(SECRET)), "answered", id="cut at its time"
        ),
        pytest.param(
            lambda: Says(THOUGHT, TookTooLongError(SECRET)),
            "answered",
            id="cut at its time after a thought",
        ),
        pytest.param(lambda: Says(WORDS), "failed", id="no finish after a word"),
        pytest.param(lambda: Says(TextDelta("")), "failed", id="no finish and nothing"),
        pytest.param(lambda: Says(ContextOverflowError(SECRET)), None, id="too large"),
        pytest.param(lambda: Says(RequestRejectedError(SECRET)), None, id="refused request"),
        pytest.param(lambda: Says(UnsupportedFeatureError(SECRET)), None, id="no such feature"),
        pytest.param(lambda: Says(ModelNotFoundError("m", SECRET)), None, id="no such model"),
        pytest.param(lambda: Says(UnsupportedServerError(SECRET)), None, id="server too old"),
        pytest.param(
            lambda: Says(WORDS, RequestRejectedError(SECRET)), "answered", id="refused after a word"
        ),
        pytest.param(lambda: Says(RuntimeError(SECRET)), None, id="anything else"),
        pytest.param(
            lambda: Says(WORDS, RuntimeError(SECRET)), "answered", id="anything else after a word"
        ),
    ],
)
async def test_a_try_ends_answered_failed_or_with_nothing_to_say_by_how_its_stream_ended(
    script: Callable[[], Says], outcome: Outcome, budgets: Budgets
) -> None:
    """Failed: an error another try may not meet again, before or after a word, but an
    answer cut at its time after something was handed on; or a stream that ended
    without its finish. Answered: otherwise, once something was handed on. Otherwise
    nothing to say: the request's own refusal, or anything else."""
    assert await ends_as(script(), budgets) == outcome


@pytest.mark.parametrize(
    ("script", "outcome"),
    [
        pytest.param((TextDelta(""), WORDS, FINISHED), None, id="after an empty piece"),
        pytest.param((WORDS, FINISHED), "answered", id="after a word"),
        pytest.param((THOUGHT, FINISHED), "answered", id="after a thought"),
    ],
)
async def test_a_try_closed_before_its_end_has_answered_once_something_was_handed_on(
    script: tuple[StreamEvent, ...], outcome: Outcome, budgets: Budgets
) -> None:
    assert await ends_as(Says(*script), budgets, closed_after_one) == outcome


@pytest.mark.parametrize(
    ("words", "outcome"), [((), None), ((WORDS,), "answered")], ids=["before a word", "after one"]
)
async def test_a_try_a_stop_cuts_off_is_recorded_and_the_stop_passes_on(
    words: tuple[StreamEvent, ...], outcome: Outcome, budgets: Budgets
) -> None:
    """Cancelled through a scope, as a turn's halt does, while its model waits: its
    breaker's call has ended, as an answer once something was handed on, and with
    nothing to say before. The cancellation isn't swallowed: what waited for the stream
    is cancelled with it."""
    chat = HeldAfter(*words)
    stop = anyio.CancelScope()
    reached_the_end = False

    async def turn(client: ChatProvider) -> None:
        nonlocal reached_the_end
        with stop:
            await read(client, into=[])
            reached_the_end = True

    async def stopped(client: ChatProvider) -> None:
        running = asyncio.create_task(turn(client))
        await asyncio.wait_for(chat.waiting.wait(), 5)
        stop.cancel()
        await asyncio.wait_for(running, 5)

    assert await ends_as(chat, budgets, stopped) == outcome
    assert stop.cancelled_caught
    assert not reached_the_end


async def test_a_try_that_tries_a_model_once_its_wait_is_over_ends_however_it_is_stopped(
    budgets: Budgets,
) -> None:
    """While it waits, it is the one request that tries the model; once a task cancelled
    from outside has given up on it, the cancellation is the task's, and no try is left
    under way."""
    chat = HeldAfter()
    model = failing_now(offered("watched", chat=chat))
    after_the_wait = NOON + timedelta(seconds=30)
    client = asked_through(model, budgets, GUESS, lambda: after_the_wait)
    reading = asyncio.create_task(read(client, into=[]))
    await asyncio.wait_for(chat.waiting.wait(), 5)

    assert model.breaker.trying == 1
    assert model.breaker.failing(after_the_wait) == Failing(None)
    reading.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(reading, 5)
    assert model.breaker.trying == 0
    assert model.breaker.failing(after_the_wait) is None


@pytest.mark.parametrize(
    "script",
    [
        pytest.param(lambda: Says(ContextOverflowError(SECRET)), id="too large"),
        pytest.param(lambda: Says(RequestRejectedError(SECRET)), id="refused"),
        pytest.param(lambda: Says(UnsupportedFeatureError(SECRET)), id="no such feature"),
        pytest.param(lambda: Says(ModelNotFoundError("m", SECRET)), id="no such model"),
        pytest.param(lambda: Says(UnsupportedServerError(SECRET)), id="server too old"),
        pytest.param(lambda: Says(WORDS, TookTooLongError(SECRET)), id="cut at its time"),
        pytest.param(lambda: HeldAfter(), id="stopped before a word"),
        pytest.param(lambda: HeldAfter(WORDS), id="stopped after one"),
    ],
)
async def test_nobody_has_a_model_asked_last_with_requests_of_their_own_ten_times_running(
    script: Callable[[], ChatProvider], budgets: Budgets
) -> None:
    """A request too large or refused, a model or a feature the server hasn't got, an
    answer cut at its time on purpose, a turn stopped: none is the model's failure. The
    same ten times over a model that is too busy opens its breaker at the fifth."""
    model = offered("watched", chat=script())
    busy = offered("busy", chat=Says(ProviderUnavailableError(SECRET)))

    for _ in range(10):
        for each in (model, busy):
            client = asked_through(each, budgets, GUESS, lambda: NOON)
            if isinstance(each.chat, HeldAfter):
                reading = asyncio.create_task(to_its_end(client))
                await asyncio.wait_for(each.chat.waiting.wait(), 5)
                each.chat.waiting.clear()
                reading.cancel()
                with suppress(asyncio.CancelledError):
                    await asyncio.wait_for(reading, 5)
            else:
                await to_its_end(client)

    assert model.breaker.failing(NOON) is None
    assert busy.breaker.failing(NOON) == Failing(timedelta(seconds=30))


async def test_the_recording_client_hands_on_the_models_events_and_its_error_as_they_came(
    budgets: Budgets,
) -> None:
    """The same objects, through the counted client too: the walk sorts the error by
    its kind, and what the budget takes is as before."""
    thought, words, error = ReasoningDelta("Hm."), TextDelta("A chain"), ProviderError(SECRET)
    model = counted_model(Says(thought, words, error))
    received: list[StreamEvent] = []

    with pytest.raises(ProviderError) as raised:
        await read(asked_through(model, budgets, GUESS, lambda: NOON), into=received)

    assert [a is b for a, b in zip(received, (thought, words), strict=True)] == [True, True]
    assert raised.value is error
    assert await taken(budgets, model_budget(model)) == {
        "requests a minute": 1,
        "tokens a minute": GUESS + tokens_in("Hm." + "A chain"),
    }


async def test_a_refused_connection_is_a_failure_and_its_request_is_given_back_all_the_same(
    budgets: Budgets,
) -> None:
    """The counted client inside is handed the error as it came, and gives back what it
    took: nothing reached the server. Another failure keeps the request and the guess."""
    refused = counted_model(Says(ConnectionFailedError(SECRET)))
    busy = offered(
        "busy",
        chat=Says(ProviderUnavailableError(SECRET)),
        budget={"requests_a_minute": 10, "tokens_a_minute": 1_000},
    )

    for model in (refused, busy):
        for _ in range(5):
            await to_its_end(asked_through(model, budgets, GUESS, lambda: NOON))

    assert await taken(budgets, model_budget(refused)) == {
        "requests a minute": 0,
        "tokens a minute": 0,
    }
    assert await taken(budgets, model_budget(busy)) == {
        "requests a minute": 5,
        "tokens a minute": 5 * GUESS,
    }
    assert refused.breaker.failing(NOON) == Failing(timedelta(seconds=30))
    assert busy.breaker.failing(NOON) == Failing(timedelta(seconds=30))


class Tries:
    """A model whose every try follows the next of its scripts, and the last for each
    try after them."""

    def __init__(self, *scripts: tuple[StreamEvent | Exception, ...]) -> None:
        self.scripts = scripts
        self.tries = 0

    async def stream(self, request: ChatRequest) -> AsyncGenerator[StreamEvent]:
        assert request.messages
        script = self.scripts[min(self.tries, len(self.scripts) - 1)]
        self.tries += 1
        for item in script:
            if isinstance(item, Exception):
                raise item
            yield item


async def test_the_log_says_what_each_try_changed_by_the_models_name_and_numbers_alone(
    budgets: Budgets, caplog: pytest.LogCaptureFixture
) -> None:
    """Opened by its fifth failure, for 30 seconds; failed again once they were over,
    for a minute; and closed by an answer. Then opened again by three failures after two
    answers, which the line counts apart: three of five. Never an error's text, nor its
    kind."""
    busy = (ProviderUnavailableError(SECRET),)
    answers = (WORDS, FINISHED)
    model = offered("watched", chat=Tries(*[busy] * 6, answers, answers, busy))
    now = NOON
    client = asked_through(model, budgets, GUESS, lambda: now)

    for _ in range(5):
        await to_its_end(client)
    now += timedelta(seconds=30)
    await to_its_end(client)
    now += timedelta(minutes=1)
    for _ in range(2):
        await to_its_end(client)
    for _ in range(3):
        await to_its_end(client)

    assert caplog.messages == [
        "model watched: 5 of its last 5 requests failed, and for 30 s it is asked after"
        " models that aren't failing",
        "model watched: it failed again, and for 60 s more it is asked after models that"
        " aren't failing",
        "model watched: it answered, and is asked in its place again",
        "model watched: 3 of its last 5 requests failed, and for 30 s it is asked after"
        " models that aren't failing",
    ]
    assert SECRET not in caplog.text
    assert "ProviderUnavailableError" not in caplog.text


class Noting:
    """A model that notes in `log` when it is asked, and when its stream is closed."""

    def __init__(self, name: str, log: list[str], *script: StreamEvent | Exception) -> None:
        self.name = name
        self.log = log
        self.script = script

    async def stream(self, request: ChatRequest) -> AsyncGenerator[StreamEvent]:
        assert request.messages
        self.log.append(f"{self.name} asked")
        try:
            for item in self.script:
                if isinstance(item, Exception):
                    raise item
                yield item
        finally:
            self.log.append(f"{self.name} closed")


class NotedHold:
    """What a link holds while it is asked, as a gate: it notes in `log` when it is held
    and given back."""

    def __init__(self, name: str, log: list[str]) -> None:
        self.name = name
        self.log = log

    async def __aenter__(self) -> None:
        self.log.append(f"{self.name} held")

    async def __aexit__(self, *_: object) -> None:
        self.log.append(f"{self.name} given back")


async def test_through_a_walk_each_try_is_recorded_and_closed_in_the_walks_own_order(
    budgets: Budgets,
) -> None:
    """A model whose connection is refused is asked once more, and fails again: two
    failures, each try's stream closed before the next begins, and its gate given back
    once the second's is, before the next model's is held."""
    log: list[str] = []
    first = offered("first", chat=Noting("first", log, ConnectionFailedError(SECRET)))
    second = offered("second", chat=Noting("second", log, WORDS, FINISHED))
    walk = Walk(
        [
            Link(model.name, asked_through(model, budgets, GUESS, lambda: NOON), REQUEST, hold)
            for model, hold in (
                (first, NotedHold("gate 1", log)),
                (second, NotedHold("gate 2", log)),
            )
        ]
    )

    async with asyncio.timeout(5), aclosing(walk.stream()) as events:
        received = [event async for event in events]

    assert received == [WORDS, FINISHED]
    assert log == [
        "gate 1 held",
        "first asked",
        "first closed",
        "first asked",
        "first closed",
        "gate 1 given back",
        "gate 2 held",
        "second asked",
        "second closed",
        "gate 2 given back",
    ]
    # Two failures kept: the fifth is three more away.
    assert [first.breaker.begins(NOON).ends("failed", NOON) for _ in range(3)] == [
        None,
        None,
        Opened(5, 5, timedelta(seconds=30)),
    ]


# --- A provider's own wait ------------------------------------------------------------

DAY = timedelta(days=1)


@pytest.mark.parametrize(
    ("named", "kept_for"),
    [
        pytest.param(None, None, id="none"),
        pytest.param(math.nan, None, id="nan"),
        pytest.param(math.inf, None, id="inf"),
        pytest.param(-math.inf, None, id="-inf"),
        pytest.param(-1e308, None, id="a long time ago"),
        pytest.param(-1.0, None, id="a second ago"),
        pytest.param(0, None, id="no wait"),
        pytest.param(1, None, id="a second"),
        pytest.param(2.0, None, id="the walk's longest"),
        # Larger by the least a float can be, kept as the 2 s a `timedelta` holds of it.
        pytest.param(math.nextafter(2.0, math.inf), timedelta(seconds=2), id="the next larger"),
        pytest.param(2.001, timedelta(seconds=2.001), id="2.001 s"),
        pytest.param(49.846993202, timedelta(seconds=49.846993202), id="the Gemini API's minute"),
        pytest.param(83_280.0, timedelta(hours=23, minutes=8), id="the Gemini API's 23 hours"),
        pytest.param(86_399.5, timedelta(seconds=86_399.5), id="half a second short of a day"),
        pytest.param(86_400, DAY, id="a day"),
        pytest.param(86_401.0, DAY, id="a day and a second"),
        pytest.param(1e308, DAY, id="1e308 s"),
        pytest.param(sys.float_info.max, DAY, id="the largest float"),
    ],
)
def test_a_wait_a_provider_names_is_kept_when_longer_than_the_walks_and_for_a_day_at_most(
    named: float | None, kept_for: timedelta | None
) -> None:
    """The walk waits 2 s or less out itself, and asks once more: nothing is left of
    such a wait to keep. Nonsense is no wait, and a wait longer than a day is a day's,
    whatever it is: nothing raises."""
    assert believed(named) == kept_for
    assert BELIEVED_AT_MOST == DAY


def test_a_providers_wait_is_left_until_its_moment_and_its_newest_word_replaces_it() -> None:
    wait = OwnWait()
    nothing_yet = wait.left(NOON)
    wait.told(NOON + timedelta(seconds=49))
    before, nearly, at, after = (
        wait.left(NOON + timedelta(seconds=seconds)) for seconds in (0, 48.5, 49, 50)
    )
    wait.told(NOON + timedelta(minutes=5))
    later = wait.left(NOON)
    wait.told(NOON + timedelta(seconds=10))
    sooner = wait.left(NOON)

    assert nothing_yet is None
    assert (before, nearly, at, after) == (
        timedelta(seconds=49),
        timedelta(seconds=0.5),
        None,
        None,
    )
    assert (later, sooner) == (timedelta(minutes=5), timedelta(seconds=10))


def test_a_providers_wait_that_is_over_is_forgotten_and_says_whether_it_was_running() -> None:
    """Whether it was running at the moment given: at its own moment it was over
    already. Forgotten all the same, also for a moment before it."""
    wait = OwnWait()
    with_nothing_kept = wait.over(NOON, words=wait.words)
    wait.told(NOON + timedelta(seconds=49))
    running, again = wait.over(NOON, words=wait.words), wait.over(NOON, words=wait.words)
    forgotten = wait.left(NOON)
    wait.told(NOON + timedelta(seconds=49))
    at_its_moment = wait.over(NOON + timedelta(seconds=49), words=wait.words)

    assert (with_nothing_kept, running, again, forgotten) == (False, True, False, None)
    assert at_its_moment is False
    assert wait.left(NOON) is None


def test_an_answer_to_a_request_sent_before_the_providers_word_leaves_its_wait() -> None:
    """A try reads how many times the provider has said to wait when it begins. Its
    answer ends the wait only when the provider has said nothing since: one named
    while the try was under way stays, and so does a second word's after a first."""
    wait = OwnWait()
    before_any = wait.words
    wait.told(NOON + timedelta(seconds=49))
    after_the_first = wait.words
    sent_before = wait.over(NOON, words=before_any)
    still_left = wait.left(NOON)
    wait.told(NOON + timedelta(seconds=30))
    sent_between = wait.over(NOON, words=after_the_first)
    left_of_the_second = wait.left(NOON)
    sent_after = wait.over(NOON, words=wait.words)

    assert (before_any, after_the_first, wait.words) == (0, 1, 2)
    assert (sent_before, still_left) == (False, timedelta(seconds=49))
    assert (sent_between, left_of_the_second) == (False, timedelta(seconds=30))
    assert sent_after is True
    assert wait.left(NOON) is None


def told_to_wait(seconds: float | None) -> RateLimitedError:
    """A provider's 429 that names a wait, in words that must stay out of the log."""
    return RateLimitedError(f"429: slow down, {SECRET}", retry_after_s=seconds)


def waits_line(model: str, seconds: int) -> str:
    """The log's line for a model whose provider said to wait, and is believed."""
    return (
        f"model {model}: its provider said to wait {seconds} s, and until then it counts as"
        " having no budget left"
    )


def over_line(model: str) -> str:
    """The log's line for a model that answered while its provider's wait was running."""
    return (
        f"model {model}: it answered before its provider's wait was over, and counts as having"
        " budget again"
    )


async def test_a_429_that_names_a_longer_wait_than_the_walks_is_kept_from_the_end_of_its_try(
    budgets: Budgets, caplog: pytest.LogCaptureFixture
) -> None:
    """Dated by the clock when the try ended, 10 s after it began, and handed on as it
    came, the same object. The log says it by the model's name and the wait alone, in
    whole seconds, rounded up."""
    error = told_to_wait(49.2)
    model = offered("watched", chat=Says(error))
    begun_then_ended = iter([NOON, NOON + timedelta(seconds=10)])

    with pytest.raises(RateLimitedError) as raised:
        await read(asked_through(model, budgets, GUESS, lambda: next(begun_then_ended)), into=[])

    assert raised.value is error
    assert model.own_wait.left(NOON) == timedelta(seconds=59.2)
    assert model.own_wait.left(NOON + timedelta(seconds=59.2)) is None
    assert caplog.messages == [waits_line("watched", 50)]
    assert SECRET not in caplog.text


@pytest.mark.parametrize(
    "named",
    [None, 0, 0.01, 1, 2.0, math.nan, math.inf, -1.0],
    ids=["none", "no wait", "a moment", "a second", "2 s", "nan", "inf", "a second ago"],
)
async def test_a_429_whose_wait_the_walk_waits_out_or_that_names_none_keeps_nothing(
    named: float | None, budgets: Budgets, caplog: pytest.LogCaptureFixture
) -> None:
    """The error is handed on as it came, and nothing is said of a wait."""
    error = told_to_wait(named)
    model = offered("watched", chat=Says(error))

    with pytest.raises(RateLimitedError) as raised:
        await read(asked_through(model, budgets, GUESS, lambda: NOON), into=[])

    assert raised.value is error
    assert model.own_wait.left(NOON) is None
    assert caplog.messages == []


@pytest.mark.parametrize(
    "named",
    [86_400, 86_401, 1e308, sys.float_info.max],
    ids=["a day", "a day and a second", "1e308 s", "the largest float"],
)
async def test_a_429_that_names_a_day_or_more_is_kept_for_a_day(
    named: float, budgets: Budgets, caplog: pytest.LogCaptureFixture
) -> None:
    error = told_to_wait(named)
    model = offered("watched", chat=Says(error))

    with pytest.raises(RateLimitedError) as raised:
        await read(asked_through(model, budgets, GUESS, lambda: NOON), into=[])

    assert raised.value is error
    assert model.own_wait.left(NOON) == DAY
    assert caplog.messages == [waits_line("watched", 86_400)]


async def test_a_wait_kept_keeps_a_429s_request_and_guess_counted_in_the_budget_as_before(
    budgets: Budgets,
) -> None:
    """The counted client inside is handed the error as it came: a refused request was
    sent, and stays counted."""
    error = told_to_wait(49)
    model = counted_model(Says(error))

    with pytest.raises(RateLimitedError) as raised:
        await read(asked_through(model, budgets, GUESS, lambda: NOON), into=[])

    assert raised.value is error
    assert model.own_wait.left(NOON) == timedelta(seconds=49)
    assert await taken(budgets, model_budget(model)) == {
        "requests a minute": 1,
        "tokens a minute": GUESS,
    }


async def test_a_429_that_names_a_wait_is_still_a_failure_whose_breakers_line_comes_first(
    budgets: Budgets, caplog: pytest.LogCaptureFixture
) -> None:
    """Five of five tries open the breaker, and the fifth's end says that first."""
    model = offered("watched", chat=Says(told_to_wait(49)))
    client = asked_through(model, budgets, GUESS, lambda: NOON)

    for _ in range(5):
        await to_its_end(client)

    assert model.breaker.failing(NOON) == Failing(timedelta(seconds=30))
    assert caplog.messages == [
        *[waits_line("watched", 49)] * 4,
        "model watched: 5 of its last 5 requests failed, and for 30 s it is asked after"
        " models that aren't failing",
        waits_line("watched", 49),
    ]
    assert SECRET not in caplog.text
    assert "RateLimitedError" not in caplog.text


@pytest.mark.parametrize(
    ("script", "reading", "ends_it"),
    [
        pytest.param((WORDS, FINISHED), to_its_end, True, id="an answer"),
        pytest.param((THOUGHT, FINISHED), to_its_end, True, id="an answer of a thought"),
        pytest.param((WORDS, FINISHED), closed_after_one, True, id="closed after a word"),
        pytest.param((WORDS, TookTooLongError(SECRET)), to_its_end, True, id="cut at its time"),
        pytest.param(
            (TextDelta(""), WORDS, FINISHED), closed_after_one, False, id="closed before a word"
        ),
        pytest.param((ProviderUnavailableError(SECRET),), to_its_end, False, id="too busy"),
        pytest.param((WORDS,), to_its_end, False, id="no finish after a word"),
        pytest.param((ContextOverflowError(SECRET),), to_its_end, False, id="too large"),
        pytest.param((told_to_wait(1),), to_its_end, False, id="a 429 the walk waits out"),
        pytest.param((told_to_wait(None),), to_its_end, False, id="a 429 that names none"),
    ],
)
async def test_only_a_try_that_answered_ends_a_providers_wait_before_its_time(
    script: tuple[StreamEvent | Exception, ...],
    reading: Callable[[ChatProvider], Awaitable[None]],
    ends_it: bool,
    budgets: Budgets,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An answer, by the breaker's rule, ends it, with its line. Anything else leaves
    the wait as it was, and says nothing of it."""
    model = offered("watched", chat=Tries((told_to_wait(49),), script))
    client = asked_through(model, budgets, GUESS, lambda: NOON)
    await to_its_end(client)
    caplog.clear()

    await reading(client)

    assert model.own_wait.left(NOON) == (None if ends_it else timedelta(seconds=49))
    assert caplog.messages == ([over_line("watched")] if ends_it else [])


async def test_an_answer_under_way_when_the_provider_said_to_wait_doesnt_end_that_wait(
    budgets: Budgets, caplog: pytest.LogCaptureFixture
) -> None:
    """Two requests at once: the first is taken, and writes; the second is refused, with
    a wait of 49 s. The first one's answer ends after that, and was sent before it: the
    wait stays, and nothing is said. The next answer, sent after it, ends it."""
    model = offered(
        "watched", chat=Tries((WORDS, FINISHED), (told_to_wait(49),), (WORDS, FINISHED))
    )
    client = asked_through(model, budgets, GUESS, lambda: NOON)

    async with aclosing(client.stream(REQUEST)) as under_way:
        first_word = await anext(under_way)
        await to_its_end(client)  # refused meanwhile
        caplog.clear()
        rest = [event async for event in under_way]
    left_after_the_first = model.own_wait.left(NOON)
    said_after_the_first = list(caplog.messages)
    await to_its_end(client)

    assert (first_word, rest) == (WORDS, [FINISHED])
    assert left_after_the_first == timedelta(seconds=49)
    assert said_after_the_first == []
    assert model.own_wait.left(NOON) is None
    assert caplog.messages == [over_line("watched")]


async def test_an_answer_with_no_wait_running_says_nothing_of_one(
    budgets: Budgets, caplog: pytest.LogCaptureFixture
) -> None:
    """A model never told to wait, and one whose wait is over by the clock when it
    answers."""
    never_told = offered("never", chat=Says(WORDS, FINISHED))
    told = offered("watched", chat=Tries((told_to_wait(49),), (WORDS, FINISHED)))
    now = NOON
    await to_its_end(asked_through(never_told, budgets, GUESS, lambda: now))
    client = asked_through(told, budgets, GUESS, lambda: now)
    await to_its_end(client)
    now += timedelta(seconds=49)

    await to_its_end(client)

    assert caplog.messages == [waits_line("watched", 49)]


@pytest.mark.parametrize(
    ("words", "ends_it"), [((), False), ((WORDS,), True)], ids=["before a word", "after one"]
)
async def test_a_try_a_stop_cuts_off_ends_a_providers_wait_only_once_a_word_was_handed_on(
    words: tuple[StreamEvent, ...],
    ends_it: bool,
    budgets: Budgets,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Cancelled through a scope, as a turn's halt does, while its model waits."""
    chat = HeldAfter(*words)
    model = offered("watched", chat=chat)
    model.own_wait.told(NOON + timedelta(seconds=49))
    stop = anyio.CancelScope()

    async def turn() -> None:
        with stop:
            await read(asked_through(model, budgets, GUESS, lambda: NOON), into=[])

    running = asyncio.create_task(turn())
    await asyncio.wait_for(chat.waiting.wait(), 5)
    stop.cancel()
    await asyncio.wait_for(running, 5)

    assert stop.cancelled_caught
    assert model.own_wait.left(NOON) == (None if ends_it else timedelta(seconds=49))
    assert caplog.messages == ([over_line("watched")] if ends_it else [])


async def test_a_later_429_replaces_the_wait_kept_with_a_later_moment_or_a_sooner_one(
    budgets: Budgets, caplog: pytest.LogCaptureFixture
) -> None:
    """The provider's newest word, from the end of the try that heard it."""
    model = offered(
        "watched", chat=Tries((told_to_wait(49),), (told_to_wait(300),), (told_to_wait(5),))
    )
    now = NOON
    client = asked_through(model, budgets, GUESS, lambda: now)

    await to_its_end(client)
    now += timedelta(seconds=10)
    await to_its_end(client)
    later = model.own_wait.left(now)
    await to_its_end(client)
    sooner = model.own_wait.left(now)

    assert (later, sooner) == (timedelta(seconds=300), timedelta(seconds=5))
    assert caplog.messages == [
        waits_line("watched", 49),
        waits_line("watched", 300),
        waits_line("watched", 5),
    ]


def told_at_noon(model: OfferedModel, seconds: float) -> OfferedModel:
    """`model`, its provider having said at noon to wait `seconds`."""
    model.own_wait.told(NOON + timedelta(seconds=seconds))
    return model


def own(seconds: float) -> Spent:
    """What is said of a model whose provider's wait has `seconds` left."""
    return Spent(timedelta(seconds=seconds), PROVIDERS_WAIT)


def test_a_wait_is_said_in_whole_seconds_rounded_up_and_one_at_the_least() -> None:
    """As the log and a refusal say it. No wait that is kept or looked up is nothing, so
    the least shows only for one made by hand."""
    assert [own(seconds).seconds for seconds in (0, 0.2, 1, 48.5, 49, 49.2)] == [
        1,
        1,
        1,
        49,
        49,
        50,
    ]


def test_a_ready_model_whose_provider_said_to_wait_has_no_budget_until_its_moment() -> None:
    """Whether it has a budget or not, beside what the look said of the others, and
    for what is left of the wait. A model that isn't ready is never among them."""
    told, plain = told_at_noon(offered("told"), 49), offered("plain")
    away = told_at_noon(offered("away", budget={"requests_a_day": 1}), 49)
    chain = Chain(((told, READY), (plain, READY), (away, AWAY)))
    looked = {"plain": SPENT}

    found = chain.with_own_waits(looked, NOON)

    assert found == {"plain": SPENT, "told": own(49)}
    assert looked == {"plain": SPENT}  # what the look said is left as it was
    assert chain.with_own_waits(NONE_SPENT, NOON + timedelta(seconds=40)) == {"told": own(9)}
    assert chain.with_own_waits(NONE_SPENT, NOON + timedelta(seconds=49)) == {}
    assert str(own(49).limit) == "its provider's own wait"
    assert own(48.2).seconds == 49


@pytest.mark.parametrize(
    ("counted_for", "said"),
    [(60, "the count's"), (30, "the provider's"), (49, "the provider's")],
    ids=["the count's longer", "the count's shorter", "the two as long"],
)
def test_of_a_wait_by_the_count_and_the_providers_own_the_longer_is_said(
    counted_for: int, said: str
) -> None:
    model = told_at_noon(offered("both", budget={"requests_a_minute": 1}), 49)
    by_count = Spent(timedelta(seconds=counted_for), model_budget(model).buckets[0])

    found = Chain(((model, READY),)).with_own_waits({"both": by_count}, NOON)

    assert found == {"both": by_count if said == "the count's" else own(49)}


async def test_two_models_that_are_one_model_on_one_provider_are_told_to_wait_together() -> None:
    """A 429 through one name has the other asked last too. The same model's name on
    another provider has a wait of its own, and so has each other model."""
    text = (
        GOOD
        + AGAIN
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
"""
    )

    async with catalogs.build(parsed(text), ollama_url="http://ollama") as catalog:
        standard, big, again, elsewhere = (
            catalog.get(name) for name in ("standard", "big", "again", "elsewhere")
        )

    assert big.own_wait is again.own_wait
    assert elsewhere.own_wait is not big.own_wait
    assert standard.own_wait is not big.own_wait
    assert standard.own_wait is not elsewhere.own_wait
    told_at_noon(big, 49)
    chain = Chain(((again, READY), (elsewhere, READY), (standard, READY)))
    spent = chain.with_own_waits(NONE_SPENT, NOON)
    assert spent == {"again": own(49)}
    assert chain.asked_with(10, spent, NONE_FAILING) == Asked(
        ((elsewhere, READY), (standard, READY), (again, READY)), (), ((again, own(49)),)
    )


def test_by_the_rule_a_model_told_to_wait_is_asked_last_and_alone_is_refused() -> None:
    """As a model whose budget the look found spent: `asked_with` is as it was."""
    told, other = told_at_noon(offered("told"), 49), offered("other")
    both, alone = Chain(((told, READY), (other, READY))), Chain(((told, READY),))

    assert both.asked_with(1000, both.with_own_waits(NONE_SPENT, NOON), NONE_FAILING) == Asked(
        ((other, READY), (told, READY)), (), ((told, own(49)),)
    )
    assert alone.asked_with(1000, alone.with_own_waits(NONE_SPENT, NOON), NONE_FAILING) == Asked(
        (), (), ((told, own(49)),), refused=own(49)
    )


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
#
# An answer is named by its account and its number there: `A2` is account A's second.


class Answering:
    """An answer that passes a gate as its account's, and holds its place there until it
    is let go. Its account is its name without the number, unless one is given. `then`,
    when set, is called in the very step in which it leaves the gate, before any other
    task runs. With `told`, it asks the gate to be told how many answers have to end
    before it starts, and keeps each number it is told."""

    def __init__(
        self, gate: Gate, passed: list[str], name: str, account: str = "", *, told: bool = False
    ) -> None:
        self.name = name
        self.done = asyncio.Event()
        self.then: Callable[[], object] | None = None
        self.told: list[int] | None = [] if told else None
        """Each number it was told, in order, when it asked to be told: `None` when it
        didn't."""
        self.task = asyncio.create_task(
            self._pass(gate, passed, account or name.rstrip("0123456789"))
        )

    async def _pass(self, gate: Gate, passed: list[str], account: str) -> None:
        placed = None if self.told is None else self.told.append
        async with gate.passing(account, placed):
            passed.append(self.name)
            await self.done.wait()
        if self.then is not None:
            self.then()


def came(gate: Gate, passed: list[str], *names: str, told: bool = False) -> list[Answering]:
    """Answers that come to `gate` in this order, before the loop runs again: each asking
    to be told its number, with `told`."""
    return [Answering(gate, passed, name, told=told) for name in names]


async def settled() -> None:
    """Let every task that can go on do so."""
    for _ in range(5):
        await asyncio.sleep(0)


async def ends(*answers: Answering) -> None:
    """Let each answer go, in this order, each once the one before has left the gate and
    its place has gone on. One that hasn't passed yet leaves as soon as it passes."""
    for answer in answers:
        answer.done.set()
        await settled()


async def test_one_accounts_answers_pass_in_the_order_they_came_as_many_as_it_takes() -> None:
    gate, passed = Gate(2), list[str]()
    first, second, third, fourth = came(gate, passed, "A1", "A2", "A3", "A4")
    await settled()
    at_first, waiting = list(passed), gate.waiting

    await ends(second)
    then = list(passed)
    await asyncio.wait_for(ends(first, third, fourth), 1)

    assert (at_first, waiting) == (["A1", "A2"], 2)
    assert then == ["A1", "A2", "A3"]
    assert passed == ["A1", "A2", "A3", "A4"]
    assert (gate.full, gate.waiting, gate.accounts) == (False, 0, 0)


async def test_one_accounts_answers_at_once_dont_hold_up_another_accounts_first() -> None:
    """One answer at a time. A asks three at once, and B one a moment later: B's is
    answered second, not fourth."""
    gate, passed = Gate(1), list[str]()
    a1, a2, a3 = came(gate, passed, "A1", "A2", "A3")
    await settled()
    [b1] = came(gate, passed, "B1")
    await settled()

    await asyncio.wait_for(ends(a1, b1, a2, a3), 1)

    assert passed == ["A1", "B1", "A2", "A3"]
    assert (gate.full, gate.waiting, gate.accounts) == (False, 0, 0)


async def test_accounts_with_answers_waiting_take_turns_each_in_the_order_it_asked() -> None:
    """H holds the one place while A asks three, and B three after them."""
    gate, passed = Gate(1), list[str]()
    [h1] = came(gate, passed, "H1")
    await settled()
    a1, a2, a3, b1, b2, b3 = came(gate, passed, "A1", "A2", "A3", "B1", "B2", "B3")
    await settled()
    waiting = gate.waiting

    await asyncio.wait_for(ends(h1, a1, b1, a2, b2, a3, b3), 1)

    assert waiting == 6
    assert passed == ["H1", "A1", "B1", "A2", "B2", "A3", "B3"]


async def test_the_account_holding_fewer_places_goes_before_one_given_a_place_longer_ago() -> None:
    """Four at once. A's three pass, then B's first, and the server is full. A's fourth
    and B's second wait. When B's first ends, A holds three places and B none, so B's
    second passes, though A was given its last place before B was given its."""
    gate, passed = Gate(4), list[str]()
    a1, a2, a3, b1 = came(gate, passed, "A1", "A2", "A3", "B1")
    await settled()
    a4, b2 = came(gate, passed, "A4", "B2")
    await settled()
    full, waiting = gate.full, gate.waiting

    await ends(b1)
    then = list(passed)
    await asyncio.wait_for(ends(a1, a2, a3, b2, a4), 1)

    assert (full, waiting) == (True, 2)
    assert then == ["A1", "A2", "A3", "B1", "B2"]
    assert passed == ["A1", "A2", "A3", "B1", "B2", "A4"]


async def test_an_account_that_left_is_remembered_while_somebody_waits() -> None:
    """One answer at a time. A asks two at once, B and C one each. B's ends, and while
    C's is written, B asks again. A's second, which has waited all along, passes before
    B's: B was given a place after A was. Forgotten when it left, B would pass first, as
    an account never given a place."""
    gate, passed = Gate(1), list[str]()
    a1, a2, b1, c1 = came(gate, passed, "A1", "A2", "B1", "C1")
    await settled()
    await ends(a1, b1)
    [b2] = came(gate, passed, "B2")  # while C's first is written
    await settled()

    await asyncio.wait_for(ends(c1, a2, b2), 1)

    assert passed == ["A1", "B1", "C1", "A2", "B2"]


async def test_an_account_is_forgotten_once_nobody_waits() -> None:
    """A's first passes and ends while nobody waits. Then H holds the one place, and A's
    second comes, then C's first. A's passes first, having come first: were A still
    remembered, C's would, C never having been given a place."""
    gate, passed = Gate(1), list[str]()
    [a1] = came(gate, passed, "A1")
    await settled()
    await ends(a1)
    left_behind = gate.accounts
    [h1] = came(gate, passed, "H1")
    await settled()
    a2, c1 = came(gate, passed, "A2", "C1")
    await settled()

    await asyncio.wait_for(ends(h1, a2, c1), 1)

    assert left_behind == 0
    assert passed == ["A1", "H1", "A2", "C1"]


async def test_an_account_that_holds_a_place_is_remembered_though_nobody_waits() -> None:
    """One answer at a time. H's holds the place, and A's first waits. H's ends, A's
    first passes, and nobody waits: A, which holds the place, is remembered as given it.
    A's second comes then, and B's first after it. When A's first ends, B's passes, B
    never having been given a place, though A's second came before it."""
    gate, passed = Gate(1), list[str]()
    h1, a1 = came(gate, passed, "H1", "A1")
    await settled()
    await ends(h1)
    a2, b1 = came(gate, passed, "A2", "B1")
    await settled()

    await asyncio.wait_for(ends(a1, b1, a2), 1)

    assert passed == ["H1", "A1", "B1", "A2"]


async def test_the_gate_keeps_nothing_of_accounts_that_have_gone() -> None:
    """Guests are accounts, and there is no end of them. Forty come, each once: two pass,
    the others wait, and a third of those give up waiting. Once all have gone, the gate
    keeps what a new one keeps."""
    gate, passed = Gate(2), list[str]()
    answers = [
        Answering(gate, passed, str(number), account=f"guest {number}") for number in range(40)
    ]
    await settled()
    while_they_wait = gate.accounts
    giving_up = answers[2::3]  # none of the first two, which hold the places
    for answer in giving_up:
        answer.task.cancel()
    await settled()
    after_some_gave_up = gate.accounts

    await asyncio.wait_for(ends(*(answer for answer in answers if answer not in giving_up)), 1)

    assert (while_they_wait, after_some_gave_up) == (40, 40 - len(giving_up))
    assert len(passed) == 40 - len(giving_up)
    assert (gate.full, gate.waiting, gate.accounts) == (False, 0, Gate(2).accounts)


async def test_an_answer_that_gives_up_waiting_leaves_the_others_as_the_rule_has_them() -> None:
    """One answer at a time. A's first holds the place, and B's and C's wait. B's passes
    when A's ends, and A, which left, is remembered while C's waits. D's comes and gives
    up waiting, stopped: nothing changes for the others, and A is still remembered. A's
    second and E's first come: C's passes, then E's, never given a place, then A's."""
    gate, passed = Gate(1), list[str]()
    a1, b1, c1 = came(gate, passed, "A1", "B1", "C1")
    await settled()
    await ends(a1)
    [d1] = came(gate, passed, "D1")
    await settled()

    d1.task.cancel()  # stopped while it waits
    await settled()
    afterwards = (list(passed), gate.full, gate.waiting)
    a2, e1 = came(gate, passed, "A2", "E1")
    await settled()
    await asyncio.wait_for(ends(b1, c1, e1, a2), 1)

    assert afterwards == (["A1", "B1"], True, 1)
    assert d1.task.cancelled()
    assert passed == ["A1", "B1", "C1", "E1", "A2"]
    assert (gate.full, gate.waiting, gate.accounts) == (False, 0, 0)


async def test_a_place_handed_to_an_answer_cancelled_in_that_step_goes_on_by_the_rule() -> None:
    """One answer at a time, held by this test as A's. B's first, A's second, C's first
    and B's second wait. The test leaves, the place is handed to B's first, and that one
    is cancelled before it could go on with it: in the very step, before the loop runs
    again. The place goes on to C's, by the rule, and not to A's second, which came
    before it: A was given a place, C never. B counts as given that place: so B's second
    passes after A's second, A having been given its last place before B was. Not
    counted, B's second would pass before A's."""
    gate, passed = Gate(1), list[str]()
    async with asyncio.timeout(1), gate.passing("A"):
        b1, a2, c1, b2 = came(gate, passed, "B1", "A2", "C1", "B2")
        await settled()
    handed_on = (gate.full, gate.waiting)
    b1.task.cancel()  # before B's first could go on with the place it was handed
    await settled()
    then = (list(passed), gate.full, gate.waiting, gate.accounts)

    await asyncio.wait_for(ends(c1, a2, b2), 1)

    assert handed_on == (True, 3)
    assert b1.task.cancelled()
    assert then == (["C1"], True, 2, 3)  # C holds the place, and A and B wait
    assert passed == ["C1", "A2", "B2"]
    assert (gate.full, gate.waiting, gate.accounts) == (False, 0, 0)


async def test_an_answer_cancelled_in_the_step_a_place_comes_free_isnt_handed_it() -> None:
    """One answer at a time, held by this test as H's. A's first and B's first wait. A's
    is cancelled, and in the very step, before it could leave its line, the place is
    given back: it goes to B's, and isn't lost with A's. And when the one cancelled so
    is the only one waiting, C's, nobody is left to hand the place to: it is free, and
    an answer that comes in that same step, D's, passes without waiting."""
    gate, passed = Gate(1), list[str]()
    async with asyncio.timeout(1), gate.passing("H"):
        a1, b1 = came(gate, passed, "A1", "B1")
        await settled()
        a1.task.cancel()  # stopped while it waits: its task hasn't run again yet
    await settled()
    then = (list(passed), gate.full, gate.waiting)
    await asyncio.wait_for(ends(b1), 1)

    async with asyncio.timeout(1), gate.passing("H"):
        [c1] = came(gate, passed, "C1")
        await settled()
        c1.task.cancel()
    # Before the loop runs again: C's is in its line still.
    async with asyncio.timeout(1), gate.passing("D"):
        held_by_d = gate.full
    await settled()

    assert then == (["B1"], True, 0)
    assert held_by_d
    assert a1.task.cancelled()
    assert c1.task.cancelled()
    assert passed == ["B1"]
    assert (gate.full, gate.waiting, gate.accounts) == (False, 0, 0)


async def test_once_the_last_answer_waiting_gives_up_the_absent_are_forgotten() -> None:
    """One answer at a time. A's first holds the place, B's and C's wait. A's ends, B's
    passes, and A, which left, is remembered while C's waits. C's gives up: nobody waits
    now, and the gate keeps nothing of A, nor of C. So A's second, which comes then,
    passes before D's first, which comes after it: were A still remembered, D's would
    pass first, D never having been given a place."""
    gate, passed = Gate(1), list[str]()
    a1, b1, c1 = came(gate, passed, "A1", "B1", "C1")
    await settled()
    await ends(a1)
    while_one_waits = gate.accounts

    c1.task.cancel()  # stopped while it waits, the last one waiting
    await settled()
    once_it_gave_up = gate.accounts
    a2, d1 = came(gate, passed, "A2", "D1")
    await settled()
    await asyncio.wait_for(ends(b1, a2, d1), 1)

    assert (while_one_waits, once_it_gave_up) == (3, 1)  # A, B and C; then B alone
    assert passed == ["A1", "B1", "A2", "D1"]
    assert (gate.full, gate.waiting, gate.accounts) == (False, 0, 0)


async def test_of_accounts_alike_the_one_whose_waiting_answer_came_first_passes_first() -> None:
    """One answer at a time, held by H's. A's first comes to wait, then B's first, then
    A's second, and A's first gives up. Neither A nor B holds a place or was ever given
    one: B's first passes before A's second, having come before it, though A has had an
    answer waiting here for longer than B."""
    gate, passed = Gate(1), list[str]()
    [h1] = came(gate, passed, "H1")
    await settled()
    a1, b1, a2 = came(gate, passed, "A1", "B1", "A2")
    await settled()
    a1.task.cancel()  # stopped while it waits
    await settled()

    await asyncio.wait_for(ends(h1, b1, a2), 1)

    assert a1.task.cancelled()
    assert passed == ["H1", "B1", "A2"]


async def test_a_place_given_back_no_longer_counts_as_its_accounts_when_the_next_is_chosen() -> (
    None
):
    """Three at once. A's two pass, then B's first, and the server is full. B's second
    comes to wait, then A's third. When A's first ends, A holds one place and B one: so
    A's third passes, A having been given its last place before B was, though B's second
    came first. Were the place just given back still counted as A's, B's would pass."""
    gate, passed = Gate(3), list[str]()
    a1, a2, b1 = came(gate, passed, "A1", "A2", "B1")
    await settled()
    b2, a3 = came(gate, passed, "B2", "A3")
    await settled()

    await ends(a1)
    then = list(passed)
    await asyncio.wait_for(ends(a2, b1, a3, b2), 1)

    assert then == ["A1", "A2", "B1", "A3"]
    assert passed == ["A1", "A2", "B1", "A3", "B2"]


async def test_a_place_at_the_gate_is_given_back_however_the_answer_ended() -> None:
    gate = Gate(1)

    with pytest.raises(RuntimeError):
        async with asyncio.timeout(1), gate.passing("A"):
            raise RuntimeError("the model failed")
    after_a_failure = gate.full

    async def holds_its_place() -> None:
        async with gate.passing("A"):
            await asyncio.Event().wait()

    holding = asyncio.create_task(holds_its_place())
    await settled()
    while_held = gate.full
    holding.cancel()  # from outside, as a process going down does
    await asyncio.wait_for(asyncio.gather(holding, return_exceptions=True), 1)

    assert (after_a_failure, while_held, gate.full) == (False, True, False)


# How many answers have to end before a waiting one starts: what the gate tells an answer
# that asks (ADR 0023, revision "a turn says that it waits, and behind how many answers").


async def test_an_answer_that_waits_is_told_its_number_as_it_comes_and_one_that_passes_nothing() -> (
    None
):
    """One answer at a time. A's first passes at once, and is told nothing. B's comes,
    and is told 1 in the very step in which it comes: before the loop runs again."""
    gate, passed = Gate(1), list[str]()
    [a1] = came(gate, passed, "A1", told=True)
    await settled()

    [b1] = came(gate, passed, "B1", told=True)
    await asyncio.sleep(0)  # B's task takes its first step: it comes to wait
    as_it_came = (gate.waiting, list(b1.told or []))
    await asyncio.wait_for(ends(a1, b1), 1)

    assert as_it_came == (1, [1])
    assert a1.told == []
    assert b1.told == [1]  # nothing more: that its turn has come, it sees by passing
    assert passed == ["A1", "B1"]


async def test_the_gate_tells_an_answer_once_its_books_are_complete() -> None:
    """When an answer is told, the one that comes is in its line already, and a place
    given back has gone on, to an answer out of its line. Each answer is told what it
    sees then: one place held, and how many wait."""
    gate, passed = Gate(1), list[str]()
    seen: list[tuple[str, int, int, bool]] = []

    async def waits(account: str) -> None:
        def placed(behind: int) -> None:
            seen.append((account, behind, gate.waiting, gate.full))

        async with gate.passing(account, placed):
            passed.append(account)

    async with asyncio.timeout(1):
        async with gate.passing("H"):
            first = asyncio.create_task(waits("A"))
            await settled()
            second = asyncio.create_task(waits("B"))
            await settled()
        await asyncio.wait_for(asyncio.gather(first, second), 1)

    assert seen == [
        ("A", 1, 1, True),  # A in its line, as the one place is held
        ("B", 2, 2, True),
        ("B", 1, 1, True),  # the place went to A, out of its line, before B was told
    ]
    assert passed == ["A", "B"]


async def test_answers_are_told_the_order_the_rule_has_them_pass_and_one_less_as_each_ends() -> (
    None
):
    """One answer at a time. A's first holds the place; A's second, B's first and C's
    first come, in that order. By the rule B's passes first, then C's, then A's second:
    they are told 1, 2 and 3, not in the order they came. Each is told one less as each
    place goes on, and nobody the same number twice."""
    gate, passed = Gate(1), list[str]()
    [a1] = came(gate, passed, "A1", told=True)
    await settled()
    a2, b1, c1 = came(gate, passed, "A2", "B1", "C1", told=True)
    await settled()
    once_all_came = [(answer.told or [])[-1] for answer in (b1, c1, a2)]

    await asyncio.wait_for(ends(a1, b1, c1, a2), 1)

    assert once_all_came == [1, 2, 3]
    assert passed == ["A1", "B1", "C1", "A2"]
    # A's second was told 1 alone, then behind B's, then behind C's too.
    assert a2.told == [1, 2, 3, 2, 1]
    assert b1.told == [1]
    assert c1.told == [2, 1]


async def test_an_answer_is_told_a_higher_number_when_one_that_goes_before_it_comes() -> None:
    """One answer at a time. A's first holds the place, and A's second waits behind it:
    1. B's first comes, B never having been given a place: it goes before A's second,
    which is told 2."""
    gate, passed = Gate(1), list[str]()
    [a1] = came(gate, passed, "A1", told=True)
    await settled()
    [a2] = came(gate, passed, "A2", told=True)
    await settled()
    before = list(a2.told or [])

    [b1] = came(gate, passed, "B1", told=True)
    await settled()
    after = list(a2.told or [])
    await asyncio.wait_for(ends(a1, b1, a2), 1)

    assert (before, after) == ([1], [1, 2])
    assert b1.told == [1]
    assert passed == ["A1", "B1", "A2"]


async def test_when_an_answer_gives_up_waiting_those_behind_it_are_told_and_those_before_not() -> (
    None
):
    """One answer at a time, held by H's. A's, B's and C's first come: 1, 2 and 3. B's
    gives up waiting: C's is told 2, and A's nothing."""
    gate, passed = Gate(1), list[str]()
    [h1] = came(gate, passed, "H1")
    await settled()
    a1, b1, c1 = came(gate, passed, "A1", "B1", "C1", told=True)
    await settled()

    b1.task.cancel()  # stopped while it waits
    await settled()
    await asyncio.wait_for(ends(h1, a1, c1), 1)

    assert b1.task.cancelled()
    assert a1.told == [1]
    assert b1.told == [2]
    assert c1.told == [3, 2, 1]
    assert passed == ["H1", "A1", "C1"]


async def test_an_answer_cancelled_before_its_accounts_next_isnt_played_forward_in_its_place() -> (
    None
):
    """One answer at a time, held by this test as H's. B's first, B's second and C's
    first wait: 1, 3 and 2, since B's second goes after C's once B has had a place. B's
    first is cancelled, and before it could leave its line, D's first comes, which has
    everyone told anew: B's first waits no more, so B's second is the next to pass, then
    C's, then D's. B's second is told 1, once, though the answer that stands before it
    in its account's line is the cancelled one: it isn't taken out of the forward play
    in its place. And nobody is told anything more when the cancelled one has left."""
    gate, passed = Gate(1), list[str]()
    async with asyncio.timeout(1), gate.passing("H"):
        b1, b2, c1 = came(gate, passed, "B1", "B2", "C1", told=True)
        await settled()
        while_all_wait = [list(answer.told or []) for answer in (b1, b2, c1)]
        # D's task is to run before B's first runs again: so it comes first, and B's
        # first, cancelled after, is in its line still when D's comes to wait.
        [d1] = came(gate, passed, "D1", told=True)
        b1.task.cancel()  # stopped while it waits
        await settled()
        then = [list(answer.told or []) for answer in (b2, c1, d1)]
    await asyncio.wait_for(ends(b2, c1, d1), 1)

    assert while_all_wait == [[1], [2, 3], [2]]
    assert then == [[2, 3, 1], [2], [3]]
    assert b1.task.cancelled()
    assert b1.told == [1]
    assert b2.told == [2, 3, 1]
    assert passed == ["B2", "C1", "D1"]


async def test_an_answer_cancelled_in_the_step_a_place_comes_free_counts_for_nobodys_number() -> (
    None
):
    """One answer at a time, held by this test as H's. A's, B's and C's first wait: 1, 2
    and 3. A's is cancelled, and in the very step, before it could leave its line, the
    place is given back: it goes to B's, and C's is told 1, not 2. A's is told nothing
    more."""
    gate, passed = Gate(1), list[str]()
    async with asyncio.timeout(1), gate.passing("H"):
        a1, b1, c1 = came(gate, passed, "A1", "B1", "C1", told=True)
        await settled()
        a1.task.cancel()  # stopped while it waits: its task hasn't run again yet
    told_then = list(c1.told or [])
    await settled()
    await asyncio.wait_for(ends(b1, c1), 1)

    assert told_then == [3, 1]
    assert a1.task.cancelled()
    assert a1.told == [1]
    assert b1.told == [2]
    assert c1.told == [3, 1]
    assert passed == ["B1", "C1"]


async def test_after_an_answer_handed_a_place_is_cancelled_the_others_numbers_are_right() -> None:
    """One answer at a time, held by this test as A's. B's first, A's second, C's first
    and B's second wait: 1, 3, 2 and 4. The test leaves, the place is handed to B's
    first, and that one is cancelled in that very step: the place goes on to C's, by the
    rule. A's second is told 1 then, and B's second 2, and they pass so."""
    gate, passed = Gate(1), list[str]()
    async with asyncio.timeout(1), gate.passing("A"):
        b1, a2, c1, b2 = came(gate, passed, "B1", "A2", "C1", "B2", told=True)
        await settled()
        while_held = [(answer.told or [])[-1] for answer in (b1, a2, c1, b2)]
    b1.task.cancel()  # before B's first could go on with the place it was handed
    await settled()
    then = [(answer.told or [])[-1] for answer in (a2, b2)]

    await asyncio.wait_for(ends(c1, a2, b2), 1)

    assert while_held == [1, 3, 2, 4]
    assert then == [1, 2]
    assert passed == ["C1", "A2", "B2"]
    assert c1.told == [2, 1]
    assert a2.told == [2, 3, 2, 1]
    assert b2.told == [4, 3, 2, 1]


async def test_at_more_than_one_at_once_the_places_held_come_back_in_the_order_they_were_given() -> (
    None
):
    """Two at once. A's first, then B's first, hold the places; A's second, B's second
    and C's first come. Played forward with A's place coming back first: C's passes, C
    never having been given a place; then B's comes back, and A's second passes, A
    having been given its place before B; then B's second. So they are told 2, 3 and 1,
    and pass so when the places come back in that order. Which place comes back first
    decides before C's has come, while A's second and B's second wait alone: with A's
    place first, A's second is told 1 and B's second 2, as they are; played forward with
    B's place first, it would be the other way round."""
    gate, passed = Gate(2), list[str]()
    a1, b1 = came(gate, passed, "A1", "B1", told=True)
    await settled()
    a2, b2, c1 = came(gate, passed, "A2", "B2", "C1", told=True)
    await settled()
    once_all_came = [(answer.told or [])[-1] for answer in (a2, b2, c1)]

    await asyncio.wait_for(ends(a1, b1, c1, a2, b2), 1)

    assert once_all_came == [2, 3, 1]
    assert passed == ["A1", "B1", "C1", "A2", "B2"]
    assert a2.told == [1, 2, 1]
    assert b2.told == [2, 3, 2, 1]
    assert c1.told == [1]


async def test_an_answer_whose_telling_raises_is_logged_and_the_others_are_told_all_the_same(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One answer at a time, held by this test as H's. C's first comes, then an answer
    whose telling raises each time, then D's first: 1, 2 and 3. The test leaves: C's
    passes, the one whose telling raises is told 1, which raises in the test's own step,
    and D's is told 2 all the same. The place isn't lost, nothing reaches the test, and
    the log says it with its trace, and nothing of whose answer it is."""
    gate, passed = Gate(1), list[str]()
    raised: list[int] = []

    def raises(behind: int) -> None:
        raised.append(behind)
        raise RuntimeError("a reader's mistake")

    async def raising() -> None:
        async with gate.passing("the one whose telling raises", raises):
            passed.append("R1")
            await let_go.wait()

    let_go = asyncio.Event()
    async with asyncio.timeout(1), gate.passing("H"):
        [c1] = came(gate, passed, "C1", told=True)
        await settled()
        their = asyncio.create_task(raising())
        await settled()
        [d1] = came(gate, passed, "D1", told=True)
        await settled()
    await settled()
    then = (list(passed), gate.full, gate.waiting)
    await asyncio.wait_for(ends(c1), 1)
    let_go.set()
    await asyncio.wait_for(their, 1)
    await asyncio.wait_for(ends(d1), 1)

    assert raised == [2, 1]
    assert then == (["C1"], True, 2)
    assert passed == ["C1", "R1", "D1"]
    assert c1.told == [1]
    assert d1.told == [3, 2, 1]
    logged = [record for record in caplog.records if record.name == "actio.catalog"]
    assert [record.getMessage() for record in logged] == [
        "an answer that waits at a gate couldn't be told its number"
    ] * 2
    assert all(record.exc_info is not None for record in logged)
    assert "the one whose telling raises" not in caplog.text
    assert (gate.full, gate.waiting, gate.accounts) == (False, 0, 0)


async def test_an_answer_that_asked_to_be_told_nothing_counts_in_the_others_numbers() -> None:
    """As a title waits: `passing(account)` alone. One answer at a time, held by H's. A's
    first, a title of T's and B's first come: A's is told 1 and B's 3, the title counted
    between them, and told nothing itself."""
    gate, passed = Gate(1), list[str]()
    [h1] = came(gate, passed, "H1")
    await settled()
    [a1] = came(gate, passed, "A1", told=True)
    [title] = came(gate, passed, "T1")
    [b1] = came(gate, passed, "B1", told=True)
    await settled()

    await asyncio.wait_for(ends(h1, a1, title, b1), 1)

    assert passed == ["H1", "A1", "T1", "B1"]
    assert a1.told == [1]
    assert title.told is None
    assert b1.told == [3, 2, 1]


async def test_nothing_is_played_forward_while_no_waiting_answer_asked_to_be_told(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Answers that asked nothing, as titles do, cost the gate what they cost before it
    told anybody anything: no forward play of the rule at any change."""
    played: list[int] = []
    order = Gate._order

    def counted(gate: Gate) -> Any:
        played.append(1)
        return order(gate)

    monkeypatch.setattr(Gate, "_order", counted)
    gate, passed = Gate(1), list[str]()
    a1, b1, c1 = came(gate, passed, "A1", "B1", "C1")
    await settled()
    c1.task.cancel()
    await settled()
    without = len(played)
    await asyncio.wait_for(ends(a1, b1), 1)
    [d1] = came(gate, passed, "D1", told=True)
    await settled()
    await asyncio.wait_for(ends(d1), 1)

    assert (without, len(played)) == (0, 0)  # D's passed at once, and was never told
    [e1, f1] = came(gate, passed, "E1", "F1", told=True)
    await settled()
    assert len(played) == 1  # F's came to wait
    await asyncio.wait_for(ends(e1, f1), 1)
    # Once those that asked have gone, the others cost nothing again.
    g1, h1 = came(gate, passed, "G1", "H1")
    await settled()
    await asyncio.wait_for(ends(g1, h1), 1)
    assert len(played) == 1
    assert passed == ["A1", "B1", "D1", "E1", "F1", "G1", "H1"]


@dataclass
class PlainGate:
    """A gate written the plain way, from its rules, with no asyncio: which answers hold
    a place, which wait, and the number of each remembered account's last place."""

    at_once: int
    holding: dict[str, str] = field(default_factory=dict)
    """Each answer that holds a place, with its account."""
    waiting: list[tuple[str, str]] = field(default_factory=list)
    """Each answer that waits, with its account, in the order they came."""
    last: dict[str, int] = field(default_factory=dict)
    given: int = 0
    passed: list[str] = field(default_factory=list)
    """The answers that went on with the place they were given, in the order they did."""

    def comes(self, answer: str, account: str) -> None:
        if len(self.holding) < self.at_once:
            self._gives(answer, account)
            self.passed.append(answer)
        else:
            self.waiting.append((answer, account))

    def gives_up(self, answer: str) -> None:
        self.waiting = [each for each in self.waiting if each[0] != answer]
        self._forgets()

    def ends(self, answer: str) -> None:
        del self.holding[answer]
        handed = self._hands_on()
        if handed is not None:
            self.passed.append(handed)

    def ends_and_cancels(self, answer: str) -> str | None:
        """`answer` ends, and the answer its place is handed to is cancelled before it
        goes on with it: that one counts as given the place, and gives it back at once.
        Which answer that is, if one waited."""
        del self.holding[answer]
        handed = self._hands_on()
        if handed is not None:
            del self.holding[handed]
            then = self._hands_on()
            if then is not None:
                self.passed.append(then)
        return handed

    @property
    def kept(self) -> int:
        """How many accounts it keeps anything of."""
        waiting = {account for _, account in self.waiting}
        return len(set(self.holding.values()) | waiting | set(self.last))

    def _hands_on(self) -> str | None:
        """The answer a place that was given back goes to, if one waits."""
        handed = None
        if self.waiting:
            held = Counter(self.holding.values())
            handed, account = min(
                self.waiting,
                key=lambda each: (
                    held[each[1]],
                    self.last.get(each[1], 0),
                    self.waiting.index(each),
                ),
            )
            self.waiting.remove((handed, account))
            self._gives(handed, account)
        self._forgets()
        return handed

    def _gives(self, answer: str, account: str) -> None:
        self.given += 1
        self.last[account] = self.given
        self.holding[answer] = account

    def _forgets(self) -> None:
        if not self.waiting:
            holders = set(self.holding.values())
            self.last = {account: last for account, last in self.last.items() if account in holders}


GATE_STEPS = st.lists(
    st.one_of(
        st.tuples(st.just("comes"), st.sampled_from("ABCD")),
        st.tuples(st.just("comes"), st.sampled_from("ABCD")),
        st.tuples(st.just("ends"), st.integers(0, 3)),
        st.tuples(st.just("ends, and the next is cancelled"), st.integers(0, 3)),
        st.tuples(st.just("gives up"), st.integers(0, 9)),
    ),
    max_size=50,
)
"""Answers of four accounts that come, more often than anything else, end, end with the
answer handed their place cancelled in that step, and give up waiting: each by its
place among those that hold a place, or that wait, counted round."""


@settings(deadline=None)  # each example runs an event loop of its own
@given(at_once=st.integers(1, 4), steps=GATE_STEPS)
def test_the_gate_passes_answers_as_the_plain_one_does_whatever_comes_ends_and_gives_up(
    at_once: int, steps: list[tuple[str, Any]]
) -> None:
    """At every step the same answers have passed, in the same order, as through the
    plain gate, and the same hold a place and wait; never more than `at_once` hold one,
    none is free while one waits, and each passes once at the most; it keeps what the
    plain gate keeps of accounts, and once nobody waits, nothing of those that hold no
    place. Once every answer has ended, it keeps nothing."""

    async def play() -> None:
        gate, plain = Gate(at_once), PlainGate(at_once)
        passed: list[str] = []
        answers: dict[str, Answering] = {}

        def check() -> None:
            inside = [name for name in passed if not answers[name].task.done()]
            assert passed == plain.passed
            assert len(passed) == len(set(passed))
            assert sorted(inside) == sorted(plain.holding)
            assert len(inside) <= at_once
            assert gate.waiting == len(plain.waiting)
            if gate.waiting:
                assert len(inside) == at_once
            assert gate.full == (len(inside) == at_once)
            assert gate.accounts == plain.kept
            if not gate.waiting:
                assert gate.accounts == len(set(plain.holding.values()))

        for number, (kind, which) in enumerate(steps):
            holding, waiting = list(plain.holding), [name for name, _ in plain.waiting]
            match kind:
                case "comes":
                    name = f"{which}{number}"
                    answers[name] = Answering(gate, passed, name)
                    plain.comes(name, which)
                case "ends" if holding:
                    name = holding[which % len(holding)]
                    answers[name].done.set()
                    plain.ends(name)
                case "ends, and the next is cancelled" if holding:
                    name = holding[which % len(holding)]
                    handed = plain.ends_and_cancels(name)
                    if handed is not None:
                        answers[name].then = answers[handed].task.cancel
                    answers[name].done.set()
                case "gives up" if waiting:
                    name = waiting[which % len(waiting)]
                    answers[name].task.cancel()
                    plain.gives_up(name)
            await settled()
            check()
        while plain.holding:
            name = next(iter(plain.holding))
            answers[name].done.set()
            plain.ends(name)
            await settled()
            check()
        assert (gate.full, gate.waiting, gate.accounts) == (False, 0, 0)

    asyncio.run(asyncio.wait_for(play(), 10))


@settings(deadline=None)  # each example runs an event loop of its own
@given(at_once=st.integers(1, 4), steps=GATE_STEPS)
# Three at once: A's, B's and C's hold the places, and D's two and C's second wait. As the
# rule is played forward, D's first takes A's place, and when B's comes back D holds one
# place and C one, C's given longer ago: C's second passes second. Played forward without
# counting the place D's first was given, D's second would.
@example(
    at_once=3,
    steps=[("comes", account) for account in "ABCDDC"],
)
def test_the_answers_pass_in_the_order_of_the_numbers_they_were_last_told(
    at_once: int, steps: list[tuple[str, Any]]
) -> None:
    """After any history of answers that come, end, end with the answer handed their
    place cancelled in that step, and give up waiting, at a gate of one to four places,
    nobody comes or leaves, and the places held come back in the order they were given.
    Then each waiting answer that asked to be told passes as the one whose number it was
    last told, counted among all that wait: so those numbers are each its own, from 1 up.
    D's answers ask to be told nothing, as a title's, and count in the others' numbers."""

    async def play() -> None:
        gate, plain = Gate(at_once), PlainGate(at_once)
        passed: list[str] = []
        answers: dict[str, Answering] = {}

        for number, (kind, which) in enumerate(steps):
            holding, waiting = list(plain.holding), [name for name, _ in plain.waiting]
            match kind:
                case "comes":
                    name = f"{which}{number}"
                    answers[name] = Answering(gate, passed, name, told=which != "D")
                    plain.comes(name, which)
                case "ends" if holding:
                    name = holding[which % len(holding)]
                    answers[name].done.set()
                    plain.ends(name)
                case "ends, and the next is cancelled" if holding:
                    name = holding[which % len(holding)]
                    handed = plain.ends_and_cancels(name)
                    if handed is not None:
                        answers[name].then = answers[handed].task.cancel
                    answers[name].done.set()
                case "gives up" if waiting:
                    name = waiting[which % len(waiting)]
                    answers[name].task.cancel()
                    plain.gives_up(name)
            await settled()

        waiting = [name for name, _ in plain.waiting]
        last_told = {name: told[-1] for name in waiting if (told := answers[name].told) is not None}
        before = len(passed)
        # The place given the longest ago comes back first: the plain gate keeps the
        # answers that hold one in the order they were given it.
        while plain.holding:
            name = next(iter(plain.holding))
            answers[name].done.set()
            plain.ends(name)
            await settled()
        then = passed[before:]

        assert sorted(then) == sorted(waiting)
        assert {name: then.index(name) + 1 for name in last_told} == last_told
        assert (gate.full, gate.waiting, gate.accounts) == (False, 0, 0)

    asyncio.run(asyncio.wait_for(play(), 10))


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


def each_own(chains: dict[str, Chain]) -> dict[str, ModelStatus]:
    """Each model's own status, as every model's chain says it (`Catalog.chains`)."""
    return {name: chain.status for name, chain in chains.items()}


async def test_a_server_that_does_not_answer_is_unreachable_not_an_error() -> None:
    transports = {"ollama": unreachable(), "lab": unreachable()}

    async with catalogs.build(
        parsed(GOOD), ollama_url="http://ollama", transports=transports
    ) as catalog:
        statuses = each_own(await catalog.chains(now=NOON))

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
        statuses = each_own(await catalog.chains(now=NOON))

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


async def test_every_models_chain_is_given_with_what_its_server_says() -> None:
    ollama = FakeOllama({"qwen3.5:4b-q4_K_M": ["completion", "thinking"]})
    transports: dict[str, Any] = {
        "ollama": ollama.transport,
        "lab": httpx2.ASGITransport(app=create_fake_llm()),
    }

    async with catalogs.build(
        parsed(GOOD), ollama_url="http://ollama", transports=transports
    ) as catalog:
        chains = await catalog.chains(now=NOON)

    assert list(chains) == ["standard", "big"]  # the file's order
    assert [[model.name for model, _ in chain.models] for chain in chains.values()] == [
        ["standard"],
        ["big"],
    ]
    assert each_own(chains) == {
        "standard": ModelStatus("ready", True),
        "big": ModelStatus("ready", True),
    }


# --- What is known of a model, while its server is asked again ------------------------

PATIENCE_S = 5.0
"""How long a test waits for the catalog to get somewhere. It takes milliseconds; this
only keeps a catalog that waits for an answer held for ever from hanging the run."""
AGED = timedelta(seconds=20)
"""How old what is known is, in most tests below: older than a quarter of a minute, and
far from five minutes."""


class ListsWhenLetGo(httpx2.AsyncBaseTransport):
    """A server that speaks OpenAI's API, and lists `fake-chat`: at once, until a test
    holds its answers (`held`), and then each once that event is set. Counts how often it
    was asked, and says whether its client had been closed by the time an answer it held
    was given up on."""

    def __init__(self) -> None:
        self.asked = 0
        self.held: asyncio.Event | None = None
        self.holding = asyncio.Event()
        """Set once an answer is held."""
        self.given_up = 0
        self.closed = False
        self.closed_before_given_up: bool | None = None
        """`None` while no answer it held has been given up on."""

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/v1/models"
        self.asked += 1
        if self.held is not None:
            self.holding.set()
            try:
                await self.held.wait()
            except asyncio.CancelledError:
                self.given_up += 1
                self.closed_before_given_up = self.closed
                raise
        return httpx2.Response(200, json={"object": "list", "data": [{"id": "fake-chat"}]})

    async def aclose(self) -> None:
        self.closed = True


async def test_a_turns_start_is_given_a_ready_older_than_a_quarter_of_a_minute_at_once(
    asks_ended: Any,
) -> None:
    """Through the catalog `build` makes, its server's answer held: none of three
    requests of a turn's start waits for it, and its server is asked once. The answer
    is dated by the `now` of the request that had it asked, not by a later one's: it is
    reused for a quarter of a minute from that `now`, and asked again behind the first
    request after. The time limit fails a catalog that waits."""
    lab = ListsWhenLetGo()
    held = asyncio.Event()
    ready = ModelStatus("ready", thinking=True)  # `thinking = true` in the file

    async with (
        asyncio.timeout(PATIENCE_S),
        catalogs.build(
            parsed(GOOD), ollama_url="http://ollama", transports={"lab": lab}
        ) as catalog,
    ):
        first = await catalog.status("big", now=NOON)
        lab.held = held
        old = await catalog.chain_as_known("big", now=NOON + timedelta(seconds=15))
        await lab.holding.wait()
        meanwhile = await asyncio.gather(
            catalog.chain_as_known("big", now=NOON + AGED),
            catalog.chain_as_known("big", now=NOON + AGED),
        )
        asked_while_held = lab.asked
        held.set()
        await asks_ended()
        within = await catalog.chain_as_known("big", now=NOON + timedelta(seconds=29.9))
        asked_within = lab.asked
        lab.held = None
        after = await catalog.chain_as_known("big", now=NOON + timedelta(seconds=30))
        await asks_ended()

    assert first == ready
    assert [chain.status for chain in (old, *meanwhile, within, after)] == [ready] * 5
    assert (asked_while_held, asked_within, lab.asked) == (2, 2, 3)


async def test_everything_but_a_turns_start_waits_for_an_old_ready_and_shares_its_ask(
    lineup: Any,
) -> None:
    """In the same moment, the servers' answers held: `chain_status`, `status` and
    `chains` wait for them, as the list, the warm-up and a title do, while a turn's start
    is given the old `ready` at once. One call to each server serves them all."""
    lineup.add("standard", then=["second"])
    lineup.add("second")
    catalog = lineup.catalog()
    await catalog.chain_status("standard", now=NOON)
    held = [lineup.hold("standard"), lineup.hold("second")]
    later = NOON + AGED

    async with asyncio.timeout(PATIENCE_S):
        waiting = [
            asyncio.create_task(catalog.chain_status("standard", now=later)),
            asyncio.create_task(catalog.status("standard", now=later)),
            asyncio.create_task(catalog.chains(now=later)),
        ]
        turn = await catalog.chain_as_known("standard", now=later)
        for each in held:
            await each.asked.wait()
        await settled()
        done_while_held = [each.done() for each in waiting]
        for each in held:
            each.answers.set()
        chain, status, chains = await asyncio.gather(*waiting)

    assert done_while_held == [False, False, False]
    assert [model.name for model, _ in turn.ready] == ["standard", "second"]
    assert chain.status == status == chains["standard"].status == READY
    assert lineup.asked == ["standard", "second", "standard", "second"]


@pytest.mark.parametrize(
    ("known_for", "waits"),
    [
        (None, True),
        (timedelta(minutes=5) - timedelta(microseconds=1), False),
        (timedelta(minutes=5), True),
    ],
    ids=["nothing known", "five minutes less a moment", "five minutes"],
)
async def test_a_turns_start_uses_an_old_ready_for_five_minutes_less_a_moment_and_waits_after(
    known_for: timedelta | None, waits: bool, lineup: Any, asks_ended: Any
) -> None:
    """Two requests of a turn's start at once, the server's answer held: both wait for
    it, or neither does, and its server is asked once."""
    lineup.add("standard")
    catalog = lineup.catalog()
    now = NOON
    if known_for is not None:
        await catalog.status("standard", now=NOON)
        now = NOON + known_for
    held = lineup.hold("standard")

    async with asyncio.timeout(PATIENCE_S):
        asking = [
            asyncio.create_task(catalog.chain_as_known("standard", now=now)) for _ in range(2)
        ]
        await held.asked.wait()
        await settled()
        waited = [not each.done() for each in asking]
        held.answers.set()
        said = await asyncio.gather(*asking)
        await asks_ended()

    assert waited == [waits, waits]
    assert [chain.status for chain in said] == [READY, READY]
    assert lineup.asked == ["standard"] * (1 if known_for is None else 2)


@pytest.mark.parametrize("state", ["unreachable", "missing"])
async def test_a_model_last_seen_not_ready_is_passed_over_at_once_while_a_stand_in_is_known_ready(
    state: Any, lineup: Any, asks_ended: Any
) -> None:
    """By a turn's start: its server is asked again behind two requests at once, neither
    of which waits for it, and the request after them has its answer: it is back, and
    asked first."""
    lineup.add("standard", state=state, then=["second"])
    lineup.add("second")
    catalog = lineup.catalog()
    await catalog.chain_status("standard", now=NOON)
    lineup.becomes("standard", "ready")
    held = lineup.hold("standard")

    async with asyncio.timeout(PATIENCE_S):
        at_once = await asyncio.gather(
            catalog.chain_as_known("standard", now=NOON + AGED),
            catalog.chain_as_known("standard", now=NOON + AGED + timedelta(seconds=1)),
        )
        await held.asked.wait()
        held.answers.set()
        await asks_ended()
        after = await catalog.chain_as_known("standard", now=NOON + AGED + timedelta(seconds=2))

    for chain in at_once:
        assert chain.status.state == state
        assert [model.name for model, _ in chain.ready] == ["second"]
        assert [model.name for model, _ in chain.passed_over] == ["standard"]
    assert [model.name for model, _ in after.ready] == ["standard", "second"]
    # Each asked at first, and once more behind the two requests: not after them.
    assert lineup.asked == ["standard", "second", "standard", "second"]


async def test_a_chain_none_of_whose_models_is_known_ready_waits_for_its_old_statuses_at_once(
    lineup: Any,
) -> None:
    """Nothing is refused by an old status, at a turn's start. The two old ones are asked
    again and waited for together, both servers holding their answers at the same time,
    and the chain is as they answer. The one asked a moment ago is not asked again, and
    stays as it is."""
    lineup.add("standard", state="unreachable", then=["second", "third"])
    lineup.add("second", state="missing")
    lineup.add("third", state="unreachable")
    catalog = lineup.catalog()
    await catalog.chain_status("standard", now=NOON)
    # Old by then, and not ready: asked again, and waited for.
    await catalog.status("third", now=NOON + timedelta(seconds=15))
    lineup.becomes("standard", "ready")
    lineup.becomes("second", "ready")
    held = [lineup.hold("standard"), lineup.hold("second")]

    async with asyncio.timeout(PATIENCE_S):
        asking = asyncio.create_task(catalog.chain_as_known("standard", now=NOON + AGED))
        for each in held:
            await each.asked.wait()
        await settled()
        waited = not asking.done()
        for each in held:
            each.answers.set()
        chain = await asking

    assert waited
    assert states(chain) == [
        ("standard", "ready"),
        ("second", "ready"),
        ("third", "unreachable"),
    ]
    assert lineup.asked == ["standard", "second", "third", "third", "standard", "second"]


def states(chain: Chain) -> list[tuple[str, str]]:
    """Each model of a chain, by its name, with its state."""
    return [(model.name, status.state) for model, status in chain.models]


async def test_a_turns_start_waits_for_no_old_status_while_a_fresh_one_is_ready(
    lineup: Any, asks_ended: Any
) -> None:
    """The asked model was asked a moment ago, and is ready; its stand-in was last seen
    unreachable 20 s ago, and its server holds its answer. Whether a model is ready is
    judged among them all, the fresh one too: the request doesn't wait, and the
    stand-in is passed over by its old status, its server asked behind."""
    lineup.add("standard", then=["second"])
    lineup.add("second", state="unreachable")
    catalog = lineup.catalog()
    await catalog.chain_status("standard", now=NOON)
    await catalog.status("standard", now=NOON + timedelta(seconds=15))  # fresh by then
    held = lineup.hold("second")

    async with asyncio.timeout(PATIENCE_S):
        chain = await catalog.chain_as_known("standard", now=NOON + AGED)
        await held.asked.wait()
        held.answers.set()
        await asks_ended()

    assert states(chain) == [("standard", "ready"), ("second", "unreachable")]
    assert lineup.asked == ["standard", "second", "standard", "second"]


async def test_a_turns_start_asks_old_and_unknown_at_once_and_waits_for_the_unknown_alone(
    lineup: Any, asks_ended: Any
) -> None:
    """Nothing is known of the asked model, and its stand-in was last seen unreachable
    20 s ago. Both servers are asked at once, and hold their answers at the same moment
    before either is let go. The asked model answers that it is ready: the request has
    what it needs, and doesn't wait for the stand-in's server, which still holds its
    answer."""
    lineup.add("standard", then=["second"])
    lineup.add("second", state="unreachable")
    catalog = lineup.catalog()
    await catalog.status("second", now=NOON)
    held = {name: lineup.hold(name) for name in ("standard", "second")}

    async with asyncio.timeout(PATIENCE_S):
        asking = asyncio.create_task(catalog.chain_as_known("standard", now=NOON + AGED))
        for each in held.values():
            await each.asked.wait()
        await settled()
        waited = not asking.done()
        held["standard"].answers.set()
        chain = await asking
        held["second"].answers.set()
        await asks_ended()

    assert waited
    assert states(chain) == [("standard", "ready"), ("second", "unreachable")]
    assert lineup.asked == ["second", "standard", "second"]


async def test_a_turns_start_waits_for_the_unknown_then_for_the_old_ask_already_under_way(
    lineup: Any,
) -> None:
    """A chain of three: the asked model asked a moment ago and unreachable, the first
    stand-in last seen unreachable 20 s ago, and nothing known of the second. The two
    stand-ins' servers are asked at once. The second answers that it is unreachable too:
    no model is ready, so the request waits for the first stand-in's ask, the one under
    way already, and no other; it answers that it is back."""
    lineup.add("standard", state="unreachable", then=["second", "third"])
    lineup.add("second", state="unreachable")
    lineup.add("third", state="unreachable")
    catalog = lineup.catalog()
    await catalog.status("second", now=NOON)
    await catalog.status("standard", now=NOON + timedelta(seconds=15))
    lineup.becomes("second", "ready")
    held = {name: lineup.hold(name) for name in ("second", "third")}

    async with asyncio.timeout(PATIENCE_S):
        asking = asyncio.create_task(catalog.chain_as_known("standard", now=NOON + AGED))
        for each in held.values():
            await each.asked.wait()
        held["third"].answers.set()
        await settled()
        waited_for_the_old_one = not asking.done()
        held["second"].answers.set()
        chain = await asking

    assert waited_for_the_old_one
    assert states(chain) == [
        ("standard", "unreachable"),
        ("second", "ready"),
        ("third", "unreachable"),
    ]
    assert lineup.asked == ["second", "standard", "second", "third"]


async def test_a_turns_start_reads_an_old_status_again_once_it_has_waited(
    lineup: Any,
) -> None:
    """Nothing is known of the asked model, and its stand-in was last seen ready 20 s
    ago, whose server says now that it is unreachable, and answers first. The asked
    model's server answers that it is unreachable too: the chain is as both answered,
    and no model of it is ready. The `ready` known when the request came is not used
    after the wait."""
    lineup.add("standard", state="unreachable", then=["second"])
    lineup.add("second")
    catalog = lineup.catalog()
    await catalog.status("second", now=NOON)
    lineup.becomes("second", "unreachable")
    held = {name: lineup.hold(name) for name in ("standard", "second")}

    async with asyncio.timeout(PATIENCE_S):
        asking = asyncio.create_task(catalog.chain_as_known("standard", now=NOON + AGED))
        for each in held.values():
            await each.asked.wait()
        [stand_ins] = [task for task in asyncio.all_tasks() if task.get_name() == "status-second"]
        held["second"].answers.set()
        await asyncio.wait({stand_ins})
        held["standard"].answers.set()
        chain = await asking

    assert states(chain) == [("standard", "unreachable"), ("second", "unreachable")]
    assert chain.ready == []


async def test_a_request_waits_for_every_ask_and_is_raised_the_first_mistake_in_its_chain(
    lineup: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Three asks, waited for together: the third fails first, then the second, and
    the first answers last. The request ends only then, and is raised the second's
    mistake, the first of its chain's order. A request is raised one error, so the
    catalog's log says the third's, with its trace: no mistake is said nowhere."""
    lineup.add("standard", then=["second", "third"])
    lineup.add("second")
    lineup.add("third")
    catalog: Catalog = lineup.catalog()
    let_go = {name: asyncio.Event() for name in ("standard", "second", "third")}

    async def answers_when_let_go(offered: OfferedModel) -> ModelStatus:
        await let_go[offered.name].wait()
        if offered.name != "standard":
            raise RuntimeError(f"a mistake of Actio's own about {offered.name}")
        return READY

    monkeypatch.setattr(catalog, "_ask", answers_when_let_go)
    async with asyncio.timeout(PATIENCE_S):
        asking = asyncio.create_task(catalog.chain_status("standard", now=NOON))
        ended: list[bool] = []
        for name in ("third", "second", "standard"):
            await settled()
            ended.append(asking.done())
            let_go[name].set()
        await asyncio.wait({asking})

    assert ended == [False, False, False]
    raised = asking.exception()
    assert isinstance(raised, RuntimeError)
    assert str(raised) == "a mistake of Actio's own about second"
    assert said_by_the_catalog(caplog) == [
        (logging.ERROR, "model third: asking its server about it failed")
    ]
    [logged] = [record for record in caplog.records if record.name == "actio.catalog"]
    assert logged.exc_info is not None
    assert str(logged.exc_info[1]) == "a mistake of Actio's own about third"
    assert logged.exc_info[2] is not None  # its trace, as the ask left it


class SlipError(Exception):
    """A mistake of Actio's own, of a kind a test can watch go: a weak reference can't
    be made to an error of Python's own kinds."""


async def test_each_request_that_waits_is_raised_the_mistake_with_the_asks_trace_and_its_own(
    lineup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two requests wait for one ask that fails, and are raised the same error one after
    the other. Each finds in its trace the ask's frames and its own, and never the
    frames of the request that was raised it before."""
    lineup.add("standard")
    catalog: Catalog = lineup.catalog()
    let_go = asyncio.Event()

    async def breaks(offered: OfferedModel) -> ModelStatus:
        await let_go.wait()
        raise SlipError(offered.name)

    async def first_waiter() -> list[str]:
        try:
            await catalog.status("standard", now=NOON)
        except SlipError as error:
            return [frame.name for frame in traceback.extract_tb(error.__traceback__)]
        return []

    async def second_waiter() -> list[str]:
        try:
            await catalog.chain_as_known("standard", now=NOON)
        except SlipError as error:
            return [frame.name for frame in traceback.extract_tb(error.__traceback__)]
        return []

    monkeypatch.setattr(catalog, "_ask", breaks)
    async with asyncio.timeout(PATIENCE_S):
        waiting = [asyncio.create_task(first_waiter()), asyncio.create_task(second_waiter())]
        await settled()
        let_go.set()
        first, second = await asyncio.gather(*waiting)

    assert [name for name in first if name.endswith("_waiter")] == ["first_waiter"]
    assert [name for name in second if name.endswith("_waiter")] == ["second_waiter"]
    # Where it went wrong is in both: the ask's own frames, last.
    assert first[-1] == second[-1] == "breaks"


async def test_a_mistake_raised_to_a_request_is_not_kept_with_the_models_last_ask(
    lineup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The error a request was raised holds that request's frames, and what they hold of
    it. Once the request is over, the catalog holds nothing of the error: it is gone
    when the test lets go of it, before the model is asked about again."""
    lineup.add("standard")
    catalog: Catalog = lineup.catalog()

    async def breaks(offered: OfferedModel) -> ModelStatus:
        raise SlipError(offered.name)

    monkeypatch.setattr(catalog, "_ask", breaks)
    async with asyncio.timeout(PATIENCE_S):
        asking = asyncio.create_task(catalog.status("standard", now=NOON))
        await asyncio.wait({asking})
    raised = asking.exception()
    assert isinstance(raised, SlipError)
    gone = weakref.ref(raised)
    del raised, asking
    await settled()
    gc.collect()

    assert gone() is None


async def test_a_request_that_needs_a_status_asked_behind_another_waits_for_that_ask(
    lineup: Any,
) -> None:
    """`standard` stands in for `first`, and was last seen unreachable: a turn's start
    for `first` has it asked behind it. A request for `standard` alone, by `status`,
    asks and waits, and waits for that same ask: its server is asked once."""
    lineup.add("first", then=["standard"])
    lineup.add("standard", state="unreachable")
    catalog = lineup.catalog()
    await catalog.chain_status("first", now=NOON)
    lineup.becomes("standard", "ready")
    held = lineup.hold("standard")

    async with asyncio.timeout(PATIENCE_S):
        behind = await catalog.chain_as_known("first", now=NOON + AGED)
        await held.asked.wait()
        alone = asyncio.create_task(
            catalog.status("standard", now=NOON + AGED + timedelta(seconds=1))
        )
        await settled()
        waited = not alone.done()
        held.answers.set()
        status = await alone

    assert [model.name for model, _ in behind.ready] == ["first"]
    assert waited
    assert status == READY
    assert lineup.asked == ["first", "standard", "first", "standard"]


@pytest.mark.parametrize("others", [0, 1], ids=["alone", "beside another"])
async def test_a_request_that_stops_waiting_leaves_the_ask_to_go_on_and_its_answer_kept(
    others: int, lineup: Any, asks_ended: Any
) -> None:
    """As a title's time limit cuts its wait short: a request that waits beside it still
    gets the answer, and the request after them asks nobody."""
    lineup.add("standard")
    catalog = lineup.catalog()
    held = lineup.hold("standard")

    async with asyncio.timeout(PATIENCE_S):
        cut = asyncio.create_task(catalog.status("standard", now=NOON))
        beside = [asyncio.create_task(catalog.status("standard", now=NOON)) for _ in range(others)]
        await held.asked.wait()
        cut.cancel()
        await asyncio.wait({cut})
        held.answers.set()
        answers = await asyncio.gather(*beside)
        await asks_ended()
        after = await catalog.status("standard", now=NOON + timedelta(seconds=1))

    assert cut.cancelled()
    assert answers == [READY] * others
    assert after == READY
    assert lineup.asked == ["standard"]


def said_by_the_catalog(caplog: pytest.LogCaptureFixture) -> list[tuple[int, str]]:
    return [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if record.name == "actio.catalog"
    ]


async def test_an_ask_behind_a_request_that_fails_in_actios_own_code_is_logged_and_asked_again(
    lineup: Any,
    asks_ended: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Nobody waits for it, behind a turn's start, so the log says it, by the model's
    name, with its trace, and the error ends there: the task itself ends well. What was
    known is still used, and the next request asks again: no ask is left as if under
    way."""
    lineup.add("standard")
    catalog = lineup.catalog()
    await catalog.status("standard", now=NOON)
    tries = 0

    async def breaks(offered: OfferedModel) -> ModelStatus:
        nonlocal tries
        tries += 1
        raise RuntimeError(f"a mistake of Actio's own about {offered.name}")

    monkeypatch.setattr(catalog, "_ask", breaks)
    async with asyncio.timeout(PATIENCE_S):
        later = await catalog.chain_as_known("standard", now=NOON + AGED)
        [behind] = [task for task in asyncio.all_tasks() if task.get_name() == "status-standard"]
        await asyncio.wait({behind})
        again = await catalog.chain_as_known("standard", now=NOON + AGED + timedelta(seconds=1))
        await asks_ended()

    assert later.status == again.status == READY
    assert behind.exception() is None
    assert tries == 2
    assert (
        said_by_the_catalog(caplog)
        == [(logging.ERROR, "model standard: asking its server about it failed")] * 2
    )
    logged = [record for record in caplog.records if record.name == "actio.catalog"]
    assert all(record.exc_info and record.exc_info[0] is RuntimeError for record in logged)


async def test_an_ask_that_fails_as_its_only_waiter_is_cancelled_is_logged(
    lineup: Any,
    asks_ended: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The ask fails in the very step in which its one waiter was cancelled, before the
    waiter has gone on: nobody is told the mistake, so the log says it."""
    lineup.add("standard")
    catalog = lineup.catalog()
    asked, let_go = asyncio.Event(), asyncio.Event()

    async def breaks(offered: OfferedModel) -> ModelStatus:
        asked.set()
        await let_go.wait()
        raise RuntimeError(f"a mistake of Actio's own about {offered.name}")

    monkeypatch.setattr(catalog, "_ask", breaks)
    async with asyncio.timeout(PATIENCE_S):
        waiter = asyncio.create_task(catalog.status("standard", now=NOON))
        await asked.wait()
        # Nothing awaited between the two: the ask's step comes first, the waiter's after.
        let_go.set()
        waiter.cancel()
        await asyncio.wait({waiter})
        await asks_ended()

    assert waiter.cancelled()
    assert said_by_the_catalog(caplog) == [
        (logging.ERROR, "model standard: asking its server about it failed")
    ]


async def test_an_ask_that_fails_in_actios_own_code_is_raised_to_each_request_that_waits(
    lineup: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Two requests at once, for a model nothing is known of: both are told, by the
    error itself, from one ask, and the catalog's log says nothing of it: whoever is told
    says it. Nothing was kept, so the next request asks again, and has its answer."""
    lineup.add("standard")
    catalog: Catalog = lineup.catalog()
    asks = catalog._ask
    let_go = asyncio.Event()
    tries = 0

    async def breaks_once(offered: OfferedModel) -> ModelStatus:
        nonlocal tries
        tries += 1
        if tries == 1:
            await let_go.wait()
            raise RuntimeError("a mistake of Actio's own")
        return await asks(offered)

    monkeypatch.setattr(catalog, "_ask", breaks_once)
    async with asyncio.timeout(PATIENCE_S):
        asking = [asyncio.create_task(catalog.status("standard", now=NOON)) for _ in range(2)]
        await settled()
        let_go.set()
        await asyncio.wait(asking)
        after = await catalog.status("standard", now=NOON)

    assert [type(each.exception()) for each in asking] == [RuntimeError, RuntimeError]
    assert after == READY
    assert tries == 2
    assert said_by_the_catalog(caplog) == []


async def test_an_ask_under_way_is_given_up_on_before_its_servers_client_is_closed() -> None:
    """One behind a turn's start, whose server never answers: `build`'s exit cancels it
    and waits for it, while the client is still open, and none is left pending."""
    lab = ListsWhenLetGo()

    async with (
        asyncio.timeout(PATIENCE_S),
        catalogs.build(
            parsed(GOOD), ollama_url="http://ollama", transports={"lab": lab}
        ) as catalog,
    ):
        await catalog.status("big", now=NOON)
        lab.held = asyncio.Event()  # nothing sets it: only a cancellation ends the ask
        behind = await catalog.chain_as_known("big", now=NOON + AGED)
        await lab.holding.wait()

    assert behind.status == ModelStatus("ready", thinking=True)
    assert lab.closed
    assert (lab.given_up, lab.closed_before_given_up) == (1, False)
    assert [task for task in asyncio.all_tasks() if task.get_name().startswith("status-")] == []


async def test_every_models_chain_is_found_at_once_and_a_model_in_several_is_asked_once(
    lineup: Any,
) -> None:
    """As the list of models asks: the three servers hold their answers at the same
    time, and `second`, in all three chains, is asked once."""
    lineup.add("standard", then=["second"])
    lineup.add("second")
    lineup.add("third", then=["second"])
    held = [lineup.hold(name) for name in ("standard", "second", "third")]
    catalog = lineup.catalog()

    async with asyncio.timeout(PATIENCE_S):
        listing = asyncio.create_task(catalog.chains(now=NOON))
        for each in held:
            await each.asked.wait()
        for each in held:
            each.answers.set()
        chains = await listing

    assert lineup.asked == ["standard", "second", "third"]
    assert {name: [model.name for model, _ in chain.ready] for name, chain in chains.items()} == {
        "standard": ["standard", "second"],
        "second": ["second"],
        "third": ["third", "second"],
    }
