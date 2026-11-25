"""The providers' budgets' counters, against a real Valkey server.

The examples pin down what a bucket means, with the figures of the free plans the
example models file states (ADR 0030). The property test then plays random sequences of
looks and adds to the Lua script and to buckets written the plain way, counted exactly,
and holds the script's answers to within its rounding to the millisecond.
"""

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from fractions import Fraction

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from valkey.asyncio import Valkey

from actio.core.budget import Bucket, Budgets, BudgetsUnavailableError, Counts, Period
from actio.core.resources import open_resources
from actio.core.settings import Settings

pytestmark = pytest.mark.anyio

NOON = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
NOTHING = timedelta(0)
MINUTE_MS = 60_000
DAY_MS = 86_400_000


def ms(amount: int) -> timedelta:
    return timedelta(milliseconds=amount)


def bucket(
    size: int,
    period: Period = "minute",
    counts: Counts = "tokens",
    *,
    provider: str = "groq",
    model: str = "openai/gpt-oss-20b",
) -> Bucket:
    return Bucket(counts, period, provider, model, size)


@pytest.fixture
async def valkey(valkey_url: str) -> AsyncIterator[Valkey]:
    async with Valkey.from_url(valkey_url) as client:
        yield client


@pytest.fixture
def budgets(valkey: Valkey) -> Budgets:
    """Budgets whose keys no other test shares."""
    return Budgets(valkey, prefix=f"test-{uuid.uuid4().hex}")


# --- A bucket ---------------------------------------------------------------------------


def test_a_bucket_says_its_limit_in_words_and_in_its_key() -> None:
    tokens = bucket(7_200)
    requests = bucket(900, "day", "requests")

    assert (str(tokens), tokens.limit, tokens.period_ms) == (
        "tokens a minute",
        "tokens-a-minute",
        MINUTE_MS,
    )
    assert (str(requests), requests.limit, requests.period_ms) == (
        "requests a day",
        "requests-a-day",
        DAY_MS,
    )


@pytest.mark.parametrize(
    ("size", "provider", "model", "fault"),
    [
        (0, "groq", "m", "1 at the least"),
        (-5, "groq", "m", "1 at the least"),
        (5, "gr:oq", "m", "a provider's name"),
        (5, "", "m", "a provider's name"),
        (5, "groq", "", "of a model"),
    ],
)
def test_a_bucket_that_cant_be_counted_is_refused(
    size: int, provider: str, model: str, fault: str
) -> None:
    with pytest.raises(ValueError, match=fault):
        Bucket("tokens", "minute", provider, model, size)


async def test_a_key_names_the_limit_the_provider_and_the_model_last(valkey: Valkey) -> None:
    """Readable: they are names from the models file. A model's name may hold anything,
    a ':' too, and is last."""
    budgets = Budgets(valkey, prefix="actio:b")

    assert budgets.key(bucket(7_200)) == "actio:b:tokens-a-minute:groq:openai/gpt-oss-20b"
    assert (
        budgets.key(bucket(900, "day", "requests", provider="ollama", model="qwen3.5:4b"))
        == "actio:b:requests-a-day:ollama:qwen3.5:4b"
    )


async def test_the_api_and_the_worker_count_under_the_same_keys(
    settings: Settings, valkey: Valkey
) -> None:
    """Both build their budgets in `open_resources`, under the settings' prefix: what
    one process takes, another one finds taken."""
    rpm = bucket(27, counts="requests")

    async with open_resources(settings) as api, open_resources(settings) as worker:
        await api.budgets.add((rpm, 27), now=NOON)
        found = await worker.budgets.look((rpm, 1), now=NOON)

    key = f"{settings.valkey_prefix}:b:requests-a-minute:groq:openai/gpt-oss-20b"
    assert await valkey.exists(key) == 1
    assert found == [ms(2_222)]
    await valkey.delete(key)


# --- What a bucket means ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("size", "then_one_waits_ms"),
    [(27, 2_216), (13, 4_610)],
    ids=["27 of 30 a minute", "13 of 15 a minute"],
)
async def test_a_full_bucket_gives_its_whole_size_one_at_a_time_and_nothing_more(
    size: int, then_one_waits_ms: int, budgets: Budgets
) -> None:
    """Each one moves the moment it is full again by its time to come back, rounded
    down: rounded up, the last one of a burst wouldn't fit."""
    requests = bucket(size, counts="requests")
    for _ in range(size):
        assert await budgets.look((requests, 1), now=NOON) == [NOTHING]
        await budgets.add((requests, 1), now=NOON)

    assert await budgets.look((requests, 1), now=NOON) == [ms(then_one_waits_ms)]


@pytest.mark.parametrize(("size", "one_waits_ms"), [(27, 2_222), (13, 4_615)])
async def test_a_full_bucket_gives_its_whole_size_at_once_and_nothing_more(
    size: int, one_waits_ms: int, budgets: Budgets
) -> None:
    requests = bucket(size, counts="requests")

    assert await budgets.look((requests, size), now=NOON) == [NOTHING]
    await budgets.add((requests, size), now=NOON)

    assert await budgets.look((requests, 1), now=NOON) == [ms(one_waits_ms)]


async def test_what_is_left_fits_and_one_more_waits_for_what_it_lacks(budgets: Budgets) -> None:
    """5,000 of 7,200 taken: 2,200 are left. 3,000 lack 800, which take 6,666 ms to come
    back at 120 a second."""
    tpm = bucket(7_200)
    await budgets.add((tpm, 5_000), now=NOON)

    assert await budgets.look((tpm, 2_200), now=NOON) == [NOTHING]
    [one_more] = await budgets.look((tpm, 2_201), now=NOON)
    assert NOTHING < one_more < ms(10)
    assert await budgets.look((tpm, 3_000), now=NOON) == [ms(800 * MINUTE_MS // 7_200)]


async def test_more_than_a_bucket_holds_fits_it_only_when_full_and_waits_until_it_is(
    budgets: Budgets,
) -> None:
    tpm = bucket(7_200)

    assert await budgets.look((tpm, 7_500), now=NOON) == [NOTHING]
    await budgets.add((tpm, 5_000), now=NOON)

    assert await budgets.look((tpm, 7_500), now=NOON) == [ms(41_666)]
    assert await budgets.look((tpm, 7_500), now=NOON + ms(41_666)) == [NOTHING]


async def test_half_a_period_gives_half_a_bucket_back_and_a_bucket_holds_its_size_at_most(
    budgets: Budgets,
) -> None:
    tpm = bucket(7_200)
    await budgets.add((tpm, 7_200), now=NOON)
    half_a_minute = NOON + timedelta(seconds=30)

    assert await budgets.look((tpm, 3_600), now=half_a_minute) == [NOTHING]
    assert await budgets.look((tpm, 3_601), now=half_a_minute) > [NOTHING]

    a_week_later = NOON + timedelta(days=7)
    await budgets.add((tpm, 7_200), now=a_week_later)
    assert await budgets.look((tpm, 1), now=a_week_later) == [ms(8)]


async def test_an_add_never_leaves_less_than_nothing(budgets: Budgets) -> None:
    """An answer longer than what was left is counted only as far as empty: half a
    minute later, half of the bucket is there again."""
    tpm = bucket(7_200)
    await budgets.add((tpm, 7_200), now=NOON)
    await budgets.add((tpm, 7_200), now=NOON)

    assert await budgets.look((tpm, 7_200), now=NOON) == [ms(MINUTE_MS)]
    assert await budgets.look((tpm, 3_600), now=NOON + timedelta(seconds=30)) == [NOTHING]


async def test_what_is_given_back_comes_back_and_no_more_than_was_taken(
    budgets: Budgets, valkey: Valkey
) -> None:
    """A request taken and given back leaves the bucket as it was. Giving back more
    than was taken leaves it full: no key."""
    tpm = bucket(7_200)
    await budgets.add((tpm, 5_000), now=NOON)
    before = await valkey.get(budgets.key(tpm))
    await budgets.add((tpm, 1_234), now=NOON)
    await budgets.add((tpm, -1_234), now=NOON)

    assert await valkey.get(budgets.key(tpm)) == before
    await budgets.add((tpm, -9_000), now=NOON)
    assert await valkey.exists(budgets.key(tpm)) == 0
    assert await budgets.look((tpm, 7_200), now=NOON) == [NOTHING]


async def test_a_look_writes_nothing(budgets: Budgets, valkey: Valkey) -> None:
    """No key appears for a bucket that is full, and a key's moment and time to live
    stay as they were, also when the look takes it for empty."""
    full, taken = bucket(7_200), bucket(27, counts="requests")
    too_far = bucket(13, model="openai/gpt-oss-120b")
    await budgets.add((taken, 10), now=NOON)
    moment, alive_for = await valkey.get(budgets.key(taken)), await valkey.pttl(budgets.key(taken))
    far_away = str(int(NOON.timestamp() * 1000) + 10 * MINUTE_MS)
    await valkey.set(budgets.key(too_far), far_away)

    for now in (NOON, NOON + timedelta(seconds=10), NOON + timedelta(days=1)):
        await budgets.look((full, 1), (taken, 27), (too_far, 13), now=now)

    assert await valkey.exists(budgets.key(full)) == 0
    assert await valkey.get(budgets.key(taken)) == moment
    assert alive_for - 1_000 < await valkey.pttl(budgets.key(taken)) <= alive_for
    assert await valkey.get(budgets.key(too_far)) == far_away.encode()
    assert await valkey.pttl(budgets.key(too_far)) == -1  # still no time to live


async def test_several_buckets_in_one_look_each_wait_their_own(budgets: Budgets) -> None:
    """As a model's budget is looked at: a request from each bucket of requests, and the
    prompt's tokens from each bucket of tokens."""
    rpm, rpd = bucket(27, counts="requests"), bucket(900, "day", "requests")
    tpm, tpd = bucket(7_200), bucket(180_000, "day")
    await budgets.add((rpm, 27), (tpm, 5_000), now=NOON)

    waits = await budgets.look((rpm, 1), (rpd, 1), (tpm, 3_000), (tpd, 3_000), now=NOON)

    assert waits == [ms(2_222), NOTHING, ms(6_666), NOTHING]


async def test_a_bucket_of_225000_tokens_a_minute_counts(budgets: Budgets, valkey: Valkey) -> None:
    """3,750 tokens a second: more than one a millisecond, which the rate limiter's
    buckets can't hold. A take of 12,828 is full again 3,420 ms later, and leaves
    212,172."""
    large = bucket(225_000, model="gemini-3.5-flash-lite", provider="gemini")
    await budgets.add((large, 12_828), now=NOON)

    assert await valkey.pttl(budgets.key(large)) <= 3_420
    assert await budgets.look((large, 212_172), now=NOON) == [NOTHING]
    assert await budgets.look((large, 225_000), now=NOON) == [ms(3_420)]

    await budgets.add((large, 225_000), now=NOON)
    assert await budgets.look((large, 3_750), now=NOON) == [ms(1_000)]
    assert await budgets.look((large, 3_750), now=NOON + ms(1_000)) == [NOTHING]


async def test_a_stored_moment_further_away_than_an_empty_buckets_is_taken_for_empty(
    budgets: Budgets, valkey: Valkey
) -> None:
    """A limit that was raised, or a clock that ran ahead, closes a bucket for a period
    at most, not until that moment. An add stores it as empty."""
    tpm = bucket(7_200)
    await valkey.set(budgets.key(tpm), str(int(NOON.timestamp() * 1000) + 10 * MINUTE_MS))

    assert await budgets.look((tpm, 1), (tpm, 7_200), now=NOON) == [ms(8), ms(MINUTE_MS)]
    await budgets.add((tpm, 0), now=NOON)
    assert int(await valkey.get(budgets.key(tpm)) or 0) == int(NOON.timestamp() * 1000) + MINUTE_MS


async def test_adds_arriving_together_all_count(budgets: Budgets) -> None:
    """120 tokens of 7,200 a minute are a second's: no rounding between them."""
    tpm = bucket(7_200)

    await asyncio.gather(*(budgets.add((tpm, 120), now=NOON) for _ in range(40)))

    assert await budgets.look((tpm, 2_400), now=NOON) == [NOTHING]
    assert await budgets.look((tpm, 2_401), now=NOON) > [NOTHING]


# --- What Valkey holds ----------------------------------------------------------------


async def test_a_full_bucket_takes_no_key_and_a_key_expires_when_its_bucket_is_full(
    budgets: Budgets, valkey: Valkey
) -> None:
    tpm, large = bucket(7_200), bucket(225_000, provider="gemini", model="lite")

    await budgets.add((tpm, 3_600), now=NOON)
    half = await valkey.pttl(budgets.key(tpm))
    await budgets.add((tpm, 3_600), now=NOON)
    whole = await valkey.pttl(budgets.key(tpm))
    await budgets.add((large, 3), now=NOON)  # less than a millisecond's worth

    assert 29_000 < half <= 30_000
    assert 59_000 < whole <= 60_000
    assert await valkey.exists(budgets.key(large)) == 0


async def test_the_script_is_sent_again_after_valkey_forgets_it(
    budgets: Budgets, valkey: Valkey
) -> None:
    tpm = bucket(7_200)
    await budgets.add((tpm, 7_200), now=NOON)
    await valkey.script_flush()  # what a restart does

    assert await budgets.look((tpm, 1), now=NOON) == [ms(8)]


async def test_an_unreachable_valkey_is_said_not_guessed_around() -> None:
    async with Valkey.from_url("valkey://127.0.0.1:1/0") as nowhere:  # nothing listens there
        budgets = Budgets(nowhere)

        with pytest.raises(BudgetsUnavailableError):
            await budgets.look((bucket(7_200), 1), now=NOON)
        with pytest.raises(BudgetsUnavailableError):
            await budgets.add((bucket(7_200), 1), now=NOON)
        # Nothing to count asks Valkey nothing.
        assert await budgets.look(now=NOON) == []
        await budgets.add(now=NOON)


async def test_a_look_is_for_an_amount_of_nothing_or_more(budgets: Budgets) -> None:
    with pytest.raises(ValueError, match="0 or more"):
        await budgets.look((bucket(7_200), -1), now=NOON)


# --- The script against the plain definition ------------------------------------------


class PlainBuckets:
    """Token buckets as they are usually described, to test the script against.

    Each holds a level, from nothing to its size, which grows steadily, by its size over
    its period, and was last read at a moment of its own. An amount is taken from the
    level, or given back to it, no further than empty and no further than full. It fits
    when the level holds it, or, when it is larger than the size, when the bucket is
    full. All of it in fractions, exactly.

    The script keeps a bucket as the moment it is full again, in whole milliseconds,
    and rounds each amount it adds to a millisecond: by less than one each time, either
    way. So it may be off from this by less than a millisecond for each add since the
    bucket was last full for longer than that, and less than one more for a look's own
    rounding (`slack`)."""

    def __init__(self) -> None:
        self._held: dict[Bucket, tuple[Fraction, int]] = {}  # level, as of when
        self._rounded: dict[Bucket, int] = {}  # adds since it was surely full

    def look(self, amounts: list[tuple[Bucket, int]], now_ms: int) -> list[Fraction]:
        """For each bucket, the milliseconds until its amount fits it, 0 when it does."""
        waits = []
        for bucket, amount in amounts:
            short = min(amount, bucket.size) - self._level(bucket, now_ms)
            waits.append(max(Fraction(0), short * bucket.period_ms / bucket.size))
        return waits

    def add(self, amounts: list[tuple[Bucket, int]], now_ms: int) -> None:
        for bucket, amount in amounts:
            level = self._level(bucket, now_ms) - amount
            self._held[bucket] = (min(Fraction(bucket.size), max(Fraction(0), level)), now_ms)
            self._rounded[bucket] = self._rounded.get(bucket, 0) + 1

    def slack(self, bucket: Bucket) -> int:
        """How many milliseconds the script's wait may be off from this one's: less than
        this."""
        return self._rounded.get(bucket, 0) + 1

    def _level(self, bucket: Bucket, now_ms: int) -> Fraction:
        size = Fraction(bucket.size)
        level, as_of = self._held.get(bucket, (size, now_ms))
        grown = level + Fraction((now_ms - as_of) * bucket.size, bucket.period_ms)
        # Full for as long as the script's moment may be off: the script's bucket is full
        # too, and the two are the same again.
        if (grown - size) * bucket.period_ms / bucket.size >= self._rounded.get(bucket, 0):
            self._rounded[bucket] = 0
        return min(size, grown)


BUCKETS = (
    bucket(27, counts="requests"),
    bucket(13, counts="requests", provider="gemini", model="gemini-3.5-flash-lite"),
    bucket(7_200),
    bucket(225_000, provider="gemini", model="gemini-3.5-flash-lite"),
    bucket(900, "day", "requests"),
    bucket(180_000, "day"),
)
"""Every period is a minute or longer, as the providers' are."""

AFTER_AN_ADD_MS = 10_000
"""How far the test's clock moves on at least, after an add, before the next step.

A key expires in real time, while the test's clock is moved by hand. An add that leaves
a bucket a few milliseconds from full writes a key that lives that long, and that could
be gone by the next step, where the test's clock says that the bucket isn't full yet.
After this, the script finds such a bucket full whether its key is there or not, and a
key that lives longer is still there when the example ends."""

ADVANCES_MS = st.one_of(
    st.just(0),
    st.integers(0, 1_000),
    st.sampled_from([8, 2_222, 4_615, 30_000, 59_999, 60_000, 60_001, DAY_MS]),
    st.integers(0, 2 * DAY_MS),
)


def _amounts(operation: str) -> st.SearchStrategy[list[tuple[Bucket, int]]]:
    """Up to three buckets, each with an amount: up to twice its size, and, for an add,
    as much given back."""

    def lowest(b: Bucket) -> int:
        return 0 if operation == "look" else -2 * b.size

    def each(chosen: list[Bucket]) -> st.SearchStrategy[list[tuple[Bucket, int]]]:
        return st.tuples(
            *(st.tuples(st.just(b), st.integers(lowest(b), 2 * b.size)) for b in chosen)
        ).map(list)

    return st.lists(st.sampled_from(BUCKETS), min_size=1, max_size=3, unique=True).flatmap(each)


STEPS = st.lists(
    st.sampled_from(["look", "add"]).flatmap(
        lambda operation: st.tuples(ADVANCES_MS, st.just(operation), _amounts(operation))
    ),
    min_size=1,
    max_size=60,
)


@settings(deadline=None)  # an example is up to 61 round trips to a server: no fixed time
@given(steps=STEPS)
def test_the_script_agrees_with_plain_token_buckets(
    valkey_url: str, steps: list[tuple[int, str, list[tuple[Bucket, int]]]]
) -> None:
    async def play() -> None:
        async with Valkey.from_url(valkey_url) as client:
            budgets = Budgets(client, prefix=f"test-{uuid.uuid4().hex}")
            plain = PlainBuckets()
            elapsed_ms = 0
            added = False

            async def agree(amounts: list[tuple[Bucket, int]]) -> None:
                now = NOON + ms(elapsed_ms)
                answers = await budgets.look(*amounts, now=now)
                expected = plain.look(amounts, int(NOON.timestamp()) * 1000 + elapsed_ms)
                for (bucket, _), answer, wait in zip(amounts, answers, expected, strict=True):
                    assert abs(answer // ms(1) - wait) < plain.slack(bucket), (bucket, wait)

            try:
                for advance_ms, operation, amounts in steps:
                    elapsed_ms += advance_ms + (AFTER_AN_ADD_MS if added else 0)
                    added = operation == "add"
                    if added:
                        await budgets.add(*amounts, now=NOON + ms(elapsed_ms))
                        plain.add(amounts, int(NOON.timestamp()) * 1000 + elapsed_ms)
                    else:
                        await agree(amounts)
                # How far from full each bucket ended, which says what the last adds did.
                elapsed_ms += AFTER_AN_ADD_MS if added else 0
                await agree([(b, b.size) for b in BUCKETS])
            finally:
                await client.delete(*(budgets.key(b) for b in BUCKETS))

    started = time.monotonic()
    asyncio.run(play())
    # What `AFTER_AN_ADD_MS` rests on: an example takes far less than that in real time.
    assert time.monotonic() - started < AFTER_AN_ADD_MS / 1000
