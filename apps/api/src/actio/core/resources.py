"""What a process holds open while it runs: its database pool and its Valkey client,
and what counts in Valkey through that client."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from valkey.asyncio import Valkey

from actio.core import schema
from actio.core.database import create_engine, create_session_factory
from actio.core.rate_limit import RateLimiter
from actio.core.settings import Settings
from actio_quota.engine import ValkeyQuota

VALKEY_TIMEOUT_S = 2.0
"""How long Valkey gets to accept a connection, and to answer a command. Its commands
here are short, so a longer silence means it is unreachable: better to say so than to
keep a request waiting."""


@dataclass(frozen=True, slots=True)
class Resources:
    engine: AsyncEngine
    database: async_sessionmaker[AsyncSession]
    """Call it for a database session: `async with resources.database() as db:`."""
    valkey: Valkey
    limiter: RateLimiter
    quota: ValkeyQuota
    """The usage limits' counters (`actio.usage`). Built here once, for the API that
    counts every answer and for the worker that counts again what was missed, so that
    the two can't count under different keys."""
    schema_head: str
    """The migration this code expects the database to be at."""


@asynccontextmanager
async def open_resources(settings: Settings) -> AsyncIterator[Resources]:
    """Create the process's connections, and close them on the way out.

    Nothing connects here: the pool and the client connect on first use. So a process
    starts even while a dependency is down, and its readiness probe says what is missing.
    """
    engine = create_engine(settings.database_url.get_secret_value())
    valkey = Valkey.from_url(
        settings.valkey_url.get_secret_value(),
        socket_connect_timeout=VALKEY_TIMEOUT_S,
        socket_timeout=VALKEY_TIMEOUT_S,
    )
    resources = Resources(
        engine=engine,
        database=create_session_factory(engine),
        valkey=valkey,
        limiter=RateLimiter(valkey, prefix=f"{settings.valkey_prefix}:rl"),
        # Beside the rate limits' keys, under the same prefix.
        quota=ValkeyQuota(valkey, prefix=f"{settings.valkey_prefix}:q"),
        schema_head=schema.head(),
    )
    try:
        yield resources
    finally:
        await resources.valkey.aclose()
        await resources.engine.dispose()
