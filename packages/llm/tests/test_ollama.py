"""The Ollama adapter.

The first half replays cassettes: real exchanges recorded from an Ollama server (see
conftest.py). The second half feeds the adapter responses written here, for cases a
healthy server won't produce on demand: broken streams, timeouts, old versions.
"""

import asyncio
import json
import time
import zlib
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import aclosing
from typing import Any

import httpx2
import pytest

from actio_llm import (
    Assistant,
    ChatRequest,
    ConnectionFailedError,
    ContextOverflowError,
    Finish,
    FinishReason,
    LoadsAhead,
    ModelNotFoundError,
    Ollama,
    OpenAICompatible,
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
    TookTooLongError,
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


async def test_a_model_is_loaded_ahead_of_a_question(ollama_with: Any) -> None:
    """A chat request with no messages: the server loads the model, and answers one line
    that says so. The request is the cassette's, so a change to it fails here too."""
    ollama, tape = ollama_with("load")
    await ollama.load(MODEL, context_window=CONTEXT)

    [answer] = tape.chunks
    assert (answer["done"], answer["done_reason"]) == (True, "load")
    assert answer["message"]["content"] == ""


async def test_loading_a_model_the_server_has_not_got_says_so(ollama_with: Any) -> None:
    ollama, _ = ollama_with("load_unknown_model")
    with pytest.raises(ModelNotFoundError, match="not found") as caught:
        await ollama.load("no-such-model:1b", context_window=CONTEXT)

    assert caught.value.model == "no-such-model:1b"


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
        (400, "[" * 200_000, RequestRejectedError),  # too deep to read: no `ValueError`
        (400, '{"error":"prompt exceeds the available context size"}', ContextOverflowError),
    ],
)
async def test_http_errors_become_typed_errors(
    serve: Serve, status: int, body: str, error: type[Exception]
) -> None:
    ollama = serve(lambda _request: httpx2.Response(status, content=body.encode()))
    with pytest.raises(error):
        await collect(ollama.stream(ask("hi")))


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
async def test_an_unreachable_server_is_unavailable(
    serve: Serve, failure: type[httpx2.TransportError], error: type[Exception], call: str
) -> None:
    """A connection that failed at once, with nothing sent, is a kind of its own, which
    is worth asking again at once. Any other failure of the connection before an answer
    is a plain one: its time is spent, or the request may have been read."""

    def fail(request: httpx2.Request) -> httpx2.Response:
        raise failure("the connection failed", request=request)

    ollama = serve(fail, check_version=False)

    async def called() -> object:
        if call == "a chat":
            return await collect(ollama.stream(ask("hi")))
        return await ollama.describe(MODEL)

    with pytest.raises(ProviderUnavailableError, match="can't reach Ollama") as caught:
        await called()

    assert type(caught.value) is error


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


# A body that says it is compressed, and can't be unpacked. The HTTP client has an
# error of its own for that, which is none of its errors of the connection: unwrapped,
# it failed a turn as a mistake of Actio's, and the list of models with a status 500.

GZIPPED = {"content-encoding": "gzip"}


def packed_badly(status: int = 200) -> Handler:
    """Answers with a body that was never gzip, as one read from the network: a response
    made with its content at hand is unpacked as it is made, in the test itself."""
    return lambda _request: httpx2.Response(
        status, headers=GZIPPED, stream=Paced(b"this was never gzip")
    )


async def test_an_answer_that_cant_be_unpacked_is_the_providers_failure(serve: Serve) -> None:
    with pytest.raises(ProviderError, match="Ollama sent an answer that can't be unpacked"):
        await collect(serve(packed_badly()).stream(ask("hi")))


async def test_an_answer_that_cant_be_unpacked_half_way_is_an_interruption(serve: Serve) -> None:
    packer = zlib.compressobj(wbits=31)  # gzip
    first = packer.compress(ndjson(piece("Hel"))) + packer.flush(zlib.Z_SYNC_FLUSH)
    broken = Paced(first, b"\xff no longer gzip \xff")
    ollama = serve(lambda _request: httpx2.Response(200, headers=GZIPPED, stream=broken))
    received: list[StreamEvent] = []

    async def read() -> None:
        async for event in ollama.stream(ask("hi")):
            received.append(event)  # noqa: PERF401 - the list must outlive the exception

    with pytest.raises(StreamInterruptedError, match="couldn't be unpacked half-way"):
        await read()

    assert received == [TextDelta("Hel")]


async def test_a_small_call_answered_with_what_cant_be_unpacked_is_the_providers_failure(
    serve: Serve,
) -> None:
    with pytest.raises(ProviderError, match=r"answered /api/show with something that can't be"):
        await serve(packed_badly()).describe(MODEL)


async def test_a_refusal_that_cant_be_unpacked_is_the_providers_failure(serve: Serve) -> None:
    """The body of an error is read to say what the server refused."""
    with pytest.raises(ProviderError, match="Ollama sent an answer that can't be unpacked"):
        await collect(serve(packed_badly(400)).stream(ask("hi")))


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
        b"[" * 200_000 + b"\n",  # too deep for the JSON reader: no `ValueError`
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


# --- Loading a model ahead -----------------------------------------------------------

LOADED: dict[str, Any] = {
    "message": {"role": "assistant", "content": ""},
    "done": True,
    "done_reason": "load",
}
"""What Ollama 0.40.16 answers a chat request with no messages, but its two moments."""


async def test_ollama_can_load_ahead_and_a_server_that_speaks_openais_api_cannot() -> None:
    async with httpx2.AsyncClient(base_url="http://ollama.test") as client:
        assert isinstance(Ollama(client), LoadsAhead)
        assert not isinstance(OpenAICompatible(client, provider="Lab"), LoadsAhead)


async def test_a_load_is_the_request_a_question_would_be_without_its_messages(
    serve: Serve,
) -> None:
    """Whatever decides whether the server loads the model again must be the same in
    both. Measured on Ollama 0.40.16: after a load without `"shift": false`, the next
    question loaded the model again, for 1.4 to 3.4 s (ADR 0026)."""
    sent: list[dict[str, Any]] = []

    def chat(request: httpx2.Request) -> httpx2.Response:
        sent.append(json.loads(request.content))
        return httpx2.Response(200, content=ndjson(DONE if sent[-1]["messages"] else LOADED))

    ollama = serve(chat, keep_alive="30m")
    await ollama.load("qwen3.5:4b", context_window=8192)
    question = ChatRequest(model="qwen3.5:4b", messages=[User("hi")], context_window=8192)
    await collect(ollama.stream(question))

    loaded, asked = sent
    assert loaded["messages"] == []
    assert {**loaded, "messages": asked["messages"]} == asked
    assert (loaded["shift"], loaded["options"]["num_ctx"], loaded["keep_alive"]) == (
        False,
        8192,
        "30m",
    )


async def test_a_load_is_waited_for_as_long_as_a_first_word_not_as_a_small_call() -> None:
    """A cold load took up to 3.1 s, measured, and the catalog gives the small calls, such
    as asking whether a model is there, 3 s. Here the server takes longer than the small
    calls' patience, and much less than the first word's. A real connection: only the
    HTTP client's own transport keeps its read timeout."""

    async def answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = (await reader.readuntil(b"\r\n\r\n")).lower()
        await reader.readexactly(int(head.split(b"content-length: ")[1].split(b"\r\n")[0]))
        await asyncio.sleep(0.3)  # loading
        body = ndjson(LOADED)
        writer.write(
            b"HTTP/1.1 200 OK\r\ncontent-type: application/x-ndjson\r\n"
            b"content-length: %d\r\n\r\n%b" % (len(body), body)
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(answer, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    patience = Timeouts(request_s=0.05, first_event_s=5.0)
    async with (
        asyncio.timeout(5),
        server,
        httpx2.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client,
    ):
        await Ollama(client, timeouts=patience, check_version=False).load(
            MODEL, context_window=CONTEXT
        )


async def test_a_load_waits_no_longer_than_a_whole_answer_may_take(serve: Serve) -> None:
    """A load is read as an answer's first line is: within the shorter of the time to
    begin and the time for a whole answer. A server given less for an answer than to
    begin one has its load cut at the shorter."""
    silent = Paced(60.0, ndjson(LOADED))
    ollama = serve(
        lambda _request: httpx2.Response(200, stream=silent),
        timeouts=Timeouts(first_event_s=5.0, answer_s=0.05),
    )
    loop = asyncio.get_running_loop()
    began = loop.time()

    with pytest.raises(ProviderUnavailableError, match=r"didn't start answering within 0\.05 s"):
        async with asyncio.timeout(5):
            await ollama.load(MODEL, context_window=CONTEXT)

    assert loop.time() - began < 1.0
    assert silent.closed


@pytest.mark.parametrize(
    "chunks",
    [[], [{"error": "llama runner process has terminated"}]],
    ids=["nothing", "an error"],
)
async def test_a_load_the_server_never_says_it_made_is_an_interruption(
    serve: Serve, chunks: list[dict[str, Any]]
) -> None:
    with pytest.raises(StreamInterruptedError):
        await serve(answering(*chunks)).load(MODEL, context_window=CONTEXT)


async def test_an_old_server_is_refused_before_a_load_is_sent(serve: Serve) -> None:
    asked: list[httpx2.Request] = []

    def chat(request: httpx2.Request) -> httpx2.Response:
        asked.append(request)
        return httpx2.Response(200, content=ndjson(LOADED))

    with pytest.raises(UnsupportedServerError):
        await serve(chat, version="0.31.2").load(MODEL, context_window=CONTEXT)
    assert asked == []


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
    """The time to begin and the time between two lines cover waiting for Ollama, never
    the time the caller takes per event. The time for a whole answer, further down,
    counts that too."""
    ollama = serve(answering(piece("a"), piece("b"), piece("c"), DONE), timeouts=FAST)
    text = ""
    async with aclosing(ollama.stream(ask("hi"))) as events:
        async for event in events:
            await asyncio.sleep(0.1)  # twice the idle limit
            if isinstance(event, TextDelta):
                text += event.text

    assert text == "abc"


PATIENCE_S = 5.0
"""How long a test of the time a whole answer is given waits for the client to end the
answer. Its own times are tenths of a second: this keeps a client that reads such an
answer for ever from hanging the run."""


class Endless(httpx2.AsyncByteStream):
    """A response body that never ends: the same line, again and again, every `every_s`
    seconds, as a server that keeps sending does. The first comes after `after_s`.

    It counts the lines it sent (`sent`): a busy machine sends fewer in a given time,
    never more, so a count that is too high is a time that was too long."""

    def __init__(self, line: bytes, *, every_s: float, after_s: float = 0.0) -> None:
        self._line = line
        self._every_s = every_s
        self._after_s = after_s
        self.sent = 0
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        await asyncio.sleep(self._after_s)
        while True:
            self.sent += 1
            yield self._line
            await asyncio.sleep(self._every_s)

    async def aclose(self) -> None:
        self.closed = True


class HeldUp(httpx2.AsyncByteStream):
    """A response body whose one part is read only after the event loop was held for
    `held_s`, as when the process was busy elsewhere. Nothing waits meanwhile, so no time
    limit around the read can fire: only the clock says how late it is."""

    def __init__(self, part: bytes, *, held_s: float) -> None:
        self._part = part
        self._held_s = held_s
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        time.sleep(self._held_s)  # noqa: ASYNC251 - the loop is held on purpose
        yield self._part

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    ("line", "words"),
    [(ndjson(piece("again ")), True), (ndjson(piece("")), False)],
    ids=["words", "lines with no word"],
)
async def test_an_answer_that_goes_on_past_its_time_is_cut_there(
    serve: Serve, line: bytes, words: bool
) -> None:
    """A line a hundred times a second, far more often than the gap a server may leave,
    and never an end: once the answer's time is over, and not before, the answer is
    cut, and its connection closed. Lines with no word in them hand nothing on."""
    endless = Endless(line, every_s=0.01)
    timeouts = Timeouts(idle_s=0.1, answer_s=0.3)
    ollama = serve(lambda _request: httpx2.Response(200, stream=endless), timeouts=timeouts)
    received: list[StreamEvent] = []
    loop = asyncio.get_running_loop()
    began = loop.time()

    async def read() -> None:
        async with asyncio.timeout(PATIENCE_S), aclosing(ollama.stream(ask("hi"))) as events:
            async for event in events:
                received.append(event)  # noqa: PERF401 - the list must outlive the exception

    with pytest.raises(TookTooLongError, match=r"Ollama was still answering after 0\.3 s"):
        await read()

    assert 0.3 <= loop.time() - began < 1.0
    # A line every hundredth of a second for three tenths of one: thirty at the most,
    # and some sixty if the answer were given twice its time.
    assert 1 <= endless.sent <= 40
    assert endless.closed
    if words:
        assert received
        assert set(received) == {TextDelta("again ")}
    else:
        assert received == []


async def test_the_answers_time_counts_from_the_request_not_from_its_first_line(
    serve: Serve,
) -> None:
    """A server that begins late has what is left of the time, not the whole of it again.
    Its first line comes after half a second, of one second for the answer, and then a
    line a hundred times a second: fifty more at the most, and a hundred if the time
    began with the first line."""
    endless = Endless(ndjson(piece("again ")), every_s=0.01, after_s=0.5)
    ollama = serve(
        lambda _request: httpx2.Response(200, stream=endless), timeouts=Timeouts(answer_s=1.0)
    )

    with pytest.raises(TookTooLongError, match=r"Ollama was still answering after 1 s"):
        async with asyncio.timeout(PATIENCE_S):
            await collect(ollama.stream(ask("hi")))

    assert 1 <= endless.sent <= 65
    assert endless.closed


async def test_a_first_line_read_after_the_answers_time_is_a_server_away_not_an_answer_cut(
    serve: Serve,
) -> None:
    """Nothing was handed on, so the server didn't begin in time, however the client
    finds that out: by a wait that runs out, or, as here, by the clock, when a line that
    took too long is read without a wait."""
    late = HeldUp(ndjson(piece("Hel"), DONE), held_s=0.15)
    ollama = serve(
        lambda _request: httpx2.Response(200, stream=late), timeouts=Timeouts(answer_s=0.05)
    )

    with pytest.raises(
        ProviderUnavailableError, match=r"didn't start answering within 0\.05 s"
    ) as caught:
        async with asyncio.timeout(PATIENCE_S):
            await collect(ollama.stream(ask("hi")))

    assert type(caught.value) is ProviderUnavailableError
    assert late.closed


async def test_what_had_arrived_when_the_answers_time_was_over_is_not_handed_on(
    serve: Serve,
) -> None:
    """A reader that falls behind: forty words arrive at once, and the reader is still at
    the first when the answer's time is over. They are read without a wait, so a time
    limit around each read alone would hand every one of them on."""
    body = Paced(ndjson(*(piece(f"w{number} ") for number in range(40))), 60.0)
    ollama = serve(
        lambda _request: httpx2.Response(200, stream=body), timeouts=Timeouts(answer_s=0.1)
    )

    async with asyncio.timeout(PATIENCE_S), aclosing(ollama.stream(ask("hi"))) as events:
        first = await anext(events)
        await asyncio.sleep(0.2)  # the reader's own work, past the answer's time
        with pytest.raises(TookTooLongError):
            await anext(events)

    assert first == TextDelta("w0 ")
    assert body.closed


async def test_a_server_that_hasnt_begun_when_the_answers_time_is_over_is_unavailable(
    serve: Serve,
) -> None:
    """The first line has to come within the shorter of the two times: then nothing has
    been handed on, and the server counts as away, as after its time to begin."""
    silent = Paced(60.0, ndjson(DONE))
    ollama = serve(
        lambda _request: httpx2.Response(200, stream=silent),
        timeouts=Timeouts(first_event_s=5.0, answer_s=0.05),
    )
    loop = asyncio.get_running_loop()
    began = loop.time()

    with pytest.raises(
        ProviderUnavailableError, match=r"didn't start answering within 0\.05 s"
    ) as caught:
        async with asyncio.timeout(PATIENCE_S):
            await collect(ollama.stream(ask("hi")))

    assert type(caught.value) is ProviderUnavailableError
    assert loop.time() - began < 1.0
    assert silent.closed


@pytest.mark.parametrize(
    ("timeouts", "error", "message"),
    [
        (
            Timeouts(idle_s=0.05, answer_s=3.0),
            StreamInterruptedError,
            r"Ollama sent nothing for 0\.05 s",
        ),
        (
            Timeouts(idle_s=3.0, answer_s=0.1),
            TookTooLongError,
            r"Ollama was still answering after 0\.1 s",
        ),
    ],
    ids=["the gap is over first", "the answer's time is over first"],
)
async def test_a_silence_in_an_answer_is_whichever_time_is_over_first(
    serve: Serve, timeouts: Timeouts, error: type[StreamInterruptedError], message: str
) -> None:
    """And it ends when that time is over, not when the other is: each of the two is
    three seconds here when it is the longer one."""
    stalled = Paced(ndjson(piece("Hel")), 60.0, ndjson(DONE))
    ollama = serve(lambda _request: httpx2.Response(200, stream=stalled), timeouts=timeouts)
    loop = asyncio.get_running_loop()
    began = loop.time()

    with pytest.raises(StreamInterruptedError, match=message) as caught:
        async with asyncio.timeout(PATIENCE_S):
            await collect(ollama.stream(ask("hi")))

    assert type(caught.value) is error
    assert loop.time() - began < 1.0
    assert stalled.closed


@pytest.mark.parametrize("answer_s", [0.5, 300.0])
async def test_an_answer_that_ends_within_its_time_is_read_whole(
    serve: Serve, answer_s: float
) -> None:
    """And the client says how long it was given, as it says its other times. Left out,
    an answer is given five minutes."""
    paced = Paced(ndjson(piece("a")), 0.02, ndjson(piece("b")), 0.02, ndjson(piece("c"), DONE))
    timeouts = Timeouts(answer_s=answer_s)
    ollama = serve(lambda _request: httpx2.Response(200, stream=paced), timeouts=timeouts)

    events = await collect(ollama.stream(ask("hi")))

    assert text_of(events) == "abc"
    assert isinstance(events[-1], Finish)
    assert ollama.timeouts == timeouts
    assert ollama.timeouts.answer_s == answer_s
    assert Timeouts().answer_s == 300.0


async def test_closing_the_stream_closes_the_connection(serve: Serve) -> None:
    """That is how a turn is stopped: Ollama stops generating when the client hangs up."""
    endless = Paced(ndjson(piece("one")), ndjson(piece("two")), 60.0)
    ollama = serve(lambda _request: httpx2.Response(200, stream=endless))

    stream = ollama.stream(ask("hi"))
    assert await anext(stream) == TextDelta("one")
    assert not endless.closed
    await stream.aclose()

    assert endless.closed
