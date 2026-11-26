"""`make preflight` asks the cloud providers what an account may use, with its keys, and
holds the models file against what they state.

`scripts/preflight.py` runs here against a fake of both providers. Its lists, answers
and headers are shaped as the providers' documentation shows them, and its refusals of
a key as each provider answered a request made with a made-up key. Seen with real keys
on 2026-11-25, Groq's list entries carried the window and the longest answer, and no
answer of the Gemini API carried a header about limits: the fake leaves the longest
answer out of some entries, to show what is said without it. The Gemini API's answer of
a model too busy, 503, is as it was seen on 2026-11-26, and its refusal at a limit is
shaped as two answers saved on 2026-11-25 were, with values made up. Other refusals are
made up here, in no shape their provider was seen to give: Groq's at a rate limit and
its 503, the Gemini API's 403, and the 500 and the 502 of a server's other errors. So
are the bodies that are broken on purpose. A request the fake
doesn't expect fails the test, and a test that must see no request at all gives it a
fake that fails at any. What a run prints is read for the facts in its lines, not for
their wording.

Each run reads a models file that its test wrote under its own tmp_path and named, by
default one without a table at either provider, as the shipped one is: each provider is
then checked as itself, with its own variable and its own models, as the script did
before it read a file. No test reads the repository's `config/` by the directory pytest
was started in: the tests of the shipped files read them by a path made from this
file's own, and the test of the default path changes directory to its own folder.
"""

import io
import json
import re
import time
import unicodedata
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx2
import pytest
from scripts import preflight

from actio.catalog import LIMITS, CatalogError, read_file

# Two keys that share no piece as long as the pieces the script hides, so that one
# sent to the other provider, or hidden in its text, would show.
GROQ_KEY = "key-for-tests"
GEMINI_KEY = "made-up-value"
BOTH = {"GROQ_API_KEY": GROQ_KEY, "GEMINI_API_KEY": GEMINI_KEY}
# A third, for a variable a table of the models file names, that shares no such piece
# with either.
OTHER_KEY = "another-secret"

GROQ_HOST = "api.groq.com"
GOOGLE_HOST = "generativelanguage.googleapis.com"

# The headers of Groq's documentation, with the values of its example.
GROQ_LIMITS = {
    "x-ratelimit-limit-requests": "14400",
    "x-ratelimit-limit-tokens": "18000",
    "x-ratelimit-remaining-requests": "14370",
    "x-ratelimit-remaining-tokens": "17997",
    "x-ratelimit-reset-requests": "2m59.56s",
    "x-ratelimit-reset-tokens": "7.66s",
}

# Groq's list of models. Its entries carried the longest answer on 2026-11-25: one here
# does, and the others don't, to show what is said without it.
GROQ_MODELS: list[dict[str, Any]] = [
    {
        "id": "openai/gpt-oss-120b",
        "object": "model",
        "created": 1754408224,
        "owned_by": "OpenAI",
        "active": True,
        "context_window": 131072,
        "public_apps": None,
        "max_completion_tokens": 65536,
    },
    {
        "id": "openai/gpt-oss-20b",
        "object": "model",
        "created": 1754407957,
        "owned_by": "OpenAI",
        "active": True,
        "context_window": 131072,
        "public_apps": None,
    },
    {
        "id": "llama-3.1-8b-instant",
        "object": "model",
        "created": 1693721698,
        "owned_by": "Meta",
        "active": True,
        "context_window": 131072,
        "public_apps": None,
    },
]


ASKED = ("generateContent", "countTokens")
EMBEDS = ("embedContent", "countTokens")


def gemini_model(
    model: str, *, thinking: bool | None = True, methods: tuple[str, ...] | str | None = ASKED
) -> dict[str, Any]:
    """A model as the Gemini API describes it, and lists it. `None` leaves a member out."""
    described: dict[str, Any] = {
        "name": f"models/{model}",
        "baseModelId": model,
        "version": "001",
        "displayName": model.replace("-", " ").title(),
        "description": "A fictional model of these tests.",
        "inputTokenLimit": 1048576,
        "outputTokenLimit": 65536,
        "temperature": 1,
        "maxTemperature": 2,
        "topP": 0.95,
        "topK": 64,
    }
    if methods is not None:
        described["supportedGenerationMethods"] = methods
    if thinking is not None:
        described["thinking"] = thinking
    return described


# The Gemini API's list of models, over two pages: one default model is on the second,
# beside one that makes embeddings and can't be asked a question.
GEMINI_PAGES: dict[str | None, tuple[list[dict[str, Any]], str | None]] = {
    None: ([gemini_model("gemini-3.8-flash"), gemini_model("gemini-3.6-flash")], "page-2"),
    "page-2": (
        [gemini_model("gemini-3.5-flash-lite"), gemini_model("text-embedding-005", methods=EMBEDS)],
        None,
    ),
}

GROQ_REFUSES_THE_KEY = {
    "error": {
        "message": "Invalid API Key",
        "type": "invalid_request_error",
        "code": "invalid_api_key",
    }
}
GEMINI_REFUSES_THE_KEY = {
    "error": {
        "code": 400,
        "message": "API key not valid. Please pass a valid API key.",
        "status": "INVALID_ARGUMENT",
        "details": [
            {
                "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                "reason": "API_KEY_INVALID",
                "domain": "googleapis.com",
                "metadata": {"service": "generativelanguage.googleapis.com"},
            },
            {
                "@type": "type.googleapis.com/google.rpc.LocalizedMessage",
                "locale": "en-US",
                "message": "API key not valid. Please pass a valid API key.",
            },
        ],
    }
}
# Its OpenAI-compatible surface answers with a list of one error.
GEMINI_REFUSES_THE_QUESTION = [
    {"error": {"code": 400, "message": "Please pass a valid API key", "status": "INVALID_ARGUMENT"}}
]

type Answer = Callable[[httpx2.Request], httpx2.Response]


def asked(request: httpx2.Request) -> str:
    """The model a question asks."""
    model = json.loads(request.content)["model"]
    assert isinstance(model, str)
    return model


def call_of(request: httpx2.Request) -> str:
    """Which call a request is, as the fake names it: `groq list`, `groq ask <model>`,
    `gemini list`, `gemini describe <model>`, `gemini ask <model>`."""
    method, host, path = request.method, request.url.host, request.url.path
    if (method, host, path) == ("GET", GROQ_HOST, "/openai/v1/models"):
        return "groq list"
    if (method, host, path) == ("POST", GROQ_HOST, "/openai/v1/chat/completions"):
        return f"groq ask {asked(request)}"
    if (method, host, path) == ("GET", GOOGLE_HOST, "/v1beta/models"):
        return "gemini list"
    if (method, host) == ("GET", GOOGLE_HOST) and path.startswith("/v1beta/models/"):
        return f"gemini describe {path.removeprefix('/v1beta/models/')}"
    if (method, host, path) == ("POST", GOOGLE_HOST, "/v1beta/openai/chat/completions"):
        return f"gemini ask {asked(request)}"
    raise AssertionError(f"no provider answers {method} {request.url}")


def completion(model: str, question: int, answer: int) -> dict[str, Any]:
    """An answer to a question, in OpenAI's shape, as both providers give it."""
    return {
        "id": "chatcmpl-made-up",
        "object": "chat.completion",
        "created": 1795600000,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "OK"},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": question,
            "completion_tokens": answer,
            "total_tokens": question + answer,
        },
    }


def groq_lists(_request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(200, json={"object": "list", "data": GROQ_MODELS}, headers=GROQ_LIMITS)


def groq_answers(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(200, json=completion(asked(request), 74, 9), headers=GROQ_LIMITS)


def gemini_lists(request: httpx2.Request) -> httpx2.Response:
    models, token = GEMINI_PAGES[request.url.params.get("pageToken")]
    page: dict[str, Any] = {"models": models}
    if token:
        page["nextPageToken"] = token
    return httpx2.Response(200, json=page)


def gemini_describes(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(200, json=gemini_model(call_of(request).split()[-1]))


def gemini_answers(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(200, json=completion(asked(request), 7, 1))


DEFAULTS: dict[str, Answer] = {
    "groq list": groq_lists,
    "groq ask": groq_answers,
    "gemini list": gemini_lists,
    "gemini describe": gemini_describes,
    "gemini ask": gemini_answers,
}


class Providers:
    """Both providers, faked: a transport's handler, which keeps every request it is
    sent. A test changes what a call answers through `answers`, by the call's name with
    its model, such as `groq ask openai/gpt-oss-20b`, or without, for every model."""

    def __init__(self) -> None:
        self.sent: list[httpx2.Request] = []
        self.answers: dict[str, Answer] = {}

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.sent.append(request)
        call = call_of(request)
        kind = " ".join(call.split()[:2])
        answer = self.answers.get(call) or self.answers.get(kind) or DEFAULTS[kind]
        return answer(request)

    def calls(self) -> list[str]:
        return [call_of(request) for request in self.sent]

    def asked(self, provider: str) -> list[str]:
        """The models `provider` was asked a question of, in order."""
        return [call.split()[-1] for call in self.calls() if call.startswith(f"{provider} ask ")]


# A models file without a table at either provider, as the shipped one is.
NO_CLOUD = """\
default = "standard"

[providers.ollama]
kind = "ollama"

[models.standard]
title = "Local"
provider = "ollama"
model = "local-model"
context_window = 8192
max_output_tokens = 2048
"""

GROQ_URL = "https://api.groq.com/openai/v1"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai"


def provider_table(name: str, url: str, key: str | None) -> str:
    """A `[providers.<name>]` table of a server that speaks OpenAI's API, with the
    variable of its key, or none."""
    table = f'[providers.{name}]\nkind = "openai-compatible"\nurl = "{url}"\n'
    return table + (f'api_key_env = "{key}"\n' if key else "")


GROQ_TABLE = provider_table("groq", GROQ_URL, "GROQ_API_KEY")
GEMINI_TABLE = provider_table("gemini", GEMINI_URL, "GEMINI_API_KEY")


def model_table(name: str, table: str, model: str, **figures: int | str) -> str:
    """A `[models.<name>]` table of `model` on the server `table`, with a window and a
    longest answer that both fakes' models have room for, unless `figures` say otherwise,
    and the limits `figures` state. `model` is written into a TOML string as it is, and
    a figure as JSON writes it."""
    stated: dict[str, int | str] = {"context_window": 131072, "max_output_tokens": 2048}
    stated.update(figures)
    lines = [f"[models.{name}]", f'title = "{name}"', f'provider = "{table}"', f'model = "{model}"']
    lines += [f"{key} = {json.dumps(figure)}" for key, figure in stated.items()]
    return "\n".join(lines) + "\n"


def models_file(*tables: str) -> str:
    """A models file of NO_CLOUD's local model, its default, and `tables`."""
    return "\n".join([NO_CLOUD, *tables])


def one_model(table: str, model: str, **figures: int | str) -> str:
    """A models file with both providers' tables, and one model, `models.big`, of
    `model` on `table`."""
    return models_file(GROQ_TABLE, GEMINI_TABLE, model_table("big", table, model, **figures))


class Folder:
    """Where `run` writes a test's models file: the test's own tmp_path, which the
    fixture `folder` sets before each test."""

    path: Path | None = None


@pytest.fixture(autouse=True)
def folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Folder, "path", tmp_path)


def run(
    *arguments: str,
    environ: Mapping[str, str] = BOTH,
    providers: Answer | None = None,
    models: str | None = NO_CLOUD,
) -> tuple[int, str]:
    """The exit status of a run with these arguments and keys, and what it printed. The
    run reads `models`, written to the test's folder and named on its command line, or,
    when it is None, the file that its arguments or `environ` name, or the default."""
    if models is not None:
        assert Folder.path is not None, "the fixture `folder` sets it"
        path = Folder.path / "models.toml"
        path.write_text(models)
        arguments = ("--models-file", str(path), *arguments)
    out = io.StringIO()
    transport = httpx2.MockTransport(providers or Providers())
    status = preflight.run(arguments, environ, out, transport)
    return status, out.getvalue()


def under(printed: str, model: str) -> str:
    """What a run said of `model`: the lines under its heading, which names it, and its
    names in the models file after it, when it has any."""
    lines = printed.splitlines()
    [start] = [
        at + 1
        for at, line in enumerate(lines)
        if line == f"  {model}" or line.startswith(f"  {model} (")
    ]
    end = start
    while end < len(lines) and lines[end].startswith("    "):
        end += 1
    return "\n".join(lines[start:end])


def section(printed: str, heading: str) -> str:
    """What a run said under a check's heading, up to the empty line after it."""
    lines = printed.splitlines()
    start = end = lines.index(heading) + 1
    while end < len(lines) and lines[end]:
        end += 1
    return "\n".join(lines[start:end])


def summary(printed: str, label: str) -> str:
    """What the last line says of the check named `label`."""
    parts = printed.splitlines()[-1].removesuffix(".").split("; ")
    [said] = [
        part for part in parts if part == f"{label} failed" or part.startswith(f"{label} passed")
    ]
    return said


def line_of(printed: str, *words: str) -> str:
    """The one line of what was printed that holds all of `words`."""
    lines = [line for line in printed.splitlines() if all(word in line for word in words)]
    assert len(lines) == 1, f"{len(lines)} lines hold {words}:\n{printed}"
    return lines[0]


def failures(printed: str) -> list[str]:
    return [line.strip() for line in printed.splitlines() if "FAILED" in line]


def asks_nothing(request: httpx2.Request) -> httpx2.Response:
    raise AssertionError(f"asked {request.url} without a key")


@pytest.mark.parametrize("environ", [{}, {"GROQ_API_KEY": "", "GEMINI_API_KEY": "  "}])
def test_without_a_key_nothing_is_asked_and_the_run_fails(environ: dict[str, str]) -> None:
    status, printed = run(environ=environ, providers=asks_nothing)

    assert status == 1
    assert "https://console.groq.com/keys" in line_of(printed, "GROQ_API_KEY")
    assert "https://aistudio.google.com/apikey" in line_of(printed, "GEMINI_API_KEY")
    assert "nothing was checked" in printed


@pytest.mark.parametrize(
    ("environ", "checked", "skipped", "keys_at"),
    [
        ({"GROQ_API_KEY": GROQ_KEY}, GROQ_HOST, "GEMINI_API_KEY", "aistudio.google.com/apikey"),
        ({"GEMINI_API_KEY": GEMINI_KEY}, GOOGLE_HOST, "GROQ_API_KEY", "console.groq.com/keys"),
    ],
)
def test_a_provider_without_a_key_is_skipped_and_the_other_is_checked(
    environ: dict[str, str], checked: str, skipped: str, keys_at: str
) -> None:
    providers = Providers()

    status, printed = run(environ=environ, providers=providers)

    assert status == 0
    assert {request.url.host for request in providers.sent} == {checked}
    assert keys_at in line_of(printed, skipped)
    assert not failures(printed)


def test_a_run_says_first_that_each_question_counts_against_the_limits() -> None:
    status, printed = run()

    assert status == 0
    first = printed.splitlines()[0]
    assert "one short question" in first
    assert "limits" in first


def test_each_model_is_asked_the_one_short_question_with_nothing_else_in_it() -> None:
    providers = Providers()

    run(providers=providers)

    questions = [
        json.loads(request.content)
        for request in providers.sent
        if call_of(request).split()[1] == "ask"
    ]
    assert questions == [
        {"model": model, "messages": [{"role": "user", "content": "Reply with the word OK."}]}
        for model in [*preflight.GROQ.models, *preflight.GEMINI.models]
    ]


def test_what_groq_says_of_a_model_and_how_it_answered() -> None:
    status, printed = run("groq", environ={"GROQ_API_KEY": GROQ_KEY})

    assert status == 0
    said = under(printed, "openai/gpt-oss-120b")
    assert "window of 131,072 tokens" in said
    assert "answers up to 65,536 tokens" in said
    assert "answered in " in said
    assert "74 tokens in the question" in said
    assert "9 in the answer" in said
    # Its entry in the list doesn't say how long an answer may be, so nothing is said.
    assert "window of 131,072 tokens" in under(printed, "openai/gpt-oss-20b")
    assert "answers up to" not in under(printed, "openai/gpt-oss-20b")
    assert "Groq passed" in printed.splitlines()[-1]


@pytest.mark.parametrize(
    ("thinking", "said"), [(True, "can reason"), (False, "can't reason"), (None, None)]
)
def test_what_the_gemini_api_says_of_a_model(thinking: bool | None, said: str | None) -> None:
    providers = Providers()
    providers.answers["gemini describe gemini-3.6-flash"] = lambda _request: httpx2.Response(
        200, json=gemini_model("gemini-3.6-flash", thinking=thinking)
    )

    status, printed = run("gemini", environ={"GEMINI_API_KEY": GEMINI_KEY}, providers=providers)

    assert status == 0
    described = under(printed, "gemini-3.6-flash")
    assert "window of 1,048,576 tokens" in described
    assert "answers up to 65,536 tokens" in described
    assert "7 tokens in the question" in described
    assert "1 in the answer" in described
    if said:
        assert said in described
    else:
        assert "reason" not in described
    assert "gemini describe gemini-3.6-flash" in providers.calls()


def test_the_gemini_apis_list_of_models_is_read_to_its_last_page() -> None:
    providers = Providers()

    status, printed = run("gemini", environ={"GEMINI_API_KEY": GEMINI_KEY}, providers=providers)

    assert status == 0
    # The second default model is on the list's second page.
    assert "answered in " in under(printed, "gemini-3.5-flash-lite")
    pages = [request.url.params for request in providers.sent if call_of(request) == "gemini list"]
    assert [page.get("pageToken") for page in pages] == [None, "page-2"]

    # And a model that isn't offered names those of both pages.
    status, printed = run("gemini=gemini-9-made-up", environ={"GEMINI_API_KEY": GEMINI_KEY})

    assert status == 1
    [failed] = failures(printed)
    assert all(offered in failed for offered in ["gemini-3.6-flash", "gemini-3.5-flash-lite"])


def test_a_gemini_model_that_cant_be_asked_a_question_is_not_offered() -> None:
    providers = Providers()
    page = [
        gemini_model("gemini-3.8-flash"),
        gemini_model("text-embedding-005", methods=EMBEDS),
        # Without the member, or with something else than a list in it: kept.
        gemini_model("gemini-without-methods", methods=None),
        gemini_model("gemini-odd-methods", methods="generateContent"),
    ]
    providers.answers["gemini list"] = lambda _request: httpx2.Response(200, json={"models": page})

    status, printed = run(
        "gemini=text-embedding-005", environ={"GEMINI_API_KEY": GEMINI_KEY}, providers=providers
    )

    assert status == 1
    [failed] = failures(printed)
    offers = failed.partition("It offers:")[2]
    assert "text-embedding-005" not in offers
    assert all(
        model in offers
        for model in ["gemini-3.8-flash", "gemini-without-methods", "gemini-odd-methods"]
    )
    assert providers.asked("gemini") == []


def test_the_key_goes_in_the_header_each_call_takes_and_nowhere_else() -> None:
    providers = Providers()

    status, printed = run(providers=providers)

    assert status == 0
    for request in providers.sent:
        call = call_of(request)
        key, other = (GROQ_KEY, GEMINI_KEY) if call.startswith("groq") else (GEMINI_KEY, GROQ_KEY)
        # The Gemini API's own surface takes the key in a header of its own, and its
        # OpenAI-compatible one as Groq does.
        native = call.startswith(("gemini list", "gemini describe"))
        own = "x-goog-api-key" if native else "authorization"
        assert request.headers.get(own) == (key if native else f"Bearer {key}"), call
        assert [name for name, value in request.headers.items() if key in value] == [own], call
        assert not [name for name, value in request.headers.items() if other in value], call
        for sent in (key, other):
            assert sent not in str(request.url), call
            assert sent.encode() not in request.content, call
    assert {call_of(request).split()[1] for request in providers.sent} == {
        "list",
        "describe",
        "ask",
    }
    assert GROQ_KEY not in printed
    assert GEMINI_KEY not in printed


def test_a_key_the_provider_quotes_is_not_printed() -> None:
    providers = Providers()
    providers.answers["groq list"] = lambda _request: httpx2.Response(
        401, json={"error": {"message": f"Invalid API Key: {GROQ_KEY}", "code": "invalid_api_key"}}
    )
    providers.answers["gemini ask"] = lambda _request: httpx2.Response(
        400,
        json=[
            {"error": {"code": 400, "message": f"Please pass a valid API key, not {GEMINI_KEY}"}}
        ],
    )

    status, printed = run(providers=providers)

    assert status == 1
    assert len(failures(printed)) == 3
    assert "Invalid API Key" in printed
    assert "Please pass a valid API key" in printed
    assert GROQ_KEY not in printed
    assert GEMINI_KEY not in printed


def test_a_piece_of_a_key_in_a_providers_text_is_hidden_too() -> None:
    # As some providers quote a key: its first and last few characters.
    text = f"Incorrect API key provided: {GROQ_KEY[:7]}*****{GROQ_KEY[-6:]}."

    shown = preflight.printable(text, GROQ_KEY)

    assert shown.startswith("Incorrect API key provided: ")
    for start in range(len(GROQ_KEY) - preflight.KEY_PIECE + 1):
        assert GROQ_KEY[start : start + preflight.KEY_PIECE] not in shown


def test_a_providers_text_is_printed_without_escape_sequences_on_one_line() -> None:
    providers = Providers()
    message = "Invalid \x1b[31mAPI\x1b[0m Key\x1b]0;a title\x07\r\nand\tmore\N{RIGHT-TO-LEFT OVERRIDE}\x9b"
    providers.answers["groq list"] = lambda _request: httpx2.Response(
        401, json={"error": {"message": message}}
    )

    status, printed = run("groq", environ={"GROQ_API_KEY": GROQ_KEY}, providers=providers)

    assert status == 1
    assert "Invalid API Key and more" in line_of(printed, "GROQ_API_KEY")
    assert all(char == "\n" or unicodedata.category(char) not in {"Cc", "Cf"} for char in printed)


def test_a_providers_text_is_cut() -> None:
    shown = preflight.printable("word " * 1000, GROQ_KEY)

    assert len(shown) == preflight.SHOWN_AT_MOST
    assert shown.endswith("…")


def test_groqs_headers_state_requests_a_day_and_tokens_a_minute() -> None:
    status, printed = run("groq", environ={"GROQ_API_KEY": GROQ_KEY})

    assert status == 0
    said = under(printed, "openai/gpt-oss-120b")
    requests = line_of(said, "requests a day")
    assert all(fact in requests for fact in ["14,400", "14,370 left", "2 min 59.56 s"])
    tokens = line_of(said, "tokens a minute")
    assert all(fact in tokens for fact in ["18,000", "17,997 left", "7.66 s"])
    # What its API doesn't state, said once for Groq, with where a person reads it.
    unstated = line_of(printed, "requests a minute")
    assert "tokens a day" in unstated
    assert "https://console.groq.com/settings/limits" in unstated


@pytest.mark.parametrize(
    ("duration", "seconds"),
    [
        ("7.66s", 7.66),
        ("2m59.56s", 179.56),
        ("1h0m0s", 3600),
        ("120ms", 0.12),
        ("0", 0),
        ("1.5h", 5400),
        ("250us", 0.00025),
        ("250µs", 0.00025),
        ("90ns", 0.00000009),
        (".5s", 0.5),
    ],
)
def test_a_duration_in_gos_notation_is_read(duration: str, seconds: float) -> None:
    assert preflight.seconds_in(duration) == pytest.approx(seconds)


@pytest.mark.parametrize("duration", ["", "7.66", "soon", "-1s", "1d", "1h 2m", "s", "0s0"])
def test_what_isnt_a_duration_is_not_read(duration: str) -> None:
    assert preflight.seconds_in(duration) is None


@pytest.mark.parametrize(
    ("seconds", "said"),
    [
        (7.66, "7.66 s"),
        (179.56, "2 min 59.56 s"),
        (3600, "1 h"),
        (3725.5, "1 h 2 min 5.5 s"),
        (0.12, "0.12 s"),
        (0, "0 s"),
        (59.999, "1 min"),
    ],
)
def test_a_time_is_said_to_the_hundredth_of_a_second(seconds: float, said: str) -> None:
    assert preflight.spoken(seconds) == said


def test_a_limit_that_cant_be_read_is_shown_as_it_came_and_fails_nothing() -> None:
    providers = Providers()
    unreadable = GROQ_LIMITS | {
        "x-ratelimit-limit-requests": "lots",
        "x-ratelimit-reset-tokens": "soon\x1b[2J",
    }
    providers.answers["groq ask"] = lambda request: httpx2.Response(
        200, json=completion(asked(request), 74, 9), headers=unreadable
    )

    status, printed = run("groq", environ={"GROQ_API_KEY": GROQ_KEY}, providers=providers)

    assert status == 0
    said = under(printed, "openai/gpt-oss-120b")
    requests = line_of(said, "requests a day")
    assert '"lots" (not understood)' in requests
    assert "14,370 left" in requests
    tokens = line_of(said, "tokens a minute")
    assert '"soon" (not understood)' in tokens
    assert "17,997 left" in tokens
    assert "\x1b" not in printed


@pytest.mark.parametrize(
    ("named", "environ", "model", "limits_at"),
    [
        (
            "groq",
            {"GROQ_API_KEY": GROQ_KEY},
            "openai/gpt-oss-120b",
            "https://console.groq.com/settings/limits",
        ),
        # No answer of the Gemini API carried a header about limits on 2026-11-25: its
        # answers here have none.
        (
            "gemini",
            {"GEMINI_API_KEY": GEMINI_KEY},
            "gemini-3.6-flash",
            "https://aistudio.google.com/rate-limit",
        ),
    ],
)
def test_an_answer_that_states_no_limits_says_where_to_read_them(
    named: str, environ: dict[str, str], model: str, limits_at: str
) -> None:
    providers = Providers()
    providers.answers["groq ask"] = lambda request: httpx2.Response(
        200, json=completion(asked(request), 74, 9)
    )

    status, printed = run(named, environ=environ, providers=providers)

    assert status == 0
    said = under(printed, model)
    assert limits_at in line_of(said, "no limits")
    assert "requests" not in said
    assert "tokens a minute" not in said


def test_a_model_not_offered_fails_and_the_others_are_still_checked() -> None:
    providers = Providers()

    status, printed = run("groq=llama-9-made-up,openai/gpt-oss-20b", "gemini", providers=providers)

    assert status == 1
    [failed] = failures(printed)
    assert "llama-9-made-up" in failed
    assert all(model["id"] in failed for model in GROQ_MODELS)
    assert providers.asked("groq") == ["openai/gpt-oss-20b"]
    assert providers.asked("gemini") == list(preflight.GEMINI.models)
    last = printed.splitlines()[-1]
    assert "Groq failed" in last
    assert "Gemini passed" in last


@pytest.mark.parametrize(
    ("named", "environ", "variable", "refusal", "message"),
    [
        (
            "groq",
            {"GROQ_API_KEY": GROQ_KEY},
            "GROQ_API_KEY",
            httpx2.Response(401, json=GROQ_REFUSES_THE_KEY),
            "Invalid API Key",
        ),
        (
            "gemini",
            {"GEMINI_API_KEY": GEMINI_KEY},
            "GEMINI_API_KEY",
            httpx2.Response(400, json=GEMINI_REFUSES_THE_KEY),
            "API key not valid",
        ),
        (
            "gemini",
            {"GEMINI_API_KEY": GEMINI_KEY},
            "GEMINI_API_KEY",
            httpx2.Response(
                403,
                json={
                    "error": {
                        "code": 403,
                        "message": "Method doesn't allow unregistered callers.",
                        "status": "PERMISSION_DENIED",
                    }
                },
            ),
            "unregistered callers",
        ),
    ],
)
def test_a_refused_key_is_one_line_and_no_model_is_asked(
    named: str, environ: dict[str, str], variable: str, refusal: httpx2.Response, message: str
) -> None:
    providers = Providers()
    providers.answers[f"{named} list"] = lambda _request: refusal

    status, printed = run(named, environ=environ, providers=providers)

    assert status == 1
    [failed] = failures(printed)
    assert variable in failed
    assert message in failed
    assert providers.calls() == [f"{named} list"]


def times_out(request: httpx2.Request) -> httpx2.Response:
    raise httpx2.ReadTimeout("timed out", request=request)


def connects_too_slowly(request: httpx2.Request) -> httpx2.Response:
    raise httpx2.ConnectTimeout("timed out", request=request)


def cant_connect(request: httpx2.Request) -> httpx2.Response:
    raise httpx2.ConnectError("[Errno 8] nodename nor servname provided", request=request)


@pytest.mark.parametrize(
    ("call", "answer", "facts"),
    [
        (
            "groq ask openai/gpt-oss-120b",
            lambda _request: httpx2.Response(
                429,
                json={
                    "error": {
                        "message": "Rate limit reached for model `openai/gpt-oss-120b` on "
                        "tokens per minute (TPM): Limit 8000, Used 7990, Requested 74.",
                        "type": "tokens",
                        "code": "rate_limit_exceeded",
                    }
                },
                headers={"retry-after": "7"},
            ),
            ["429", "Rate limit reached for model", "7 s"],
        ),
        (
            "gemini ask gemini-3.6-flash",
            lambda _request: httpx2.Response(400, json=GEMINI_REFUSES_THE_QUESTION),
            ["400", "Please pass a valid API key"],
        ),
        (
            "groq ask openai/gpt-oss-120b",
            lambda _request: httpx2.Response(
                200, text="<html>Bad gateway</html>", headers={"content-type": "text/html"}
            ),
            ["isn't JSON"],
        ),
        (
            "gemini ask gemini-3.6-flash",
            lambda _request: httpx2.Response(200, json={"object": "list", "data": []}),
            ["isn't shaped as expected"],
        ),
        (
            # A choice with no message in it is no answer.
            "groq ask openai/gpt-oss-120b",
            lambda _request: httpx2.Response(200, json={"choices": [{"index": 0}]}),
            ["isn't shaped as expected"],
        ),
        (
            # Only 503 is a provider too busy: any other error of its server's fails.
            "groq ask openai/gpt-oss-120b",
            lambda _request: httpx2.Response(502, json={"error": {"message": "Bad gateway"}}),
            ["502", "Bad gateway"],
        ),
        (
            "gemini ask gemini-3.6-flash",
            lambda _request: httpx2.Response(
                500, json=[{"error": {"code": 500, "message": "Internal error"}}]
            ),
            ["500", "Internal error"],
        ),
        ("groq ask openai/gpt-oss-120b", cant_connect, ["can't reach Groq", "ConnectError"]),
        ("gemini ask gemini-3.6-flash", times_out, ["didn't answer within 30 s"]),
        ("groq ask openai/gpt-oss-120b", connects_too_slowly, ["couldn't connect", "within 5 s"]),
    ],
)
def test_a_question_that_fails_is_one_line_and_the_next_model_is_asked(
    call: str, answer: Answer, facts: list[str]
) -> None:
    providers = Providers()
    providers.answers[call] = answer
    provider, _, model = call.split()
    [other] = [
        each
        for each in (preflight.GROQ if provider == "groq" else preflight.GEMINI).models
        if each != model
    ]

    status, printed = run(providers=providers)

    assert status == 1
    [failed] = failures(printed)
    assert all(fact in failed for fact in facts), failed
    assert failed in under(printed, model)
    assert "answered in " in under(printed, other)
    assert "Traceback" not in printed


def test_models_named_for_a_provider_replace_its_own_and_no_others() -> None:
    providers = Providers()

    status, _ = run("groq=llama-3.1-8b-instant,openai/gpt-oss-20b", "gemini", providers=providers)

    assert status == 0
    assert providers.asked("groq") == ["llama-3.1-8b-instant", "openai/gpt-oss-20b"]
    assert providers.asked("gemini") == list(preflight.GEMINI.models)


def test_a_provider_named_alone_is_the_only_one_checked() -> None:
    providers = Providers()

    status, printed = run("groq=openai/gpt-oss-20b", providers=providers)

    assert status == 0
    assert {request.url.host for request in providers.sent} == {GROQ_HOST}
    assert "GEMINI_API_KEY" not in printed


@pytest.mark.parametrize("arguments", [["openai"], ["groq="], ["gemini=,"], ["Groq"]])
def test_a_provider_it_doesnt_know_is_a_usage_error(
    arguments: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exited:
        run(*arguments, providers=asks_nothing)

    assert exited.value.code == 2
    assert "usage:" in capsys.readouterr().err


@pytest.mark.parametrize(
    "key",
    [
        # As pasted from a web page, with its quotes.
        f"\N{LEFT DOUBLE QUOTATION MARK}{GROQ_KEY}\N{RIGHT DOUBLE QUOTATION MARK}",
        # With straight quotes that were meant for the shell and reached the key.
        f'"{GROQ_KEY}"',
        f"'{GROQ_KEY}'",
        "key-for\N{ZERO WIDTH SPACE}-tests",
        "key-for tests",
        "key-for\ntests",
    ],
)
def test_a_key_that_cant_be_one_is_one_line_and_nothing_is_asked_with_it(key: str) -> None:
    providers = Providers()

    status, printed = run(
        environ={"GROQ_API_KEY": key, "GEMINI_API_KEY": GEMINI_KEY}, providers=providers
    )

    assert status == 1
    [failed] = failures(printed)
    assert "GROQ_API_KEY" in failed
    assert GROQ_HOST not in {request.url.host for request in providers.sent}
    assert providers.asked("gemini") == list(preflight.GEMINI.models)
    # But for the line that names the models file: its path is this machine's, and a
    # folder's name may share a piece with the key.
    said = "\n".join(line for line in printed.splitlines() if str(Folder.path) not in line)
    assert "Gemini" in said
    for start in range(len(key) - preflight.KEY_PIECE + 1):
        assert key[start : start + preflight.KEY_PIECE] not in said


NINES = "9" * 400


@pytest.mark.parametrize(
    ("status", "headers", "said"),
    [
        (429, {"retry-after": NINES}, "FAILED"),
        (200, GROQ_LIMITS | {"x-ratelimit-reset-tokens": f"{NINES}s"}, "tokens a minute"),
        # More digits than Python reads as a whole number.
        (200, GROQ_LIMITS | {"x-ratelimit-limit-requests": "9" * 5000}, "requests a day"),
    ],
)
def test_a_number_too_long_to_read_is_shown_as_it_came_and_the_next_model_asked(
    status: int, headers: dict[str, str], said: str
) -> None:
    providers = Providers()
    body = (
        (lambda model: completion(model, 74, 9))
        if status == 200
        else (lambda _model: {"error": {"message": "Rate limit reached"}})
    )
    providers.answers["groq ask openai/gpt-oss-120b"] = lambda request: httpx2.Response(
        status, json=body(asked(request)), headers=headers
    )

    ran, printed = run("groq", environ={"GROQ_API_KEY": GROQ_KEY}, providers=providers)

    assert ran == (0 if status == 200 else 1)
    shown = line_of(under(printed, "openai/gpt-oss-120b"), said)
    assert '"999' in shown
    assert "(not understood)" in shown
    assert "answered in " in under(printed, "openai/gpt-oss-20b")


@pytest.mark.parametrize("count", [-3, True])
def test_a_count_that_isnt_one_is_not_said(count: int) -> None:
    # A number below zero, and JSON's `true`, which Python takes for the number 1.
    providers = Providers()
    providers.answers["groq ask"] = lambda request: httpx2.Response(
        200, json=completion(asked(request), 0, 9) | {"usage": {"prompt_tokens": count}}
    )

    status, printed = run("groq", environ={"GROQ_API_KEY": GROQ_KEY}, providers=providers)

    assert status == 0
    said = line_of(under(printed, "openai/gpt-oss-120b"), "answered in ")
    assert "in the question" not in said
    assert "tokens" not in said


@pytest.mark.parametrize(("status", "fact"), [(200, "nested too deep"), (400, "HTTP 400")])
def test_a_body_nested_too_deep_to_read_is_one_line(status: int, fact: str) -> None:
    providers = Providers()
    providers.answers["groq ask openai/gpt-oss-120b"] = lambda _request: httpx2.Response(
        status, content="[" * 100_000, headers={"content-type": "application/json"}
    )

    ran, printed = run("groq", environ={"GROQ_API_KEY": GROQ_KEY}, providers=providers)

    assert ran == 1
    [failed] = failures(printed)
    assert fact in failed
    assert "answered in " in under(printed, "openai/gpt-oss-20b")


def test_a_page_token_too_long_for_an_address_is_one_line() -> None:
    providers = Providers()
    providers.answers["gemini list"] = lambda _request: httpx2.Response(
        200, json={"models": [gemini_model("gemini-3.8-flash")], "nextPageToken": "t" * 70_000}
    )

    status, printed = run(providers=providers)

    assert status == 1
    [failed] = failures(printed)
    assert "InvalidURL" in failed
    assert "Groq passed" in printed.splitlines()[-1]


def test_a_list_that_names_the_same_next_page_for_ever_ends_as_a_failure() -> None:
    # Without the limit on pages, the list would be read for ever: this test's own limit
    # of five seconds would end it then, as a failure.
    deadline = time.monotonic() + 5

    def for_ever(_request: httpx2.Request) -> httpx2.Response:
        assert time.monotonic() < deadline, "the list was still being read after 5 s"
        page = {"models": [gemini_model("gemini-3.8-flash")], "nextPageToken": "again"}
        return httpx2.Response(200, json=page)

    providers = Providers()
    providers.answers["gemini list"] = for_ever

    status, printed = run("gemini", environ={"GEMINI_API_KEY": GEMINI_KEY}, providers=providers)

    assert status == 1
    [failed] = failures(printed)
    assert f"{preflight.MOST_PAGES} pages" in failed
    assert providers.calls().count("gemini list") == preflight.MOST_PAGES


@pytest.mark.parametrize(
    ("named", "call"),
    [
        ("gemini", "gemini list"),
        ("gemini=gemini-3.8-flash", "gemini ask gemini-3.8-flash"),
        ("groq=openai/gpt-oss-20b", "groq ask openai/gpt-oss-20b"),
    ],
)
def test_a_redirect_to_another_host_is_not_followed(named: str, call: str) -> None:
    providers = Providers()
    providers.answers[call] = lambda _request: httpx2.Response(
        302, headers={"location": "https://elsewhere.example/collects-keys"}
    )

    status, printed = run(named, providers=providers)

    assert status == 1
    [failed] = failures(printed)
    assert "302" in failed
    assert providers.calls().count(call) == 1
    host = GROQ_HOST if named.startswith("groq") else GOOGLE_HOST
    assert {request.url.host for request in providers.sent} == {host}


def test_what_a_provider_is_asked_through_doesnt_show_its_key() -> None:
    with httpx2.Client(transport=httpx2.MockTransport(asks_nothing)) as client:
        asking = preflight.Asking(preflight.GROQ, GROQ_KEY, client, "GROQ_API_KEY")

        assert GROQ_KEY not in repr(asking)


def test_the_words_after_a_status_are_not_the_servers_own() -> None:
    # A server writes what it likes after the number of its status. What is printed is
    # the HTTP library's own words for that number.
    providers = Providers()
    providers.answers["groq list"] = lambda _request: httpx2.Response(
        500, extensions={"reason_phrase": f"\x1b[31mlook {GROQ_KEY}".encode()}
    )

    status, printed = run("groq", environ={"GROQ_API_KEY": GROQ_KEY}, providers=providers)

    assert status == 1
    [failed] = failures(printed)
    assert "500 Internal Server Error" in failed
    assert "look" not in printed
    assert "\x1b" not in printed
    assert GROQ_KEY not in printed


def test_the_models_a_provider_offers_are_printed_as_its_other_text_is() -> None:
    # The ids of a provider's list are its own text too: without what a terminal would
    # act on, and with the key hidden.
    providers = Providers()
    listed = [*GROQ_MODELS, {"id": f"\x1b[2Jodd-{GROQ_KEY}", "context_window": 8192}]
    providers.answers["groq list"] = lambda _request: httpx2.Response(
        200, json={"object": "list", "data": listed}
    )

    status, printed = run(
        "groq=llama-9-made-up", environ={"GROQ_API_KEY": GROQ_KEY}, providers=providers
    )

    assert status == 1
    [failed] = failures(printed)
    assert f"odd-{preflight.HIDDEN}" in failed.partition("It offers:")[2]
    assert "\x1b" not in printed
    assert GROQ_KEY not in printed


def test_a_models_id_takes_its_own_place_in_an_address_and_no_more() -> None:
    # An id comes from the command line and from the provider's list. Its signs must
    # not become more of the path, a query or a fragment.
    odd = "tuned/one?alt=x#y"
    providers = Providers()
    providers.answers["gemini list"] = lambda _request: httpx2.Response(
        200, json={"models": [gemini_model(odd)]}
    )

    status, _ = run(f"gemini={odd}", environ={"GEMINI_API_KEY": GEMINI_KEY}, providers=providers)

    assert status == 0
    [described] = [
        request for request in providers.sent if call_of(request).startswith("gemini describe")
    ]
    assert described.url.raw_path == b"/v1beta/models/tuned%2Fone%3Falt%3Dx%23y"
    assert providers.asked("gemini") == [odd]


def test_every_request_has_five_seconds_to_connect_and_thirty_to_be_answered() -> None:
    providers = Providers()

    status, _ = run(providers=providers)

    assert status == 0
    assert {call_of(request).split()[1] for request in providers.sent} == {
        "list",
        "describe",
        "ask",
    }
    for request in providers.sent:
        limits = request.extensions["timeout"]
        assert limits["connect"] == 5, call_of(request)
        assert limits["read"] == 30, call_of(request)


# --- The models file, held against what the providers state ---------------------------

# The figures of the models file that a fake provider states, each with the provider's
# own: Groq's list and its answers' headers, and the Gemini API's description.
STATED = [
    pytest.param("groq", "openai/gpt-oss-120b", "context_window", 131_072, id="groq-window"),
    pytest.param("groq", "openai/gpt-oss-120b", "max_output_tokens", 65_536, id="groq-longest"),
    pytest.param("groq", "openai/gpt-oss-120b", "requests_a_day", 14_400, id="groq-a-day"),
    pytest.param("groq", "openai/gpt-oss-120b", "tokens_a_minute", 18_000, id="groq-a-minute"),
    pytest.param("gemini", "gemini-3.6-flash", "context_window", 1_048_576, id="gemini-window"),
    pytest.param("gemini", "gemini-3.6-flash", "max_output_tokens", 65_536, id="gemini-longest"),
]


@pytest.mark.parametrize(("table", "model", "key", "theirs"), STATED)
def test_a_figure_larger_than_the_providers_fails_the_run(
    table: str, model: str, key: str, theirs: int
) -> None:
    status, printed = run(table, models=one_model(table, model, **{key: theirs + 1}))

    assert status == 1
    [failed] = failures(printed)
    for fact in ["models.big", key, f"{theirs + 1:,}", f"{theirs:,}"]:
        assert fact in failed, failed
    said = under(printed, model)
    assert failed in said
    # The model's other lines are printed all the same.
    assert "answered in " in said
    assert "failed" in printed.splitlines()[-1]


def test_every_figure_larger_than_the_providers_is_said() -> None:
    figures = {"context_window": 131_073, "requests_a_day": 14_401}

    status, printed = run("groq", models=one_model("groq", "openai/gpt-oss-120b", **figures))

    assert status == 1
    window, requests = failures(printed)
    assert "context_window" in window
    assert "requests_a_day" in requests


@pytest.mark.parametrize(("table", "model", "key", "theirs"), STATED)
def test_a_figure_as_large_as_the_providers_passes(
    table: str, model: str, key: str, theirs: int
) -> None:
    status, printed = run(table, models=one_model(table, model, **{key: theirs}))

    assert status == 0
    assert not failures(printed)
    # It was held against the provider's: a line says so.
    assert f"{theirs:,}" in line_of(under(printed, model), key)


@pytest.mark.parametrize(("table", "model", "key", "theirs"), STATED)
def test_a_figure_smaller_than_the_providers_passes_with_the_providers_beside_it(
    table: str, model: str, key: str, theirs: int
) -> None:
    status, printed = run(table, models=one_model(table, model, **{key: theirs - 1}))

    assert status == 0
    assert not failures(printed)
    held = line_of(under(printed, model), key)
    assert f"{theirs - 1:,}" in held
    assert f"{theirs:,}" in held


HUGE = 1_000_000_000


def test_what_groqs_api_doesnt_state_is_not_checked_however_large() -> None:
    figures = {"requests_a_minute": HUGE, "tokens_a_day": HUGE}

    status, printed = run("groq", models=one_model("groq", "openai/gpt-oss-120b", **figures))

    assert status == 0
    assert not failures(printed)
    unchecked = line_of(under(printed, "openai/gpt-oss-120b"), "requests_a_minute")
    # Why: its API never states them, which is another reason than an answer without one.
    for fact in ["not checked", "tokens_a_day", f"{HUGE:,}", "API", preflight.GROQ.limits_at]:
        assert fact in unchecked, unchecked


def test_no_limit_of_a_gemini_model_is_checked_however_large() -> None:
    figures = dict.fromkeys(LIMITS, HUGE)

    status, printed = run("gemini", models=one_model("gemini", "gemini-3.6-flash", **figures))

    assert status == 0
    assert not failures(printed)
    unchecked = line_of(under(printed, "gemini-3.6-flash"), "not checked")
    for fact in [*LIMITS, f"{HUGE:,}", "API", preflight.GEMINI.limits_at]:
        assert fact in unchecked, unchecked


@pytest.mark.parametrize(
    ("figures", "too_large"),
    [
        ({"requests_a_minute": 20_000, "requests_a_day": 14_000}, None),
        ({"requests_a_minute": 14_000, "requests_a_day": 20_000}, "requests_a_day"),
        ({"tokens_a_day": 20_000, "tokens_a_minute": 17_000}, None),
        ({"tokens_a_day": 17_000, "tokens_a_minute": 20_000}, "tokens_a_minute"),
    ],
)
def test_each_of_groqs_headers_is_held_against_the_key_of_what_it_counts(
    figures: dict[str, int], too_large: str | None
) -> None:
    # Groq's headers state 14,400 requests a day and 18,000 tokens a minute.
    status, printed = run("groq", models=one_model("groq", "openai/gpt-oss-120b", **figures))

    said = under(printed, "openai/gpt-oss-120b")
    if too_large is None:
        assert status == 0
        assert not failures(printed)
        # The other key of the two is not checked.
        assert "20,000" in line_of(said, "not checked")
    else:
        assert status == 1
        [failed] = failures(printed)
        assert too_large in failed
        assert "20,000" in failed


def test_a_limit_the_provider_states_and_the_file_leaves_out_is_said_and_fails_nothing() -> None:
    status, printed = run("groq", models=one_model("groq", "openai/gpt-oss-120b"))

    assert status == 0
    assert not failures(printed)
    said = under(printed, "openai/gpt-oss-120b")
    for key, theirs in [("requests_a_day", "14,400"), ("tokens_a_minute", "18,000")]:
        left_out = line_of(said, key)
        assert theirs in left_out
        assert "none" in left_out


def answered_with(headers: Mapping[str, str]) -> Answer:
    """An answer to every question, with these headers."""
    return lambda request: httpx2.Response(
        200, json=completion(asked(request), 74, 9), headers=headers
    )


@pytest.mark.parametrize(
    ("answer", "model", "figures", "key", "figure", "why", "where"),
    [
        pytest.param(
            answered_with(GROQ_LIMITS | {"x-ratelimit-limit-requests": "lots"}),
            "openai/gpt-oss-120b",
            {"requests_a_day": 1000},
            "requests_a_day",
            "1,000",
            "number",
            preflight.GROQ.limits_at,
            id="a-header-that-isnt-a-number",
        ),
        pytest.param(
            answered_with(
                {
                    name: value
                    for name, value in GROQ_LIMITS.items()
                    if name != "x-ratelimit-limit-tokens"
                }
            ),
            "openai/gpt-oss-120b",
            {"tokens_a_minute": 1000},
            "tokens_a_minute",
            "1,000",
            "answer",
            preflight.GROQ.limits_at,
            id="a-header-that-is-missing",
        ),
        # Its entry in Groq's list has no `max_completion_tokens`.
        pytest.param(
            None,
            "openai/gpt-oss-20b",
            {},
            "max_output_tokens",
            "2,048",
            "list",
            preflight.GROQ.models_at,
            id="an-entry-without-the-longest-answer",
        ),
    ],
)
def test_what_the_provider_didnt_state_or_stated_unreadably_is_not_checked_and_fails_nothing(
    answer: Answer | None,
    model: str,
    figures: dict[str, int],
    key: str,
    figure: str,
    why: str,
    where: str,
) -> None:
    providers = Providers()
    if answer is not None:
        providers.answers["groq ask"] = answer

    status, printed = run("groq", models=one_model("groq", model, **figures), providers=providers)

    assert status == 0
    assert not failures(printed)
    unchecked = line_of(under(printed, model), key)
    # Why it wasn't checked is said by what was missing: a number, an answer's header, or
    # the list's entry.
    for fact in ["not checked", figure, why, where]:
        assert fact in unchecked, unchecked
    for other in {"number", "answer", "list"} - {why}:
        assert other not in unchecked, unchecked


def test_a_size_that_isnt_a_number_is_not_checked_and_fails_nothing() -> None:
    providers = Providers()
    listed = [GROQ_MODELS[0] | {"max_completion_tokens": "65536"}]
    providers.answers["groq list"] = lambda _request: httpx2.Response(
        200, json={"object": "list", "data": listed}
    )

    status, printed = run(
        "groq", models=one_model("groq", "openai/gpt-oss-120b"), providers=providers
    )

    assert status == 0
    assert not failures(printed)
    unchecked = line_of(under(printed, "openai/gpt-oss-120b"), "max_output_tokens")
    for fact in ["not checked", "2,048", "number", preflight.GROQ.models_at]:
        assert fact in unchecked, unchecked


def test_the_limits_of_an_id_two_models_share_are_said_once_for_both() -> None:
    limits = {"requests_a_minute": 30, "requests_a_day": 1000}
    models = models_file(
        GROQ_TABLE,
        model_table("big", "groq", "openai/gpt-oss-120b", **limits),
        model_table("again", "groq", "openai/gpt-oss-120b", **limits),
    )

    status, printed = run("groq", models=models)

    assert status == 0
    said = under(printed, "openai/gpt-oss-120b")
    # One line each, for both models: the limit that was held, the one the file leaves
    # out, and the one that wasn't checked.
    for fact in ["requests_a_day 1,000", "tokens_a_minute", "requests_a_minute 30"]:
        line = line_of(said, fact)
        assert "models.big" in line, line
        assert "models.again" in line, line
    # And the one that wasn't checked counts once.
    assert re.search(r"\b1\b", summary(printed, "Groq (providers.groq)"))


def test_the_files_models_are_asked_each_id_once_and_each_figure_is_its_models_own() -> None:
    providers = Providers()
    models = models_file(
        GROQ_TABLE,
        model_table("big", "groq", "openai/gpt-oss-120b"),
        model_table("again", "groq", "openai/gpt-oss-120b", max_output_tokens=70_000),
    )

    status, printed = run("groq", models=models, providers=providers)

    assert status == 1
    # One question, and not the script's own models.
    assert providers.asked("groq") == ["openai/gpt-oss-120b"]
    assert line_of(printed, "(models.big, models.again)") == (
        "  openai/gpt-oss-120b (models.big, models.again)"
    )
    [failed] = failures(printed)
    assert "models.again" in failed
    assert "models.big" not in failed
    assert "70,000" in failed


def test_a_provider_the_file_has_no_table_of_is_asked_its_own_models_and_compares_nothing() -> None:
    providers = Providers()
    models = models_file(GROQ_TABLE, model_table("big", "groq", "openai/gpt-oss-120b"))

    status, printed = run(models=models, providers=providers)

    assert status == 0
    assert providers.asked("gemini") == list(preflight.GEMINI.models)
    gemini = section(printed, "Gemini")
    assert "Gemini" in line_of(gemini, "models file", "none")
    assert "not checked" not in gemini
    assert summary(printed, "Gemini") == "Gemini passed"
    # The file's own table was held against Groq's figures.
    assert "131,072" in line_of(under(printed, "openai/gpt-oss-120b"), "context_window")


def test_the_models_file_is_the_arguments_or_else_the_environments_or_else_the_apis_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Each file puts another of Groq's models on its table, so that its question shows
    # which file was read. The default is seen from the directory the script runs in.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    for path, model in [
        ("argument.toml", "llama-3.1-8b-instant"),
        ("environment.toml", "openai/gpt-oss-20b"),
        ("config/models.toml", "openai/gpt-oss-120b"),
    ]:
        (tmp_path / path).write_text(models_file(GROQ_TABLE, model_table("big", "groq", model)))
    argument, environment = str(tmp_path / "argument.toml"), str(tmp_path / "environment.toml")
    keys = {"GROQ_API_KEY": GROQ_KEY}

    for arguments, environ, read, model in [
        (
            ["--models-file", argument],
            keys | {"ACTIO_MODELS_FILE": environment},
            argument,
            "llama-3.1-8b-instant",
        ),
        ([], keys | {"ACTIO_MODELS_FILE": environment}, environment, "openai/gpt-oss-20b"),
        ([], keys, "config/models.toml", "openai/gpt-oss-120b"),
    ]:
        providers = Providers()

        status, printed = run("groq", *arguments, environ=environ, models=None, providers=providers)

        assert status == 0, printed
        assert providers.asked("groq") == [model]
        lines = printed.splitlines()
        # Said before anything about a provider.
        named = lines.index(line_of(printed, read))
        assert named < min(at for at, line in enumerate(lines) if "Groq" in line)


@pytest.mark.parametrize("value", ["", " "])
def test_a_variable_that_is_set_and_names_no_file_is_taken_as_the_api_takes_it(
    value: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The API takes an ACTIO_MODELS_FILE that is set and empty for the directory it runs
    # in, and doesn't start. So the default isn't read in its place, though it is there.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "models.toml").write_text(NO_CLOUD)

    status, printed = run(
        environ=BOTH | {"ACTIO_MODELS_FILE": value}, models=None, providers=asks_nothing
    )

    assert status == 1
    [line] = printed.splitlines()
    assert "FAILED" in line
    assert "models file" in line
    # The line says which variable to set, as the API's does.
    assert "ACTIO_MODELS_FILE" in line


def test_the_models_file_may_be_named_between_providers(tmp_path: Path) -> None:
    path = tmp_path / "models.toml"
    path.write_text(NO_CLOUD)
    providers = Providers()

    status, _ = run("groq", "--models-file", str(path), "gemini", models=None, providers=providers)

    assert status == 0
    assert providers.asked("groq") == list(preflight.GROQ.models)
    assert providers.asked("gemini") == list(preflight.GEMINI.models)


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(None, id="missing"),
        pytest.param(b"default = \n", id="not-toml"),
        pytest.param(
            one_model("groq", "openai/gpt-oss-120b", requests_a_day="1000").encode(),
            id="a-mistake",
        ),
        # What the TOML parser gives up on in a way of its own.
        pytest.param(b'default = "\xff"\n', id="not-utf-8"),
        pytest.param(b"a = " + b"9" * 5000 + b"\n", id="a-number-too-long"),
        pytest.param(b"a = " + b"[" * 10_000 + b"]" * 10_000 + b"\n", id="too-deep"),
    ],
)
def test_a_models_file_that_cant_be_read_is_one_line_in_the_readers_words_and_asks_nothing(
    content: bytes | None, tmp_path: Path
) -> None:
    # In a folder whose name alone is longer than a provider's text may be printed.
    path = tmp_path / ("a-folder-of-its-own-" * 10) / "models.toml"
    path.parent.mkdir()
    if content is not None:
        path.write_bytes(content)
    with pytest.raises(CatalogError) as refused:
        read_file(path)

    status, printed = run("--models-file", str(path), models=None, providers=asks_nothing)

    assert status == 1
    [line] = printed.splitlines()
    assert "FAILED" in line
    # Whole: not cut as a provider's text is.
    assert len(str(refused.value)) > preflight.SHOWN_AT_MOST
    assert str(refused.value) in line
    assert str(path) in line


def bearer_of(request: httpx2.Request) -> str:
    return request.headers.get("authorization", "")


def test_a_tables_key_is_read_from_the_variable_it_names() -> None:
    providers = Providers()
    models = models_file(
        provider_table("groq", GROQ_URL, "OTHER_KEY"),
        model_table("big", "groq", "openai/gpt-oss-120b"),
    )

    status, printed = run(
        "groq",
        environ={"OTHER_KEY": OTHER_KEY, "GROQ_API_KEY": GROQ_KEY},
        models=models,
        providers=providers,
    )

    assert status == 0
    assert providers.calls() == ["groq list", "groq ask openai/gpt-oss-120b"]
    for request in providers.sent:
        assert bearer_of(request) == f"Bearer {OTHER_KEY}"
        assert not [name for name, value in request.headers.items() if GROQ_KEY in value]
        assert GROQ_KEY not in str(request.url)
        assert GROQ_KEY.encode() not in request.content
    assert OTHER_KEY not in printed


def test_a_table_whose_variable_holds_no_key_is_skipped_by_a_line_that_names_it() -> None:
    models = models_file(
        provider_table("groq", GROQ_URL, "OTHER_KEY"),
        model_table("big", "groq", "openai/gpt-oss-120b"),
    )

    status, printed = run(
        "groq", environ={"GROQ_API_KEY": GROQ_KEY}, models=models, providers=asks_nothing
    )

    assert status == 1
    assert "https://console.groq.com/keys" in line_of(printed, "OTHER_KEY")
    assert "GROQ_API_KEY" not in printed


@pytest.mark.parametrize(
    ("key", "refusal"),
    [
        pytest.param(OTHER_KEY, httpx2.Response(401, json=GROQ_REFUSES_THE_KEY), id="refused"),
        pytest.param("another secret", None, id="cant-be-a-key"),
    ],
)
def test_what_is_said_of_a_tables_key_names_the_tables_variable(
    key: str, refusal: httpx2.Response | None
) -> None:
    providers = Providers()
    if refusal is not None:
        providers.answers["groq list"] = lambda _request: refusal
    models = models_file(
        provider_table("groq", GROQ_URL, "OTHER_KEY"),
        model_table("big", "groq", "openai/gpt-oss-120b"),
    )

    status, printed = run(
        "groq",
        environ={"OTHER_KEY": key, "GROQ_API_KEY": GROQ_KEY},
        models=models,
        providers=providers,
    )

    assert status == 1
    [failed] = failures(printed)
    assert "OTHER_KEY" in failed
    assert "GROQ_API_KEY" not in printed


def test_a_table_that_names_no_variable_for_its_key_fails_and_nothing_is_asked() -> None:
    models = models_file(
        provider_table("groq", GROQ_URL, None),
        model_table("big", "groq", "openai/gpt-oss-120b"),
    )

    status, printed = run("groq", models=models, providers=asks_nothing)

    assert status == 1
    [failed] = failures(printed)
    assert "providers.groq" in failed
    assert "api_key_env" in failed
    assert "failed" in summary(printed, "Groq (providers.groq)")


def test_two_tables_of_one_provider_are_two_checks_each_with_its_own_key() -> None:
    providers = Providers()
    models = models_file(
        GROQ_TABLE,
        provider_table("spare", GROQ_URL, "SPARE_GROQ_KEY"),
        model_table("big", "groq", "openai/gpt-oss-120b"),
        model_table("small", "spare", "openai/gpt-oss-20b"),
    )

    status, printed = run(
        "groq",
        environ={"GROQ_API_KEY": GROQ_KEY, "SPARE_GROQ_KEY": OTHER_KEY},
        models=models,
        providers=providers,
    )

    assert status == 0
    assert [(call_of(request), bearer_of(request)) for request in providers.sent] == [
        ("groq list", f"Bearer {GROQ_KEY}"),
        ("groq ask openai/gpt-oss-120b", f"Bearer {GROQ_KEY}"),
        ("groq list", f"Bearer {OTHER_KEY}"),
        ("groq ask openai/gpt-oss-20b", f"Bearer {OTHER_KEY}"),
    ]
    assert summary(printed, "Groq (providers.groq)").startswith("Groq (providers.groq) passed")
    assert summary(printed, "Groq (providers.spare)").startswith("Groq (providers.spare) passed")


def test_a_table_with_no_model_on_it_has_its_key_checked_and_says_so() -> None:
    providers = Providers()

    status, printed = run("groq", models=models_file(GROQ_TABLE), providers=providers)

    assert status == 0
    assert providers.calls() == ["groq list"]
    assert "providers.groq" in line_of(section(printed, "Groq (providers.groq)"), "no model")


def test_a_model_on_another_server_is_asked_nothing_and_its_limits_are_not_checked() -> None:
    providers = Providers()
    models = models_file(
        provider_table("other", "https://api.example.org/v1", "OTHER_KEY"),
        model_table("far", "other", "far-model", requests_a_day=10, tokens_a_minute=500),
        model_table("near", "other", "near-model"),
        # A local server too: Ollama.
        model_table("counted", "ollama", "other-local-model", requests_a_minute=3),
    )

    status, printed = run(
        environ=BOTH | {"OTHER_KEY": OTHER_KEY}, models=models, providers=providers
    )

    assert status == 0
    assert {request.url.host for request in providers.sent} == {GROQ_HOST, GOOGLE_HOST}
    for request in providers.sent:
        assert not [name for name, value in request.headers.items() if OTHER_KEY in value]
    far = line_of(printed, "models.far")
    for fact in ["providers.other", "requests_a_day 10", "tokens_a_minute 500"]:
        assert fact in far, far
    assert "requests_a_minute 3" in line_of(printed, "models.counted")
    # Models that state no limit get no line.
    assert "models.near" not in printed
    assert "models.standard" not in printed

    # When the command line names providers, nothing is said of other servers.
    status, printed = run("groq", "gemini", environ=BOTH, models=models)

    assert status == 0
    assert "models.far" not in printed
    assert "models.counted" not in printed


@pytest.mark.parametrize(
    ("url", "is_groqs"),
    [
        ("https://API.GROQ.COM/openai/v1", True),
        ("https://api.groq.com.example.org/v1", False),
        ("https://notapi.groq.com/openai/v1", False),
        # An address the url parser can't take apart.
        ("https://[api.groq.com/openai/v1", False),
    ],
)
def test_a_table_is_groqs_by_its_whole_host(url: str, is_groqs: bool) -> None:
    providers = Providers()
    models = models_file(
        provider_table("near", url, "GROQ_API_KEY"),
        model_table("big", "near", "llama-3.1-8b-instant", requests_a_day=10),
    )

    status, printed = run(models=models, providers=providers)

    assert status == 0
    last = printed.splitlines()[-1]
    if is_groqs:
        assert "Groq (providers.near) passed" in last
        assert providers.asked("groq") == ["llama-3.1-8b-instant"]
    else:
        assert "providers.near" not in last
        # Groq is checked as itself, and the model's limit isn't checked.
        assert providers.asked("groq") == list(preflight.GROQ.models)
        assert "requests_a_day 10" in line_of(printed, "models.big", "providers.near")


def test_models_named_on_the_command_line_replace_the_files() -> None:
    providers = Providers()
    models = models_file(
        GROQ_TABLE,
        model_table("small", "groq", "openai/gpt-oss-20b"),
        model_table("big", "groq", "openai/gpt-oss-120b"),
    )

    status, printed = run(
        "groq=openai/gpt-oss-120b,llama-3.1-8b-instant", models=models, providers=providers
    )

    assert status == 0
    assert providers.asked("groq") == ["openai/gpt-oss-120b", "llama-3.1-8b-instant"]
    # The one the file has on the table is compared, and the other says it isn't.
    assert "131,072" in line_of(under(printed, "openai/gpt-oss-120b"), "context_window")
    not_in_file = under(printed, "llama-3.1-8b-instant")
    assert "providers.groq" in line_of(not_in_file, "models file")
    assert "context_window" not in not_in_file


@pytest.mark.parametrize(
    ("first", "call", "answer", "fact", "second"),
    [
        pytest.param(
            "llama-9-made-up",
            None,
            None,
            "llama-9-made-up",
            "openai/gpt-oss-120b",
            id="not-offered",
        ),
        pytest.param(
            "openai/gpt-oss-120b",
            "groq ask openai/gpt-oss-120b",
            lambda _request: httpx2.Response(429, json={"error": {"message": "Rate limit"}}),
            "429",
            "openai/gpt-oss-20b",
            id="refused",
        ),
    ],
)
def test_a_model_that_fails_before_its_answer_compares_nothing_and_the_next_one_is_compared(
    first: str, call: str | None, answer: Answer | None, fact: str, second: str
) -> None:
    providers = Providers()
    if call is not None and answer is not None:
        providers.answers[call] = answer
    # Larger than the provider's: compared, it would fail.
    models = models_file(
        GROQ_TABLE,
        model_table("first", "groq", first, max_output_tokens=70_000),
        model_table("second", "groq", second),
    )

    status, printed = run("groq", models=models, providers=providers)

    assert status == 1
    [failed] = failures(printed)
    assert fact in failed
    assert "max_output_tokens" not in under(printed, first)
    assert "131,072" in line_of(under(printed, second), "context_window")


def test_the_last_line_counts_what_a_check_didnt_check_and_fails_one_with_a_figure_too_large() -> (
    None
):
    models = models_file(
        GROQ_TABLE,
        GEMINI_TABLE,
        model_table(
            "big", "groq", "openai/gpt-oss-120b", requests_a_minute=30, tokens_a_day=200_000
        ),
        # Too large, and with a limit that isn't checked: a check that failed says that
        # it failed, and not how many figures it didn't check.
        model_table(
            "flash", "gemini", "gemini-3.6-flash", max_output_tokens=70_000, requests_a_day=20
        ),
    )

    status, printed = run(models=models)

    assert status == 1
    [failed] = failures(printed)
    assert "models.flash" in failed
    assert "requests_a_day 20" in line_of(under(printed, "gemini-3.6-flash"), "not checked")
    groq = summary(printed, "Groq (providers.groq)")
    assert "passed" in groq
    assert re.search(r"\b2\b", groq), groq
    assert summary(printed, "Gemini (providers.gemini)") == "Gemini (providers.gemini) failed"


ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "config" / "examples" / "free-cloud" / "models.toml"
SHIPPED = ROOT / "config" / "models.toml"


def as_seen_on_2026_11_26(providers: Providers) -> None:
    """Groq as one run with free keys saw it: both models with a window of 131,072 and
    answers up to 65,536 tokens, and 1,000 requests a day and 8,000 tokens a minute in
    the headers. The fake's Gemini API states what it saw."""
    listed = [
        entry | {"context_window": 131072, "max_completion_tokens": 65536}
        for entry in GROQ_MODELS[:2]
    ]
    headers = GROQ_LIMITS | {
        "x-ratelimit-limit-requests": "1000",
        "x-ratelimit-limit-tokens": "8000",
        "x-ratelimit-remaining-requests": "999",
        "x-ratelimit-remaining-tokens": "7666",
    }
    providers.answers["groq list"] = lambda _request: httpx2.Response(
        200, json={"object": "list", "data": listed}
    )
    providers.answers["groq ask"] = answered_with(headers)


def test_the_example_holds_against_what_the_providers_were_seen_to_state() -> None:
    providers = Providers()
    as_seen_on_2026_11_26(providers)

    status, printed = run("--models-file", str(EXAMPLE), models=None, providers=providers)

    assert status == 0, printed
    assert not failures(printed)
    assert providers.asked("groq") == ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]
    assert providers.asked("gemini") == ["gemini-3.6-flash", "gemini-3.5-flash-lite"]
    # Groq's requests a minute and tokens a day of both models, and the three limits
    # each of the Gemini API's two states.
    groq = summary(printed, "Groq (providers.groq)")
    assert re.search(r"\b4\b", groq), groq
    gemini = summary(printed, "Gemini (providers.gemini)")
    assert re.search(r"\b6\b", gemini), gemini
    # Its cloud models are on tables the script knows: none is said to be on a server it
    # can't ask, and its local model states no limit.
    assert " on providers." not in printed


def test_the_shipped_file_has_the_scripts_own_models_asked() -> None:
    providers = Providers()

    status, _ = run("--models-file", str(SHIPPED), models=None, providers=providers)

    assert status == 0
    assert providers.asked("groq") == list(preflight.GROQ.models)
    assert providers.asked("gemini") == list(preflight.GEMINI.models)


def test_the_scripts_own_models_are_the_examples_cloud_models() -> None:
    example = read_file(EXAMPLE)

    def on(host: str) -> list[str]:
        return [
            model.model
            for model in example.models.values()
            if urlsplit(example.providers[model.provider].url or "").hostname == host
        ]

    assert on(GROQ_HOST) == list(preflight.GROQ.models)
    assert on(GOOGLE_HOST) == list(preflight.GEMINI.models)


@pytest.mark.parametrize(
    ("url", "said"),
    [
        ("http://api.groq.com/other", True),
        # Another scheme alone, with which Actio would send the key unencrypted.
        ("http://api.groq.com/openai/v1", True),
        # Another path alone.
        ("https://api.groq.com/other", True),
        ("https://api.groq.com/openai/v1/", False),
        ("https://api.groq.com/openai/v1", False),
        # A host in capitals is the same host.
        ("https://API.GROQ.COM/openai/v1", False),
    ],
)
def test_questions_go_to_the_scripts_own_address_whatever_the_tables_url(
    url: str, said: bool
) -> None:
    providers = Providers()
    models = models_file(
        provider_table("groq", url, "GROQ_API_KEY"),
        model_table("big", "groq", "openai/gpt-oss-120b"),
    )

    status, printed = run("groq", models=models, providers=providers)

    assert status == 0
    assert providers.calls() == ["groq list", "groq ask openai/gpt-oss-120b"]
    for request in providers.sent:
        assert str(request.url).startswith(f"{GROQ_URL}/"), request.url
    if said:
        assert GROQ_URL in line_of(printed, url)
    else:
        # The script's own address is said nowhere else.
        assert GROQ_URL not in printed


def test_a_tables_url_is_printed_without_its_key_and_without_escape_sequences() -> None:
    # A url may carry a key, as the Gemini API's own surface takes one in an address.
    # The escape sequence is written as TOML writes a control character in a string.
    url = f"https://api.groq.com/openai/v1?key={GROQ_KEY}&more=\\u001b[2Jodd"
    models = models_file(
        provider_table("groq", url, "GROQ_API_KEY"),
        model_table("big", "groq", "openai/gpt-oss-120b"),
    )

    status, printed = run("groq", models=models)

    assert status == 0
    said = line_of(printed, "api.groq.com/openai/v1?key=")
    assert preflight.HIDDEN in said
    assert "more=odd" in said
    for start in range(len(GROQ_KEY) - preflight.KEY_PIECE + 1):
        assert GROQ_KEY[start : start + preflight.KEY_PIECE] not in printed
    assert all(char == "\n" or unicodedata.category(char) not in {"Cc", "Cf"} for char in printed)


def test_the_files_path_is_printed_without_escape_sequences(tmp_path: Path) -> None:
    path = tmp_path / "mo\x1b[2Jdels.toml"
    path.write_text(NO_CLOUD)

    status, printed = run("--models-file", str(path), models=None)

    assert status == 0
    assert str(tmp_path / "models.toml") in printed.splitlines()[1]
    assert all(char == "\n" or unicodedata.category(char) not in {"Cc", "Cf"} for char in printed)


def test_a_models_id_from_the_file_is_printed_without_escape_sequences() -> None:
    odd = "\x1b[2Jodd\x1b]0;title\x07-model"
    providers = Providers()
    listed = [*GROQ_MODELS, {"id": odd, "context_window": 131072, "max_completion_tokens": 65536}]
    providers.answers["groq list"] = lambda _request: httpx2.Response(
        200, json={"object": "list", "data": listed}
    )
    # As TOML writes the control characters in a string.
    escaped = odd.replace("\x1b", "\\u001b").replace("\x07", "\\u0007")

    status, printed = run("groq", models=one_model("groq", escaped), providers=providers)

    assert status == 0
    assert providers.asked("groq") == [odd]
    assert line_of(printed, "(models.big)") == "  odd-model (models.big)"
    assert "[2J" not in printed
    assert all(char == "\n" or unicodedata.category(char) not in {"Cc", "Cf"} for char in printed)


def test_what_a_providers_headers_count_is_a_key_of_the_models_file() -> None:
    for provider in preflight.PROVIDERS:
        for kind, window in provider.counts.items():
            key = preflight.counted_by(kind, window)
            assert LIMITS[key] == (kind, window.removeprefix("a "))


# As the Gemini API's OpenAI-compatible surface answered a question on 2026-11-26, to a
# model too busy: a list of one error, and no header that names a wait.
GEMINI_TOO_BUSY = [
    {
        "error": {
            "code": 503,
            "message": "This model is currently experiencing high demand. Spikes in demand "
            "are usually temporary. Please try again later.",
            "status": "UNAVAILABLE",
        }
    }
]


def too_busy(_request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(503, json=GEMINI_TOO_BUSY)


def test_a_model_too_busy_to_answer_fails_nothing_and_is_counted() -> None:
    providers = Providers()
    providers.answers["gemini ask gemini-3.6-flash"] = too_busy
    models = models_file(
        GEMINI_TABLE,
        model_table("flash", "gemini", "gemini-3.6-flash", requests_a_day=20),
        model_table("lite", "gemini", "gemini-3.5-flash-lite"),
    )

    status, printed = run("gemini", models=models, providers=providers)

    assert status == 0
    assert not failures(printed)
    # Asked once, and the model after it as usual.
    assert providers.asked("gemini") == ["gemini-3.6-flash", "gemini-3.5-flash-lite"]
    said = under(printed, "gemini-3.6-flash")
    busy = line_of(said, "too busy")
    for fact in ["503", "high demand", "no failure"]:
        assert fact in busy, busy
    assert "answered in" not in said
    # What the provider said of the model before the question is held all the same.
    assert "1,048,576" in line_of(said, "context_window")
    assert "requests_a_day 20" in line_of(said, "not checked")
    last = summary(printed, "Gemini (providers.gemini)")
    assert "passed" in last
    assert "1 model too busy" in last
    assert "1 figure" in last


def test_a_figure_too_large_fails_though_its_model_was_too_busy() -> None:
    providers = Providers()
    providers.answers["gemini ask gemini-3.6-flash"] = too_busy
    models = one_model("gemini", "gemini-3.6-flash", max_output_tokens=70_000)

    status, printed = run("gemini", models=models, providers=providers)

    assert status == 1
    [failed] = failures(printed)
    for fact in ["models.big", "max_output_tokens", "70,000", "65,536"]:
        assert fact in failed, failed
    assert "too busy" in under(printed, "gemini-3.6-flash")
    assert summary(printed, "Gemini (providers.gemini)") == "Gemini (providers.gemini) failed"


def test_the_limits_an_answer_would_have_stated_are_not_checked_for_a_model_too_busy() -> None:
    providers = Providers()
    # As Groq's server would say it, with a wait.
    providers.answers["groq ask"] = lambda _request: httpx2.Response(
        503,
        json={"error": {"message": "Service unavailable", "type": "service_unavailable"}},
        headers={"retry-after": "30"},
    )
    limits = {"requests_a_day": 1000, "tokens_a_minute": 8000, "requests_a_minute": 30}
    models = models_file(
        GROQ_TABLE,
        model_table("big", "groq", "openai/gpt-oss-120b", **limits),
        model_table("small", "groq", "openai/gpt-oss-20b"),
    )

    status, printed = run("groq", models=models, providers=providers)

    assert status == 0
    assert not failures(printed)
    said = under(printed, "openai/gpt-oss-120b")
    # The wait the provider names is said.
    assert "30 s" in line_of(said, "too busy to answer,")
    # The two limits an answer's headers state: not checked, since no answer came.
    headers = line_of(said, "requests_a_day 1,000")
    for fact in ["not checked", "tokens_a_minute 8,000", "too busy", preflight.GROQ.limits_at]:
        assert fact in headers, headers
    assert "API" not in headers
    # The one no answer states: not checked for the reason it never is.
    never = line_of(said, "requests_a_minute 30")
    assert "API" in never
    assert "too busy" not in never
    # Its window and its longest answer are in Groq's list, and held.
    assert "65,536" in line_of(said, "max_output_tokens 2,048")
    # Both models; and the three limits of the one that states any, with the other's
    # longest answer, which the fake's list leaves out of its entry.
    last = summary(printed, "Groq (providers.groq)")
    assert "passed" in last
    assert "2 models too busy" in last
    assert "4 figures" in last


def test_a_model_of_a_provider_without_a_table_that_is_too_busy_fails_nothing() -> None:
    providers = Providers()
    providers.answers["gemini ask gemini-3.6-flash"] = too_busy

    status, printed = run("gemini", providers=providers)

    assert status == 0
    assert "too busy" in under(printed, "gemini-3.6-flash")
    assert "answered in" in under(printed, "gemini-3.5-flash-lite")
    assert summary(printed, "Gemini") == "Gemini passed, with 1 model too busy to answer"


@pytest.mark.parametrize(
    ("call", "label"),
    [
        ("groq list", "Groq (providers.groq)"),
        ("gemini list", "Gemini (providers.gemini)"),
        ("gemini describe gemini-3.6-flash", "Gemini (providers.gemini)"),
    ],
)
def test_only_a_question_answered_with_503_is_a_model_too_busy(call: str, label: str) -> None:
    providers = Providers()
    providers.answers[call] = too_busy
    models = models_file(
        GROQ_TABLE,
        GEMINI_TABLE,
        model_table("big", "groq", "openai/gpt-oss-120b"),
        model_table("flash", "gemini", "gemini-3.6-flash"),
    )

    status, printed = run(models=models, providers=providers)

    # A list of models or a description answered so fails its check, as it did.
    assert status == 1
    [failed] = failures(printed)
    assert "503" in failed
    assert summary(printed, label) == f"{label} failed"
    assert "model too busy" not in printed


def test_a_wait_that_isnt_one_is_cut_where_a_providers_other_text_is() -> None:
    providers = Providers()
    providers.answers["groq ask openai/gpt-oss-120b"] = lambda _request: httpx2.Response(
        429, json={"error": {"message": "Rate limit"}}, headers={"retry-after": "x" * 1000}
    )

    status, printed = run("groq=openai/gpt-oss-120b", providers=providers)

    assert status == 1
    [failed] = failures(printed)
    # Two hundred characters of it, the last of them the mark of a cut: only a refusal's
    # message is shown further.
    assert "x" * 199 + "…" in failed
    assert "x" * 200 not in failed


def gemini_at_a_limit(limits: int) -> list[dict[str, Any]]:
    """A refusal at `limits` limits, shaped as the Gemini API's were in answers saved on
    2026-11-25: 236 characters before the first limit, each limit on a line of its own
    with its value, and the wait last. The values are made up."""
    before = (
        "You exceeded your current quota, please check your plan and billing details. For "
        "more information on this error, head to: https://aistudio.google.com/docs/rate-limits. "
        "To monitor your current usage, head to: https://ai.dev/rate-limit. "
    )
    reached = [
        "\n* Quota exceeded for metric: generativelanguage.googleapis.com/"
        f"generate_content_free_tier_requests, limit: {15 + number}, model: "
        "gemini-3.5-flash-lite, description: Generate content requests per minute limit "
        "exceeded on Free Tier. See https://aistudio.google.com/docs/rate-limits."
        for number in range(limits)
    ]
    message = before + "".join(reached) + "\nPlease retry in 49.846993202s."
    assert message.index("* Quota exceeded") == 236
    return [{"error": {"code": 429, "message": message, "status": "RESOURCE_EXHAUSTED"}}]


def test_a_refusal_at_a_limit_shows_the_limit_the_provider_names_and_its_wait() -> None:
    providers = Providers()
    providers.answers["gemini ask gemini-3.5-flash-lite"] = lambda _request: httpx2.Response(
        429, json=gemini_at_a_limit(1)
    )

    status, printed = run("gemini=gemini-3.5-flash-lite", providers=providers)

    assert status == 1
    [failed] = failures(printed)
    for fact in ["429", "requests per minute", "limit: 15", "Please retry in 49.8"]:
        assert fact in failed, failed


def test_a_refusals_message_is_cut_too_only_later_than_other_text() -> None:
    providers = Providers()
    providers.answers["gemini ask gemini-3.5-flash-lite"] = lambda _request: httpx2.Response(
        429, json=gemini_at_a_limit(40)
    )

    status, printed = run("gemini=gemini-3.5-flash-lite", providers=providers)

    assert status == 1
    [failed] = failures(printed)
    # The first limit is on the line, and the last, with the wait after it, is not.
    assert "limit: 15" in failed
    assert "limit: 54" not in failed
    assert "Please retry" not in failed
    assert "…" in failed
    assert preflight.SHOWN_AT_MOST < len(failed) < preflight.REFUSAL_SHOWN_AT_MOST + 100
