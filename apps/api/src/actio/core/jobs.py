"""Background jobs: a queue in Postgres, and the worker that takes jobs from it.

A job is deferred in the transaction of the change it follows (`defer`), so it exists
if and only if that change was committed. A worker runs it later (`work`), one at a
time, and runs the jobs that have a schedule by their schedule.

The queue is Procrastinate's (ADR 0024), and this is the only module that knows it: a
job's handler is a plain async function, and nothing of Procrastinate leaves here. Its
tables are built by Procrastinate's own SQL, applied by a migration of ours
(`schema_files`).
"""

import asyncio
import importlib.resources
import logging
import signal
import threading
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

import procrastinate
import psycopg
from procrastinate import exceptions as queue_errors
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from actio.core import alive
from actio.core.signals import STOP_SIGNALS

logger = logging.getLogger(__name__)

type Argument = str | int | float | bool | list[Argument] | dict[str, Argument] | None
"""What a job may be deferred with: what JSON can hold. Procrastinate writes a job's
arguments in its log, so they are ids and such, never what people wrote."""

SHUTDOWN_GRACE_S = 15.0
"""How long a worker that is told to stop lets the job in hand finish. It takes no new
job meanwhile. After this the job is cut off: it stays in the queue as `aborted`, and
nothing runs it again.

Whatever stops the process has to wait longer than this, or it cuts the job off
itself, without a word in the queue."""

STARTS_AGAIN_AFTER_S = 1.0
"""How long a worker that stopped without being told to waits before it starts again.

Procrastinate stops a worker that has lost its database, as when the database restarts.
Starting again waits for the database as the first start did. The pause keeps a worker
that stops at once, for any other reason, from going round in a circle."""

_DEFERRING = procrastinate.App(connector=procrastinate.PsycopgConnector())
"""Writes a job into the transaction it is given. It is never opened, so a process that
only defers, as the API does, holds no pool and no connection for the queue."""


@dataclass(frozen=True, slots=True)
class Job:
    """A job a worker can run: its name, and what running it does."""

    name: str
    run: Callable[..., Awaitable[None]]
    """Called with the arguments the job was deferred with. What else it needs, such as
    the database, is bound in by whoever lists the job."""
    every: str | None = None
    """When it also runs by itself: a cron line in UTC, such as `*/10 * * * *`, of five
    fields, or six with the seconds last. `None`: only when it is deferred."""


class DatabaseUnreachableError(Exception):
    """The worker couldn't connect to its database when it started."""


class DeferError(SQLAlchemyError):
    """The database refused a job, or was lost while the job was written.

    It is one of SQLAlchemy's errors because it is what any statement of the session
    raises when it fails: whoever handles those handles this. And as after any of
    them, the transaction is of no use any more."""


async def defer(db: AsyncSession, name: str, /, **args: Argument) -> None:
    """Queue the job called `name`, with `args`, in `db`'s transaction. Never commits.

    The job is there once the transaction commits, and never if it is rolled back: with
    it, or with a savepoint it was deferred in. Until then no worker sees it.

    A job is named, not imported: the code that defers sits below the code that runs
    it. A worker that has no job of that name marks it `failed`.

    Raises `DeferError` when the database refuses the job.
    """
    connection = await db.connection()
    raw = await connection.get_raw_connection()
    deferrer = _DEFERRING.configure_task(name, connection=raw.driver_connection)
    try:
        await deferrer.defer_async(**args)
    except queue_errors.ProcrastinateException as error:
        # Procrastinate wraps the driver's error in one of its own, which nothing
        # outside this module may know. What the database said is the cause.
        said = error.__cause__ or error
        raise DeferError(f"the job {name} couldn't be deferred: {said}") from said


async def work(
    listed: Sequence[Job],
    *,
    database_url: str,
    grace: float = SHUTDOWN_GRACE_S,
    until_idle: bool = False,
    on_signals: bool = True,
    check: Callable[[], Awaitable[None]] | None = None,
    on_beat: Callable[[], None] | None = None,
) -> None:
    """Run the jobs of `listed` as they are deferred, and those with a schedule by it,
    until the process is told to stop (SIGTERM or SIGINT). Then return.

    One job at a time. A worker hears of a new job when it is committed, and looks for
    one every five seconds besides. A job that succeeded is deleted. One that failed
    stays, as `failed`, and its error is in the log; it isn't tried again.

    Told to stop, the worker takes no new job, gives the one in hand `grace` seconds to
    finish, and cuts it off after that. It listens for the signals from before it
    connects, so it is told whenever it is; the first signal is the one that counts,
    and later ones change nothing.

    The worker connects through a pool of its own, of two connections at most, and one
    more that listens for new jobs. Raises `DatabaseUnreachableError` when the pool has
    no connection after 30 seconds. A worker that stops without being told to, as
    Procrastinate's does when it loses its database, starts again after
    `STARTS_AGAIN_AFTER_S`, and waits for the database as it did the first time.

    `check` is awaited each time the worker has connected, the first time and after
    each start again, before it takes a job. What it raises ends `work` with that
    exception, as anything else the worker raises does: such as a database that isn't
    at the migration the code expects.

    `on_beat` is called while the worker works: once it has connected and passed
    `check`, and every `alive.BEAT_S` after that, until that run of the worker ends.
    Not while it connects, nor while it waits to start again. One that fails is said
    once in the log, and the worker goes on.

    `until_idle` returns once nothing is left to run. `on_signals=False` leaves the
    process's signals alone: a test runs a worker so, and stops it by cancelling it.
    """
    signals = _StopSignals() if on_signals else None
    try:
        while True:
            await _work_once(
                listed,
                database_url=database_url,
                grace=grace,
                until_idle=until_idle,
                check=check,
                on_beat=on_beat,
            )
            if until_idle or (signals is not None and signals.told):
                return
            logger.warning(
                "The worker stopped, though nobody told it to: the lines above say why, such"
                " as a lost connection to the database. It starts again in %g s.",
                STARTS_AGAIN_AFTER_S,
            )
            await asyncio.sleep(STARTS_AGAIN_AFTER_S)
    except asyncio.CancelledError:
        # Cancelling it is how the worker is told to stop: by a signal, and then it has
        # done what it was told, or by whoever runs it, who is then told it stopped.
        if signals is None or not signals.stopped_it():
            raise
    finally:
        if signals is not None:
            signals.stop_listening()


class _StopSignals:
    """SIGTERM and SIGINT, heard on the event loop for the task that runs a worker.

    The first one stops the worker by cancelling that task: Procrastinate's worker then
    takes no new job, and gives the one in hand its grace. Later ones change nothing, so
    that job keeps its grace whatever else arrives. Procrastinate listens for signals
    only once its worker runs, and for the first one alone, so it is never asked to.
    """

    def __init__(self) -> None:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("a worker runs in a task of its own")
        self.told = False
        self._task = task
        self._loop = asyncio.get_running_loop()
        self._before: dict[signal.Signals, Any] = {}
        # Only the main thread gets signals: elsewhere, nothing is listened for.
        if threading.current_thread() is threading.main_thread():
            for stop in STOP_SIGNALS:
                self._before[stop] = signal.getsignal(stop)
                self._loop.add_signal_handler(stop, self._heard, stop)

    def _heard(self, received: signal.Signals) -> None:
        if self.told:
            logger.info("told to stop again (%s): the job in hand keeps its time", received.name)
            return
        self.told = True
        logger.info("told to stop (%s)", received.name)
        self._task.cancel()

    def stopped_it(self) -> bool:
        """Whether the cancellation that ends the worker is the one a signal asked for,
        and no other. If so, it is taken back: the worker did as it was told."""
        return self.told and self._task.uncancel() == 0

    def stop_listening(self) -> None:
        """Hand the signals back to whoever handled them before."""
        for stop, before in self._before.items():
            self._loop.remove_signal_handler(stop)
            if before is not None:  # None: a handler that wasn't set from Python
                signal.signal(stop, before)


async def _work_once(
    listed: Sequence[Job],
    *,
    database_url: str,
    grace: float,
    until_idle: bool,
    check: Callable[[], Awaitable[None]] | None,
    on_beat: Callable[[], None] | None,
) -> None:
    """One run of Procrastinate's worker, on a pool of its own, closed at the end."""
    app = procrastinate.App(
        connector=procrastinate.PsycopgConnector(conninfo=database_url, min_size=1, max_size=2)
    )
    for job in listed:
        task = app.task(name=job.name)(_handler(job))
        if job.every is not None:
            app.periodic(cron=job.every)(task)
    try:
        try:
            await app.open_async()
        except psycopg.OperationalError as error:
            # The pool's own wait for its first connection, which it logged its tries at.
            raise DatabaseUnreachableError(f"couldn't connect to the database: {error}") from error
        if check is not None:
            await check()
        beats = asyncio.create_task(_beat(on_beat)) if on_beat is not None else None
        try:
            await app.run_worker_async(
                wait=not until_idle,
                delete_jobs="successful",
                shutdown_graceful_timeout=grace,
                install_signal_handlers=False,  # `work` listens for them
            )
        finally:
            if beats is not None:
                beats.cancel()
                await asyncio.wait({beats})  # no beat after the run has ended
    finally:
        # Also when the worker was cancelled while it was still connecting: the pool
        # would go on trying behind its back.
        await app.close_async()


async def _beat(on_beat: Callable[[], None]) -> None:
    """Call `on_beat` now, and every `alive.BEAT_S` after, until cancelled.

    One that fails is said in the log once, not at every beat, until one succeeds
    again. The worker goes on: that it can't say so doesn't stop it working."""
    failing = False
    while True:
        try:
            on_beat()
        except Exception:
            if not failing:
                logger.warning(
                    "The worker couldn't say that it works, and goes on working. Its health"
                    " check will say that it doesn't.",
                    exc_info=True,
                )
            failing = True
        else:
            failing = False
        await asyncio.sleep(alive.BEAT_S)


def _handler(job: Job) -> Callable[..., Awaitable[None]]:
    async def handle(**args: Any) -> None:
        if job.every is not None:
            # Procrastinate tells a scheduled run which time it is for. No job needs it.
            args.pop("timestamp", None)
        await job.run(**args)

    return handle


def schema_files(up_to: str) -> list[str]:
    """The SQL that builds the queue's tables, as Procrastinate wrote it for each of its
    versions up to `up_to`, such as `"3.11.0"`, in the order it is applied.

    A migration of ours applies it. Procrastinate adds a file for each change to its
    schema, and never changes one, so this is the same whichever version is installed,
    as long as it is `up_to` or newer. Its `schema.sql`, the whole schema in one file,
    would be whatever version is installed: an upgrade would then change what an old
    migration builds.
    """
    folder = importlib.resources.files("procrastinate.sql") / "migrations"
    files = sorted(
        (_version(file.name.split("_", 1)[0]), file.name, file)
        for file in folder.iterdir()
        if file.name.endswith(".sql")
    )
    last = _version(up_to)
    return [file.read_text(encoding="utf-8") for version, _, file in files if version <= last]


def _version(written: str) -> tuple[int, ...]:
    """`3.11.0`, or `03.11.00` as Procrastinate's files are named, as numbers."""
    return tuple(int(part) for part in written.split("."))
