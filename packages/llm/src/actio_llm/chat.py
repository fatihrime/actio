"""The neutral chat format: what Actio asks a model, and what streams back.

Every provider adapter takes a `ChatRequest` and yields `StreamEvent`s, so the rest of
Actio never sees a provider's wire format. A stream is:

- any number of `TextDelta`, `ReasoningDelta` and `ToolCall` events, in the order the
  model produced them;
- then exactly one `Finish`, always last.

A failure raises an `LLMError` (see `actio_llm.errors`) instead of ending the stream.
Closing the stream early stops the model: wrap it in `contextlib.aclosing`.
"""

from collections.abc import AsyncGenerator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

# --- Messages ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolCall:
    """The model asks for a tool to be run. Part of an answer, and a stream event."""

    id: str
    name: str
    arguments: Mapping[str, Any] = field(hash=False)
    """The arguments as a parsed JSON object."""


@dataclass(frozen=True, slots=True)
class System:
    """Instructions that come before the conversation."""

    content: str


@dataclass(frozen=True, slots=True)
class User:
    content: str


@dataclass(frozen=True, slots=True)
class Assistant:
    """An earlier answer of the model, sent back as history."""

    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    reasoning: str = ""
    """The thinking that led to `tool_calls`. Send it back only inside the same tool
    loop; in later turns it would just use up the context."""


@dataclass(frozen=True, slots=True)
class ToolResult:
    """What a tool returned for one `ToolCall`."""

    call_id: str
    name: str
    content: str


type Message = System | User | Assistant | ToolResult


# --- Request -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """A tool the model may call."""

    name: str
    description: str
    parameters: Mapping[str, Any] = field(hash=False)
    """The JSON Schema of the tool's arguments."""


@dataclass(frozen=True, slots=True)
class ChatRequest:
    model: str
    """The model's name at the provider, such as `qwen3.5:4b`."""
    messages: Sequence[Message] = field(hash=False)
    tools: Sequence[ToolSpec] = field(default=(), hash=False)
    max_output_tokens: int | None = None
    """The most tokens the answer may have, reasoning included. `None`: the adapter's
    own bound, which is never more than the context window."""
    context_window: int | None = None
    """How many tokens the model may hold: the prompt plus the answer. `None`: the
    provider's default.

    Who enforces it depends on the provider. A local runtime that sizes the model's
    memory by it refuses a prompt that doesn't fit, and reloads the model when the size
    changes, so keep it the same for every request to a model. Other runtimes and the
    hosted providers ignore it: there it only caps the answer, and keeping the prompt
    inside it is the caller's job."""
    thinking: bool | str | None = None
    """`True` or `False` to turn reasoning on or off, or a level the model defines,
    such as `"low"`. `None`: the model's default."""
    temperature: float | None = None
    top_p: float | None = None
    seed: int | None = None
    stop: Sequence[str] = field(default=(), hash=False)
    """Strings that end the answer when the model writes them."""

    def __post_init__(self) -> None:
        if not self.model:
            raise ValueError("a request needs a model")
        if not self.messages:
            raise ValueError("a request needs at least one message")
        for name in ("max_output_tokens", "context_window"):
            value = getattr(self, name)
            if value is not None and value < 1:
                raise ValueError(f"{name} must be at least 1, got {value}")
        names = [tool.name for tool in self.tools]
        if len(set(names)) != len(names):
            raise ValueError(f"tool names must be unique, got {names}")


# --- Stream events -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TextDelta:
    """The next piece of the answer."""

    text: str


@dataclass(frozen=True, slots=True)
class ReasoningDelta:
    """The next piece of the model's thinking, which comes before its answer."""

    text: str


class FinishReason(StrEnum):
    STOP = "stop"
    """The model finished, or wrote a stop string."""
    LENGTH = "length"
    """The answer hit `max_output_tokens`, or filled the context window."""
    TOOL_CALLS = "tool_calls"
    """The model asked for tools: run them and send the results back."""
    CONTENT_FILTER = "content_filter"
    """The provider's own safety filter cut the answer."""


@dataclass(frozen=True, slots=True)
class Usage:
    """Tokens the request used, as the provider counted them."""

    input_tokens: int
    """The whole prompt, cached part included."""
    output_tokens: int
    """Everything the model generated: reasoning, answer and tool calls."""
    cached_input_tokens: int = 0
    """How many of `input_tokens` the provider served from its prompt cache."""


@dataclass(frozen=True, slots=True)
class Timings:
    """Where the time went, for providers that report it (local runtimes do)."""

    load_ms: int
    """Loading the model into memory. Large on a cold start, near zero when warm."""
    prompt_ms: int
    """Reading the prompt."""
    generation_ms: int
    """Writing the answer."""
    total_ms: int


@dataclass(frozen=True, slots=True)
class Finish:
    """The last event of every stream."""

    reason: FinishReason
    usage: Usage | None
    """`None` when the provider reported no token counts. Zeros would read as "free",
    so the caller must count the tokens itself."""
    timings: Timings | None = None

    @property
    def tokens_per_second(self) -> float | None:
        """Generation speed, when the provider reported how long generation took."""
        if self.usage is None or self.timings is None or self.timings.generation_ms <= 0:
            return None
        return self.usage.output_tokens * 1000 / self.timings.generation_ms


type StreamEvent = TextDelta | ReasoningDelta | ToolCall | Finish


class ChatProvider(Protocol):
    """What every provider adapter offers."""

    def stream(self, request: ChatRequest) -> AsyncGenerator[StreamEvent]:
        """Send `request` and yield the model's answer as it is written."""
        ...
