"""Actio's model clients.

- `breaker`: whether a model keeps failing, from the calls to it that ended, so that
  its caller asks it after the others of its chain for a while.
- `chat`: the neutral chat format every adapter speaks, and `LoadsAhead`, for those
  whose server can load a model before it is asked anything.
- `errors`: why a model call failed.
- `estimate`: a guess at a text's tokens, for when no provider has counted them.
- `ollama`: the adapter for Ollama's native API.
- `openai_compatible`: the adapter for servers that speak OpenAI's Chat Completions API.
- `router`: the walk down a chain of models, each asked in the place of the one before
  it that couldn't answer, before the first word, and `hands_on`, what counts as that
  word.
- `wire`: reading a streamed answer under deadlines, shared by the adapters.
- `cli`: `actio-llm`, to chat with a local model from the terminal.
"""

from actio_llm.breaker import Breaker, Call, Change, Failing
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
    FOR_NOW,
    ConnectionFailedError,
    ContextOverflowError,
    LLMError,
    ModelNotFoundError,
    ProviderError,
    ProviderUnavailableError,
    RateLimitedError,
    RequestRejectedError,
    StreamInterruptedError,
    TookTooLongError,
    UnsupportedFeatureError,
    UnsupportedServerError,
)
from actio_llm.estimate import prompt_tokens, tokens_in
from actio_llm.ollama import LocalModel, ModelInfo, Ollama
from actio_llm.openai_compatible import OpenAICompatible
from actio_llm.router import Link, Walk, hands_on
from actio_llm.wire import Timeouts

__all__ = [
    "FOR_NOW",
    "Assistant",
    "Breaker",
    "Call",
    "Change",
    "ChatProvider",
    "ChatRequest",
    "ConnectionFailedError",
    "ContextOverflowError",
    "Failing",
    "Finish",
    "FinishReason",
    "LLMError",
    "Link",
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
    "TookTooLongError",
    "ToolCall",
    "ToolResult",
    "ToolSpec",
    "UnsupportedFeatureError",
    "UnsupportedServerError",
    "Usage",
    "User",
    "Walk",
    "hands_on",
    "prompt_tokens",
    "tokens_in",
]
