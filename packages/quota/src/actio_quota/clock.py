"""Clocks. The engine never reads the time itself; callers pass `now_ms` from one of these."""

import time
from typing import Protocol


class Clock(Protocol):
    def now_ms(self) -> int:
        """Milliseconds since the Unix epoch."""
        ...


class SystemClock:
    """The real wall clock."""

    def now_ms(self) -> int:
        return time.time_ns() // 1_000_000


class ManualClock:
    """A clock that only moves when told to, for tests."""

    def __init__(self, start_ms: int) -> None:
        self._now_ms = start_ms

    def now_ms(self) -> int:
        return self._now_ms

    def advance(self, ms: int) -> None:
        if ms < 0:
            raise ValueError("a clock can't go backwards")
        self._now_ms += ms
