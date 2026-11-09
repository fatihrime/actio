"""Credit math: integers only, rounded up once per request.

Prices are integer credits per million tokens, so a request's cost in *micro-credits*
(millionths of a credit) is an exact integer sum. It's rounded up to whole credits once,
at the end. The Valkey scripts only ever add and subtract the resulting integers.
"""

from dataclasses import dataclass

MICRO = 1_000_000
"""Micro-credits per credit. A price of `r` credits per million tokens is `r` micro-credits per token."""

MAX_SAFE = 2**53 - 1
"""The largest integer Lua stores exactly: Valkey's Lua numbers are 64-bit floats."""

MAX_CREDITS = MAX_SAFE // MICRO
"""The largest credit amount whose micro-credit value is still exact in Lua."""


def ceil_div(numerator: int, denominator: int) -> int:
    """Integer division rounded up, for non-negative integers."""
    if numerator < 0 or denominator <= 0:
        raise ValueError("ceil_div needs numerator >= 0 and denominator > 0")
    return -(-numerator // denominator)


def check_count(name: str, value: int, limit: int = MAX_SAFE) -> None:
    """Reject negative counts and counts Lua couldn't represent exactly."""
    if not 0 <= value <= limit:
        raise ValueError(f"{name} must be between 0 and {limit}, got {value}")


@dataclass(frozen=True, slots=True)
class Rates:
    """A model's prices, in credits per million tokens."""

    input: int
    output: int
    cached_input: int | None = None
    """Price of prompt tokens served from the provider's cache. `None` means the input price."""

    def __post_init__(self) -> None:
        check_count("input", self.input)
        check_count("output", self.output)
        if self.cached_input is not None:
            check_count("cached_input", self.cached_input)


@dataclass(frozen=True, slots=True)
class Usage:
    """Tokens a request used, as reported by the provider."""

    input_tokens: int
    output_tokens: int
    cached_input_tokens: int = 0
    """How many of `input_tokens` came from the cache. Counted at the cached price."""

    def __post_init__(self) -> None:
        check_count("input_tokens", self.input_tokens)
        check_count("output_tokens", self.output_tokens)
        check_count("cached_input_tokens", self.cached_input_tokens, self.input_tokens)


def cost_micro(usage: Usage, rates: Rates) -> int:
    """The exact cost of `usage`, in micro-credits."""
    cached_rate = rates.input if rates.cached_input is None else rates.cached_input
    fresh = usage.input_tokens - usage.cached_input_tokens
    return (
        fresh * rates.input
        + usage.cached_input_tokens * cached_rate
        + usage.output_tokens * rates.output
    )


def cost_credits(usage: Usage, rates: Rates) -> int:
    """What a request costs in credits: its exact cost, rounded up once."""
    return ceil_div(cost_micro(usage, rates), MICRO)
