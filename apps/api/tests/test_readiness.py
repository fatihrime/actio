"""The readiness probe, against a real Postgres and a real Valkey."""

from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from actio.core import schema
from actio.core.settings import Settings

pytestmark = pytest.mark.anyio

NOWHERE_DATABASE = SecretStr("postgresql://actio:s3cret-db@127.0.0.1:1/actio")
NOWHERE_VALKEY = SecretStr("valkey://:s3cret-kv@127.0.0.1:1/0")


async def test_ready_when_the_database_is_migrated_and_valkey_answers(
    api: httpx.AsyncClient,
) -> None:
    response = await api.get("/api/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "database": "ok", "valkey": "ok"}


async def test_a_database_that_was_never_migrated_is_not_ready(running: httpx.AsyncClient) -> None:
    response = await running.get("/api/readyz")

    assert response.status_code == 503
    assert response.json() == {
        "status": "unavailable",
        "database": "migrations pending",
        "valkey": "ok",
    }


async def test_a_database_behind_the_code_is_not_ready(
    running: httpx.AsyncClient, database_url: str
) -> None:
    """After a deploy whose migration hasn't run yet."""
    schema.upgrade(database_url)
    schema.downgrade(database_url, "0001")

    response = await running.get("/api/readyz")

    assert (response.status_code, response.json()["database"]) == (503, "migrations pending")


async def test_an_unreachable_database_is_named_and_nothing_else_is_said(
    start_app: Any, settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    broken = await start_app(settings.model_copy(update={"database_url": NOWHERE_DATABASE}))
    response = await broken.get("/api/readyz")

    assert response.status_code == 503
    assert response.json() == {
        "status": "unavailable",
        "database": "unreachable",
        "valkey": "ok",
    }
    # Why it failed goes to the log, without the URL or its password.
    assert "the database is unreachable" in caplog.text
    assert "s3cret" not in response.text + caplog.text


async def test_an_unreachable_valkey_is_named(
    start_app: Any, settings: Settings, migrated: str
) -> None:
    assert migrated
    broken = await start_app(settings.model_copy(update={"valkey_url": NOWHERE_VALKEY}))
    response = await broken.get("/api/readyz")

    assert response.status_code == 503
    assert response.json() == {
        "status": "unavailable",
        "database": "ok",
        "valkey": "unreachable",
    }
    assert "s3cret" not in response.text


async def test_the_process_stays_alive_while_its_dependencies_are_down(
    client: httpx.AsyncClient,
) -> None:
    """Liveness and readiness answer different questions: restarting won't fix a database."""
    assert (await client.get("/api/healthz")).status_code == 200
    assert (await client.get("/api/readyz")).status_code == 503


async def test_the_probe_is_in_the_api_description(client: httpx.AsyncClient) -> None:
    paths = (await client.get("/api/openapi.json")).json()["paths"]

    assert set(paths["/api/readyz"]["get"]["responses"]) == {"200", "503"}
