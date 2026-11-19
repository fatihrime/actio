"""The worker process: the jobs it runs, and the process itself, started as it is in a
deployment and told to stop. And the worker inside the API's process (`run_inside`):
what it does when it can't work, and when it is told to stop."""

import asyncio
import dataclasses
import itertools
import logging
import os
import signal
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from actio import worker
from actio.core import jobs
from actio.core.resources import open_resources
from actio.core.settings import Settings
from actio.identity import accounts, sessions
from actio.identity.models import BrowserSession, User
from actio.identity.passwords import Passwords
from actio.identity.sessions import ACCOUNT_IDLE_LIFETIME, GUEST_LIFETIME

pytestmark = pytest.mark.anyio

NOWHERE_VALKEY = "valkey://127.0.0.1:1/0"  # required by the settings; the worker never uses it
NOWHERE_DATABASE = "postgresql://actio:unused@127.0.0.1:1/actio"  # nothing listens on port 1


def the_sweep(listed: list[jobs.Job]) -> jobs.Job:
    [sweep] = [job for job in listed if job.name == worker.SWEEP]
    return sweep


async def test_the_sweep_runs_every_ten_minutes(settings: Settings, clock: Any) -> None:
    async with open_resources(settings) as resources:
        sweep = the_sweep(worker.listed(resources, clock=lambda: clock.now))

    assert sweep.every == "*/10 * * * *"


async def test_the_sweep_deletes_what_expired_by_the_clock_it_was_given(
    db: AsyncSession,
    settings: Settings,
    clock: Any,
    passwords: Passwords,
    work_through: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The worker's clock is a century ahead here. By it the first guest's day is over,
    and by the machine's it is still to come."""
    clock.now = datetime(2126, 11, 10, 12, 0, tzinfo=UTC)
    await sessions.start_guest_session(db, now=clock.now - GUEST_LIFETIME)
    _, alive = await sessions.start_guest_session(db, now=clock.now - timedelta(hours=1))
    idle_since = clock.now - ACCOUNT_IDLE_LIFETIME - timedelta(days=1)
    _, account = await accounts.sign_up(
        db,
        passwords,
        email="deniz@example.com",
        password="correct horse battery staple",
        current=None,
        now=idle_since,
    )
    await jobs.defer(db, worker.SWEEP)
    await db.commit()
    caplog.set_level(logging.INFO, logger="actio.worker")

    async with open_resources(settings) as resources:
        sweep = the_sweep(worker.listed(resources, clock=lambda: clock.now))
        # Run when it was deferred only, not by its schedule as well: that is the test above.
        await work_through(dataclasses.replace(sweep, every=None))

    users = set((await db.scalars(select(User.id))).all())
    assert users == {alive.user_id, account.user_id}
    session_ids = set((await db.scalars(select(BrowserSession.id))).all())
    assert session_ids == {alive.session_id}
    said = [record.getMessage() for record in caplog.records if record.name == "actio.worker"]
    assert said == ["deleted expired guests: 1, expired sessions: 1"]


async def test_a_sweep_that_finds_nothing_says_nothing(
    db: AsyncSession, settings: Settings, clock: Any, caplog: pytest.LogCaptureFixture
) -> None:
    _, guest = await sessions.start_guest_session(db, now=clock.now)
    await db.commit()
    caplog.set_level(logging.INFO, logger="actio.worker")

    async with open_resources(settings) as resources:
        await the_sweep(worker.listed(resources, clock=lambda: clock.now)).run()

    assert (await db.scalars(select(User.id))).all() == [guest.user_id]
    assert [record for record in caplog.records if record.name == "actio.worker"] == []


@pytest.mark.parametrize("told", [signal.SIGTERM, signal.SIGINT], ids=["SIGTERM", "SIGINT"])
async def test_the_worker_process_sweeps_by_its_schedule_and_ends_well_when_told_to_stop(
    told: signal.Signals, db: AsyncSession, migrated: str, tmp_path: Path
) -> None:
    """Started as a deployment starts it, and stopped as Docker stops it, or as Ctrl-C
    does. Nobody defers the job: it is the sweep, which the worker's own schedule puts
    in the queue when it starts. What it finds is a guest whose day ended yesterday, by
    the machine's clock: the one the process reads.

    While it works, its health check, run as Docker runs it, finds it working."""
    await sessions.start_guest_session(db, now=datetime.now(UTC) - timedelta(days=2))
    await db.commit()
    alive_file = tmp_path / "alive"
    environment = os.environ | {
        "ACTIO_DATABASE_URL": migrated,
        "ACTIO_VALKEY_URL": NOWHERE_VALKEY,
        "ACTIO_ALIVE_FILE": str(alive_file),
    }

    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "actio",
        "--role",
        "worker",
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        # Until the guest is gone, and the job too: it succeeded.
        async with asyncio.timeout(30):
            while await left_in(migrated) != (0, 0):
                assert process.returncode is None, await process.communicate()
                await asyncio.sleep(0.1)
        health = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "actio.core.alive",
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        async with asyncio.timeout(10):
            checked = await health.communicate()
        process.send_signal(told)
        async with asyncio.timeout(10):
            out, err = await process.communicate()
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()

    log = err.decode()
    assert process.returncode == 0, log
    assert out == b""
    assert "Traceback" not in log
    # The log is set up by the process itself: one line a record, at the info level.
    assert " INFO actio.worker: deleted expired guests: 1, expired sessions: 0\n" in log
    assert f" INFO actio.core.jobs: told to stop ({told.name})\n" in log
    # It read which migration the database is at, and ran none: nothing of Alembic's.
    assert "alembic" not in log
    assert (health.returncode, checked) == (0, (b"", b""))
    assert alive_file.is_file()


async def test_the_worker_touches_its_file_only_when_its_settings_name_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run by hand, it is told of no file, and writes none. Here nothing connects: the
    worker itself is replaced by one that notes what it was given."""
    given: list[dict[str, Any]] = []

    async def work(_listed: list[jobs.Job], **options: Any) -> None:
        given.append(options)

    monkeypatch.setattr(jobs, "work", work)
    database, valkey = SecretStr(NOWHERE_DATABASE), SecretStr(NOWHERE_VALKEY)
    alive_file = tmp_path / "alive"

    await worker.run(Settings(database_url=database, valkey_url=valkey))
    await worker.run(Settings(database_url=database, valkey_url=valkey, alive_file=alive_file))

    by_hand, in_a_container = given
    assert by_hand["on_beat"] is None
    in_a_container["on_beat"]()
    assert alive_file.is_file()


async def test_a_worker_that_cant_ask_the_database_for_its_migration_says_so_in_one_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The database went away again in the moment after the worker had connected. Here
    nothing ever listened: the worker itself is replaced by one that only makes the
    check it was given. What comes out is the error the command line ends on with one
    line, in the driver's own words, without the line SQLAlchemy adds about itself."""

    async def work(_listed: list[jobs.Job], **options: Any) -> None:
        await options["check"]()

    monkeypatch.setattr(jobs, "work", work)
    nowhere = Settings(
        database_url=SecretStr(NOWHERE_DATABASE), valkey_url=SecretStr(NOWHERE_VALKEY)
    )

    with pytest.raises(jobs.DatabaseUnreachableError) as refused:
        await worker.run(nowhere)

    said = str(refused.value)
    assert said.startswith("couldn't read the database's migration: connection failed")
    assert "\n" not in said
    assert "sqlalche.me" not in said


async def test_the_worker_process_told_to_stop_while_it_connects_ends_well() -> None:
    """Nothing listens where its settings point, so its pool is still trying to connect,
    as it would for 30 seconds. A SIGTERM ends it at once, as it does at any other time."""
    environment = os.environ | {
        "ACTIO_DATABASE_URL": NOWHERE_DATABASE,
        "ACTIO_VALKEY_URL": NOWHERE_VALKEY,
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "actio",
        "--role",
        "worker",
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    said: list[bytes] = []
    try:
        assert process.stderr is not None
        # The pool logs each try that fails. By the first one, the worker listens.
        async with asyncio.timeout(15):
            async for line in process.stderr:
                said.append(line)
                if b"error connecting" in line:
                    break
            else:
                pytest.fail(f"it ended before it tried to connect:\n{b''.join(said).decode()}")
        told = time.monotonic()
        process.send_signal(signal.SIGTERM)
        async with asyncio.timeout(10):
            out, err = await process.communicate()
        took = time.monotonic() - told
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()

    log = (b"".join(said) + err).decode()
    assert process.returncode == 0, log
    assert took < 3, log
    assert out == b""
    assert "Traceback" not in log


def nowhere(**more: Any) -> Settings:
    """Settings whose database and Valkey are nowhere: in the tests that take them,
    nothing connects."""
    return Settings(
        database_url=SecretStr(NOWHERE_DATABASE), valkey_url=SecretStr(NOWHERE_VALKEY), **more
    )


async def until(done: Callable[[], bool], *, within: float) -> None:
    """Wait until `done()` holds, looking every hundredth of a second. The test fails
    after `within` seconds."""
    async with asyncio.timeout(within):
        while not done():  # noqa: ASYNC110 - what is waited for is noted by a stand-in
            await asyncio.sleep(0.01)


def said_inside(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """What the worker inside the API said about itself."""
    return [record for record in caplog.records if record.name == "actio.worker"]


async def test_inside_the_api_the_worker_leaves_the_signals_alone_and_touches_its_file_as_ever(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whoever runs it hears the signals. The worker itself is replaced by one that
    notes what it was given."""
    given: list[dict[str, Any]] = []

    async def work(_listed: list[jobs.Job], **options: Any) -> None:
        given.append(options)

    monkeypatch.setattr(jobs, "work", work)
    alive_file = tmp_path / "alive"

    async with open_resources(nowhere()) as resources, asyncio.timeout(5):
        await worker.run_inside(nowhere(), resources)
        await worker.run_inside(nowhere(alive_file=alive_file), resources)

    by_hand, in_a_container = given
    assert by_hand["on_signals"] is False
    assert by_hand["on_beat"] is None
    in_a_container["on_beat"]()
    assert alive_file.is_file()


@pytest.mark.parametrize(
    "error",
    [
        jobs.DatabaseUnreachableError(
            "couldn't connect to the database: pool initialization incomplete after 30.0 sec"
        ),
        worker.NotMigratedError(
            'the database was never migrated, and this code needs 0009: run "actio --role'
            ' migrate" first'
        ),
    ],
    ids=["unreachable", "not migrated"],
)
async def test_inside_the_api_a_worker_that_cant_work_says_why_in_one_line_each_time_and_tries_again(
    error: Exception, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Its process serves the API, which stays up: nothing would start the worker again
    if it ended. It can't work twice, and works the third time. The worker itself is
    replaced by one that does so."""
    monkeypatch.setattr(worker, "TRIES_AGAIN_AFTER_S", 0.1)
    tries: list[float] = []

    async def work(_listed: list[jobs.Job], **_options: Any) -> None:
        tries.append(time.monotonic())
        if len(tries) <= 2:
            raise error
        await asyncio.Event().wait()  # it works, until it is told to stop

    monkeypatch.setattr(jobs, "work", work)
    async with open_resources(nowhere()) as resources:
        inside = asyncio.create_task(worker.run_inside(nowhere(), resources))
        try:
            await until(lambda: len(tries) == 3, within=5)
        finally:
            inside.cancel()
            await asyncio.wait([inside], timeout=5)

    said = said_inside(caplog)
    assert [record.getMessage() for record in said] == [
        f"The worker can't work: {error}. It tries again in 0.1 s."
    ] * 2
    assert [(record.levelno, record.exc_info) for record in said] == [(logging.ERROR, None)] * 2
    assert all(later - earlier >= 0.1 for earlier, later in itertools.pairwise(tries))
    assert inside.cancelled()  # it ended as it was told, by the cancellation


async def test_inside_the_api_a_worker_that_ends_with_another_error_says_it_with_its_trace_and_starts_again(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(worker, "TRIES_AGAIN_AFTER_S", 0.1)
    broken = RuntimeError("something nobody expected")
    tries: list[float] = []

    async def work(_listed: list[jobs.Job], **_options: Any) -> None:
        tries.append(time.monotonic())
        if len(tries) == 1:
            raise broken
        await asyncio.Event().wait()

    monkeypatch.setattr(jobs, "work", work)
    async with open_resources(nowhere()) as resources:
        inside = asyncio.create_task(worker.run_inside(nowhere(), resources))
        try:
            await until(lambda: len(tries) == 2, within=5)
        finally:
            inside.cancel()
            await asyncio.wait([inside], timeout=5)

    [said] = said_inside(caplog)
    assert said.levelno == logging.ERROR
    assert said.getMessage() == "The worker ended with an error. It starts again in 0.1 s."
    assert said.exc_info is not None
    assert said.exc_info[1] is broken
    assert tries[1] - tries[0] >= 0.1
    assert inside.cancelled()


async def test_inside_the_api_a_worker_told_to_stop_while_it_waits_to_try_again_ends_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Its pause is an hour here."""
    monkeypatch.setattr(worker, "TRIES_AGAIN_AFTER_S", 3600.0)
    tries: list[bool] = []

    async def work(_listed: list[jobs.Job], **_options: Any) -> None:
        tries.append(True)
        raise worker.NotMigratedError("the database was never migrated")

    monkeypatch.setattr(jobs, "work", work)
    async with open_resources(nowhere()) as resources:
        inside = asyncio.create_task(worker.run_inside(nowhere(), resources))
        await until(lambda: bool(tries), within=5)
        await asyncio.sleep(0.05)  # in its pause
        told = time.monotonic()
        inside.cancel()
        await asyncio.wait([inside], timeout=5)
        took = time.monotonic() - told

    assert inside.cancelled()
    assert took < 0.5
    assert tries == [True]


@pytest.mark.parametrize(
    ("instead", "says", "with_trace"),
    [
        (
            jobs.DatabaseUnreachableError("couldn't read the database's migration"),
            "The worker can't work: couldn't read the database's migration.",
            False,
        ),
        (
            RuntimeError("couldn't take the worker off the list"),
            "The worker ended with an error.",
            True,
        ),
    ],
    ids=["can't work", "another error"],
)
async def test_inside_the_api_a_worker_whose_stop_ends_in_an_error_is_not_started_again(
    instead: Exception,
    says: str,
    with_trace: bool,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Told to stop while its database is away, Procrastinate's worker waits for a
    connection to take itself off its list, and then raises its own error in place of
    the cancellation. The worker was told to stop all the same: it says what happened,
    and ends. Started again, it would work on, and nothing would tell it to stop again:
    its process would wait for it for ever.

    The worker itself is replaced by one that fails so when it is cancelled: the first
    time it runs only, so that one that was started again can still be ended, and the
    test fails instead of hanging."""
    monkeypatch.setattr(worker, "TRIES_AGAIN_AFTER_S", 0.05)
    tries: list[bool] = []
    working = asyncio.Event()

    async def work(_listed: list[jobs.Job], **_options: Any) -> None:
        tries.append(True)
        working.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if len(tries) > 1:
                raise
            raise instead from None

    monkeypatch.setattr(jobs, "work", work)
    async with open_resources(nowhere()) as resources:
        inside = asyncio.create_task(worker.run_inside(nowhere(), resources))
        try:
            async with asyncio.timeout(5):
                await working.wait()
            inside.cancel()
            ended, _ = await asyncio.wait([inside], timeout=2)
        finally:
            if not inside.done():  # the test has failed: leave nothing running
                inside.cancel()
                await asyncio.wait([inside], timeout=5)

    assert ended == {inside}
    assert tries == [True]
    assert inside.cancelled() or inside.exception() is None  # nothing left to retrieve
    [said] = said_inside(caplog)
    assert said.getMessage() == says
    assert (said.exc_info is not None) is with_trace


async def left_in(database_url: str) -> tuple[int, int]:
    """How many guests are in the database, and how many jobs in the queue."""
    async with await psycopg.AsyncConnection.connect(database_url) as connection:
        cursor = await connection.execute(
            "SELECT (SELECT count(*) FROM users), (SELECT count(*) FROM procrastinate_jobs)"
        )
        row = await cursor.fetchone()
        assert row is not None
        return row[0], row[1]
