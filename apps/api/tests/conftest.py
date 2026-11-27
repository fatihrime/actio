"""Shared fixtures for the API tests.

The Postgres and Valkey servers come from the conftest.py at the repository's root.
"""

import asyncio
import json
import os
import uuid
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
    Iterator,
    Mapping,
)
from contextlib import (
    AbstractAsyncContextManager,
    AsyncExitStack,
    asynccontextmanager,
    contextmanager,
)
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, LiteralString, Protocol

import argon2.profiles
import httpx
import httpx2
import psycopg
import pytest
import uvicorn
from argon2 import PasswordHasher
from psycopg import sql
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from actio import catalog as catalogs
from actio.api.dependencies import get_clock, get_new_id
from actio.app import create_app
from actio.catalog import (
    Catalog,
    CatalogFile,
    Gate,
    ModelStatus,
    OfferedModel,
    OwnWait,
    budget_of,
)
from actio.chat import running as turn_runner
from actio.chat.events import TurnEvents
from actio.core import jobs, schema
from actio.core.database import create_engine, create_session_factory
from actio.core.settings import Settings
from actio.identity.passwords import Passwords
from actio.usage.plans import Plans, PlansFile
from actio_fakellm.app import create_app as create_fake_llm
from actio_llm import Breaker, ChatProvider, ChatRequest, StreamEvent

# For tests that never reach a database: nothing listens on port 1.
NOWHERE_DATABASE = "postgresql://actio:unused@127.0.0.1:1/actio"
NOWHERE_VALKEY = "valkey://127.0.0.1:1/0"


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hide the developer's ACTIO_* settings, so every test starts from the defaults.

    The variables that steer the tests themselves (ACTIO_TEST_*, ACTIO_REQUIRE_DOCKER) stay.
    """
    for name in list(os.environ):
        if name.startswith("ACTIO_") and not name.startswith(("ACTIO_TEST_", "ACTIO_REQUIRE_")):
            monkeypatch.delenv(name)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _no_patience_at_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    """An app that is closed while an answer is still being written gives it a moment to
    finish. Under test it gives none: a test that leaves a model waiting for ever would
    wait that moment out. The test of the moment itself sets its own."""
    monkeypatch.setattr(turn_runner, "GRACE", timedelta(0))


class Clock:
    """The time the app under test believes it is. Tests move it by hand."""

    def __init__(self) -> None:
        self.now = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)

    def advance(self, **amount: float) -> None:
        self.now += timedelta(**amount)


@pytest.fixture
def clock() -> Clock:
    return Clock()


def cheap_passwords() -> Passwords:
    """Real Argon2id with the smallest parameters, so a test's hash takes microseconds
    instead of 50 ms. The hashes are as real as production's, only weaker."""
    return Passwords(PasswordHasher.from_parameters(argon2.profiles.CHEAPEST))


@pytest.fixture
def passwords() -> Passwords:
    return cheap_passwords()


# --- Databases ------------------------------------------------------------------------


@contextmanager
def _empty_database(postgres_url: str) -> Iterator[str]:
    name = f"test_{uuid.uuid4().hex}"
    with psycopg.connect(postgres_url, autocommit=True) as server:
        server.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    yield f"{postgres_url.rsplit('/', 1)[0]}/{name}"
    with psycopg.connect(postgres_url, autocommit=True) as server:
        server.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture
def database_url(postgres_url: str) -> Iterator[str]:
    """A new, empty database for one test, on the session's Postgres server."""
    with _empty_database(postgres_url) as url:
        yield url


@pytest.fixture
def another_database_url(postgres_url: str) -> Iterator[str]:
    """A second empty database, for a test that compares two."""
    with _empty_database(postgres_url) as url:
        yield url


@pytest.fixture
def migrated(database_url: str) -> str:
    """This test's database, with every migration applied."""
    schema.upgrade(database_url)
    return database_url


@pytest.fixture
async def db(migrated: str) -> AsyncIterator[AsyncSession]:
    """A database session on this test's migrated database, for tests below the API."""
    engine = create_engine(migrated)
    async with create_session_factory(engine)() as session:
        yield session
    await engine.dispose()


class WorkThrough(Protocol):
    def __call__(self, *listed: jobs.Job) -> Awaitable[None]: ...


@pytest.fixture
def work_through(migrated: str) -> WorkThrough:
    """Call with jobs for a worker on this test's database that runs what is queued for
    them, and returns once nothing is left. It leaves the signals of the test's process
    alone, and fails the test after ten seconds, or eleven with a job that never ends."""

    async def work(*listed: jobs.Job) -> None:
        async with asyncio.timeout(10):
            await jobs.work(
                listed, database_url=migrated, grace=1, until_idle=True, on_signals=False
            )

    return work


class QueueBooks:
    """The queue's books on this test's database, written as Procrastinate's workers
    write them: for the tests of what looking after the queue does with them, and of the
    jobs put back after an error, to be run again once their wait is over.

    A worker that starts takes the workers that died more than 30 seconds ago off the
    list by itself. So what a dead worker leaves is written while the test's worker
    runs: by a job of the test's, before it looks after the queue."""

    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    async def _one(self, statement: LiteralString, *params: Any) -> Any:
        async with await psycopg.AsyncConnection.connect(self.database_url) as connection:
            cursor = await connection.execute(statement, params)
            row = await cursor.fetchone()
        assert row is not None
        return row[0]

    async def _do(self, *statements: tuple[LiteralString, tuple[Any, ...]]) -> None:
        async with await psycopg.AsyncConnection.connect(self.database_url) as connection:
            for statement, params in statements:
                await connection.execute(statement, params)

    async def deferred(self, db: AsyncSession, name: str, **args: jobs.Argument) -> int:
        """Defer the job `name` in `db`, and commit it. Its id in the queue."""
        await jobs.defer(db, name, **args)
        await db.commit()
        job_id: int = await self._one("SELECT max(id) FROM procrastinate_jobs")
        return job_id

    async def already_run_again(self, job_id: int, times: int) -> None:
        """Make the job one that has already been run again `times` times."""
        await self._do(
            ("UPDATE procrastinate_jobs SET attempts = %s WHERE id = %s", (times, job_id))
        )

    async def due(self) -> list[tuple[str, str, int, float | None]]:
        """The jobs in the queue, oldest first: each one's name, its status, how many
        times it has been run again, and in how many seconds it is due, to the nearest
        ten. `None` for a job deferred to run at once, and never put back.

        By this process's clock, which dated the job: a job that is put back is dated by
        its worker's clock, and a test's worker runs in the test's process. The
        database's clock runs elsewhere, and what it would say of the wait is off by the
        difference between the two. To the nearest ten seconds, for the moment between
        the job being put back and this look: a job put back to be run at once is due
        in 0 seconds, and one put back for a minute in 60."""
        async with await psycopg.AsyncConnection.connect(self.database_url) as connection:
            cursor = await connection.execute(
                "SELECT task_name, status::text, attempts, scheduled_at"
                " FROM procrastinate_jobs ORDER BY id"
            )
            now = datetime.now(UTC)
            return [
                (
                    name,
                    status,
                    attempts,
                    None if at is None else round((at - now).total_seconds(), -1),
                )
                for name, status, attempts, at in await cursor.fetchall()
            ]

    async def wait_is_over(self) -> None:
        """End the wait of every job that waits to be run again: due now, by the
        database's clock, so that the next worker that works through the queue takes it.

        A job that is put back is dated by its worker's clock, and taken by the
        database's, a few milliseconds apart or more, also when it is put back to be run
        at once; and a worker that waits looks for it only every five seconds. A test
        waits for neither."""
        await self._do(
            (
                "UPDATE procrastinate_jobs SET scheduled_at = now()"
                " WHERE status = 'todo' AND scheduled_at > now()",
                (),
            )
        )

    async def a_dead_worker(self) -> int:
        """A worker on the queue's list that last said it was alive a minute ago: twice
        the 30 seconds after which it is taken for dead. Its id."""
        worker_id: int = await self._one(
            "INSERT INTO procrastinate_workers (last_heartbeat)"
            " VALUES (now() - interval '1 minute') RETURNING id"
        )
        return worker_id

    async def a_live_worker(self) -> int:
        """A worker on the queue's list that has just said it is alive. Its id."""
        worker_id: int = await self._one(
            "INSERT INTO procrastinate_workers DEFAULT VALUES RETURNING id"
        )
        return worker_id

    async def left_doing(self, job_id: int, *, worker_gone: bool = False) -> None:
        """Leave the job `doing`, as a worker that was killed with it in hand does: under
        a worker that has been silent for a minute, or, `worker_gone`, under one that has
        been taken off the list since, which leaves the job with no worker."""
        worker_id = await self.a_dead_worker()
        statements: list[tuple[LiteralString, tuple[Any, ...]]] = [
            (
                "UPDATE procrastinate_jobs SET status = 'doing', worker_id = %s WHERE id = %s",
                (worker_id, job_id),
            )
        ]
        if worker_gone:
            statements.append(("DELETE FROM procrastinate_workers WHERE id = %s", (worker_id,)))
        await self._do(*statements)

    async def ended(self, job_id: int, status: str, *, ago: timedelta) -> None:
        """End the job as `status`, `failed`, `aborted` or `cancelled`, `ago`: the time
        of its last event. One that is cancelled never began."""
        statements: list[tuple[LiteralString, tuple[Any, ...]]] = []
        if status != "cancelled":
            statements.append(
                ("UPDATE procrastinate_jobs SET status = 'doing' WHERE id = %s", (job_id,))
            )
        statements += [
            (
                "UPDATE procrastinate_jobs SET status = %s::procrastinate_job_status WHERE id = %s",
                (status, job_id),
            ),
            ("UPDATE procrastinate_events SET at = now() - %s WHERE job_id = %s", (ago, job_id)),
        ]
        await self._do(*statements)

    async def workers(self) -> list[int]:
        """The ids of the workers on the queue's list."""
        async with await psycopg.AsyncConnection.connect(self.database_url) as connection:
            cursor = await connection.execute("SELECT id FROM procrastinate_workers ORDER BY id")
            return [worker_id for (worker_id,) in await cursor.fetchall()]

    async def events_of(self, *job_ids: int) -> int:
        """How many events the queue holds for these jobs."""
        count: int = await self._one(
            "SELECT count(*) FROM procrastinate_events WHERE job_id = ANY(%s)", list(job_ids)
        )
        return count


@pytest.fixture
def queue_books(migrated: str) -> QueueBooks:
    """The queue's books on this test's migrated database: see `QueueBooks`."""
    return QueueBooks(migrated)


# --- The app --------------------------------------------------------------------------


@pytest.fixture
def settings(database_url: str, valkey_url: str) -> Settings:
    """Settings that point at this test's own database and the session's Valkey.

    The Valkey server is shared by the whole run, so each test gets its own key prefix:
    what one test counts against a rate limit, the next one doesn't inherit.
    """
    return Settings(
        database_url=SecretStr(database_url),
        valkey_url=SecretStr(valkey_url),
        valkey_prefix=f"test-{uuid.uuid4().hex[:16]}",
    )


FAKE_MODELS = CatalogFile.model_validate(
    {
        "default": "standard",
        "providers": {"fake": {"kind": "openai-compatible", "url": "http://fakellm/v1"}},
        "models": {
            "standard": {
                "title": "Fake",
                "provider": "fake",
                "model": "fake-chat",
                "context_window": 8192,
                "max_output_tokens": 1024,
                "thinking": True,
            }
        },
    }
)
"""What the app under test offers: one model, on a provider called `fake`."""


@pytest.fixture
async def catalog() -> AsyncIterator[Catalog]:
    """The models of the app under test, served by fakellm inside this process: no
    Ollama, no network, and answers that are the same every time."""
    fake = httpx2.ASGITransport(app=create_fake_llm())
    async with catalogs.build(
        FAKE_MODELS, ollama_url="http://127.0.0.1:1", transports={"fake": fake}
    ) as built:
        yield built


def plans_for_tests(
    *,
    guest: tuple[int, int] | None = (1_000_000, 10_000_000),
    account: tuple[int, int] | None = (1_000_000, 10_000_000),
    free: bool = False,
    min_answer_tokens: int = 16,
    models: Iterable[str] = ("standard",),
    free_models: Iterable[str] = (),
    answers_at_once: int | None = None,
    credits_a_token: int = 1,
    prices: Mapping[str, Mapping[str, int]] | None = None,
    plans: Mapping[str, Mapping[str, Any]] | None = None,
) -> Plans:
    """Prices and plans for the app under test.

    A model costs a credit a token, read or written, so what a turn costs is its tokens
    added up. `credits_a_token` makes it dearer, and `free=True` makes it cost nothing.
    `guest` and `account` are what each may use in a session and in a week, or `None`
    for no limits; by default far more than a test uses. `models` are the models to price: the one that nearly every catalog here
    offers, unless a test offers others. `free_models` are offered beside them, and cost
    nothing whatever `free` says. `answers_at_once` is how many answers a guest and an
    account may each have being written at once; by default as many as they ask for.
    `prices` states models' prices as a plans file would, such as `{"standard":
    {"input": 250_000, "output": 1_000_000, "cached_input": 25_000}}`: each in the place
    of the price the rest would give it, or beside them for a model they don't name.
    `plans` states plans beside `guest` and `member` as a plans file would, such as
    `{"dev": {"title": "Developer", "session_credits": 5_000, "week_credits": 50_000}}`:
    plans that no account has unless it is put on one (`accounts.put_on_plan`).
    """

    def plan(title: str, allowance: tuple[int, int] | None) -> dict[str, Any]:
        limits: dict[str, Any] = {"title": title}
        if allowance is not None:
            limits |= {"session_credits": allowance[0], "week_credits": allowance[1]}
        if answers_at_once is not None:
            limits["answers_at_once"] = answers_at_once
        return limits

    price = 0 if free else credits_a_token * 1_000_000
    priced = {name: {"input": price, "output": price} for name in models}
    priced |= {name: {"input": 0, "output": 0} for name in free_models}
    priced |= {name: dict(stated) for name, stated in (prices or {}).items()}
    return Plans(
        PlansFile.model_validate(
            {
                "guests": "guest",
                "accounts": "member",
                "session_hours": 5,
                "week_resets": "MON 00:00 UTC",
                "min_answer_tokens": min_answer_tokens,
                "prices": priced,
                "plans": {"guest": plan("Guest", guest), "member": plan("Member", account)}
                | {name: dict(stated) for name, stated in (plans or {}).items()},
            }
        )
    )


class PlansWith(Protocol):
    def __call__(
        self,
        *,
        guest: tuple[int, int] | None = ...,
        account: tuple[int, int] | None = ...,
        free: bool = ...,
        min_answer_tokens: int = ...,
        models: Iterable[str] = ...,
        free_models: Iterable[str] = ...,
        answers_at_once: int | None = ...,
        credits_a_token: int = ...,
        prices: Mapping[str, Mapping[str, int]] | None = ...,
        plans: Mapping[str, Mapping[str, Any]] | None = ...,
    ) -> Plans: ...


@pytest.fixture
def plans_with() -> PlansWith:
    """Call it for plans with limits a test can reach: `plans_with(guest=(1_000, 10_000))`
    lets a guest use 1,000 credits a session and 10,000 a week, and
    `plans_with(answers_at_once=1)` lets it have one answer written at a time.
    `plans_with(plans={"dev": {...}})` offers a plan beside the two, that an account is
    put on. See `plans_for_tests`."""
    return plans_for_tests


class ScriptedModel:
    """A model that says exactly what a test tells it to, and remembers what it was asked.

    The script is a list of what to do in order: an event to yield, an error to raise,
    an `asyncio.Event` to wait for, which lets a test hold the answer half-way, or a
    function to call, such as one that moves the test's `clock` between two events.

    `closes_when` makes closing its stream take a while, as closing the connection to a
    real model's server does: the stream ends only once that event is set.
    """

    def __init__(self, *script: Any, closes_when: asyncio.Event | None = None) -> None:
        self.script = list(script)
        self.closes_when = closes_when
        self.requests: list[ChatRequest] = []
        self.started = asyncio.Event()
        """Set once the model has been asked: the turn is now running."""
        self.waiting = asyncio.Event()
        """Set while the model waits for an `asyncio.Event` of its script. Whatever it
        yielded before has been taken by then: stored in the turn, and streamed."""
        self.closing = asyncio.Event()
        """Set when the answer's stream begins to end, whoever ends it."""
        self.closed = asyncio.Event()
        """Set when the answer's stream ends, whoever ended it."""

    async def stream(self, request: ChatRequest) -> AsyncGenerator[StreamEvent]:
        self.requests.append(request)
        self.started.set()
        try:
            for item in self.script:
                if isinstance(item, BaseException):
                    raise item
                if isinstance(item, asyncio.Event):
                    self.waiting.set()
                    await item.wait()
                    self.waiting.clear()
                elif callable(item):
                    item()
                else:
                    yield item
        finally:
            self.closing.set()
            if self.closes_when is not None:
                await self.closes_when.wait()
            self.closed.set()


type Once = Callable[[BaseException], Callable[[], None]]


def _once(error: BaseException) -> Callable[[], None]:
    raised = False

    def raise_the_first_time() -> None:
        nonlocal raised
        if not raised:
            raised = True
            raise error

    return raise_the_first_time


@pytest.fixture
def once() -> Once:
    """Call it with an error to get what, in a `ScriptedModel`'s script, raises that
    error the first time the model gets there, and nothing later, when the script goes
    on past it: `lineup.add("standard", once(error), TextDelta("Hi"), DONE)` is a model
    that fails when it is first asked, and answers when it is asked again."""
    return _once


def catalog_of(
    model: ChatProvider,
    *,
    thinking: bool = True,
    also: Iterable[str] = (),
    titles: str | None = None,
) -> Catalog:
    """A catalog that offers one model, `standard`, which is `model`. Each name in `also`
    is the same model offered once more, such as `local`, titled "Local". `titles` names
    the model that writes conversations' titles: by default none does."""
    named = {"standard": "Scripted"} | {name: name.capitalize() for name in also}
    offered = {
        name: OfferedModel(
            name=name,
            title=title,
            kind="openai-compatible",
            model="scripted-1",
            context_window=4096,
            max_output_tokens=512,
            chat=model,
        )
        for name, title in named.items()
    }
    return Catalog(
        offered, default="standard", thinking=dict.fromkeys(offered, thinking), titles=titles
    )


type State = Literal["ready", "missing", "unreachable"]

SCRIPTED = "scripted"
"""The provider of a `Lineup`'s models, as their budgets' keys name it."""


class Held:
    """What the server of a `Lineup`'s model answers about the model, held until the
    test lets it go (`Lineup.hold`)."""

    def __init__(self) -> None:
        self.asked = asyncio.Event()
        """Set once its server has been asked, and holds the answer."""
        self.answers = asyncio.Event()
        """Set it to have the server answer: each ask held so far, and every one after.
        It answers what the lineup says of the model then (`Lineup.becomes`)."""


class Lineup:
    """Models a test offers, each with a script of its own, on servers of the test's
    choosing: a server that writes only so many answers at once is a `Gate`, which the
    models on it share. The first model added is the default.

    A model may name others that stand in for it (`then`), and say what its server says
    of it (`state`), and whether it can reason (`thinking`). Each has a window of 4,096
    tokens and answers of 512 at most, no limit of tokens a minute, and no budget, unless
    a test says otherwise.

    Each is on a provider called `scripted`, as `scripted-<its name>`, unless a test
    gives it another name there (`on_server`): two models with the same name there that
    each have a budget share it, as two models of a models file do, and they share one
    breaker and one provider's own wait, whether or not they have a budget.

    The models keep their breakers, and the waits their providers named, for as long as
    the lineup lives: every catalog it gives has the same ones, which a test reads
    through `catalog().get(name).breaker` and `catalog().get(name).own_wait`.

    What a model's server says of it can change while the test runs (`becomes`), and
    its answer can be held (`hold`): every catalog the lineup gives asks its servers
    through the lineup, which notes each ask (`asked`)."""

    def __init__(self) -> None:
        self._offered: dict[str, OfferedModel] = {}
        self._thinking: dict[str, bool] = {}
        self._states: dict[str, State] = {}
        self._holds: dict[str, Held] = {}
        self._breakers: dict[str, Breaker] = {}
        self._own_waits: dict[str, OwnWait] = {}
        self.asked: list[str] = []
        """The names of the models whose servers were asked about them, in the order
        asked: each when its server is asked, before any hold."""

    def add(
        self,
        name: str,
        *script: Any,
        gate: Gate | None = None,
        closes_when: asyncio.Event | None = None,
        then: Iterable[str] = (),
        state: State = "ready",
        thinking: bool = False,
        context_window: int = 4096,
        max_output_tokens: int = 512,
        tokens_a_minute: int | None = None,
        budget: Mapping[str, int] | None = None,
        on_server: str | None = None,
    ) -> ScriptedModel:
        """Offer a model called `name` that follows `script`, behind `gate` if any. See
        `ScriptedModel` for `closes_when`, and `offer` for the rest."""
        model = ScriptedModel(*script, closes_when=closes_when)
        self.offer(
            name,
            model,
            gate=gate,
            then=then,
            state=state,
            thinking=thinking,
            context_window=context_window,
            max_output_tokens=max_output_tokens,
            tokens_a_minute=tokens_a_minute,
            budget=budget,
            on_server=on_server,
        )
        return model

    def offer(
        self,
        name: str,
        chat: ChatProvider,
        *,
        gate: Gate | None = None,
        then: Iterable[str] = (),
        state: State = "ready",
        thinking: bool = False,
        context_window: int = 4096,
        max_output_tokens: int = 512,
        tokens_a_minute: int | None = None,
        budget: Mapping[str, int] | None = None,
        on_server: str | None = None,
    ) -> None:
        """Offer `chat` as the model called `name`, behind `gate` if any: for a model
        that is more than its script, such as one whose server can load it ahead. `then`
        names the models asked in its place, `state` is what its server says of it,
        `thinking` whether it can reason, which a model that isn't ready never can, and
        `tokens_a_minute` how many tokens its server takes in a minute, which with its
        window makes its room (`OfferedModel.room`), and nothing else.

        `budget` gives it a budget, from what a models file would state of its
        provider's limits, such as `{"requests_a_day": 10}`: each figure is kept as the
        catalog keeps it (`actio.catalog.budget_of`). `on_server` is its name on its
        server, `scripted-<name>` unless given, and the models with one name there share
        one breaker and one provider's own wait."""
        model = on_server or f"scripted-{name}"
        self._offered[name] = OfferedModel(
            name=name,
            title=name.capitalize(),
            kind="openai-compatible",
            model=model,
            context_window=context_window,
            max_output_tokens=max_output_tokens,
            chat=chat,
            gate=gate,
            then=tuple(then),
            tokens_a_minute=tokens_a_minute,
            budget=None if budget is None else budget_of(SCRIPTED, model, budget),
            breaker=self._breakers.setdefault(model, Breaker()),
            own_wait=self._own_waits.setdefault(model, OwnWait()),
        )
        self._thinking[name] = thinking
        self._states[name] = state

    def becomes(self, name: str, state: State) -> None:
        """From now on, the server of the model called `name` says `state` of it when it
        is asked: for a model whose server goes away, or comes back, or that is pulled."""
        self._states[name] = state

    def hold(self, name: str) -> Held:
        """Hold what the server of the model called `name` answers about it, at each ask
        from now on, until the test sets the `Held.answers` this gives. An ask held when
        the test ends is cancelled with its app, or by `Catalog.stop_tasks`."""
        held = self._holds[name] = Held()
        return held

    @property
    def names(self) -> list[str]:
        return list(self._offered)

    def catalog(self, *, titles: str | None = None) -> Catalog:
        """The catalog of the models added so far. `titles` names the one that writes
        conversations' titles: by default none does."""
        default = next(iter(self._offered))
        return _LinedUp(
            self._offered, default=default, thinking=self._thinking, titles=titles, lineup=self
        )


class _LinedUp(Catalog):
    """The catalog of a `Lineup`: each model's server says what the lineup says of it
    when it is asked, after the lineup's hold, if any."""

    def __init__(
        self,
        models: Mapping[str, OfferedModel],
        *,
        default: str,
        thinking: Mapping[str, bool],
        titles: str | None,
        lineup: Lineup,
    ) -> None:
        super().__init__(models, default=default, thinking=thinking, titles=titles)
        self._lineup = lineup

    async def _ask(self, offered: OfferedModel) -> ModelStatus:
        self._lineup.asked.append(offered.name)
        held = self._lineup._holds.get(offered.name)
        if held is not None:
            held.asked.set()
            await held.answers.wait()
        state = self._lineup._states[offered.name]
        if state == "missing":
            # As a server that speaks OpenAI's API and hasn't got the model: the file's
            # word on reasoning stands.
            return ModelStatus(state, thinking=self._declared_thinking.get(offered.name, False))
        if state != "ready":
            return ModelStatus(state, thinking=False)
        return await super()._ask(offered)


@pytest.fixture
def lineup() -> Lineup:
    """An empty `Lineup`: add the models, then start an app with its `catalog()`."""
    return Lineup()


type AsksEnded = Callable[[], Awaitable[None]]


async def _asks_ended() -> None:
    asking = [task for task in asyncio.all_tasks() if task.get_name().startswith("status-")]
    if asking:
        async with asyncio.timeout(5):
            await asyncio.wait(asking)


@pytest.fixture
def asks_ended() -> AsksEnded:
    """Call it, and await, to wait until every ask about a model's status that is under
    way in this process has ended, however it ends: one that runs behind the request
    that started it has kept its answer by then (`Catalog.chain_as_known`). Each is the
    catalog's task called `status-<the model's name>`. The test fails after five
    seconds."""
    return _asks_ended


class Appended:
    """What turns have added to their streams, each event once it is there, in the order
    added: for a test that waits until a turn has said something before it goes on, such
    as a number a turn waits behind at a gate, which it hands on from a task of its own.
    The first event, `start`, is not among them: it opens the stream."""

    def __init__(self) -> None:
        self.events: list[tuple[str, bytes]] = []
        """Each event, with the id of its turn."""

    def waits(self, turn_id: object) -> list[int]:
        """How many answers the turn called `turn_id` said it waits behind, in the order
        said: each `data-waits` chunk of its stream so far."""
        said = []
        for turn, event in self.events:
            if turn == str(turn_id) and b'"type":"data-waits"' in event:
                said.append(json.loads(event.removeprefix(b"data: "))["data"]["behind"])
        return said


@pytest.fixture
def appended(monkeypatch: pytest.MonkeyPatch) -> Appended:
    """What turns add to their streams from now on (`Appended`), noted once Valkey has
    taken each."""
    noted = Appended()
    append = TurnEvents.append

    async def noting(events: TurnEvents, turn_id: uuid.UUID, event: bytes) -> bool:
        added = await append(events, turn_id, event)
        noted.events.append((str(turn_id), event))
        return added

    monkeypatch.setattr(TurnEvents, "append", noting)
    return noted


class Offer(Protocol):
    def __call__(
        self, *script: Any, thinking: bool = True, titles: str | None = None
    ) -> tuple[ScriptedModel, Catalog]: ...


@pytest.fixture
def offer() -> Offer:
    """Call with a script to get a model that follows it, and the catalog that offers
    only that model: for a test that starts the app itself, with `open_app`. With
    `titles="standard"`, the model also writes conversations' titles."""

    def make(
        *script: Any, thinking: bool = True, titles: str | None = None
    ) -> tuple[ScriptedModel, Catalog]:
        model = ScriptedModel(*script)
        return model, catalog_of(model, thinking=thinking, titles=titles)

    return make


class ServerIds:
    """The ids the app under test gives to what it creates itself: a turn, an answer.

    Counted up from 1 instead of random, so a test can say beforehand what the next
    one is, and a recorded stream is the same every time.
    """

    def __init__(self) -> None:
        self.made: list[uuid.UUID] = []

    def __call__(self) -> uuid.UUID:
        self.made.append(self.number(len(self.made) + 1))
        return self.made[-1]

    @staticmethod
    def number(n: int) -> uuid.UUID:
        """The `n`th id: `01900000-0000-7000-8000-…`, ending in `n`. A valid version 7."""
        return uuid.UUID(int=(0x0190 << 112) | (0x7 << 76) | (0b10 << 62) | n)


@pytest.fixture
def server_ids() -> ServerIds:
    return ServerIds()


@pytest.fixture
async def catalog_without_its_model() -> AsyncIterator[Catalog]:
    """Like `catalog`, but the server hasn't got the model the catalog names."""
    fake = httpx2.ASGITransport(app=create_fake_llm(models=["some-other-model"]))
    async with catalogs.build(
        FAKE_MODELS, ollama_url="http://127.0.0.1:1", transports={"fake": fake}
    ) as built:
        yield built


@pytest.fixture
async def catalog_without_its_server() -> AsyncIterator[Catalog]:
    """Like `catalog`, but nothing answers at the server's address."""

    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused", request=request)

    async with catalogs.build(
        FAKE_MODELS,
        ollama_url="http://127.0.0.1:1",
        transports={"fake": httpx2.MockTransport(refuse)},
    ) as built:
        yield built


class OpenApp(Protocol):
    def __call__(
        self,
        settings: Settings,
        *,
        address: str = "127.0.0.1",
        passwords: Passwords | None = None,
        catalog: Catalog | None = None,
        plans: Plans | None = None,
        with_worker: bool = False,
    ) -> AbstractAsyncContextManager[httpx.AsyncClient]: ...


@pytest.fixture
def open_app(clock: Clock, catalog: Catalog, server_ids: ServerIds) -> OpenApp:
    """Call with settings for an app that runs inside an `async with` block, which gives
    an HTTP client wired straight to it. Leaving the block shuts the app down, as a
    process that is told to stop does: for tests of what a shutdown does.

    No server and no network: requests go to the app in process. The client's URL is
    the app's public address, so it keeps cookies as a browser there would. The app
    reads the time from `clock`, and takes the ids of the turns and answers it creates
    from `server_ids`. `address` is the network address the app sees the requests come
    from. `passwords` replaces the cheap password hasher, to watch it, `catalog` the
    fake models, and `plans` the usage limits, which are otherwise out of reach.
    `with_worker=True` runs the worker inside the app, as `actio --role all` does.
    """
    fake_models = catalog

    @asynccontextmanager
    async def opened(
        settings: Settings,
        *,
        address: str = "127.0.0.1",
        passwords: Passwords | None = None,
        catalog: Catalog | None = None,
        plans: Plans | None = None,
        with_worker: bool = False,
    ) -> AsyncIterator[httpx.AsyncClient]:
        offered = catalog or fake_models
        app = create_app(
            settings,
            passwords=passwords or cheap_passwords(),
            catalog=offered,
            plans=plans or plans_for_tests(models=[model.name for model in offered]),
            with_worker=with_worker,
        )
        app.dependency_overrides[get_clock] = lambda: lambda: clock.now
        app.dependency_overrides[get_new_id] = lambda: server_ids
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, client=(address, 50_000)),
                base_url=settings.public_url,
            ) as browser,
        ):
            yield browser

    return opened


class StartApp(Protocol):
    def __call__(
        self,
        settings: Settings,
        *,
        address: str = "127.0.0.1",
        passwords: Passwords | None = None,
        catalog: Catalog | None = None,
        plans: Plans | None = None,
    ) -> Awaitable[httpx.AsyncClient]: ...


@pytest.fixture
async def start_app(open_app: OpenApp) -> AsyncIterator[StartApp]:
    """Call with settings to get an HTTP client wired straight to a started app, which
    runs until the test is over. See `open_app` for what it is given."""
    async with AsyncExitStack() as stack:

        async def start(
            settings: Settings,
            *,
            address: str = "127.0.0.1",
            passwords: Passwords | None = None,
            catalog: Catalog | None = None,
            plans: Plans | None = None,
        ) -> httpx.AsyncClient:
            return await stack.enter_async_context(
                open_app(
                    settings, address=address, passwords=passwords, catalog=catalog, plans=plans
                )
            )

        yield start


@pytest.fixture
async def client(start_app: StartApp) -> httpx.AsyncClient:
    """The app with no database and no Valkey behind it, for what needs neither."""
    return await start_app(
        Settings(database_url=SecretStr(NOWHERE_DATABASE), valkey_url=SecretStr(NOWHERE_VALKEY))
    )


@pytest.fixture
async def running(start_app: StartApp, settings: Settings) -> httpx.AsyncClient:
    """The app on this test's database as it was created: empty, not migrated."""
    return await start_app(settings)


@pytest.fixture
async def api(start_app: StartApp, settings: Settings, migrated: str) -> httpx.AsyncClient:
    """The app on this test's migrated database and the session's Valkey."""
    assert migrated == settings.database_url.get_secret_value()
    return await start_app(settings)


class Serve(Protocol):
    def __call__(self, catalog: Catalog, *, plans: Plans | None = None) -> Awaitable[str]: ...


@pytest.fixture
async def serve(
    clock: Clock, server_ids: ServerIds, settings: Settings, migrated: str
) -> AsyncIterator[Serve]:
    """Call with a catalog to run the app on a real port of this machine, and get its
    address.

    `start_app` hands a test the whole response once the app has finished with it.
    That can't show a response arriving piece by piece, or a reader leaving half-way:
    for those, the app has to be served over a real connection, as uvicorn serves it.
    """
    assert migrated
    running: list[tuple[uvicorn.Server, asyncio.Task[None]]] = []

    async def start(catalog: Catalog, *, plans: Plans | None = None) -> str:
        app = create_app(
            settings,
            passwords=cheap_passwords(),
            catalog=catalog,
            plans=plans or plans_for_tests(models=[model.name for model in catalog]),
        )
        app.dependency_overrides[get_clock] = lambda: lambda: clock.now
        app.dependency_overrides[get_new_id] = lambda: server_ids
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on")
        )
        task = asyncio.create_task(server.serve())
        running.append((server, task))
        while not server.started:
            if task.done():
                task.result()  # it failed to start: say why
            await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    yield start
    for server, task in running:
        server.should_exit = True
        await task


class Served(Protocol):
    def __call__(
        self, *script: Any, thinking: bool = True, plans: Plans | None = None
    ) -> Awaitable[tuple[ScriptedModel, str]]: ...


@pytest.fixture
def served(serve: Serve) -> Served:
    """Call with a script to get a model that follows it, and the address of a served
    app that offers only that model."""

    async def make(
        *script: Any, thinking: bool = True, plans: Plans | None = None
    ) -> tuple[ScriptedModel, str]:
        model = ScriptedModel(*script)
        return model, await serve(catalog_of(model, thinking=thinking), plans=plans)

    return make


class Scripted(Protocol):
    def __call__(
        self,
        *script: Any,
        thinking: bool = True,
        plans: Plans | None = None,
        also: Iterable[str] = (),
    ) -> Awaitable[tuple[ScriptedModel, httpx.AsyncClient]]: ...


@pytest.fixture
def scripted(start_app: StartApp, settings: Settings, migrated: str) -> Scripted:
    """Call with a script to get a model that follows it, and a guest's browser on an
    app that offers only that model: as `standard`, and under each name in `also`."""
    assert migrated

    async def make(
        *script: Any,
        thinking: bool = True,
        plans: Plans | None = None,
        also: Iterable[str] = (),
    ) -> tuple[ScriptedModel, httpx.AsyncClient]:
        model = ScriptedModel(*script)
        browser = await start_app(
            settings, catalog=catalog_of(model, thinking=thinking, also=also), plans=plans
        )
        await browser.post("/api/session/guest")
        return model, browser

    return make
