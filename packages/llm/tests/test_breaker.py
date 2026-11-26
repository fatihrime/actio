"""A model's circuit breaker: when it opens, for how long, what tries the model once its
wait is over, and what closes it again (ADR 0031).

The breaker reads no clock: each test gives it the moments, and moves them by hand. The
examples hold each rule, and each constant by its number. The property test then plays
made-up sequences of calls that begin and end in any order, and of moments, against a
breaker written the plain way here, and holds what always holds.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from actio_llm import Breaker, Call, Change, Failing
from actio_llm.breaker import (
    AT_LEAST,
    LAST_CALLS,
    WAITS,
    WITHIN,
    Closed,
    Opened,
    OpenedAgain,
    Outcome,
)

NOON = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


def at(seconds: float) -> datetime:
    """`seconds` after noon."""
    return NOON + timedelta(seconds=seconds)


def ended(breaker: Breaker, outcome: Outcome, now: datetime) -> Change | None:
    """A call that begins and ends at `now`, as `outcome`: what that changed."""
    return breaker.begins(now).ends(outcome, now)


def failed_times(breaker: Breaker, count: int, now: datetime) -> list[Change | None]:
    """`count` calls that each fail at `now`: what each changed."""
    return [ended(breaker, "failed", now) for _ in range(count)]


def opened_at(now: datetime) -> Breaker:
    """A breaker that five failures at `now` opened."""
    breaker = Breaker()
    *_, opened = failed_times(breaker, 5, now)
    assert opened == Opened(5, 5, timedelta(seconds=30))
    return breaker


# --- The numbers ----------------------------------------------------------------------


def test_the_numbers_are_twenty_calls_of_half_an_hour_five_at_the_least_and_the_waits() -> None:
    assert [LAST_CALLS, WITHIN.total_seconds(), AT_LEAST] == [20, 30 * 60, 5]
    assert [wait.total_seconds() for wait in WAITS] == [30, 60, 120, 240, 300]


# --- R1 and R2: closed, and when it opens ---------------------------------------------


def test_a_new_breaker_is_closed_and_tries_nothing() -> None:
    breaker = Breaker()

    assert breaker.failing(NOON) is None
    assert breaker.trying == 0


def test_four_failures_open_nothing_and_the_fifth_opens_it_for_thirty_seconds() -> None:
    breaker = Breaker()

    assert failed_times(breaker, 4, NOON) == [None] * 4
    assert breaker.failing(NOON) is None
    assert ended(breaker, "failed", at(1)) == Opened(5, 5, timedelta(seconds=30))
    assert breaker.failing(at(1)) == Failing(timedelta(seconds=30))
    assert breaker.failing(at(11)) == Failing(timedelta(seconds=20))


def test_half_of_the_calls_failed_opens_it_and_less_than_half_never_does() -> None:
    """Three answers and then three failures: at the fifth call, 2 of 5 failed, and at
    the sixth 3 of 6, which is half."""
    half = Breaker()
    for outcome in ("answered", "answered", "answered", "failed", "failed"):
        assert ended(half, outcome, NOON) is None

    assert ended(half, "failed", NOON) == Opened(3, 6, timedelta(seconds=30))

    # A third of them, for as long as it goes on: never.
    third = Breaker()
    for _ in range(20):
        changes = [ended(third, outcome, NOON) for outcome in ("answered", "answered", "failed")]
        assert changes == [None, None, None]
    assert third.failing(NOON) is None


def test_an_answer_never_opens_it_whatever_the_share() -> None:
    """Four failures and an answer: 4 of 5 failed, and the answer opens nothing. The
    next failure does, by 5 of 6."""
    breaker = Breaker()
    failed_times(breaker, 4, NOON)

    assert ended(breaker, "answered", NOON) is None
    assert breaker.failing(NOON) is None
    assert ended(breaker, "failed", NOON) == Opened(5, 6, timedelta(seconds=30))


def test_it_keeps_the_last_twenty_calls() -> None:
    """After twenty answers, each failure pushes an answer out: the tenth makes 10 of
    the last 20."""
    breaker = Breaker()
    for _ in range(20):
        ended(breaker, "answered", NOON)

    assert failed_times(breaker, 9, NOON) == [None] * 9
    assert ended(breaker, "failed", NOON) == Opened(10, 20, timedelta(seconds=30))


def test_a_call_that_ended_thirty_minutes_ago_counts_and_one_a_second_longer_ago_doesnt() -> None:
    within = Breaker()
    failed_times(within, 4, NOON)
    beyond = Breaker()
    failed_times(beyond, 4, NOON)

    assert ended(within, "failed", at(30 * 60)) == Opened(5, 5, timedelta(seconds=30))
    assert ended(beyond, "failed", at(30 * 60 + 1)) is None
    # The four are gone: four more are needed, and then it is 5 of 5 again.
    assert failed_times(beyond, 3, at(30 * 60 + 1)) == [None] * 3
    assert ended(beyond, "failed", at(30 * 60 + 1)) == Opened(5, 5, timedelta(seconds=30))


def test_an_opening_forgets_the_calls_it_kept() -> None:
    """Closed again by an answer, it starts from that answer alone: four failures make
    4 of 5, and the wait is the first one again."""
    breaker = opened_at(NOON)
    assert ended(breaker, "answered", at(5)) == Closed()

    assert failed_times(breaker, 3, at(6)) == [None] * 3
    assert ended(breaker, "failed", at(6)) == Opened(4, 5, timedelta(seconds=30))


# --- R3: open, before its wait is over ------------------------------------------------


def test_before_its_wait_is_over_a_failure_changes_nothing() -> None:
    """The wait neither grows nor starts again, and no call tries it."""
    breaker = opened_at(NOON)

    assert failed_times(breaker, 3, at(20)) == [None] * 3
    assert breaker.failing(at(20)) == Failing(timedelta(seconds=10))
    assert breaker.trying == 0
    assert breaker.failing(at(30)) is None


def test_before_its_wait_is_over_an_answer_closes_it() -> None:
    """As a last resort, or as a chain's only model, it may still be asked."""
    breaker = opened_at(NOON)
    call = breaker.begins(at(10))

    assert call.ends("answered", at(12)) == Closed()
    assert breaker.failing(at(12)) is None


def test_before_its_wait_is_over_a_call_with_nothing_to_say_changes_nothing() -> None:
    breaker = opened_at(NOON)

    assert ended(breaker, None, at(10)) is None
    assert breaker.failing(at(10)) == Failing(timedelta(seconds=20))


# --- R4: open, once its wait is over --------------------------------------------------


def test_once_its_wait_is_over_it_is_asked_in_its_place_one_request_at_a_time() -> None:
    """While the call that tries it is under way, it is failing for everyone else, with
    no wait to name. A call that ends with nothing to say, as a turn stopped before its
    first word, lets the next request try it."""
    breaker = opened_at(NOON)
    assert breaker.failing(at(30)) is None

    trying = breaker.begins(at(30))

    assert breaker.trying == 1
    assert breaker.failing(at(31)) == Failing(None)
    assert trying.ends(None, at(32)) is None
    assert breaker.trying == 0
    assert breaker.failing(at(32)) is None


@pytest.mark.parametrize("began", [30, 45], ids=["at the wait's end", "after it"])
def test_a_call_that_tries_it_and_fails_opens_it_again_for_a_minute(began: float) -> None:
    breaker = opened_at(NOON)
    trying = breaker.begins(at(began))

    assert trying.ends("failed", at(began + 1)) == OpenedAgain(timedelta(minutes=1))
    assert breaker.failing(at(began + 1)) == Failing(timedelta(minutes=1))
    assert breaker.trying == 0


def test_a_call_that_tries_it_and_answers_closes_it() -> None:
    breaker = opened_at(NOON)
    trying = breaker.begins(at(31))

    assert trying.ends("answered", at(33)) == Closed()
    assert breaker.failing(at(33)) is None
    assert breaker.trying == 0


def test_each_time_it_fails_again_it_waits_the_next_wait_and_then_five_minutes_for_ever() -> None:
    breaker = opened_at(NOON)
    now = at(30)
    waits = []
    for _ in range(6):
        change = breaker.begins(now).ends("failed", now)
        assert isinstance(change, OpenedAgain)
        waits.append(change.seconds)
        assert breaker.failing(now) == Failing(change.wait)
        now += change.wait

    assert waits == [60, 120, 240, 300, 300, 300]
    # An answer closes it, and the next opening waits 30 seconds again.
    assert ended(breaker, "answered", now) == Closed()
    failed_times(breaker, 4, now)
    assert breaker.failing(now) == Failing(timedelta(seconds=30))


def test_a_last_resort_from_before_the_wait_was_over_that_fails_after_it_opens_it_again() -> None:
    """It is the first call to end failed at or after the wait's end."""
    breaker = opened_at(NOON)
    last_resort = breaker.begins(at(25))

    assert breaker.trying == 0
    assert breaker.failing(at(31)) is None  # it isn't a call that tries it
    assert last_resort.ends("failed", at(31)) == OpenedAgain(timedelta(minutes=1))


# --- R5: an answer closes it ----------------------------------------------------------


def test_an_answer_closes_it_from_any_open_state() -> None:
    before = opened_at(NOON)
    trying = opened_at(NOON)
    trying.begins(at(30))
    again = opened_at(NOON)
    again.begins(at(30)).ends("failed", at(30))

    for breaker in (before, trying, again):
        assert ended(breaker, "answered", at(31)) == Closed()
        assert breaker.failing(at(31)) is None
        assert breaker.trying == 0


# --- R6: calls end in any order -------------------------------------------------------


def test_two_calls_under_way_one_from_before_the_wait_was_over_end_in_either_order() -> None:
    # The one that tries it ends first, with nothing to say: the next request tries it,
    # and the last resort, failing after the wait, opens it again.
    first = opened_at(NOON)
    last_resort, trying = first.begins(at(20)), first.begins(at(30))
    assert first.trying == 1
    assert trying.ends(None, at(35)) is None
    assert first.failing(at(35)) is None
    assert last_resort.ends("failed", at(36)) == OpenedAgain(timedelta(minutes=1))
    assert first.trying == 0

    # The last resort ends first, and answers: closed. The one that tries it then fails,
    # into a closed breaker, which keeps it as one failure of two.
    second = opened_at(NOON)
    last_resort, trying = second.begins(at(20)), second.begins(at(30))
    assert last_resort.ends("answered", at(35)) == Closed()
    assert second.trying == 0
    assert trying.ends("failed", at(36)) is None
    assert second.trying == 0
    assert second.failing(at(36)) is None
    assert failed_times(second, 2, at(36)) == [None, None]
    assert ended(second, "failed", at(36)) == Opened(4, 5, timedelta(seconds=30))


def test_a_call_that_tried_it_and_ends_after_it_opened_again_leaves_no_one_trying() -> None:
    """Of two calls that each began once the wait was over, the first fails and opens it
    again: the second, ending later with nothing to say, holds nothing, and once the
    next wait is over the next request tries it."""
    breaker = opened_at(NOON)
    one, other = breaker.begins(at(30)), breaker.begins(at(30))
    assert breaker.trying == 2

    assert one.ends("failed", at(31)) == OpenedAgain(timedelta(minutes=1))
    assert breaker.trying == 0
    assert other.ends(None, at(40)) is None
    assert breaker.trying == 0
    assert breaker.failing(at(91)) is None


def test_a_call_ends_once() -> None:
    breaker = Breaker()
    call = breaker.begins(NOON)
    call.ends(None, NOON)

    with pytest.raises(RuntimeError, match="once"):
        call.ends("failed", NOON)


def test_a_wait_is_said_in_whole_seconds_rounded_up_and_one_at_the_least() -> None:
    assert Failing(timedelta(0)).seconds == 1
    assert Failing(timedelta(milliseconds=1)).seconds == 1
    assert Failing(timedelta(seconds=29, milliseconds=1)).seconds == 30
    assert Failing(timedelta(seconds=30)).seconds == 30
    assert Failing(None).seconds is None
    assert Opened(5, 5, timedelta(seconds=30)).seconds == 30
    assert OpenedAgain(timedelta(minutes=5)).seconds == 300


# --- What always holds ----------------------------------------------------------------


@dataclass
class Plain:
    """A breaker written the plain way, from the module's rules: what each call ending
    must change, and what the breaker must say at each moment."""

    kept: list[tuple[datetime, bool]] = field(default_factory=list)
    until: datetime | None = None
    wait: int = 0
    trying: set[int] = field(default_factory=set)
    """The ids of the calls under way that began once the wait was over."""

    def begins(self, number: int, now: datetime) -> None:
        if self.until is not None and now >= self.until:
            self.trying.add(number)

    def ends(self, number: int, outcome: Outcome, now: datetime) -> Change | None:
        self.trying.discard(number)
        if outcome is None:
            return None
        if self.until is None:
            self.kept = [
                (when, failure)
                for when, failure in [*self.kept, (now, outcome == "failed")][-20:]
                if now - when <= timedelta(minutes=30)
            ]
            failures = sum(failure for _, failure in self.kept)
            if outcome == "answered" or len(self.kept) < 5 or failures * 2 < len(self.kept):
                return None
            return Opened(failures, len(self.kept), self._open(now))
        if outcome == "answered":
            self.kept, self.until, self.wait, self.trying = [(now, False)], None, 0, set()
            return Closed()
        if now < self.until:
            return None
        self.wait = min(self.wait + 1, 4)
        return OpenedAgain(self._open(now))

    def _open(self, now: datetime) -> timedelta:
        wait = [30, 60, 120, 240, 300][self.wait]
        self.until = now + timedelta(seconds=wait)
        self.kept, self.trying = [], set()
        return timedelta(seconds=wait)

    def failing(self, now: datetime) -> Failing | None:
        if self.until is None:
            return None
        if now < self.until:
            return Failing(self.until - now)
        return Failing(None) if self.trying else None


OUTCOMES = st.sampled_from(["failed", "failed", "failed", "answered", None])
"""Failures most of all, so that a breaker opens, and is tried, in most sequences."""

STEPS = st.lists(
    st.one_of(
        # A call that begins and ends in the same moment, as most do by the clock here.
        st.tuples(st.just("call"), OUTCOMES),
        st.tuples(st.just("call"), OUTCOMES),
        st.tuples(st.just("begin")),
        st.tuples(st.just("end"), st.integers(0, 1_000), OUTCOMES),
        # Mostly a few seconds, often about a wait, and now and then past half an hour.
        st.tuples(
            st.just("later"),
            st.one_of(
                st.integers(0, 5_000),
                st.integers(25_000, 70_000),
                st.integers(0, 400_000),
                st.integers(0, 2_000_000),
            ),
        ),
    ),
    min_size=20,
    max_size=100,
)


@given(steps=STEPS)
def test_what_always_holds_whatever_the_calls_and_moments(steps: list[tuple[object, ...]]) -> None:
    """Calls begin and end in any order, as answered, failed or with nothing to say, at
    moments that move on by milliseconds or by most of an hour. At every step the breaker
    changes and says what the plain one does, and:

    - it never opens with fewer than five calls kept, or with fewer than half failed;
    - it never says that a model is failing while it is closed, nor tries it;
    - a wait is never longer than the longest of the waits;
    - an answer always closes it, open as it was;
    - a call that ended longer ago than half an hour never counts;
    - the calls that try it never go below nothing, nor are more than those under way;
    - once every call has ended, a breaker whose wait is over asks the model in its
      place: nothing is left holding it."""
    breaker, plain = Breaker(), Plain()
    now = NOON
    under_way: dict[int, Call] = {}
    ended_at: list[tuple[datetime, bool]] = []  # every call that ended while closed
    numbers = iter(range(10_000))

    def check() -> None:
        said = breaker.failing(now)
        assert said == plain.failing(now)
        assert 0 <= breaker.trying <= len(under_way)
        assert breaker.trying == len(plain.trying)
        if plain.until is None:
            assert said is None
            assert breaker.trying == 0
        if said is not None and said.wait is not None:
            assert timedelta(0) < said.wait <= max(WAITS)

    def end(number: int, outcome: Outcome) -> None:
        was_open = plain.until is not None
        change = under_way.pop(number).ends(outcome, now)
        assert change == plain.ends(number, outcome, now)
        if outcome == "answered" and was_open:
            assert change == Closed()
            assert breaker.failing(now) is None
        if not was_open and outcome is not None:
            ended_at.append((now, outcome == "failed"))
        if isinstance(change, Opened):
            recent = [failure for when, failure in ended_at[-20:] if now - when <= WITHIN]
            assert AT_LEAST <= change.calls <= len(recent)
            assert change.failed * 2 >= change.calls
            assert change.failed <= sum(recent)
        if change is not None:
            ended_at.clear()
            if isinstance(change, Closed):
                ended_at.append((now, False))
        if change is not None and not isinstance(change, Closed):
            assert change.wait in WAITS

    def begin() -> int:
        number = next(numbers)
        under_way[number] = breaker.begins(now)
        plain.begins(number, now)
        return number

    for step in steps:
        match step:
            case ("call", outcome):
                number = begin()
                check()
                end(number, outcome)  # type: ignore[arg-type]
            case ("begin",):
                begin()
            case ("end", which, outcome) if under_way:
                assert isinstance(which, int)
                number = list(under_way)[which % len(under_way)]
                end(number, outcome)  # type: ignore[arg-type]
            case ("later", milliseconds):
                assert isinstance(milliseconds, int)
                now += timedelta(milliseconds=milliseconds)
            case _:
                pass
        check()

    for number in list(under_way):
        end(number, None)
    check()
    assert breaker.trying == 0
    if plain.until is not None:
        assert breaker.failing(max(now, plain.until)) is None
