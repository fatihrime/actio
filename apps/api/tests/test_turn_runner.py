"""The runner by itself: tasks that go on without whoever started them, a stop that
waits until the turn is over, and a shutdown that gives turns a moment.

The turns here are a few lines each. A real one is `actio.api.turns._write`, tested
through the API in `test_turns_running_api.py`.
"""

import asyncio
import logging
from collections.abc import Callable, Coroutine
from datetime import timedelta
from typing import Any

import pytest

from actio.chat import running
from actio.chat.answering import Halt
from actio.chat.running import TurnRunner
from actio.core.ids import new_id

pytestmark = pytest.mark.anyio

PATIENCE_S = 5.0


class Work:
    """A turn that waits for `release`, the way a turn waits for its model, and then
    records how it ended. `recording` holds the recording back, for tests that look at
    the moment between."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.recording = asyncio.Event()
        self.recording.set()
        self.ended: list[str] = []

    def __call__(self, halt: Halt) -> Coroutine[Any, Any, None]:
        return self._run(halt)

    async def _run(self, halt: Halt) -> None:
        self.started.set()
        with halt.scope:
            await self.release.wait()
        await self.recording.wait()
        self.ended.append(halt.reason or "completed")


async def soon(condition: Callable[[], bool]) -> None:
    """Wait until `condition` holds, which takes a moment and not longer."""
    async with asyncio.timeout(PATIENCE_S):
        while True:
            if condition():
                return
            await asyncio.sleep(0.005)


async def test_a_turn_goes_on_after_whoever_started_it_has_returned() -> None:
    runner, work, turn = TurnRunner(), Work(), new_id()

    runner.start(turn, work)
    running_at_once = runner.runs(turn)
    await work.started.wait()
    work.release.set()
    await soon(lambda: not runner.runs(turn))

    assert running_at_once
    assert work.ended == ["completed"]


async def test_stopping_a_turn_halts_it_and_waits_until_it_is_recorded() -> None:
    runner, work, turn = TurnRunner(), Work(), new_id()
    runner.start(turn, work)
    await work.started.wait()
    work.recording.clear()

    stopping = asyncio.create_task(runner.stop(turn))
    await asyncio.sleep(0.05)
    still_waiting = not stopping.done()  # halted, but not recorded yet
    work.recording.set()

    assert still_waiting
    assert await stopping is True
    assert work.ended == ["stopped"]
    assert not runner.runs(turn)


async def test_there_is_nothing_to_stop_in_a_turn_that_is_not_running_here() -> None:
    runner, work, turn = TurnRunner(), Work(), new_id()
    runner.start(turn, work)
    work.release.set()
    await soon(lambda: not runner.runs(turn))

    assert await runner.stop(turn) is False  # it has ended
    assert await runner.stop(new_id()) is False  # it never ran here
    assert work.ended == ["completed"]


async def test_a_turn_is_stopped_once_however_often_it_is_asked() -> None:
    runner, work, turn = TurnRunner(), Work(), new_id()
    runner.start(turn, work)
    await work.started.wait()

    answers = await asyncio.gather(runner.stop(turn), runner.stop(turn), runner.stop(turn))

    assert list(answers) == [True, True, True]
    assert work.ended == ["stopped"]


async def test_a_stop_that_is_given_up_on_still_stops_the_turn() -> None:
    """Whoever asked for the stop closes the connection while it is being recorded. The
    turn is recorded all the same."""
    runner, work, turn = TurnRunner(), Work(), new_id()
    runner.start(turn, work)
    await work.started.wait()
    work.recording.clear()
    stopping = asyncio.create_task(runner.stop(turn))
    await asyncio.sleep(0.05)

    stopping.cancel()
    await asyncio.gather(stopping, return_exceptions=True)
    work.recording.set()
    await soon(lambda: not runner.runs(turn))

    assert work.ended == ["stopped"]


async def test_a_shutdown_lets_a_turn_finish_that_does_so_in_time() -> None:
    runner, work, turn = TurnRunner(), Work(), new_id()
    runner.start(turn, work)
    await work.started.wait()

    draining = asyncio.create_task(runner.drain(timedelta(seconds=PATIENCE_S)))
    await asyncio.sleep(0.05)
    work.release.set()
    await draining

    assert work.ended == ["completed"]


async def test_a_shutdown_halts_what_is_still_running_after_its_grace() -> None:
    runner, quick, slow = TurnRunner(), Work(), Work()
    runner.start(new_id(), quick)
    runner.start(new_id(), slow)
    await slow.started.wait()
    quick.release.set()

    await runner.drain(timedelta(milliseconds=50))

    assert quick.ended == ["completed"]
    assert slow.ended == ["interrupted"]


async def test_told_to_stop_the_process_halts_what_is_still_running_when_its_time_is_up() -> None:
    """Not at the shutdown, which comes later: the web server first waits for the
    requests it has, and the readers of these turns are among them."""
    runner, quick, slow = TurnRunner(), Work(), Work()
    runner.start(new_id(), quick)
    runner.start(new_id(), slow)
    await slow.started.wait()

    runner.stopping(timedelta(milliseconds=100))
    await asyncio.sleep(0.03)
    during = list(slow.ended)
    quick.release.set()  # this one finishes in time
    await soon(lambda: bool(slow.ended))

    assert during == []
    assert quick.ended == ["completed"]
    assert slow.ended == ["interrupted"]


async def test_the_time_runs_from_when_the_process_was_told_not_from_its_shutdown() -> None:
    runner, work = TurnRunner(), Work()
    runner.start(new_id(), work)
    await work.started.wait()
    loop = asyncio.get_running_loop()
    told = loop.time()

    runner.stopping(timedelta(milliseconds=150))
    runner.stopping(timedelta(seconds=30))  # said again: the first time counts
    await asyncio.sleep(0.05)
    await runner.drain(timedelta(seconds=30))  # the shutdown, with a patience it no longer has

    assert work.ended == ["interrupted"]
    assert 0.1 < loop.time() - told < 2


async def test_a_turn_started_after_the_process_was_told_to_stop_has_what_is_left_of_the_time() -> (
    None
):
    """A request that was already on its way may still ask."""
    runner, late = TurnRunner(), Work()
    runner.stopping(timedelta(milliseconds=100))

    runner.start(new_id(), late)
    await late.started.wait()
    still_running = not late.ended
    await soon(lambda: bool(late.ended))

    assert still_running
    assert late.ended == ["interrupted"]


async def test_a_shutdown_with_nothing_running_is_over_at_once() -> None:
    async with asyncio.timeout(1):
        await TurnRunner().drain(timedelta(seconds=30))


async def test_a_turn_started_during_a_shutdown_ends_as_soon_as_it_begins() -> None:
    runner, work = TurnRunner(), Work()
    await runner.drain(timedelta(0))

    turn = new_id()
    runner.start(turn, work)
    await soon(lambda: not runner.runs(turn))

    assert work.ended == ["interrupted"]


async def test_a_turn_that_was_stopped_is_not_called_interrupted_by_a_shutdown() -> None:
    runner, work, turn = TurnRunner(), Work(), new_id()
    runner.start(turn, work)
    await work.started.wait()
    work.recording.clear()
    stopping = asyncio.create_task(runner.stop(turn))
    await asyncio.sleep(0.05)

    draining = asyncio.create_task(runner.drain(timedelta(0)))
    await asyncio.sleep(0.05)
    work.recording.set()
    await asyncio.gather(stopping, draining)

    assert work.ended == ["stopped"]


async def test_a_turn_that_fails_is_logged_and_takes_nothing_else_down(
    caplog: pytest.LogCaptureFixture,
) -> None:
    runner, other, broken = TurnRunner(), Work(), new_id()

    async def fails(halt: Halt) -> None:
        assert halt.reason is None
        raise RuntimeError("something of Actio's own")

    runner.start(new_id(), other)
    with caplog.at_level(logging.ERROR, logger="actio.chat.running"):
        runner.start(broken, fails)
        await soon(lambda: not runner.runs(broken))
    other.release.set()
    await runner.drain(timedelta(seconds=PATIENCE_S))

    assert f"turn {broken}: it ended with an error" in caplog.text
    assert other.ended == ["completed"]


async def test_a_turn_that_cannot_be_recorded_does_not_hold_a_shutdown_up_for_ever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(running, "LAST_WORDS", timedelta(milliseconds=50))
    runner, work, turn = TurnRunner(), Work(), new_id()
    runner.start(turn, work)
    await work.started.wait()
    work.recording.clear()  # as with a database that doesn't answer

    async with asyncio.timeout(PATIENCE_S):
        await runner.drain(timedelta(0))

    assert work.ended == []  # it stays `running`, and is given up on later
    assert not runner.runs(turn)


# --- Halting by itself ----------------------------------------------------------------


async def test_a_halt_ends_the_wait_it_guards_and_nothing_after_it() -> None:
    halt = Halt()
    reached: list[str] = []

    async def turn() -> None:
        with halt.scope:
            await asyncio.Event().wait()
            reached.append("past the wait")
        await asyncio.sleep(0)  # what comes after is not cancelled with it
        reached.append("recorded")

    task = asyncio.create_task(turn())
    await asyncio.sleep(0.01)
    halt("stopped")
    halt("interrupted")  # too late to change the reason
    await task

    assert reached == ["recorded"]
    assert halt.reason == "stopped"


async def test_a_turn_halted_before_it_begins_never_waits() -> None:
    halt = Halt()
    halt("interrupted")

    async with asyncio.timeout(1):
        with halt.scope:
            await asyncio.Event().wait()

    assert halt.reason == "interrupted"


async def test_a_halt_that_comes_after_the_wait_changes_nothing() -> None:
    halt = Halt()
    with halt.scope:
        await asyncio.sleep(0)

    halt("stopped")
    await asyncio.sleep(0.01)  # nothing is cancelled: the turn was past it already

    assert halt.reason == "stopped"
