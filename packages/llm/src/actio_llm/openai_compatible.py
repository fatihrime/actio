"""Any server that speaks OpenAI's Chat Completions API.

One adapter serves the test fake, vLLM and the hosted providers, Groq's and the Gemini
API's OpenAI-compatible layers among them, because they share a wire format. Where they
differ, it reads leniently and sends explicitly:

- **Reasoning** arrives as `reasoning` (vLLM, Groq and OpenRouter today) or under the
  older name `reasoning_content`. Both are read. It may also arrive inside the text: a
  piece marked as a thought (`extra_content.google.thought`), as the Gemini API sends
  its thoughts when it is asked to, with `<thought>` at the start of the first one and
  `</thought>` at the start of the answer's first piece. Those pieces are reasoning:
  `<thought>` is taken off the start of a thought piece, `</thought>` off the start of
  the first piece of text after thoughts, and neither anywhere else.
- **A tool call** arrives in fragments: its name first, then pieces of its arguments'
  JSON text, each with the call's `index`. They are joined and parsed, and the call is
  yielded whole. A fragment without an `index`, as the Gemini API sends a whole call,
  starts a new call when it carries an id of its own.
- **Token counts** are asked for with `stream_options.include_usage`. A server that
  ignores the option gets `Finish.usage = None`, never zeros. A `completion_tokens`
  that leaves the reasoning out, as the Gemini API's does, is made whole from
  `total_tokens`.
- **Where the time went** is read when the usage says it in seconds, as Groq's does
  (`completion_time` and the others), and becomes `Finish.timings`.
- **The output limit** is sent as `max_completion_tokens`. For a server that only knows
  the older name, pass `output_limit_field="max_tokens"`.
- **What a model is told about reasoning** is the caller's to say, per model:
  `thinking_off` and `thinking_on`. Left out, off is `reasoning_effort: "none"`.
- **A refusal** is `{"error": {...}}`, or a list of one such object, as the Gemini
  API's chat answers. How long a 429 asks to wait is read from `Retry-After`, or else
  from the body's `google.rpc.RetryInfo`. The Gemini API may also send such a list in
  the middle of a stream, as plain lines that are no event, and end it there: the
  answer broke off, and the error's message says what the list said.
- **A model's id** in the list of models may be written `models/<name>`, as the Gemini
  API writes it, while its chat takes `<name>`. Another server may name a model so and
  take exactly that, as vLLM started with a path does: so the list holds both
  spellings of such an id.

This is not the adapter for Ollama: its OpenAI-compatible endpoint can't set the context
window, and cuts long prompts silently. See `actio_llm.ollama` and ADR 0007. The shapes
named above were seen on the real servers: ADR 0027.
"""

import copy
import json
import math
import re
from collections.abc import AsyncGenerator, Mapping
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
    RateLimitedError,
    RequestRejectedError,
    StreamInterruptedError,
)
from actio_llm.wire import Timeouts, request_json, stream_lines

OWN_MEMBERS = frozenset(
    {
        "model",
        "messages",
        "stream",
        "stream_options",
        "tools",
        "temperature",
        "top_p",
        "seed",
        "stop",
        "max_completion_tokens",
        "max_tokens",
    }
)
"""The members of a request's body that the client writes itself. What a model is told
about reasoning, `thinking_off` and `thinking_on`, may hold none of them."""


class OpenAICompatible:
    """Chat with the models of one OpenAI-compatible server.

    `client` is an `httpx2.AsyncClient` whose `base_url` ends where the API starts, such
    as `https://api.example.com/v1`, and which already carries the credentials
    (`headers={"Authorization": "Bearer ..."}`). The caller owns and closes it.

    `provider` names the server in error messages.

    `thinking_off` and `thinking_on` are what the model is told about reasoning: the
    members put into a request's body when it asks for none (`thinking=False`), and when
    it asks for some (`thinking=True`). Left out, off is `{"reasoning_effort": "none"}`
    and on is nothing, the model's default; an empty mapping is nothing, also for off.
    They may hold none of `OWN_MEMBERS`: a `ValueError` names the one that is there.
    """

    def __init__(
        self,
        client: httpx2.AsyncClient,
        *,
        provider: str = "the provider",
        timeouts: Timeouts | None = None,
        output_limit_field: str = "max_completion_tokens",
        thinking_off: Mapping[str, Any] | None = None,
        thinking_on: Mapping[str, Any] | None = None,
    ) -> None:
        for option, members in (("thinking_off", thinking_off), ("thinking_on", thinking_on)):
            for member in members or {}:
                if member in OWN_MEMBERS:
                    raise ValueError(f"{option} can't hold {member!r}: the client writes it")
        self._client = client
        self._provider = provider
        self._timeouts = timeouts or Timeouts()
        self._output_limit_field = output_limit_field
        # Copies, so that what the caller does with its own later changes nothing here.
        self._thinking_off = copy.deepcopy(
            dict(thinking_off) if thinking_off is not None else {"reasoning_effort": "none"}
        )
        self._thinking_on = copy.deepcopy(dict(thinking_on or {}))

    async def stream(self, request: ChatRequest) -> AsyncGenerator[StreamEvent]:
        """Send `request` and yield the answer as it is written; a `Finish` comes last.

        `request.context_window` is ignored: these servers fix it per model.
        `request.thinking` says what goes with it: `False` sends `thinking_off`, `True`
        sends `thinking_on`, a level is sent as `reasoning_effort`, and `None` leaves
        the server's default.
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
        """The ids of the models the server offers. Also a cheap health check.

        An id written `models/<name>` is there twice: as it is written, and as `<name>`.
        The Gemini API lists its models so and its chat takes `<name>`, while another
        server may name a model so and take exactly that, as vLLM started with a path
        does. No id is there twice over."""
        data = await request_json(
            self._client,
            "GET",
            "/models",
            provider=self._provider,
            timeouts=self._timeouts,
            rejection=lambda response, text: self._rejection(response, text, ""),
        )
        ids: dict[str, None] = {}  # in order, each once
        for entry in data.get("data") or ():
            written = str(entry.get("id", ""))
            ids[written] = None
            ids[written.removeprefix("models/")] = None
        return list(ids)

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
        # A copy for each request: nothing nested is shared between two bodies.
        if request.thinking is False:
            body.update(copy.deepcopy(self._thinking_off))
        elif request.thinking is True:
            body.update(copy.deepcopy(self._thinking_on))
        elif isinstance(request.thinking, str):
            body["reasoning_effort"] = request.thinking
        return body

    def _rejection(self, response: httpx2.Response, text: str, model: str) -> LLMError:
        """The error for a response that isn't 200. Bodies are `{"error": {...}}`, or a
        list of one such object."""
        status = response.status_code
        message, code = _error_parts(text)
        detail = f"{self._provider} answered HTTP {status}: {message}"
        if status == 429:
            header = response.headers.get("retry-after")
            wait = _seconds(header) if header is not None else _retry_delay(text)
            return RateLimitedError(detail, retry_after_s=wait)
        if status in (401, 403) or (status == 400 and "api key" in message.lower()):
            # Not the request's fault, and another provider can still serve it. The
            # Gemini API refuses a key it doesn't know with 400.
            return ProviderError(f"{self._provider} refused Actio's credentials: {detail}")
        if status == 404 and (code == "model_not_found" or "model" in message.lower()):
            return ModelNotFoundError(model, detail)
        if status in (502, 503, 504, 529):
            return ProviderUnavailableError(detail)
        if status >= 500 or status == 404:  # a 404 that isn't about the model: a wrong URL
            return ProviderError(detail)
        if status == 413:
            # More than the provider takes from this account in one request, as Groq
            # says of a request over its tokens a minute: no wait changes that, and its
            # `Retry-After` means nothing. Another provider may have the room.
            return ContextOverflowError(detail)
        if code == "context_length_exceeded" or "context length" in message.lower():
            return ContextOverflowError(detail)
        return RequestRejectedError(detail)


_FIELDS = frozenset({"", "data", "event", "id", "retry"})
"""The names before a line's first colon that belong to server-sent events: the fields,
and none for a comment, such as a keep-alive."""
_STRAY_LINES = 256
"""The most lines that are no field of an event kept from one stream: a bound on what a
server that sends nonsense can make the client hold, with room for an error. The one
refusal of a spent quota that was seen, pretty-printed, is 64 lines by itself."""


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
        self._timings: Timings | None = None
        self._partial: dict[int, _PartialCall] = {}
        self._last: int | None = None
        """The key in `_partial` of the call that a fragment was added to last."""
        self._called = False
        self._after_thoughts = False
        """True once a thought came inside the text, until the answer's text that
        follows has begun."""
        self._stray: list[str] = []
        """Lines that are no field of an event, kept in case they say why the stream
        ended: the Gemini API sends an error half-way as plain lines of JSON."""

    def feed(self, line: str) -> list[StreamEvent]:
        name, _, value = line.partition(":")
        if name != "data":  # a comment used as a keep-alive, or an `event:` line
            if name not in _FIELDS and len(self._stray) < _STRAY_LINES:
                self._stray.append(line)
            return []
        data = value.strip()
        if data == "[DONE]":
            self.done = True
            return []
        try:
            chunk = json.loads(data)
        except (ValueError, RecursionError) as error:
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
            self._timings = _timings(chunk["usage"])
        events: list[StreamEvent] = []
        for choice in (chunk.get("choices") or ())[:1]:  # one answer was asked for
            delta = choice.get("delta") or {}
            reasoning = delta.get("reasoning") or delta.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning:
                events.append(ReasoningDelta(reasoning))
            if isinstance(content := delta.get("content"), str) and content:
                events.extend(self._content(content, thought=_is_thought(delta)))
            for part in delta.get("tool_calls") or ():
                self._collect(part)
            if choice.get("finish_reason"):
                self._reason = str(choice["finish_reason"])
                events.extend(self._finished_calls())
        return events

    def finish(self) -> Finish:
        """The stream's last event. Call it when the stream has ended."""
        if self._reason is None:
            ended = f"{self._provider}'s answer ended before it finished"
            said = _error_of("\n".join(self._stray))
            if isinstance(said, dict) and isinstance(said.get("message"), str):
                ended += f": {said['message']}"
            raise StreamInterruptedError(ended)
        if self._reason == "length":
            reason = FinishReason.LENGTH
        elif self._reason == "content_filter":
            reason = FinishReason.CONTENT_FILTER
        elif self._called:  # whatever the server calls it: some say "stop" here
            reason = FinishReason.TOOL_CALLS
        else:
            reason = FinishReason.STOP
        return Finish(reason, self._usage, self._timings)

    def _content(self, content: str, *, thought: bool) -> list[StreamEvent]:
        """A piece of `content`, which isn't empty: the answer's text, or a thought sent
        inside it. A thought's `<thought>` is taken off its start, and `</thought>` off
        the start of the first piece of text after thoughts: never elsewhere, since an
        answer may write either itself."""
        if thought:
            self._after_thoughts = True
            reasoning = content.removeprefix("<thought>")
            return [ReasoningDelta(reasoning)] if reasoning else []
        if self._after_thoughts:
            self._after_thoughts = False
            content = content.removeprefix("</thought>")
        return [TextDelta(content)] if content else []

    def _collect(self, part: Any) -> None:
        """Add one fragment to the tool call it belongs to: by its `index`, or, without
        one, to the call collected last, unless it carries an id other than that call's
        or there is none yet. Then it starts a new call."""
        if not isinstance(part, dict):
            raise ProviderError(f"{self._provider} sent a tool call that can't be read: {part!r}")
        index = part.get("index")
        if isinstance(index, int):
            key = index
        else:
            given = str(part.get("id") or "")
            if self._last is None or (given and given != self._partial[self._last].id):
                key = max(self._partial, default=-1) + 1
            else:
                key = self._last
        self._last = key
        call = self._partial.setdefault(key, _PartialCall())
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
            except (ValueError, RecursionError):
                arguments = None
            if not call.name or not isinstance(arguments, dict):
                raise ProviderError(
                    f"{self._provider} sent a tool call that can't be read "
                    f"(was the answer cut off?): {call.name or '(no name)'}({text[:200]})"
                )
            calls.append(ToolCall(call.id or f"call_{number}", call.name, arguments))
        self._partial.clear()
        self._last = None
        self._called = self._called or bool(calls)
        return calls


def _is_thought(delta: Mapping[str, Any]) -> bool:
    """Whether a piece of `content` is a thought, as the Gemini API marks one."""
    extra = delta.get("extra_content")
    google = extra.get("google") if isinstance(extra, dict) else None
    return isinstance(google, dict) and google.get("thought") is True


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
    # Reasoning tokens are part of `completion_tokens`, or else, as the Gemini API
    # counts, of `total_tokens` alone.
    total = reported.get("total_tokens")
    output = max(completion, total - prompt) if isinstance(total, int) else completion
    cached = (reported.get("prompt_tokens_details") or {}).get("cached_tokens")
    return Usage(
        input_tokens=prompt,
        output_tokens=output,
        cached_input_tokens=min(cached, prompt) if isinstance(cached, int) else 0,
    )


_TIMES = ("queue_time", "prompt_time", "completion_time", "total_time")
_LONGEST_S = 1_000_000.0
"""The longest time a usage may say, in seconds: some eleven days, longer than any
answer, and small enough that two of them, in milliseconds, fit the 32-bit count a
turn's record holds."""


def _timings(reported: dict[str, Any]) -> Timings | None:
    """Where the time went, from a usage that says it in seconds, as Groq's does. None
    without `completion_time`; a missing one of the others counts 0. A time that isn't
    a number, is negative or is longer than `_LONGEST_S` gives none rather than wrong
    ones. Comparisons alone decide it: they are false for NaN and infinity, and convert
    nothing, so an integer of 400 digits can't raise."""
    if "completion_time" not in reported:
        return None
    seconds: dict[str, float] = {}
    for name in _TIMES:
        value = reported.get(name, 0)
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        if not 0 <= value <= _LONGEST_S:
            return None
        seconds[name] = value
    return Timings(
        load_ms=0,
        prompt_ms=round(seconds["prompt_time"] * 1000),
        generation_ms=round(seconds["completion_time"] * 1000),
        total_ms=round((seconds["queue_time"] + seconds["total_time"]) * 1000),
    )


def _error_of(body: str) -> Any:
    """What an error body holds under `error`: the body is `{"error": ...}`, or a list
    whose first item is that, as the Gemini API's chat answers. Otherwise `None`."""
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        return None
    if isinstance(data, list) and data:
        data = data[0]
    return data.get("error") if isinstance(data, dict) else None


def _error_parts(body: str) -> tuple[str, str | None]:
    """The message and the code of an error body, whatever its exact shape. A code that
    is a number, with its name in `status` as the Gemini API gives it, is that name."""
    error = _error_of(body)
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        code = error.get("code")
        if not isinstance(code, str):
            code = error.get("status")
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


_DURATION = re.compile(r"(\d+(?:\.\d+)?)s")
"""A duration as Google's APIs write one in JSON: seconds, with an `s`."""


def _retry_delay(body: str) -> float | None:
    """How long a refusal's body asks to wait, in seconds: the `retryDelay` of the
    `google.rpc.RetryInfo` among its error's `details`, as the Gemini API gives it, such
    as `"83280s"`. `None` when there is none, or it can't be read."""
    error = _error_of(body)
    details = error.get("details") if isinstance(error, dict) else None
    for detail in details if isinstance(details, list) else ():
        kind = detail.get("@type") if isinstance(detail, dict) else None
        if isinstance(kind, str) and kind.endswith("google.rpc.RetryInfo"):
            delay = detail.get("retryDelay")
            written = _DURATION.fullmatch(delay) if isinstance(delay, str) else None
            seconds = float(written[1]) if written else None
            return seconds if seconds is not None and math.isfinite(seconds) else None
    return None
