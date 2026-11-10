"""What a process holds open while it runs: its database pool and its Valkey client."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine
from valkey.asyncio import Valkey

from actio.core import schema
from actio.core.database import create_engine
from actio.core.settings import Settings


@dataclass(frozen=True, slots=True)
class Resources:
    engine: AsyncEngine
    valkey: Valkey
    schema_head: str
    """The migration this code expects the database to be at."""


@asynccontextmanager
async def open_resources(settings: Settings) -> AsyncIterator[Resources]:
    """Create the process's connections, and close them on the way out.

    Nothing connects here: the pool and the client connect on first use. So a process
    starts even while a dependency is down, and its readiness probe says what is missing.
    """
    resources = Resources(
        engine=create_engine(settings.database_url.get_secret_value()),
        valkey=Valkey.from_url(settings.valkey_url.get_secret_value()),
        schema_head=schema.head(),
    )
    try:
        yield resources
    finally:
        await resources.valkey.aclose()
        await resources.engine.dispose()
