"""Shared fixtures for the API tests.

The Postgres and Valkey servers come from the conftest.py at the repository's root.
"""

import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Iterator
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from typing import Protocol

import argon2.profiles
import httpx
import psycopg
import pytest
from argon2 import PasswordHasher
from psycopg import sql
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from actio.api.dependencies import get_now
from actio.app import create_app
from actio.core import schema
from actio.core.database import create_engine, create_session_factory
from actio.core.settings import Settings
from actio.identity.passwords import Passwords

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


class StartApp(Protocol):
    def __call__(
        self, settings: Settings, *, address: str = "127.0.0.1", passwords: Passwords | None = None
    ) -> Awaitable[httpx.AsyncClient]: ...


@pytest.fixture
async def start_app(clock: Clock) -> AsyncIterator[StartApp]:
    """Call with settings to get an HTTP client wired straight to a started app.

    No server and no network: requests go to the app in process. The client's URL is
    the app's public address, so it keeps cookies as a browser there would, and the app
    reads the time from `clock`. `address` is the network address the app sees the
    requests come from. `passwords` replaces the cheap password hasher, to watch it.
    """
    async with AsyncExitStack() as stack:

        async def start(
            settings: Settings, *, address: str = "127.0.0.1", passwords: Passwords | None = None
        ) -> httpx.AsyncClient:
            app = create_app(settings, passwords=passwords or cheap_passwords())
            app.dependency_overrides[get_now] = lambda: clock.now
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
