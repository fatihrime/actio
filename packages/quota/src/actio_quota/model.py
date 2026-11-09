"""The engine's inputs and outputs, shared by the reference model and the Valkey engine.

Everything is an integer: credits, micro-credits and milliseconds. Times are passed in
explicitly (`now_ms`), never read from a clock inside the engine, so tests can replay
any sequence of events exactly.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from actio_quota.cost import MAX_CREDITS, MAX_SAFE, MICRO, Rates, ceil_div, check_count

WARN_THRESHOLDS = (50, 75, 90)
"""Percentages of a bucket's limit at which the user is warned, once per window."""

DONE_TTL_MS = 48 * 3_600_000
"""How long a finished reservation is remembered, so retries stay idempotent."""

GC_BATCH = 32
"""How many expired reservations one RESERVE call releases at most."""

_NAME = re.compile(r"[a-z0-9][a-z0-9_:.-]{0,63}")


def check_name(kind: str, value: str) -> None:
    if not _NAME.fullmatch(value):
        raise ValueError(f"{kind} {value!r} must match {_NAME.pattern}")


@dataclass(frozen=True, slots=True)
class Rolling:
    """A window that opens on the first admitted request and lasts `duration_ms`."""

    duration_ms: int

    def __post_init__(self) -> None:
        check_count("duration_ms", self.duration_ms)
        if self.duration_ms == 0:
            raise ValueError("duration_ms must be positive")


@dataclass(frozen=True, slots=True)
class Fixed:
    """A calendar window, `[start_ms, end_ms)`, worked out by the caller."""

    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        check_count("start_ms", self.start_ms)
        check_count("end_ms", self.end_ms)
        if self.end_ms <= self.start_ms:
            raise ValueError("a window must end after it starts")


@dataclass(frozen=True, slots=True)
class Bucket:
    """One limit that a request must fit in, such as `session` or `week`."""

    name: str
    limit: int
    """Credits allowed per window."""
    window: Rolling | Fixed

    def __post_init__(self) -> None:
        check_name("bucket name", self.name)
        check_count("limit", self.limit, MAX_CREDITS)


@dataclass(frozen=True, slots=True)
class Estimate:
    """What a request may cost: a fixed part plus a per-unit part.

    For a model call the units are output tokens: `base_micro` prices the prompt, and
    `unit_micro` prices each output token. The engine grants the most units it can,
    between `min_units` and `max_units`, so answers get shorter near a limit instead of
    being refused. A request that can't afford `min_units` is denied.
    """

    base_micro: int
    unit_micro: int = 0
    min_units: int = 0
    max_units: int = 0

    def __post_init__(self) -> None:
        for name in ("base_micro", "unit_micro", "min_units", "max_units"):
            check_count(name, getattr(self, name))
        if self.min_units > self.max_units:
            raise ValueError("min_units must not exceed max_units")
        if self.base_micro + self.max_units * self.unit_micro > MAX_SAFE:
            raise ValueError("the largest possible cost is too big to count exactly in Lua")

    @classmethod
    def flat(cls, credits: int) -> "Estimate":
        """A known, fixed cost, such as a video of a given length."""
        check_count("credits", credits, MAX_CREDITS)
        return cls(base_micro=credits * MICRO)

    @classmethod
    def tokens(
        cls, rates: Rates, *, input_tokens: int, max_output: int, min_output: int = 1
    ) -> "Estimate":
        """A model call: the estimated prompt plus up to `max_output` output tokens.

        Pass `min_output=512` (the tool-step floor) when the step may emit tool calls:
        a smaller limit could cut a tool call's JSON off mid-argument, so such a step is
        denied rather than shrunk.
        """
        return cls(
            base_micro=input_tokens * rates.input,
            unit_micro=rates.output,
            min_units=min(min_output, max_output),
            max_units=max_output,
        )

    def credits(self, units: int) -> int:
        """The credits to reserve for `units`, rounded up."""
        return ceil_div(self.base_micro + units * self.unit_micro, MICRO)


@dataclass(frozen=True, slots=True)
class Granted:
    """An admitted request: `credits` are held until it is settled or released."""

    rid: str
    credits: int
    units: int
    """How many units (output tokens) the request may use: pass it on as `max_tokens`."""
    windows: Mapping[str, int] = field(hash=False)
    """Each bucket's window start, so usage counts toward the window that admitted it."""


@dataclass(frozen=True, slots=True)
class Denied:
    """A refused request. Nothing was reserved and no window was opened."""

    rid: str
    buckets: tuple[str, ...]
    """The buckets that can't fit even the smallest allowed request."""
    retry_at_ms: int | None
    """When the last of those buckets resets. `None` if the request can never fit."""


@dataclass(frozen=True, slots=True)
class NeedsRebuild:
    """The user's counters are missing (a new user, or Valkey lost its data).

    Rebuild them from the ledger with `rebuild(..., if_epoch=None)`, then retry.
    """


type ReserveResult = Granted | Denied | NeedsRebuild


class Outcome(StrEnum):
    """How a reservation ended."""

    SETTLED = "settled"
    RELEASED = "released"
    EXPIRED = "expired"
    """Its holder stopped extending it, so the engine released it."""
    UNKNOWN = "unknown"
    """Never reserved, forgotten after `DONE_TTL_MS`, or lost with Valkey's data."""


@dataclass(frozen=True, slots=True)
class UsageWarning:
    """A bucket crossed one of `WARN_THRESHOLDS` for the first time this window."""

    bucket: str
    percent: int


@dataclass(frozen=True, slots=True)
class Finished:
    """The result of settling or releasing a reservation."""

    outcome: Outcome
    applied: bool
    """False when the reservation had already ended: the call changed nothing."""
    warnings: tuple[UsageWarning, ...] = ()


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """A settled request, as the Postgres ledger records it. Rebuilds replay these."""

    rid: str
    credits: int
    windows: Mapping[str, int] = field(hash=False)


@dataclass(frozen=True, slots=True)
class BucketView:
    """A bucket as the user sees it now. A closed window shows zeros and no times."""

    name: str
    limit: int
    used: int
    reserved: int
    start_ms: int | None
    reset_ms: int | None

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used - self.reserved)


def check_rid(rid: str) -> None:
    """Reservation ids are short opaque tokens, such as a UUID."""
    check_name("reservation id", rid)
