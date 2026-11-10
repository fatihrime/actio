"""Liveness and readiness probes.

- `/api/healthz` answers whenever the process can serve requests. Restart it if not.
- `/api/readyz` says whether the process can do its work: the database is reachable
  and migrated, and Valkey answers. Send it no traffic while it says no.

Both live under /api like every other API route, so they also work through the proxy.
"""

import asyncio
import logging
from typing import Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError
from valkey.exceptions import ValkeyError

from actio import __version__
from actio.core import schema
from actio.core.resources import Resources

CHECK_TIMEOUT_S = 2.0
"""How long each dependency gets to answer a readiness check."""

logger = logging.getLogger(__name__)
router = APIRouter(tags=["health"])


class Health(BaseModel):
    status: Literal["ok"]
    version: str


class Readiness(BaseModel):
    """What is missing, by name. Never why: a failure's details go to the log, because
    they can name hosts and users."""

    status: Literal["ready", "unavailable"]
    database: Literal["ok", "unreachable", "migrations pending"]
    valkey: Literal["ok", "unreachable"]


@router.get("/api/healthz")
async def healthz() -> Health:
    """Answers whenever the process can serve requests."""
    return Health(status="ok", version=__version__)


@router.get("/api/readyz", responses={503: {"model": Readiness}})
async def readyz(request: Request, response: Response) -> Readiness:
    """200 when the database is reachable and migrated and Valkey answers; 503 otherwise."""
    resources: Resources = request.app.state.resources
    database, valkey = await asyncio.gather(_database(resources), _valkey(resources))
    ready = database == "ok" and valkey == "ok"
    if not ready:
        response.status_code = 503
    return Readiness(status="ready" if ready else "unavailable", database=database, valkey=valkey)


async def _database(resources: Resources) -> Literal["ok", "unreachable", "migrations pending"]:
    try:
        async with asyncio.timeout(CHECK_TIMEOUT_S), resources.engine.connect() as connection:
            revision = await schema.current(connection)
    except (SQLAlchemyError, OSError, TimeoutError) as error:
        logger.warning("readiness: the database is unreachable (%s)", type(error).__name__)
        return "unreachable"
    if revision != resources.schema_head:
        logger.warning(
            "readiness: the database is at migration %s, this code needs %s",
            revision,
            resources.schema_head,
        )
        return "migrations pending"
    return "ok"


async def _valkey(resources: Resources) -> Literal["ok", "unreachable"]:
    try:
        async with asyncio.timeout(CHECK_TIMEOUT_S):
            await resources.valkey.ping()
    except (ValkeyError, OSError, TimeoutError) as error:
        logger.warning("readiness: Valkey is unreachable (%s)", type(error).__name__)
        return "unreachable"
    return "ok"
