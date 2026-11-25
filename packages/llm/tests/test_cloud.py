"""The OpenAI-compatible adapter, against Groq and the Gemini API.

Each test replays a cassette: one real exchange with the provider's OpenAI-compatible
layer, made with a free key (see conftest.py). What the adapter yields is compared with
the cassette's answer, read here apart from the adapter, so that a test holds for the
recorded answer and for a fresh one alike. `make test-cloud` asks the real servers
instead, with your keys: about fifteen short requests, which count against your
accounts' limits. Two of them go to `gemini-3.6-flash`, of which a free account had 20
a day on 2026-11-25: everything else of the Gemini API's is asked of
`gemini-3.5-flash-lite`, which had 500. Five exchanges depend on the state of the
account or of the provider, such as a limit that was reached or a model too busy:
written from real answers, they are never asked live.

What each provider was seen to do, and why the adapter reads it as it does: ADR 0027.
"""

import asyncio
import json
import re
from collections.abc import AsyncGenerator, Iterator
from contextlib import aclosing
from pathlib import Path
from typing import Any

import pytest

from actio_llm import (
    ChatRequest,
    ContextOverflowError,
    Finish,
    FinishReason,
    Message,
    ModelNotFoundError,
    OpenAICompatible,
    ProviderError,
    ProviderUnavailableError,
    RateLimitedError,
    ReasoningDelta,
    RequestRejectedError,
    StreamEvent,
    StreamInterruptedError,
    System,
    TextDelta,
    Timings,
    ToolCall,
    ToolSpec,
    Usage,
    User,
)

pytestmark = pytest.mark.anyio

FIXTURES = Path(__file__).parent / "fixtures"
WAITS_AT_MOST_S = 60
"""How long a test waits for a real server, live."""

LARGE, SMALL = "openai/gpt-oss-120b", "openai/gpt-oss-20b"
FLASH, LITE = "gemini-3.6-flash", "gemini-3.5-flash-lite"

BRIEFLY = System("You are a helpful assistant. Answer briefly.")
QUESTION = "What is the capital of France? Answer in one short sentence."
TRAIN = (
    "A train leaves at 9:40 and takes 95 minutes. When does it arrive? "
    "Answer in one short sentence."
)
SEATING = (
    "Five friends (Ada, Ben, Cem, Dua, Eli) sit in a row. Ada is not at either end. "
    "Ben sits left of Cem. Dua sits next to Eli. Cem is not next to Ada. Eli is at an "
    "end. Work out one seating that fits, checking each rule, then give it in one line."
)
BOTH_CITIES = (
    "What is the weather in Paris and in Rome right now? "
    "Call the tool once for each city, both calls at once."
)
WEATHER = ToolSpec(
    name="get_weather",
    description="The weather in a city, right now.",
    parameters={
        "type": "object",
        "properties": {"city": {"type": "string", "description": "The city's name"}},
        "required": ["city"],
    },
)
MADE_UP_KEY = "a-made-up-key-for-tests"

# What the models file of config/examples/free-cloud tells a model about reasoning:
# Groq's when a turn asks for none, flash-lite's when a turn asks for some. Asked to
# show its thoughts alone, flash-lite didn't reason at all.
LEAST_REASONING = {"reasoning_effort": "low"}
SHOW_THOUGHTS = {
    "extra_body": {
        "google": {"thinking_config": {"include_thoughts": True, "thinking_level": "high"}}
    }
}


def ask(model: str, prompt: str = QUESTION, *, briefly: bool = True, **options: Any) -> ChatRequest:
    options.setdefault("max_output_tokens", 512)
    messages: list[Message] = [BRIEFLY, User(prompt)] if briefly else [User(prompt)]
    return ChatRequest(model=model, messages=messages, **options)


async def collect(stream: AsyncGenerator[StreamEvent]) -> list[StreamEvent]:
    async with asyncio.timeout(WAITS_AT_MOST_S), aclosing(stream) as events:
        return [event async for event in events]


async def listed(adapter: OpenAICompatible) -> list[str]:
    async with asyncio.timeout(WAITS_AT_MOST_S):
        return await adapter.models()


def text_of(events: list[StreamEvent]) -> str:
    return "".join(event.text for event in events if isinstance(event, TextDelta))


def thoughts_of(events: list[StreamEvent]) -> list[str]:
    return [event.text for event in events if isinstance(event, ReasoningDelta)]


def thought_before_text(events: list[StreamEvent]) -> bool:
    kinds = [type(event) for event in events if isinstance(event, ReasoningDelta | TextDelta)]
    return kinds == sorted(kinds, key=lambda kind: kind is TextDelta)


# --- What the tape holds, read apart from the adapter ---------------------------------


def deltas(tape: Any) -> list[dict[str, Any]]:
    """The delta of each event's choice: each asked for one answer."""
    return [choice["delta"] for event in tape.events for choice in event.get("choices", [])]


def usage_of(tape: Any) -> dict[str, Any]:
    """The last count the answer stated."""
    counted: dict[str, Any] = [event["usage"] for event in tape.events if event.get("usage")][-1]
    return counted


def written(tape: Any) -> str:
    """The answer's text, as its events wrote it."""
    return "".join(delta.get("content") or "" for delta in deltas(tape) if not is_thought(delta))


def is_thought(delta: dict[str, Any]) -> bool:
    return ((delta.get("extra_content") or {}).get("google") or {}).get("thought") is True


def fragments_of(tape: Any) -> list[dict[str, Any]]:
    return [part for delta in deltas(tape) for part in delta.get("tool_calls") or []]


def calls_in(tape: Any) -> list[ToolCall]:
    """The tool calls of an answer: each whole in one fragment, as both providers were
    seen to send them."""
    return [
        ToolCall(part["id"], part["function"]["name"], json.loads(part["function"]["arguments"]))
        for part in fragments_of(tape)
    ]


def finish_reason_of(tape: Any) -> str:
    reasons = [
        choice["finish_reason"]
        for event in tape.events
        for choice in event.get("choices", [])
        if choice.get("finish_reason")
    ]
    return str(reasons[-1])


# --- Groq ------------------------------------------------------------------------------


async def test_groq_reasons_before_it_answers_and_says_where_the_time_went(
    cloud_with: Any,
) -> None:
    """Asked to reason as little as it can: "low", as the example's models file tells
    it when a turn asks for no reasoning."""
    groq, tape = cloud_with("groq", "answer", thinking_off=LEAST_REASONING)
    events = await collect(groq.stream(ask(LARGE, thinking=False)))

    reasoning = [delta["reasoning"] for delta in deltas(tape) if delta.get("reasoning")]
    assert reasoning
    assert thoughts_of(events) == reasoning
    assert text_of(events) == written(tape)
    assert written(tape)
    assert thought_before_text(events)
    usage = usage_of(tape)
    # Its count of the answer holds the reasoning already.
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]
    cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
    assert events[-1] == Finish(
        FinishReason.STOP,
        Usage(usage["prompt_tokens"], usage["completion_tokens"], cached),
        Timings(
            load_ms=0,
            prompt_ms=round(usage["prompt_time"] * 1000),
            generation_ms=round(usage["completion_time"] * 1000),
            total_ms=round((usage["queue_time"] + usage["total_time"]) * 1000),
        ),
    )


async def test_groq_refuses_to_be_told_not_to_reason(cloud_with: Any) -> None:
    """The client's own word for no reasoning, "none", which a model on Groq is sent
    unless its `thinking_off` says otherwise."""
    groq, tape = cloud_with("groq", "reasoning_refused")

    with pytest.raises(RequestRejectedError) as caught:
        await collect(groq.stream(ask(LARGE, thinking=False)))

    assert type(caught.value) is RequestRejectedError
    assert str(caught.value) == f"groq answered HTTP 400: {tape.body['error']['message']}"
    assert "reasoning_effort" in str(caught.value)


async def test_groq_calls_a_tool(cloud_with: Any) -> None:
    groq, tape = cloud_with("groq", "tool_call", thinking_off=LEAST_REASONING)
    request = ask(LARGE, BOTH_CITIES, briefly=False, tools=[WEATHER], thinking=False)
    events = await collect(groq.stream(request))

    calls = [event for event in events if isinstance(event, ToolCall)]
    assert calls
    assert calls == calls_in(tape)
    assert all(isinstance(part.get("index"), int) for part in fragments_of(tape))
    assert finish_reason_of(tape) == "tool_calls"
    finish = events[-1]
    assert isinstance(finish, Finish)
    assert finish.reason is FinishReason.TOOL_CALLS


async def test_groq_says_that_it_has_no_such_model(cloud_with: Any) -> None:
    groq, tape = cloud_with("groq", "unknown_model")

    with pytest.raises(ModelNotFoundError) as caught:
        await collect(groq.stream(ask("openai/gpt-oss-999b")))

    assert caught.value.model == "openai/gpt-oss-999b"
    assert tape.body["error"]["code"] == "model_not_found"


async def test_groq_refuses_a_made_up_key(cloud_with: Any) -> None:
    groq, tape = cloud_with("groq", "key_refused", key=MADE_UP_KEY)

    with pytest.raises(ProviderError) as caught:
        await collect(groq.stream(ask(LARGE)))

    assert type(caught.value) is ProviderError
    message = tape.body["error"]["message"]
    assert (
        str(caught.value) == f"groq refused Actio's credentials: groq answered HTTP 401: {message}"
    )


async def test_groq_lists_its_models(cloud_with: Any) -> None:
    groq, tape = cloud_with("groq", "models")

    models = await listed(groq)

    assert models == [entry["id"] for entry in tape.body["data"]]
    assert {LARGE, SMALL} <= set(models)


async def test_groq_refuses_a_request_larger_than_the_account_takes_in_a_minute(
    cloud_with: Any,
) -> None:
    """Over the tokens a minute of the free plan, 8,000, in one request: 413, which no
    wait changes, whatever its `Retry-After` says. Asked once with a long prompt; the
    cassette's request is a short one."""
    groq, tape = cloud_with(
        "groq",
        "too_large",
        replay_only="it depends on the account's state, a request over its tokens a minute",
        thinking_off=LEAST_REASONING,
    )

    with pytest.raises(ContextOverflowError) as caught:
        await collect(groq.stream(ask(LARGE, thinking=False, max_output_tokens=16)))

    assert type(caught.value) is ContextOverflowError
    assert tape.body["error"]["message"] in str(caught.value)


async def test_groq_says_how_long_to_wait_at_its_rate_limit(cloud_with: Any) -> None:
    groq, _ = cloud_with(
        "groq",
        "rate_limited",
        replay_only="it depends on the account's state, its tokens a minute used up",
        thinking_off=LEAST_REASONING,
    )

    with pytest.raises(RateLimitedError) as caught:
        await collect(groq.stream(ask(SMALL, thinking=False, max_output_tokens=64)))

    assert caught.value.retry_after_s == 2.0  # its `Retry-After`


# --- The Gemini API --------------------------------------------------------------------


async def test_gemini_answers(cloud_with: Any) -> None:
    gemini, tape = cloud_with("gemini", "answer")
    events = await collect(gemini.stream(ask(LITE)))

    assert written(tape)
    assert text_of(events) == written(tape)
    assert thoughts_of(events) == []
    usage = usage_of(tape)
    # This model doesn't reason unless it is told to.
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]
    assert events[-1] == Finish(
        FinishReason.STOP, Usage(usage["prompt_tokens"], usage["completion_tokens"])
    )


async def test_gemini_counts_the_reasoning_it_doesnt_show_in_its_total_alone(
    cloud_with: Any,
) -> None:
    """Asked for reasoning, and told nothing more: it reasons, says nothing of it, and
    leaves it out of `completion_tokens`. The output is counted whole all the same."""
    gemini, tape = cloud_with("gemini", "reasoning_unseen")
    events = await collect(gemini.stream(ask(FLASH, TRAIN, thinking=True, max_output_tokens=2048)))

    usage = usage_of(tape)
    unnamed = usage["total_tokens"] - usage["prompt_tokens"] - usage["completion_tokens"]
    assert unnamed > 0
    assert thoughts_of(events) == []
    assert text_of(events) == written(tape)
    assert events[-1] == Finish(
        FinishReason.STOP, Usage(usage["prompt_tokens"], usage["completion_tokens"] + unnamed)
    )


async def test_gemini_reasons_not_at_all_when_told_none(cloud_with: Any) -> None:
    """The client's own word for no reasoning, which this model takes."""
    gemini, tape = cloud_with("gemini", "reasoning_off")
    events = await collect(gemini.stream(ask(FLASH, TRAIN, thinking=False)))

    usage = usage_of(tape)
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]
    assert text_of(events) == written(tape)
    assert events[-1] == Finish(
        FinishReason.STOP, Usage(usage["prompt_tokens"], usage["completion_tokens"])
    )


async def test_gemini_sends_its_thoughts_inside_the_text_when_asked_to(cloud_with: Any) -> None:
    gemini, tape = cloud_with("gemini", "thoughts", thinking_on=SHOW_THOUGHTS)
    request = ask(LITE, SEATING, briefly=False, thinking=True, max_output_tokens=4096)
    events = await collect(gemini.stream(request))

    thoughts = [delta["content"] for delta in deltas(tape) if is_thought(delta)]
    assert len(thoughts) > 1  # in several pieces
    assert thoughts[0].startswith("<thought>")
    assert written(tape).startswith("</thought>")
    assert thoughts_of(events) == [
        thought for each in thoughts if (thought := each.removeprefix("<thought>"))
    ]
    assert text_of(events) == written(tape).removeprefix("</thought>")
    assert thought_before_text(events)
    usage = usage_of(tape)
    assert usage["total_tokens"] - usage["prompt_tokens"] > usage["completion_tokens"]
    assert events[-1] == Finish(
        FinishReason.STOP,
        Usage(usage["prompt_tokens"], usage["total_tokens"] - usage["prompt_tokens"]),
    )


async def test_gemini_breaks_off_a_stream_with_an_error_that_is_no_event(cloud_with: Any) -> None:
    """Too busy half-way: after three thoughts, its refusal as plain lines of JSON, no
    `data:` line among them, and then the end, with no finish and no `[DONE]`."""
    gemini, tape = cloud_with(
        "gemini",
        "thoughts_broken_off",
        replay_only="it depends on the provider's state, a model too busy half-way",
        thinking_on=SHOW_THOUGHTS,
    )
    received: list[StreamEvent] = []

    async def read() -> None:
        request = ask(LITE, SEATING, briefly=False, thinking=True, max_output_tokens=4096)
        async with asyncio.timeout(WAITS_AT_MOST_S):
            async for event in gemini.stream(request):
                received.append(event)  # noqa: PERF401 - the list must outlive the exception

    with pytest.raises(StreamInterruptedError) as caught:
        await read()

    thoughts = [delta["content"] for delta in deltas(tape) if is_thought(delta)]
    assert len(thoughts) == 3
    assert received == [ReasoningDelta(thought.removeprefix("<thought>")) for thought in thoughts]
    [refusal] = json.loads(tape.rest)
    assert refusal["error"]["code"] == 503
    message = refusal["error"]["message"]
    assert str(caught.value) == f"gemini's answer ended before it finished: {message}"


async def test_gemini_calls_two_tools_at_once_without_an_index(cloud_with: Any) -> None:
    gemini, tape = cloud_with("gemini", "tool_calls")
    events = await collect(gemini.stream(ask(LITE, BOTH_CITIES, briefly=False, tools=[WEATHER])))

    calls = [event for event in events if isinstance(event, ToolCall)]
    assert not any("index" in part for part in fragments_of(tape))
    assert len(calls) == 2
    assert len({call.id for call in calls}) == 2
    assert calls == calls_in(tape)
    finish = events[-1]
    assert isinstance(finish, Finish)
    assert finish.reason is FinishReason.TOOL_CALLS  # whatever it says: "stop"


async def test_gemini_says_that_it_has_no_such_model(cloud_with: Any) -> None:
    gemini, tape = cloud_with("gemini", "unknown_model")

    with pytest.raises(ModelNotFoundError) as caught:
        await collect(gemini.stream(ask("gemini-9.9-nothing")))

    [refusal] = tape.body  # a list of one
    assert refusal["error"]["status"] == "NOT_FOUND"
    assert caught.value.model == "gemini-9.9-nothing"
    assert str(caught.value) == f"gemini answered HTTP 404: {refusal['error']['message']}"


async def test_gemini_refuses_a_made_up_key_with_400(cloud_with: Any) -> None:
    gemini, tape = cloud_with("gemini", "key_refused", key=MADE_UP_KEY)

    with pytest.raises(ProviderError) as caught:
        await collect(gemini.stream(ask(LITE)))

    [refusal] = tape.body
    message = refusal["error"]["message"]
    assert type(caught.value) is ProviderError
    assert (
        str(caught.value)
        == f"gemini refused Actio's credentials: gemini answered HTTP 400: {message}"
    )


async def test_gemini_refuses_a_made_up_key_its_list_of_models_too(cloud_with: Any) -> None:
    gemini, tape = cloud_with("gemini", "models_key_refused", key=MADE_UP_KEY)

    with pytest.raises(ProviderError) as caught:
        await listed(gemini)

    assert type(caught.value) is ProviderError
    assert tape.body["error"]["message"] in str(caught.value)  # an object, not a list


async def test_gemini_lists_its_models_by_their_paths(cloud_with: Any) -> None:
    gemini, tape = cloud_with("gemini", "models")

    models = await listed(gemini)

    ids = [entry["id"] for entry in tape.body["data"]]
    assert all(each.startswith("models/") for each in ids)
    # Each as it is written, and by the name its chat takes.
    assert models == [spelling for each in ids for spelling in (each, each.removeprefix("models/"))]
    assert {FLASH, LITE, f"models/{FLASH}", f"models/{LITE}"} <= set(models)


async def test_gemini_says_how_long_to_wait_for_a_spent_quota_in_its_body(
    cloud_with: Any,
) -> None:
    """Its answers carry no `Retry-After`. Seen for a model the free tier doesn't
    include, whose limits are 0."""
    gemini, tape = cloud_with(
        "gemini",
        "quota_spent",
        replay_only="it depends on the account's state, a model beyond its free tier",
    )

    with pytest.raises(RateLimitedError) as caught:
        await collect(gemini.stream(ask("gemini-3.1-pro-preview", max_output_tokens=16)))

    [refusal] = tape.body
    assert refusal["error"]["status"] == "RESOURCE_EXHAUSTED"
    assert caught.value.retry_after_s == 83280.0


async def test_gemini_says_when_a_model_is_too_busy(cloud_with: Any) -> None:
    gemini, tape = cloud_with(
        "gemini",
        "overloaded",
        replay_only="it depends on the provider's state, a model too busy to answer",
    )

    with pytest.raises(ProviderUnavailableError) as caught:
        await collect(gemini.stream(ask("gemini-3.8-flash")))

    [refusal] = tape.body
    assert type(caught.value) is ProviderUnavailableError
    assert str(caught.value) == f"gemini answered HTTP 503: {refusal['error']['message']}"


# --- What a cassette may hold ----------------------------------------------------------

ORGANIZATION = "org_00000000000000000000000000"
"""What a cassette holds in place of the account's organization, which Groq names."""
SIGNATURE = "c2lnbmF0dXJl"
"""What a cassette holds in place of a thought signature."""
REQUEST = "req_00000000000000000000000000"
"""What a cassette holds in place of Groq's id of a request."""
COMPLETION = "chatcmpl-00000000-0000-0000-0000-000000000000"
"""What a cassette holds in place of Groq's id of a chat completion."""


def test_what_is_saved_of_an_answer_names_no_account_and_no_request(tidied: Any) -> None:
    """The ids and the signature are made up here, shaped as the servers' were. The
    check of the cassettes below sees only what was saved already."""
    chunk = {
        "id": "chatcmpl-6a1f0c2e-93b4-4d1a-8e2f-7c5b9a0d3e41",
        "choices": [{"index": 0, "delta": {"content": "Paris."}, "finish_reason": None}],
        "x_groq": {"id": "req_01kmadeupforthesetests0000"},
    }
    signed = {
        "choices": [
            {
                "index": 0,
                "delta": {"extra_content": {"google": {"thought_signature": "Q2lj" * 100}}},
                "finish_reason": "stop",
            }
        ]
    }
    refusal = {
        "error": {"message": "Limit reached in organization `org_01kmadeup0000000000000000`."}
    }
    body = f"data: {json.dumps(chunk)}\n\ndata: {json.dumps(signed)}\n\n"

    answer = tidied("/openai/v1/chat/completions", 200, body)
    refused = tidied("/openai/v1/chat/completions", 429, json.dumps(refusal))

    for made_up in ("6a1f0c2e", "req_01kmadeup", "Q2lj", "org_01kmadeup"):
        assert made_up not in answer + refused
    assert COMPLETION in answer
    assert REQUEST in answer
    assert f'"thought_signature": "{SIGNATURE}"' in answer
    assert ORGANIZATION in refused
    assert "Paris." in answer


def test_what_is_saved_of_a_list_of_models_is_their_ids_alone(tidied: Any) -> None:
    listing = {
        "object": "list",
        "data": [
            {"id": "openai/gpt-oss-20b", "owned_by": "OpenAI", "context_window": 131072},
            {"id": "models/gemini-3.6-flash", "display_name": "Gemini 3.6 Flash"},
        ],
    }

    saved = tidied("/v1beta/openai/models", 200, json.dumps(listing))

    assert json.loads(saved) == {
        "data": [{"id": "openai/gpt-oss-20b"}, {"id": "models/gemini-3.6-flash"}]
    }


async def test_a_replay_fails_when_the_request_is_not_the_recorded_one(cloud_with: Any) -> None:
    groq, _ = cloud_with(
        "groq",
        "answer",
        replay_only="a replay is checked against its cassette's request, and live is no replay",
        thinking_off=LEAST_REASONING,
    )

    with pytest.raises(AssertionError, match="no longer sends the request recorded in answer"):
        await collect(groq.stream(ask(LARGE, "What is the capital of Italy?", thinking=False)))


def strings_in(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, inner in value.items():
            yield key
            yield from strings_in(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from strings_in(inner)


def names_in(value: Any) -> Iterator[str]:
    """The name of every member of what a cassette holds, at any depth: a header kept
    under any of them is among them."""
    if isinstance(value, dict):
        for key, inner in value.items():
            yield key
            yield from names_in(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from names_in(inner)


def test_no_cassette_names_the_account_or_holds_a_key() -> None:
    """The cassettes are public. Each is tidied as it is written; this is the check."""
    faults = []
    for cassette in sorted(FIXTURES.glob("*/*.json")):
        recorded = json.loads(cassette.read_text())
        text = "\n".join(strings_in(recorded))
        name = f"{cassette.parent.name}/{cassette.name}"
        faults += [f"{name}: {org}" for org in re.findall(r"org_\w*", text) if org != ORGANIZATION]
        faults += [
            f"{name}: a thought signature of {len(signature)} characters"
            for signature in re.findall(r'"thought_signature"\s*:\s*"([^"]*)"', text)
            if len(signature) > len(SIGNATURE)
        ]
        headers = list(names_in(recorded))
        faults += [
            f"{name}: a header {each}" for each in headers if each.lower() == "authorization"
        ]

    assert faults == []
