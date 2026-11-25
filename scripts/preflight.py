"""Asks the cloud providers what your own account may use, before Actio relies on it.

Actio is to route answers to the free tiers of two cloud providers, Groq and Google's
Gemini API, beside the local Ollama. What a free tier allows is set for each account,
and changes, so `make preflight` asks. For each provider whose key is in the
environment, it checks:

- that the provider takes the key;
- that each model Actio means to use is among those it offers that key;
- what it says of the model, each where it says it: its window and its longest answer,
  and for the Gemini API whether it can reason;
- that the model answers one short question, asked through the provider's
  OpenAI-compatible API, which Actio's client speaks, and how long that took;
- which limits the answer's headers state, with what is left of each and when it is
  full again.

Neither provider's API states all of its limits: what it leaves out, a person reads on
the provider's site, at an address this prints. Each question counts against the
account's limits like any other. Nothing is retried, and nothing is kept but what is
printed.

The keys are read from the environment alone, GROQ_API_KEY and GEMINI_API_KEY: `make
preflight` adds what .env holds, when there is one, to what the shell exports, which it
leaves as it is. A key that can't be one, holding a space, a quote, or a character
outside visible ASCII, is a failure, and its provider is asked nothing. A key goes in
the header its call takes, and nowhere else, never in an address. The script itself
prints no key and no part of one. Of a provider's own text, such as an error's message,
the whole key and any piece of it KEY_PIECE characters long or longer are hidden before
it is printed, and so are the control characters a terminal would act on: a shorter
piece that a provider quotes is printed as it is.

Exit status: 0 when every provider that had a key passed; 1 when one failed, or when
none had a key, since a run that checked nothing is no pass; 2 for arguments that can't
be read.

Usage: preflight.py [PROVIDER[=MODEL,...] ...]   (--help says more)
"""

import argparse
import os
import re
import sys
import time
import unicodedata
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, TextIO
from urllib.parse import quote

import httpx2

# What each model is asked. Short, so that it costs little of the account's limits.
QUESTION = "Reply with the word OK."
CONNECT_S = 5.0
REQUEST_S = 30.0
TIMEOUT = httpx2.Timeout(REQUEST_S, connect=CONNECT_S)

GROQ_API = "https://api.groq.com/openai/v1"
# The Gemini API's own surface, which lists and describes its models. Its
# OpenAI-compatible one, which answers questions, is under it, at /openai.
GEMINI_API = "https://generativelanguage.googleapis.com/v1beta"
# The Gemini API lists its models a page at a time, this many at the most. Any more
# pages than MOST_PAGES would be a list that never ends.
GEMINI_PAGE = 1000
MOST_PAGES = 20

# How much of a provider's text, such as an error's message, is printed.
SHOWN_AT_MOST = 200
# A piece of a key this long or longer, found in a provider's text, is hidden as the
# whole key is: some providers quote a key's first and last few characters. A shorter
# piece says little of a key, and the text's own words could match it.
KEY_PIECE = 6
HIDDEN = "[hidden]"
# What a key can hold: one or more visible ASCII characters, but for the two quotes. A
# space, a quote, straight or as pasted from a web page, or a character that can't be
# seen is in no key, and the HTTP client can't send some of them in a header at all.
KEY = re.compile(r"[!#-&(-~]+")

# The escape sequences a terminal acts on: CSI ones, which colour text and move the
# cursor, OSC ones, which set a window's title or make a link, and the two-character
# ones. What is left of a control character after them is removed as well.
ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|[@-_])")
# The numbers below are read only as long as they can be: a longer one is shown as it
# came, as one that isn't understood. Python refuses to read a whole number of more
# than 4,300 digits, and a decimal one of 400 is infinite.
# A whole number, as a limit and what is left of it are written.
NUMBER = re.compile(r"[0-9]{1,15}")
# Seconds, as `retry-after` is written.
SECONDS = re.compile(r"[0-9]{1,9}(?:\.[0-9]{1,9})?")
# A duration in Go's notation, as Groq writes when a limit is full again: `7.66s`,
# `2m59.56s`, `1h0m0s`, `120ms`. Go writes a zero as `0`, without a unit.
GO_PART = re.compile(r"([0-9]{1,9}(?:\.[0-9]{0,9})?|\.[0-9]{1,9})(ns|us|µs|μs|ms|s|m|h)")
GO_DURATION = re.compile(rf"(?:{GO_PART.pattern})+|0")
GO_UNIT_S = {"ns": 1e-9, "us": 1e-6, "µs": 1e-6, "μs": 1e-6, "ms": 1e-3, "s": 1, "m": 60, "h": 3600}

type Listed = Mapping[str, Any]
"""A model as the provider's list of models has it."""


class CheckError(Exception):
    """A check that failed, said in one line, in words."""


@dataclass(frozen=True)
class Provider:
    """A cloud provider, and how it is asked."""

    name: str
    """As people write it."""
    argument: str
    """As the command line names it."""
    key_variable: str
    keys_at: str
    """Where a key is made."""
    limits_at: str
    """Where a person reads the limits its API doesn't state."""
    unstated: str | None
    """The limits its API never states, said after every check of it."""
    counts: Mapping[str, str]
    """What its `x-ratelimit-*-requests` and `-tokens` headers count, by the two words:
    "a day", "a minute". Nothing for a provider whose documentation doesn't say."""
    models: tuple[str, ...]
    """Those asked about when the command line names none."""
    chat_at: str
    """Where a question is asked: its OpenAI-compatible API's chat completions."""
    offered: Callable[["Asking"], dict[str, Listed]]
    """The models it offers the key, by their ids."""
    described: Callable[["Asking", str, Listed], list[str]]
    """What it says of a model, in words, from the model's entry in its list or asked
    for apart."""


@dataclass(frozen=True)
class Asking:
    """One provider, its key, and the client it is asked through."""

    provider: Provider
    key: str = field(repr=False)
    client: httpx2.Client

    def bearer(self) -> dict[str, str]:
        """The key as an OpenAI-compatible API takes it."""
        return {"Authorization": f"Bearer {self.key}"}

    def google(self) -> dict[str, str]:
        """The key as the Gemini API's own surface takes it."""
        return {"x-goog-api-key": self.key}

    def call(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, str | int] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> httpx2.Response:
        """One request, whatever it is answered with. A request whose address can't be
        made, as with a page token longer than an address may hold, and a provider that
        can't be reached or doesn't answer in time, are failures, named by the error's
        kind alone."""
        name = self.provider.name
        try:
            return self.client.request(method, url, headers=headers, params=params, json=body)
        except httpx2.InvalidURL as error:
            raise CheckError(
                f"the request to {name} can't be made ({type(error).__name__})"
            ) from None
        except httpx2.ConnectTimeout:
            raise CheckError(f"couldn't connect to {name} within {CONNECT_S:g} s") from None
        except httpx2.TimeoutException:
            raise CheckError(f"{name} didn't answer within {REQUEST_S:g} s") from None
        except httpx2.TransportError as error:
            raise CheckError(f"can't reach {name} ({type(error).__name__})") from None
        except httpx2.RequestError as error:
            raise CheckError(f"{name}'s answer can't be read ({type(error).__name__})") from None

    def printable(self, text: str) -> str:
        return printable(text, self.key)


def printable(text: str, key: str) -> str:
    """A provider's `text` as it may be printed: without escape sequences and other
    control characters, on one line, with `key` hidden, whole or in part, and cut to
    SHOWN_AT_MOST characters."""
    text = ESCAPE.sub("", text[: SHOWN_AT_MOST * 10])
    text = "".join(
        " " if char.isspace() else char
        for char in text
        if char.isspace() or unicodedata.category(char) not in {"Cc", "Cf"}
    )
    text = " ".join(hidden(text, key).split())
    return text if len(text) <= SHOWN_AT_MOST else text[: SHOWN_AT_MOST - 1] + "…"


def hidden(text: str, key: str) -> str:
    """`text` with each piece of `key` in it that is KEY_PIECE characters or longer, the
    whole key too, put as HIDDEN."""
    kept: list[str] = []
    at = 0
    while at < len(text):
        end = at
        while end < len(text) and text[at : end + 1] in key:
            end += 1
        if end - at >= KEY_PIECE:
            kept.append(HIDDEN)
            at = end
        else:
            kept.append(text[at])
            at += 1
    return "".join(kept)


def usable(key: str) -> bool:
    """Whether `key` can be a key: one or more visible ASCII characters, none of them a
    quote, and nothing else."""
    return KEY.fullmatch(key) is not None


def counted(value: Any) -> int | None:
    """A count from a provider's JSON, or None for anything else: a number below zero,
    true and false too."""
    return value if type(value) is int and value >= 0 else None


def seconds_in(duration: str) -> float | None:
    """How many seconds a duration in Go's notation is, or None for text that isn't one."""
    if not GO_DURATION.fullmatch(duration):
        return None
    return sum(float(amount) * GO_UNIT_S[unit] for amount, unit in GO_PART.findall(duration))


def spoken(seconds: float) -> str:
    """A time in words, to the hundredth of a second: `2 min 59.56 s`, `1 h`."""
    hours, rest = divmod(round(seconds * 100), 360_000)
    minutes, hundredths = divmod(rest, 6_000)
    parts = [f"{hours} h"] if hours else []
    if minutes:
        parts.append(f"{minutes} min")
    if hundredths or not parts:
        parts.append(f"{hundredths / 100:g} s")
    return " ".join(parts)


def number(value: str, key: str) -> str:
    """A count from a header, with thousands separators, or as it came if it isn't one."""
    if NUMBER.fullmatch(value.strip()):
        return f"{int(value):,}"
    return f'"{printable(value, key)}" (not understood)'


def duration(value: str, key: str) -> str:
    """A duration from a header, in words, or as it came if it isn't one."""
    seconds = seconds_in(value.strip())
    if seconds is None:
        return f'"{printable(value, key)}" (not understood)'
    return spoken(seconds)


def json_of(asking: Asking, response: httpx2.Response, what: str) -> Any:
    """The JSON of an answer, or the failure an answer that can't be read as JSON is.
    Python's reader gives up on JSON nested some thousand levels deep."""
    try:
        return response.json()
    except ValueError:
        raise CheckError(f"{asking.provider.name}'s {what} isn't JSON") from None
    except RecursionError:
        raise CheckError(f"{asking.provider.name}'s {what} is nested too deep to read") from None


def error_of(response: httpx2.Response) -> Any:
    """The `error` member of a refusal's body: an object at Groq and on the Gemini API's
    own surface, a list of one such object on its OpenAI-compatible one. Nothing for a
    body that can't be read as JSON."""
    try:
        body = response.json()
    except (ValueError, RecursionError):
        return None
    if isinstance(body, list) and len(body) == 1:
        body = body[0]
    return body.get("error") if isinstance(body, dict) else None


def refusal(asking: Asking, response: httpx2.Response) -> str:
    """A refusal in a few words: its status, and the provider's message when it has one."""
    said = f"HTTP {response.status_code} {httpx2.codes.get_reason_phrase(response.status_code)}"
    error = error_of(response)
    message = error.get("message") if isinstance(error, dict) else error
    if isinstance(message, str) and (shown := asking.printable(message)):
        return f"{said.rstrip()}: {shown}"
    return said.rstrip()


def refuses_key(response: httpx2.Response) -> bool:
    """Whether a refusal is of the key: a 401 or a 403 from either provider, or a 400
    that gives the reason `API_KEY_INVALID`. Groq was seen refusing a made-up key with
    401, and the Gemini API with that 400."""
    if response.status_code in {401, 403}:
        return True
    error = error_of(response)
    details = error.get("details") if isinstance(error, dict) else None
    return (
        response.status_code == 400
        and isinstance(details, list)
        and any(
            isinstance(detail, dict) and detail.get("reason") == "API_KEY_INVALID"
            for detail in details
        )
    )


def listing(asking: Asking, response: httpx2.Response) -> Any:
    """The JSON of a list of models, or the failure its refusal is."""
    provider = asking.provider
    if response.status_code == 200:
        return json_of(asking, response, "list of models")
    if refuses_key(response):
        raise CheckError(
            f"{provider.name} refused the key in {provider.key_variable} "
            f"({refusal(asking, response)}), so no model was asked"
        )
    raise CheckError(f"{provider.name} didn't list its models ({refusal(asking, response)})")


def sizes(window: Any, longest: Any) -> list[str]:
    """A model's window and longest answer, in words, each where the provider says it."""
    facts = []
    if (tokens := counted(window)) is not None:
        facts.append(f"window of {tokens:,} tokens")
    if (tokens := counted(longest)) is not None:
        facts.append(f"answers up to {tokens:,} tokens")
    return facts


def groq_offered(asking: Asking) -> dict[str, Listed]:
    """Groq's list of the models it offers: all of them, on one page."""
    body = listing(asking, asking.call("GET", f"{GROQ_API}/models", headers=asking.bearer()))
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list):
        raise CheckError("Groq's list of models isn't shaped as expected")
    return {
        entry["id"]: entry
        for entry in data
        if isinstance(entry, dict) and isinstance(entry.get("id"), str)
    }


def groq_described(_asking: Asking, _model: str, listed: Listed) -> list[str]:
    """What Groq's list says of a model. Seen on 2026-11-25, each entry of the list
    carried the model's window and its longest answer."""
    return sizes(listed.get("context_window"), listed.get("max_completion_tokens"))


def can_be_asked(entry: Listed) -> bool:
    """Whether a model of the Gemini API's list can be asked a question. The list holds
    others, such as models that make embeddings. The member's name is the
    documentation's, and wasn't seen with a real key: an entry without it, or with
    something else than a list there, is taken to be one that can."""
    methods = entry.get("supportedGenerationMethods")
    return not isinstance(methods, list) or "generateContent" in methods


def gemini_offered(asking: Asking) -> dict[str, Listed]:
    """The Gemini API's list of the models it offers that can be asked a question, read
    to its last page. It names a model `models/<id>`."""
    offered: dict[str, Listed] = {}
    params: dict[str, str | int] = {"pageSize": GEMINI_PAGE}
    for _ in range(MOST_PAGES):
        response = asking.call(
            "GET", f"{GEMINI_API}/models", headers=asking.google(), params=params
        )
        body = listing(asking, response)
        models = body.get("models", []) if isinstance(body, dict) else None
        token = body.get("nextPageToken") if isinstance(body, dict) else None
        if not isinstance(models, list) or not (token is None or isinstance(token, str)):
            raise CheckError("Gemini's list of models isn't shaped as expected")
        for entry in models:
            if (
                isinstance(entry, dict)
                and isinstance(entry.get("name"), str)
                and can_be_asked(entry)
            ):
                offered[entry["name"].removeprefix("models/")] = entry
        if not token:
            return offered
        params = {"pageSize": GEMINI_PAGE, "pageToken": token}
    raise CheckError(f"Gemini's list of models went on past {MOST_PAGES} pages")


def gemini_described(asking: Asking, model: str, _listed: Listed) -> list[str]:
    """What the Gemini API says of a model, asked for apart."""
    url = f"{GEMINI_API}/models/{quote(model, safe='')}"
    response = asking.call("GET", url, headers=asking.google())
    if response.status_code != 200:
        raise CheckError(f"Gemini didn't describe {model} ({refusal(asking, response)})")
    body = json_of(asking, response, "description of the model")
    if not isinstance(body, dict):
        raise CheckError("Gemini's description of the model isn't shaped as expected")
    facts = sizes(body.get("inputTokenLimit"), body.get("outputTokenLimit"))
    if body.get("thinking") is True:
        facts.append("can reason")
    elif body.get("thinking") is False:
        facts.append("can't reason")
    return facts


GROQ = Provider(
    name="Groq",
    argument="groq",
    key_variable="GROQ_API_KEY",
    keys_at="https://console.groq.com/keys",
    limits_at="https://console.groq.com/settings/limits",
    # It limits these too, for the whole organisation, and no endpoint returns them.
    unstated="requests a minute and tokens a day",
    counts={"requests": "a day", "tokens": "a minute"},
    models=("openai/gpt-oss-120b", "openai/gpt-oss-20b"),
    chat_at=f"{GROQ_API}/chat/completions",
    offered=groq_offered,
    described=groq_described,
)
GEMINI = Provider(
    name="Gemini",
    argument="gemini",
    key_variable="GEMINI_API_KEY",
    keys_at="https://aistudio.google.com/apikey",
    limits_at="https://aistudio.google.com/rate-limit",
    # Seen on 2026-11-25, no answer of the API carried a header about limits, and its
    # documentation names no endpoint for them: an answer that states none says where to
    # read them.
    unstated=None,
    counts={},
    models=("gemini-3.6-flash", "gemini-3.5-flash-lite"),
    chat_at=f"{GEMINI_API}/openai/chat/completions",
    offered=gemini_offered,
    described=gemini_described,
)
PROVIDERS = (GROQ, GEMINI)


def limits(asking: Asking, headers: httpx2.Headers) -> list[str]:
    """The limits an answer's headers state, a line each, with what is left of each and
    when it is full again. A header that is missing states nothing."""
    lines = []
    for kind in ("requests", "tokens"):
        limit, left, reset = (
            headers.get(f"x-ratelimit-{part}-{kind}") for part in ("limit", "remaining", "reset")
        )
        said = []
        if limit is not None:
            said.append(f"limit {number(limit, asking.key)}")
        if left is not None:
            said.append(f"{number(left, asking.key)} left")
        if reset is not None:
            said.append(f"full again in {duration(reset, asking.key)}")
        if said:
            window = asking.provider.counts.get(kind)
            counts = f"{kind} {window}" if window else f"{kind}, over a time not documented"
            lines.append(f"{counts}: {', '.join(said)}")
    return lines


def answered(asking: Asking, response: httpx2.Response, took_s: float) -> str:
    """That a model answered, how long it took and how many tokens the question and the
    answer were, or the failure an answer that isn't one is."""
    body = json_of(asking, response, "answer")
    choices = body.get("choices") if isinstance(body, dict) else None
    if not (
        isinstance(choices, list)
        and choices
        and isinstance(choices[0], dict)
        and isinstance(choices[0].get("message"), dict)
    ):
        raise CheckError(f"{asking.provider.name}'s answer isn't shaped as expected")
    usage = body.get("usage")
    question, answer = (
        (counted(usage.get("prompt_tokens")), counted(usage.get("completion_tokens")))
        if isinstance(usage, dict)
        else (None, None)
    )
    tokens = []
    if question is not None:
        tokens.append(f"{question:,} tokens in the question")
    if answer is not None:
        tokens.append(f"{answer:,} in the answer" if tokens else f"{answer:,} tokens in the answer")
    return f"answered in {took_s:.2f} s" + (f": {', '.join(tokens)}" if tokens else "")


def waits(asking: Asking, response: httpx2.Response) -> str:
    """How long a refusal asks to wait, when it says, in a few words to follow it."""
    after = response.headers.get("retry-after")
    if after is None:
        return ""
    if SECONDS.fullmatch(after.strip()):
        return f"; it asks to wait {spoken(float(after))}"
    return f'; it asks to wait "{asking.printable(after)}" (not understood)'


def model_checked(asking: Asking, model: str, offered: Mapping[str, Listed]) -> Iterator[str]:
    """What a model's checks found, a line at a time as each is done. The first that
    fails raises it, and the model's checks end there."""
    provider = asking.provider
    listed = offered.get(model)
    if listed is None:
        ids = ", ".join(sorted(asking.printable(offer) for offer in offered)) or "none"
        raise CheckError(f"{provider.name} doesn't offer {model} to this key. It offers: {ids}")
    if facts := provider.described(asking, model, listed):
        yield ", ".join(facts)
    question = {"model": model, "messages": [{"role": "user", "content": QUESTION}]}
    started = time.monotonic()
    response = asking.call("POST", provider.chat_at, headers=asking.bearer(), body=question)
    took_s = time.monotonic() - started
    if response.status_code != 200:
        raise CheckError(
            f"the question was refused ({refusal(asking, response)}){waits(asking, response)}"
        )
    yield answered(asking, response, took_s)
    yield from limits(asking, response.headers) or [
        f"no limits stated in the answer: read them at {provider.limits_at}"
    ]


def checked(asking: Asking, models: Sequence[str], out: TextIO) -> bool:
    """Checks one provider's key and models, writes what it found, and says whether
    everything passed."""
    provider = asking.provider
    out.write(f"\n{provider.name}\n")
    try:
        offered = provider.offered(asking)
    except CheckError as failure:
        out.write(f"  FAILED: {failure}\n")
        passed = False
    else:
        passed = True
        for model in models:
            out.write(f"  {model}\n")
            try:
                for line in model_checked(asking, model, offered):
                    out.write(f"    {line}\n")
            except CheckError as failure:
                out.write(f"    FAILED: {failure}\n")
                passed = False
    if provider.unstated:
        out.write(
            f"  Not stated by {provider.name}'s API: {provider.unstated}. "
            f"Read them at {provider.limits_at}\n"
        )
    return passed


def named(argument: str) -> tuple[Provider, tuple[str, ...]]:
    """A provider the command line names, and the models named for it, if any."""
    name, equals, models = argument.partition("=")
    provider = next((each for each in PROVIDERS if each.argument == name), None)
    if provider is None:
        known = " or ".join(each.argument for each in PROVIDERS)
        raise argparse.ArgumentTypeError(f"no provider is called {name!r}: name {known}")
    listed = tuple(model.strip() for model in models.split(",") if model.strip())
    if equals and not listed:
        raise argparse.ArgumentTypeError(f"{argument!r} names no model after its '='")
    return provider, listed


def chosen(arguments: Sequence[str]) -> list[tuple[Provider, tuple[str, ...]]]:
    """The providers to check, in the table's order, each with the models to ask it.
    argparse ends the process with status 2 for arguments it can't read."""
    table = "\n".join(
        f"  {each.argument:<8} {each.key_variable:<16} {', '.join(each.models)}"
        for each in PROVIDERS
    )
    parser = argparse.ArgumentParser(
        prog="preflight.py",
        description=(
            "Checks that each cloud provider takes its key, offers the models Actio means\n"
            "to use, and has each answer one short question, which counts against the\n"
            "account's limits like any other. Prints what the provider says of each model,\n"
            "and the limits its answers state."
        ),
        epilog=(
            "The providers, the variable that holds each one's key, and the models each\n"
            "is asked about when none are named:\n"
            f"{table}\n\n"
            "Every provider is checked when none is named. A provider named with models\n"
            "after '=', separated by commas, asks those instead of its own.\n\n"
            "examples:\n"
            "  make preflight\n"
            "  make preflight args=groq\n"
            '  make preflight args="groq=openai/gpt-oss-20b gemini"\n'
            '  make preflight args="gemini=gemini-3.8-flash,gemini-3.6-flash"\n\n'
            "Exit status: 0 when every provider that had a key passed, 1 when one failed\n"
            "or none had a key, 2 for arguments that can't be read."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "named",
        nargs="*",
        type=named,
        metavar="PROVIDER[=MODEL,...]",
        help="a provider to check: " + " or ".join(each.argument for each in PROVIDERS),
    )
    given: list[tuple[Provider, tuple[str, ...]]] = parser.parse_args(arguments).named
    models: dict[str, list[str]] = {}
    for provider, listed in given:
        models.setdefault(provider.argument, []).extend(listed)
    return [
        (provider, tuple(dict.fromkeys(models.get(provider.argument, []))) or provider.models)
        for provider in PROVIDERS
        if not given or provider.argument in models
    ]


def run(
    arguments: Sequence[str],
    environ: Mapping[str, str],
    out: TextIO,
    transport: httpx2.BaseTransport | None = None,
) -> int:
    """Checks the providers the arguments name, with the keys in `environ`, writes what
    it found to `out`, and returns the exit status. Requests go through `transport`, or
    out to the providers when it is None."""
    providers = chosen(arguments)
    out.write(
        "Each model is asked one short question, which counts against the account's "
        "limits like any other.\n"
    )
    results = []
    # A redirect isn't followed: the client would take `x-goog-api-key` along to another
    # host. It takes away only `Authorization`.
    with httpx2.Client(transport=transport, timeout=TIMEOUT, follow_redirects=False) as client:
        for provider, models in providers:
            key = environ.get(provider.key_variable, "").strip()
            if not key:
                out.write(
                    f"\n{provider.name}: skipped, as {provider.key_variable} holds no key. "
                    f"A key is made at {provider.keys_at}\n"
                )
                continue
            if not usable(key):
                out.write(
                    f"\n{provider.name}\n  FAILED: {provider.key_variable} holds what can't be "
                    "a key: a key has no spaces, no quotes and no other characters but "
                    "letters, digits and signs. Look at its line in .env, or at what the "
                    "shell exports\n"
                )
                results.append((provider.name, False))
                continue
            passed = checked(Asking(provider, key, client), models, out)
            results.append((provider.name, passed))
    if not results:
        out.write("\nNo provider had a key, so nothing was checked.\n")
        return 1
    said = "; ".join(f"{name} {'passed' if passed else 'failed'}" for name, passed in results)
    out.write(f"\n{said}.\n")
    return 0 if all(passed for _, passed in results) else 1


def main(arguments: Sequence[str]) -> int:
    return run(arguments, os.environ, sys.stdout)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
