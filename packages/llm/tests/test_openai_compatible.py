"""The OpenAI-compatible adapter.

The first half talks to fakellm, in process. fakellm is an independent implementation of
the wire format, itself checked with OpenAI's official SDK, so the two keep each other
honest. Directives in the prompt, such as `[[think: ...]]`, script what it answers.

The second half feeds the adapter responses written here: the ways real servers differ
from one another, which one fake shouldn't try to imitate all at once.
"""

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
    ContextOverflowError,
    Finish,
    FinishReason,
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
    ToolCall,
    ToolResult,
    ToolSpec,
    Usage,
    User,
)
from actio_llm.openai_compatible import OpenAICompatible

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
    assert await fake().models() == ["fake-chat"]


# --- Responses written here ----------------------------------------------------------

type Handler = Callable[[httpx2.Request], httpx2.Response]
type Serve = Callable[..., OpenAICompatible]

STOP = {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}


def delta(**fields: Any) -> dict[str, Any]:
    return {"choices": [{"index": 0, "delta": fields, "finish_reason": None}]}


def sse(*events: dict[str, Any] | str) -> bytes:
    lines = [e if isinstance(e, str) else f"data: {json.dumps(e)}" for e in events]
    return "".join(f"{line}\n\n" for line in lines).encode()


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


async def test_reasoning_is_read_under_its_older_name_too(serve: Serve) -> None:
    body = sse(delta(reasoning_content="Hmm."), delta(content="Yes."), STOP, "data: [DONE]")
    events = await collect(serve(body).stream(ask("hi")))

    assert events[:2] == [ReasoningDelta("Hmm."), TextDelta("Yes.")]


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


@pytest.mark.parametrize(
    "body",
    [
        sse("data: <html>gateway timeout</html>"),
        sse("data: [1, 2, 3]"),
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
