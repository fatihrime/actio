"""Shared fixtures for the API tests.

The Postgres and Valkey servers come from the conftest.py at the repository's root.
"""

import os
import uuid
from collections.abc import AsyncIterator, Iterator

import httpx
import psycopg
import pytest
from psycopg import sql
from pydantic import SecretStr

from actio.app import create_app
from actio.core.settings import Settings

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
def settings(database_url: str, valkey_url: str) -> Settings:
    """Settings that point at this test's own database and the session's Valkey."""
    return Settings(database_url=SecretStr(database_url), valkey_url=SecretStr(valkey_url))


async def _client_for(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    """An HTTP client wired straight to a started app: no server, no network."""
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://testserver") as client,
    ):
        yield client


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    """The app with no database and no Valkey behind it, for what needs neither."""
    nowhere = Settings(
        database_url=SecretStr(NOWHERE_DATABASE), valkey_url=SecretStr(NOWHERE_VALKEY)
    )
    async for client in _client_for(nowhere):
        yield client


@pytest.fixture
async def running(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    """The app on this test's database and the session's Valkey."""
    async for client in _client_for(settings):
        yield client
