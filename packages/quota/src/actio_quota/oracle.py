"""The reference model of the quota engine: plain Python, kept in memory.

This is the specification. The Valkey engine runs the same operations as Lua scripts,
and the differential tests drive both with the same random event sequences and require
the same results and the same stored state (`dump`). Production never uses this class.

Per user, the engine stores:

- **buckets:** for each bucket, its current window (`start_ms`, `reset_ms`), the `limit`
  it was last admitted under, credits `used` and `reserved`, and the warning thresholds
  already announced in this window;
- **reservations:** admitted requests that haven't finished, each with its grant and an
  expiry time that the holder keeps pushing back with `extend`;
- **done markers:** how each finished reservation ended, kept for `DONE_TTL_MS`;
- **epoch:** bumped by every rebuild. A user with no epoch has no counters yet.

Time only matters through the `now_ms` arguments. Expired state is never deleted on a
timer: each operation ignores or collects it when it next looks, so replaying the same
events at the same `now_ms` values always gives the same state.
"""

import copy
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from actio_quota.cost import check_count
from actio_quota.model import (
    DONE_TTL_MS,
    GC_BATCH,
    WARN_THRESHOLDS,
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
    ReserveResult,
    Rolling,
    UsageWarning,
    check_rid,
)


@dataclass
class _BucketState:
    start_ms: int
    reset_ms: int
    limit: int
    used: int = 0
    reserved: int = 0
    warned: set[int] = field(default_factory=set)


@dataclass
class _Reservation:
    grant: Granted
    expires_ms: int


@dataclass
class _Done:
    outcome: Outcome
    grant: Granted
    expires_ms: int


@dataclass
class _User:
    epoch: int
    buckets: dict[str, _BucketState] = field(default_factory=dict)
    reservations: dict[str, _Reservation] = field(default_factory=dict)
    done: dict[str, _Done] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _Current:
    """A bucket's window as of `now_ms`. `state` is None when the window is closed."""

    spec: Bucket
    state: _BucketState | None
    start_ms: int | None
    reset_ms: int | None

    @property
    def used(self) -> int:
        return self.state.used if self.state else 0

    @property
    def reserved(self) -> int:
        return self.state.reserved if self.state else 0


def _current(user: _User, spec: Bucket, now_ms: int) -> _Current:
    """Find the bucket's live window, treating a past window as closed (rollover)."""
    stored = user.buckets.get(spec.name)
    match spec.window:
        case Rolling():
            if stored is not None and now_ms < stored.reset_ms:
                return _Current(spec, stored, stored.start_ms, stored.reset_ms)
            # Closed: it opens when a request is admitted, so it has no times yet.
            return _Current(spec, None, None, None)
        case Fixed(start_ms=start, end_ms=end):
            if stored is not None and stored.start_ms == start:
                return _Current(spec, stored, stored.start_ms, stored.reset_ms)
            return _Current(spec, None, start, end)


def _check_buckets(buckets: Sequence[Bucket]) -> None:
    names = [b.name for b in buckets]
    if len(set(names)) != len(names):
        raise ValueError(f"bucket names must be unique, got {names}")


def _warn(name: str, state: _BucketState) -> list[UsageWarning]:
    """Announce each threshold the bucket has newly crossed, once per window."""
    if state.limit == 0:
        return []
    crossed = []
    for percent in WARN_THRESHOLDS:
        if percent not in state.warned and state.used * 100 >= state.limit * percent:
            state.warned.add(percent)
            crossed.append(UsageWarning(name, percent))
    return crossed


class QuotaOracle:
    """The quota engine's reference model, for any number of users."""

    def __init__(self) -> None:
        self._users: dict[str, _User] = {}

    # --- Admission -------------------------------------------------------------------

    def reserve(
        self,
        user_id: str,
        rid: str,
        buckets: Sequence[Bucket],
        estimate: Estimate,
        *,
        ttl_ms: int,
        now_ms: int,
    ) -> ReserveResult:
        """Admit a request if it fits in every bucket, and hold its credits.

        1. A retry of an admitted `rid` gets the original grant back.
        2. Up to `GC_BATCH` expired reservations are released.
        3. The request gets the most units that fit every bucket's
           `limit - used - reserved`, down to `estimate.min_units`.
           A request whose smallest cost is 0 credits always fits.
        4. If even that doesn't fit, it's denied and nothing else is written. Otherwise
           closed windows open (a rolling one at `now_ms`), each bucket's `reserved`
           grows by the grant's credits, and the reservation expires at
           `now_ms + ttl_ms` unless extended.
        """
        check_rid(rid)
        check_count("ttl_ms", ttl_ms)
        _check_buckets(buckets)
        user = self._users.get(user_id)
        if user is None:
            return NeedsRebuild()

        if rid in user.reservations:
            return user.reservations[rid].grant
        done = user.done.get(rid)
        if done is not None and now_ms < done.expires_ms:
            return done.grant

        self._collect(user, now_ms)

        current = [_current(user, spec, now_ms) for spec in buckets]
        smallest = estimate.credits(estimate.min_units)
        # A free request always fits, even when usage ran over the limit: that keeps
        # "Use Local (free)" working once the paid models are blocked.
        short = [
            c for c in current if smallest > 0 and c.used + c.reserved + smallest > c.spec.limit
        ]
        if short:
            # A closed window has room for anything up to its limit, so a short bucket is
            # either open (it frees up at its reset) or too small for the request at all.
            never = any(smallest > c.spec.limit for c in short)
            retry_at = None if never else max(c.reset_ms or 0 for c in short)
            return Denied(rid, tuple(c.spec.name for c in short), retry_at)

        units = estimate.max_units
        if current and estimate.unit_micro > 0:
            headroom = min(c.spec.limit - c.used - c.reserved for c in current)
            room_micro = headroom * 1_000_000 - estimate.base_micro
            units = min(units, max(estimate.min_units, room_micro // estimate.unit_micro))
        credits = estimate.credits(units)

        windows: dict[str, int] = {}
        for c in current:
            state = c.state
            if state is None:
                match c.spec.window:
                    case Rolling(duration_ms=duration):
                        state = _BucketState(now_ms, now_ms + duration, c.spec.limit)
                    case Fixed(start_ms=start, end_ms=end):
                        state = _BucketState(start, end, c.spec.limit)
                user.buckets[c.spec.name] = state
            state.limit = c.spec.limit
            state.reserved += credits
            windows[c.spec.name] = state.start_ms

        grant = Granted(rid, credits, units, windows)
        user.reservations[rid] = _Reservation(grant, now_ms + ttl_ms)
        return grant

    # --- Finishing a reservation -----------------------------------------------------

    def settle(self, user_id: str, rid: str, credits: int, *, now_ms: int) -> Finished:
        """Record what an admitted request actually cost.

        Only windows that are still the ones that admitted it change: their `reserved`
        drops by the held credits and their `used` grows by `credits`. `credits` may be
        more than was held (the prompt estimate was low): it is recorded as it is.
        """
        check_count("credits", credits)
        return self._finish(user_id, rid, Outcome.SETTLED, credits, now_ms)

    def release(self, user_id: str, rid: str, *, now_ms: int) -> Finished:
        """Give back an admitted request's held credits without charging anything."""
        return self._finish(user_id, rid, Outcome.RELEASED, 0, now_ms)

    def extend(self, user_id: str, rid: str, *, ttl_ms: int, now_ms: int) -> bool:
        """Keep a running request's reservation alive until `now_ms + ttl_ms`."""
        check_count("ttl_ms", ttl_ms)
        user = self._users.get(user_id)
        if user is None or rid not in user.reservations:
            return False
        user.reservations[rid].expires_ms = now_ms + ttl_ms
        return True

    def collect_expired(self, user_id: str, *, now_ms: int) -> int:
        """Release up to `GC_BATCH` expired reservations, as `reserve` does first."""
        user = self._users.get(user_id)
        return 0 if user is None else self._collect(user, now_ms)

    def _collect(self, user: _User, now_ms: int) -> int:
        expired = sorted(
            (r.expires_ms, rid) for rid, r in user.reservations.items() if r.expires_ms <= now_ms
        )[:GC_BATCH]
        for _, rid in expired:
            self._end(user, rid, Outcome.EXPIRED, 0, now_ms)
        return len(expired)

    def _finish(
        self, user_id: str, rid: str, outcome: Outcome, credits: int, now_ms: int
    ) -> Finished:
        user = self._users.get(user_id)
        if user is None:
            return Finished(Outcome.UNKNOWN, applied=False)
        if rid in user.reservations:
            return Finished(outcome, True, self._end(user, rid, outcome, credits, now_ms))
        done = user.done.get(rid)
        if done is not None and now_ms < done.expires_ms:
            return Finished(done.outcome, applied=False)
        return Finished(Outcome.UNKNOWN, applied=False)

    def _end(
        self, user: _User, rid: str, outcome: Outcome, credits: int, now_ms: int
    ) -> tuple[UsageWarning, ...]:
        grant = user.reservations.pop(rid).grant
        warnings: list[UsageWarning] = []
        for name, start in grant.windows.items():
            state = user.buckets.get(name)
            if state is None or state.start_ms != start:
                continue  # that window has rolled over; its counters are gone
            state.reserved -= grant.credits
            if outcome is Outcome.SETTLED:
                state.used += credits
                warnings += _warn(name, state)
        user.done[rid] = _Done(outcome, grant, now_ms + DONE_TTL_MS)
        return tuple(warnings)

    # --- Reading and recovering ------------------------------------------------------

    def snapshot(
        self, user_id: str, buckets: Sequence[Bucket], *, now_ms: int
    ) -> tuple[BucketView, ...] | None:
        """The buckets as of `now_ms`, for the usage meter. `None` means: rebuild first."""
        _check_buckets(buckets)
        user = self._users.get(user_id)
        if user is None:
            return None
        views = []
        for spec in buckets:
            c = _current(user, spec, now_ms)
            views.append(
                BucketView(spec.name, spec.limit, c.used, c.reserved, c.start_ms, c.reset_ms)
            )
        return tuple(views)

    def rebuild(
        self,
        user_id: str,
        buckets: Sequence[Bucket],
        ledger: Iterable[LedgerEntry],
        *,
        now_ms: int,
        if_epoch: int | None,
    ) -> bool:
        """Replace the user's counters with totals recomputed from the ledger.

        Applies only if the user's epoch is still `if_epoch` (`None`: no counters), so
        two processes racing to rebuild don't both apply. Reservations in flight are
        dropped, so their usage goes uncounted here: in the user's favour, and still in
        the ledger. Thresholds already crossed are marked as announced.
        """
        _check_buckets(buckets)
        user = self._users.get(user_id)
        if (user.epoch if user else None) != if_epoch:
            return False
        rebuilt = _User(epoch=(if_epoch or 0) + 1)
        entries = list(ledger)
        for spec in buckets:
            in_window: list[int] = []
            match spec.window:
                case Fixed(start_ms=start, end_ms=end):
                    in_window = [e.credits for e in entries if e.windows.get(spec.name) == start]
                    window = (start, end)
                case Rolling(duration_ms=duration):
                    starts = [
                        s
                        for e in entries
                        if (s := e.windows.get(spec.name)) is not None and now_ms < s + duration
                    ]
                    if not starts:
                        continue
                    latest = max(starts)
                    in_window = [e.credits for e in entries if e.windows.get(spec.name) == latest]
                    window = (latest, latest + duration)
            if not in_window:
                continue
            state = _BucketState(*window, limit=spec.limit, used=sum(in_window))
            _warn(spec.name, state)
            rebuilt.buckets[spec.name] = state
        self._users[user_id] = rebuilt
        return True

    def flush(self) -> None:
        """Forget every user's counters, as if Valkey lost its data."""
        self._users.clear()

    def epoch(self, user_id: str) -> int | None:
        user = self._users.get(user_id)
        return None if user is None else user.epoch

    def dump(self, user_id: str, *, now_ms: int) -> dict[str, Any] | None:
        """The user's stored state as plain data, for comparing engines.

        Done markers past their expiry are left out: the engines may differ in when
        they physically delete them, but never in whether they still count.
        """
        user = self._users.get(user_id)
        if user is None:
            return None
        return {
            "epoch": user.epoch,
            "buckets": {
                name: {
                    "start_ms": s.start_ms,
                    "reset_ms": s.reset_ms,
                    "limit": s.limit,
                    "used": s.used,
                    "reserved": s.reserved,
                    "warned": sorted(s.warned),
                }
                for name, s in sorted(user.buckets.items())
            },
            "reservations": {
                rid: {"expires_ms": r.expires_ms, **_grant_data(r.grant)}
                for rid, r in sorted(user.reservations.items())
            },
            "done": {
                rid: {"outcome": str(d.outcome), "expires_ms": d.expires_ms}
                for rid, d in sorted(user.done.items())
                if now_ms < d.expires_ms
            },
        }

    def copy(self) -> "QuotaOracle":
        """An independent copy, so a test can compare 'before' and 'after'."""
        twin = QuotaOracle()
        twin._users = copy.deepcopy(self._users)
        return twin


def _grant_data(grant: Granted) -> dict[str, Any]:
    return {"credits": grant.credits, "units": grant.units, "windows": dict(grant.windows)}


def ledger_total(entries: Iterable[LedgerEntry], bucket: str, start_ms: int) -> int:
    """Credits the ledger attributes to one window of one bucket."""
    return sum(e.credits for e in entries if e.windows.get(bucket) == start_ms)
