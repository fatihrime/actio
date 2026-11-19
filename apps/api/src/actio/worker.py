"""The worker: the process that runs background jobs (`actio --role worker`).

A job is deferred by its name, in the transaction of the change it follows
(`actio.core.jobs.defer`). Every job a worker can run is listed here, in `listed`, with
what its handler needs bound in. A job deferred under a name that isn't listed fails.

This is a top layer, like `actio.api`: it reads the clock, and passes the time to the
functions it calls as `now`.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial

from actio.core import jobs
from actio.core.resources import Resources, open_resources
from actio.core.settings import Settings
from actio.identity import sessions

logger = logging.getLogger(__name__)

type Clock = Callable[[], datetime]

SWEEP = "sweep-expired"
"""Deletes a batch of expired guests and sessions, every ten minutes. The API also
sweeps after each new guest: a busy installation is kept clear by its newcomers, a
quiet one by this."""


def listed(resources: Resources, *, clock: Clock) -> list[jobs.Job]:
    """Every job a worker runs, each with what it needs."""
    return [jobs.Job(SWEEP, partial(sweep_expired, resources, clock), every="*/10 * * * *")]


async def run(settings: Settings) -> None:
    """Run the jobs until the process is told to stop."""
    async with open_resources(settings) as resources:
        await jobs.work(
            listed(resources, clock=_now), database_url=settings.database_url.get_secret_value()
        )


async def sweep_expired(resources: Resources, clock: Clock) -> None:
    """Delete a batch of expired guests and sessions, as its own unit of work."""
    async with resources.database() as db:
        guests, expired_sessions = await sessions.delete_expired(db, now=clock())
        await db.commit()
    if guests or expired_sessions:
        logger.info("deleted expired guests: %d, expired sessions: %d", guests, expired_sessions)


def _now() -> datetime:
    return datetime.now(UTC)
