"""Clocks: the engine never reads the time itself, so these are all it depends on."""

import time

import pytest

from actio_quota import Clock, ManualClock, SystemClock


def test_the_system_clock_reads_wall_time_in_ms() -> None:
    clock: Clock = SystemClock()
    assert abs(clock.now_ms() - time.time() * 1000) < 1_000


def test_a_manual_clock_moves_only_forward_and_only_when_told() -> None:
    clock = ManualClock(1_000)
    clock.advance(500)
    assert clock.now_ms() == 1_500
    with pytest.raises(ValueError, match="backwards"):
        clock.advance(-1)
