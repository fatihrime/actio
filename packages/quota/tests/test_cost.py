"""Credit math: integers, rounded up once, never cheaper for more tokens."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from actio_quota.cost import MICRO, Rates, Usage, ceil_div, cost_credits, cost_micro

rates = st.builds(
    Rates,
    input=st.integers(0, 20_000),
    output=st.integers(0, 80_000),
    cached_input=st.none() | st.integers(0, 20_000),
)
tokens = st.integers(0, 2_000_000)


@st.composite
def usages(draw: st.DrawFn) -> Usage:
    input_tokens = draw(tokens)
    return Usage(input_tokens, draw(tokens), draw(st.integers(0, input_tokens)))


def test_rounds_up_once_per_request() -> None:
    standard = Rates(input=1_000, output=4_000)
    # 10 + 3 tokens cost 10 * 1000 + 3 * 4000 = 22,000 micro-credits: 0.022 credits.
    assert cost_micro(Usage(10, 3), standard) == 22_000
    assert cost_credits(Usage(10, 3), standard) == 1
    assert cost_credits(Usage(0, 0), standard) == 0
    assert cost_credits(Usage(1_000_000, 0), standard) == 1_000


def test_cached_tokens_use_the_cached_price() -> None:
    standard = Rates(input=1_000, output=4_000, cached_input=250)
    assert cost_micro(Usage(1_000, 0, cached_input_tokens=800), standard) == 200 * 1_000 + 800 * 250
    # Without a cached price, cached tokens cost as much as fresh ones.
    assert cost_micro(Usage(1_000, 0, 800), Rates(1_000, 4_000)) == 1_000_000


def test_rejects_impossible_counts() -> None:
    with pytest.raises(ValueError, match="input_tokens"):
        Usage(-1, 0)
    with pytest.raises(ValueError, match="cached_input_tokens"):
        Usage(10, 0, cached_input_tokens=11)
    with pytest.raises(ValueError, match="output"):
        Rates(input=1, output=2**53)
    with pytest.raises(ValueError, match="ceil_div"):
        ceil_div(1, 0)


@given(usages(), rates)
def test_cost_is_the_exact_cost_rounded_up(usage: Usage, rates: Rates) -> None:
    credits = cost_credits(usage, rates)
    assert isinstance(credits, int)
    assert (credits - 1) * MICRO < cost_micro(usage, rates) <= credits * MICRO


@given(usages(), rates, st.integers(0, 1_000), st.integers(0, 1_000))
def test_more_tokens_never_cost_less(
    usage: Usage, rates: Rates, more_in: int, more_out: int
) -> None:
    bigger = Usage(
        usage.input_tokens + more_in, usage.output_tokens + more_out, usage.cached_input_tokens
    )
    assert cost_credits(bigger, rates) >= cost_credits(usage, rates)


@given(usages(), usages(), rates)
def test_splitting_a_request_costs_at_most_one_credit_more(
    a: Usage, b: Usage, rates: Rates
) -> None:
    together = Usage(
        a.input_tokens + b.input_tokens,
        a.output_tokens + b.output_tokens,
        a.cached_input_tokens + b.cached_input_tokens,
    )
    apart = cost_credits(a, rates) + cost_credits(b, rates)
    assert cost_credits(together, rates) <= apart <= cost_credits(together, rates) + 1
