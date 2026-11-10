"""Ollama, through its native API, with silent truncation turned off.

Ollama also offers OpenAI-compatible endpoints, but only `/api/chat` can set the context
window per request, and a model loaded with the default window (4,096 tokens on a 16 GB
Mac) quietly drops the start of a longer prompt, system prompt included, and still
answers 200.

So every chat request here carries two flags, measured against Ollama 0.40.16:

- `"truncate": false`: a prompt that doesn't fit is refused with HTTP 400, which becomes
  `ContextOverflowError`, instead of being cut;
- `"shift": false`: an answer that fills the window stops there (`FinishReason.LENGTH`)
  instead of pushing the oldest tokens out to keep writing.

Older servers may ignore the flags, which would bring silent truncation back, so the
adapter refuses servers older than `MIN_VERSION`. See ADR 0007.
"""

import json
import re
from collections.abc import AsyncGenerator
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any

import httpx2

from actio_llm.chat import (
    Assistant,
    ChatRequest,
    Finish,
    FinishReason,
    Message,
    ReasoningDelta,
    StreamEvent,
    System,
    TextDelta,
    Timings,
    ToolCall,
    ToolResult,
    Usage,
    User,
)
from actio_llm.errors import (
    ContextOverflowError,
    LLMError,
    ModelNotFoundError,
    ProviderError,
    ProviderUnavailableError,
    RequestRejectedError,
    StreamInterruptedError,
    UnsupportedFeatureError,
    UnsupportedServerError,
)
from actio_llm.wire import Timeouts, request_json, stream_lines

MIN_VERSION = (0, 40, 0)
"""The oldest Ollama this adapter accepts: the release line its strict flags were measured on."""

_OVERFLOW = re.compile(r"\((\d+) tokens\) exceeds the available context size \((\d+) tokens\)")
_VERSION = re.compile(r"\d+(?:\.\d+)*")


@dataclass(frozen=True, slots=True)
class LocalModel:
    """A model on the server's disk, from `GET /api/tags`."""

    name: str
    size_bytes: int
    parameter_size: str | None
    quantization: str | None


@dataclass(frozen=True, slots=True)
class ModelInfo:
    """What a model can do, from `POST /api/show`."""

    name: str
    capabilities: frozenset[str]
    """Such as `completion`, `tools`, `thinking`, `vision`."""
    context_length: int | None
    """The largest context window the model was built for. What it runs with is the
    request's `context_window`."""
    thinking_levels: tuple[str, ...]
    """Named reasoning levels, such as `low`, for models that have them."""
    parameter_size: str | None
    quantization: str | None

    def can(self, capability: str) -> bool:
        return capability in self.capabilities


class Ollama:
    """Chat with the models of one Ollama server.

    `client` is an `httpx2.AsyncClient` whose `base_url` is the server, such as
    `http://127.0.0.1:11434`. The caller owns and closes it.
    """

    def __init__(
        self,
        client: httpx2.AsyncClient,
        *,
        keep_alive: str | int = "5m",
        timeouts: Timeouts | None = None,
        check_version: bool = True,
    ) -> None:
        self._client = client
        self._keep_alive = keep_alive
        self._timeouts = timeouts or Timeouts()
        self._version_ok = not check_version

    # --- Chat ------------------------------------------------------------------------

    async def stream(self, request: ChatRequest) -> AsyncGenerator[StreamEvent]:
        """Send `request` and yield the answer as it is written; a `Finish` comes last.

        Closing the generator closes the connection, and Ollama stops generating.
        """
        await self._ensure_supported()
        parser = _Parser()
        lines = stream_lines(
            self._client,
            "/api/chat",
            _body(request, self._keep_alive),
            provider="Ollama",
            timeouts=self._timeouts,
            rejection=lambda response, text: _rejection(response.status_code, text, request.model),
        )
        async with aclosing(lines):  # closing this stream must close the connection too
            async for line in lines:
                for event in parser.feed(line):
                    yield event
                if parser.finished:
                    return
        raise StreamInterruptedError("Ollama's answer ended without its final chunk")

    # --- The server and its models ---------------------------------------------------

    async def version(self) -> tuple[int, ...]:
        """The server's version, such as `(0, 40, 16)`."""
        data = await self._json("GET", "/api/version")
        match = _VERSION.match(str(data.get("version", "")))
        if match is None:
            raise ProviderError(f"Ollama reported a version that can't be read: {data!r}")
        parts = tuple(int(part) for part in match[0].split("."))
        return parts + (0,) * (3 - len(parts))

    async def models(self) -> list[LocalModel]:
        """The models on the server's disk."""
        data = await self._json("GET", "/api/tags")
        found = []
        for entry in data.get("models") or ():
            details = entry.get("details") or {}
            found.append(
                LocalModel(
                    name=str(entry.get("name", "")),
                    size_bytes=int(entry.get("size", 0)),
                    parameter_size=details.get("parameter_size"),
                    quantization=details.get("quantization_level"),
                )
            )
        return found

    async def describe(self, model: str) -> ModelInfo:
        """What `model` can do. Ask before sending `thinking` or tools to a model."""
        data = await self._json("POST", "/api/show", body={"model": model}, model=model)
        details = data.get("details") or {}
        info = data.get("model_info") or {}
        # The key carries the model's architecture, such as `granite.context_length`.
        context = next(
            (v for k, v in info.items() if k.endswith(".context_length") and isinstance(v, int)),
            None,
        )
        thinking = data.get("thinking") or {}
        levels = tuple(v for v in thinking.get("values") or () if isinstance(v, str))
        return ModelInfo(
            name=model,
            capabilities=frozenset(data.get("capabilities") or ()),
            context_length=context,
            thinking_levels=levels,
            parameter_size=details.get("parameter_size"),
            quantization=details.get("quantization_level"),
        )

    # --- Plumbing --------------------------------------------------------------------

    async def _ensure_supported(self) -> None:
        if self._version_ok:
            return
        version = await self.version()
        if version < MIN_VERSION:
            raise UnsupportedServerError(
                f"Ollama {_dotted(version)} is older than {_dotted(MIN_VERSION)}, the oldest "
                "version tested to refuse a prompt that doesn't fit instead of cutting it. "
                "Upgrade Ollama."
            )
        self._version_ok = True

    async def _json(
        self, method: str, path: str, *, body: dict[str, Any] | None = None, model: str = ""
    ) -> dict[str, Any]:
        return await request_json(
            self._client,
            method,
            path,
            body=body,
            provider="Ollama",
            timeouts=self._timeouts,
            rejection=lambda response, text: _rejection(response.status_code, text, model),
        )


class _Parser:
    """Turns the lines of one `/api/chat` stream into events."""

    def __init__(self) -> None:
        self.finished = False
        self._tool_calls = 0

    def feed(self, line: str) -> list[StreamEvent]:
        try:
            chunk = json.loads(line)
        except ValueError as error:
            raise ProviderError(f"Ollama sent a line that isn't JSON: {line[:200]!r}") from error
        if not isinstance(chunk, dict):
            raise ProviderError(f"Ollama sent a line that isn't a JSON object: {line[:200]!r}")
        if "error" in chunk:
            raise StreamInterruptedError(f"Ollama reported an error mid-answer: {chunk['error']}")

        events: list[StreamEvent] = []
        message = chunk.get("message") or {}
        if thinking := message.get("thinking"):
            events.append(ReasoningDelta(thinking))
        if content := message.get("content"):
            events.append(TextDelta(content))
        # A tool call arrives whole, in one chunk, with its arguments already parsed.
        events.extend(self._tool_call(call) for call in message.get("tool_calls") or ())
        if chunk.get("done"):
            self.finished = True
            events.append(self._finish(chunk))
        return events

    def _tool_call(self, call: Any) -> ToolCall:
        function = call.get("function") if isinstance(call, dict) else None
        name = function.get("name") if isinstance(function, dict) else None
        arguments = function.get("arguments", {}) if isinstance(function, dict) else None
        if not isinstance(name, str) or not name or not isinstance(arguments, dict):
            raise ProviderError(f"Ollama sent a tool call that can't be read: {call!r}")
        self._tool_calls += 1
        return ToolCall(
            id=call.get("id") or f"call_{self._tool_calls}", name=name, arguments=arguments
        )

    def _finish(self, chunk: dict[str, Any]) -> Finish:
        if chunk.get("done_reason") == "length":
            reason = FinishReason.LENGTH
        elif self._tool_calls:
            # Ollama says "stop" here too; the neutral format names it, as OpenAI does.
            reason = FinishReason.TOOL_CALLS
        else:
            reason = FinishReason.STOP
        input_tokens = _count(chunk, "prompt_eval_count")
        usage = Usage(
            input_tokens=input_tokens,
            output_tokens=_count(chunk, "eval_count"),
            cached_input_tokens=min(_count(chunk, "prompt_eval_cached_count"), input_tokens),
        )
        timings = Timings(
            load_ms=_count(chunk, "load_duration") // 1_000_000,
            prompt_ms=_count(chunk, "prompt_eval_duration") // 1_000_000,
            generation_ms=_count(chunk, "eval_duration") // 1_000_000,
            total_ms=_count(chunk, "total_duration") // 1_000_000,
        )
        return Finish(reason, usage, timings)


def _count(chunk: dict[str, Any], key: str) -> int:
    """A counter from the final chunk; 0 when Ollama left it out."""
    value = chunk.get(key, 0)
    return value if isinstance(value, int) and value > 0 else 0


def _body(request: ChatRequest, keep_alive: str | int) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": request.model,
        "messages": [_message(message) for message in request.messages],
        "stream": True,
        "truncate": False,
        "shift": False,
        "keep_alive": keep_alive,
    }
    options: dict[str, Any] = {}
    if request.context_window is not None:
        options["num_ctx"] = request.context_window
    if request.max_output_tokens is not None:
        options["num_predict"] = request.max_output_tokens
    if request.temperature is not None:
        options["temperature"] = request.temperature
    if request.top_p is not None:
        options["top_p"] = request.top_p
    if request.seed is not None:
        options["seed"] = request.seed
    if request.stop:
        options["stop"] = list(request.stop)
    if options:
        body["options"] = options
    if request.tools:
        body["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": dict(tool.parameters),
                },
            }
            for tool in request.tools
        ]
    if request.thinking is not None:
        body["think"] = request.thinking
    return body


def _message(message: Message) -> dict[str, Any]:
    match message:
        case System(content=content):
            return {"role": "system", "content": content}
        case User(content=content):
            return {"role": "user", "content": content}
        case Assistant():
            out: dict[str, Any] = {"role": "assistant", "content": message.content}
            if message.reasoning:
                out["thinking"] = message.reasoning
            if message.tool_calls:
                out["tool_calls"] = [
                    {
                        "type": "function",
                        "function": {
                            "index": index,
                            "name": call.name,
                            "arguments": dict(call.arguments),
                        },
                    }
                    for index, call in enumerate(message.tool_calls)
                ]
            return out
        case ToolResult():
            # Ollama links a result to its call by the tool's name, not by an id.
            return {"role": "tool", "tool_name": message.name, "content": message.content}


def _rejection(status: int, body: str, model: str) -> LLMError:
    """The error for a response that isn't 200. Ollama's errors are `{"error": "..."}`."""
    detail = _error_text(body)
    if status == 404:
        return ModelNotFoundError(model, detail)
    if status in (502, 503, 504):
        return ProviderUnavailableError(
            f"Ollama is overloaded or restarting (HTTP {status}): {detail}"
        )
    if status >= 500:
        return ProviderError(f"Ollama failed (HTTP {status}): {detail}")
    if "exceeds the available context size" in detail:
        sizes = _OVERFLOW.search(detail)
        return ContextOverflowError(
            detail,
            prompt_tokens=int(sizes[1]) if sizes else None,
            context_window=int(sizes[2]) if sizes else None,
        )
    if "does not support" in detail:
        return UnsupportedFeatureError(detail)
    return RequestRejectedError(f"Ollama refused the request (HTTP {status}): {detail}")


def _error_text(body: str) -> str:
    try:
        data = json.loads(body)
    except ValueError:
        data = None
    if isinstance(data, dict) and isinstance(data.get("error"), str):
        return str(data["error"])
    return body.strip()[:500] or "(empty response)"


def _dotted(version: tuple[int, ...]) -> str:
    return ".".join(map(str, version))
