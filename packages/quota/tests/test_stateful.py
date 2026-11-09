"""Random event sequences against the engines, checking invariants after every step.

- `TestQuotaOracle` runs them against the Python reference model.
- `test_valkey_matches_the_oracle` runs the same machine with every call sent to both
  engines: the results, and the complete stored state, must be identical at every step.
  That is the differential test: the Lua scripts must behave exactly like the oracle.
"""

from typing import Any

from hypothesis import strategies as st
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    consumes,
    initialize,
    invariant,
    multiple,
    precondition,
    rule,
    run_state_machine_as_test,
)

from actio_quota import (
    Bucket,
    BucketView,
    CalendarReset,
    Denied,
    Estimate,
    Fixed,
    Granted,
    LedgerEntry,
    ManualClock,
    NeedsRebuild,
    Outcome,
    QuotaOracle,
    Rates,
    Rolling,
)
from actio_quota.model import WARN_THRESHOLDS, Finished, ReserveResult
from actio_quota.state import ledger_total

MINUTE = 60_000
HOUR = 60 * MINUTE
SESSION_LIMIT = 100
WEEK_LIMIT = 300
WEEKLY = CalendarReset.parse("MON 00:00 Europe/Istanbul")
RATES = Rates(input=1_000, output=4_000)
USER = "u1"

estimates = st.one_of(
    st.integers(0, 60).map(Estimate.flat),
    st.builds(
        lambda prompt, out, floor: Estimate.tokens(
            RATES, input_tokens=prompt, max_output=out, min_output=floor
        ),
        st.integers(0, 30_000),
        st.integers(0, 20_000),
        st.sampled_from([0, 1, 512]),
    ),
)


class QuotaMachine(RuleBasedStateMachine):
    granted = Bundle("granted")
    finished = Bundle("finished")

    def __init__(self, engine: QuotaOracle | None = None) -> None:
        super().__init__()
        self.oracle = engine or QuotaOracle()
        self.clock = ManualClock(1_800_000_000_000)
        self.ledger: list[LedgerEntry] = []
        self.counter = 0

    def buckets(self) -> list[Bucket]:
        start, end = WEEKLY.window(self.clock.now_ms())
        return [
            Bucket("session", SESSION_LIMIT, Rolling(5 * HOUR)),
            Bucket("week", WEEK_LIMIT, Fixed(start, end)),
        ]

    def views(self) -> dict[str, BucketView]:
        snapshot = self.oracle.snapshot(USER, self.buckets(), now_ms=self.clock.now_ms())
        assert snapshot is not None
        return {v.name: v for v in snapshot}

    def rebuild(self) -> None:
        epoch = self.oracle.epoch(USER)
        assert self.oracle.rebuild(
            USER, self.buckets(), self.ledger, now_ms=self.clock.now_ms(), if_epoch=epoch
        )

    @initialize()
    def start(self) -> None:
        self.rebuild()

    # --- Events ----------------------------------------------------------------------

    @rule(target=granted, estimate=estimates, ttl=st.sampled_from([MINUTE, 10 * MINUTE, 6 * HOUR]))
    def reserve(self, estimate: Estimate, ttl: int) -> object:
        self.counter += 1
        rid = f"r{self.counter}"
        now = self.clock.now_ms()
        # What the engine should see: the state after collecting expired reservations.
        expected = self.oracle.copy()
        expected.collect_expired(USER, now_ms=now)
        before = {v.name: v for v in expected.snapshot(USER, self.buckets(), now_ms=now) or ()}
        smallest = estimate.credits(estimate.min_units)
        fits = smallest == 0 or all(
            v.used + v.reserved + smallest <= v.limit for v in before.values()
        )

        result = self.oracle.reserve(USER, rid, self.buckets(), estimate, ttl_ms=ttl, now_ms=now)

        assert not isinstance(result, NeedsRebuild)
        assert isinstance(result, Granted) == fits, "admission must be exact"
        if isinstance(result, Denied):
            # A denial writes nothing beyond the garbage collection every call does.
            assert self.oracle.dump(USER, now_ms=now) == expected.dump(USER, now_ms=now)
            return multiple()
        assert estimate.min_units <= result.units <= estimate.max_units
        assert result.credits == estimate.credits(result.units)
        after = self.views()
        for name, view in after.items():
            if smallest > 0:
                assert view.used + view.reserved <= view.limit, f"{name} overcommitted"
            if before[name].start_ms is None:  # the window opens at the first admission
                assert view.start_ms == now
        # Shrinking stops exactly at the limit: one more unit wouldn't have fit.
        if result.units < estimate.max_units:
            one_more = estimate.credits(result.units + 1)
            assert any(
                v.used + v.reserved - result.credits + one_more > v.limit for v in after.values()
            )
        return result

    @rule(target=finished, grant=consumes(granted), error=st.integers(-30, 30))
    def settle(self, grant: Granted, error: int) -> Granted:
        actual = max(0, grant.credits + error)  # the prompt estimate may be off either way
        self.ledger.append(LedgerEntry(grant.rid, actual, grant.windows))  # the ledger first
        result = self.oracle.settle(USER, grant.rid, actual, now_ms=self.clock.now_ms())
        assert result.outcome in {Outcome.SETTLED, Outcome.EXPIRED, Outcome.UNKNOWN}
        for warning in result.warnings:
            assert warning.percent in WARN_THRESHOLDS
        return grant

    @rule(target=finished, grant=consumes(granted))
    def release(self, grant: Granted) -> Granted:
        result = self.oracle.release(USER, grant.rid, now_ms=self.clock.now_ms())
        assert result.outcome in {Outcome.RELEASED, Outcome.EXPIRED, Outcome.UNKNOWN}
        return grant

    @rule(grant=consumes(granted))
    def crash(self, grant: Granted) -> None:
        """The request's process dies: nobody settles or extends it again."""

    @rule(grant=granted, ttl=st.sampled_from([MINUTE, 6 * HOUR]))
    def extend(self, grant: Granted, ttl: int) -> None:
        self.oracle.extend(USER, grant.rid, ttl_ms=ttl, now_ms=self.clock.now_ms())

    @rule(grant=finished, settle=st.booleans())
    def retry_finished(self, grant: Granted, settle: bool) -> None:
        now = self.clock.now_ms()
        before = self.oracle.dump(USER, now_ms=now)
        if settle:
            result = self.oracle.settle(USER, grant.rid, grant.credits, now_ms=now)
        else:
            result = self.oracle.release(USER, grant.rid, now_ms=now)
        assert not result.applied, "finishing twice must change nothing"
        assert self.oracle.dump(USER, now_ms=now) == before

    @rule(grant=finished)
    def retry_reserve(self, grant: Granted) -> None:
        now = self.clock.now_ms()
        state = self.oracle.dump(USER, now_ms=now)
        if state is None or grant.rid not in state["done"]:
            return  # forgotten: a reserve with this id would be a new request
        result = self.oracle.reserve(
            USER, grant.rid, self.buckets(), Estimate.flat(0), ttl_ms=MINUTE, now_ms=now
        )
        assert result == grant, "a retried reserve gets the original grant back"
        assert self.oracle.dump(USER, now_ms=now) == state

    @rule(ms=st.sampled_from([1, MINUTE, 10 * MINUTE, HOUR, 5 * HOUR, 7 * 24 * HOUR]))
    def advance(self, ms: int) -> None:
        self.clock.advance(ms)

    @rule()
    def lose_valkey_data(self) -> None:
        self.oracle.flush()
        self.rebuild()
        for name, view in self.views().items():
            if view.start_ms is not None:
                assert view.used == ledger_total(self.ledger, name, view.start_ms)
            assert view.reserved == 0

    # --- Invariants ------------------------------------------------------------------

    @invariant()
    def reserved_matches_live_reservations(self) -> None:
        state = self.oracle.dump(USER, now_ms=self.clock.now_ms())
        assert state is not None
        for name, bucket in state["buckets"].items():
            held = sum(
                r["credits"]
                for r in state["reservations"].values()
                if r["windows"].get(name) == bucket["start_ms"]
            )
            assert bucket["reserved"] == held, name
            assert bucket["used"] >= 0
            assert bucket["reserved"] >= 0

    @invariant()
    @precondition(lambda self: self.ledger)
    def counters_never_exceed_the_ledger(self) -> None:
        """Counters only ever undercount, never invent usage."""
        for name, view in self.views().items():
            if view.start_ms is not None:
                assert view.used <= ledger_total(self.ledger, name, view.start_ms)


TestQuotaOracle = QuotaMachine.TestCase


class CheckedOracle(QuotaOracle):
    """The oracle, sending every call to a second engine too and requiring the same answer."""

    def __init__(self, other: Any) -> None:
        super().__init__()
        self.other = other

    def _check[T](self, name: str, ours: T, *args: Any, **kwargs: Any) -> T:
        theirs = getattr(self.other, name)(*args, **kwargs)
        assert theirs == ours, f"{name}{args}: Valkey gave {theirs!r}, the oracle {ours!r}"
        return ours

    def reserve(self, *args: Any, **kwargs: Any) -> ReserveResult:
        return self._check("reserve", super().reserve(*args, **kwargs), *args, **kwargs)

    def settle(self, *args: Any, **kwargs: Any) -> Finished:
        return self._check("settle", super().settle(*args, **kwargs), *args, **kwargs)

    def release(self, *args: Any, **kwargs: Any) -> Finished:
        return self._check("release", super().release(*args, **kwargs), *args, **kwargs)

    def extend(self, *args: Any, **kwargs: Any) -> bool:
        return self._check("extend", super().extend(*args, **kwargs), *args, **kwargs)

    def collect_expired(self, *args: Any, **kwargs: Any) -> int:
        return self._check(
            "collect_expired", super().collect_expired(*args, **kwargs), *args, **kwargs
        )

    def snapshot(self, *args: Any, **kwargs: Any) -> tuple[BucketView, ...] | None:
        return self._check("snapshot", super().snapshot(*args, **kwargs), *args, **kwargs)

    def rebuild(self, *args: Any, **kwargs: Any) -> bool:
        return self._check("rebuild", super().rebuild(*args, **kwargs), *args, **kwargs)

    def epoch(self, *args: Any, **kwargs: Any) -> int | None:
        return self._check("epoch", super().epoch(*args, **kwargs), *args, **kwargs)

    def dump(self, *args: Any, **kwargs: Any) -> dict[str, Any] | None:
        return self._check("dump", super().dump(*args, **kwargs), *args, **kwargs)

    def flush(self) -> None:
        super().flush()
        self.other.flush()


def test_valkey_matches_the_oracle(valkey_quota: Any) -> None:
    class Differential(QuotaMachine):
        def __init__(self) -> None:
            valkey_quota.flush()
            super().__init__(CheckedOracle(valkey_quota))

        @invariant()
        def same_stored_state(self) -> None:
            self.oracle.dump(USER, now_ms=self.clock.now_ms())

    run_state_machine_as_test(Differential)
