"""Shared fixtures for the fakellm tests."""

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI

from actio_fakellm.app import create_app


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def now() -> int:
    """A fixed clock reading, so `created` timestamps are predictable too."""
    return 1_700_000_000


@pytest.fixture
def app(now: int) -> FastAPI:
    return create_app(clock=lambda: now)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """An HTTP client wired straight to the app: no server, no network."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://fakellm") as client:
        yield client
