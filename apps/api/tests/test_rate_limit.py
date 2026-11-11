"""The rate limiter, against a real Valkey server.

The examples pin down what a limit means. The property test then plays random
sequences of attempts to the Lua script and to token buckets written the plain way, and
requires the same answer at every step.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from valkey.asyncio import Valkey

from actio.core.rate_limit import LimiterUnavailableError, Rate, RateLimiter, Take

pytestmark = pytest.mark.anyio

NOON = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
NOTHING = timedelta(0)
THREE_A_MINUTE = Rate.parse("3/minute")  # one token back every 20 seconds
ONE_AN_HOUR = Rate.parse("1/hour")


def seconds(amount: float) -> timedelta:
    return timedelta(seconds=amount)


def logins(subject: str, rate: Rate = THREE_A_MINUTE) -> Take:
    return Take("logins", subject, rate)


@pytest.fixture
async def valkey(valkey_url: str) -> AsyncIterator[Valkey]:
    async with Valkey.from_url(valkey_url) as client:
        yield client


@pytest.fixture
def limiter(valkey: Valkey) -> RateLimiter:
    """A limiter whose keys no other test shares."""
    return RateLimiter(valkey, prefix=f"test-{uuid.uuid4().hex}")


# --- Writing a rate -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "count", "interval_ms"),
    [
        ("30/hour", 30, 120_000),
        ("3/day", 3, 28_800_000),
        ("1/second", 1, 1_000),
        ("1000/second", 1000, 1),
        (" 10/minute ", 10, 6_000),
        ("7/hour", 7, 514_285),  # rounded down: never slower than what was written
    ],
)
def test_a_rate_is_written_as_a_count_per_unit(text: str, count: int, interval_ms: int) -> None:
    rate = Rate.parse(text)

    assert rate.count == count
    assert rate.interval_ms == interval_ms
    assert str(rate) == text.strip()


@pytest.mark.parametrize(
    "text",
    ["", "30", "30/", "/hour", "0/hour", "-1/hour", "1.5/hour", "30 / hour", "30/hours", "30/week"],
)
def test_a_rate_written_any_other_way_is_refused(text: str) -> None:
    with pytest.raises(ValueError, match="must look like 30/hour"):
        Rate.parse(text)


def test_a_rate_is_at_most_one_per_millisecond() -> None:
    with pytest.raises(ValueError, match="at most one per millisecond"):
        Rate.parse("1001/second")


def test_a_limits_name_is_plain_because_it_becomes_part_of_a_key() -> None:
    with pytest.raises(ValueError, match="a limit's name"):
        Take("logins:*", "x", THREE_A_MINUTE)


# --- What a limit means ---------------------------------------------------------------


async def test_a_burst_fits_and_the_next_attempt_waits_for_a_token(limiter: RateLimiter) -> None:
    for _ in range(3):
        assert await limiter.spend(logins("a"), now=NOON) == NOTHING

    assert await limiter.spend(logins("a"), now=NOON) == seconds(20)


async def test_one_token_comes_back_every_interval(limiter: RateLimiter) -> None:
    for _ in range(3):
        await limiter.spend(logins("a"), now=NOON)

    assert await limiter.spend(logins("a"), now=NOON + seconds(19.999)) == seconds(0.001)
    assert await limiter.spend(logins("a"), now=NOON + seconds(20)) == NOTHING
    assert await limiter.spend(logins("a"), now=NOON + seconds(20)) == seconds(20)
    # Two more intervals bring two tokens back, and no more.
    for _ in range(2):
        assert await limiter.spend(logins("a"), now=NOON + seconds(60)) == NOTHING
    assert await limiter.spend(logins("a"), now=NOON + seconds(60)) == seconds(20)


async def test_a_bucket_never_holds_more_than_its_size(limiter: RateLimiter) -> None:
    await limiter.spend(logins("a"), now=NOON)
    a_week_later = NOON + timedelta(days=7)

    answers = [await limiter.spend(logins("a"), now=a_week_later) for _ in range(4)]

    assert answers == [NOTHING, NOTHING, NOTHING, seconds(20)]


async def test_a_refused_attempt_costs_nothing(limiter: RateLimiter) -> None:
    for _ in range(3):
        await limiter.spend(logins("a"), now=NOON)

    for _ in range(50):
        assert await limiter.spend(logins("a"), now=NOON) == seconds(20)

    assert await limiter.spend(logins("a"), now=NOON + seconds(20)) == NOTHING


async def test_each_subject_and_each_limit_has_its_own_bucket(limiter: RateLimiter) -> None:
    for _ in range(3):
        await limiter.spend(logins("a"), now=NOON)

    assert await limiter.spend(logins("b"), now=NOON) == NOTHING
    assert await limiter.spend(Take("signups", "a", THREE_A_MINUTE), now=NOON) == NOTHING
    assert await limiter.spend(logins("a"), now=NOON) == seconds(20)


async def test_several_limits_are_charged_together_or_not_at_all(limiter: RateLimiter) -> None:
    by_email = Take("logins-email", "deniz@example.com", ONE_AN_HOUR)
    by_address = Take("logins-address", "203.0.113.7", THREE_A_MINUTE)
    assert await limiter.spend(by_address, by_email, now=NOON) == NOTHING

    # The email's bucket is empty now. Attempts it refuses don't use up the address's.
    for _ in range(10):
        assert await limiter.spend(by_address, by_email, now=NOON) == timedelta(hours=1)

    assert await limiter.spend(by_address, now=NOON) == NOTHING
    assert await limiter.spend(by_address, now=NOON) == NOTHING
    assert await limiter.spend(by_address, now=NOON) == seconds(20)


async def test_the_wait_is_for_the_slowest_limit_in_the_way(limiter: RateLimiter) -> None:
    quick, slow = logins("a"), Take("slow", "a", ONE_AN_HOUR)
    for _ in range(3):
        await limiter.spend(quick, now=NOON)
    await limiter.spend(slow, now=NOON)

    assert await limiter.spend(quick, slow, now=NOON) == timedelta(hours=1)
    assert await limiter.spend(slow, quick, now=NOON) == timedelta(hours=1)


async def test_attempts_arriving_together_cannot_share_the_last_token(
    limiter: RateLimiter,
) -> None:
    five = Take("logins", "a", Rate.parse("5/hour"))

    answers = await asyncio.gather(*(limiter.spend(five, now=NOON) for _ in range(40)))

    assert answers.count(NOTHING) == 5


async def test_a_clock_that_ran_ahead_cannot_close_a_bucket_for_long(limiter: RateLimiter) -> None:
    """Time is an argument, so one process with a wrong clock could write a moment far in
    the future. The others are held up for one refill at most, not until that moment."""
    next_month = NOON + timedelta(days=30)
    for _ in range(3):
        await limiter.spend(logins("a"), now=next_month)

    assert await limiter.spend(logins("a"), now=NOON) == seconds(20)
    assert await limiter.spend(logins("a"), now=NOON + seconds(20)) == NOTHING


async def test_loosening_a_limit_takes_effect_at_once(limiter: RateLimiter) -> None:
    """Someone refused under "1/hour" isn't still held to it after the limit is raised:
    the bucket counts as empty under the new limit, and fills at the new pace."""
    await limiter.spend(logins("a", ONE_AN_HOUR), now=NOON)
    assert await limiter.spend(logins("a", ONE_AN_HOUR), now=NOON) == timedelta(hours=1)

    assert await limiter.spend(logins("a", THREE_A_MINUTE), now=NOON) == seconds(20)
    assert await limiter.spend(logins("a", THREE_A_MINUTE), now=NOON + seconds(20)) == NOTHING


async def test_tightening_a_limit_keeps_what_was_already_spent(limiter: RateLimiter) -> None:
    for _ in range(3):
        await limiter.spend(logins("a", THREE_A_MINUTE), now=NOON)  # full again in a minute

    # Under "1/hour" that is a bucket that needs a minute more, not a full one.
    assert await limiter.spend(logins("a", ONE_AN_HOUR), now=NOON) == seconds(60)
    assert await limiter.spend(logins("a", ONE_AN_HOUR), now=NOON + seconds(60)) == NOTHING


# --- What Valkey holds ----------------------------------------------------------------


async def test_a_bucket_is_forgotten_when_it_is_full_again(
    limiter: RateLimiter, valkey: Valkey
) -> None:
    take = logins("a")

    await limiter.spend(take, now=NOON)
    after_one = await valkey.pttl(limiter.key(take))
    await limiter.spend(take, now=NOON)
    await limiter.spend(take, now=NOON)
    when_empty = await valkey.pttl(limiter.key(take))

    assert 19_000 < after_one <= 20_000  # one token short: full again in 20 seconds
    assert 59_000 < when_empty <= 60_000


async def test_a_refusal_writes_nothing(limiter: RateLimiter, valkey: Valkey) -> None:
    empty, untouched = Take("empty", "a", ONE_AN_HOUR), logins("a")
    await limiter.spend(empty, now=NOON)

    await limiter.spend(untouched, empty, now=NOON)

    assert await valkey.exists(limiter.key(untouched)) == 0


async def test_keys_do_not_hold_what_people_typed(limiter: RateLimiter, valkey: Valkey) -> None:
    take = Take("logins-email", "deniz@example.com", THREE_A_MINUTE)

    await limiter.spend(take, now=NOON)

    key = limiter.key(take)
    assert await valkey.exists(key) == 1
    assert ":logins-email:" in key  # what is counted is readable; for whom is not
    assert "deniz" not in key
    assert "example" not in key


async def test_the_script_is_sent_again_after_valkey_forgets_it(
    limiter: RateLimiter, valkey: Valkey
) -> None:
    await limiter.spend(logins("a"), now=NOON)
    await valkey.script_flush()  # what a restart does

    assert await limiter.spend(logins("a"), now=NOON) == NOTHING


async def test_an_unreachable_valkey_is_reported_not_guessed_around() -> None:
    async with Valkey.from_url("valkey://127.0.0.1:1/0") as nowhere:  # nothing listens there
        limiter = RateLimiter(nowhere)

        with pytest.raises(LimiterUnavailableError):
            await limiter.spend(logins("a"), now=NOON)


# --- The script against the plain definition ------------------------------------------


class TokenBuckets:
    """Token buckets as they are usually described, to test the script against.

    Each bucket holds up to `count` tokens and gets one back every interval. Tokens are
    counted in milliseconds of refill time, so every number stays whole.
    """

    def __init__(self) -> None:
        self._buckets: dict[tuple[str, str], tuple[int, int]] = {}  # refill held, as of when

    def spend(self, takes: list[Take], now_ms: int) -> int:
        """Milliseconds to wait, or 0 after taking one token from every bucket."""
        held: dict[tuple[str, str], int] = {}
        wait = 0
        for take in takes:
            interval, full = take.rate.interval_ms, take.rate.count * take.rate.interval_ms
            had, as_of = self._buckets.get((take.name, take.subject), (full, now_ms))
            held[take.name, take.subject] = min(full, had + now_ms - as_of)
            wait = max(wait, interval - held[take.name, take.subject])
        if wait > 0:
            return wait
        for take in takes:
            bucket = (take.name, take.subject)
            self._buckets[bucket] = (held[bucket] - take.rate.interval_ms, now_ms)
        return 0


# Every interval is a minute or longer: a key must not expire, in real time, while an
# example that takes milliseconds is still playing.
LIMITS = {
    "single": Rate.parse("1/minute"),
    "small": Rate.parse("3/hour"),
    "burst": Rate.parse("30/hour"),
    "uneven": Rate.parse("7/hour"),
    "daily": Rate.parse("5/day"),
}
BUCKETS = st.tuples(st.sampled_from(sorted(LIMITS)), st.sampled_from(["a", "b"]))
ADVANCES_MS = st.one_of(
    st.just(0),
    st.integers(0, 1_000),
    st.sampled_from([59_999, 60_000, 60_001, 119_999, 120_000, 514_285, 3_600_000]),
    st.integers(0, 6 * 3_600_000),
)
STEPS = st.lists(
    st.tuples(ADVANCES_MS, st.lists(BUCKETS, min_size=1, max_size=3, unique=True)),
    min_size=1,
    max_size=60,
)


@settings(deadline=None)  # an example is up to 60 round trips to a server: no fixed time
@given(steps=STEPS)
def test_the_script_agrees_with_plain_token_buckets(
    valkey_url: str, steps: list[tuple[int, list[tuple[str, str]]]]
) -> None:
    async def play() -> None:
        async with Valkey.from_url(valkey_url) as client:
            limiter = RateLimiter(client, prefix=f"test-{uuid.uuid4().hex}")
            model = TokenBuckets()
            elapsed_ms = 0
            used: set[str] = set()
            try:
                for advance_ms, buckets in steps:
                    elapsed_ms += advance_ms
                    takes = [Take(name, subject, LIMITS[name]) for name, subject in buckets]
                    used.update(limiter.key(take) for take in takes)
                    now = NOON + timedelta(milliseconds=elapsed_ms)

                    answer = await limiter.spend(*takes, now=now)

                    expected = model.spend(takes, int(NOON.timestamp()) * 1000 + elapsed_ms)
                    assert answer == timedelta(milliseconds=expected)
            finally:
                await client.delete(*used)

    asyncio.run(play())
