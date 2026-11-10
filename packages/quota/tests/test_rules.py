"""The engine's rules, one example each, checked against both engines.

Every test runs twice: on the Python reference model and on the Lua scripts in Valkey.
The engines share an interface; tests annotate them as `QuotaOracle`, the specification.
The state-machine tests check the same rules with random event sequences.
"""

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
def engine(fresh: QuotaOracle) -> QuotaOracle:
    """An engine with one user, `u1`, whose counters exist but are empty."""
    assert fresh.rebuild("u1", buckets(), [], now_ms=T0, if_epoch=None)
    return fresh


def reserve(
    engine: QuotaOracle, rid: str, credits: int, *, now: int = T0, **limits: int
) -> Granted | Denied:
    result = engine.reserve(
        "u1", rid, buckets(**limits), Estimate.flat(credits), ttl_ms=TTL, now_ms=now
    )
    assert not isinstance(result, NeedsRebuild)
    return result


def views(engine: QuotaOracle, now: int = T0) -> dict[str, BucketView]:
    snapshot = engine.snapshot("u1", buckets(), now_ms=now)
    assert snapshot is not None
    return {v.name: v for v in snapshot}


def test_a_user_without_counters_needs_a_rebuild_first(fresh: QuotaOracle) -> None:
    engine = fresh
    result = engine.reserve("new", "r1", buckets(), Estimate.flat(1), ttl_ms=TTL, now_ms=T0)
    assert result == NeedsRebuild()
    assert engine.snapshot("new", buckets(), now_ms=T0) is None
    assert engine.settle("new", "r1", 1, now_ms=T0) == Finished(Outcome.UNKNOWN, applied=False)


def test_the_session_opens_at_the_first_admitted_request(engine: QuotaOracle) -> None:
    assert views(engine)["session"].start_ms is None
    later = T0 + 7 * 60_000
    grant = reserve(engine, "r1", 10, now=later)
    assert isinstance(grant, Granted)
    session = views(engine, later)["session"]
    assert (session.start_ms, session.reset_ms) == (later, later + 5 * HOUR)
    assert grant.windows == {"session": later, "week": WEEK.start_ms}


def test_a_denial_changes_nothing_and_opens_no_window(engine: QuotaOracle) -> None:
    before = engine.dump("u1", now_ms=T0)
    denied = reserve(engine, "r1", 101)
    assert denied == Denied("r1", ("session",), retry_at_ms=None)  # 101 never fits in 100
    assert engine.dump("u1", now_ms=T0) == before


def test_a_denial_says_when_the_blocking_window_resets(engine: QuotaOracle) -> None:
    assert isinstance(reserve(engine, "r1", 60), Granted)
    later = T0 + 60_000  # r1 is still running
    assert reserve(engine, "r2", 60, now=later) == Denied(
        "r2", ("session",), retry_at_ms=T0 + 5 * HOUR
    )
    # Blocked by both: the request waits for the later reset.
    assert reserve(engine, "r3", 60, now=later, week=100) == Denied(
        "r3", ("session", "week"), retry_at_ms=WEEK.end_ms
    )


def test_admission_counts_reserved_credits(engine: QuotaOracle) -> None:
    assert isinstance(reserve(engine, "r1", 50), Granted)
    assert isinstance(reserve(engine, "r2", 50), Granted)
    assert isinstance(reserve(engine, "r3", 1), Denied)
    assert views(engine)["session"] == BucketView("session", 100, 0, 100, T0, T0 + 5 * HOUR)


def test_answers_get_shorter_near_the_limit(engine: QuotaOracle) -> None:
    rates = Rates(input=1_000, output=4_000)
    # 20,000 prompt tokens cost 20 credits, leaving 80 credits: 20,000 output tokens.
    estimate = Estimate.tokens(rates, input_tokens=20_000, max_output=50_000)
    grant = engine.reserve("u1", "r1", buckets(), estimate, ttl_ms=TTL, now_ms=T0)
    assert grant == Granted("r1", 100, 20_000, {"session": T0, "week": WEEK.start_ms})


def test_the_tool_step_floor_denies_instead_of_shrinking(engine: QuotaOracle) -> None:
    rates = Rates(input=1_000, output=4_000)
    assert isinstance(reserve(engine, "r0", 98), Granted)  # 2 credits left: 500 tokens
    tool_step = Estimate.tokens(rates, input_tokens=0, max_output=4_096, min_output=512)
    assert isinstance(
        engine.reserve("u1", "r1", buckets(), tool_step, ttl_ms=TTL, now_ms=T0), Denied
    )
    plain = Estimate.tokens(rates, input_tokens=0, max_output=4_096)
    grant = engine.reserve("u1", "r2", buckets(), plain, ttl_ms=TTL, now_ms=T0)
    assert isinstance(grant, Granted)
    assert grant.units == 500


def test_settling_moves_credits_from_reserved_to_used(engine: QuotaOracle) -> None:
    assert isinstance(reserve(engine, "r1", 40), Granted)
    assert engine.settle("u1", "r1", 25, now_ms=T0) == Finished(Outcome.SETTLED, applied=True)
    assert (views(engine)["session"].used, views(engine)["session"].reserved) == (25, 0)
    assert views(engine)["week"].used == 25


def test_settle_and_release_are_idempotent(engine: QuotaOracle) -> None:
    assert isinstance(reserve(engine, "r1", 40), Granted)
    assert isinstance(reserve(engine, "r2", 40), Granted)
    engine.settle("u1", "r1", 30, now_ms=T0)
    engine.release("u1", "r2", now_ms=T0)
    after = engine.dump("u1", now_ms=T0)
    assert engine.settle("u1", "r1", 30, now_ms=T0) == Finished(Outcome.SETTLED, applied=False)
    assert engine.settle("u1", "r2", 30, now_ms=T0) == Finished(Outcome.RELEASED, applied=False)
    assert engine.release("u1", "r1", now_ms=T0) == Finished(Outcome.SETTLED, applied=False)
    assert engine.dump("u1", now_ms=T0) == after
    assert engine.settle("u1", "never", 1, now_ms=T0).outcome is Outcome.UNKNOWN


def test_a_retried_reserve_gets_the_same_grant(engine: QuotaOracle) -> None:
    first = reserve(engine, "r1", 40)
    assert reserve(engine, "r1", 40) == first
    engine.settle("u1", "r1", 40, now_ms=T0)
    assert reserve(engine, "r1", 40) == first  # still remembered after it finished
    assert views(engine)["session"].used == 40  # and nothing was charged twice


def test_finished_reservations_are_forgotten_after_two_days(engine: QuotaOracle) -> None:
    assert isinstance(reserve(engine, "r1", 10), Granted)
    engine.settle("u1", "r1", 10, now_ms=T0)
    much_later = T0 + DONE_TTL_MS
    assert engine.settle("u1", "r1", 10, now_ms=much_later).outcome is Outcome.UNKNOWN


def test_warnings_fire_once_per_window(engine: QuotaOracle) -> None:
    assert isinstance(reserve(engine, "r1", 80), Granted)
    first = engine.settle("u1", "r1", 80, now_ms=T0)
    assert first.warnings == (UsageWarning("session", 50), UsageWarning("session", 75))
    assert isinstance(reserve(engine, "r2", 15), Granted)
    assert engine.settle("u1", "r2", 15, now_ms=T0).warnings == (UsageWarning("session", 90),)
    # In the next session window the counters, and the warnings, start over.
    next_session = T0 + 5 * HOUR
    assert isinstance(reserve(engine, "r3", 60, now=next_session), Granted)
    assert engine.settle("u1", "r3", 60, now_ms=next_session).warnings == (
        UsageWarning("session", 50),
    )


def test_usage_counts_toward_the_window_that_admitted_it(engine: QuotaOracle) -> None:
    assert isinstance(reserve(engine, "r1", 30), Granted)
    assert engine.extend("u1", "r1", ttl_ms=6 * HOUR, now_ms=T0)  # a long-running request
    next_session = T0 + 5 * HOUR
    assert isinstance(reserve(engine, "r2", 10, now=next_session), Granted)
    # r1 finishes after its session window closed: the new window isn't charged for it,
    # but the week, which hasn't rolled over, is.
    engine.settle("u1", "r1", 30, now_ms=next_session)
    now = views(engine, next_session)
    assert (now["session"].used, now["session"].reserved) == (0, 10)
    assert (now["week"].used, now["week"].reserved) == (30, 10)


def test_actual_usage_may_exceed_the_estimate(engine: QuotaOracle) -> None:
    assert isinstance(reserve(engine, "r1", 90), Granted)
    engine.settle("u1", "r1", 104, now_ms=T0)  # the prompt was longer than estimated
    assert views(engine)["session"].used == 104
    assert views(engine)["session"].remaining == 0
    assert isinstance(reserve(engine, "r2", 0), Granted)  # free requests still fit
    assert isinstance(reserve(engine, "r3", 1), Denied)


def test_expired_reservations_are_released_by_the_next_reserve(engine: QuotaOracle) -> None:
    assert isinstance(reserve(engine, "r1", 60), Granted)
    assert isinstance(reserve(engine, "r2", 30), Granted)
    assert engine.extend("u1", "r2", ttl_ms=2 * TTL, now_ms=T0)
    after_ttl = T0 + TTL
    assert isinstance(reserve(engine, "r3", 60, now=after_ttl), Granted)  # r1 was collected
    assert not engine.extend("u1", "r1", ttl_ms=TTL, now_ms=after_ttl)  # its hold is gone
    assert engine.settle("u1", "r2", 30, now_ms=after_ttl).applied  # r2 was kept alive


def test_a_settle_after_expiry_is_still_charged(engine: QuotaOracle) -> None:
    """Expiry gives back the hold, not the bill.

    A settle follows a ledger row, so the usage is real: a slow request, or the partial
    answer of a crashed one. The counters must follow the ledger.
    """
    assert isinstance(reserve(engine, "r1", 60), Granted)
    after_ttl = T0 + TTL
    assert isinstance(reserve(engine, "r2", 60, now=after_ttl), Granted)  # r1's hold was released
    late = engine.settle("u1", "r1", 55, now_ms=after_ttl)
    assert late == Finished(Outcome.SETTLED, applied=True, warnings=(UsageWarning("session", 50),))
    now = views(engine, after_ttl)
    assert (now["session"].used, now["session"].reserved) == (55, 60)  # over the limit, and shown
    assert now["week"].used == 55
    # It has ended now, so settling it again changes nothing.
    assert engine.settle("u1", "r1", 55, now_ms=after_ttl) == Finished(
        Outcome.SETTLED, applied=False
    )
    assert views(engine, after_ttl)["session"].used == 55


def test_a_release_after_expiry_ends_the_reservation(engine: QuotaOracle) -> None:
    assert isinstance(reserve(engine, "r1", 60), Granted)
    after_ttl = T0 + TTL
    assert engine.collect_expired("u1", now_ms=after_ttl) == 1
    assert engine.release("u1", "r1", now_ms=after_ttl) == Finished(Outcome.RELEASED, applied=True)
    # The first finish decided how it ended: a settle after it is a retry.
    assert engine.settle("u1", "r1", 60, now_ms=after_ttl) == Finished(
        Outcome.RELEASED, applied=False
    )
    assert views(engine, after_ttl)["session"].used == 0


def test_a_late_request_cannot_reopen_last_weeks_window(engine: QuotaOracle) -> None:
    """Two requests race across the weekly reset, and arrive in the wrong order.

    Time is an argument, so callers can disagree about it: the request that read its
    clock just before the reset reaches the engine after one that read it just after.
    """
    reset = WEEK.end_ms
    next_week = Fixed(reset, reset + 7 * 24 * HOUR)
    after_reset = [Bucket("week", 1_000, next_week)]
    before_reset = [Bucket("week", 1_000, WEEK)]
    first = engine.reserve("u1", "r1", after_reset, Estimate.flat(40), ttl_ms=TTL, now_ms=reset)
    assert isinstance(first, Granted)
    late = engine.reserve("u1", "r2", before_reset, Estimate.flat(40), ttl_ms=TTL, now_ms=reset - 1)
    assert isinstance(late, Granted)
    assert late.windows == {"week": reset}  # it joined the window that is open
    (week,) = engine.snapshot("u1", after_reset, now_ms=reset) or ()
    assert (week.start_ms, week.reserved) == (reset, 80)  # and r1's hold survived
    # Both still settle into the new week.
    engine.settle("u1", "r1", 40, now_ms=reset)
    engine.settle("u1", "r2", 40, now_ms=reset)
    (week,) = engine.snapshot("u1", after_reset, now_ms=reset) or ()
    assert (week.used, week.reserved) == (80, 0)


def test_garbage_collection_is_batched(engine: QuotaOracle) -> None:
    for i in range(GC_BATCH + 3):
        assert isinstance(reserve(engine, f"r{i}", 0), Granted)
    assert engine.collect_expired("u1", now_ms=T0 + TTL) == GC_BATCH
    assert engine.collect_expired("u1", now_ms=T0 + TTL) == 3
    assert engine.collect_expired("nobody", now_ms=T0) == 0


def test_a_rebuild_restores_used_credits_from_the_ledger(fresh: QuotaOracle) -> None:
    session_start = T0 - HOUR
    ledger = [
        LedgerEntry("a", 30, {"session": session_start, "week": WEEK.start_ms}),
        LedgerEntry("b", 50, {"session": session_start, "week": WEEK.start_ms}),
        # An older session window, and last week: both already reset.
        LedgerEntry("c", 70, {"session": session_start - 6 * HOUR, "week": WEEK.start_ms}),
        LedgerEntry("d", 99, {"session": session_start - 30 * 24 * HOUR, "week": 0}),
    ]
    engine = fresh
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


def test_only_one_of_two_racing_rebuilds_applies(engine: QuotaOracle) -> None:
    assert engine.epoch("u1") == 1
    assert not engine.rebuild("u1", buckets(), [], now_ms=T0, if_epoch=None)
    assert engine.rebuild("u1", buckets(), [], now_ms=T0, if_epoch=1)
    assert engine.epoch("u1") == 2


def test_losing_valkey_data_drops_in_flight_reservations(engine: QuotaOracle) -> None:
    assert isinstance(reserve(engine, "r1", 10), Granted)
    engine.flush()
    assert engine.dump("u1", now_ms=T0) is None
    assert engine.rebuild("u1", buckets(), [], now_ms=T0, if_epoch=None)
    assert engine.settle("u1", "r1", 10, now_ms=T0).outcome is Outcome.UNKNOWN
    assert views(engine)["session"].used == 0


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


def test_rejects_duplicate_buckets_and_bad_ids(engine: QuotaOracle) -> None:
    twice = [*buckets(), Bucket("week", 5, WEEK)]
    with pytest.raises(ValueError, match="unique"):
        engine.reserve("u1", "r1", twice, Estimate.flat(1), ttl_ms=TTL, now_ms=T0)
    with pytest.raises(ValueError, match="reservation id"):
        engine.reserve("u1", "R 1", buckets(), Estimate.flat(1), ttl_ms=TTL, now_ms=T0)
