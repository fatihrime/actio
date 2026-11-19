"""The worker: the process that runs background jobs (`actio --role worker`), or the
part of the API's process that does (`actio --role all`, `run_inside`).

A job is deferred by its name, in the transaction of the change it follows
(`actio.core.jobs.defer`). Every job a worker can run is listed here, in `listed`, with
what its handler needs bound in. A job deferred under a name that isn't listed fails.

This is a top layer, like `actio.api`: it reads the clock, and passes the time to the
functions it calls as `now`.
"""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from actio.chat import turns
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

GIVE_UP = "give-up-on-stale-turns"
"""Gives up on a batch of stale turns, those a process that died left `running`, in
every conversation, every ten minutes. Whoever looks at a conversation still gives up
on its turn at once: this is for the conversations nobody looks at."""

TRIES_AGAIN_AFTER_S = 10.0
"""How long the worker inside the API's process (`run_inside`) waits to try again, after
it couldn't work or ended with an error.

A database that isn't migrated answers at once: without a pause the worker would go
round in a circle, and fill the log with the same line."""


class NotMigratedError(Exception):
    """The database isn't at the migration this code expects, so the worker doesn't
    start: its jobs would run on tables that aren't the ones they were written for."""


def listed(resources: Resources, *, clock: Clock) -> list[jobs.Job]:
    """Every job a worker runs, each with what it needs."""
    return [
        jobs.Job(SWEEP, partial(sweep_expired, resources, clock), every="*/10 * * * *"),
        jobs.Job(GIVE_UP, partial(give_up_on_stale_turns, resources, clock), every="*/10 * * * *"),
    ]


async def run(settings: Settings) -> None:
    """Run the jobs until the process is told to stop.

    Each time the worker has connected, it first makes sure that the database is
    migrated (`NotMigratedError`). While it works, it touches `settings.alive_file`, if
    there is one, for its health check to look at (`actio.core.alive`)."""
    async with open_resources(settings) as resources:
        await _work(settings, resources, on_signals=True)


async def run_inside(
    settings: Settings,
    resources: Resources,
    *,
    on_beat: Callable[[], None] | None = None,
    on_stopped: Callable[[], None] | None = None,
) -> None:
    """Run the jobs inside the API's process (`actio --role all`), on that process's
    `resources`, until the task this runs in is cancelled.

    `on_beat` is called each time the worker says that it works, beside the touch of
    `settings.alive_file`, which is as in `run`. Each is done whatever the other does.
    `on_stopped` is called each time the worker has ended with an error or couldn't
    work, for whatever its beats said is over: before it waits to try again, or before
    it ends, when it had been told to stop. Not when it ends by the cancellation itself.

    It works as `run` does, with two differences:

    - it leaves the process's signals alone. Whoever runs it hears them, and tells it to
      stop by cancelling its task, once: a second cancellation would cut off the job in
      hand;
    - nothing but that ends it. A worker that can't work ends its own process, which is
      started again; this one's process serves the API, which stays up without its
      database. So it says in the log why it can't work, in one line for a database it
      can't reach or that isn't migrated, with the trace for anything else, and tries
      again after `TRIES_AGAIN_AFTER_S`. Once it was told to stop, it ends instead: an
      error can come in place of the cancellation, as Procrastinate's does when it can't
      take its worker off its list.
    """
    while True:
        try:
            await _work(settings, resources, on_signals=False, on_beat=on_beat)
        except (jobs.DatabaseUnreachableError, NotMigratedError) as error:
            # Its own words say what is wrong, and what to do about it: no trace.
            logger.error("The worker can't work: %s.%s", error, _then("It tries again"))
        except Exception:
            logger.exception("The worker ended with an error.%s", _then("It starts again"))
        else:
            return  # `work` returns only when a signal told it to stop, and here it hears none
        if on_stopped is not None:
            # Its last beat may be fresh: a run beats as soon as it has connected.
            on_stopped()
        if _told_to_stop():
            return
        await asyncio.sleep(TRIES_AGAIN_AFTER_S)


async def _work(
    settings: Settings,
    resources: Resources,
    *,
    on_signals: bool,
    on_beat: Callable[[], None] | None = None,
) -> None:
    """One worker on `resources`, with every job of `listed`, which checks its database
    each time it has connected, and while it works touches `settings.alive_file`, if
    the settings name one, and calls `on_beat`, if there is one."""
    await jobs.work(
        listed(resources, clock=_now),
        database_url=settings.database_url.get_secret_value(),
        on_signals=on_signals,
        check=partial(_migrated, resources),
        on_beat=_beat(settings.alive_file, on_beat),
    )


def _beat(alive_file: Path | None, also: Callable[[], None] | None) -> Callable[[], None] | None:
    """What the worker does each time it says that it works: call `also`, and touch
    `alive_file`. `None` when there is neither.

    Each is done whatever the other does: one that fails doesn't keep the other from
    being said. What failed is raised all the same, for `jobs.work` to say in the log."""
    if alive_file is None:
        return also
    touch = partial(alive.touch, alive_file)
    if also is None:
        return touch

    def both() -> None:
        try:
            also()
        finally:
            touch()

    return both


def _told_to_stop() -> bool:
    """Whether the task this runs in was asked to cancel."""
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


def _then(again: str) -> str:
    """What the worker inside the API does after it couldn't work: `again`, after the
    pause, or nothing, once it was told to stop."""
    return "" if _told_to_stop() else f" {again} in {TRIES_AGAIN_AFTER_S:g} s."


async def sweep_expired(resources: Resources, clock: Clock) -> None:
    """Delete a batch of expired guests and sessions, as its own unit of work."""
    async with resources.database() as db:
        guests, expired_sessions = await sessions.delete_expired(db, now=clock())
        await db.commit()
    if guests or expired_sessions:
        logger.info("deleted expired guests: %d, expired sessions: %d", guests, expired_sessions)


async def give_up_on_stale_turns(resources: Resources, clock: Clock) -> None:
    """Give up on a batch of stale turns, in every conversation, as its own unit of work."""
    async with resources.database() as db:
        given_up = await turns.give_up_on_stale_everywhere(db, now=clock())
        await db.commit()
    if given_up:
        logger.info("gave up on stale turns: %d", len(given_up))


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
