"""Background jobs, against a real Postgres: deferred in the transaction of a change, and
run by a worker in this process.

A test stops a worker by cancelling it, which ends it the way the process's signal
does. The real process, and its signal, are in `test_worker.py`.
"""

import asyncio
import logging
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
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
async def test_a_job_that_outlasts_the_grace_is_cut_off_and_kept_as_aborted(
    db: AsyncSession, migrated: str
) -> None:
    started = asyncio.Event()
    cut_off: list[bool] = []

    async def never_ends() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cut_off.append(True)
            raise

    await jobs.defer(db, "endless")
    await db.commit()
    worker = asyncio.create_task(
        jobs.work(
            [jobs.Job("endless", never_ends)], database_url=migrated, grace=0.5, on_signals=False
        )
    )
    async with asyncio.timeout(10):
        await started.wait()

    told = time.monotonic()
    worker.cancel()
    await asyncio.wait([worker], timeout=10)
    took = time.monotonic() - told

    assert worker.cancelled()
    assert cut_off == [True]
    assert 0.45 < took < 2.0
    assert await queue_of(migrated) == [("endless", "aborted")]


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
