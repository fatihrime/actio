"""The fakellm app: OpenAI's Chat Completions API with predictable answers.

Every reply echoes the last user message as "Echo: <text>". Each word, with the
whitespace after it, counts as one token. A test can therefore predict every byte of a
response, streamed or not, and the usage numbers that come with it.

A test can also script the reply, with directives in the last user message:

    [[think: some text]]      reason first: the text streams as reasoning, before the answer
    [[call: name {"a": 1}]]   call a tool instead of answering; repeat for several calls
    [[fail: 503]]             answer with this HTTP status (a 429 adds `Retry-After: 1`)
    [[fail: 400 some_code]]   the same, with this error code in the body
    [[drop: 2]]               stream 2 tokens, then end the stream without finishing it
    [[delay: 0.5]]            wait this many seconds before answering

Directives are left out of the echoed text. They live in the message, not in a control
API, so the fake keeps no state between requests, and a test that drives the whole stack
can script it by typing into a chat box.

After a tool's result (a last message with the role `tool`), the reply echoes that
result and no directive applies. An agent loop scripted to call a tool therefore gets a
plain answer on its next step, instead of the same tool call again.
"""

import asyncio
import itertools
import json
import re
import time
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass, field

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.sse import EventSourceResponse

from actio_fakellm import __version__
from actio_fakellm.schemas import (
    AssistantMessage,
    ChatCompletion,
    ChatCompletionChunk,
    ChatCompletionRequest,
    Choice,
    ChunkChoice,
    Delta,
    ErrorDetail,
    ErrorResponse,
    FinishReason,
    FunctionCall,
    FunctionCallDelta,
    Model,
    ModelList,
    ToolCall,
    ToolCallDelta,
    Usage,
)

DEFAULT_MODELS = ("fake-chat",)
MAX_DELAY_S = 30.0

# One token: a word plus the whitespace after it. Joining the tokens restores the text.
_TOKEN = re.compile(r"\S+\s*")
_DIRECTIVE = re.compile(r"\[\[(\w+):\s*(.*?)\]\]", re.DOTALL)
_FRAGMENT = 12
"""How many characters of a tool call's arguments each stream chunk carries."""


class ScriptError(ValueError):
    """A directive that fakellm can't read. Reported as a 400, so a test fails loudly."""


@dataclass
class Script:
    """What one reply will do, read from the request."""

    reasoning: list[str] = field(default_factory=list)
    answer: list[str] = field(default_factory=list)
    calls: list[tuple[str, str]] = field(default_factory=list)
    """Tool calls, each a name and its arguments as JSON text."""
    fail: tuple[int, str | None] | None = None
    drop_after: int | None = None
    delay_s: float = 0.0


def create_app(
    models: Iterable[str] = DEFAULT_MODELS, clock: Callable[[], float] = time.time
) -> FastAPI:
    """Build a fake that serves `models`. Inject `clock` to fix the `created` timestamps."""
    served = tuple(models)
    ids = itertools.count(1)
    app = FastAPI(title="fakellm", version=__version__)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, exc: RequestValidationError) -> JSONResponse:
        error = exc.errors()[0]
        param = ".".join(str(part) for part in error["loc"][1:])  # drop the leading "body"
        return _error(400, f"{param or 'body'}: {error['msg']}", param=param or None)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/models")
    async def list_models() -> ModelList:
        return ModelList(data=[Model(id=name) for name in served])

    @app.post("/v1/chat/completions", response_model=None)
    async def chat_completions(
        request: ChatCompletionRequest,
    ) -> ChatCompletion | EventSourceResponse | JSONResponse:
        if request.model not in served:
            message = f"The model `{request.model}` does not exist."
            return _error(404, message, param="model", code="model_not_found")
        try:
            script = read_script(request)
        except ScriptError as error:
            return _error(400, f"fakellm: {error}", param="messages")
        if script.fail is not None:
            await asyncio.sleep(script.delay_s)
            return _scripted_failure(*script.fail)

        completion_id = f"chatcmpl-fake-{next(ids):06d}"
        created = int(clock())
        finish_reason = _apply_limit(script, request.max_completion_tokens or request.max_tokens)
        calls = [
            ToolCall(
                id=f"call_fake_{number:06d}",
                function=FunctionCall(name=name, arguments=arguments),
            )
            for number, (name, arguments) in enumerate(script.calls, start=1)
        ]
        prompt_tokens = sum(_count_tokens(message.text()) for message in request.messages)
        completion_tokens = (
            len(script.reasoning)
            + len(script.answer)
            + sum(_count_tokens(f"{name} {arguments}") for name, arguments in script.calls)
        )
        usage = Usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        )

        if not request.stream:
            await asyncio.sleep(script.delay_s)
            reply = AssistantMessage(
                content=None if calls else "".join(script.answer),
                reasoning="".join(script.reasoning) or None,
                tool_calls=calls or None,
            )
            return ChatCompletion(
                id=completion_id,
                created=created,
                model=request.model,
                choices=[Choice(message=reply, finish_reason=finish_reason)],
                usage=usage,
            )

        def chunk(delta: Delta | None = None, *, last: bool = False) -> ChatCompletionChunk:
            if delta is None:  # OpenAI's extra, final chunk: usage and no choices
                choices = []
            else:
                reason = finish_reason if last else None
                choices = [ChunkChoice(delta=delta, finish_reason=reason)]
            return ChatCompletionChunk(
                id=completion_id,
                created=created,
                model=request.model,
                choices=choices,
                usage=usage if delta is None else None,
            )

        tokens = [
            *(chunk(Delta(reasoning=token)) for token in script.reasoning),
            *(chunk(Delta(content=token)) for token in script.answer),
        ]
        chunks = [chunk(Delta(role="assistant", content="")), *tokens]
        if script.drop_after is not None:
            # The connection "drops": no finish reason, no usage, no [DONE].
            chunks = chunks[: 1 + script.drop_after]
            return _stream(chunks, script.delay_s, finished=False)
        for index, call in enumerate(calls):
            chunks += [chunk(delta) for delta in _tool_call_deltas(index, call)]
        chunks.append(chunk(Delta(), last=True))
        if request.stream_options is not None and request.stream_options.include_usage:
            chunks.append(chunk())
        return _stream(chunks, script.delay_s, finished=True)

    return app


def read_script(request: ChatCompletionRequest) -> Script:
    """The reply's script: an echo, changed by the directives in the last user message."""
    last = request.messages[-1]
    if last.role == "tool":
        return Script(answer=_TOKEN.findall(f"Echo: {last.text()}"))

    text = next((m.text() for m in reversed(request.messages) if m.role == "user"), "")
    directives = _DIRECTIVE.findall(text)
    script = Script()
    for name, raw in directives:
        value = raw.strip()
        try:
            _apply(script, name, value)
        except ScriptError:
            raise
        except ValueError as error:
            raise ScriptError(f"can't read the directive [[{name}: {value}]]") from error
    if directives:  # leave them out of the echo, and tidy the spaces they leave behind
        text = " ".join(_DIRECTIVE.sub(" ", text).split())
    if not script.calls:
        script.answer = _TOKEN.findall(f"Echo: {text}")
    return script


def _apply(script: Script, name: str, value: str) -> None:
    match name:
        case "think":
            script.reasoning += _TOKEN.findall(value)
        case "call":
            tool, _, arguments = value.partition(" ")
            arguments = arguments.strip() or "{}"
            if not tool or not isinstance(json.loads(arguments), dict):
                raise ValueError(value)
            script.calls.append((tool, arguments))
        case "fail":
            status, _, code = value.partition(" ")
            if not 400 <= int(status) <= 599:
                raise ValueError(value)
            script.fail = (int(status), code.strip() or None)
        case "drop":
            script.drop_after = int(value)
            if script.drop_after < 0:
                raise ValueError(value)
        case "delay":
            script.delay_s = float(value)
            if not 0 <= script.delay_s <= MAX_DELAY_S:
                raise ValueError(value)
        case _:
            raise ScriptError(f"unknown directive [[{name}: ...]]")


def _apply_limit(script: Script, limit: int | None) -> FinishReason:
    """Cut the reasoning, then the answer, to the request's output limit.

    Tool calls are never cut: half a JSON object is of no use to a test.
    """
    cut = limit is not None and len(script.reasoning) + len(script.answer) > limit
    if limit is not None:
        script.reasoning = script.reasoning[:limit]
        script.answer = script.answer[: limit - len(script.reasoning)]
    if script.calls:
        return "tool_calls"
    return "length" if cut else "stop"


def _tool_call_deltas(index: int, call: ToolCall) -> list[Delta]:
    """One tool call as OpenAI streams it: its id and name first, then its arguments'
    JSON text in fragments, which the client has to join."""
    arguments = call.function.arguments
    opening = ToolCallDelta(
        index=index,
        id=call.id,
        type="function",
        function=FunctionCallDelta(name=call.function.name, arguments=""),
    )
    fragments = [
        ToolCallDelta(
            index=index, function=FunctionCallDelta(arguments=arguments[i : i + _FRAGMENT])
        )
        for i in range(0, len(arguments), _FRAGMENT)
    ]
    return [Delta(tool_calls=[part]) for part in (opening, *fragments)]


def _count_tokens(text: str) -> int:
    return len(_TOKEN.findall(text))


def _stream(
    chunks: list[ChatCompletionChunk], delay_s: float, *, finished: bool
) -> EventSourceResponse:
    return EventSourceResponse(
        _sse(chunks, delay_s, finished=finished), headers={"Cache-Control": "no-cache"}
    )


async def _sse(
    chunks: list[ChatCompletionChunk], delay_s: float, *, finished: bool
) -> AsyncIterator[str]:
    """OpenAI's stream format: one `data:` event per chunk, then `data: [DONE]`."""
    await asyncio.sleep(delay_s)
    for chunk in chunks:
        yield f"data: {chunk.model_dump_json()}\n\n"
    if finished:
        yield "data: [DONE]\n\n"


def _scripted_failure(status: int, code: str | None) -> JSONResponse:
    kind = "invalid_request_error" if status < 500 else "server_error"
    response = _error(status, f"fakellm: scripted failure ({status})", code=code, kind=kind)
    if status == 429:
        response.headers["Retry-After"] = "1"
    return response


def _error(
    status: int,
    message: str,
    *,
    param: str | None = None,
    code: str | None = None,
    kind: str = "invalid_request_error",
) -> JSONResponse:
    """An error in OpenAI's format, which SDKs turn into typed exceptions."""
    body = ErrorResponse(error=ErrorDetail(message=message, type=kind, param=param, code=code))
    return JSONResponse(body.model_dump(), status_code=status)
