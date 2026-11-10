"""Any server that speaks OpenAI's Chat Completions API.

One adapter serves the test fake, vLLM and the hosted providers, because they share a
wire format. Where they differ, it reads leniently and sends explicitly:

- **Reasoning** arrives as `reasoning` (vLLM, Groq and OpenRouter today) or under the
  older name `reasoning_content`. Both are read.
- **A tool call** arrives in fragments: its name first, then pieces of its arguments'
  JSON text. They are joined and parsed, and the call is yielded whole.
- **Token counts** are asked for with `stream_options.include_usage`. A server that
  ignores the option gets `Finish.usage = None`, never zeros.
- **The output limit** is sent as `max_completion_tokens`. For a server that only knows
  the older name, pass `output_limit_field="max_tokens"`.

This is not the adapter for Ollama: its OpenAI-compatible endpoint can't set the context
window, and cuts long prompts silently. See `actio_llm.ollama` and ADR 0007.
"""

import json
from collections.abc import AsyncGenerator
from contextlib import aclosing
from dataclasses import dataclass, field
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
    RateLimitedError,
    RequestRejectedError,
    StreamInterruptedError,
)
from actio_llm.wire import Timeouts, request_json, stream_lines


class OpenAICompatible:
    """Chat with the models of one OpenAI-compatible server.

    `client` is an `httpx2.AsyncClient` whose `base_url` ends where the API starts, such
    as `https://api.example.com/v1`, and which already carries the credentials
    (`headers={"Authorization": "Bearer ..."}`). The caller owns and closes it.

    `provider` names the server in error messages.
    """

    def __init__(
        self,
        client: httpx2.AsyncClient,
        *,
        provider: str = "the provider",
        timeouts: Timeouts | None = None,
        output_limit_field: str = "max_completion_tokens",
    ) -> None:
        self._client = client
        self._provider = provider
        self._timeouts = timeouts or Timeouts()
        self._output_limit_field = output_limit_field

    async def stream(self, request: ChatRequest) -> AsyncGenerator[StreamEvent]:
        """Send `request` and yield the answer as it is written; a `Finish` comes last.

        `request.context_window` is ignored: these servers fix it per model.
        `request.thinking` becomes `reasoning_effort`: `False` sends `"none"`, a level is
        sent as it is, and `True` leaves the server's default.
        """
        parser = _Parser(self._provider)
        lines = stream_lines(
            self._client,
            "/chat/completions",
            self._body(request),
            provider=self._provider,
            timeouts=self._timeouts,
            rejection=lambda response, text: self._rejection(response, text, request.model),
        )
        async with aclosing(lines):  # closing this stream must close the connection too
            async for line in lines:
                for event in parser.feed(line):
                    yield event
                if parser.done:
                    break
        yield parser.finish()

    async def models(self) -> list[str]:
        """The ids of the models the server offers. Also a cheap health check."""
        data = await request_json(
            self._client,
            "GET",
            "/models",
            provider=self._provider,
            timeouts=self._timeouts,
            rejection=lambda response, text: self._rejection(response, text, ""),
        )
        return [str(entry.get("id", "")) for entry in data.get("data") or ()]

    def _body(self, request: ChatRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": request.model,
            "messages": [_message(message) for message in request.messages],
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if request.max_output_tokens is not None:
            body[self._output_limit_field] = request.max_output_tokens
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.top_p is not None:
            body["top_p"] = request.top_p
        if request.seed is not None:
            body["seed"] = request.seed
        if request.stop:
            body["stop"] = list(request.stop)
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
        if request.thinking is False:
            body["reasoning_effort"] = "none"
        elif isinstance(request.thinking, str):
            body["reasoning_effort"] = request.thinking
        return body

    def _rejection(self, response: httpx2.Response, text: str, model: str) -> LLMError:
        """The error for a response that isn't 200. Bodies are `{"error": {...}}`."""
        status = response.status_code
        message, code = _error_parts(text)
        detail = f"{self._provider} answered HTTP {status}: {message}"
        if status == 429:
            return RateLimitedError(
                detail, retry_after_s=_seconds(response.headers.get("retry-after"))
            )
        if status in (401, 403):
            # Not the request's fault, and another provider can still serve it.
            return ProviderError(f"{self._provider} refused Actio's credentials: {detail}")
        if status == 404 and (code == "model_not_found" or "model" in message.lower()):
            return ModelNotFoundError(model, detail)
        if status in (502, 503, 504, 529):
            return ProviderUnavailableError(detail)
        if status >= 500 or status == 404:  # a 404 that isn't about the model: a wrong URL
            return ProviderError(detail)
        if code == "context_length_exceeded" or "context length" in message.lower():
            return ContextOverflowError(detail)
        return RequestRejectedError(detail)


@dataclass
class _PartialCall:
    """A tool call that is still arriving."""

    id: str = ""
    name: str = ""
    arguments: list[str] = field(default_factory=list)


class _Parser:
    """Turns the server-sent events of one chat completion stream into stream events."""

    def __init__(self, provider: str) -> None:
        self.done = False
        """True once the server sent its `[DONE]` marker."""
        self._provider = provider
        self._reason: str | None = None
        self._usage: Usage | None = None
        self._partial: dict[int, _PartialCall] = {}
        self._called = False

    def feed(self, line: str) -> list[StreamEvent]:
        name, _, value = line.partition(":")
        if name != "data":  # a comment used as a keep-alive, or an `event:` line
            return []
        data = value.strip()
        if data == "[DONE]":
            self.done = True
            return []
        try:
            chunk = json.loads(data)
        except ValueError as error:
            raise ProviderError(
                f"{self._provider} sent an event that isn't JSON: {data[:200]!r}"
            ) from error
        if not isinstance(chunk, dict):
            raise ProviderError(f"{self._provider} sent an event that isn't a JSON object")
        if chunk.get("error"):
            message, _ = _error_parts(json.dumps(chunk))
            raise StreamInterruptedError(
                f"{self._provider} reported an error mid-answer: {message}"
            )

        if isinstance(chunk.get("usage"), dict):
            self._usage = _usage(chunk["usage"])
        events: list[StreamEvent] = []
        for choice in (chunk.get("choices") or ())[:1]:  # one answer was asked for
            delta = choice.get("delta") or {}
            reasoning = delta.get("reasoning") or delta.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning:
                events.append(ReasoningDelta(reasoning))
            if isinstance(content := delta.get("content"), str) and content:
                events.append(TextDelta(content))
            for part in delta.get("tool_calls") or ():
                self._collect(part)
            if choice.get("finish_reason"):
                self._reason = str(choice["finish_reason"])
                events.extend(self._finished_calls())
        return events

    def finish(self) -> Finish:
        """The stream's last event. Call it when the stream has ended."""
        if self._reason is None:
            raise StreamInterruptedError(f"{self._provider}'s answer ended before it finished")
        if self._reason == "length":
            reason = FinishReason.LENGTH
        elif self._reason == "content_filter":
            reason = FinishReason.CONTENT_FILTER
        elif self._called:  # whatever the server calls it: some say "stop" here
            reason = FinishReason.TOOL_CALLS
        else:
            reason = FinishReason.STOP
        return Finish(reason, self._usage)

    def _collect(self, part: Any) -> None:
        """Add one fragment to the tool call it belongs to, by its `index`."""
        if not isinstance(part, dict):
            raise ProviderError(f"{self._provider} sent a tool call that can't be read: {part!r}")
        index = part.get("index")
        call = self._partial.setdefault(index if isinstance(index, int) else 0, _PartialCall())
        function = part.get("function") or {}
        call.id = call.id or str(part.get("id") or "")
        call.name = call.name or str(function.get("name") or "")
        if isinstance(fragment := function.get("arguments"), str):
            call.arguments.append(fragment)

    def _finished_calls(self) -> list[StreamEvent]:
        calls: list[StreamEvent] = []
        for number, (_, call) in enumerate(sorted(self._partial.items()), start=1):
            text = "".join(call.arguments) or "{}"
            try:
                arguments = json.loads(text)
            except ValueError:
                arguments = None
            if not call.name or not isinstance(arguments, dict):
                raise ProviderError(
                    f"{self._provider} sent a tool call that can't be read "
                    f"(was the answer cut off?): {call.name or '(no name)'}({text[:200]})"
                )
            calls.append(ToolCall(call.id or f"call_{number}", call.name, arguments))
        self._partial.clear()
        self._called = self._called or bool(calls)
        return calls


def _message(message: Message) -> dict[str, Any]:
    match message:
        case System(content=content):
            return {"role": "system", "content": content}
        case User(content=content):
            return {"role": "user", "content": content}
        case Assistant():
            # Earlier reasoning isn't sent back: this format has no agreed field for it.
            if not message.tool_calls:
                return {"role": "assistant", "content": message.content}
            return {
                "role": "assistant",
                "content": message.content or None,
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(dict(call.arguments)),
                        },
                    }
                    for call in message.tool_calls
                ],
            }
        case ToolResult():
            return {"role": "tool", "tool_call_id": message.call_id, "content": message.content}


def _usage(reported: dict[str, Any]) -> Usage | None:
    prompt, completion = reported.get("prompt_tokens"), reported.get("completion_tokens")
    if not isinstance(prompt, int) or not isinstance(completion, int):
        return None
    cached = (reported.get("prompt_tokens_details") or {}).get("cached_tokens")
    return Usage(
        input_tokens=prompt,
        output_tokens=completion,  # reasoning tokens are already part of it
        cached_input_tokens=min(cached, prompt) if isinstance(cached, int) else 0,
    )


def _error_parts(body: str) -> tuple[str, str | None]:
    """The message and the code of an error body, whatever its exact shape."""
    try:
        data = json.loads(body)
    except ValueError:
        data = None
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        code = error.get("code")
        return error["message"], code if isinstance(code, str) else None
    if isinstance(error, str):
        return error, None
    return body.strip()[:500] or "(empty response)", None


def _seconds(value: str | None) -> float | None:
    """A `Retry-After` header given in seconds. The date form is ignored."""
    try:
        seconds = float(value) if value is not None else None
    except ValueError:
        return None
    return seconds if seconds is not None and seconds >= 0 else None
