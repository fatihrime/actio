"""Actio's model clients.

- `chat`: the neutral chat format every adapter speaks, and `LoadsAhead`, for those
  whose server can load a model before it is asked anything.
- `errors`: why a model call failed.
- `estimate`: a guess at a text's tokens, for when no provider has counted them.
- `ollama`: the adapter for Ollama's native API.
- `openai_compatible`: the adapter for servers that speak OpenAI's Chat Completions API.
- `wire`: reading a streamed answer under deadlines, shared by the adapters.
- `cli`: `actio-llm`, to chat with a local model from the terminal.
"""

from actio_llm.chat import (
    Assistant,
    ChatProvider,
    ChatRequest,
    Finish,
    FinishReason,
    LoadsAhead,
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
    RateLimitedError,
    RequestRejectedError,
    StreamInterruptedError,
    UnsupportedFeatureError,
    UnsupportedServerError,
)
from actio_llm.estimate import prompt_tokens, tokens_in
from actio_llm.ollama import LocalModel, ModelInfo, Ollama
from actio_llm.openai_compatible import OpenAICompatible
from actio_llm.wire import Timeouts

__all__ = [
    "Assistant",
    "ChatProvider",
    "ChatRequest",
    "ContextOverflowError",
    "Finish",
    "FinishReason",
    "LLMError",
    "LoadsAhead",
    "LocalModel",
    "Message",
    "ModelInfo",
    "ModelNotFoundError",
    "Ollama",
    "OpenAICompatible",
    "ProviderError",
    "ProviderUnavailableError",
    "RateLimitedError",
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
    "prompt_tokens",
    "tokens_in",
]
