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

from sqlalchemy.exc import SQLAlchemyError

from actio.core import alive, jobs, schema
from actio.core.resources import Resources, open_resources
from actio.core.settings import Settings
from actio.identity import sessions

logger = logging.getLogger(__name__)

type Clock = Callable[[], datetime]

SWEEP = "sweep-expired"
"""Deletes a batch of expired guests and sessions, every ten minutes. The API also
sweeps after each new guest: a busy installation is kept clear by its newcomers, a
quiet one by this."""


class NotMigratedError(Exception):
    """The database isn't at the migration this code expects, so the worker doesn't
    start: its jobs would run on tables that aren't the ones they were written for."""


def listed(resources: Resources, *, clock: Clock) -> list[jobs.Job]:
    """Every job a worker runs, each with what it needs."""
    return [jobs.Job(SWEEP, partial(sweep_expired, resources, clock), every="*/10 * * * *")]


async def run(settings: Settings) -> None:
    """Run the jobs until the process is told to stop.

    Each time the worker has connected, it first makes sure that the database is
    migrated (`NotMigratedError`). While it works, it touches `settings.alive_file`, if
    there is one, for its health check to look at (`actio.core.alive`)."""
    async with open_resources(settings) as resources:
        await jobs.work(
            listed(resources, clock=_now),
            database_url=settings.database_url.get_secret_value(),
            check=partial(_migrated, resources),
            on_beat=(
                partial(alive.touch, settings.alive_file)
                if settings.alive_file is not None
                else None
            ),
        )


async def sweep_expired(resources: Resources, clock: Clock) -> None:
    """Delete a batch of expired guests and sessions, as its own unit of work."""
    async with resources.database() as db:
        guests, expired_sessions = await sessions.delete_expired(db, now=clock())
        await db.commit()
    if guests or expired_sessions:
        logger.info("deleted expired guests: %d, expired sessions: %d", guests, expired_sessions)


async def _migrated(resources: Resources) -> None:
    """Raise `NotMigratedError` unless the database is at the migration this code
    expects, as the API's readiness probe asks.

    Raises `jobs.DatabaseUnreachableError` when it can't ask: the database went away
    again in the moment after the worker had connected."""
    try:
        async with resources.engine.connect() as connection:
            revision = await schema.current(connection)
    except (SQLAlchemyError, OSError) as error:
        # The driver's own words, in one line: SQLAlchemy adds a second one about itself.
        said = str(getattr(error, "orig", None) or error).splitlines()[0]
        raise jobs.DatabaseUnreachableError(
            f"couldn't read the database's migration: {said}"
        ) from error
    needed = resources.schema_head
    if revision == needed:
        return
    if revision is None:
        at, to_do = "was never migrated", 'run "actio --role migrate" first'
    elif schema.knows(revision):
        at, to_do = f"is at migration {revision}", 'run "actio --role migrate" first'
    else:
        # Applied by newer code than this. Migrating with this code would not help.
        at, to_do = f"is at migration {revision}", "the database is ahead of this code"
    raise NotMigratedError(f"the database {at}, and this code needs {needed}: {to_do}")


def _now() -> datetime:
    return datetime.now(UTC)
