"""The API with the worker inside it (`actio --role all`), against a real Postgres.

An app created with the worker runs the jobs, on the app's own resources, and one
created without runs none. Shut down, it waits for the job in hand. Told to stop by a
signal, the worker stops at once, while the app still serves, and is told only once. On
a database that isn't migrated yet, the app serves, and its worker starts once it is.
And the process itself, started as a deployment starts it, and told to stop.

The worker of an app here runs jobs of the test's own, in place of the real ones
(`lists`), except in the process.
"""

import asyncio
import contextlib
import logging
import multiprocessing
import os
import re
import signal
import socket
import sys
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import FrameType
from typing import Any, Protocol

import httpx2
import psycopg
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import actio.app
from actio import worker
from actio.chat.running import TurnRunner
from actio.core import jobs, schema
from actio.core.database import create_engine, create_session_factory
from actio.core.resources import Resources, open_resources
from actio.core.settings import Settings
from actio.identity import sessions

pytestmark = pytest.mark.anyio

CONFIG = Path(__file__).resolve().parents[3] / "config"
NOWHERE_VALKEY = "valkey://127.0.0.1:1/0"  # required by the settings; nothing here uses it


class Noted:
    """A job's handler that notes what each run was given."""

    def __init__(self) -> None:
        self.runs: list[dict[str, Any]] = []

    async def __call__(self, **args: Any) -> None:
        self.runs.append(args)


class Slow:
    """A job's handler that takes `takes` seconds, and notes when it has begun and
    when it has finished."""

    def __init__(self, takes: float) -> None:
        self.takes = takes
        self.started = asyncio.Event()
        self.finished = False

    async def __call__(self) -> None:
        self.started.set()
        await asyncio.sleep(self.takes)
        self.finished = True


class Lists(Protocol):
    def __call__(self, *listed: jobs.Job) -> list[Resources]: ...


@pytest.fixture
def lists(monkeypatch: pytest.MonkeyPatch) -> Lists:
    """Call with the jobs that every worker of the test runs, in place of the real ones.
    Returns the resources each worker is given, as it is given them."""

    def replace(*listed: jobs.Job) -> list[Resources]:
        given: list[Resources] = []

        def listing(resources: Resources, *, clock: Any) -> list[jobs.Job]:  # noqa: ARG001
            given.append(resources)
            return list(listed)

        monkeypatch.setattr(worker, "listed", listing)
        return given

    return replace


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list[Resources]:
    """The resources each app of the test opens, as it opens them."""
    recorded: list[Resources] = []

    @asynccontextmanager
    async def recording(settings: Settings) -> AsyncIterator[Resources]:
        async with open_resources(settings) as resources:
            recorded.append(resources)
            yield resources

    monkeypatch.setattr(actio.app, "open_resources", recording)
    return recorded


async def queue_of(database_url: str) -> list[tuple[str, str]]:
    """The jobs in the queue as another connection sees them, oldest first: each one's
    name and status."""
    async with await psycopg.AsyncConnection.connect(database_url) as connection:
        cursor = await connection.execute(
            "SELECT task_name, status::text FROM procrastinate_jobs ORDER BY id"
        )
        return [(name, status) for name, status in await cursor.fetchall()]


async def until(done: Callable[[], bool], *, within: float) -> None:
    """Wait until `done()` holds, looking every twentieth of a second. The test fails
    after `within` seconds."""
    async with asyncio.timeout(within):
        while not done():  # noqa: ASYNC110 - what is waited for is noted by a job or the log
            await asyncio.sleep(0.05)


@contextmanager
def standing_in_for_the_signals() -> Iterator[list[int]]:
    """A handler of the test's own for SIGTERM and SIGINT, in place of the test run's,
    so that neither can stop the run. It notes what reaches it. What it found is put
    back, pass or fail."""
    reached: list[int] = []

    def stand_in(received: int, _frame: FrameType | None) -> None:
        reached.append(received)

    before = {told: signal.signal(told, stand_in) for told in (signal.SIGTERM, signal.SIGINT)}
    try:
        yield reached
    finally:
        for told, handler in before.items():
            signal.signal(told, handler)


async def test_an_app_with_the_worker_runs_a_deferred_job_on_the_apps_own_resources(
    db: AsyncSession,
    settings: Settings,
    migrated: str,
    open_app: Any,
    lists: Lists,
    opened: list[Resources],
) -> None:
    """The job is in the queue before the app starts: one committed in the instant a
    worker starts can wait for its next look, five seconds later."""
    noted = Noted()
    given = lists(jobs.Job("note", noted))
    await jobs.defer(db, "note", n=1)
    await db.commit()

    # The limit is on the app's start and shutdown too: an app that waited for ever for
    # its worker would hang here, and the run with it.
    async with asyncio.timeout(20), open_app(settings, with_worker=True):
        await until(lambda: noted.runs == [{"n": 1}], within=10)

    [the_apps] = opened
    [the_workers] = given
    assert the_workers is the_apps  # the worker opened none of its own
    assert await queue_of(migrated) == []  # it succeeded, and was deleted


async def test_an_app_without_the_worker_runs_no_job(
    db: AsyncSession, settings: Settings, migrated: str, open_app: Any, lists: Lists
) -> None:
    noted = Noted()
    given = lists(jobs.Job("note", noted))
    await jobs.defer(db, "note", n=1)
    await db.commit()

    async with open_app(settings):
        await asyncio.sleep(1)  # a worker takes a job that waits for it within a second

    assert given == []
    assert noted.runs == []
    assert await queue_of(migrated) == [("note", "todo")]


async def test_shutting_the_app_down_waits_for_the_job_in_hand_and_ends_the_worker(
    db: AsyncSession,
    settings: Settings,
    migrated: str,
    open_app: Any,
    lists: Lists,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """As when the web server shuts the application down without a signal. The worker
    is told to stop then, once, and the app is gone only when it has. Its resources are
    closed after that: the job in hand has the database to its end."""
    slow, noted = Slow(takes=0.3), Noted()
    lists(jobs.Job("slow", slow), jobs.Job("note", noted))
    await jobs.defer(db, "slow")
    await db.commit()
    finished_when_the_resources_closed: list[bool] = []

    @asynccontextmanager
    async def noting_the_close(settings: Settings) -> AsyncIterator[Resources]:
        async with open_resources(settings) as resources:
            yield resources
        finished_when_the_resources_closed.append(slow.finished)

    monkeypatch.setattr(actio.app, "open_resources", noting_the_close)
    before = asyncio.all_tasks()

    async with asyncio.timeout(10):
        async with open_app(settings, with_worker=True):
            await slow.started.wait()
            in_hand = not slow.finished
        finished_when_closed = slow.finished

    await jobs.defer(db, "note", n=1)
    await db.commit()
    await asyncio.sleep(0.5)  # a worker that works takes it within milliseconds

    assert in_hand
    assert finished_when_closed
    assert finished_when_the_resources_closed == [True]
    assert asyncio.all_tasks() == before  # the worker's task has ended
    assert noted.runs == []
    assert await queue_of(migrated) == [("note", "todo")]  # the slow job succeeded


async def test_shut_down_without_a_signal_the_worker_is_told_before_the_answers_are_waited_for(
    db: AsyncSession,
    settings: Settings,
    migrated: str,
    open_app: Any,
    lists: Lists,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the app shuts down, the answers being written get a moment to finish, and
    the job in hand gets its own. The two run together: the worker is told first.

    Here the wait for the answers takes half a second, and a second job is queued behind
    the one in hand, which is over well before that. A worker that was told only after
    the wait would have taken it."""
    drain = TurnRunner.drain

    async def drain_that_takes_a_while(runner: TurnRunner, grace: timedelta | None = None) -> None:
        await asyncio.sleep(0.5)
        await drain(runner, grace)

    monkeypatch.setattr(TurnRunner, "drain", drain_that_takes_a_while)
    slow, noted = Slow(takes=0.2), Noted()
    lists(jobs.Job("slow", slow), jobs.Job("note", noted))
    await jobs.defer(db, "slow")
    await jobs.defer(db, "note", n=1)
    await db.commit()

    async with asyncio.timeout(20):
        async with open_app(settings, with_worker=True):
            await slow.started.wait()

    assert slow.finished
    assert noted.runs == []
    assert await queue_of(migrated) == [("note", "todo")]  # the slow job succeeded


async def test_told_by_a_signal_the_worker_takes_no_new_job_while_the_app_still_serves(
    db: AsyncSession, settings: Settings, migrated: str, open_app: Any, lists: Lists
) -> None:
    """A SIGTERM while a job is in hand, then a SIGINT. The web server goes on answering
    its requests after a signal, for up to 25 seconds; the worker stops at the first.
    The job in hand finishes, the app tells the worker nothing more at the second
    signal, and a job deferred afterwards waits, though the app is still open.

    This is the app's own part. In the process, uvicorn takes a SIGINT that follows
    another signal as its force quit, and skips the application's shutdown altogether.

    The signals reach the app's listener, then the test's own handler: never the test
    run."""
    slow, noted = Slow(takes=0.5), Noted()
    lists(jobs.Job("slow", slow), jobs.Job("note", noted))
    await jobs.defer(db, "slow")
    await db.commit()

    with standing_in_for_the_signals() as reached:
        async with asyncio.timeout(20), open_app(settings, with_worker=True) as browser:
            await slow.started.wait()
            os.kill(os.getpid(), signal.SIGTERM)
            await asyncio.sleep(0.1)  # a fifth of the way into the job in hand
            in_hand = not slow.finished
            os.kill(os.getpid(), signal.SIGINT)
            await until(lambda: slow.finished, within=10)
            await jobs.defer(db, "note", n=1)
            await db.commit()
            await asyncio.sleep(0.5)  # a worker that works takes it within milliseconds
            taken_while_open = list(noted.runs)
            still_serving = await browser.get("/api/healthz")

    assert reached == [signal.SIGTERM, signal.SIGINT]
    assert in_hand
    assert taken_while_open == []
    assert still_serving.status_code == 200
    assert noted.runs == []
    assert await queue_of(migrated) == [("note", "todo")]  # the slow job succeeded


async def test_a_job_in_hand_at_the_signal_still_finishes_when_the_app_is_then_shut_down(
    db: AsyncSession, settings: Settings, migrated: str, open_app: Any, lists: Lists
) -> None:
    """Shut down while the job is still in hand, as the web server does once its
    requests have ended. The worker was told at the first signal, and is told neither
    by the second nor by the shutdown: told twice, it would cut the job off."""
    slow = Slow(takes=0.6)
    lists(jobs.Job("slow", slow))
    await jobs.defer(db, "slow")
    await db.commit()

    with standing_in_for_the_signals() as reached:
        async with asyncio.timeout(20):
            async with open_app(settings, with_worker=True):
                await slow.started.wait()
                os.kill(os.getpid(), signal.SIGTERM)
                await asyncio.sleep(0.1)
                os.kill(os.getpid(), signal.SIGINT)
                await asyncio.sleep(0.05)
                in_hand = not slow.finished
            finished_when_closed = slow.finished

    assert reached == [signal.SIGTERM, signal.SIGINT]
    assert in_hand
    assert finished_when_closed
    assert await queue_of(migrated) == []  # it succeeded, and was deleted


async def test_in_a_server_started_from_another_process_only_the_first_signal_is_handed_on(
    settings: Settings, open_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """uvicorn's reloader runs the server in a process of its own, as in `make dev`.
    Such a server is told to stop several times over, and uvicorn takes a SIGINT that
    follows another signal for "quit at once": it would never shut the application
    down. So there the web server, which the test's handler stands in for, gets the
    first signal and no other. Here the test's process is made to look like one that
    another started."""
    monkeypatch.setattr(multiprocessing, "parent_process", lambda: object())

    with standing_in_for_the_signals() as reached:
        async with asyncio.timeout(20), open_app(settings):
            os.kill(os.getpid(), signal.SIGTERM)
            os.kill(os.getpid(), signal.SIGINT)
            os.kill(os.getpid(), signal.SIGTERM)
            await asyncio.sleep(0.05)

    assert reached == [signal.SIGTERM]


def cant_work(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """What the worker said when it couldn't work."""
    return [
        record
        for record in caplog.records
        if record.name == "actio.worker" and record.getMessage().startswith("The worker can't")
    ]


async def defer_in(database_url: str, name: str, **args: jobs.Argument) -> None:
    """Defer a job in a transaction of its own, and commit it."""
    engine = create_engine(database_url)
    try:
        async with create_session_factory(engine)() as db:
            await jobs.defer(db, name, **args)
            await db.commit()
    finally:
        await engine.dispose()


async def test_an_app_on_a_database_that_isnt_migrated_serves_and_its_worker_starts_once_it_is(
    settings: Settings,
    database_url: str,
    open_app: Any,
    lists: Lists,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """As when the API was started before the migration ran. The app serves all the
    same. The worker says why it can't work, in one line, and tries again after its
    pause, until it can.

    The job is deferred while the worker may be starting: it may wait for the worker's
    next look, five seconds later."""
    monkeypatch.setattr(worker, "TRIES_AGAIN_AFTER_S", 0.2)
    noted = Noted()
    lists(jobs.Job("note", noted))

    async with asyncio.timeout(60), open_app(settings, with_worker=True) as browser:
        healthy = await browser.get("/api/healthz")
        await until(lambda: bool(cant_work(caplog)), within=10)
        await asyncio.to_thread(schema.upgrade, database_url)
        await defer_in(database_url, "note", n=1)
        await until(lambda: noted.runs == [{"n": 1}], within=20)

    assert healthy.status_code == 200
    first = cant_work(caplog)[0]
    assert first.levelno == logging.ERROR
    assert first.exc_info is None
    assert first.getMessage() == (
        f"The worker can't work: the database was never migrated, and this code needs"
        f' {schema.head()}: run "actio --role migrate" first. It tries again in 0.2 s.'
    )


def a_free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


async def left_in(database_url: str) -> tuple[int, int]:
    """How many guests are in the database, and how many jobs in the queue."""
    async with await psycopg.AsyncConnection.connect(database_url) as connection:
        cursor = await connection.execute(
            "SELECT (SELECT count(*) FROM users), (SELECT count(*) FROM procrastinate_jobs)"
        )
        row = await cursor.fetchone()
        assert row is not None
        return row[0], row[1]


async def test_the_all_process_serves_sweeps_by_the_workers_schedule_and_ends_when_told_to_stop(
    db: AsyncSession, migrated: str
) -> None:
    """Started as a deployment starts it, and stopped as Docker stops it. Nobody defers
    the sweep: the worker's own schedule puts it in the queue when it starts. What it
    finds is a guest whose day ended yesterday, by the machine's clock: the one the
    process reads.

    It ends as the API's process ends: uvicorn, once it has shut down, sends itself the
    signal it was told with again, and a SIGTERM that Python doesn't handle ends a
    process by the signal."""
    await sessions.start_guest_session(db, now=datetime.now(UTC) - timedelta(days=2))
    await db.commit()
    port = a_free_port()
    environment = os.environ | {
        "ACTIO_DATABASE_URL": migrated,
        "ACTIO_VALKEY_URL": NOWHERE_VALKEY,
        "ACTIO_PORT": str(port),
        "ACTIO_MODELS_FILE": str(CONFIG / "models.toml"),
        "ACTIO_PLANS_FILE": str(CONFIG / "plans.toml"),
    }

    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "actio",
        "--role",
        "all",
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(30), httpx2.AsyncClient() as client:
            while True:
                assert process.returncode is None, await process.communicate()
                try:
                    healthy = await client.get(f"http://127.0.0.1:{port}/api/healthz")
                    break
                except httpx2.ConnectError:  # not listening yet
                    await asyncio.sleep(0.1)
        # Until the guest is gone, and the job too: it succeeded.
        async with asyncio.timeout(30):
            while await left_in(migrated) != (0, 0):
                assert process.returncode is None, await process.communicate()
                await asyncio.sleep(0.1)
        process.send_signal(signal.SIGTERM)
        async with asyncio.timeout(10):
            out, err = await process.communicate()
            ended_with = await process.wait()
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()

    log = err.decode()
    assert ended_with == -signal.SIGTERM, log
    assert healthy.status_code == 200
    assert "Traceback" not in log
    # uvicorn's lines as uvicorn writes them, and the worker's as the worker role does.
    assert "INFO:     Application startup complete.\n" in log
    assert " INFO actio.worker: deleted expired guests: 1, expired sessions: 0\n" in log
    # It read which migration the database is at, and ran none: nothing of Alembic's.
    assert "alembic" not in log
    assert b'"GET /api/healthz HTTP/1.1" 200' in out


async def test_the_server_that_the_reloader_started_shuts_the_application_down_whatever_follows(
    migrated: str,
) -> None:
    """`make dev` runs the role with --reload: uvicorn's reloader, and the server in a
    process of its own. A Ctrl-C there reaches that server more than once, and with a
    SIGTERM beside it: from the terminal, from concurrently, from the reloader. Here
    the server itself is sent a SIGTERM and then a SIGINT, the order in which uvicorn
    quits at once. It shuts the application down all the same, its worker included.

    The SIGINT comes while the server waits for a request it is answering, which is
    when uvicorn would act on it: a readiness probe that takes two seconds, because
    what stands in for Valkey here takes every connection and never says a word. That
    request is answered too.

    The two processes are in a group of their own, which is ended as one, pass or fail:
    the server holds the pipe that the log is read from."""
    asked = asyncio.Event()
    hold = asyncio.Event()

    async def say_nothing(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        asked.set()
        await hold.wait()
        writer.close()

    silent = await asyncio.start_server(say_nothing, "127.0.0.1", 0)
    port = a_free_port()
    environment = os.environ | {
        "ACTIO_DATABASE_URL": migrated,
        "ACTIO_VALKEY_URL": f"valkey://127.0.0.1:{silent.sockets[0].getsockname()[1]}/0",
        "ACTIO_PORT": str(port),
        "ACTIO_MODELS_FILE": str(CONFIG / "models.toml"),
        "ACTIO_PLANS_FILE": str(CONFIG / "plans.toml"),
    }
    reloader = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "actio",
        "--role",
        "all",
        "--reload",
        env=environment,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    assert reloader.stderr is not None
    said: list[str] = []
    server: int | None = None

    async def read_until(awaited: str, *, within: float) -> None:
        """Read the log up to the line that holds `awaited`, noting the server's
        process on the way."""
        nonlocal server
        assert reloader.stderr is not None
        async with asyncio.timeout(within):
            async for line in reloader.stderr:
                said.append(line.decode())
                if started := re.search(r"Started server process \[(\d+)\]", said[-1]):
                    server = int(started[1])
                if awaited in said[-1]:
                    return
        pytest.fail(f"it ended before it said {awaited!r}:\n{''.join(said)}")

    ended_well = False
    try:
        # Its worker works: the application has started, and listens for the signals.
        await read_until("Starting worker on all queues", within=30)
        assert server is not None
        async with httpx2.AsyncClient(timeout=20) as client:
            probe = asyncio.create_task(client.get(f"http://127.0.0.1:{port}/api/readyz"))
            async with asyncio.timeout(10):
                await asked.wait()  # the request is being answered
            os.kill(server, signal.SIGTERM)
            await read_until("Shutting down", within=10)  # the server waits for its request
            os.kill(server, signal.SIGINT)
            await read_until("Finished server process", within=15)
            async with asyncio.timeout(10):
                answered = await probe
        reloader.send_signal(signal.SIGTERM)
        async with asyncio.timeout(15):
            _, rest = await reloader.communicate()
        ended_well = True
    finally:
        hold.set()
        silent.close()
        if not ended_well:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(reloader.pid, signal.SIGKILL)
            async with asyncio.timeout(10):
                await reloader.communicate()

    log = "".join(said) + rest.decode()
    assert answered.status_code == 503  # Valkey never answered, and the request was
    assert "Stopped worker on all queues" in log
    assert "Waiting for application shutdown." in log
    assert "Application shutdown complete." in log
    assert "Traceback" not in log
