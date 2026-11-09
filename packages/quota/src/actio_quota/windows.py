"""Calendar reset windows, such as "every Monday at 00:00 in Europe/Istanbul".

Python works these out and passes them to the engine as plain millisecond timestamps,
because Lua in Valkey has no time zone data. A window is half-open: `[start_ms, end_ms)`.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MS = timedelta(milliseconds=1)
_DAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")


@dataclass(frozen=True, slots=True)
class CalendarReset:
    """A reset at a local wall-clock time, either daily or on one day of the week."""

    at: time
    tz: ZoneInfo
    weekday: int | None = None
    """0 is Monday, as in `datetime.weekday()`. `None` resets every day."""

    def __post_init__(self) -> None:
        if self.weekday is not None and not 0 <= self.weekday <= 6:
            raise ValueError(f"weekday must be 0 (Monday) to 6 (Sunday), got {self.weekday}")
        if self.at.tzinfo is not None:
            raise ValueError("`at` is a wall-clock time in `tz`; give it without a tzinfo")
        if self.at.fold:
            # Resets always use a wall-clock time's first reading (see `_boundary_utc`).
            raise ValueError("`at` must have fold=0")

    @classmethod
    def parse(cls, spec: str) -> "CalendarReset":
        """Parse "MON 00:00 Europe/Istanbul" (weekly) or "04:00 UTC" (daily)."""
        parts = spec.split()
        weekday = None
        if len(parts) == 3:
            day = parts.pop(0).upper()
            if day not in _DAYS:
                raise ValueError(f"unknown day {day!r}; use one of {', '.join(_DAYS)}")
            weekday = _DAYS.index(day)
        if len(parts) != 2:
            raise ValueError(f"expected '[DAY] HH:MM Area/City', got {spec!r}")
        clock, zone = parts
        return cls(at=time.fromisoformat(clock), tz=ZoneInfo(zone), weekday=weekday)

    @property
    def _period(self) -> timedelta:
        return timedelta(days=1 if self.weekday is None else 7)

    def _boundary_utc(self, local_day: datetime) -> datetime:
        """The UTC instant of the reset time on `local_day`'s date.

        A wall-clock time can be skipped (spring forward) or repeated (fall back) in
        `tz`. `fold=0` picks its first reading, which zoneinfo also uses for skipped
        times. Either way the result is a fixed, deterministic instant.
        """
        local = datetime.combine(local_day.date(), self.at, tzinfo=self.tz)
        return local.astimezone(UTC)

    def window(self, now_ms: int) -> tuple[int, int]:
        """The `[start_ms, end_ms)` window that contains `now_ms`."""
        now = _EPOCH + now_ms * _MS
        local_now = now.astimezone(self.tz)
        days_back = 0 if self.weekday is None else (local_now.weekday() - self.weekday) % 7
        start_day = local_now - timedelta(days=days_back)
        start = self._boundary_utc(start_day)
        # The boundary on the matching day may still be ahead (later today), so step
        # back one period. A loop, because a DST jump can shift the boundary by an hour.
        while start > now:
            start_day -= self._period
            start = self._boundary_utc(start_day)
        end = self._boundary_utc(start_day + self._period)
        while end <= now:
            start_day += self._period
            start, end = end, self._boundary_utc(start_day + self._period)
        return _to_ms(start), _to_ms(end)


def _to_ms(instant: datetime) -> int:
    return (instant - _EPOCH) // _MS
