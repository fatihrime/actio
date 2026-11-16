"""The turns this process is writing, each in a task of its own.

A turn doesn't live inside the request that started it. The request starts it here and
returns; the turn goes on whether or not anyone is reading, and tells its readers what
it writes through `actio.chat.events`. So closing a page, or losing the connection,
ends nothing.

Two things end a turn before its model has finished, and both go through its `Halt`
(`actio.chat.answering`):

- `stop`: whoever asked doesn't want the rest;
- the process is told to stop (`stopping`). Turns get `GRACE` to finish. What is still
  running after it is halted, and recorded as `interrupted` with what it had written.
  That happens while the process still serves the requests it has: whoever is reading
  along with such a turn is told how it ended.

A turn can only be stopped by the process that runs it. Today one process serves the
API, and it runs them all.
"""

import asyncio
import logging
import uuid
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from actio.chat.answering import Halt

logger = logging.getLogger(__name__)

GRACE = timedelta(seconds=15)
"""How long the turns that are running get to finish, from the moment the process is
told to stop. Long enough for most answers that were nearly done, and short enough
that stopping the stack doesn't feel stuck."""

LAST_WORDS = timedelta(seconds=5)
"""How long a halted turn gets to record what it had written. It takes milliseconds;
this only keeps a database that is gone from holding up a shutdown for ever."""


@dataclass(frozen=True, slots=True)
class _Running:
    task: asyncio.Task[None]
    halt: Halt


class TurnRunner:
    """Runs turns apart from their requests, and ends them when asked."""

    def __init__(self) -> None:
        self._turns: dict[uuid.UUID, _Running] = {}
        self._halt_at: float | None = None
        """When the turns that are still running are halted, on the event loop's clock.
        Set once the process was told to stop."""
        self._timer: asyncio.TimerHandle | None = None
        self._halted = False

    def start(self, turn_id: uuid.UUID, work: Callable[[Halt], Coroutine[Any, Any, None]]) -> None:
        """Run a turn: `work`, given the `Halt` that ends it early.

        Returns at once. `work` must record the turn however it ends; what it raises
        is logged, and goes nowhere else.
        """
        halt = Halt()
        if self._halted:
            halt("interrupted")  # too late to start anything: it ends as soon as it begins
        task = asyncio.create_task(self._run(turn_id, work(halt)), name=f"turn-{turn_id}")
        self._turns[turn_id] = _Running(task, halt)

    def runs(self, turn_id: uuid.UUID) -> bool:
        """Whether this process is writing the turn."""
        return turn_id in self._turns

    async def stop(self, turn_id: uuid.UUID) -> bool:
        """Stop a turn, and wait until it is recorded.

        Returns whether there was something to stop: `False` for a turn this process
        isn't writing, because it has ended, or never ran here.
        """
        running = self._turns.get(turn_id)
        if running is None:
            return False
        running.halt("stopped")
        # `wait` leaves the turn alone if whoever asked for the stop goes away.
        await asyncio.wait({running.task})
        return True

    def stopping(self, grace: timedelta | None = None) -> None:
        """The process was told to stop: the turns that are running have `grace` from
        now (`GRACE`, when left out), and are halted then. Saying it again changes
        nothing: the time runs from the first."""
        if self._halt_at is not None:
            return
        wait_s = (GRACE if grace is None else grace).total_seconds()
        loop = asyncio.get_running_loop()
        self._halt_at = loop.time() + wait_s
        self._timer = loop.call_later(wait_s, self._halt_the_rest)

    async def drain(self, grace: timedelta | None = None) -> None:
        """Before the process exits: wait for the running turns until their time is up,
        halt the rest, and wait until each is recorded.

        Their time began when the process was told to stop (`stopping`). If nobody
        said so, it begins now."""
        self.stopping(grace)
        assert self._halt_at is not None  # noqa: S101 - `stopping` has just set it
        loop = asyncio.get_running_loop()
        tasks = {running.task for running in self._turns.values()}
        if tasks and (left := self._halt_at - loop.time()) > 0:
            await asyncio.wait(tasks, timeout=left)
        self._halt_the_rest()
        pending = {task for task in tasks if not task.done()}
        if pending:
            _, stuck = await asyncio.wait(pending, timeout=LAST_WORDS.total_seconds())
            for task in stuck:  # they stay `running`, and are given up on later
                task.cancel()
            await asyncio.gather(*stuck, return_exceptions=True)

    def _halt_the_rest(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
        if self._halted:
            return
        self._halted = True
        if self._turns:
            logger.warning(
                "stopping: halting %d answer(s) that were still being written", len(self._turns)
            )
        for running in self._turns.values():
            running.halt("interrupted")

    async def _run(self, turn_id: uuid.UUID, work: Coroutine[Any, Any, None]) -> None:
        try:
            await work
        except Exception:
            logger.exception("turn %s: it ended with an error", turn_id)
        finally:
            self._turns.pop(turn_id, None)
