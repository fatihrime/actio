"""Shared fixtures for the API tests."""

import os
from collections.abc import AsyncIterator

import httpx
import pytest

from actio.app import create_app


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hide the developer's ACTIO_* variables, so every test starts from the defaults."""
    for name in list(os.environ):
        if name.startswith("ACTIO_"):
            monkeypatch.delenv(name)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    """An HTTP client wired straight to the app: no server, no network."""
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client
