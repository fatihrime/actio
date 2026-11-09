"""The fakellm app: OpenAI's Chat Completions API with predictable answers.

Every reply echoes the last user message as "Echo: <text>". Each word, with the
whitespace after it, counts as one token. A test can therefore predict every byte of a
response, streamed or not, and the usage numbers that come with it.
"""

import itertools
import re
import time
from collections.abc import AsyncIterator, Callable, Iterable

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
    Model,
    ModelList,
    Usage,
)

DEFAULT_MODELS = ("fake-chat",)

# One token: a word plus the whitespace after it. Joining the tokens restores the text.
_TOKEN = re.compile(r"\S+\s*")


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

        completion_id = f"chatcmpl-fake-{next(ids):06d}"
        created = int(clock())
        tokens, finish_reason = _reply(request)
        prompt_tokens = sum(_count_tokens(message.text()) for message in request.messages)
        usage = Usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=len(tokens),
            total_tokens=prompt_tokens + len(tokens),
        )

        if not request.stream:
            return ChatCompletion(
                id=completion_id,
                created=created,
                model=request.model,
                choices=[
                    Choice(
                        message=AssistantMessage(content="".join(tokens)),
                        finish_reason=finish_reason,
                    )
                ],
                usage=usage,
            )

        def chunk(choices: list[ChunkChoice], *, with_usage: bool = False) -> ChatCompletionChunk:
            return ChatCompletionChunk(
                id=completion_id,
                created=created,
                model=request.model,
                choices=choices,
                usage=usage if with_usage else None,
            )

        chunks = [
            chunk([ChunkChoice(delta=Delta(role="assistant", content=""))]),
            *(chunk([ChunkChoice(delta=Delta(content=token))]) for token in tokens),
            chunk([ChunkChoice(delta=Delta(), finish_reason=finish_reason)]),
        ]
        if request.stream_options is not None and request.stream_options.include_usage:
            chunks.append(chunk([], with_usage=True))  # OpenAI's extra, final usage chunk
        return EventSourceResponse(_sse(chunks), headers={"Cache-Control": "no-cache"})

    return app


def _reply(request: ChatCompletionRequest) -> tuple[list[str], FinishReason]:
    """The reply as tokens, cut to the request's output limit, and why it ended."""
    last_user = next((m.text() for m in reversed(request.messages) if m.role == "user"), "")
    tokens = _TOKEN.findall(f"Echo: {last_user}")
    limit = request.max_completion_tokens or request.max_tokens
    if limit is not None and len(tokens) > limit:
        return tokens[:limit], "length"
    return tokens, "stop"


def _count_tokens(text: str) -> int:
    return len(_TOKEN.findall(text))


async def _sse(chunks: list[ChatCompletionChunk]) -> AsyncIterator[str]:
    """OpenAI's stream format: one `data:` event per chunk, then `data: [DONE]`."""
    for chunk in chunks:
        yield f"data: {chunk.model_dump_json()}\n\n"
    yield "data: [DONE]\n\n"


def _error(
    status: int, message: str, *, param: str | None = None, code: str | None = None
) -> JSONResponse:
    """An error in OpenAI's format, which SDKs turn into typed exceptions."""
    body = ErrorResponse(error=ErrorDetail(message=message, param=param, code=code))
    return JSONResponse(body.model_dump(), status_code=status)
