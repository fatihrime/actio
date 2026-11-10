"""Actio's model clients.

- `chat`: the neutral chat format every adapter speaks.
- `errors`: why a model call failed.
- `ollama`: the adapter for Ollama's native API.
- `cli`: `actio-llm`, to chat with a local model from the terminal.
"""

from actio_llm.chat import (
    Assistant,
    ChatProvider,
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
    ToolSpec,
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
from actio_llm.ollama import LocalModel, ModelInfo, Ollama, Timeouts

__all__ = [
    "Assistant",
    "ChatProvider",
    "ChatRequest",
    "ContextOverflowError",
    "Finish",
    "FinishReason",
    "LLMError",
    "LocalModel",
    "Message",
    "ModelInfo",
    "ModelNotFoundError",
    "Ollama",
    "ProviderError",
    "ProviderUnavailableError",
    "ReasoningDelta",
    "RequestRejectedError",
    "StreamEvent",
    "StreamInterruptedError",
    "System",
    "TextDelta",
    "Timeouts",
    "Timings",
    "ToolCall",
    "ToolResult",
    "ToolSpec",
    "UnsupportedFeatureError",
    "UnsupportedServerError",
    "Usage",
    "User",
]
