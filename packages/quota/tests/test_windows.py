"""Calendar resets: every instant belongs to exactly one window, across DST changes."""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import strategies as st

from actio_quota.windows import CalendarReset

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS


def ms(iso: str) -> int:
    """An ISO 8601 instant with an offset, in milliseconds since the epoch."""
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


def test_weekly_reset_in_istanbul() -> None:
    reset = CalendarReset.parse("MON 00:00 Europe/Istanbul")
    # Istanbul is UTC+3 all year, so Monday 00:00 there is Sunday 21:00 UTC.
    start, end = reset.window(ms("2026-11-11T12:00:00+03:00"))  # a Wednesday
    assert start == ms("2026-11-09T00:00:00+03:00")
    assert end == ms("2026-11-16T00:00:00+03:00")
    assert end - start == 7 * DAY_MS


def test_the_reset_instant_starts_the_next_window() -> None:
    reset = CalendarReset.parse("MON 00:00 Europe/Istanbul")
    boundary = ms("2026-11-16T00:00:00+03:00")
    assert reset.window(boundary - 1)[1] == boundary
    assert reset.window(boundary)[0] == boundary


def test_a_berlin_week_with_the_spring_dst_change_is_an_hour_short() -> None:
    reset = CalendarReset.parse("MON 00:00 Europe/Berlin")
    # Clocks went forward on Sunday 2026-03-29, from +01:00 to +02:00.
    start, end = reset.window(ms("2026-03-27T12:00:00+01:00"))
    assert start == ms("2026-03-23T00:00:00+01:00")
    assert end == ms("2026-03-30T00:00:00+02:00")
    assert end - start == 7 * DAY_MS - HOUR_MS


def test_a_berlin_week_with_the_autumn_dst_change_is_an_hour_long() -> None:
    reset = CalendarReset.parse("MON 00:00 Europe/Berlin")
    start, end = reset.window(ms("2026-10-25T12:00:00+01:00"))
    assert start == ms("2026-10-19T00:00:00+02:00")
    assert end == ms("2026-10-26T00:00:00+01:00")
    assert end - start == 7 * DAY_MS + HOUR_MS


def test_daily_reset() -> None:
    reset = CalendarReset.parse("04:00 UTC")
    assert reset.window(ms("2026-11-09T03:59:00+00:00")) == (
        ms("2026-11-08T04:00:00+00:00"),
        ms("2026-11-09T04:00:00+00:00"),
    )


@pytest.mark.parametrize(
    ("spec", "error"),
    [
        ("FUNDAY 00:00 UTC", "unknown day"),
        ("00:00", "expected"),
        ("MON 25:00 UTC", "hour"),
        ("MON 00:00 Mars/Olympus_Mons", "Mars"),
    ],
)
def test_rejects_bad_specs(spec: str, error: str) -> None:
    with pytest.raises((ValueError, KeyError), match=error):
        CalendarReset.parse(spec)


def test_rejects_a_reset_time_with_fold_1() -> None:
    # fold=1 would pick an ambiguous time's second reading, and for a skipped time it
    # moves the reset back to the previous day. Hypothesis found this in Santiago.
    with pytest.raises(ValueError, match="fold"):
        CalendarReset(at=time(0, 0, fold=1), tz=ZoneInfo("America/Santiago"))


ZONES = [
    "UTC",
    "Europe/Istanbul",
    "Europe/Berlin",
    "America/New_York",
    "America/Santiago",  # its DST changes happen at midnight, so some midnights don't exist
    "Australia/Lord_Howe",  # a 30-minute DST change
]

resets = st.builds(
    CalendarReset,
    at=st.times().map(lambda t: t.replace(second=0, microsecond=0, fold=0)),
    tz=st.sampled_from(ZONES).map(ZoneInfo),
    weekday=st.none() | st.integers(0, 6),
)
instants = st.integers(ms("2020-01-01T00:00:00+00:00"), ms("2036-01-01T00:00:00+00:00"))


@given(resets, instants)
def test_every_instant_is_in_its_window(reset: CalendarReset, now_ms: int) -> None:
    start, end = reset.window(now_ms)
    assert start <= now_ms < end
    period = DAY_MS if reset.weekday is None else 7 * DAY_MS
    assert period - HOUR_MS <= end - start <= period + HOUR_MS


@given(resets, instants)
def test_windows_tile_time_without_gaps_or_overlaps(reset: CalendarReset, now_ms: int) -> None:
    start, end = reset.window(now_ms)
    assert reset.window(start) == (start, end)
    assert reset.window(end)[0] == end
    assert reset.window(start - 1)[1] == start


@given(resets, instants)
def test_windows_start_at_the_local_reset_time(reset: CalendarReset, now_ms: int) -> None:
    start, _ = reset.window(now_ms)
    local = datetime.fromtimestamp(start / 1000, UTC).astimezone(reset.tz)
    wall = datetime.combine(local.date(), reset.at)
    exists = (
        wall.replace(tzinfo=reset.tz).astimezone(UTC).astimezone(reset.tz).replace(tzinfo=None)
        == wall
    )
    if exists:  # a skipped wall-clock time starts the window up to an hour off it
        assert local.time() == reset.at
        if reset.weekday is not None:
            assert local.weekday() == reset.weekday
    else:
        assert abs(local.replace(tzinfo=None) - wall) <= timedelta(hours=1)
