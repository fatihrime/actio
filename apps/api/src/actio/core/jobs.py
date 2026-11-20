"""Background jobs: a queue in Postgres, and the worker that takes jobs from it.

A job is deferred in the transaction of the change it follows (`defer`), so it exists
if and only if that change was committed. A worker runs it later (`work`), one at a
time, and runs the jobs that have a schedule by their schedule.

A job may run more than once: from its start again, after a stop cut it off, after the
worker that had it died, or after an error that its listing names as worth another try
(`RUNS_AGAIN`, `Queue.look_after`, `Again`). So every handler has to be safe to repeat.

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
from datetime import timedelta
from typing import Any

import procrastinate
import psycopg
from procrastinate import exceptions as queue_errors
from procrastinate import jobs as queued
from procrastinate.manager import JobManager
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
job meanwhile. After this the job is cut off, and goes back into the queue, to be run
again from its start by the next worker, up to `RUNS_AGAIN` times.

Whatever stops the process has to wait longer than this, or it cuts the job off
itself, without a word in the queue: the job is then left `doing`, as by a worker that
died, until a look after the queue finds it (`Queue.look_after`).

And it has to stay, with one beat of the worker, under `WORKER_DEAD_AFTER_S`: see there."""

RUNS_AGAIN = 3
"""How many times a job is run again from its start, in all, whatever the reason: after a
stop cut it off, after a worker that died left it, or after an error that its listing
names as worth another try (`Job.again`). One count for the three, which Procrastinate
keeps in the job's `attempts`. Then it isn't, once more: cut off, it stays `aborted`;
left by a worker that died, or failed with such an error, it ends as `failed`. A job
that takes its worker down each time would otherwise go round for ever, since the
worker's container starts again when it fails, and so would one whose error comes back
each time.

A job that fails with any other error is never run again."""

BEATS_EVERY_S = 10.0
"""How often a worker of the queue says that it is alive: a beat it writes into the
database, by the database's clock. Procrastinate's own default, said here because
`WORKER_DEAD_AFTER_S` and `SHUTDOWN_GRACE_S` are measured against it."""

WORKER_DEAD_AFTER_S = 30.0
"""How long a worker of the queue goes without saying that it is alive before it is taken
for dead, and the job it had in hand for left behind: three of its beats, Procrastinate's
own default.

A worker that is told to stop says no more that it is alive from that moment, while the
job in hand has its grace. So a beat and the grace together have to stay under this, or
another worker could take one that is stopping for dead, and put its job back while it
is still being finished. A test holds that."""

STARTS_AGAIN_AFTER_S = 1.0
"""How long a worker that stopped without being told to waits before it starts again.

Procrastinate stops a worker that has lost its database, as when the database restarts.
Starting again waits for the database as the first start did. The pause keeps a worker
that stops at once, for any other reason, from going round in a circle."""

_DEFERRING = procrastinate.App(connector=procrastinate.PsycopgConnector())
"""Writes a job into the transaction it is given. It is never opened, so a process that
only defers, as the API does, holds no pool and no connection for the queue."""

_QUEUE_LOG = logging.getLogger("procrastinate.worker")
"""The logger through which Procrastinate's worker says how each job ended."""


@dataclass(frozen=True, slots=True)
class Again:
    """Which errors of a job's handler are worth another try, and how long the job waits
    before each time it is run again after one (`Job.again`).

    Such an error puts the job back into the queue, to be run again from its start once
    its wait is over, and it counts as any other time the job is run again: `RUNS_AGAIN`
    in all, whatever the reason. The next one fails the job, as any other error does at
    once. The handler says in the log what went wrong, in its own words, before it raises:
    the queue's line for a job that is run again carries no trace.

    An error worth another try is raised without its cause (`from None`), and its text
    holds kinds and ids alone. Its trace is in the log when the last try fails it, and a
    cause's text, such as a model's error that quotes a question, would be printed there.

    Refused when it is made, with `ValueError` or `TypeError`, unless it names one error
    or more, each a kind of `Exception`, and one wait or more, each a `timedelta` and none
    below zero, both as tuples."""

    errors: tuple[type[Exception], ...]
    """The errors worth another try: these, and those of a kind derived from one of them."""
    waits: tuple[timedelta, ...]
    """How long the job waits before it is run again after such an error: the first before
    the first time it is run again, the second before the second, and so on. The last one
    also serves any time after it. A job that is put back is dated by its worker's clock,
    and taken by the database's, so a difference between the two makes the wait that
    much longer, or shorter."""

    def __post_init__(self) -> None:
        # Taken as anything: what is checked here is what the types say, at run time.
        errors: object = self.errors
        waits: object = self.waits
        if not (isinstance(errors, tuple) and isinstance(waits, tuple)):
            # What a worker asks of an error, `isinstance`, takes a tuple of kinds alone.
            raise TypeError("a job's listing gives its errors and its waits as tuples")
        if not errors:
            raise ValueError("a job's listing names at least one error worth another try")
        for error in errors:
            if not (isinstance(error, type) and issubclass(error, Exception)):
                # A cancellation, which is how a stop cuts a job off, among them: it has
                # its own rule.
                raise TypeError(f"{error!r} is no kind of Exception, and can't be tried again")
        if not waits:
            raise ValueError("a job's listing gives at least one wait before it is run again")
        if not all(isinstance(wait, timedelta) for wait in waits):
            raise TypeError("a wait before a job is run again is a timedelta")
        if any(wait < timedelta(0) for wait in waits):
            raise ValueError("a wait before a job is run again can't be below zero")

    def wait(self, run_again: int) -> timedelta:
        """The wait before the job is run again, when it has been run again `run_again`
        times already, for any reason."""
        return self.waits[min(run_again, len(self.waits) - 1)]


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
    with_queue: bool = False
    """Whether `run` looks after the queue itself. It is then called with the `Queue`
    first, as the job that runs sees it, and the arguments the job was deferred with
    after it."""
    again: Again | None = None
    """Which errors of `run` are worth another try, and how long the job waits before
    each. `None`: none is, and a job that raises fails at once."""


@dataclass(frozen=True, slots=True)
class LeftBehind:
    """A job that a worker which died had left `doing`: its name, and its id in the
    queue."""

    name: str
    id: int


@dataclass(frozen=True, slots=True)
class Upkeep:
    """What one look after the queue did (`Queue.look_after`)."""

    run_again: int
    """Jobs that workers which died had left `doing`, put back to be run again."""
    given_up: tuple[LeftBehind, ...]
    """Jobs they had left that had already been run again `RUNS_AGAIN` times: ended as
    `failed`."""
    dead_workers: int
    """Workers taken off the queue's list of workers."""


class Queue:
    """The queue, as the job that looks after it sees it (`Job.with_queue`).

    Procrastinate keeps the queue's books itself: which worker is alive, by a beat each
    one writes into the database every `BEATS_EVERY_S`, and when each job ended. They go
    by the database's clock, not the machine's, so nothing here is told the time. One
    thing in them goes by the machine's: the time a job that is put back is dated for,
    which a worker waits out when the database's clock is behind."""

    def __init__(self, manager: JobManager, looking: queued.Job) -> None:
        self._manager = manager
        self._looking = looking
        """The job that is looking: it is `doing` while it looks."""

    async def look_after(self, *, kept_for: timedelta) -> Upkeep:
        """See to what workers left behind: in this order,

        - run again the jobs that workers which died had left `doing`, or end those
          that have been run again `RUNS_AGAIN` times as `failed`. A job whose worker
          is alive is left alone, and so is the job that is looking;
        - take the workers that died off the queue's list;
        - delete the jobs that failed, were cut off or were cancelled, once they ended
          more than `kept_for` ago, in whole hours, with their events.

        A worker is taken for dead once it hasn't said that it is alive for
        `WORKER_DEAD_AFTER_S`. The worker that is looking says so first, so that it is
        never taken for one, as it could be after the machine slept.

        Each step is written at once: what was done stays done when a later one fails.
        Raises `UpkeepError` when the database refuses a step, or is lost."""
        try:
            return await self._look_after(kept_for)
        except queue_errors.ProcrastinateException as error:
            # As in `defer`: nothing outside this module may know Procrastinate's errors.
            said = error.__cause__ or error
            raise UpkeepError(f"the queue couldn't be looked after: {said}") from said

    async def _look_after(self, kept_for: timedelta) -> Upkeep:
        looking_id = self._looking.id
        if self._looking.worker_id is not None:
            await self._manager.update_heartbeat(self._looking.worker_id)
        run_again = 0
        given_up: list[LeftBehind] = []
        for job in await self._manager.get_stalled_jobs(
            seconds_since_heartbeat=WORKER_DEAD_AFTER_S
        ):
            # Its own worker's row may be gone, taken off the list by another, and then
            # the job that is looking is among those Procrastinate finds.
            if job.id is None or job.id == looking_id:
                continue
            try:
                if job.attempts < RUNS_AGAIN:
                    await self._manager.retry_job(job)
                    run_again += 1
                else:
                    await self._manager.finish_job(job, queued.Status.FAILED, delete_job=False)
                    given_up.append(LeftBehind(job.task_name, job.id))
            except queue_errors.ConnectorException as error:
                if not isinstance(error.__cause__, psycopg.errors.RaiseException):
                    raise
                # Procrastinate's own refusal: the job is no longer `doing`. Its worker
                # wasn't dead after all, and has ended it since it was looked at.
                continue
        dead = await self._manager.prune_stalled_workers(WORKER_DEAD_AFTER_S)
        await self._manager.delete_old_jobs(
            nb_hours=int(kept_for / timedelta(hours=1)),
            include_failed=True,
            include_cancelled=True,
            include_aborted=True,
        )
        return Upkeep(run_again=run_again, given_up=tuple(given_up), dead_workers=len(dead))


class UpkeepError(Exception):
    """The queue couldn't be looked after: its database refused a step, or was lost
    (`Queue.look_after`)."""


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
    stays, as `failed`, and its error is in the log, with its trace; it isn't tried
    again. Unless its listing names the error as worth another try (`Job.again`), and it
    hasn't been run again `RUNS_AGAIN` times already: then it goes back into the queue,
    to be run again after its wait, and the log says so in one line, without the trace.
    A worker hears nothing of a job that is put back: it finds it at a look once it is
    due.

    Told to stop, the worker takes no new job, gives the one in hand `grace` seconds to
    finish, and cuts it off after that: the job goes back into the queue, to be run
    again from its start, unless it has been run again `RUNS_AGAIN` times already, and
    then it stays as `aborted`. It listens for the signals from before it connects, so
    it is told whenever it is; the first signal is the one that counts, and later ones
    change nothing.

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
    _leave_out_the_trace_of_an_error_tried_again()
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
        run_again = _RunAgain(job.again)
        if job.with_queue:
            task = app.task(name=job.name, retry=run_again, pass_context=True)(_looking_after(job))
        else:
            task = app.task(name=job.name, retry=run_again)(_handler(job))
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
                # Procrastinate's defaults, said here: the look after the queue and the
                # grace at a stop are measured against them.
                update_heartbeat_interval=BEATS_EVERY_S,
                stalled_worker_timeout=WORKER_DEAD_AFTER_S,
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


def _looking_after(job: Job) -> Callable[..., Awaitable[None]]:
    """What runs a job that looks after the queue: Procrastinate passes it the context
    of the run, which holds the worker's own app and the job itself."""

    async def handle(context: procrastinate.JobContext, **args: Any) -> None:
        if job.every is not None:
            args.pop("timestamp", None)  # as in `_handler`
        await job.run(Queue(context.app.job_manager, context.job), **args)

    return handle


class _RunAgain(procrastinate.BaseRetryStrategy):
    """Procrastinate asks this of every job that didn't succeed: whether to run it again,
    and when. One for each job a worker lists, with that job's `Again`, if it has one.

    - A job that has been run again `RUNS_AGAIN` times already, for any reason, is not.
    - A job cut off at a stop, which Procrastinate does by cancelling its task, is run
      again at once, by the next worker that takes it.
    - A job that raised an error its `Again` names is run again once the wait for that
      time is over. Procrastinate reads the clock for it, as for a job put back at once.
    - A job that raised any other error is not: it stays `failed`."""

    def __init__(self, again: Again | None) -> None:
        self._again = again

    def get_retry_decision(
        self, *, exception: BaseException, job: queued.Job
    ) -> procrastinate.RetryDecision | None:
        if job.attempts >= RUNS_AGAIN:
            return None
        if isinstance(exception, asyncio.CancelledError):
            return procrastinate.RetryDecision()
        if self._again is not None and isinstance(exception, self._again.errors):
            wait = self._again.wait(job.attempts)
            return procrastinate.RetryDecision(
                # The wait exactly, as the one whole number Procrastinate takes for it.
                retry_in={"microseconds": wait // timedelta(microseconds=1)}
            )
        return None


class _NoTraceForAnErrorTriedAgain(logging.Filter):
    """Takes the trace off Procrastinate's line for a job that raised an error its listing
    names as worth another try, and goes back into the queue: "… ended with status: Error,
    to retry". Such an error is no error yet, and its handler has said in its own words
    what went wrong. Its trace would say nothing more, in some 25 lines, at each try.

    Every other line keeps its trace: the last try's, which fails the job, as an error;
    that of a job failed by any other error; and that of a job cut off at a stop
    ("Aborted, to retry"), which says where the job was when it was cut off."""

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "action", None) == "job_error_retry":  # Procrastinate's name
            record.exc_info = None
        return True


def _leave_out_the_trace_of_an_error_tried_again() -> None:
    """Put `_NoTraceForAnErrorTriedAgain` on Procrastinate's worker's log, unless it is
    there: one for the process, however many times a worker starts in it, and whoever
    set the log up."""
    if not any(isinstance(each, _NoTraceForAnErrorTriedAgain) for each in _QUEUE_LOG.filters):
        _QUEUE_LOG.addFilter(_NoTraceForAnErrorTriedAgain())


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
