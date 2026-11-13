"""Shared fixtures for the API tests.

The Postgres and Valkey servers come from the conftest.py at the repository's root.
"""

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Iterator
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

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
from actio.catalog import Catalog, CatalogFile, OfferedModel
from actio.core import schema
from actio.core.database import create_engine, create_session_factory
from actio.core.settings import Settings
from actio.identity.passwords import Passwords
from actio_fakellm.app import create_app as create_fake_llm
from actio_llm import ChatProvider, ChatRequest, StreamEvent

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


@pytest.fixture
def database_url(postgres_url: str) -> Iterator[str]:
    """A new, empty database for one test, on the session's Postgres server."""
    name = f"test_{uuid.uuid4().hex}"
    with psycopg.connect(postgres_url, autocommit=True) as server:
        server.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    yield f"{postgres_url.rsplit('/', 1)[0]}/{name}"
    with psycopg.connect(postgres_url, autocommit=True) as server:
        server.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


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


class ScriptedModel:
    """A model that says exactly what a test tells it to, and remembers what it was asked.

    The script is a list of what to do in order: an event to yield, an error to raise,
    or an `asyncio.Event` to wait for, which lets a test hold the answer half-way.
    """

    def __init__(self, *script: Any) -> None:
        self.script = list(script)
        self.requests: list[ChatRequest] = []
        self.started = asyncio.Event()
        """Set once the model has been asked: the turn is now running."""
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
                    await item.wait()
                else:
                    yield item
        finally:
            self.closed.set()


def catalog_of(model: ChatProvider, *, thinking: bool = True) -> Catalog:
    """A catalog that offers one model, `standard`, which is `model`."""
    offered = OfferedModel(
        name="standard",
        title="Scripted",
        kind="openai-compatible",
        model="scripted-1",
        context_window=4096,
        max_output_tokens=512,
        chat=model,
    )
    return Catalog({"standard": offered}, default="standard", thinking={"standard": thinking})


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


class StartApp(Protocol):
    def __call__(
        self,
        settings: Settings,
        *,
        address: str = "127.0.0.1",
        passwords: Passwords | None = None,
        catalog: Catalog | None = None,
    ) -> Awaitable[httpx.AsyncClient]: ...


@pytest.fixture
async def start_app(
    clock: Clock, catalog: Catalog, server_ids: ServerIds
) -> AsyncIterator[StartApp]:
    """Call with settings to get an HTTP client wired straight to a started app.

    No server and no network: requests go to the app in process. The client's URL is
    the app's public address, so it keeps cookies as a browser there would. The app
    reads the time from `clock`, and takes the ids of the turns and answers it creates
    from `server_ids`. `address` is the network address the app sees the requests come
    from. `passwords` replaces the cheap password hasher, to watch it, and `catalog`
    the fake models.
    """
    fake_models = catalog
    async with AsyncExitStack() as stack:

        async def start(
            settings: Settings,
            *,
            address: str = "127.0.0.1",
            passwords: Passwords | None = None,
            catalog: Catalog | None = None,
        ) -> httpx.AsyncClient:
            app = create_app(
                settings,
                passwords=passwords or cheap_passwords(),
                catalog=catalog or fake_models,
            )
            app.dependency_overrides[get_clock] = lambda: lambda: clock.now
            app.dependency_overrides[get_new_id] = lambda: server_ids
            await stack.enter_async_context(app.router.lifespan_context(app))
            return await stack.enter_async_context(
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app, client=(address, 50_000)),
                    base_url=settings.public_url,
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
    def __call__(self, catalog: Catalog) -> Awaitable[str]: ...


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

    async def start(catalog: Catalog) -> str:
        app = create_app(settings, passwords=cheap_passwords(), catalog=catalog)
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
        self, *script: Any, thinking: bool = True
    ) -> Awaitable[tuple[ScriptedModel, str]]: ...


@pytest.fixture
def served(serve: Serve) -> Served:
    """Call with a script to get a model that follows it, and the address of a served
    app that offers only that model."""

    async def make(*script: Any, thinking: bool = True) -> tuple[ScriptedModel, str]:
        model = ScriptedModel(*script)
        return model, await serve(catalog_of(model, thinking=thinking))

    return make


class Scripted(Protocol):
    def __call__(
        self, *script: Any, thinking: bool = True
    ) -> Awaitable[tuple[ScriptedModel, httpx.AsyncClient]]: ...


@pytest.fixture
def scripted(start_app: StartApp, settings: Settings, migrated: str) -> Scripted:
    """Call with a script to get a model that follows it, and a guest's browser on an
    app that offers only that model."""
    assert migrated

    async def make(*script: Any, thinking: bool = True) -> tuple[ScriptedModel, httpx.AsyncClient]:
        model = ScriptedModel(*script)
        browser = await start_app(settings, catalog=catalog_of(model, thinking=thinking))
        await browser.post("/api/session/guest")
        return model, browser

    return make
