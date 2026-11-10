"""The part of OpenAI's Chat Completions wire format that fakellm speaks.

Requests accept and ignore fields the fake doesn't use (temperature, tools, ...), as
OpenAI-compatible servers do. Responses send unset fields as explicit nulls, except the
ones marked `_OMITTED`: reasoning and tool calls appear only in replies that have them,
as on real servers.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

FinishReason = Literal["stop", "length", "tool_calls"]


def _omitted_when_unset() -> Any:
    """A field that is left out of the JSON while it is None."""
    return Field(default=None, exclude_if=lambda value: value is None)


class ContentPart(BaseModel):
    """One part of a multi-part message. Only text parts carry text the fake reads."""

    model_config = ConfigDict(extra="allow")

    type: str
    text: str | None = None


class Message(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: Literal["system", "developer", "user", "assistant", "tool"]
    content: str | list[ContentPart] | None = None

    def text(self) -> str:
        if isinstance(self.content, list):
            return " ".join(part.text for part in self.content if part.type == "text" and part.text)
        return self.content or ""


class StreamOptions(BaseModel):
    include_usage: bool = False


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[Message] = Field(min_length=1)
    stream: bool = False
    stream_options: StreamOptions | None = None
    max_completion_tokens: int | None = Field(default=None, ge=1)
    max_tokens: int | None = Field(default=None, ge=1)  # the older name for the same limit


class Usage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class FunctionCall(BaseModel):
    name: str
    arguments: str
    """The arguments as JSON text, which is how OpenAI's format carries them."""


class ToolCall(BaseModel):
    id: str
    type: Literal["function"] = "function"
    function: FunctionCall


class AssistantMessage(BaseModel):
    role: Literal["assistant"] = "assistant"
    content: str | None
    reasoning: str | None = _omitted_when_unset()
    tool_calls: list[ToolCall] | None = _omitted_when_unset()


class Choice(BaseModel):
    index: int = 0
    message: AssistantMessage
    finish_reason: FinishReason


class ChatCompletion(BaseModel):
    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str
    choices: list[Choice]
    usage: Usage


class FunctionCallDelta(BaseModel):
    name: str | None = _omitted_when_unset()
    arguments: str
    """The next fragment of the arguments' JSON text."""


class ToolCallDelta(BaseModel):
    index: int
    id: str | None = _omitted_when_unset()
    type: Literal["function"] | None = _omitted_when_unset()
    function: FunctionCallDelta


class Delta(BaseModel):
    role: Literal["assistant"] | None = None
    content: str | None = None
    reasoning: str | None = _omitted_when_unset()
    tool_calls: list[ToolCallDelta] | None = _omitted_when_unset()


class ChunkChoice(BaseModel):
    index: int = 0
    delta: Delta
    finish_reason: FinishReason | None = None


class ChatCompletionChunk(BaseModel):
    id: str
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: int
    model: str
    choices: list[ChunkChoice]
    usage: Usage | None = None


class Model(BaseModel):
    id: str
    object: Literal["model"] = "model"
    created: int = 0
    owned_by: str = "fakellm"


class ModelList(BaseModel):
    object: Literal["list"] = "list"
    data: list[Model]


class ErrorDetail(BaseModel):
    message: str
    type: str = "invalid_request_error"
    param: str | None = None
    code: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail
