"""The worker: the process that runs background jobs (`actio --role worker`), or the
part of the API's process that does (`actio --role all`, `run_inside`).

A job is deferred by its name, in the transaction of the change it follows
(`actio.core.jobs.defer`). Every job a worker can run is listed here, in `listed`, with
what its handler needs bound in. A job deferred under a name that isn't listed fails.

Most jobs need only the database, and one Valkey. The one that writes a conversation's
title asks a model, so the worker has the models on offer too (`actio.catalog`): its
own in the worker role, the API's in the API's process, where a title waits at the same
gate as the answers.

This is a top layer, like `actio.api`: it reads the clock, and passes the time to the
functions it calls as `now`. Except to the queue's own upkeep (`look_after_the_queue`),
which goes by the database's clock, as the queue's books do.
"""

import asyncio
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from actio.catalog import Catalog, open_catalog
from actio.chat import titles, turns
from actio.core import alive, jobs, schema
from actio.core.resources import Resources, open_resources
from actio.core.settings import Settings
from actio.identity import sessions
from actio.usage import drift, missed

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

COUNT_MISSED = "count-missed-usage"
"""Tells the usage counters in Valkey of a batch of usage they missed, every ten
minutes: rows of the ledger whose settle Valkey didn't answer, or whose process died
before it settled. The process that writes a row tells the counters itself; this is for
what it couldn't."""

LOOK_AFTER_QUEUE = "look-after-the-queue"
"""Looks after what the queue's workers leave behind, every ten minutes: runs again the
jobs a worker that died left `doing`, takes such workers off the queue's list, and
deletes the jobs that failed, were cut off or were cancelled once they are `KEPT_FOR`
old. A job a stop cut off goes back into the queue by itself: this is for the worker
that died before it could say anything."""

COMPARE_USAGE = "compare-usage"
"""Compares the usage counters in Valkey with the ledger in Postgres, every hour at five
past, and says in the log how far apart they are. It repairs nothing: the counters are
put right when they are next rebuilt from the ledger. Not on the ten-minute marks of
the other jobs, and five minutes after `COUNT_MISSED` has counted what it could.

Every hour, not every night: a worker that starts more than ten minutes after a
scheduled time skips it, and an installation on a machine that is off at night would
never run a nightly job."""

APART_SAID = 10
"""How many windows that are apart `compare_usage` names in the log at most, one line
each. The rest it counts, in one more line."""

KEPT_FOR = timedelta(days=7)
"""How long a job that failed, was cut off for the last time or was cancelled stays in
the queue, to be looked at: long enough for a long weekend. One that succeeded is
deleted at once."""

TRIES_AGAIN_AFTER_S = 10.0
"""How long the worker inside the API's process (`run_inside`) waits to try again, after
it couldn't work or ended with an error.

A database that isn't migrated answers at once: without a pause the worker would go
round in a circle, and fill the log with the same line."""


class NotMigratedError(Exception):
    """The database isn't at the migration this code expects, so the worker doesn't
    start: its jobs would run on tables that aren't the ones they were written for."""


def listed(resources: Resources, catalog: Catalog, *, clock: Clock) -> list[jobs.Job]:
    """Every job a worker runs, each with what it needs, and, for the one job that has
    any, the errors of its own that are worth another try."""
    return [
        jobs.Job(SWEEP, partial(sweep_expired, resources, clock), every="*/10 * * * *"),
        jobs.Job(GIVE_UP, partial(give_up_on_stale_turns, resources, clock), every="*/10 * * * *"),
        jobs.Job(COUNT_MISSED, partial(count_missed_usage, resources, clock), every="*/10 * * * *"),
        jobs.Job(
            LOOK_AFTER_QUEUE,
            partial(look_after_the_queue, kept_for=KEPT_FOR),
            every="*/10 * * * *",
            with_queue=True,
        ),
        jobs.Job(COMPARE_USAGE, partial(compare_usage, resources, clock), every="5 * * * *"),
        # Deferred once for a conversation, by the first of its turns that completes. A
        # title its model couldn't give for now is asked for again, later.
        jobs.Job(
            titles.WRITE_TITLE,
            partial(write_title, resources, catalog, clock),
            again=jobs.Again(errors=(titles.NotWrittenError,), waits=titles.ASKED_AGAIN_AFTER),
        ),
    ]


async def run(settings: Settings) -> None:
    """Run the jobs until the process is told to stop.

    The models on offer are the worker's own, read from `settings.models_file`, with
    gates in front of their servers that only this process knows.

    Each time the worker has connected, it first makes sure that the database is
    migrated (`NotMigratedError`). While it works, it touches `settings.alive_file`, if
    there is one, for its health check to look at (`actio.core.alive`)."""
    async with open_resources(settings) as resources, open_catalog(settings) as catalog:
        await _work(settings, resources, catalog, on_signals=True)


async def run_inside(
    settings: Settings,
    resources: Resources,
    catalog: Catalog,
    *,
    on_beat: Callable[[], None] | None = None,
    on_stopped: Callable[[], None] | None = None,
) -> None:
    """Run the jobs inside the API's process (`actio --role all`), on that process's
    `resources` and its `catalog`, until the task this runs in is cancelled: a title then
    waits at the same gate as the API's answers.

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
            await _work(settings, resources, catalog, on_signals=False, on_beat=on_beat)
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
    catalog: Catalog,
    *,
    on_signals: bool,
    on_beat: Callable[[], None] | None = None,
) -> None:
    """One worker on `resources` and `catalog`, with every job of `listed`, which checks
    its database each time it has connected, and while it works touches
    `settings.alive_file`, if the settings name one, and calls `on_beat`, if there is
    one."""
    await jobs.work(
        listed(resources, catalog, clock=_now),
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


async def count_missed_usage(resources: Resources, clock: Clock) -> None:
    """Tell the usage counters of a batch of usage they missed, as its own unit of work."""
    async with resources.database() as db:
        repeated = await missed.count_missed(db, resources.quota, now=clock())
        await db.commit()
    if repeated.taken or repeated.had_already or repeated.not_taken:
        logger.info(
            "usage the counters had missed, counted now: %d, counted before: %d, "
            "left to their next rebuild: %d",
            repeated.taken,
            repeated.had_already,
            repeated.not_taken,
        )
    if repeated.valkey_away:
        logger.warning(
            "usage the counters had missed: Valkey didn't answer, and what is left waits "
            "for the next run"
        )


async def compare_usage(resources: Resources, clock: Clock) -> None:
    """Compare the usage counters with the ledger, and say in the log how far apart they
    are: every run says something, so that a run can be told from none. It writes
    nothing, and commits nothing."""
    async with resources.database() as db:
        compared = await drift.compare(db, resources.quota, now=clock())
    if not compared.apart:
        logger.info(
            "usage counters compared with the ledger, nothing apart, accounts compared: %d",
            compared.accounts,
        )
    else:
        logger.warning(
            "usage counters compared with the ledger, apart in %d of %d accounts compared, "
            "in credits: %s",
            len({found.user_id for found in compared.apart}),
            compared.accounts,
            ", ".join(
                f"{name} {totals.short} short and {totals.over} over"
                for name, totals in compared.totals().items()
            ),
        )
        for found in compared.apart[:APART_SAID]:
            logger.warning(
                "usage counters apart from the ledger: account %s, %s opened at %s, "
                "counted %d, in the ledger %d",
                found.user_id,
                found.window,
                found.opened_at.isoformat(),
                found.counters,
                found.ledger,
            )
        if len(compared.apart) > APART_SAID:
            logger.warning(
                "usage counters apart from the ledger: %d more windows, not named",
                len(compared.apart) - APART_SAID,
            )
    if compared.more:
        logger.warning(
            "usage counters compared with the ledger in %d accounts, as many as a run "
            "compares: the others weren't looked at",
            compared.accounts,
        )
    if compared.valkey_away:
        logger.warning(
            "usage counters compared with the ledger: Valkey didn't answer after %d "
            "accounts, and the next run tries again",
            compared.accounts,
        )


async def write_title(
    resources: Resources, catalog: Catalog, clock: Clock, *, conversation_id: str
) -> None:
    """Ask a model for the conversation's title, and write it, unless someone named the
    conversation meanwhile: see `titles.write`, which also says in the log what kept a
    title from being written. It raises `titles.NotWrittenError` when the model couldn't
    give one for now, and the queue runs the job again, after the wait `listed` gives.
    What its request takes of the model's budget is counted in the buckets that the
    API's turns count in too (`Resources.budgets`)."""
    await titles.write(
        resources.database,
        catalog,
        uuid.UUID(conversation_id),
        now=clock(),
        budgets=resources.budgets,
        clock=clock,
    )


async def look_after_the_queue(queue: jobs.Queue, *, kept_for: timedelta) -> None:
    """See to what the queue's workers left behind, and delete the jobs that ended more
    than `kept_for` ago without succeeding.

    The queue keeps its books by the database's clock, so this takes none: it is the
    one job that isn't given the time."""
    upkeep = await queue.look_after(kept_for=kept_for)
    if upkeep.run_again or upkeep.dead_workers:
        logger.info(
            "jobs left by workers that died, run again: %d, dead workers taken off the list: %d",
            upkeep.run_again,
            upkeep.dead_workers,
        )
    for job in upkeep.given_up:
        logger.warning(
            "a job left by a worker that died had been run again %d times already, and "
            "ends as failed: %s[%d]",
            jobs.RUNS_AGAIN,
            job.name,
            job.id,
        )


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
