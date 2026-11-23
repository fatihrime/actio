"""Liveness and readiness probes.

- `/api/healthz` answers whenever the process can serve requests. Restart it if not.
- `/api/readyz` says whether the process can do its work: the database is reachable
  and migrated, and Valkey answers. In a process that runs the worker inside it
  (`actio --role all`), also that the worker works. Send it no traffic while it says no.

Whether the worker works, the app knows: it puts on its state, as `worker_works`, what
to call to ask, or `None` when it runs no worker. The worker works from the moment it
says so until `alive.STALE_AFTER_S` after it last did, as the worker's own container is
healthy while its file is that fresh (`actio.core.alive`), or until it ends with an
error or finds that it can't work, if that comes first.

Both live under /api like every other API route, so they also work through the proxy.
"""

import asyncio
import logging
from collections.abc import Callable
from typing import Literal

from fastapi import APIRouter, Request, Response
from pydantic.json_schema import SkipJsonSchema
from sqlalchemy.exc import SQLAlchemyError
from valkey.exceptions import ValkeyError

from actio import __version__
from actio.api.described import Described
from actio.core import schema
from actio.core.resources import Resources

CHECK_TIMEOUT_S = 2.0
"""How long each dependency gets to answer a readiness check."""

logger = logging.getLogger(__name__)
router = APIRouter(tags=["health"])


class Health(Described):
    status: Literal["ok"]
    version: str


class Readiness(Described):
    """What is missing, by name. Never why: a failure's details go to the log, because
    they can name hosts and users.

    `worker` is there only in a process that runs the worker inside it, as
    `actio --role all` does. It is `not working` before the worker has said that it
    works, from the moment the worker ends with an error or can't work until it says so
    again, and once it hasn't said so for 20 seconds, whatever it is doing."""

    status: Literal["ready", "unavailable"]
    database: Literal["ok", "unreachable", "migrations pending"]
    valkey: Literal["ok", "unreachable"]
    # Left out of the answer when there is no worker, never sent as null: the
    # description says so.
    worker: Literal["ok", "not working"] | SkipJsonSchema[None] = None


@router.get("/api/healthz")
async def healthz() -> Health:
    """Answers whenever the process can serve requests."""
    return Health(status="ok", version=__version__)


@router.get(
    "/api/readyz",
    responses={503: {"model": Readiness}},
    # A process that runs no worker answers without the key, as it did before there was one.
    response_model_exclude_none=True,
)
async def readyz(request: Request, response: Response) -> Readiness:
    """200 when the database is reachable and migrated, Valkey answers, and the worker
    works, where this process runs one inside it; 503 otherwise."""
    resources: Resources = request.app.state.resources
    database, valkey = await asyncio.gather(_database(resources), _valkey(resources))
    worker = _worker(request.app.state.worker_works)
    ready = database == "ok" and valkey == "ok" and worker != "not working"
    if not ready:
        response.status_code = 503
    return Readiness(
        status="ready" if ready else "unavailable",
        database=database,
        valkey=valkey,
        worker=worker,
    )


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


def _worker(works: Callable[[], bool] | None) -> Literal["ok", "not working"] | None:
    """`None` in a process that runs no worker. Why one doesn't work, the worker's own
    lines in the log say."""
    if works is None:
        return None
    if not works():
        logger.warning("readiness: the worker inside this process isn't working")
        return "not working"
    return "ok"
