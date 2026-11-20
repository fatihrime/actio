"""Background jobs, against a real Postgres: deferred in the transaction of a change, and
run by a worker in this process. And what is left behind when a stop cuts a job off, or
a worker dies with one in hand: looked after by a job that looks after the queue.

A test stops a worker by cancelling it, which ends it the way the process's signal
does. The real process, and its signal, are in `test_worker.py`. A worker that died is
written into the queue's books as one that was killed leaves them (`queue_books`).
"""

import asyncio
import logging
import os
import signal
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta
from functools import partial
from types import FrameType
from typing import Any

import psycopg
import pytest
from psycopg import sql
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from actio.core import alive, jobs
from actio.core.database import create_engine, create_session_factory
from actio.identity import sessions
from actio.identity.models import User


class Noted:
    """A job's handler that notes what each run was given."""

    def __init__(self) -> None:
        self.runs: list[dict[str, Any]] = []

    async def __call__(self, **args: Any) -> None:
        self.runs.append(args)


async def queue_of(database_url: str) -> list[tuple[str, str]]:
    """The jobs in the queue as another connection sees them, oldest first: each one's
    name and status."""
    async with await psycopg.AsyncConnection.connect(database_url) as connection:
        cursor = await connection.execute(
            "SELECT task_name, status::text FROM procrastinate_jobs ORDER BY id"
        )
        return [(name, status) for name, status in await cursor.fetchall()]


async def attempts_in(database_url: str) -> list[tuple[str, str, int]]:
    """The jobs in the queue, oldest first: each one's name, status, and how many times
    it has been run again."""
    async with await psycopg.AsyncConnection.connect(database_url) as connection:
        cursor = await connection.execute(
            "SELECT task_name, status::text, attempts FROM procrastinate_jobs ORDER BY id"
        )
        return [(name, status, attempts) for name, status, attempts in await cursor.fetchall()]


async def someone_waits_for_a_lock(database_url: str) -> bool:
    """Whether a connection to this database waits for a lock that another holds."""
    async with await psycopg.AsyncConnection.connect(database_url) as connection:
        cursor = await connection.execute(
            "SELECT count(*) FROM pg_stat_activity"
            " WHERE datname = current_database() AND wait_event_type = 'Lock'"
        )
        row = await cursor.fetchone()
    return row is not None and row[0] > 0


class LooksAfter:
    """A job that looks after the queue, as the worker's own does, and notes what each
    look did.

    `first` is awaited before it looks, while its worker runs: a worker takes the
    workers that died off the list when it starts, so what a dead worker leaves is
    written there."""

    def __init__(
        self,
        *,
        kept_for: timedelta = timedelta(days=7),
        first: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.kept_for = kept_for
        self.first = first
        self.looks: list[jobs.Upkeep] = []

    async def __call__(self, queue: jobs.Queue) -> None:
        if self.first is not None:
            await self.first()
        self.looks.append(await queue.look_after(kept_for=self.kept_for))


async def until(done: Callable[[], bool], *, within: float = 30) -> None:
    """Wait until `done()` holds, looking every twentieth of a second. The test fails
    after `within` seconds.

    What is waited for is written by code that knows nothing of the test, such as a log
    record, so there is no event to wait on."""
    async with asyncio.timeout(within):
        while not done():  # noqa: ASYNC110 - see above
            await asyncio.sleep(0.05)


@pytest.mark.anyio
async def test_a_job_deferred_in_a_transaction_that_commits_is_run_with_its_arguments(
    db: AsyncSession, work_through: Any
) -> None:
    noted = Noted()
    arguments: dict[str, jobs.Argument] = {
        "text": "Velora Cycles: ön fren balatası",
        "count": 3,
        "share": 0.25,
        "paid": True,
        "nothing": None,
        "order": {"sizes": [52, 54, 56], "frame": {"colour": "teal", "tags": ["gravel", 2]}},
    }
    await jobs.defer(db, "note", **arguments)
    await db.commit()

    await work_through(jobs.Job("note", noted))

    assert noted.runs == [arguments]


@pytest.mark.anyio
async def test_a_job_deferred_in_a_transaction_that_is_rolled_back_never_exists(
    db: AsyncSession, migrated: str, work_through: Any
) -> None:
    noted = Noted()
    await jobs.defer(db, "note", n=1)
    await db.rollback()

    await work_through(jobs.Job("note", noted))

    assert noted.runs == []
    assert await queue_of(migrated) == []


@pytest.mark.anyio
async def test_a_job_deferred_in_a_session_left_by_an_exception_never_exists(
    migrated: str, work_through: Any
) -> None:
    engine = create_engine(migrated)

    async def defer_then_fail() -> None:
        async with create_session_factory(engine)() as db:
            await jobs.defer(db, "note", n=1)
            raise RuntimeError("something failed after the job was deferred")

    try:
        with pytest.raises(RuntimeError, match="something failed"):
            await defer_then_fail()
    finally:
        await engine.dispose()
    noted = Noted()
    await work_through(jobs.Job("note", noted))

    assert noted.runs == []
    assert await queue_of(migrated) == []


@pytest.mark.anyio
async def test_a_savepoint_that_is_rolled_back_takes_only_its_own_job_with_it(
    db: AsyncSession, work_through: Any
) -> None:
    await jobs.defer(db, "note", n=1)
    savepoint = await db.begin_nested()
    await jobs.defer(db, "note", n=2)
    await savepoint.rollback()
    await jobs.defer(db, "note", n=3)
    await db.commit()
    noted = Noted()

    await work_through(jobs.Job("note", noted))

    assert noted.runs == [{"n": 1}, {"n": 3}]


@pytest.mark.anyio
async def test_until_the_commit_nothing_outside_the_transaction_sees_the_job(
    db: AsyncSession, migrated: str, work_through: Any, clock: Any
) -> None:
    """Neither the job nor the change beside it, which the session goes on making after
    the deferral. A worker that works through the queue meanwhile runs nothing."""
    noted = Noted()
    await jobs.defer(db, "note", n=1)
    _, guest = await sessions.start_guest_session(db, now=clock.now)

    assert await queue_of(migrated) == []
    await work_through(jobs.Job("note", noted))
    assert noted.runs == []

    await db.commit()
    await work_through(jobs.Job("note", noted))
    assert noted.runs == [{"n": 1}]
    assert (await db.scalars(select(User.id))).all() == [guest.user_id]


def test_a_process_that_runs_no_worker_defers_a_job(migrated: str) -> None:
    """As the API defers: in a process where nothing of the queue was opened, through
    the connection of the session's own transaction."""
    deferring = (
        "import asyncio, sys\n"
        "from actio.core import jobs\n"
        "from actio.core.database import create_engine, create_session_factory\n"
        "async def main() -> None:\n"
        "    engine = create_engine(sys.argv[1])\n"
        "    async with create_session_factory(engine)() as db:\n"
        "        await jobs.defer(db, 'note', said='from a process that runs no worker')\n"
        "        await db.commit()\n"
        "    await engine.dispose()\n"
        "asyncio.run(main())\n"
    )

    done = subprocess.run(  # noqa: S603 - this interpreter, running the lines above
        [sys.executable, "-c", deferring, migrated],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert done.returncode == 0, done.stderr[-400:]
    with psycopg.connect(migrated) as connection:
        queued = connection.execute(
            "SELECT task_name, args, status::text FROM procrastinate_jobs"
        ).fetchall()
    assert queued == [("note", {"said": "from a process that runs no worker"}, "todo")]


@pytest.mark.anyio
async def test_a_job_the_database_refuses_fails_as_any_statement_of_the_session_does(
    db: AsyncSession,
) -> None:
    """Here the transaction was already lost, to a statement before the deferral. What
    comes out is an error of SQLAlchemy's, which the code that handles a failed
    statement handles too, and none of the queue's library."""
    with pytest.raises(SQLAlchemyError, match="division by zero"):
        await db.execute(text("SELECT 1 / 0"))

    with pytest.raises(jobs.DeferError, match=r"note couldn't be deferred.*aborted") as refused:
        await jobs.defer(db, "note", n=1)

    assert isinstance(refused.value, SQLAlchemyError)
    assert not type(refused.value.__cause__).__module__.startswith("procrastinate")


@pytest.mark.anyio
async def test_a_worker_cancelled_while_it_connects_leaves_nothing_running() -> None:
    """Nothing listens where it is told to connect, so it is still trying when it is
    cancelled. Its pool must not go on trying behind its back."""
    before = asyncio.all_tasks()
    worker = asyncio.create_task(
        jobs.work([], database_url="postgresql://actio:pw@127.0.0.1:1/actio", on_signals=False)
    )
    await asyncio.sleep(0.3)

    worker.cancel()
    await asyncio.wait([worker], timeout=10)

    assert worker.cancelled()
    assert asyncio.all_tasks() == before


@pytest.mark.anyio
async def test_a_worker_checks_once_it_has_connected_and_then_says_at_once_that_it_works(
    migrated: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A beat an hour long: the first is the one that comes when the worker starts."""
    monkeypatch.setattr(alive, "BEAT_S", 3600.0)
    happened: list[str] = []

    async def check() -> None:
        happened.append("checked")

    worker = asyncio.create_task(
        jobs.work(
            [],
            database_url=migrated,
            on_signals=False,
            check=check,
            on_beat=lambda: happened.append("beat"),
        )
    )
    try:
        await until(lambda: "beat" in happened, within=10)
    finally:
        worker.cancel()
        await asyncio.wait([worker], timeout=10)

    assert happened == ["checked", "beat"]


@pytest.mark.anyio
async def test_a_worker_says_again_and_again_that_it_works_and_never_once_it_stopped(
    migrated: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(alive, "BEAT_S", 0.05)
    beats: list[float] = []
    worker = asyncio.create_task(
        jobs.work(
            [],
            database_url=migrated,
            on_signals=False,
            on_beat=lambda: beats.append(time.monotonic()),
        )
    )
    try:
        await until(lambda: len(beats) >= 3, within=10)
    finally:
        worker.cancel()
        await asyncio.wait([worker], timeout=10)
    stopped_with = len(beats)
    await asyncio.sleep(0.25)  # five beats' time

    assert worker.cancelled()
    assert len(beats) == stopped_with


@pytest.mark.anyio
async def test_a_worker_that_cannot_connect_neither_checks_nor_says_that_it_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing listens where it is told to connect, so it is still trying, for ten
    beats, when it is cancelled."""
    monkeypatch.setattr(alive, "BEAT_S", 0.05)
    happened: list[str] = []

    async def check() -> None:
        happened.append("checked")

    worker = asyncio.create_task(
        jobs.work(
            [],
            database_url="postgresql://actio:pw@127.0.0.1:1/actio",
            on_signals=False,
            check=check,
            on_beat=lambda: happened.append("beat"),
        )
    )
    await asyncio.sleep(0.5)

    worker.cancel()
    await asyncio.wait([worker], timeout=10)

    assert worker.cancelled()
    assert happened == []


@pytest.mark.anyio
async def test_a_worker_that_cannot_say_that_it_works_says_so_once_and_goes_on_working(
    db: AsyncSession,
    migrated: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """As when the disk its file is on is full."""
    monkeypatch.setattr(alive, "BEAT_S", 0.05)
    tries: list[bool] = []

    def full_disk() -> None:
        tries.append(True)
        raise OSError(28, "No space left on device")

    noted = Noted()
    worker = asyncio.create_task(
        jobs.work(
            [jobs.Job("note", noted)], database_url=migrated, on_signals=False, on_beat=full_disk
        )
    )
    try:
        await until(lambda: len(tries) >= 4, within=10)
        await jobs.defer(db, "note", n=1)
        await db.commit()
        await until(lambda: noted.runs == [{"n": 1}])

        assert not worker.done()
    finally:
        worker.cancel()
        await asyncio.wait([worker], timeout=10)

    said = [
        record
        for record in caplog.records
        if record.name == "actio.core.jobs" and "couldn't say that it works" in record.getMessage()
    ]
    assert len(tries) >= 4
    assert [record.levelno for record in said] == [logging.WARNING]
    assert said[0].exc_info is not None
    assert "No space left on device" in str(said[0].exc_info[1])


@pytest.mark.anyio
async def test_a_worker_whose_check_fails_runs_no_job_and_ends_with_the_checks_error(
    db: AsyncSession, migrated: str
) -> None:
    """The job is in the queue before the worker starts, so a worker that took jobs
    before its check would run it. The worker's pool is closed: nothing of it is left
    running."""
    noted = Noted()
    await jobs.defer(db, "note", n=1)
    await db.commit()
    refused = RuntimeError("not the database this code expects")

    async def check() -> None:
        raise refused

    before = asyncio.all_tasks()
    with pytest.raises(RuntimeError) as ended:
        async with asyncio.timeout(10):
            await jobs.work(
                [jobs.Job("note", noted)], database_url=migrated, on_signals=False, check=check
            )

    assert ended.value is refused
    assert noted.runs == []
    assert await queue_of(migrated) == [("note", "todo")]
    assert asyncio.all_tasks() == before


@pytest.mark.anyio
async def test_a_job_that_fails_is_kept_as_failed_and_logged_and_the_next_one_runs(
    db: AsyncSession, migrated: str, work_through: Any, caplog: pytest.LogCaptureFixture
) -> None:
    bent = RuntimeError("the frame is bent")
    tries: list[bool] = []

    async def breaks() -> None:
        tries.append(True)
        raise bent

    noted = Noted()
    await jobs.defer(db, "breaks")
    await jobs.defer(db, "note", n=1)
    await db.commit()

    await work_through(jobs.Job("breaks", breaks), jobs.Job("note", noted))

    assert await queue_of(migrated) == [("breaks", "failed")]
    assert tries == [True]  # once: a job that failed isn't tried again
    assert noted.runs == [{"n": 1}]
    [said] = [
        record
        for record in caplog.records
        if record.levelno >= logging.ERROR and "breaks" in record.getMessage()
    ]
    assert said.exc_info is not None
    assert said.exc_info[1] is bent


@pytest.mark.anyio
async def test_a_job_that_no_handler_is_listed_for_ends_failed_and_the_next_one_runs(
    db: AsyncSession, migrated: str, work_through: Any
) -> None:
    """Deferred by newer code than the worker runs, say."""
    noted = Noted()
    await jobs.defer(db, "known-to-a-newer-worker")
    await jobs.defer(db, "note", n=1)
    await db.commit()

    await work_through(jobs.Job("note", noted))

    assert await queue_of(migrated) == [("known-to-a-newer-worker", "failed")]
    assert noted.runs == [{"n": 1}]


@pytest.mark.anyio
async def test_a_job_that_succeeded_leaves_nothing_behind(
    db: AsyncSession, migrated: str, work_through: Any
) -> None:
    """A job every ten minutes would otherwise leave its row, and its events, for ever."""
    await jobs.defer(db, "note", n=1)
    await db.commit()

    await work_through(jobs.Job("note", Noted()))

    async with await psycopg.AsyncConnection.connect(migrated) as connection:
        cursor = await connection.execute(
            "SELECT (SELECT count(*) FROM procrastinate_jobs),"
            " (SELECT count(*) FROM procrastinate_events)"
        )
        assert await cursor.fetchone() == (0, 0)


@pytest.mark.anyio
async def test_a_scheduled_job_runs_by_its_schedule_and_once_whatever_the_workers(
    migrated: str,
) -> None:
    """Every second, with two workers at once, for three and a half seconds.

    A worker that starts runs the second that has begun, and each later one half a
    second after it has begun. So three or four seconds are run in that time, whenever
    in a second the workers start. Were each worker to run each of them, it would be
    six runs or more.
    """
    ran: list[float] = []

    async def tick() -> None:
        ran.append(time.monotonic())

    every_second = jobs.Job("tick", tick, every="* * * * * *")  # six fields: seconds last
    workers = [
        asyncio.create_task(jobs.work([every_second], database_url=migrated, on_signals=False))
        for _ in range(2)
    ]
    try:
        await asyncio.sleep(3.5)
    finally:
        for worker in workers:
            worker.cancel()
        await asyncio.wait(workers, timeout=10)

    assert all(worker.done() for worker in workers)
    assert 3 <= len(ran) <= 5, ran


@pytest.mark.anyio
async def test_told_to_stop_a_worker_lets_the_job_in_hand_finish(
    db: AsyncSession, migrated: str
) -> None:
    started = asyncio.Event()
    finished: list[bool] = []

    async def takes_a_moment() -> None:
        started.set()
        await asyncio.sleep(0.3)
        finished.append(True)

    await jobs.defer(db, "slow")
    await db.commit()
    worker = asyncio.create_task(
        jobs.work(
            [jobs.Job("slow", takes_a_moment)], database_url=migrated, grace=5, on_signals=False
        )
    )
    async with asyncio.timeout(10):
        await started.wait()

    worker.cancel()
    await asyncio.wait([worker], timeout=10)

    assert worker.cancelled()
    assert finished == [True]
    assert await queue_of(migrated) == []  # it succeeded, and was deleted


@pytest.mark.anyio
async def test_a_job_cut_off_at_a_stop_goes_back_into_the_queue_and_the_next_worker_runs_it(
    db: AsyncSession, migrated: str
) -> None:
    """The first time it runs, the job outlasts the grace. The second time, it ends."""
    started = asyncio.Event()
    happened: list[str] = []

    async def ends_the_second_time() -> None:
        if happened:
            happened.append("ran to its end")
            return
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            happened.append("cut off")
            raise

    endless = jobs.Job("endless", ends_the_second_time)
    await jobs.defer(db, "endless")
    await db.commit()
    first = asyncio.create_task(
        jobs.work([endless], database_url=migrated, grace=0.5, on_signals=False)
    )
    try:
        async with asyncio.timeout(10):
            await started.wait()
        told = time.monotonic()
    finally:
        first.cancel()  # which is the stop, and also leaves nothing running if it never started
        await asyncio.wait([first], timeout=10)
    took = time.monotonic() - told
    after_the_stop = await attempts_in(migrated)
    second = asyncio.create_task(jobs.work([endless], database_url=migrated, on_signals=False))
    try:
        await until(lambda: len(happened) == 2, within=15)
    finally:
        second.cancel()
        await asyncio.wait([second], timeout=10)

    assert first.cancelled()
    assert 0.45 < took < 2.0
    assert after_the_stop == [("endless", "todo", 1)]  # to be run again, by nobody yet
    assert happened == ["cut off", "ran to its end"]
    assert await queue_of(migrated) == []  # it succeeded, and was deleted


@pytest.mark.parametrize(
    ("run_again_before", "then"),
    [(jobs.RUNS_AGAIN - 1, "todo"), (jobs.RUNS_AGAIN, "aborted")],
    ids=["once more", "no more"],
)
@pytest.mark.anyio
async def test_a_job_cut_off_at_a_stop_is_run_again_only_so_many_times(
    run_again_before: int, then: str, db: AsyncSession, migrated: str, queue_books: Any
) -> None:
    """A job that takes its worker down each time would otherwise go round for ever.
    Here it has already been run again: one time short of the limit, or as many times
    as that. Cut off once more, it goes back into the queue, or stays as `aborted`."""
    started = asyncio.Event()
    cut_off: list[bool] = []

    async def never_ends() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cut_off.append(True)
            raise

    job_id = await queue_books.deferred(db, "endless")
    await queue_books.already_run_again(job_id, run_again_before)
    worker = asyncio.create_task(
        jobs.work(
            [jobs.Job("endless", never_ends)], database_url=migrated, grace=0.2, on_signals=False
        )
    )
    try:
        async with asyncio.timeout(10):
            await started.wait()
    finally:
        worker.cancel()  # which is the stop, and also leaves nothing running if it never started
        await asyncio.wait([worker], timeout=10)

    assert worker.cancelled()
    assert cut_off == [True]
    assert await queue_of(migrated) == [("endless", then)]


@pytest.mark.anyio
async def test_the_job_that_looks_after_the_queue_goes_back_into_it_too_when_a_stop_cuts_it_off(
    db: AsyncSession, migrated: str
) -> None:
    """Every job a worker lists is asked the same, the one that is given the queue as
    well: a look that a stop cut off is no job that failed."""
    started = asyncio.Event()

    async def never_ends(_queue: jobs.Queue) -> None:
        started.set()
        await asyncio.Event().wait()

    await jobs.defer(db, "look")
    await db.commit()
    worker = asyncio.create_task(
        jobs.work(
            [jobs.Job("look", never_ends, with_queue=True)],
            database_url=migrated,
            grace=0.2,
            on_signals=False,
        )
    )
    try:
        async with asyncio.timeout(10):
            await started.wait()
    finally:
        worker.cancel()
        await asyncio.wait([worker], timeout=10)

    assert await attempts_in(migrated) == [("look", "todo", 1)]


@pytest.mark.anyio
async def test_told_twice_a_worker_still_lets_the_job_in_hand_finish(
    db: AsyncSession, migrated: str
) -> None:
    """Told by real signals, sent to this process: a SIGTERM, then a SIGINT. The second
    changes nothing, and the worker ends as the first told it to.

    While the test runs, a handler of its own stands in for the test run's, so that
    neither signal can stop the run if the worker doesn't listen. It notes what reaches
    it, which should be nothing."""
    started = asyncio.Event()
    finished: list[bool] = []

    async def takes_a_moment() -> None:
        started.set()
        await asyncio.sleep(0.5)
        finished.append(True)

    reached_the_test: list[int] = []

    def stand_in(received: int, _frame: FrameType | None) -> None:
        reached_the_test.append(received)

    await jobs.defer(db, "slow")
    await db.commit()
    before = {told: signal.signal(told, stand_in) for told in (signal.SIGTERM, signal.SIGINT)}
    try:
        worker = asyncio.create_task(
            jobs.work([jobs.Job("slow", takes_a_moment)], database_url=migrated, grace=5)
        )
        try:
            async with asyncio.timeout(10):
                await started.wait()
            os.kill(os.getpid(), signal.SIGTERM)
            # The second comes once the worker is waiting for the job in hand: a tenth
            # of a second into the job's half.
            await asyncio.sleep(0.1)
            assert finished == []
            os.kill(os.getpid(), signal.SIGINT)
            await asyncio.wait([worker], timeout=10)
        finally:
            if not worker.done():  # the test has failed: leave nothing running
                worker.cancel()
                await asyncio.wait([worker], timeout=10)
        handled_after = [signal.getsignal(told) for told in before]
    finally:
        for told, handler in before.items():
            signal.signal(told, handler)

    assert not worker.cancelled()
    assert worker.exception() is None  # it returned, as a worker that was told to stop
    assert finished == [True]
    assert reached_the_test == []
    assert handled_after == [stand_in, stand_in]  # what it found, given back
    assert await queue_of(migrated) == []  # the job succeeded, and was deleted


@pytest.mark.anyio
async def test_a_worker_that_listens_for_signals_is_still_cancelled_by_whoever_runs_it(
    db: AsyncSession, migrated: str
) -> None:
    """No signal came, so the cancellation isn't one the worker asked for: it stays a
    cancellation. And the signals are handled afterwards as they were before."""
    handled_before = [signal.getsignal(told) for told in (signal.SIGTERM, signal.SIGINT)]
    noted = Noted()
    await jobs.defer(db, "note", n=1)
    await db.commit()
    worker = asyncio.create_task(jobs.work([jobs.Job("note", noted)], database_url=migrated))
    try:
        await until(lambda: noted.runs == [{"n": 1}])
    finally:
        worker.cancel()
        await asyncio.wait([worker], timeout=10)

    assert worker.cancelled()
    handled_after = [signal.getsignal(told) for told in (signal.SIGTERM, signal.SIGINT)]
    assert handled_after == handled_before


def started_again(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """What the worker said when it stopped without being told to, and started again."""
    return [
        record
        for record in caplog.records
        if record.name == "actio.core.jobs" and "nobody told it to" in record.getMessage()
    ]


@pytest.mark.anyio
async def test_a_worker_lives_through_its_database_going_away_for_a_moment(
    db: AsyncSession,
    migrated: str,
    postgres_url: str,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The database refuses connections, and those it had are cut, as when it restarts.
    Procrastinate's worker can't listen for new jobs again at once, and stops. The same
    call of `work` starts it again once the database is back, checks it again, runs the
    next job, and says again that it works.

    Only this test's database goes away, not the server the other tests share.

    The worker has a job with a schedule, as the real one has the sweep: Procrastinate
    watches the connection that listens only while something is scheduled."""
    monkeypatch.setattr(alive, "BEAT_S", 0.05)
    checks: list[bool] = []
    beats: list[tuple[int, int]] = []
    """For each beat, what had happened by then: how often the worker had checked its
    database, and how often it had said that it starts again."""

    async def check() -> None:
        checks.append(True)

    noted = Noted()
    yearly = jobs.Job("new-year", Noted(), every="0 0 1 1 *")
    database = migrated.rsplit("/", 1)[1]
    allow = sql.SQL("ALTER DATABASE {} WITH ALLOW_CONNECTIONS {}")
    worker = asyncio.create_task(
        jobs.work(
            [jobs.Job("note", noted), yearly],
            database_url=migrated,
            on_signals=False,
            check=check,
            on_beat=lambda: beats.append((len(checks), len(started_again(caplog)))),
        )
    )
    try:
        await jobs.defer(db, "note", n=1)
        await db.commit()
        await until(lambda: noted.runs == [{"n": 1}])

        async with await psycopg.AsyncConnection.connect(postgres_url, autocommit=True) as server:
            await server.execute(allow.format(sql.Identifier(database), sql.SQL("false")))
            try:
                await server.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
                    [database],
                )
                # Until Procrastinate has found its listening connection gone for good.
                await until(
                    lambda: any(
                        record.name.startswith("procrastinate") and record.levelno >= logging.ERROR
                        for record in caplog.records
                    )
                )
            finally:
                await server.execute(allow.format(sql.Identifier(database), sql.SQL("true")))

        await until(lambda: bool(started_again(caplog)))
        await jobs.defer(db, "note", n=2)
        await db.commit()
        await until(lambda: len(noted.runs) == 2)
        beaten = len(beats)
        await until(lambda: len(beats) >= beaten + 3)

        assert not worker.done()  # the same call of `work`, which hasn't returned
    finally:
        worker.cancel()
        await asyncio.wait([worker], timeout=10)

    assert noted.runs == [{"n": 1}, {"n": 2}]
    assert len(started_again(caplog)) == 1
    assert checks == [True, True]  # when it first connected, and when it was back
    # While it waited to start again, a second and then for its database, it didn't say
    # that it works: no beat between the word that it starts again and the next check.
    assert (1, 0) in beats
    assert (1, 1) not in beats
    assert (2, 1) in beats


@pytest.mark.parametrize(
    ("worker_gone", "run_again_before"),
    [(False, 0), (True, 0), (False, jobs.RUNS_AGAIN - 1)],
    ids=["its worker silent", "its worker off the list", "for the last time"],
)
@pytest.mark.anyio
async def test_a_job_a_dead_worker_left_doing_is_run_again_once_the_queue_is_looked_after(
    worker_gone: bool, run_again_before: int, db: AsyncSession, migrated: str, queue_books: Any
) -> None:
    """As a worker that was killed with the job in hand leaves it: `doing`, under a
    worker that has been silent for a minute, or under none, once another worker has
    taken that one off the list. The job that looks comes first in the queue, and the
    worker that runs it then runs the job it put back."""
    noted = Noted()
    await queue_books.deferred(db, "look")
    left = await queue_books.deferred(db, "note", n=1)
    await queue_books.already_run_again(left, run_again_before)
    looks = LooksAfter(first=partial(queue_books.left_doing, left, worker_gone=worker_gone))
    worker = asyncio.create_task(
        jobs.work(
            [jobs.Job("note", noted), jobs.Job("look", looks, with_queue=True)],
            database_url=migrated,
            on_signals=False,
        )
    )
    try:
        await until(lambda: noted.runs == [{"n": 1}], within=15)
    finally:
        worker.cancel()
        await asyncio.wait([worker], timeout=10)

    assert looks.looks == [
        jobs.Upkeep(run_again=1, given_up=(), dead_workers=0 if worker_gone else 1)
    ]
    assert await queue_of(migrated) == []  # both succeeded, and were deleted


@pytest.mark.anyio
async def test_a_job_a_dead_worker_left_after_it_was_run_again_for_the_last_time_ends_failed(
    db: AsyncSession, migrated: str, queue_books: Any, work_through: Any
) -> None:
    noted = Noted()
    await queue_books.deferred(db, "look")
    left = await queue_books.deferred(db, "note", n=1)
    await queue_books.already_run_again(left, jobs.RUNS_AGAIN)
    looks = LooksAfter(first=partial(queue_books.left_doing, left))

    await work_through(jobs.Job("note", noted), jobs.Job("look", looks, with_queue=True))

    assert looks.looks == [
        jobs.Upkeep(run_again=0, given_up=(jobs.LeftBehind("note", left),), dead_workers=1)
    ]
    assert noted.runs == []
    assert await queue_of(migrated) == [("note", "failed")]


@pytest.mark.anyio
async def test_a_job_whose_worker_is_alive_is_left_alone_when_the_queue_is_looked_after(
    db: AsyncSession, migrated: str, work_through: Any
) -> None:
    """Two workers on one database: one holds a job, and the other looks after the
    queue meanwhile. The job runs once, to its end."""
    started, let_go = asyncio.Event(), asyncio.Event()
    runs: list[bool] = []

    async def holds_on() -> None:
        runs.append(True)
        started.set()
        await let_go.wait()

    looks = LooksAfter()
    await jobs.defer(db, "hold")
    await db.commit()
    holding = asyncio.create_task(
        jobs.work([jobs.Job("hold", holds_on)], database_url=migrated, on_signals=False)
    )
    try:
        async with asyncio.timeout(10):
            await started.wait()
        await jobs.defer(db, "look")
        await db.commit()
        await work_through(jobs.Job("look", looks, with_queue=True))
        while_held = await queue_of(migrated)
        let_go.set()
        async with asyncio.timeout(10):
            while await queue_of(migrated):  # noqa: ASYNC110 - the queue is in the database
                await asyncio.sleep(0.05)
    finally:
        let_go.set()  # if the test has failed before: the job in hand ends, and so can its worker
        holding.cancel()
        await asyncio.wait([holding], timeout=10)

    assert looks.looks == [jobs.Upkeep(run_again=0, given_up=(), dead_workers=0)]
    assert while_held == [("hold", "doing")]
    assert runs == [True]


@pytest.mark.parametrize("its_worker", ["silent for a minute", "off the list"])
@pytest.mark.anyio
async def test_a_look_after_the_queue_neither_runs_itself_again_nor_takes_its_worker_for_dead(
    its_worker: str, db: AsyncSession, migrated: str, work_through: Any
) -> None:
    """As after the machine slept: the worker that looks hasn't said for a minute that
    it is alive, or another worker has taken it off the list meanwhile. Either way the
    job that looks is `doing` under a worker Procrastinate takes for dead, or under
    none, and Procrastinate finds it among the jobs that dead workers left."""

    async def lose_its_worker() -> None:
        async with await psycopg.AsyncConnection.connect(migrated) as connection:
            if its_worker == "silent for a minute":
                await connection.execute(
                    "UPDATE procrastinate_workers SET last_heartbeat = now() - interval '1 minute'"
                )
            else:
                await connection.execute("DELETE FROM procrastinate_workers")

    looks = LooksAfter(first=lose_its_worker)
    await jobs.defer(db, "look")
    await db.commit()

    await work_through(jobs.Job("look", looks, with_queue=True))

    assert looks.looks == [jobs.Upkeep(run_again=0, given_up=(), dead_workers=0)]
    assert await queue_of(migrated) == []  # it ran once, succeeded, and was deleted


@pytest.mark.parametrize(
    "run_again_before", [0, jobs.RUNS_AGAIN], ids=["to be run again", "to be given up on"]
)
@pytest.mark.anyio
async def test_a_job_that_ends_while_the_queue_is_looked_after_is_skipped_and_the_look_goes_on(
    run_again_before: int, db: AsyncSession, migrated: str, queue_books: Any
) -> None:
    """Its worker was only late, and ends the job between the moment the look finds it
    and the moment the look puts it back, or ends it. Here the test holds the job's
    row, so that the look waits for it, then deletes the job, as its worker does with a
    job that succeeded, and lets go. The other job, which a worker that did die left,
    is run again all the same."""
    noted = Noted()
    await queue_books.deferred(db, "look")
    ends = await queue_books.deferred(db, "note", n=1)
    await queue_books.already_run_again(ends, run_again_before)
    left = await queue_books.deferred(db, "note", n=2)

    async with await psycopg.AsyncConnection.connect(migrated) as holder:

        async def leave_both_and_hold_one() -> None:
            await queue_books.left_doing(ends)
            await queue_books.left_doing(left)
            await holder.execute(
                "SELECT id FROM procrastinate_jobs WHERE id = %s FOR UPDATE", [ends]
            )

        looks = LooksAfter(first=leave_both_and_hold_one)
        worker = asyncio.create_task(
            jobs.work(
                [jobs.Job("note", noted), jobs.Job("look", looks, with_queue=True)],
                database_url=migrated,
                grace=1,
                on_signals=False,
            )
        )
        try:
            async with asyncio.timeout(10):
                # The look waits inside the database: nothing in the test can tell.
                while not await someone_waits_for_a_lock(migrated):  # noqa: ASYNC110
                    await asyncio.sleep(0.05)
            await holder.execute("DELETE FROM procrastinate_jobs WHERE id = %s", [ends])
            await holder.commit()
            await until(lambda: noted.runs == [{"n": 2}], within=15)
        finally:
            worker.cancel()
            await asyncio.wait([worker], timeout=10)

    assert looks.looks == [jobs.Upkeep(run_again=1, given_up=(), dead_workers=2)]
    assert await queue_of(migrated) == []


@pytest.mark.anyio
async def test_a_look_that_the_database_fails_is_a_job_that_failed_with_an_error_of_this_module(
    db: AsyncSession, migrated: str, queue_books: Any
) -> None:
    """Not every refusal means that a job ended meanwhile. Here the job a dead worker
    left is held by another transaction, and the worker's own connections give up on a
    lock after a fifth of a second: putting the job back fails with the database's
    error, not with Procrastinate's refusal. That fails the look, as a job, with an
    error that names nothing of Procrastinate, and the job stays as it was left."""
    await queue_books.deferred(db, "look")
    left = await queue_books.deferred(db, "note", n=1)
    raised: list[BaseException] = []
    noted = Noted()

    async with await psycopg.AsyncConnection.connect(migrated) as holder:

        async def looks(queue: jobs.Queue) -> None:
            # Under no worker: taking a dead worker off the list would wait for the same
            # row, and fail the look by itself, whatever became of the job's own error.
            await queue_books.left_doing(left, worker_gone=True)
            await holder.execute(
                "SELECT id FROM procrastinate_jobs WHERE id = %s FOR UPDATE", [left]
            )
            try:
                await queue.look_after(kept_for=timedelta(days=7))
            except Exception as error:
                raised.append(error)
                raise

        impatient = psycopg.conninfo.make_conninfo(migrated, options="-c lock_timeout=200ms")
        async with asyncio.timeout(20):
            await jobs.work(
                [jobs.Job("note", noted), jobs.Job("look", looks, with_queue=True)],
                database_url=impatient,
                until_idle=True,
                on_signals=False,
            )
        await holder.rollback()

    assert [type(error) for error in raised] == [jobs.UpkeepError]
    assert "procrastinate" not in type(raised[0]).__module__
    assert noted.runs == []
    assert await queue_of(migrated) == [("look", "failed"), ("note", "doing")]


def test_a_worker_that_is_stopping_is_not_taken_for_dead_before_its_grace_is_over() -> None:
    """A worker that is told to stop says no more that it is alive, from that moment,
    while the job in hand has its grace. Its last beat may be one beat old by then. If
    the two together reached the time after which a worker counts as dead, another
    worker's look could put that job back while it is still being finished."""
    assert jobs.BEATS_EVERY_S + jobs.SHUTDOWN_GRACE_S < jobs.WORKER_DEAD_AFTER_S


@pytest.mark.anyio
async def test_a_look_after_the_queue_deletes_what_ended_without_succeeding_once_it_is_old(
    db: AsyncSession, migrated: str, queue_books: Any, work_through: Any
) -> None:
    """Kept for a week here: what ended eight days ago goes, with its events, and what
    ended six days ago stays, whether it failed, was cut off for the last time, or was
    cancelled."""
    old: list[int] = []
    young: list[int] = []
    for status in ("failed", "aborted", "cancelled"):
        for ago, kind in ((timedelta(days=8), old), (timedelta(days=6), young)):
            job_id = await queue_books.deferred(db, f"{status}, {ago.days} days ago")
            await queue_books.ended(job_id, status, ago=ago)
            kind.append(job_id)
    looks = LooksAfter(kept_for=timedelta(days=7))
    await jobs.defer(db, "look")
    await db.commit()

    await work_through(jobs.Job("look", looks, with_queue=True))

    assert await queue_of(migrated) == [
        ("failed, 6 days ago", "failed"),
        ("aborted, 6 days ago", "aborted"),
        ("cancelled, 6 days ago", "cancelled"),
    ]
    assert await queue_books.events_of(*old) == 0
    assert all([await queue_books.events_of(job_id) > 0 for job_id in young])


@pytest.mark.anyio
async def test_a_look_after_the_queue_takes_the_dead_workers_off_the_list_and_no_live_one(
    db: AsyncSession, queue_books: Any, work_through: Any
) -> None:
    """Two workers that have been silent for a minute, one that has just said that it
    is alive, and the one that looks, which goes off the list itself when it stops."""
    live: list[int] = []

    async def two_dead_and_one_alive() -> None:
        await queue_books.a_dead_worker()
        await queue_books.a_dead_worker()
        live.append(await queue_books.a_live_worker())

    looks = LooksAfter(first=two_dead_and_one_alive)
    await jobs.defer(db, "look")
    await db.commit()

    await work_through(jobs.Job("look", looks, with_queue=True))

    assert looks.looks == [jobs.Upkeep(run_again=0, given_up=(), dead_workers=2)]
    assert await queue_books.workers() == live
