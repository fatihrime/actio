"""The worker process: the jobs it runs, and the process itself, started as it is in a
deployment and told to stop. And the worker inside the API's process (`run_inside`):
whom it tells that it works and that it stopped, what it does when it can't work, and
when it is told to stop."""

import asyncio
import contextlib
import dataclasses
import itertools
import logging
import os
import signal
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

import psycopg
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from valkey.exceptions import ConnectionError as ValkeyConnectionError

from actio import worker
from actio.chat import turns
from actio.chat.models import Conversation, Message, Turn
from actio.chat.parts import ReasoningPart, TextPart
from actio.chat.turns import STALE_AFTER, Draft
from actio.core import jobs
from actio.core.ids import new_id
from actio.core.resources import open_resources
from actio.core.settings import Settings
from actio.identity import accounts, sessions
from actio.identity.models import BrowserSession, User
from actio.identity.passwords import Passwords
from actio.identity.sessions import ACCOUNT_IDLE_LIFETIME, GUEST_LIFETIME
from actio.usage import ledger
from actio.usage.limits import Account, UsageLimits
from actio.usage.missed import COUNTED_WITHIN
from actio.usage.models import Uncounted, UsageEntry
from actio_llm import Finish, FinishReason, TextDelta, Usage
from actio_quota import Usage as Tokens
from actio_quota.engine import ValkeyQuota

pytestmark = pytest.mark.anyio

# Required by the settings, and nothing listens on port 1. The worker asks Valkey only
# of usage its counters missed: where there is none, it never connects.
NOWHERE_VALKEY = "valkey://127.0.0.1:1/0"
NOWHERE_DATABASE = "postgresql://actio:unused@127.0.0.1:1/actio"  # nothing listens on port 1


def the_sweep(listed: list[jobs.Job]) -> jobs.Job:
    [sweep] = [job for job in listed if job.name == worker.SWEEP]
    return sweep


def giving_up_on_stale_turns(listed: list[jobs.Job]) -> jobs.Job:
    [giving_up] = [job for job in listed if job.name == worker.GIVE_UP]
    return giving_up


def counting_missed_usage(listed: list[jobs.Job]) -> jobs.Job:
    [counting] = [job for job in listed if job.name == worker.COUNT_MISSED]
    return counting


def looking_after_the_queue(listed: list[jobs.Job]) -> jobs.Job:
    [looking] = [job for job in listed if job.name == worker.LOOK_AFTER_QUEUE]
    return looking


def first_then(
    first: Callable[[], Awaitable[None]], looking: jobs.Job
) -> Callable[[jobs.Queue], Awaitable[None]]:
    """What runs `looking`, run only when it is deferred, after `first`.

    A worker takes the workers that died off the queue's list when it starts, so what a
    dead worker leaves is written while the worker that looks runs: by `first`."""

    async def run(queue: jobs.Queue) -> None:
        await first()
        await looking.run(queue)

    return run


async def does_nothing(**_args: jobs.Argument) -> None:
    """A job's handler, for a job that has to be listed."""


async def queue_of(database_url: str) -> list[tuple[str, str]]:
    """The jobs in the queue, oldest first: each one's name and status."""
    async with await psycopg.AsyncConnection.connect(database_url) as connection:
        cursor = await connection.execute(
            "SELECT task_name, status::text FROM procrastinate_jobs ORDER BY id"
        )
        return [(name, status) for name, status in await cursor.fetchall()]


async def a_turn(db: AsyncSession, *, heard_from: datetime) -> Turn:
    """A new guest's question, with its turn running, begun and last heard from at
    `heard_from`. Not committed."""
    _, guest = await sessions.start_guest_session(db, now=heard_from)
    begun = await turns.ask(
        db,
        user_id=guest.user_id,
        conversation_id=new_id(),
        message_id=new_id(),
        parent_id=None,
        parts=[TextPart(text="Name a bike part.")],
        model="standard",
        thinking=False,
        turn_id=new_id(),
        now=heard_from,
    )
    return begun.turn


async def test_the_worker_runs_each_of_its_four_jobs_every_ten_minutes(
    settings: Settings, clock: Any
) -> None:
    """The last looks after the queue itself, and is given it."""
    async with open_resources(settings) as resources:
        listed = worker.listed(resources, clock=lambda: clock.now)

    assert [(job.name, job.every, job.with_queue) for job in listed] == [
        (worker.SWEEP, "*/10 * * * *", False),
        (worker.GIVE_UP, "*/10 * * * *", False),
        (worker.COUNT_MISSED, "*/10 * * * *", False),
        (worker.LOOK_AFTER_QUEUE, "*/10 * * * *", True),
    ]


async def test_the_worker_keeps_a_job_that_ended_without_succeeding_for_a_week(
    db: AsyncSession,
    settings: Settings,
    clock: Any,
    migrated: str,
    queue_books: Any,
    work_through: Any,
) -> None:
    """One failed an hour short of a week ago, and stays. The other an hour more than a
    week ago, and goes."""
    younger = await queue_books.deferred(db, "younger")
    await queue_books.ended(younger, "failed", ago=timedelta(days=7) - timedelta(hours=1))
    older = await queue_books.deferred(db, "older")
    await queue_books.ended(older, "failed", ago=timedelta(days=7, hours=1))
    await jobs.defer(db, worker.LOOK_AFTER_QUEUE)
    await db.commit()

    async with open_resources(settings) as resources:
        looking = looking_after_the_queue(worker.listed(resources, clock=lambda: clock.now))
        # Run when it was deferred only, not by its schedule as well.
        await work_through(dataclasses.replace(looking, every=None))

    assert timedelta(days=7) == worker.KEPT_FOR
    assert await queue_of(migrated) == [("younger", "failed")]


async def test_the_worker_says_how_many_jobs_it_ran_again_and_how_many_dead_workers_went(
    db: AsyncSession,
    settings: Settings,
    clock: Any,
    queue_books: Any,
    work_through: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A worker that was killed left a job `doing`. The job that looks comes first in
    the queue."""
    await queue_books.deferred(db, worker.LOOK_AFTER_QUEUE)
    left = await queue_books.deferred(db, "note", n=1)
    caplog.set_level(logging.INFO, logger="actio.worker")

    async with open_resources(settings) as resources:
        looking = looking_after_the_queue(worker.listed(resources, clock=lambda: clock.now))
        leave_it = partial(queue_books.left_doing, left)
        await work_through(
            dataclasses.replace(looking, run=first_then(leave_it, looking), every=None),
            jobs.Job("note", does_nothing),
        )

    said = [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if record.name == "actio.worker"
    ]
    assert said == [
        (
            logging.INFO,
            "jobs left by workers that died, run again: 1, dead workers taken off the list: 1",
        )
    ]


@pytest.mark.parametrize(
    ("left_a_job", "said"),
    [
        (True, "jobs left by workers that died, run again: 1, dead workers taken off the list: 0"),
        (False, "jobs left by workers that died, run again: 0, dead workers taken off the list: 1"),
    ],
    ids=["only a job to run again", "only a dead worker"],
)
async def test_the_worker_says_so_when_a_look_did_only_one_of_the_two(
    left_a_job: bool,
    said: str,
    db: AsyncSession,
    settings: Settings,
    clock: Any,
    queue_books: Any,
    work_through: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Either is worth its line: a job run again whose worker another had already taken
    off the list, or a dead worker that had no job in hand."""
    await queue_books.deferred(db, worker.LOOK_AFTER_QUEUE)
    left = await queue_books.deferred(db, "note", n=1) if left_a_job else None
    caplog.set_level(logging.INFO, logger="actio.worker")

    async def leave_one_of_the_two() -> None:
        if left is None:
            await queue_books.a_dead_worker()
        else:
            await queue_books.left_doing(left, worker_gone=True)

    async with open_resources(settings) as resources:
        looking = looking_after_the_queue(worker.listed(resources, clock=lambda: clock.now))
        await work_through(
            dataclasses.replace(looking, run=first_then(leave_one_of_the_two, looking), every=None),
            jobs.Job("note", does_nothing),
        )

    lines = [record.getMessage() for record in caplog.records if record.name == "actio.worker"]
    assert lines == [said]


async def test_the_worker_names_each_job_it_gives_up_on(
    db: AsyncSession,
    settings: Settings,
    clock: Any,
    queue_books: Any,
    work_through: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Left `doing` by a worker that died, after it had been run again as many times as
    a job is. Its worker has been taken off the list since, by another, so that this is
    all there is to say."""
    await queue_books.deferred(db, worker.LOOK_AFTER_QUEUE)
    left = await queue_books.deferred(db, "note", n=1)
    await queue_books.already_run_again(left, jobs.RUNS_AGAIN)
    caplog.set_level(logging.INFO, logger="actio.worker")

    async with open_resources(settings) as resources:
        looking = looking_after_the_queue(worker.listed(resources, clock=lambda: clock.now))
        leave_it = partial(queue_books.left_doing, left, worker_gone=True)
        await work_through(
            dataclasses.replace(looking, run=first_then(leave_it, looking), every=None),
            jobs.Job("note", does_nothing),
        )

    said = [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if record.name == "actio.worker"
    ]
    assert said == [
        (
            logging.WARNING,
            "a job left by a worker that died had been run again 3 times already, and ends"
            f" as failed: note[{left}]",
        )
    ]


async def test_a_look_after_the_queue_that_finds_nothing_says_nothing(
    db: AsyncSession,
    settings: Settings,
    clock: Any,
    work_through: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await jobs.defer(db, worker.LOOK_AFTER_QUEUE)
    await db.commit()
    caplog.set_level(logging.INFO, logger="actio.worker")

    async with open_resources(settings) as resources:
        looking = looking_after_the_queue(worker.listed(resources, clock=lambda: clock.now))
        await work_through(dataclasses.replace(looking, every=None))

    assert [record for record in caplog.records if record.name == "actio.worker"] == []


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


async def test_the_worker_gives_up_on_stale_turns_by_the_clock_it_was_given(
    db: AsyncSession,
    settings: Settings,
    clock: Any,
    work_through: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The worker's clock is a century ahead here. By it one turn has been silent for a
    minute, and by the machine's it hasn't begun. The other was heard from 10 seconds
    ago. Nobody looks at either conversation."""
    clock.now = datetime(2126, 11, 10, 12, 0, tzinfo=UTC)
    silent = await a_turn(db, heard_from=clock.now - STALE_AFTER)
    heard_from = await a_turn(db, heard_from=clock.now - timedelta(seconds=10))
    await jobs.defer(db, worker.GIVE_UP)
    await db.commit()
    caplog.set_level(logging.INFO, logger="actio.worker")

    async with open_resources(settings) as resources:
        giving_up = giving_up_on_stale_turns(worker.listed(resources, clock=lambda: clock.now))
        # Run when it was deferred only, not by its schedule as well.
        await work_through(dataclasses.replace(giving_up, every=None))

    found = await db.execute(select(Turn.id, Turn.status, Turn.finished_at))
    assert {turn_id: (status, finished_at) for turn_id, status, finished_at in found} == {
        silent.id: ("interrupted", clock.now),
        heard_from.id: ("running", None),
    }
    said = [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if record.name == "actio.worker"
    ]
    assert said == [(logging.INFO, "gave up on stale turns: 1")]


async def test_the_worker_keeps_what_a_dead_process_had_written_by_its_last_beat(
    db: AsyncSession, settings: Settings, clock: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Two turns whose process died, in conversations nobody looks at. One had saved at
    its last beat what its answer held: it keeps that as its answer, dated at the beat,
    and its conversation shows it. The other had written nothing yet, and is given up on
    with no answer. Neither costs anything."""
    last_beat = clock.now - STALE_AFTER - timedelta(seconds=5)
    wrote = await a_turn(db, heard_from=last_beat - timedelta(seconds=15))
    draft = Draft(
        answer_id=new_id(),
        parts=[ReasoningPart(text="Something to sit on."), TextPart(text="The sad")],
    )
    await turns.still_alive(db, wrote.id, now=last_beat, draft=draft)
    silent_since = last_beat - timedelta(seconds=30)
    silent = await a_turn(db, heard_from=silent_since)
    await db.commit()
    caplog.set_level(logging.INFO, logger="actio.worker")

    async with open_resources(settings) as resources:
        await giving_up_on_stale_turns(worker.listed(resources, clock=lambda: clock.now)).run()

    await db.refresh(wrote)
    await db.refresh(silent)
    assert (wrote.status, wrote.answer_id, wrote.finished_at) == (
        "interrupted",
        draft.answer_id,
        clock.now,
    )
    assert (silent.status, silent.answer_id, silent.finished_at) == ("interrupted", None, clock.now)
    assert (await db.scalars(select(Turn.draft.is_(None)))).all() == [True, True]
    answer = await db.get(Message, draft.answer_id)
    assert answer is not None
    assert (answer.role, answer.parent_id, answer.created_at) == (
        "assistant",
        wrote.question_id,
        last_beat,
    )
    assert answer.parts == [
        {"type": "reasoning", "text": "Something to sit on."},
        {"type": "text", "text": "The sad"},
    ]
    shown = await db.execute(
        select(Conversation.id, Conversation.current_leaf_id, Conversation.last_active_at)
    )
    assert {conversation: (leaf, at) for conversation, leaf, at in shown} == {
        wrote.conversation_id: (draft.answer_id, last_beat),
        silent.conversation_id: (silent.question_id, silent_since),
    }
    assert await db.scalar(select(func.count()).select_from(UsageEntry)) == 0
    said = [record.getMessage() for record in caplog.records if record.name == "actio.worker"]
    assert said == ["gave up on stale turns: 2"]


async def test_a_worker_that_finds_no_stale_turn_says_nothing(
    db: AsyncSession, settings: Settings, clock: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """The turn has been silent for a second short of a minute."""
    await a_turn(db, heard_from=clock.now - STALE_AFTER + timedelta(seconds=1))
    await db.commit()
    caplog.set_level(logging.INFO, logger="actio.worker")

    async with open_resources(settings) as resources:
        await giving_up_on_stale_turns(worker.listed(resources, clock=lambda: clock.now)).run()

    assert (await db.scalars(select(Turn.status))).all() == ["running"]
    assert [record for record in caplog.records if record.name == "actio.worker"] == []


async def test_the_worker_counts_what_the_api_couldnt_under_the_keys_the_api_reads(
    scripted: Any, settings: Settings, clock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An answer is recorded, and its settle doesn't reach Valkey: for that moment the
    settle is one that fails as when Valkey has gone away. A minute later the worker's
    job, on resources of its own opened from the same settings, tells the counters. The
    app's own reading of the usage then shows the answer as used, and nothing held."""
    _, browser = await scripted(
        TextDelta("A derailleur."),
        Finish(FinishReason.STOP, Usage(input_tokens=40, output_tokens=3)),
    )
    question = {"id": str(new_id()), "parts": [{"type": "text", "text": "Name a bike part."}]}

    async def away(*_args: Any, **_kwargs: Any) -> None:
        raise ValkeyConnectionError("Connection refused")

    with monkeypatch.context() as patched:
        patched.setattr(ValkeyQuota, "settle", away)
        answered = await browser.post(
            f"/api/conversations/{new_id()}/turns", json={"message": question}
        )
    missed = (await browser.get("/api/usage")).json()["windows"]
    clock.advance(seconds=COUNTED_WITHIN.total_seconds())
    async with open_resources(settings) as resources:
        await counting_missed_usage(worker.listed(resources, clock=lambda: clock.now)).run()
    counted = (await browser.get("/api/usage")).json()["windows"]

    assert answered.status_code == 200
    assert [w["used"] for w in missed] == [0, 0]  # and its credits still held
    assert [(w["used"], w["reserved"]) for w in counted] == [(43, 0), (43, 0)]


async def test_the_worker_says_what_it_counted_that_the_counters_had_missed(
    db: AsyncSession,
    settings: Settings,
    clock: Any,
    plans_with: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Three rows of the ledger whose notes are a minute old: one the counters never
    heard of, one they had counted already, and one of an account they have no counters
    for. A second run finds nothing, and says nothing."""
    async with open_resources(settings) as resources:
        limits = UsageLimits(resources.quota, plans_with())
        guest = Account(new_id(), is_guest=True)
        never_heard_of, counted_already = [
            await limits.reserve(
                db,
                guest,
                new_id(),
                model="standard",
                prompt_tokens=10,
                max_output_tokens=100,
                hold_for=STALE_AFTER,
                now=clock.now,
            )
            for _ in range(2)
        ]
        for reserved in (never_heard_of, counted_already):
            limits.record(
                db, reserved, input_tokens=10, output_tokens=20, estimated=False, now=clock.now
            )
        ledger.add(
            db,
            reservation_id=new_id(),
            user_id=new_id(),
            model="standard",
            credits=30,
            tokens=Tokens(input_tokens=10, output_tokens=20),
            estimated=False,
            windows={},
            now=clock.now,
        )
        await db.commit()
        await limits.settle(counted_already, 30, now=clock.now)
        clock.advance(seconds=COUNTED_WITHIN.total_seconds())
        caplog.set_level(logging.INFO, logger="actio.worker")

        await counting_missed_usage(worker.listed(resources, clock=lambda: clock.now)).run()
        await counting_missed_usage(worker.listed(resources, clock=lambda: clock.now)).run()

    said = [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if record.name == "actio.worker"
    ]
    assert said == [
        (
            logging.INFO,
            "usage the counters had missed, counted now: 1, counted before: 1, "
            "left to their next rebuild: 1",
        )
    ]
    assert await db.scalar(select(func.count()).select_from(Uncounted)) == 0


async def test_a_worker_that_cant_reach_valkey_says_so_once_and_leaves_the_rows_for_its_next_run(
    db: AsyncSession, settings: Settings, clock: Any, caplog: pytest.LogCaptureFixture
) -> None:
    for _ in range(2):
        ledger.add(
            db,
            reservation_id=new_id(),
            user_id=new_id(),
            model="standard",
            credits=30,
            tokens=Tokens(input_tokens=10, output_tokens=20),
            estimated=False,
            windows={},
            now=clock.now - COUNTED_WITHIN,
        )
    await db.commit()
    caplog.set_level(logging.INFO, logger="actio.worker")
    cut_off = settings.model_copy(update={"valkey_url": SecretStr(NOWHERE_VALKEY)})

    async with open_resources(cut_off) as resources, asyncio.timeout(10):
        await counting_missed_usage(worker.listed(resources, clock=lambda: clock.now)).run()

    said = [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if record.name == "actio.worker"
    ]
    assert said == [
        (
            logging.WARNING,
            "usage the counters had missed: Valkey didn't answer, and what is left waits for "
            "the next run",
        )
    ]
    assert await db.scalar(select(func.count()).select_from(Uncounted)) == 2


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


async def test_the_worker_process_gives_up_on_a_stale_turn_by_its_schedule(
    db: AsyncSession, migrated: str
) -> None:
    """Started as a deployment starts it, on a database that holds a turn whose process
    died two minutes ago, by the machine's clock: the one the process reads. Nobody looks
    at its conversation, and nobody defers the job: the worker's own schedule puts it in
    the queue when it starts.

    The process is in a group of its own, which is ended as one, pass or fail."""
    turn = await a_turn(db, heard_from=datetime.now(UTC) - timedelta(minutes=2))
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
        start_new_session=True,
    )
    ended_well = False
    try:
        # Until the turn is given up on, and the queue is empty: every job succeeded.
        async with asyncio.timeout(30):
            while await turn_and_queue(migrated, turn.id) != ("interrupted", 0):
                assert process.returncode is None, await process.communicate()
                await asyncio.sleep(0.1)
        process.send_signal(signal.SIGTERM)
        async with asyncio.timeout(10):
            _, err = await process.communicate()
        ended_well = True
    finally:
        if not ended_well:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            async with asyncio.timeout(10):
                await process.communicate()

    log = err.decode()
    assert process.returncode == 0, log
    assert " INFO actio.worker: gave up on stale turns: 1\n" in log
    assert "Traceback" not in log


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


async def test_inside_the_api_a_beat_touches_the_file_and_tells_whoever_runs_the_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each half is done whatever the other does, and what failed is raised for
    `jobs.work` to say: a file that can't be touched, here a path through a file, and a
    caller that fails to hear. The worker itself is replaced by one that notes what it
    was given."""
    given: list[dict[str, Any]] = []

    async def work(_listed: list[jobs.Job], **options: Any) -> None:
        given.append(options)

    monkeypatch.setattr(jobs, "work", work)
    heard: list[str] = []

    def hear() -> None:
        heard.append("beat")

    def fail_to_hear() -> None:
        heard.append("beat, then failed")
        raise RuntimeError("couldn't note the beat")

    (tmp_path / "a-file").write_text("not a folder")
    alive_file, untouchable = tmp_path / "alive", tmp_path / "a-file" / "alive"

    async with open_resources(nowhere()) as resources, asyncio.timeout(5):
        await worker.run_inside(nowhere(alive_file=alive_file), resources, on_beat=hear)
        await worker.run_inside(nowhere(alive_file=untouchable), resources, on_beat=hear)
        await worker.run_inside(nowhere(alive_file=alive_file), resources, on_beat=fail_to_hear)

    both, cant_touch, cant_hear = (options["on_beat"] for options in given)
    both()
    assert alive_file.is_file()
    assert heard == ["beat"]

    with pytest.raises(NotADirectoryError):
        cant_touch()
    assert heard == ["beat", "beat"]

    alive_file.unlink()
    with pytest.raises(RuntimeError, match="couldn't note the beat"):
        cant_hear()
    assert heard == ["beat", "beat", "beat, then failed"]
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


async def test_inside_the_api_a_worker_that_ended_says_so_each_time_before_it_tries_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whoever runs it then forgets that it said it works. It can't work, ends with
    another error, and then works: it says that it stopped after each of the first two,
    before its pause, and neither while it works nor when it is told to stop. The worker
    itself is replaced by one that does so."""
    monkeypatch.setattr(worker, "TRIES_AGAIN_AFTER_S", 0.1)
    happened: list[tuple[str, float]] = []
    working = asyncio.Event()

    async def work(_listed: list[jobs.Job], **_options: Any) -> None:
        happened.append(("tried", time.monotonic()))
        tries = [what for what, _ in happened if what == "tried"]
        if len(tries) == 1:
            raise worker.NotMigratedError("the database was never migrated")
        if len(tries) == 2:
            raise RuntimeError("something nobody expected")
        working.set()
        await asyncio.Event().wait()  # it works, until it is told to stop

    def stopped() -> None:
        happened.append(("stopped", time.monotonic()))

    monkeypatch.setattr(jobs, "work", work)
    async with open_resources(nowhere()) as resources:
        inside = asyncio.create_task(worker.run_inside(nowhere(), resources, on_stopped=stopped))
        try:
            async with asyncio.timeout(5):
                await working.wait()
            await asyncio.sleep(0.3)  # three pauses' worth, while it works
        finally:
            inside.cancel()
            await asyncio.wait([inside], timeout=5)

    assert [what for what, _ in happened] == ["tried", "stopped", "tried", "stopped", "tried"]
    # Each said before the pause, not after it.
    at = [when for _, when in happened]
    assert at[2] - at[1] >= 0.1
    assert at[4] - at[3] >= 0.1
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
    and that it stopped, and ends. Started again, it would work on, and nothing would
    tell it to stop again: its process would wait for it for ever.

    The worker itself is replaced by one that fails so when it is cancelled: the first
    time it runs only, so that one that was started again can still be ended, and the
    test fails instead of hanging."""
    monkeypatch.setattr(worker, "TRIES_AGAIN_AFTER_S", 0.05)
    tries: list[bool] = []
    stopped: list[bool] = []
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
        inside = asyncio.create_task(
            worker.run_inside(nowhere(), resources, on_stopped=lambda: stopped.append(True))
        )
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
    assert stopped == [True]
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


async def turn_and_queue(database_url: str, turn_id: uuid.UUID) -> tuple[str, int]:
    """The turn's status, and how many jobs are in the queue."""
    async with await psycopg.AsyncConnection.connect(database_url) as connection:
        cursor = await connection.execute(
            "SELECT (SELECT status FROM turns WHERE id = %s),"
            " (SELECT count(*) FROM procrastinate_jobs)",
            [turn_id],
        )
        row = await cursor.fetchone()
        assert row is not None
        return row[0], row[1]
