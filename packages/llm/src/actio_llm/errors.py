"""Why a model call failed. Adapters raise these instead of their HTTP library's errors.

The classes answer what a caller has to decide: is another provider worth trying, is
the request itself wrong, or did the answer break off half-way?
"""


class LLMError(Exception):
    """Any failure of a model call."""


class ProviderUnavailableError(LLMError):
    """The provider couldn't be reached, is overloaded, or didn't start answering in time.

    Nothing was generated, so the same request can go to another provider.
    """


class RateLimitedError(ProviderUnavailableError):
    """The provider is refusing requests for now: a rate limit or a spent quota."""

    def __init__(self, detail: str, *, retry_after_s: float | None = None) -> None:
        super().__init__(detail)
        self.retry_after_s = retry_after_s
        """How long the provider asks to wait, in seconds, when it says."""


class StreamInterruptedError(LLMError):
    """The answer started and then broke off: the connection dropped, the provider
    reported an error half-way, or it went silent for too long.

    Events already received are real. Whether to retry depends on what was shown.
    """


class ProviderError(LLMError):
    """The provider failed, or sent something that isn't its own protocol."""


class UnsupportedServerError(LLMError):
    """The server is older than the oldest version this adapter was tested against."""


class RequestRejectedError(LLMError):
    """The provider refused the request. Sending it again unchanged won't help."""


class UnsupportedFeatureError(RequestRejectedError):
    """The model can't do something the request asked for, such as reasoning or tools."""


class ModelNotFoundError(RequestRejectedError):
    """The provider doesn't have this model."""

    def __init__(self, model: str, detail: str = "") -> None:
        super().__init__(detail or f"model {model!r} not found")
        self.model = model


class ContextOverflowError(RequestRejectedError):
    """The prompt is longer than the model's context window.

    The provider refused it rather than silently cutting it: nothing was generated.
    """

    def __init__(
        self, detail: str, *, prompt_tokens: int | None = None, context_window: int | None = None
    ) -> None:
        super().__init__(detail)
        self.prompt_tokens = prompt_tokens
        """The prompt's size as the provider counted it, when it says."""
        self.context_window = context_window
        """The window the prompt had to fit in, when the provider says."""
