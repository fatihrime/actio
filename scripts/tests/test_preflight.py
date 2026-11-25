"""`make preflight` asks the cloud providers what an account may use, with its keys.

`scripts/preflight.py` runs here against a fake of both providers. Its lists, answers
and headers are shaped as the providers' documentation shows them, and its refusals of
a key as each provider answered a request made with a made-up key. Seen with real keys
on 2026-11-25, Groq's list entries carried the window and the longest answer, and no
answer of the Gemini API carried a header about limits: the fake leaves the longest
answer out of some entries, to show what is said without it. Two refusals are
made up here, in no shape either was seen to give: Groq's at a rate limit, and the
Gemini API's 403. So are the bodies that are broken on purpose. A request the fake
doesn't expect fails the test, and a test that must see no request at all gives it a
fake that fails at any. What a run prints is read for the facts in its lines, not for
their wording.
"""

import io
import json
import time
import unicodedata
from collections.abc import Callable, Mapping
from typing import Any

import httpx2
import pytest
from scripts import preflight

# Two keys that share no piece as long as the pieces the script hides, so that one
# sent to the other provider, or hidden in its text, would show.
GROQ_KEY = "key-for-tests"
GEMINI_KEY = "made-up-value"
BOTH = {"GROQ_API_KEY": GROQ_KEY, "GEMINI_API_KEY": GEMINI_KEY}

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


def run(
    *arguments: str, environ: Mapping[str, str] = BOTH, providers: Answer | None = None
) -> tuple[int, str]:
    """The exit status of a run with these arguments and keys, and what it printed."""
    out = io.StringIO()
    transport = httpx2.MockTransport(providers or Providers())
    status = preflight.run(arguments, environ, out, transport)
    return status, out.getvalue()


def under(printed: str, model: str) -> str:
    """What a run said of `model`: the lines under its heading."""
    lines = printed.splitlines()
    start = end = lines.index(f"  {model}") + 1
    while end < len(lines) and lines[end].startswith("    "):
        end += 1
    return "\n".join(lines[start:end])


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
    for start in range(len(key) - preflight.KEY_PIECE + 1):
        assert key[start : start + preflight.KEY_PIECE] not in printed


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
        asking = preflight.Asking(preflight.GROQ, GROQ_KEY, client)

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
