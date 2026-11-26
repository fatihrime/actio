"""Asks the cloud providers what your own account may use, and holds the models file
against what they state, before Actio relies on it.

Actio is to route answers to the free tiers of two cloud providers, Groq and Google's
Gemini API, beside the local Ollama. A models file says which of their models it offers,
and states each model's window, its longest answer and its provider's limits for it,
by which Actio counts (ADR 0030). What a free tier allows is set for each account, and
changes, so `make preflight` asks.

The models file is read with the API's own reader, so that a file the API would refuse
is refused here in the same words: the one `--models-file` names, or else the one
ACTIO_MODELS_FILE names, or else the API's default, config/models.toml, seen from the
directory the script is run in. The variable is taken as the API takes it, whenever it
is set: set and empty, it names no file, here as there. A file that can't be read is one
line, and nothing is asked. Each `[providers.<name>]` table of the file whose url is at Groq's host or the
Gemini API's is one check of that provider, with the key in the variable the table
names (`api_key_env`): the figures are an account's, and the account is the key's. A
provider the file has no table of is checked as itself, with its own variable and its
own models. Each check finds:

- that the provider takes the key;
- that each model of the file on that table, each id once, or each of the provider's
  own, is among those it offers that key;
- what it says of the model, each where it says it: its window and its longest answer,
  and for the Gemini API whether it can reason;
- that the model answers one short question, asked through the provider's
  OpenAI-compatible API, which Actio's client speaks, and how long that took;
- which limits the answer's headers state, with what is left of each and when it is
  full again;
- for a model of the file that answered, whether the file states more than the
  provider does: its window, its longest answer, and the limits the answer's headers
  state, each held against the key of what that header counts (`Provider.counts`).

A figure of the file larger than the provider's is a failure: Actio would count by more
than the provider gives. One the provider states and the file leaves out is said, and is
no failure: leaving a limit out is how an installation has nothing counted by it. A
figure of the file that the provider doesn't state, or states in a way that can't be
read, is named as not checked, with where a person reads it, and is no failure either:
Groq's API never states its requests a minute and its tokens a day, nor the Gemini API
any of its limits. A model of the file on another server is asked nothing, and its
limits are named as not checked.

Questions go to the providers' own addresses, written below, whatever url a table
gives: a key goes nowhere a file names. A table whose url isn't its provider's
OpenAI-compatible address is said, and is no failure. Each question counts against the
account's limits like any other. Nothing is retried, and nothing is kept but what is
printed.

The keys are read from the environment alone: `make preflight` adds what .env holds,
when there is one, to what the shell exports, which it leaves as it is. A key that
can't be one, holding a space, a quote, or a character outside visible ASCII, is a
failure, and nothing is asked with it. A key goes in the header its call takes, and
nowhere else, never in an address. The script itself prints no key and no part of one.
Of a provider's own text, such as an error's message, the whole key and any piece of it
KEY_PIECE characters long or longer are hidden before it is printed, and so are the
control characters a terminal would act on: a shorter piece that a provider quotes is
printed as it is. What the models file holds is printed without those control
characters too, a model's id above all, and a table's url with the key hidden in it as
in a provider's text: a url may carry one.

Exit status: 0 when every check that had a key passed; 1 when one failed, a figure too
large included, when none had a key, since a run that checked nothing is no pass, and
when the models file can't be read; 2 for arguments that can't be read.

Usage: preflight.py [--models-file PATH] [PROVIDER[=MODEL,...] ...]   (--help says more)
"""

import argparse
import os
import re
import sys
import time
import unicodedata
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import quote, urlsplit

import httpx2

from actio.catalog import LIMITS, CatalogError, CatalogFile, ModelEntry, ProviderEntry, read_file
from actio.core.settings import Settings

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

# The variable that names the models file, as the API reads it, and the file the API
# reads when it names none: the API's own default, taken from its settings so that it is
# written once, and seen from the directory the script is run in.
MODELS_FILE_VARIABLE = "ACTIO_MODELS_FILE"
DEFAULT_MODELS_FILE: Path = Settings.model_fields["models_file"].default

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

# The two figures of a model's table that a provider's list or description states: its
# window and its longest answer. The limits of `LIMITS` are the others it holds.
SIZES = ("context_window", "max_output_tokens")

type Listed = Mapping[str, Any]
"""A model as the provider's list of models has it."""


class CheckError(Exception):
    """A check that failed, said in one line, in words."""


@dataclass(frozen=True)
class Stated:
    """What a provider stated of one of the models file's figures for a model: the
    figure, or, when there is none to hold the file's against, why, in words that follow
    the file's figure, such as "which this answer didn't state"."""

    figure: int | None
    why_not: str = ""


@dataclass(frozen=True)
class Description:
    """What a provider says of a model: in words, and as the figures of the models file
    it states, by their keys, the model's window and its longest answer."""

    facts: list[str]
    stated: dict[str, Stated]


@dataclass(frozen=True)
class Provider:
    """A cloud provider, and how it is asked."""

    name: str
    """As people write it."""
    argument: str
    """As the command line names it."""
    key_variable: str
    """The variable that holds its key when the models file has no table of it."""
    keys_at: str
    """Where a key is made."""
    limits_at: str
    """Where a person reads the limits its API doesn't state."""
    models_at: str
    """Where a person reads a model's window and longest answer, when its API doesn't
    state them."""
    unstated: str | None
    """The limits its API never states, said after every check of it."""
    counts: Mapping[str, str]
    """What its `x-ratelimit-*-requests` and `-tokens` headers count, by the two words:
    "a day", "a minute". Nothing for a provider whose documentation doesn't say. It also
    says which key of the models file each header's limit is held against
    (`counted_by`)."""
    models: tuple[str, ...]
    """Those asked about when the command line names none and the models file has no
    table of the provider."""
    openai_at: str
    """Its OpenAI-compatible API, where a question is asked: the url a table of the
    models file gives it, as the API asks it."""
    offered: Callable[["Asking"], dict[str, Listed]]
    """The models it offers the key, by their ids."""
    described: Callable[["Asking", str, Listed], Description]
    """What it says of a model, from the model's entry in its list or asked for apart."""

    @property
    def host(self) -> str | None:
        """The host a table's url names for the table to be one of this provider's."""
        return urlsplit(self.openai_at).hostname


@dataclass(frozen=True)
class Asking:
    """One provider, its key, the client it is asked through, and the variable the key
    was read from."""

    provider: Provider
    key: str = field(repr=False)
    client: httpx2.Client
    variable: str

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


def plain(text: str) -> str:
    """`text` without escape sequences and other control characters, on one line: as
    what the models file and the command line hold is printed, whole."""
    text = ESCAPE.sub("", text)
    text = "".join(
        " " if char.isspace() else char
        for char in text
        if char.isspace() or unicodedata.category(char) not in {"Cc", "Cf"}
    )
    return " ".join(text.split())


def printable(text: str, key: str) -> str:
    """A provider's `text` as it may be printed: without escape sequences and other
    control characters, on one line, with `key` hidden, whole or in part, and cut to
    SHOWN_AT_MOST characters."""
    text = hidden(plain(text[: SHOWN_AT_MOST * 10]), key)
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


def in_words(items: Sequence[str]) -> str:
    """Items as a sentence lists them: `a`, `a and b`, `a, b and c`."""
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


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
            f"{provider.name} refused the key in {asking.variable} "
            f"({refusal(asking, response)}), so no model was asked"
        )
    raise CheckError(f"{provider.name} didn't list its models ({refusal(asking, response)})")


def unreadable(provider: Provider) -> str:
    """Why a figure a provider stated is held against nothing, when it isn't a number."""
    return f"as what {provider.name} stated can't be read as a number"


def size(provider: Provider, value: Any, where: str) -> Stated:
    """A model's window or its longest answer, as the provider's `where` states it: its
    list of models, or its description of the model."""
    if value is None:
        return Stated(None, f"which {where} didn't state")
    if (tokens := counted(value)) is not None:
        return Stated(tokens)
    return Stated(None, unreadable(provider))


def sizes(provider: Provider, window: Any, longest: Any, where: str) -> Description:
    """A model's window and longest answer, in words and as figures, each where the
    provider says it."""
    stated = dict(
        zip(SIZES, (size(provider, value, where) for value in (window, longest)), strict=True)
    )
    facts = []
    if (tokens := stated["context_window"].figure) is not None:
        facts.append(f"window of {tokens:,} tokens")
    if (tokens := stated["max_output_tokens"].figure) is not None:
        facts.append(f"answers up to {tokens:,} tokens")
    return Description(facts, stated)


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


def groq_described(asking: Asking, _model: str, listed: Listed) -> Description:
    """What Groq's list says of a model. Seen on 2026-11-25, each entry of the list
    carried the model's window and its longest answer."""
    return sizes(
        asking.provider,
        listed.get("context_window"),
        listed.get("max_completion_tokens"),
        "Groq's list of models",
    )


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


def gemini_described(asking: Asking, model: str, _listed: Listed) -> Description:
    """What the Gemini API says of a model, asked for apart. The window the models file
    is held to is the description's `inputTokenLimit`."""
    url = f"{GEMINI_API}/models/{quote(model, safe='')}"
    response = asking.call("GET", url, headers=asking.google())
    if response.status_code != 200:
        raise CheckError(f"Gemini didn't describe {plain(model)} ({refusal(asking, response)})")
    body = json_of(asking, response, "description of the model")
    if not isinstance(body, dict):
        raise CheckError("Gemini's description of the model isn't shaped as expected")
    description = sizes(
        asking.provider,
        body.get("inputTokenLimit"),
        body.get("outputTokenLimit"),
        "Gemini's description of the model",
    )
    if body.get("thinking") is True:
        description.facts.append("can reason")
    elif body.get("thinking") is False:
        description.facts.append("can't reason")
    return description


GROQ = Provider(
    name="Groq",
    argument="groq",
    key_variable="GROQ_API_KEY",
    keys_at="https://console.groq.com/keys",
    limits_at="https://console.groq.com/settings/limits",
    models_at="https://console.groq.com/docs/models",
    # It limits these too, for the whole organisation, and no endpoint returns them.
    unstated="requests a minute and tokens a day",
    counts={"requests": "a day", "tokens": "a minute"},
    # The cloud models of config/examples/free-cloud/models.toml: a test holds the two
    # alike.
    models=("openai/gpt-oss-120b", "openai/gpt-oss-20b"),
    openai_at=GROQ_API,
    offered=groq_offered,
    described=groq_described,
)
GEMINI = Provider(
    name="Gemini",
    argument="gemini",
    key_variable="GEMINI_API_KEY",
    keys_at="https://aistudio.google.com/apikey",
    limits_at="https://aistudio.google.com/rate-limit",
    models_at="https://ai.google.dev/gemini-api/docs/models",
    # Seen on 2026-11-25, no answer of the API carried a header about limits, and its
    # documentation names no endpoint for them: an answer that states none says where to
    # read them.
    unstated=None,
    counts={},
    models=("gemini-3.6-flash", "gemini-3.5-flash-lite"),
    openai_at=f"{GEMINI_API}/openai",
    offered=gemini_offered,
    described=gemini_described,
)
PROVIDERS = (GROQ, GEMINI)


def counted_by(kind: str, window: str) -> str:
    """The key of the models file that a provider's header of `kind`, counted over
    `window` as its `counts` says, is held against: `requests` over "a day" is
    `requests_a_day`."""
    return f"{kind}_{window.replace(' ', '_')}"


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


def limits_stated(provider: Provider, headers: httpx2.Headers) -> dict[str, Stated]:
    """What an answer's headers state of the limits of the models file, by key: the
    limit of each header whose count the provider documents (`Provider.counts`)."""
    stated = {}
    for kind, window in provider.counts.items():
        limit = headers.get(f"x-ratelimit-limit-{kind}")
        if limit is None:
            said = Stated(None, "which this answer didn't state")
        elif NUMBER.fullmatch(limit.strip()):
            said = Stated(int(limit))
        else:
            said = Stated(None, unreadable(provider))
        stated[counted_by(kind, window)] = said
    return stated


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


def model_checked(
    asking: Asking, model: str, offered: Mapping[str, Listed], stated: dict[str, Stated]
) -> Iterator[str]:
    """What a model's checks found, a line at a time as each is done. The first that
    fails raises it, and the model's checks end there. What the provider states of the
    figures of the models file is put in `stated`, by key, as it is found."""
    provider = asking.provider
    listed = offered.get(model)
    if listed is None:
        ids = ", ".join(sorted(asking.printable(offer) for offer in offered)) or "none"
        raise CheckError(
            f"{provider.name} doesn't offer {plain(model)} to this key. It offers: {ids}"
        )
    description = provider.described(asking, model, listed)
    stated.update(description.stated)
    if description.facts:
        yield ", ".join(description.facts)
    question = {"model": model, "messages": [{"role": "user", "content": QUESTION}]}
    started = time.monotonic()
    response = asking.call(
        "POST", f"{provider.openai_at}/chat/completions", headers=asking.bearer(), body=question
    )
    took_s = time.monotonic() - started
    if response.status_code != 200:
        raise CheckError(
            f"the question was refused ({refusal(asking, response)}){waits(asking, response)}"
        )
    yield answered(asking, response, took_s)
    stated.update(limits_stated(provider, response.headers))
    yield from limits(asking, response.headers) or [
        f"no limits stated in the answer: read them at {provider.limits_at}"
    ]


@dataclass
class Compared:
    """What holding a model of the models file against its provider found: a line each,
    whether one of them is a failure, and how many of the file's figures weren't
    checked."""

    lines: list[str] = field(default_factory=list)
    failed: bool = False
    unchecked: int = 0


def compared(
    provider: Provider, held: Sequence[tuple[str, ModelEntry]], stated: Mapping[str, Stated]
) -> Compared:
    """The figures of the models of the file that are one model on one table, `held`, by
    their names, held against what the provider `stated` of it. Its window and its
    longest answer are each model's own. Its limits are the same for all of them, as the
    file's own rule says, and are said once, for all of them. A key that `stated` doesn't
    hold is one the provider's API never states."""
    name = provider.name
    figures = [
        (f"models.{each}", key, getattr(entry, key)) for each, entry in held for key in SIZES
    ]
    everyone = in_words([f"models.{each}" for each, _ in held])
    figures.extend((everyone, key, figure) for key, figure in held[0][1].stated.items())
    found = Compared()
    no_larger: dict[str, list[str]] = {}
    left_out: list[str] = []
    unchecked: dict[tuple[str, str, str], list[str]] = {}
    for whose, key, mine in figures:
        theirs = stated.get(key, Stated(None, f"which {name}'s API doesn't state"))
        if mine is not None and theirs.figure is not None:
            if mine > theirs.figure:
                found.lines.append(
                    f"FAILED: for {whose}, {key} {mine:,} is more than the "
                    f"{theirs.figure:,} {name} states"
                )
                found.failed = True
            else:
                beside = f" ({name} states {theirs.figure:,})" if mine < theirs.figure else ""
                no_larger.setdefault(whose, []).append(f"{key} {mine:,}{beside}")
        elif theirs.figure is not None:
            left_out.append(
                f"for {whose}, {name} states {key} {theirs.figure:,}, and the models file "
                "states none: Actio counts nothing by it"
            )
        elif mine is not None:
            where = provider.limits_at if key in LIMITS else provider.models_at
            unchecked.setdefault((whose, theirs.why_not, where), []).append(f"{key} {mine:,}")
            found.unchecked += 1
    found.lines.extend(
        f"for {whose}, no more than {name} states: {in_words(said)}"
        for whose, said in no_larger.items()
    )
    found.lines.extend(left_out)
    found.lines.extend(
        f"for {whose}, not checked: {in_words(said)}, {why}. "
        f"Read {'it' if len(said) == 1 else 'them'} at {where}"
        for (whose, why, where), said in unchecked.items()
    )
    return found


def address(url: str) -> tuple[str, ...]:
    """An address as two of them are told apart: its scheme and host in any case, and
    one slash at its end being no difference."""
    parts = urlsplit(url)
    path = parts.path.removesuffix("/")
    return (parts.scheme.lower(), parts.netloc.lower(), path, parts.query, parts.fragment)


def provider_at(table: ProviderEntry) -> Provider | None:
    """The provider whose host a table's url names, the whole host in any case, or None
    for a table at any other server."""
    if table.url is None:
        return None
    try:
        host = urlsplit(table.url).hostname
    except ValueError:
        return None
    return next((each for each in PROVIDERS if host is not None and host == each.host), None)


@dataclass(frozen=True)
class Check:
    """One check of a provider: as a table of the models file, or as itself when the
    file has none of its tables."""

    provider: Provider
    table: str | None
    """The table's name, `providers.<table>`, or None for the provider itself."""
    entry: ProviderEntry | None
    models: tuple[str, ...]
    """Those asked: the ones the command line names, or else the file's on the table,
    each id once, or else, for the provider itself, its own."""
    held: Mapping[str, tuple[tuple[str, ModelEntry], ...]]
    """The models of the file on the table, by their id on its server, each with its
    name in the file, in the file's order."""

    @property
    def label(self) -> str:
        """As the run names the check: two checks of one provider apart."""
        if self.table is None:
            return self.provider.name
        return f"{self.provider.name} (providers.{self.table})"

    @property
    def variable(self) -> str | None:
        """The variable its key is read from: the one the table names, if any, or the
        provider's own."""
        return self.entry.api_key_env if self.entry is not None else self.provider.key_variable

    def notes(self, key: str) -> list[str]:
        """What is said of the check before anything is asked. A table's url is printed
        with `key` hidden in it, whole or in part: a url may carry one, as the Gemini
        API's own surface takes a key in an address."""
        name = self.provider.name
        if self.entry is None:
            return [f"The models file offers none of {name}'s models: no figure of it is compared"]
        notes = []
        url = self.entry.url
        if url is not None and address(url) != address(self.provider.openai_at):
            notes.append(
                f"providers.{self.table}'s url, {hidden(plain(url), key)}, isn't {name}'s "
                f"OpenAI-compatible address, {self.provider.openai_at}, which is the one asked"
            )
        if not self.held:
            notes.append(f"The models file puts no model on providers.{self.table}")
        return notes


def checks_of(
    file: CatalogFile, providers: Sequence[tuple[Provider, tuple[str, ...]]]
) -> list[Check]:
    """The checks of `providers`, each with the models the command line names for it:
    one for each table of the file at its host, in the file's order, or one of the
    provider itself when the file has none."""
    checks = []
    for provider, listed in providers:
        tables = [
            (table, entry)
            for table, entry in file.providers.items()
            if provider_at(entry) is provider
        ]
        if not tables:
            checks.append(Check(provider, None, None, listed or provider.models, {}))
        for table, entry in tables:
            held: dict[str, list[tuple[str, ModelEntry]]] = {}
            for name, model in file.models.items():
                if model.provider == table:
                    held.setdefault(model.model, []).append((name, model))
            checks.append(
                Check(
                    provider,
                    table,
                    entry,
                    listed or tuple(held),
                    {model_id: tuple(models) for model_id, models in held.items()},
                )
            )
    return checks


def elsewhere(file: CatalogFile) -> Iterator[str]:
    """A line for each model of the file on a server neither provider's, that states
    any of the limits: no server is asked for it."""
    for name, model in file.models.items():
        if provider_at(file.providers[model.provider]) is not None:
            continue
        said = [f"{key} {figure:,}" for key, figure in model.stated.items() if figure is not None]
        if said:
            yield (
                f"Not checked, for models.{name} on providers.{model.provider}: "
                f"{in_words(said)}, as this script knows no way to ask that server"
            )


def checked(asking: Asking, check: Check, out: TextIO) -> tuple[bool, int]:
    """Checks one provider's key and models, as one table of the models file or as
    itself, writes what it found, and says whether everything passed, and how many
    figures of the file weren't checked."""
    provider = asking.provider
    out.write(f"\n{check.label}\n")
    for note in check.notes(asking.key):
        out.write(f"  {note}\n")
    passed, unchecked = True, 0
    try:
        offered = provider.offered(asking)
    except CheckError as failure:
        out.write(f"  FAILED: {failure}\n")
        passed = False
    else:
        for model in check.models:
            held = check.held.get(model, ())
            names = ", ".join(f"models.{name}" for name, _ in held)
            out.write(f"  {plain(model)}" + (f" ({names})" if names else "") + "\n")
            if check.table is not None and not held:
                out.write(
                    f"    not in the models file on providers.{check.table}, so nothing is "
                    "compared\n"
                )
            stated: dict[str, Stated] = {}
            try:
                for line in model_checked(asking, model, offered, stated):
                    out.write(f"    {line}\n")
            except CheckError as failure:
                out.write(f"    FAILED: {failure}\n")
                passed = False
                continue
            if held:
                found = compared(provider, held, stated)
                for line in found.lines:
                    out.write(f"    {line}\n")
                passed = passed and not found.failed
                unchecked += found.unchecked
    if provider.unstated:
        out.write(
            f"  Not stated by {provider.name}'s API: {provider.unstated}. "
            f"Read them at {provider.limits_at}\n"
        )
    return passed, unchecked


def outcome(
    check: Check, environ: Mapping[str, str], client: httpx2.Client, out: TextIO
) -> tuple[bool, int] | None:
    """Runs one check, with the key its variable holds, and says whether it passed and
    how many figures of the file it didn't check, or None when it had no key."""
    provider = check.provider
    variable = check.variable
    if variable is None:
        out.write(
            f"\n{check.label}\n  FAILED: providers.{check.table} names no api_key_env, so "
            f"Actio would ask {provider.name} without a key, and nothing is asked\n"
        )
        return False, 0
    key = environ.get(variable, "").strip()
    if not key:
        out.write(
            f"\n{check.label}: skipped, as {variable} holds no key. "
            f"A key is made at {provider.keys_at}\n"
        )
        return None
    if not usable(key):
        out.write(
            f"\n{check.label}\n  FAILED: {variable} holds what can't be a key: a key has no "
            "spaces, no quotes and no other characters but letters, digits and signs. Look "
            "at its line in .env, or at what the shell exports\n"
        )
        return False, 0
    return checked(Asking(provider, key, client, variable), check, out)


@dataclass(frozen=True)
class Options:
    """What the command line asks."""

    providers: list[tuple[Provider, tuple[str, ...]]]
    """The providers to check, in the table's order, each with the models it names for
    it, or none."""
    named: bool
    """Whether it names providers."""
    models_file: Path | None


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


def chosen(arguments: Sequence[str]) -> Options:
    """What the arguments ask. argparse ends the process with status 2 for arguments it
    can't read."""
    table = "\n".join(
        f"  {each.argument:<8} {each.key_variable:<16} {', '.join(each.models)}"
        for each in PROVIDERS
    )
    parser = argparse.ArgumentParser(
        prog="preflight.py",
        description=(
            "Checks that each cloud provider takes its key, offers the models of the models\n"
            "file, and has each answer one short question, which counts against the\n"
            "account's limits like any other. Prints what the provider says of each model,\n"
            "and the limits its answers state, and fails when the models file states more\n"
            "than the provider."
        ),
        epilog=(
            "The models file is read as the API reads it: the one --models-file names, or\n"
            f"else {MODELS_FILE_VARIABLE}, or else {DEFAULT_MODELS_FILE}. Each of its\n"
            "[providers.<name>] tables at Groq's host or the Gemini API's is checked, with\n"
            "the key in the variable it names (api_key_env), and the file's models on it\n"
            "are asked. A provider the file has no table of is checked as itself: the\n"
            "providers, the variable that holds each one's key then, and the models it is\n"
            "asked about:\n"
            f"{table}\n\n"
            "For each model of the file that answered, its window, its longest answer and\n"
            "Groq's requests a day and tokens a minute are held against what the provider\n"
            "states: a figure of the file larger than the provider's fails. One the\n"
            "provider doesn't state, such as Groq's requests a minute and tokens a day and\n"
            "every limit of the Gemini API, is named as not checked, with where to read it.\n\n"
            "Every provider is checked when none is named. A provider named with models\n"
            "after '=', separated by commas, asks those instead, in each of its checks.\n\n"
            "examples:\n"
            "  make preflight\n"
            '  make preflight args="--models-file config/examples/free-cloud/models.toml"\n'
            "  make preflight args=groq\n"
            '  make preflight args="groq=openai/gpt-oss-20b gemini"\n'
            '  make preflight args="gemini=gemini-3.8-flash,gemini-3.6-flash"\n\n'
            "Exit status: 0 when every check that had a key passed, 1 when one failed, when\n"
            "none had a key, or when the models file can't be read, 2 for arguments that\n"
            "can't be read."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--models-file",
        type=Path,
        metavar="PATH",
        help=f"the models file to read, in the place of {MODELS_FILE_VARIABLE}'s",
    )
    parser.add_argument(
        "named",
        nargs="*",
        type=named,
        metavar="PROVIDER[=MODEL,...]",
        help="a provider to check: " + " or ".join(each.argument for each in PROVIDERS),
    )
    parsed = parser.parse_intermixed_args(arguments)
    given: list[tuple[Provider, tuple[str, ...]]] = parsed.named
    models: dict[str, list[str]] = {}
    for provider, listed in given:
        models.setdefault(provider.argument, []).extend(listed)
    return Options(
        providers=[
            (provider, tuple(dict.fromkeys(models.get(provider.argument, []))))
            for provider in PROVIDERS
            if not given or provider.argument in models
        ],
        named=bool(given),
        models_file=parsed.models_file,
    )


def models_file(options: Options, environ: Mapping[str, str]) -> Path:
    """The models file to read: the one the command line names, or else the one
    ACTIO_MODELS_FILE names, or else the API's default. The variable is taken as the API
    takes it, whenever it is set: set and empty, it names the directory the script is
    run in, which is no file, and the API wouldn't start with it either."""
    if options.models_file is not None:
        return options.models_file
    if (path := environ.get(MODELS_FILE_VARIABLE)) is not None:
        return Path(path)
    return DEFAULT_MODELS_FILE


def read(path: Path) -> CatalogFile:
    """The models file, read and checked by the API's own reader, or the failure its
    one line is."""
    try:
        return read_file(path)
    except CatalogError as error:
        raise CheckError(str(error)) from None


def run(
    arguments: Sequence[str],
    environ: Mapping[str, str],
    out: TextIO,
    transport: httpx2.BaseTransport | None = None,
) -> int:
    """Checks the providers the arguments name against the models file, with the keys
    in `environ`, writes what it found to `out`, and returns the exit status. Requests
    go through `transport`, or out to the providers when it is None."""
    options = chosen(arguments)
    path = models_file(options, environ)
    try:
        file = read(path)
    except CheckError as failure:
        out.write(f"FAILED, so nothing was asked: {plain(str(failure))}\n")
        return 1
    out.write(
        "Each model is asked one short question, which counts against the account's "
        "limits like any other.\n"
    )
    out.write(f"Models file: {plain(str(path))}\n")
    # A redirect isn't followed: the client would take `x-goog-api-key` along to another
    # host. It takes away only `Authorization`.
    with httpx2.Client(transport=transport, timeout=TIMEOUT, follow_redirects=False) as client:
        results = [
            (check.label, *result)
            for check in checks_of(file, options.providers)
            if (result := outcome(check, environ, client, out)) is not None
        ]
    if not options.named and (lines := list(elsewhere(file))):
        out.write("\n" + "".join(f"{line}\n" for line in lines))
    if not results:
        out.write("\nNo provider had a key, so nothing was checked.\n")
        return 1
    out.write(f"\n{'; '.join(summary(*result) for result in results)}.\n")
    return 0 if all(passed for _, passed, _ in results) else 1


def summary(label: str, passed: bool, unchecked: int) -> str:
    """What the last line says of one check: whether it passed, and for one that did,
    how many figures of the models file it didn't check."""
    if not passed:
        return f"{label} failed"
    if not unchecked:
        return f"{label} passed"
    figures = "figure" if unchecked == 1 else "figures"
    return f"{label} passed, with {unchecked} {figures} of the models file not checked"


def main(arguments: Sequence[str]) -> int:
    return run(arguments, os.environ, sys.stdout)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
