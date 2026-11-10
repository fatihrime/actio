"""Stored state, and the read-only logic both engines share.

What lives here is interpretation, not decisions: how a stored bucket looks at a given
time, what a rebuild should write, and the canonical dump format. Admission, shrinking,
expiry and settling are written twice, in `oracle` (Python) and `lua/` (Valkey), so the
differential tests compare two independent implementations of them.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from actio_quota.model import (
    WARN_THRESHOLDS,
    Bucket,
    BucketView,
    Fixed,
    Granted,
    LedgerEntry,
    Outcome,
    Rolling,
    UsageWarning,
)


@dataclass
class BucketState:
    """One bucket's stored window and counters."""

    start_ms: int
    reset_ms: int
    limit: int
    used: int = 0
    reserved: int = 0
    warned: set[int] = field(default_factory=set)
    """Thresholds from `WARN_THRESHOLDS` already announced in this window."""


@dataclass(frozen=True, slots=True)
class Current:
    """A bucket's window as of some time. `state` is None when the window is closed."""

    spec: Bucket
    state: BucketState | None
    start_ms: int | None
    reset_ms: int | None

    @property
    def used(self) -> int:
        return self.state.used if self.state else 0

    @property
    def reserved(self) -> int:
        return self.state.reserved if self.state else 0

    def view(self) -> BucketView:
        return BucketView(
            self.spec.name, self.spec.limit, self.used, self.reserved, self.start_ms, self.reset_ms
        )


def current(stored: BucketState | None, spec: Bucket, now_ms: int) -> Current:
    """The bucket's live window, treating a past window as closed (rollover).

    - A rolling window is live until `reset_ms`. Once closed, it has no times: it opens
      when the next request is admitted.
    - A calendar window is live while it starts at the time the caller passed in, or
      later. Windows only move forward: a caller with an older window read its clock
      before the reset and arrived after a caller that read it later, so it joins the
      stored window instead of reopening the past one.
    """
    match spec.window:
        case Rolling():
            if stored is not None and now_ms < stored.reset_ms:
                return Current(spec, stored, stored.start_ms, stored.reset_ms)
            return Current(spec, None, None, None)
        case Fixed(start_ms=start, end_ms=end):
            if stored is not None and stored.start_ms >= start:
                return Current(spec, stored, stored.start_ms, stored.reset_ms)
            return Current(spec, None, start, end)


def check_buckets(buckets: Sequence[Bucket]) -> None:
    names = [b.name for b in buckets]
    if len(set(names)) != len(names):
        raise ValueError(f"bucket names must be unique, got {names}")


def warn(name: str, state: BucketState) -> list[UsageWarning]:
    """Mark and return each threshold the bucket has newly crossed this window."""
    if state.limit == 0:
        return []
    crossed = []
    for percent in WARN_THRESHOLDS:
        if percent not in state.warned and state.used * 100 >= state.limit * percent:
            state.warned.add(percent)
            crossed.append(UsageWarning(name, percent))
    return crossed


def rebuilt_buckets(
    buckets: Sequence[Bucket], ledger: Iterable[LedgerEntry], now_ms: int
) -> dict[str, BucketState]:
    """The bucket states a rebuild writes: each live window's usage, from the ledger.

    Thresholds already crossed are marked as announced, so they aren't announced again.
    A bucket with nothing in its live window gets no state (its window is closed or
    empty, which looks the same).
    """
    check_buckets(buckets)
    entries = list(ledger)
    states: dict[str, BucketState] = {}
    for spec in buckets:
        match spec.window:
            case Fixed(start_ms=start, end_ms=end):
                window = (start, end)
            case Rolling(duration_ms=duration):
                starts = [
                    s
                    for e in entries
                    if (s := e.windows.get(spec.name)) is not None and now_ms < s + duration
                ]
                if not starts:
                    continue
                window = (max(starts), max(starts) + duration)
        in_window = [e.credits for e in entries if e.windows.get(spec.name) == window[0]]
        if not in_window:
            continue
        state = BucketState(*window, limit=spec.limit, used=sum(in_window))
        warn(spec.name, state)
        states[spec.name] = state
    return states


def ledger_total(entries: Iterable[LedgerEntry], bucket: str, start_ms: int) -> int:
    """Credits the ledger attributes to one window of one bucket."""
    return sum(e.credits for e in entries if e.windows.get(bucket) == start_ms)


def dump_state(
    *,
    epoch: int,
    buckets: Mapping[str, BucketState],
    reservations: Mapping[str, tuple[Granted, int]],
    done: Mapping[str, tuple[Outcome, int]],
    now_ms: int,
) -> dict[str, Any]:
    """A user's stored state as plain, canonical data, for comparing engines.

    `reservations` map to (grant, expires_ms) and `done` to (outcome, expires_ms). Done
    markers past their expiry are left out: the engines may differ in when they delete
    them, but never in whether they still count.
    """
    return {
        "epoch": epoch,
        "buckets": {
            name: {
                "start_ms": s.start_ms,
                "reset_ms": s.reset_ms,
                "limit": s.limit,
                "used": s.used,
                "reserved": s.reserved,
                "warned": sorted(s.warned),
            }
            for name, s in sorted(buckets.items())
        },
        "reservations": {
            rid: {
                "expires_ms": expires,
                "credits": grant.credits,
                "units": grant.units,
                "windows": dict(grant.windows),
            }
            for rid, (grant, expires) in sorted(reservations.items())
        },
        "done": {
            rid: {"outcome": str(outcome), "expires_ms": expires}
            for rid, (outcome, expires) in sorted(done.items())
            if now_ms < expires
        },
    }
