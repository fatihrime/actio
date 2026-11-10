"""The Ollama adapter.

The first half replays cassettes: real exchanges recorded from an Ollama server (see
conftest.py). The second half feeds the adapter responses written here, for cases a
healthy server won't produce on demand: broken streams, timeouts, old versions.
"""

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import aclosing
from typing import Any

import httpx2
import pytest

from actio_llm import (
    Assistant,
    ChatRequest,
    ContextOverflowError,
    Finish,
    FinishReason,
    ModelNotFoundError,
    Ollama,
    ProviderError,
    ProviderUnavailableError,
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
    UnsupportedFeatureError,
    UnsupportedServerError,
    Usage,
    User,
)
from actio_llm.ollama import MIN_VERSION

pytestmark = pytest.mark.anyio

MODEL = "granite4:micro"  # the cassettes' model: small, with tools, without reasoning
CONTEXT = 4096
WEATHER = ToolSpec(
    name="get_weather",
    description="Get the current weather for a city.",
    parameters={"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
)
FILLER = "\n".join(f"Line {i}: the quick brown fox jumps over the lazy dog." for i in range(30))


def ask(prompt: str, **options: Any) -> ChatRequest:
    options.setdefault("context_window", CONTEXT)
    return ChatRequest(model=MODEL, messages=[User(prompt)], temperature=0, seed=7, **options)


async def collect(stream: AsyncGenerator[StreamEvent]) -> list[StreamEvent]:
    async with aclosing(stream) as events:
        return [event async for event in events]


def text_of(events: list[StreamEvent]) -> str:
    return "".join(event.text for event in events if isinstance(event, TextDelta))


# --- Recorded exchanges --------------------------------------------------------------


async def test_streams_the_answer_then_one_finish(ollama_with: Any) -> None:
    ollama, tape = ollama_with("chat_text")
    events = await collect(ollama.stream(ask("Say hello in five words.")))

    *deltas, finish = events
    assert deltas
    assert all(isinstance(event, TextDelta) for event in deltas)
    assert text_of(events) == "".join(chunk["message"]["content"] for chunk in tape.chunks)
    last = tape.chunks[-1]
    assert finish == Finish(
        FinishReason.STOP,
        Usage(
            input_tokens=last["prompt_eval_count"],
            output_tokens=last["eval_count"],
            cached_input_tokens=last["prompt_eval_cached_count"],
        ),
        Timings(
            load_ms=last["load_duration"] // 1_000_000,
            prompt_ms=last["prompt_eval_duration"] // 1_000_000,
            generation_ms=last["eval_duration"] // 1_000_000,
            total_ms=last["total_duration"] // 1_000_000,
        ),
    )
    assert finish.tokens_per_second is not None
    assert finish.tokens_per_second > 0


async def test_max_output_tokens_cuts_the_answer(ollama_with: Any) -> None:
    ollama, _ = ollama_with("chat_length")
    events = await collect(ollama.stream(ask("Count from 1 to 50.", max_output_tokens=5)))

    finish = events[-1]
    assert isinstance(finish, Finish)
    assert finish.reason is FinishReason.LENGTH
    assert finish.usage is not None
    assert finish.usage.output_tokens == 5


async def test_a_prompt_that_does_not_fit_is_refused_not_cut(ollama_with: Any) -> None:
    """The reason this adapter exists: by default Ollama would answer from half the prompt."""
    ollama, _ = ollama_with("chat_context_overflow")
    with pytest.raises(ContextOverflowError, match="exceeds the available context size") as caught:
        await collect(ollama.stream(ask(FILLER, context_window=256)))

    assert caught.value.context_window == 256
    assert caught.value.prompt_tokens is not None
    assert caught.value.prompt_tokens > 256


async def test_an_answer_stops_when_the_context_window_is_full(ollama_with: Any) -> None:
    """By default Ollama would push the oldest tokens out and keep writing."""
    ollama, _ = ollama_with("chat_context_full")
    lines = "\n".join(FILLER.splitlines()[:12])  # most of the window, so the answer is short
    prompt = f"{lines}\nNow count from 1 to 500, one number per line."
    events = await collect(ollama.stream(ask(prompt, context_window=256)))

    finish = events[-1]
    assert isinstance(finish, Finish)
    assert finish.reason is FinishReason.LENGTH
    assert finish.usage is not None
    assert finish.usage.input_tokens + finish.usage.output_tokens == 256


async def test_a_tool_call_arrives_whole(ollama_with: Any) -> None:
    ollama, tape = ollama_with("chat_tool_call")
    request = ask("What is the weather in Izmir right now?", tools=[WEATHER])
    events = await collect(ollama.stream(request))

    [call] = [event for event in events if isinstance(event, ToolCall)]
    [sent] = [c for chunk in tape.chunks for c in chunk["message"].get("tool_calls", [])]
    assert call == ToolCall(sent["id"], "get_weather", sent["function"]["arguments"])
    assert "city" in call.arguments
    finish = events[-1]
    assert isinstance(finish, Finish)
    assert finish.reason is FinishReason.TOOL_CALLS  # Ollama itself reports "stop"


async def test_a_tool_result_goes_back_as_history(ollama_with: Any) -> None:
    ollama, _ = ollama_with("chat_tool_result")
    call = ToolCall("call_1", "get_weather", {"city": "Izmir"})
    request = ChatRequest(
        model=MODEL,
        messages=[
            User("What is the weather in Izmir right now?"),
            Assistant(tool_calls=(call,)),
            ToolResult(call.id, call.name, "18 degrees Celsius, light rain"),
        ],
        tools=[WEATHER],
        context_window=CONTEXT,
        temperature=0,
        seed=7,
    )
    events = await collect(ollama.stream(request))

    assert "18" in text_of(events)
    finish = events[-1]
    assert isinstance(finish, Finish)
    assert finish.reason is FinishReason.STOP


async def test_an_unknown_model_is_reported_as_such(ollama_with: Any) -> None:
    ollama, _ = ollama_with("chat_unknown_model")
    request = ChatRequest(model="no-such-model:1b", messages=[User("hello")])
    with pytest.raises(ModelNotFoundError, match="not found") as caught:
        await collect(ollama.stream(request))

    assert caught.value.model == "no-such-model:1b"


async def test_reasoning_is_refused_by_a_model_without_it(ollama_with: Any) -> None:
    ollama, _ = ollama_with("chat_thinking_unsupported")
    with pytest.raises(UnsupportedFeatureError, match="does not support thinking"):
        await collect(ollama.stream(ask("What is 17 * 23?", thinking=True)))


async def test_describe_reads_what_a_model_can_do(ollama_with: Any) -> None:
    ollama, _ = ollama_with("show")
    info = await ollama.describe(MODEL)

    assert info.can("completion")
    assert info.can("tools")
    assert not info.can("thinking")
    assert info.context_length == 131_072
    assert info.parameter_size == "3.4B"
    assert info.runner is None  # the default engine, which enforces the context window
    assert info.enforces_window


async def test_models_lists_what_is_on_disk(ollama_with: Any) -> None:
    ollama, _ = ollama_with("tags")
    models = {model.name: model for model in await ollama.models()}

    assert models[MODEL].size_bytes > 1_000_000_000
    assert models[MODEL].quantization == "Q4_K_M"


async def test_the_server_reports_a_supported_version(ollama_with: Any) -> None:
    ollama, _ = ollama_with("version")
    assert await ollama.version() >= MIN_VERSION


# --- Responses written here ----------------------------------------------------------

type Handler = Callable[[httpx2.Request], httpx2.Response]
type Serve = Callable[..., Ollama]

DONE: dict[str, Any] = {
    "done": True,
    "done_reason": "stop",
    "prompt_eval_count": 10,
    "eval_count": 3,
}


def piece(text: str = "", **message: Any) -> dict[str, Any]:
    return {"message": {"role": "assistant", "content": text, **message}, "done": False}


def ndjson(*chunks: dict[str, Any]) -> bytes:
    return b"".join(json.dumps(chunk).encode() + b"\n" for chunk in chunks)


def answering(*chunks: dict[str, Any]) -> Handler:
    return lambda _request: httpx2.Response(200, content=ndjson(*chunks))


class Paced(httpx2.AsyncByteStream):
    """A response body that arrives in parts: bytes are sent, numbers are pauses in seconds."""

    def __init__(self, *parts: bytes | float | Exception) -> None:
        self._parts = parts
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for part in self._parts:
            if isinstance(part, bytes):
                yield part
            elif isinstance(part, Exception):
                raise part
            else:
                await asyncio.sleep(part)

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
async def serve() -> AsyncIterator[Serve]:
    """Call with a handler for `/api/chat` to get an adapter talking to it."""
    clients: list[httpx2.AsyncClient] = []

    def start(chat: Handler, *, version: str = "0.40.16", **options: Any) -> Ollama:
        def route(request: httpx2.Request) -> httpx2.Response:
            if request.url.path == "/api/version":
                return httpx2.Response(200, json={"version": version})
            return chat(request)

        client = httpx2.AsyncClient(
            base_url="http://ollama.test", transport=httpx2.MockTransport(route)
        )
        clients.append(client)
        return Ollama(client, **options)

    yield start
    for client in clients:
        await client.aclose()


async def test_reasoning_streams_before_the_answer(serve: Serve) -> None:
    """Written from Ollama's documented shape (`message.thinking`), not recorded: the
    cassettes' model can't reason. A live test covers a model that can."""
    ollama = serve(
        answering(piece(thinking="17 times 23"), piece(thinking=" is 391."), piece("391"), DONE)
    )
    events = await collect(ollama.stream(ask("What is 17 * 23?", thinking=True)))

    assert events[:3] == [
        ReasoningDelta("17 times 23"),
        ReasoningDelta(" is 391."),
        TextDelta("391"),
    ]


async def test_a_request_becomes_ollamas_json(serve: Serve) -> None:
    sent: list[dict[str, Any]] = []

    def chat(request: httpx2.Request) -> httpx2.Response:
        sent.append(json.loads(request.content))
        return httpx2.Response(200, content=ndjson(DONE))

    call = ToolCall("call_1", "get_weather", {"city": "Izmir"})
    request = ChatRequest(
        model="qwen3.5:4b",
        messages=[
            System("Be brief."),
            User("Weather in Izmir?"),
            Assistant(tool_calls=(call,), reasoning="I should look it up."),
            ToolResult("call_1", "get_weather", "18 degrees"),
        ],
        tools=[WEATHER],
        max_output_tokens=512,
        context_window=8192,
        thinking="low",
        temperature=0.2,
        top_p=0.9,
        seed=3,
        stop=["END"],
    )
    await collect(serve(chat, keep_alive="30m").stream(request))

    assert sent == [
        {
            "model": "qwen3.5:4b",
            "messages": [
                {"role": "system", "content": "Be brief."},
                {"role": "user", "content": "Weather in Izmir?"},
                {
                    "role": "assistant",
                    "content": "",
                    "thinking": "I should look it up.",
                    "tool_calls": [
                        {
                            "type": "function",
                            "function": {
                                "index": 0,
                                "name": "get_weather",
                                "arguments": {"city": "Izmir"},
                            },
                        }
                    ],
                },
                {"role": "tool", "tool_name": "get_weather", "content": "18 degrees"},
            ],
            "stream": True,
            "truncate": False,  # refuse a prompt that doesn't fit
            "shift": False,  # stop when the window is full
            "keep_alive": "30m",
            "options": {
                "num_ctx": 8192,
                "num_predict": 512,
                "temperature": 0.2,
                "top_p": 0.9,
                "seed": 3,
                "stop": ["END"],
            },
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
            "think": "low",
        }
    ]


async def test_a_plain_request_sets_only_the_safety_settings(serve: Serve) -> None:
    sent: list[dict[str, Any]] = []

    def chat(request: httpx2.Request) -> httpx2.Response:
        sent.append(json.loads(request.content))
        return httpx2.Response(200, content=ndjson(DONE))

    await collect(serve(chat).stream(ChatRequest(model="m", messages=[User("hi")])))

    assert sent == [
        {
            "model": "m",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
            "truncate": False,
            "shift": False,
            "keep_alive": "5m",
            "options": {"num_predict": 4096},  # an answer is never unbounded
        }
    ]


@pytest.mark.parametrize(
    ("limits", "sent"),
    [
        ({}, 4096),
        ({"max_output_tokens": 300}, 300),
        ({"context_window": 8192}, 8192),
        ({"max_output_tokens": 300, "context_window": 8192}, 300),
        ({"max_output_tokens": 9000, "context_window": 8192}, 8192),
    ],
)
async def test_every_answer_has_a_token_limit(
    serve: Serve, limits: dict[str, Any], sent: int
) -> None:
    """Ollama's MLX engine ignores the context window, so there only this limit stops an
    answer. Measured: a model left to its defaults thought for 7,297 tokens to say hello."""
    bodies: list[dict[str, Any]] = []

    def chat(request: httpx2.Request) -> httpx2.Response:
        bodies.append(json.loads(request.content))
        return httpx2.Response(200, content=ndjson(DONE))

    await collect(serve(chat).stream(ChatRequest(model="m", messages=[User("hi")], **limits)))

    assert bodies[0]["options"]["num_predict"] == sent


async def test_describe_says_which_engine_ignores_the_context_window() -> None:
    """Written from what `/api/show` answered for an MLX model on Ollama 0.40.16."""
    shown = {
        "capabilities": ["completion", "vision", "thinking", "tools"],
        "details": {"format": "safetensors", "parameter_size": "4.5B", "runner": "mlx"},
        "model_info": {"qwen3_5.context_length": 262_144},
        "thinking": {"values": [False, True], "default": True},
    }
    async with httpx2.AsyncClient(
        base_url="http://ollama.test",
        transport=httpx2.MockTransport(lambda _request: httpx2.Response(200, json=shown)),
    ) as client:
        info = await Ollama(client).describe("qwen3.5:4b")

    assert info.runner == "mlx"
    assert not info.enforces_window
    assert info.can("thinking")
    assert info.thinking_levels == ()  # this model's reasoning is on or off, with no levels
    assert info.context_length == 262_144


async def test_lines_split_across_network_chunks_are_put_back_together(serve: Serve) -> None:
    body = ndjson(piece("Mer"), piece("haba"), DONE)
    slices = [body[i : i + 7] for i in range(0, len(body), 7)]
    ollama = serve(lambda _request: httpx2.Response(200, stream=Paced(*slices)))

    assert text_of(await collect(ollama.stream(ask("hi")))) == "Merhaba"


async def test_usage_never_reports_more_cached_tokens_than_prompt_tokens(serve: Serve) -> None:
    done = {"done": True, "done_reason": "stop", "prompt_eval_cached_count": 99, "eval_count": 4}
    [finish] = await collect(serve(answering(done)).stream(ask("hi")))

    assert isinstance(finish, Finish)
    assert finish.usage == Usage(input_tokens=0, output_tokens=4, cached_input_tokens=0)
    assert finish.tokens_per_second is None  # no duration reported, so no speed


async def test_an_old_server_is_refused_before_any_prompt_is_sent(serve: Serve) -> None:
    """An older Ollama may ignore the strict flags, which would bring silent cuts back."""
    asked: list[httpx2.Request] = []

    def chat(request: httpx2.Request) -> httpx2.Response:
        asked.append(request)
        return httpx2.Response(200, content=ndjson(DONE))

    ollama = serve(chat, version="0.31.2")
    with pytest.raises(UnsupportedServerError, match=r"0\.31\.2 is older than 0\.40\.0"):
        await collect(ollama.stream(ask("hi")))
    assert asked == []

    unchecked = serve(chat, version="0.31.2", check_version=False)
    assert await collect(unchecked.stream(ask("hi")))


async def test_the_version_is_checked_once() -> None:
    paths: list[str] = []

    def route(request: httpx2.Request) -> httpx2.Response:
        paths.append(request.url.path)
        if request.url.path == "/api/version":
            return httpx2.Response(200, json={"version": "0.41.0-rc2"})
        return httpx2.Response(200, content=ndjson(DONE))

    async with httpx2.AsyncClient(
        base_url="http://ollama.test", transport=httpx2.MockTransport(route)
    ) as client:
        ollama = Ollama(client)
        await collect(ollama.stream(ask("one")))
        await collect(ollama.stream(ask("two")))
        assert await ollama.version() == (0, 41, 0)

    assert paths == ["/api/version", "/api/chat", "/api/chat", "/api/version"]


@pytest.mark.parametrize("reported", [{"version": "dev"}, {}])
async def test_an_unreadable_version_is_an_error(reported: dict[str, str]) -> None:
    async with httpx2.AsyncClient(
        base_url="http://ollama.test",
        transport=httpx2.MockTransport(lambda _request: httpx2.Response(200, json=reported)),
    ) as client:
        with pytest.raises(ProviderError, match="version"):
            await Ollama(client).version()


@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (503, '{"error":"server busy, please try again"}', ProviderUnavailableError),
        (500, '{"error":"model requires more system memory"}', ProviderError),
        (400, '{"error":"invalid options"}', RequestRejectedError),
        (400, "not json at all", RequestRejectedError),
        (400, '{"error":"prompt exceeds the available context size"}', ContextOverflowError),
    ],
)
async def test_http_errors_become_typed_errors(
    serve: Serve, status: int, body: str, error: type[Exception]
) -> None:
    ollama = serve(lambda _request: httpx2.Response(status, content=body.encode()))
    with pytest.raises(error):
        await collect(ollama.stream(ask("hi")))


async def test_an_unreachable_server_is_unavailable(serve: Serve) -> None:
    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused", request=request)

    with pytest.raises(ProviderUnavailableError, match="can't reach Ollama"):
        await collect(serve(refuse, check_version=False).stream(ask("hi")))


async def test_events_before_a_break_are_delivered_then_the_break_is_reported(serve: Serve) -> None:
    broken = Paced(ndjson(piece("Hel")), httpx2.ReadError("connection reset"))
    ollama = serve(lambda _request: httpx2.Response(200, stream=broken))
    received: list[StreamEvent] = []

    async def read() -> None:
        async for event in ollama.stream(ask("hi")):
            received.append(event)  # noqa: PERF401 - the list must outlive the exception

    with pytest.raises(StreamInterruptedError, match="connection to Ollama broke"):
        await read()

    assert received == [TextDelta("Hel")]


@pytest.mark.parametrize(
    ("chunks", "message"),
    [
        ([piece("Hel"), {"error": "llama runner process has terminated"}], "error mid-answer"),
        ([piece("Hel")], "ended without its final chunk"),
    ],
)
async def test_an_answer_that_breaks_off_is_an_interruption(
    serve: Serve, chunks: list[dict[str, Any]], message: str
) -> None:
    with pytest.raises(StreamInterruptedError, match=message):
        await collect(serve(answering(*chunks)).stream(ask("hi")))


@pytest.mark.parametrize(
    "body",
    [
        b"<html>not ollama</html>\n",
        b"[1, 2, 3]\n",
        ndjson(piece(tool_calls=[{"function": {"name": "", "arguments": {}}}])),
        ndjson(piece(tool_calls=[{"function": {"name": "f", "arguments": "not an object"}}])),
        ndjson(piece(tool_calls=["nonsense"])),
    ],
)
async def test_a_response_that_is_not_ollamas_protocol_is_a_provider_error(
    serve: Serve, body: bytes
) -> None:
    ollama = serve(lambda _request: httpx2.Response(200, content=body))
    with pytest.raises(ProviderError):
        await collect(ollama.stream(ask("hi")))


async def test_tool_calls_without_ids_get_one(serve: Serve) -> None:
    calls = [
        {"function": {"name": "a", "arguments": {}}},
        {"function": {"name": "b", "arguments": {"x": 1}}},
    ]
    events = await collect(serve(answering(piece(tool_calls=calls), DONE)).stream(ask("hi")))

    assert events[:2] == [ToolCall("call_1", "a", {}), ToolCall("call_2", "b", {"x": 1})]


# --- Time ----------------------------------------------------------------------------

FAST = Timeouts(first_event_s=0.05, idle_s=0.05)


async def test_a_server_that_never_starts_answering_is_unavailable(serve: Serve) -> None:
    silent = Paced(5.0, ndjson(DONE))
    ollama = serve(lambda _request: httpx2.Response(200, stream=silent), timeouts=FAST)

    with pytest.raises(ProviderUnavailableError, match=r"didn't start answering within 0\.05 s"):
        await collect(ollama.stream(ask("hi")))
    assert silent.closed


async def test_an_answer_that_stalls_is_an_interruption(serve: Serve) -> None:
    stalled = Paced(ndjson(piece("Hel")), 5.0, ndjson(DONE))
    ollama = serve(lambda _request: httpx2.Response(200, stream=stalled), timeouts=FAST)

    with pytest.raises(StreamInterruptedError, match=r"sent nothing for 0\.05 s"):
        await collect(ollama.stream(ask("hi")))
    assert stalled.closed


async def test_a_slow_reader_is_not_mistaken_for_a_stalled_server(serve: Serve) -> None:
    """The deadlines cover waiting for Ollama, never the time the caller takes per event."""
    ollama = serve(answering(piece("a"), piece("b"), piece("c"), DONE), timeouts=FAST)
    text = ""
    async with aclosing(ollama.stream(ask("hi"))) as events:
        async for event in events:
            await asyncio.sleep(0.1)  # twice the idle limit
            if isinstance(event, TextDelta):
                text += event.text

    assert text == "abc"


async def test_closing_the_stream_closes_the_connection(serve: Serve) -> None:
    """That is how a turn is stopped: Ollama stops generating when the client hangs up."""
    endless = Paced(ndjson(piece("one")), ndjson(piece("two")), 60.0)
    ollama = serve(lambda _request: httpx2.Response(200, stream=endless))

    stream = ollama.stream(ask("hi"))
    assert await anext(stream) == TextDelta("one")
    assert not endless.closed
    await stream.aclose()

    assert endless.closed
