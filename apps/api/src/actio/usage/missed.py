"""Usage the counters missed: rows of the ledger they were never told of, told again.

Each row of the ledger is written with a note that the counters in Valkey haven't been
told of it yet (`Uncounted`), and whoever tells them takes the note away. A note that
stays means that the settle after the ledger's commit never reached the counters:
Valkey didn't answer, or the process died in between. `count_missed` settles those
rows again, a batch at a time, and the worker runs it every ten minutes.

Repeating a settle never counts twice. The engine remembers how each request ended for
48 hours, and a request it settled already is answered `settled`, not applied. One it
still holds, or whose hold ran out, is counted now.

Like the rest of `actio.usage`, it never commits, and takes the time as `now`.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from valkey.exceptions import ValkeyError

from actio.usage import ledger
from actio.usage.limits import to_ms
from actio.usage.models import Uncounted, UsageEntry
from actio_quota import Outcome
from actio_quota.engine import ValkeyQuota

logger = logging.getLogger(__name__)

COUNTED_WITHIN = timedelta(minutes=1)
"""How long the process that wrote a row of the ledger gets to tell the counters itself.

Its settle has been answered, or has failed, a few seconds after the ledger's commit:
the Valkey client makes no retries, and gives up after two seconds. A run that settled
a row first would do no harm to the count, but the request's own settle would then be
answered "not applied", and lose the warning a turn says in its stream when it passes a
share of a window. So the rows younger than this are left to their own process."""

BATCH = 100
"""How many rows one run settles again at most."""


@dataclass(frozen=True, slots=True)
class Repeated:
    """What one run of `count_missed` did with the rows it settled again."""

    taken: int = 0
    """Rows the counters took now: they had missed them."""
    had_already: int = 0
    """Rows the counters had counted already. Only the note that they had was missing:
    the process that settled them couldn't take it away."""
    not_taken: int = 0
    """Rows the counters could no longer take: the account's counters were lost and
    rebuilt since, or they had forgotten the request, or it had been released. Each is
    counted when the account's counters are next rebuilt from the ledger, as long as
    its window is still open. And rows the engine refused to take at all."""
    valkey_away: bool = False
    """Whether the run stopped because Valkey didn't answer. The rows it didn't get to
    wait for the next run."""


async def count_missed(
    db: AsyncSession, engine: ValkeyQuota, *, now: datetime, limit: int = BATCH
) -> Repeated:
    """Settle again the rows of the ledger whose notes are older than `COUNTED_WITHIN`,
    oldest first, up to `limit`, and take away the note of each the counters answered.

    Any answer of the engine counts as told: after `settled`, `released` or `unknown`
    there is nothing a repeat could add. Nor is there for a row the engine refuses to
    take at all: its note goes too, and the log names it. When Valkey doesn't answer,
    the run stops at that row: each further try would wait out its timeout. That row and
    the rest of the batch keep their notes, for the next run.

    It never waits for a row's lock: a note another transaction holds, as when its own
    process is taking it away at that moment, is skipped. Any number of runs can go at
    once. A database error is raised as it is.
    """
    # The batch is taken and held by a statement of its own, and the notes taken away in
    # the next: as a subquery of the delete, a plan could read it again for each row,
    # and go past `limit`. Only the notes are held: the ledger's rows are never changed.
    batch = await db.execute(
        select(Uncounted.reservation_id, UsageEntry.user_id, UsageEntry.credits)
        .join(UsageEntry, UsageEntry.reservation_id == Uncounted.reservation_id)
        .where(UsageEntry.occurred_at <= now - COUNTED_WITHIN)
        .order_by(UsageEntry.occurred_at, Uncounted.reservation_id)
        .limit(limit)
        .with_for_update(of=Uncounted, skip_locked=True)
    )
    told: list[uuid.UUID] = []
    taken = had_already = not_taken = 0
    valkey_away = False
    for reservation_id, user_id, credits in batch.all():
        try:
            finished = await engine.settle(
                str(user_id), str(reservation_id), credits, now_ms=to_ms(now)
            )
        except (ValkeyError, OSError):
            valkey_away = True
            break
        except ValueError as refused:
            # The engine takes no such row: more credits than it counts exactly, which
            # takes a model's server that reports billions of tokens for one answer. It
            # would refuse at every run, and the oldest row comes first in every batch:
            # a run that failed here would never get to the rows behind it.
            logger.warning("usage: reservation %s can't be counted (%s)", reservation_id, refused)
            told.append(reservation_id)
            not_taken += 1
            continue
        told.append(reservation_id)
        if finished.applied:
            taken += 1
        elif finished.outcome is Outcome.SETTLED:
            had_already += 1
        else:
            not_taken += 1
    if told:
        await ledger.counted(db, told)
    return Repeated(taken, had_already, not_taken, valkey_away)
