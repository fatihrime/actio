"""The worker process: the jobs it runs, and the process itself, started as it is in a
deployment and told to stop."""

import asyncio
import dataclasses
import logging
import os
import signal
import sys
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
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
    told: signal.Signals, db: AsyncSession, migrated: str
) -> None:
    """Started as a deployment starts it, and stopped as Docker stops it, or as Ctrl-C
    does. Nobody defers the job: it is the sweep, which the worker's own schedule puts
    in the queue when it starts. What it finds is a guest whose day ended yesterday, by
    the machine's clock: the one the process reads."""
    await sessions.start_guest_session(db, now=datetime.now(UTC) - timedelta(days=2))
    await db.commit()
    environment = os.environ | {"ACTIO_DATABASE_URL": migrated, "ACTIO_VALKEY_URL": NOWHERE_VALKEY}

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


async def left_in(database_url: str) -> tuple[int, int]:
    """How many guests are in the database, and how many jobs in the queue."""
    async with await psycopg.AsyncConnection.connect(database_url) as connection:
        cursor = await connection.execute(
            "SELECT (SELECT count(*) FROM users), (SELECT count(*) FROM procrastinate_jobs)"
        )
        row = await cursor.fetchone()
        assert row is not None
        return row[0], row[1]
