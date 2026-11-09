"""The part of OpenAI's Chat Completions wire format that fakellm speaks.

Requests accept and ignore fields the fake doesn't use (temperature, tools, ...), as
OpenAI-compatible servers do. Responses send unset fields as explicit nulls.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

FinishReason = Literal["stop", "length"]


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


class AssistantMessage(BaseModel):
    role: Literal["assistant"] = "assistant"
    content: str


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


class Delta(BaseModel):
    role: Literal["assistant"] | None = None
    content: str | None = None


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
