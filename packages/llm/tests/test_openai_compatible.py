"""The OpenAI-compatible adapter.

The first half talks to fakellm, in process. fakellm is an independent implementation of
the wire format, itself checked with OpenAI's official SDK, so the two keep each other
honest. Directives in the prompt, such as `[[think: ...]]`, script what it answers.

The second half feeds the adapter responses written here: the ways real servers differ
from one another, which one fake shouldn't try to imitate all at once.
"""

import copy
import json
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import aclosing
from typing import Any

import httpx2
import pytest

from actio_fakellm.app import create_app
from actio_llm import (
    Assistant,
    ChatRequest,
    ConnectionFailedError,
    ContextOverflowError,
    Finish,
    FinishReason,
    LLMError,
    ModelNotFoundError,
    ProviderError,
    ProviderUnavailableError,
    RateLimitedError,
    ReasoningDelta,
    RequestRejectedError,
    StreamEvent,
    StreamInterruptedError,
    System,
    TextDelta,
    Timeouts,
    Timings,
    ToolCall,
    ToolResult,
    ToolSpec,
    Usage,
    User,
)
from actio_llm.openai_compatible import (
    _STRAY_LINES,
    OWN_MEMBERS,
    OpenAICompatible,
    _error_parts,
    _Parser,
)

pytestmark = pytest.mark.anyio

WEATHER = ToolSpec(
    name="get_weather",
    description="Get the current weather for a city.",
    parameters={"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
)


def ask(prompt: str, **options: Any) -> ChatRequest:
    return ChatRequest(model="fake-chat", messages=[User(prompt)], **options)


async def collect(stream: AsyncGenerator[StreamEvent]) -> list[StreamEvent]:
    async with aclosing(stream) as events:
        return [event async for event in events]


def text_of(events: list[StreamEvent]) -> str:
    return "".join(event.text for event in events if isinstance(event, TextDelta))


# --- Against fakellm -----------------------------------------------------------------

type Fake = Callable[..., OpenAICompatible]


@pytest.fixture
async def fake() -> AsyncIterator[Fake]:
    """Call it to get an adapter talking to a fakellm server, in process."""
    clients: list[httpx2.AsyncClient] = []

    def start(**options: Any) -> OpenAICompatible:
        client = httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=create_app()), base_url="http://fakellm/v1"
        )
        clients.append(client)
        return OpenAICompatible(client, provider="fakellm", **options)

    yield start
    for client in clients:
        await client.aclose()


async def test_streams_the_answer_then_one_finish_with_token_counts(fake: Fake) -> None:
    events = await collect(fake().stream(ask("hello there")))

    assert events == [
        TextDelta("Echo: "),
        TextDelta("hello "),
        TextDelta("there"),
        Finish(FinishReason.STOP, Usage(input_tokens=2, output_tokens=3)),
    ]


async def test_max_output_tokens_cuts_the_answer(fake: Fake) -> None:
    events = await collect(fake().stream(ask("hello there", max_output_tokens=2)))

    assert text_of(events) == "Echo: hello "
    assert events[-1] == Finish(FinishReason.LENGTH, Usage(input_tokens=2, output_tokens=2))


async def test_a_server_that_only_knows_max_tokens_can_be_served(fake: Fake) -> None:
    old_style = fake(output_limit_field="max_tokens")
    events = await collect(old_style.stream(ask("hello there", max_output_tokens=1)))

    assert text_of(events) == "Echo: "


async def test_reasoning_streams_before_the_answer(fake: Fake) -> None:
    events = await collect(fake().stream(ask("[[think: two plus two]] What is 2+2?")))

    assert events[:5] == [
        ReasoningDelta("two "),
        ReasoningDelta("plus "),
        ReasoningDelta("two"),
        TextDelta("Echo: "),
        TextDelta("What "),
    ]
    finish = events[-1]
    assert isinstance(finish, Finish)
    assert finish.usage is not None
    assert finish.usage.output_tokens == 3 + 4  # reasoning counts as output


async def test_a_tool_call_is_put_together_from_its_fragments(fake: Fake) -> None:
    """fakellm streams the arguments 12 characters at a time, as OpenAI's format allows."""
    prompt = '[[call: get_weather {"city": "Izmir", "unit": "celsius"}]] Weather in Izmir?'
    events = await collect(fake().stream(ask(prompt, tools=[WEATHER])))

    assert events[0] == ToolCall(
        "call_fake_000001", "get_weather", {"city": "Izmir", "unit": "celsius"}
    )
    finish = events[-1]
    assert isinstance(finish, Finish)
    assert finish.reason is FinishReason.TOOL_CALLS
    assert len(events) == 2


async def test_several_tool_calls_arrive_in_order(fake: Fake) -> None:
    prompt = '[[call: get_weather {"city": "Izmir"}]] [[call: get_time]] Both, please.'
    events = await collect(fake().stream(ask(prompt, tools=[WEATHER])))

    assert events[:2] == [
        ToolCall("call_fake_000001", "get_weather", {"city": "Izmir"}),
        ToolCall("call_fake_000002", "get_time", {}),
    ]


async def test_a_tool_result_goes_back_as_history(fake: Fake) -> None:
    call = ToolCall("call_fake_000001", "get_weather", {"city": "Izmir"})
    request = ChatRequest(
        model="fake-chat",
        messages=[
            User('[[call: get_weather {"city": "Izmir"}]] Weather in Izmir?'),
            Assistant(tool_calls=(call,)),
            ToolResult(call.id, call.name, "18 degrees"),
        ],
        tools=[WEATHER],
    )
    events = await collect(fake().stream(request))

    assert text_of(events) == "Echo: 18 degrees"  # the next step answers; it doesn't call again
    # 7 words in the user's message and 2 in the tool's result: fakellm counts words.
    assert events[-1] == Finish(FinishReason.STOP, Usage(input_tokens=9, output_tokens=3))


async def test_an_unknown_model_is_reported_as_such(fake: Fake) -> None:
    request = ChatRequest(model="gpt-9", messages=[User("hello")])
    with pytest.raises(ModelNotFoundError, match="does not exist") as caught:
        await collect(fake().stream(request))

    assert caught.value.model == "gpt-9"


@pytest.mark.parametrize(
    ("directive", "error"),
    [
        ("[[fail: 503]]", ProviderUnavailableError),
        ("[[fail: 500]]", ProviderError),
        ("[[fail: 401]]", ProviderError),
        ("[[fail: 400]]", RequestRejectedError),
        ("[[fail: 400 context_length_exceeded]]", ContextOverflowError),
    ],
)
async def test_http_errors_become_typed_errors(
    fake: Fake, directive: str, error: type[Exception]
) -> None:
    with pytest.raises(error, match="fakellm answered HTTP") as caught:
        await collect(fake().stream(ask(f"{directive} hello")))

    assert type(caught.value) is error


async def test_a_rate_limit_says_how_long_to_wait(fake: Fake) -> None:
    with pytest.raises(RateLimitedError) as caught:
        await collect(fake().stream(ask("[[fail: 429]] hello")))

    assert caught.value.retry_after_s == 1.0
    assert isinstance(caught.value, ProviderUnavailableError)  # so a router can fall back


async def test_an_answer_that_ends_early_is_an_interruption(fake: Fake) -> None:
    received: list[StreamEvent] = []
    stream = fake().stream(ask("[[drop: 2]] hello there"))

    async def read() -> None:
        async for event in stream:
            received.append(event)  # noqa: PERF401 - the list must outlive the exception

    with pytest.raises(StreamInterruptedError, match="ended before it finished"):
        await read()

    assert received == [TextDelta("Echo: "), TextDelta("hello ")]


async def test_a_server_that_is_too_slow_to_start_is_unavailable(fake: Fake) -> None:
    impatient = fake(timeouts=Timeouts(first_event_s=0.05))
    with pytest.raises(ProviderUnavailableError, match=r"fakellm didn't start answering"):
        await collect(impatient.stream(ask("[[delay: 2]] hello")))


async def test_models_lists_what_the_server_offers(fake: Fake) -> None:
    assert await fake().models() == ["fake-chat", "fake-quiet"]


# --- Responses written here ----------------------------------------------------------

type Handler = Callable[[httpx2.Request], httpx2.Response]
type Serve = Callable[..., OpenAICompatible]

STOP = {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}


def delta(**fields: Any) -> dict[str, Any]:
    return {"choices": [{"index": 0, "delta": fields, "finish_reason": None}]}


def sse(*events: dict[str, Any] | str) -> bytes:
    lines = [e if isinstance(e, str) else f"data: {json.dumps(e)}" for e in events]
    return "".join(f"{line}\n\n" for line in lines).encode()


def gemini_error(code: int, message: str, status: str) -> dict[str, Any]:
    """An error as the Gemini API writes one: its code a number, and its name in
    `status`. Its chat answers with a list of one such object."""
    return {"error": {"code": code, "message": message, "status": status}}


KEY_REFUSED = "Please pass a valid API key"
INVALID = "Request contains an invalid argument."
TOO_LARGE = {
    "error": {
        "message": "Request too large for model `openai/gpt-oss-120b` on tokens per minute "
        "(TPM): Limit 8000, Requested 9851, please reduce your message size and try again.",
        "type": "tokens",
        "code": "rate_limit_exceeded",
    }
}


@pytest.fixture
async def serve() -> AsyncIterator[Serve]:
    """Call with a handler, or with the body of a 200 response, to get an adapter."""
    clients: list[httpx2.AsyncClient] = []

    def start(answer: Handler | bytes, **options: Any) -> OpenAICompatible:
        def fixed(_request: httpx2.Request) -> httpx2.Response:
            assert isinstance(answer, bytes)
            return httpx2.Response(200, content=answer)

        handler = fixed if isinstance(answer, bytes) else answer
        client = httpx2.AsyncClient(
            base_url="http://provider.test/v1", transport=httpx2.MockTransport(handler)
        )
        clients.append(client)
        return OpenAICompatible(client, provider="Example", **options)

    yield start
    for client in clients:
        await client.aclose()


async def test_a_request_becomes_openais_json(serve: Serve) -> None:
    sent: list[tuple[str, dict[str, Any]]] = []

    def chat(request: httpx2.Request) -> httpx2.Response:
        sent.append((str(request.url), json.loads(request.content)))
        return httpx2.Response(200, content=sse(STOP, "data: [DONE]"))

    call = ToolCall("call_1", "get_weather", {"city": "Izmir"})
    request = ChatRequest(
        model="some-model",
        messages=[
            System("Be brief."),
            User("Weather in Izmir?"),
            Assistant(tool_calls=(call,), reasoning="I should look it up."),
            ToolResult("call_1", "get_weather", "18 degrees"),
            Assistant("It is 18 degrees."),
        ],
        tools=[WEATHER],
        max_output_tokens=512,
        context_window=8192,
        thinking=False,
        temperature=0.2,
        top_p=0.9,
        seed=3,
        stop=["END"],
    )
    await collect(serve(chat).stream(request))

    assert sent == [
        (
            "http://provider.test/v1/chat/completions",
            {
                "model": "some-model",
                "messages": [
                    {"role": "system", "content": "Be brief."},
                    {"role": "user", "content": "Weather in Izmir?"},
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": '{"city": "Izmir"}',
                                },
                            }
                        ],
                    },
                    {"role": "tool", "tool_call_id": "call_1", "content": "18 degrees"},
                    {"role": "assistant", "content": "It is 18 degrees."},
                ],
                "stream": True,
                "stream_options": {"include_usage": True},
                "max_completion_tokens": 512,
                "temperature": 0.2,
                "top_p": 0.9,
                "seed": 3,
                "stop": ["END"],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "description": "Get the current weather for a city.",
                            "parameters": dict(WEATHER.parameters),
                        },
                    }
                ],
                "reasoning_effort": "none",  # thinking=False; the context window isn't sent
            },
        )
    ]


@pytest.mark.parametrize(
    ("thinking", "sent"), [(None, None), (True, None), (False, "none"), ("low", "low")]
)
async def test_thinking_becomes_reasoning_effort(
    serve: Serve, thinking: bool | str | None, sent: str | None
) -> None:
    bodies: list[dict[str, Any]] = []

    def chat(request: httpx2.Request) -> httpx2.Response:
        bodies.append(json.loads(request.content))
        return httpx2.Response(200, content=sse(STOP))

    await collect(serve(chat).stream(ask("hi", thinking=thinking)))

    assert bodies[0].get("reasoning_effort") == sent


SHOW_THOUGHTS = {"extra_body": {"google": {"thinking_config": {"include_thoughts": True}}}}


@pytest.mark.parametrize(
    ("options", "thinking", "members"),
    [
        # A model that always reasons, and can be told no less than "low".
        ({"thinking_off": {"reasoning_effort": "low"}}, False, {"reasoning_effort": "low"}),
        ({"thinking_off": {"reasoning_effort": "low"}}, True, {}),
        # A model whose thoughts are sent only when asked for.
        ({"thinking_on": SHOW_THOUGHTS}, True, SHOW_THOUGHTS),
        ({"thinking_on": SHOW_THOUGHTS}, False, {"reasoning_effort": "none"}),
        # Told nothing when it is off: an empty mapping isn't one left out.
        ({"thinking_off": {}}, False, {}),
        # A level, or nothing asked: as without the two.
        (
            {"thinking_off": {"reasoning_effort": "low"}, "thinking_on": SHOW_THOUGHTS},
            "high",
            {"reasoning_effort": "high"},
        ),
        ({"thinking_off": {"reasoning_effort": "low"}, "thinking_on": SHOW_THOUGHTS}, None, {}),
    ],
)
async def test_what_a_model_is_told_about_reasoning_is_the_callers_to_say(
    serve: Serve, options: dict[str, Any], thinking: bool | str | None, members: dict[str, Any]
) -> None:
    bodies: list[dict[str, Any]] = []

    def chat(request: httpx2.Request) -> httpx2.Response:
        bodies.append(json.loads(request.content))
        return httpx2.Response(200, content=sse(STOP))

    await collect(serve(chat, **options).stream(ask("hi", thinking=thinking)))

    [body] = bodies
    assert {name: value for name, value in body.items() if name not in OWN_MEMBERS} == members


async def test_what_a_model_is_told_about_reasoning_is_copied_and_never_shared(
    serve: Serve,
) -> None:
    bodies: list[dict[str, Any]] = []

    def chat(request: httpx2.Request) -> httpx2.Response:
        bodies.append(json.loads(request.content))
        return httpx2.Response(200, content=sse(STOP))

    told = copy.deepcopy(SHOW_THOUGHTS)
    adapter = serve(chat, thinking_on=told)
    told["extra_body"]["google"]["thinking_config"]["include_thoughts"] = False  # its own
    await collect(adapter.stream(ask("hi", thinking=True)))
    first, second = (adapter._body(ask("hi", thinking=True)) for _ in range(2))

    assert bodies[0]["extra_body"] == SHOW_THOUGHTS["extra_body"]
    nested = [body["extra_body"]["google"]["thinking_config"] for body in (first, second)]
    assert nested[0] == nested[1]
    assert nested[0] is not nested[1]


@pytest.mark.parametrize("option", ["thinking_off", "thinking_on"])
@pytest.mark.parametrize("member", sorted(OWN_MEMBERS))
async def test_what_a_model_is_told_about_reasoning_cant_be_what_the_client_writes(
    serve: Serve, option: str, member: str
) -> None:
    with pytest.raises(ValueError, match=f"{option} can't hold '{member}'"):
        serve(b"", **{option: {"reasoning_effort": "low", member: "anything"}})


@pytest.mark.parametrize("output_limit_field", ["max_completion_tokens", "max_tokens"])
async def test_every_member_the_client_writes_is_one_of_its_own(
    serve: Serve, output_limit_field: str
) -> None:
    """So that what a model is told about reasoning never overwrites one of them."""
    bodies: list[dict[str, Any]] = []

    def chat(request: httpx2.Request) -> httpx2.Response:
        bodies.append(json.loads(request.content))
        return httpx2.Response(200, content=sse(STOP))

    request = ask(
        "hi",
        tools=[WEATHER],
        max_output_tokens=512,
        temperature=0.2,
        top_p=0.9,
        seed=3,
        stop=["END"],
        thinking="low",
    )
    await collect(serve(chat, output_limit_field=output_limit_field).stream(request))

    [body] = bodies
    assert set(body) - OWN_MEMBERS == {"reasoning_effort"}  # the level, as it was asked


async def test_reasoning_is_read_under_its_older_name_too(serve: Serve) -> None:
    body = sse(delta(reasoning_content="Hmm."), delta(content="Yes."), STOP, "data: [DONE]")
    events = await collect(serve(body).stream(ask("hi")))

    assert events[:2] == [ReasoningDelta("Hmm."), TextDelta("Yes.")]


def thought(content: str) -> dict[str, Any]:
    """A piece of a thought, sent inside the text as the Gemini API sends it."""
    return delta(content=content, extra_content={"google": {"thought": True}})


SIGNED = {
    "choices": [
        {
            "index": 0,
            "delta": {"extra_content": {"google": {"thought_signature": "c2lnbmF0dXJl"}}},
            "finish_reason": "stop",
        }
    ]
}


async def test_thoughts_sent_inside_the_text_are_reasoning(serve: Serve) -> None:
    body = sse(
        thought("<thought>**Seating them**\n\nEli is at an end."),
        thought("**Checking**\n\nDua sits next to Eli."),
        thought("**Done**\n\nIt holds.\n\n"),
        delta(content="</thought>Eli, Dua, Ada,"),
        delta(content=" Ben, Cem."),
        SIGNED,
        "data: [DONE]",
    )
    events = await collect(serve(body).stream(ask("hi", thinking=True)))

    assert events == [
        ReasoningDelta("**Seating them**\n\nEli is at an end."),
        ReasoningDelta("**Checking**\n\nDua sits next to Eli."),
        ReasoningDelta("**Done**\n\nIt holds.\n\n"),
        TextDelta("Eli, Dua, Ada,"),
        TextDelta(" Ben, Cem."),
        Finish(FinishReason.STOP, None),
    ]


async def test_the_thought_tags_are_taken_off_where_they_open_and_close_and_nowhere_else(
    serve: Serve,
) -> None:
    """An answer may write the tags itself. Only the start of a thought loses its
    opening tag, and only the first text after thoughts its closing one."""
    body = sse(
        thought("<thought>Say how a <thought> is written."),
        delta(content="</thought></thought> closes one,"),
        delta(content="</thought> too."),
        STOP,
    )
    events = await collect(serve(body).stream(ask("hi", thinking=True)))

    assert events[:3] == [
        ReasoningDelta("Say how a <thought> is written."),
        TextDelta("</thought> closes one,"),
        TextDelta("</thought> too."),
    ]


async def test_an_answer_without_thoughts_keeps_the_tags_it_writes(serve: Serve) -> None:
    body = sse(
        delta(content="</thought> closes a thought,"),
        delta(content=" and <thought> opens one."),
        STOP,
    )
    events = await collect(serve(body).stream(ask("hi")))

    assert events[:2] == [
        TextDelta("</thought> closes a thought,"),
        TextDelta(" and <thought> opens one."),
    ]


async def test_a_piece_marked_otherwise_than_as_a_thought_stays_text(serve: Serve) -> None:
    """Only `"thought": true` makes a piece a thought. The Gemini API marks other pieces
    under the same member, such as one with a thought's signature beside its text."""
    signed = delta(content="Paris", extra_content={"google": {"thought_signature": "c2ln"}})
    not_a_thought = delta(content=" it is.", extra_content={"google": {"thought": False}})
    events = await collect(serve(sse(signed, not_a_thought, STOP)).stream(ask("hi")))

    assert events == [TextDelta("Paris"), TextDelta(" it is."), Finish(FinishReason.STOP, None)]


async def test_token_counts_include_the_cached_part(serve: Serve) -> None:
    usage = {
        "choices": [],
        "usage": {
            "prompt_tokens": 1200,
            "completion_tokens": 40,
            "total_tokens": 1240,
            "prompt_tokens_details": {"cached_tokens": 1024},
        },
    }
    events = await collect(
        serve(sse(delta(content="ok"), STOP, usage, "data: [DONE]")).stream(ask("hi"))
    )

    assert events[-1] == Finish(
        FinishReason.STOP, Usage(input_tokens=1200, output_tokens=40, cached_input_tokens=1024)
    )


@pytest.mark.parametrize(
    ("counted", "output"),
    [
        # The Gemini API's: `completion_tokens` leaves out the 120 tokens of reasoning
        # that `total_tokens` holds.
        ({"prompt_tokens": 24, "completion_tokens": 7, "total_tokens": 151}, 127),
        # Groq's, and OpenAI's: the reasoning is part of `completion_tokens` already.
        ({"prompt_tokens": 96, "completion_tokens": 40, "total_tokens": 136}, 40),
        ({"prompt_tokens": 96, "completion_tokens": 40}, 40),
        ({"prompt_tokens": 96, "completion_tokens": 40, "total_tokens": 100}, 40),
        ({"prompt_tokens": 96, "completion_tokens": 40, "total_tokens": "151"}, 40),
    ],
)
async def test_output_tokens_are_counted_whole(
    serve: Serve, counted: dict[str, Any], output: int
) -> None:
    usage = {"choices": [], "usage": counted}
    events = await collect(serve(sse(delta(content="ok"), STOP, usage)).stream(ask("hi")))

    assert events[-1] == Finish(
        FinishReason.STOP, Usage(input_tokens=counted["prompt_tokens"], output_tokens=output)
    )


# Groq's, in seconds: 40 tokens written in 0.083 s, after 0.28 s in its queue.
TIMES = {
    "queue_time": 0.281741382,
    "prompt_time": 0.003890232,
    "completion_time": 0.0828675,
    "total_time": 0.086757732,
}


async def test_where_the_time_went_is_read_when_the_usage_says_it(serve: Serve) -> None:
    usage = {"choices": [], "usage": {"prompt_tokens": 96, "completion_tokens": 40, **TIMES}}
    events = await collect(serve(sse(delta(content="ok"), STOP, usage)).stream(ask("hi")))

    finish = events[-1]
    assert isinstance(finish, Finish)
    assert finish.timings == Timings(load_ms=0, prompt_ms=4, generation_ms=83, total_ms=368)
    assert finish.tokens_per_second == pytest.approx(40 / 0.083)


@pytest.mark.parametrize(
    ("times", "timings"),
    [
        ({}, None),  # nothing said of it, as most servers do
        ({"completion_time": 0.5}, Timings(0, 0, 500, 0)),  # the others count 0
        ({"completion_time": 0.0004, "total_time": 1.2}, Timings(0, 0, 0, 1200)),
        (TIMES | {"queue_time": -0.1}, None),
        (TIMES | {"queue_time": -0.001}, None),
        (TIMES | {"prompt_time": "0.003"}, None),
        (TIMES | {"completion_time": None}, None),
        (TIMES | {"total_time": True}, None),
        # `NaN` and `Infinity`, as JSON reads them.
        (TIMES | {"completion_time": float("nan")}, None),
        (TIMES | {"queue_time": float("inf")}, None),
        # Too long to be counted in milliseconds without an error, or to be held.
        (TIMES | {"completion_time": 1e306}, None),
        (TIMES | {"total_time": 10**400}, None),  # an integer of 400 digits
        (TIMES | {"queue_time": 1e308, "total_time": 1e308}, None),
    ],
)
async def test_times_that_cant_be_right_give_none(
    serve: Serve, times: dict[str, Any], timings: Timings | None
) -> None:
    usage = {"choices": [], "usage": {"prompt_tokens": 96, "completion_tokens": 40, **times}}
    events = await collect(serve(sse(delta(content="ok"), STOP, usage)).stream(ask("hi")))

    assert events[0] == TextDelta("ok")  # the answer, yielded all the same
    finish = events[-1]
    assert isinstance(finish, Finish)
    assert finish.timings == timings
    assert finish.usage == Usage(input_tokens=96, output_tokens=40)  # counted all the same


async def test_a_server_that_reports_no_usage_gets_none_not_zeros(serve: Serve) -> None:
    """Zeros would be charged as a free request. `None` tells the caller to count."""
    events = await collect(serve(sse(delta(content="ok"), STOP, "data: [DONE]")).stream(ask("hi")))

    assert events[-1] == Finish(FinishReason.STOP, None)


async def test_keep_alive_comments_and_a_missing_done_marker_are_tolerated(serve: Serve) -> None:
    body = sse(": keep-alive", "event: message", delta(content="ok"), STOP)
    events = await collect(serve(body).stream(ask("hi")))

    assert events == [TextDelta("ok"), Finish(FinishReason.STOP, None)]


@pytest.mark.parametrize(
    ("reported", "reason"),
    [
        ("content_filter", FinishReason.CONTENT_FILTER),
        ("length", FinishReason.LENGTH),
        ("eos", FinishReason.STOP),  # a server's own word for a normal end
    ],
)
async def test_finish_reasons_are_translated(
    serve: Serve, reported: str, reason: FinishReason
) -> None:
    last = {"choices": [{"index": 0, "delta": {}, "finish_reason": reported}]}
    events = await collect(serve(sse(delta(content="ok"), last)).stream(ask("hi")))

    assert events[-1] == Finish(reason, None)


async def test_a_tool_call_finishing_with_stop_is_still_a_tool_call(serve: Serve) -> None:
    """Some servers repeat the name in every fragment, send no id, and finish with "stop"."""
    parts = [
        delta(tool_calls=[{"index": 0, "function": {"name": "get_time", "arguments": '{"tz":'}}]),
        delta(tool_calls=[{"index": 0, "function": {"name": "get_time", "arguments": ' "UTC"}'}}]),
        STOP,
    ]
    events = await collect(serve(sse(*parts)).stream(ask("hi")))

    assert events == [
        ToolCall("call_1", "get_time", {"tz": "UTC"}),
        Finish(FinishReason.TOOL_CALLS, None),
    ]


def whole_call(call_id: str, city: str) -> dict[str, Any]:
    """A whole call in one fragment without an `index`, as the Gemini API sends one."""
    return {
        "function": {"arguments": json.dumps({"city": city}), "name": "get_weather"},
        "id": call_id,
        "type": "function",
    }


async def test_calls_without_an_index_are_told_apart_by_their_ids(serve: Serve) -> None:
    """Two calls at once, in two events, neither with an `index`: joined, they were one
    call whose arguments couldn't be read."""
    parts = [
        delta(tool_calls=[whole_call("call_1512", "Paris")]),
        delta(tool_calls=[whole_call("call_1513", "Rome")]),
        STOP,
    ]
    events = await collect(serve(sse(*parts)).stream(ask("hi", tools=[WEATHER])))

    assert events == [
        ToolCall("call_1512", "get_weather", {"city": "Paris"}),
        ToolCall("call_1513", "get_weather", {"city": "Rome"}),
        Finish(FinishReason.TOOL_CALLS, None),
    ]


async def test_a_fragment_without_an_index_or_a_new_id_belongs_to_the_last_call(
    serve: Serve,
) -> None:
    parts = [
        delta(tool_calls=[{"id": "call_a", "function": {"name": "get_time", "arguments": "{"}}]),
        delta(tool_calls=[{"id": "call_a", "function": {"arguments": '"tz": '}}]),
        delta(tool_calls=[{"function": {"arguments": '"UTC"}'}}]),
        delta(tool_calls=[whole_call("call_b", "Rome")]),
        STOP,
    ]
    events = await collect(serve(sse(*parts)).stream(ask("hi", tools=[WEATHER])))

    assert events[:2] == [
        ToolCall("call_a", "get_time", {"tz": "UTC"}),
        ToolCall("call_b", "get_weather", {"city": "Rome"}),
    ]


def finished(reason: str) -> dict[str, Any]:
    return {"choices": [{"index": 0, "delta": {}, "finish_reason": reason}]}


async def test_a_call_without_an_index_after_the_calls_were_given_out_is_one_of_its_own(
    serve: Serve,
) -> None:
    """The calls collected are given out at a `finish_reason`. A fragment without an
    `index` that comes after it has no call collected last to belong to."""
    parts = [
        delta(tool_calls=[whole_call("call_a", "Paris")]),
        finished("stop"),
        delta(tool_calls=[whole_call("call_b", "Rome")]),
        finished("stop"),
    ]
    events = await collect(serve(sse(*parts)).stream(ask("hi", tools=[WEATHER])))

    assert events == [
        ToolCall("call_a", "get_weather", {"city": "Paris"}),
        ToolCall("call_b", "get_weather", {"city": "Rome"}),
        Finish(FinishReason.TOOL_CALLS, None),
    ]


async def test_a_nameless_fragment_after_the_calls_were_given_out_is_the_providers_error(
    serve: Serve,
) -> None:
    parts = [
        delta(tool_calls=[whole_call("call_a", "Paris")]),
        finished("stop"),
        delta(tool_calls=[{"function": {"arguments": "{}"}}]),
        finished("stop"),
    ]
    with pytest.raises(LLMError) as caught:
        await collect(serve(sse(*parts)).stream(ask("hi", tools=[WEATHER])))

    assert type(caught.value) is ProviderError
    assert "a tool call that can't be read" in str(caught.value)


@pytest.mark.parametrize(
    "body",
    [
        sse("data: <html>gateway timeout</html>"),
        sse("data: [1, 2, 3]"),
        sse("data: " + "[" * 200_000),  # too deep for the JSON reader: no `ValueError`
        sse(
            delta(tool_calls=[{"index": 0, "function": {"name": "f", "arguments": "[" * 200_000}}]),
            STOP,
        ),
        sse(delta(tool_calls=["nonsense"])),
        sse(
            delta(tool_calls=[{"index": 0, "function": {"name": "f", "arguments": '{"a": '}}]), STOP
        ),
        sse(delta(tool_calls=[{"index": 0, "function": {"arguments": "{}"}}]), STOP),
    ],
)
async def test_a_stream_that_is_not_the_protocol_is_a_provider_error(
    serve: Serve, body: bytes
) -> None:
    with pytest.raises(ProviderError):
        await collect(serve(body).stream(ask("hi")))


async def test_an_error_sent_as_plain_lines_mid_answer_is_an_interruption_that_says_it(
    serve: Serve,
) -> None:
    """As the Gemini API sends one: a list of one error, pretty-printed over several
    lines that are no event, and then nothing, no finish and no `[DONE]`."""
    overloaded = "This model is currently experiencing high demand."
    lines = json.dumps([gemini_error(503, overloaded, "UNAVAILABLE")], indent=2).splitlines()
    body = sse(thought("<thought>Thinking.")) + "\n".join(lines).encode() + b"\n"
    received: list[StreamEvent] = []

    async def read() -> None:
        async for event in serve(body).stream(ask("hi", thinking=True)):
            received.append(event)  # noqa: PERF401 - the list must outlive the exception

    with pytest.raises(StreamInterruptedError) as caught:
        await read()

    assert len(lines) > 2
    assert received == [ReasoningDelta("Thinking.")]
    assert str(caught.value) == f"Example's answer ended before it finished: {overloaded}"


async def test_comments_and_event_lines_are_not_taken_for_part_of_an_error(serve: Serve) -> None:
    """Fields of the stream, a keep-alive comment and an `event:` line, between the
    pieces: they are no part of the error sent as plain lines after them."""
    overloaded = "This model is currently experiencing high demand."
    lines = json.dumps([gemini_error(503, overloaded, "UNAVAILABLE")], indent=2).splitlines()
    pieces = sse(thought("<thought>One."), ": keep-alive", "event: message", thought("Two."))
    body = pieces + "\n".join(lines).encode() + b"\n"

    with pytest.raises(StreamInterruptedError) as caught:
        await collect(serve(body).stream(ask("hi", thinking=True)))

    assert str(caught.value) == f"Example's answer ended before it finished: {overloaded}"


@pytest.mark.parametrize(
    "lines",
    [
        ["something: else", "[1, 2]"],
        ["[" * 200_000],  # too deep for the JSON reader: no `ValueError`
    ],
    ids=["no error", "nested too deep"],
)
async def test_lines_that_are_no_error_leave_an_interruption_unexplained(
    serve: Serve, lines: list[str]
) -> None:
    body = sse(delta(content="Hel"), *lines)
    with pytest.raises(StreamInterruptedError) as caught:
        await collect(serve(body).stream(ask("hi")))

    assert str(caught.value) == "Example's answer ended before it finished"


async def test_no_more_lines_that_are_no_event_are_kept_than_the_bound(serve: Serve) -> None:
    """A server that sends nonsense can't make the client hold all of it."""
    parser = _Parser("Example")
    for number in range(10_000):
        parser.feed(f"nonsense {number}")

    with pytest.raises(StreamInterruptedError):
        parser.finish()
    assert len(parser._stray) == _STRAY_LINES

    body = sse(delta(content="Hel"), *(f"nonsense {number}" for number in range(10_000)))
    with pytest.raises(StreamInterruptedError):
        await collect(serve(body).stream(ask("hi")))


async def test_an_error_event_mid_answer_is_an_interruption(serve: Serve) -> None:
    body = sse(delta(content="Hel"), {"error": {"message": "upstream reset", "code": None}})
    with pytest.raises(StreamInterruptedError, match=r"Example reported an error.*upstream reset"):
        await collect(serve(body).stream(ask("hi")))


@pytest.mark.parametrize(
    ("status", "body", "headers", "error"),
    [
        (429, '{"error":{"message":"slow down"}}', {}, RateLimitedError),
        (429, '{"error":{"message":"slow down"}}', {"retry-after": "tomorrow"}, RateLimitedError),
        (403, '{"error":{"message":"forbidden"}}', {}, ProviderError),
        (404, "<html>Not Found</html>", {}, ProviderError),  # a wrong base URL, not a model
        (400, '{"error":"maximum context length is 8192 tokens"}', {}, ContextOverflowError),
        (422, "", {}, RequestRejectedError),
        (529, '{"error":{"message":"overloaded"}}', {}, ProviderUnavailableError),
        # More than the provider takes from this account in one request, which no wait
        # changes, whatever its `Retry-After` says: as Groq answers it.
        (413, json.dumps(TOO_LARGE), {"retry-after": "14"}, ContextOverflowError),
        # A key the Gemini API doesn't know is refused with 400, from the chat as a list
        # and from the list of models as an object.
        (400, json.dumps([gemini_error(400, KEY_REFUSED, "INVALID_ARGUMENT")]), {}, ProviderError),
        (400, json.dumps(gemini_error(400, KEY_REFUSED, "INVALID_ARGUMENT")), {}, ProviderError),
        (
            400,
            json.dumps([gemini_error(400, INVALID, "INVALID_ARGUMENT")]),
            {},
            RequestRejectedError,
        ),
        # Too deep for the JSON reader, which raises no `ValueError` then: as one that
        # isn't JSON, a server's failure.
        (500, "[" * 200_000, {}, ProviderError),
    ],
)
async def test_other_error_shapes_are_understood(
    serve: Serve, status: int, body: str, headers: dict[str, str], error: type[Exception]
) -> None:
    adapter = serve(
        lambda _request: httpx2.Response(status, content=body.encode(), headers=headers)
    )
    with pytest.raises(error) as caught:
        await collect(adapter.stream(ask("hi")))

    assert type(caught.value) is error
    if isinstance(caught.value, RateLimitedError):
        assert caught.value.retry_after_s is None  # absent, or a date: not a number of seconds


@pytest.mark.parametrize(
    ("failure", "error"),
    [
        (httpx2.ConnectError, ConnectionFailedError),
        (httpx2.ConnectTimeout, ProviderUnavailableError),
        (httpx2.ReadError, ProviderUnavailableError),
        (httpx2.RemoteProtocolError, ProviderUnavailableError),
    ],
    ids=["refused", "never answered", "closed", "hung up on the request"],
)
@pytest.mark.parametrize("call", ["a chat", "a small call"])
async def test_a_server_that_cant_be_reached_is_unavailable(
    serve: Serve, failure: type[httpx2.TransportError], error: type[Exception], call: str
) -> None:
    """A connection that failed at once, with nothing sent, is a kind of its own, which
    is worth asking again at once. Any other failure of the connection before an answer
    is a plain one: its time is spent, or the request may have been read."""

    def fail(request: httpx2.Request) -> httpx2.Response:
        raise failure("the connection failed", request=request)

    adapter = serve(fail)

    async def called() -> object:
        if call == "a chat":
            return await collect(adapter.stream(ask("hi")))
        return await adapter.models()

    with pytest.raises(ProviderUnavailableError, match="can't reach Example") as caught:
        await called()

    assert type(caught.value) is error


async def test_an_error_in_a_list_of_one_says_its_own_message(serve: Serve) -> None:
    body = json.dumps([gemini_error(400, INVALID, "INVALID_ARGUMENT")])
    adapter = serve(lambda _request: httpx2.Response(400, content=body.encode()))

    with pytest.raises(RequestRejectedError) as caught:
        await collect(adapter.stream(ask("hi")))

    assert str(caught.value) == f"Example answered HTTP 400: {INVALID}"


@pytest.mark.parametrize(
    ("body", "parts"),
    [
        (json.dumps([gemini_error(404, "not found", "NOT_FOUND")]), ("not found", "NOT_FOUND")),
        (json.dumps(gemini_error(404, "not found", "NOT_FOUND")), ("not found", "NOT_FOUND")),
        ('{"error":{"message":"gone","code":"model_not_found"}}', ("gone", "model_not_found")),
        ('{"error":{"message":"gone","code":404}}', ("gone", None)),
        ("[]", ("[]", None)),
    ],
)
def test_a_code_that_is_a_number_is_named_by_its_status(
    body: str, parts: tuple[str, str | None]
) -> None:
    """No refusal is told apart by such a name yet: the code is read here alone."""
    assert _error_parts(body) == parts


def retry_info(delay: Any) -> list[dict[str, Any]]:
    """The Gemini API's refusal at a spent quota, which says how long to wait in its body."""
    error = gemini_error(429, "You exceeded your current quota.", "RESOURCE_EXHAUSTED")
    error["error"]["details"] = [
        {"@type": "type.googleapis.com/google.rpc.Help", "links": []},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay},
    ]
    return [error]


@pytest.mark.parametrize(
    ("delay", "headers", "wait"),
    [
        ("83280s", {}, 83280.0),
        ("1.5s", {}, 1.5),
        ("83280s", {"retry-after": "2"}, 2.0),  # the header wins
        ("soon", {}, None),
        ("-1s", {}, None),
        ("1.5", {}, None),
        (1.5, {}, None),
        ("9" * 400 + "s", {}, None),  # no number of seconds
        # Not plain seconds, the whole of it.
        ("1.5sx", {}, None),
        ("5m", {}, None),
        ("s", {}, None),
        ("", {}, None),
        (17, {}, None),
    ],
)
async def test_a_spent_quota_says_how_long_to_wait_in_its_body(
    serve: Serve, delay: Any, headers: dict[str, str], wait: float | None
) -> None:
    body = json.dumps(retry_info(delay)).encode()
    adapter = serve(lambda _request: httpx2.Response(429, content=body, headers=headers))

    with pytest.raises(RateLimitedError) as caught:
        await collect(adapter.stream(ask("hi")))

    assert caught.value.retry_after_s == wait
    assert "You exceeded your current quota." in str(caught.value)


async def test_a_model_listed_by_a_path_is_listed_by_its_name_too(serve: Serve) -> None:
    """The Gemini API lists `models/gemini-3.6-flash`, and its chat takes
    `gemini-3.6-flash`. Another server may take `models/qwen` as it is written, as vLLM
    started with a path does. An id is never there twice."""
    listed = {
        "object": "list",
        "data": [
            {"id": "models/gemini-3.6-flash", "object": "model", "owned_by": "google"},
            {"id": "fake-chat", "object": "model"},
            {"id": "models/qwen"},
            {"id": "qwen"},
        ],
    }
    adapter = serve(lambda _request: httpx2.Response(200, json=listed))

    assert await adapter.models() == [
        "models/gemini-3.6-flash",
        "gemini-3.6-flash",
        "fake-chat",
        "models/qwen",
        "qwen",
    ]


async def test_a_list_of_models_nested_too_deep_is_the_providers_error(serve: Serve) -> None:
    """The JSON reader raises no `ValueError` for it: as a list that isn't JSON."""
    adapter = serve(lambda _request: httpx2.Response(200, content=b"[" * 200_000))

    with pytest.raises(ProviderError, match="isn't JSON"):
        await adapter.models()


async def test_closing_the_stream_closes_the_connection(serve: Serve) -> None:
    class Endless(httpx2.AsyncByteStream):
        closed = False

        async def __aiter__(self) -> AsyncIterator[bytes]:
            while True:
                yield sse(delta(content="again "))

        async def aclose(self) -> None:
            self.closed = True

    body = Endless()
    stream = serve(lambda _request: httpx2.Response(200, stream=body)).stream(ask("hi"))
    assert await anext(stream) == TextDelta("again ")
    await stream.aclose()

    assert body.closed
