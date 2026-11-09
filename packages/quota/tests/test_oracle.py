"""The reference model's rules, one example each. The stateful test checks them at random."""

import pytest

from actio_quota import (
    Bucket,
    BucketView,
    Denied,
    Estimate,
    Finished,
    Fixed,
    Granted,
    LedgerEntry,
    NeedsRebuild,
    Outcome,
    QuotaOracle,
    Rates,
    Rolling,
    UsageWarning,
)
from actio_quota.model import DONE_TTL_MS, GC_BATCH

HOUR = 3_600_000
T0 = 1_800_000_000_000  # an arbitrary start time, in ms
WEEK = Fixed(T0 - 24 * HOUR, T0 + 6 * 24 * HOUR)
TTL = 10 * 60_000


def buckets(session: int = 100, week: int = 1_000) -> list[Bucket]:
    return [Bucket("session", session, Rolling(5 * HOUR)), Bucket("week", week, WEEK)]


@pytest.fixture
def oracle() -> QuotaOracle:
    """An engine with one user, `u1`, whose counters exist but are empty."""
    engine = QuotaOracle()
    assert engine.rebuild("u1", buckets(), [], now_ms=T0, if_epoch=None)
    return engine


def reserve(
    oracle: QuotaOracle, rid: str, credits: int, *, now: int = T0, **limits: int
) -> Granted | Denied:
    result = oracle.reserve(
        "u1", rid, buckets(**limits), Estimate.flat(credits), ttl_ms=TTL, now_ms=now
    )
    assert not isinstance(result, NeedsRebuild)
    return result


def views(oracle: QuotaOracle, now: int = T0) -> dict[str, BucketView]:
    snapshot = oracle.snapshot("u1", buckets(), now_ms=now)
    assert snapshot is not None
    return {v.name: v for v in snapshot}


def test_a_user_without_counters_needs_a_rebuild_first() -> None:
    engine = QuotaOracle()
    result = engine.reserve("new", "r1", buckets(), Estimate.flat(1), ttl_ms=TTL, now_ms=T0)
    assert result == NeedsRebuild()
    assert engine.snapshot("new", buckets(), now_ms=T0) is None
    assert engine.settle("new", "r1", 1, now_ms=T0) == Finished(Outcome.UNKNOWN, applied=False)


def test_the_session_opens_at_the_first_admitted_request(oracle: QuotaOracle) -> None:
    assert views(oracle)["session"].start_ms is None
    later = T0 + 7 * 60_000
    grant = reserve(oracle, "r1", 10, now=later)
    assert isinstance(grant, Granted)
    session = views(oracle, later)["session"]
    assert (session.start_ms, session.reset_ms) == (later, later + 5 * HOUR)
    assert grant.windows == {"session": later, "week": WEEK.start_ms}


def test_a_denial_changes_nothing_and_opens_no_window(oracle: QuotaOracle) -> None:
    before = oracle.dump("u1", now_ms=T0)
    denied = reserve(oracle, "r1", 101)
    assert denied == Denied("r1", ("session",), retry_at_ms=None)  # 101 never fits in 100
    assert oracle.dump("u1", now_ms=T0) == before


def test_a_denial_says_when_the_blocking_window_resets(oracle: QuotaOracle) -> None:
    assert isinstance(reserve(oracle, "r1", 60), Granted)
    later = T0 + 60_000  # r1 is still running
    assert reserve(oracle, "r2", 60, now=later) == Denied(
        "r2", ("session",), retry_at_ms=T0 + 5 * HOUR
    )
    # Blocked by both: the request waits for the later reset.
    assert reserve(oracle, "r3", 60, now=later, week=100) == Denied(
        "r3", ("session", "week"), retry_at_ms=WEEK.end_ms
    )


def test_admission_counts_reserved_credits(oracle: QuotaOracle) -> None:
    assert isinstance(reserve(oracle, "r1", 50), Granted)
    assert isinstance(reserve(oracle, "r2", 50), Granted)
    assert isinstance(reserve(oracle, "r3", 1), Denied)
    assert views(oracle)["session"] == BucketView("session", 100, 0, 100, T0, T0 + 5 * HOUR)


def test_answers_get_shorter_near_the_limit(oracle: QuotaOracle) -> None:
    rates = Rates(input=1_000, output=4_000)
    # 20,000 prompt tokens cost 20 credits, leaving 80 credits: 20,000 output tokens.
    estimate = Estimate.tokens(rates, input_tokens=20_000, max_output=50_000)
    grant = oracle.reserve("u1", "r1", buckets(), estimate, ttl_ms=TTL, now_ms=T0)
    assert grant == Granted("r1", 100, 20_000, {"session": T0, "week": WEEK.start_ms})


def test_the_tool_step_floor_denies_instead_of_shrinking(oracle: QuotaOracle) -> None:
    rates = Rates(input=1_000, output=4_000)
    assert isinstance(reserve(oracle, "r0", 98), Granted)  # 2 credits left: 500 tokens
    tool_step = Estimate.tokens(rates, input_tokens=0, max_output=4_096, min_output=512)
    assert isinstance(
        oracle.reserve("u1", "r1", buckets(), tool_step, ttl_ms=TTL, now_ms=T0), Denied
    )
    plain = Estimate.tokens(rates, input_tokens=0, max_output=4_096)
    grant = oracle.reserve("u1", "r2", buckets(), plain, ttl_ms=TTL, now_ms=T0)
    assert isinstance(grant, Granted)
    assert grant.units == 500


def test_settling_moves_credits_from_reserved_to_used(oracle: QuotaOracle) -> None:
    assert isinstance(reserve(oracle, "r1", 40), Granted)
    assert oracle.settle("u1", "r1", 25, now_ms=T0) == Finished(Outcome.SETTLED, applied=True)
    assert (views(oracle)["session"].used, views(oracle)["session"].reserved) == (25, 0)
    assert views(oracle)["week"].used == 25


def test_settle_and_release_are_idempotent(oracle: QuotaOracle) -> None:
    assert isinstance(reserve(oracle, "r1", 40), Granted)
    assert isinstance(reserve(oracle, "r2", 40), Granted)
    oracle.settle("u1", "r1", 30, now_ms=T0)
    oracle.release("u1", "r2", now_ms=T0)
    after = oracle.dump("u1", now_ms=T0)
    assert oracle.settle("u1", "r1", 30, now_ms=T0) == Finished(Outcome.SETTLED, applied=False)
    assert oracle.settle("u1", "r2", 30, now_ms=T0) == Finished(Outcome.RELEASED, applied=False)
    assert oracle.release("u1", "r1", now_ms=T0) == Finished(Outcome.SETTLED, applied=False)
    assert oracle.dump("u1", now_ms=T0) == after
    assert oracle.settle("u1", "never", 1, now_ms=T0).outcome is Outcome.UNKNOWN


def test_a_retried_reserve_gets_the_same_grant(oracle: QuotaOracle) -> None:
    first = reserve(oracle, "r1", 40)
    assert reserve(oracle, "r1", 40) == first
    oracle.settle("u1", "r1", 40, now_ms=T0)
    assert reserve(oracle, "r1", 40) == first  # still remembered after it finished
    assert views(oracle)["session"].used == 40  # and nothing was charged twice


def test_finished_reservations_are_forgotten_after_two_days(oracle: QuotaOracle) -> None:
    assert isinstance(reserve(oracle, "r1", 10), Granted)
    oracle.settle("u1", "r1", 10, now_ms=T0)
    much_later = T0 + DONE_TTL_MS
    assert oracle.settle("u1", "r1", 10, now_ms=much_later).outcome is Outcome.UNKNOWN


def test_warnings_fire_once_per_window(oracle: QuotaOracle) -> None:
    assert isinstance(reserve(oracle, "r1", 80), Granted)
    first = oracle.settle("u1", "r1", 80, now_ms=T0)
    assert first.warnings == (UsageWarning("session", 50), UsageWarning("session", 75))
    assert isinstance(reserve(oracle, "r2", 15), Granted)
    assert oracle.settle("u1", "r2", 15, now_ms=T0).warnings == (UsageWarning("session", 90),)
    # In the next session window the counters, and the warnings, start over.
    next_session = T0 + 5 * HOUR
    assert isinstance(reserve(oracle, "r3", 60, now=next_session), Granted)
    assert oracle.settle("u1", "r3", 60, now_ms=next_session).warnings == (
        UsageWarning("session", 50),
    )


def test_usage_counts_toward_the_window_that_admitted_it(oracle: QuotaOracle) -> None:
    assert isinstance(reserve(oracle, "r1", 30), Granted)
    assert oracle.extend("u1", "r1", ttl_ms=6 * HOUR, now_ms=T0)  # a long-running request
    next_session = T0 + 5 * HOUR
    assert isinstance(reserve(oracle, "r2", 10, now=next_session), Granted)
    # r1 finishes after its session window closed: the new window isn't charged for it,
    # but the week, which hasn't rolled over, is.
    oracle.settle("u1", "r1", 30, now_ms=next_session)
    now = views(oracle, next_session)
    assert (now["session"].used, now["session"].reserved) == (0, 10)
    assert (now["week"].used, now["week"].reserved) == (30, 10)


def test_actual_usage_may_exceed_the_estimate(oracle: QuotaOracle) -> None:
    assert isinstance(reserve(oracle, "r1", 90), Granted)
    oracle.settle("u1", "r1", 104, now_ms=T0)  # the prompt was longer than estimated
    assert views(oracle)["session"].used == 104
    assert views(oracle)["session"].remaining == 0
    assert isinstance(reserve(oracle, "r2", 0), Granted)  # free requests still fit
    assert isinstance(reserve(oracle, "r3", 1), Denied)


def test_expired_reservations_are_released_by_the_next_reserve(oracle: QuotaOracle) -> None:
    assert isinstance(reserve(oracle, "r1", 60), Granted)
    assert isinstance(reserve(oracle, "r2", 30), Granted)
    assert oracle.extend("u1", "r2", ttl_ms=2 * TTL, now_ms=T0)
    after_ttl = T0 + TTL
    assert isinstance(reserve(oracle, "r3", 60, now=after_ttl), Granted)  # r1 was collected
    assert oracle.settle("u1", "r1", 60, now_ms=after_ttl) == Finished(
        Outcome.EXPIRED, applied=False
    )
    assert oracle.settle("u1", "r2", 30, now_ms=after_ttl).applied  # r2 was kept alive
    assert not oracle.extend("u1", "r1", ttl_ms=TTL, now_ms=after_ttl)


def test_garbage_collection_is_batched(oracle: QuotaOracle) -> None:
    for i in range(GC_BATCH + 3):
        assert isinstance(reserve(oracle, f"r{i}", 0), Granted)
    assert oracle.collect_expired("u1", now_ms=T0 + TTL) == GC_BATCH
    assert oracle.collect_expired("u1", now_ms=T0 + TTL) == 3
    assert oracle.collect_expired("nobody", now_ms=T0) == 0


def test_a_rebuild_restores_used_credits_from_the_ledger() -> None:
    session_start = T0 - HOUR
    ledger = [
        LedgerEntry("a", 30, {"session": session_start, "week": WEEK.start_ms}),
        LedgerEntry("b", 50, {"session": session_start, "week": WEEK.start_ms}),
        # An older session window, and last week: both already reset.
        LedgerEntry("c", 70, {"session": session_start - 6 * HOUR, "week": WEEK.start_ms}),
        LedgerEntry("d", 99, {"session": session_start - 30 * 24 * HOUR, "week": 0}),
    ]
    engine = QuotaOracle()
    assert engine.rebuild("u1", buckets(), ledger, now_ms=T0, if_epoch=None)
    now = {v.name: v for v in engine.snapshot("u1", buckets(), now_ms=T0) or ()}
    assert now["session"] == BucketView(
        "session", 100, 80, 0, session_start, session_start + 5 * HOUR
    )
    assert now["week"].used == 150
    # Thresholds the user already passed are not announced again.
    assert isinstance(
        engine.reserve("u1", "r1", buckets(), Estimate.flat(5), ttl_ms=TTL, now_ms=T0), Granted
    )
    assert engine.settle("u1", "r1", 5, now_ms=T0).warnings == ()


def test_only_one_of_two_racing_rebuilds_applies(oracle: QuotaOracle) -> None:
    assert oracle.epoch("u1") == 1
    assert not oracle.rebuild("u1", buckets(), [], now_ms=T0, if_epoch=None)
    assert oracle.rebuild("u1", buckets(), [], now_ms=T0, if_epoch=1)
    assert oracle.epoch("u1") == 2


def test_losing_valkey_data_drops_in_flight_reservations(oracle: QuotaOracle) -> None:
    assert isinstance(reserve(oracle, "r1", 10), Granted)
    oracle.flush()
    assert oracle.dump("u1", now_ms=T0) is None
    assert oracle.rebuild("u1", buckets(), [], now_ms=T0, if_epoch=None)
    assert oracle.settle("u1", "r1", 10, now_ms=T0).outcome is Outcome.UNKNOWN
    assert views(oracle)["session"].used == 0


@pytest.mark.parametrize(
    ("make", "error"),
    [
        (lambda: Bucket("Session", 1, Rolling(1)), "bucket name"),
        (lambda: Bucket("s", -1, Rolling(1)), "limit"),
        (lambda: Rolling(0), "positive"),
        (lambda: Fixed(5, 5), "end after"),
        (lambda: Estimate(0, 1, min_units=2, max_units=1), "min_units"),
        (lambda: Estimate(2**53 - 10, 1, max_units=100), "too big"),
    ],
)
def test_rejects_invalid_inputs(make: object, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        make()  # type: ignore[operator]


def test_rejects_duplicate_buckets_and_bad_ids(oracle: QuotaOracle) -> None:
    twice = [*buckets(), Bucket("week", 5, WEEK)]
    with pytest.raises(ValueError, match="unique"):
        oracle.reserve("u1", "r1", twice, Estimate.flat(1), ttl_ms=TTL, now_ms=T0)
    with pytest.raises(ValueError, match="reservation id"):
        oracle.reserve("u1", "R 1", buckets(), Estimate.flat(1), ttl_ms=TTL, now_ms=T0)
