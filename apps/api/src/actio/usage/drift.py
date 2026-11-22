"""How far the usage counters in Valkey are from the ledger in Postgres: a comparison,
which repairs nothing.

The ledger is the truth, and the counters are told of it afterwards (`actio.usage.limits`).
They can come apart: a request that was being written while an account's counters were
lost and rebuilt is in the ledger and in no counter, until the counters are next
rebuilt. `compare` says where, and the worker runs it every hour and says it in its log.

It goes by the counters, not by the ledger: an account without counters has nothing to
compare, since its counters are rebuilt from the ledger when they are next needed. And
it compares the windows the counters have stored, as they are, without a plan: those
that are still open, whatever the plan says now.

Like the rest of `actio.usage`, it never commits, and takes the time as `now`. It never
writes either, to the database or to Valkey.
"""

import logging
import uuid
from collections.abc import AsyncGenerator
from contextlib import aclosing
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from valkey.exceptions import ValkeyError

from actio.usage.limits import to_moment, to_ms
from actio.usage.models import Uncounted, UsageEntry
from actio_quota.engine import ValkeyQuota
from actio_quota.state import BucketState

logger = logging.getLogger(__name__)

ACCOUNTS = 2_000
"""How many accounts one run compares at most. The worker runs one job at a time, and
gives the job in hand 15 seconds at a stop: an account takes about a millisecond or two,
so this many are a few seconds."""


@dataclass(frozen=True, slots=True)
class Apart:
    """A window of an account in which the counters and the ledger disagree."""

    user_id: uuid.UUID
    window: str
    """Its name, such as `session` or `week`."""
    opened_at: datetime
    counters: int
    """The credits the counters say were used in it."""
    ledger: int
    """The credits the ledger says were used in it. When the counters are short: those
    of its rows whose counters were told of them. When they are over: those of all its
    rows."""

    @property
    def short(self) -> int:
        """How many credits the counters miss, which the ledger has."""
        return max(0, self.ledger - self.counters)

    @property
    def over(self) -> int:
        """How many credits the counters have, which the ledger doesn't."""
        return max(0, self.counters - self.ledger)


@dataclass(frozen=True, slots=True)
class Totals:
    """What is apart in windows of one name, of every account together, in credits."""

    short: int = 0
    over: int = 0


@dataclass(frozen=True, slots=True)
class Compared:
    """What one run of `compare` found."""

    accounts: int = 0
    """How many accounts were compared: those with counters, whose comparison ended."""
    apart: tuple[Apart, ...] = ()
    """Each window of each account that is apart."""
    more: bool = False
    """Whether there were more accounts with counters than the run compares. The others
    weren't looked at."""
    valkey_away: bool = False
    """Whether the run stopped because Valkey didn't answer. What it had compared is
    kept, and the account it was at is not counted."""

    def totals(self) -> dict[str, Totals]:
        """What is apart, by the windows' names, in the order of their names.

        Never added up over the names: a row the counters missed is usually short in
        each window that admitted it, the session and the week."""
        short: dict[str, int] = {}
        over: dict[str, int] = {}
        for found in self.apart:
            short[found.window] = short.get(found.window, 0) + found.short
            over[found.window] = over.get(found.window, 0) + found.over
        return {name: Totals(short[name], over[name]) for name in sorted(short)}


async def compare(
    db: AsyncSession, engine: ValkeyQuota, *, now: datetime, limit: int = ACCOUNTS
) -> Compared:
    """Compare the counters of up to `limit` accounts with the ledger, window by window,
    and say which are apart, and by how much.

    The accounts are those that have counters, found in Valkey, in no particular order.
    An id under the engine's prefix that can't be an account's, not being a UUID as the
    API writes one, says so in the log and is left out: the others are compared all the
    same. An account whose counters vanish in the meantime is left out too, and says
    nothing.

    For each account, in this order, which is what makes it sound while answers end
    meanwhile:

    1. Its stored windows are read, and those still open are kept. None open: it is
       compared, and nothing is apart.
    2. Its rows of the ledger since the earliest of those windows opened are read, in
       one statement, each with whether its note (`Uncounted`) is still there.
    3. For each open window, with the rows that it admitted: when the counters hold more
       than all of them, they are over. The ledger is written before the counters are
       told, so whatever the counters held when they were read was in the ledger before
       its rows were read. When they hold less than the rows without a note, they may be
       short: a row without a note was answered by the counters before the ledger was
       read, but perhaps after the counters were. So they are read once more, and short
       only if they still hold less, in the same window.

    A row whose note is still there is waiting to be counted, by its own process or by
    `actio.usage.missed`: the counters may have it or not, and neither is a difference.

    `db` must see what was committed after its transaction began, as Postgres's default,
    READ COMMITTED, does: each statement reads anew. When Valkey doesn't answer, the run
    stops there and says so. It doesn't raise. A database error is raised as it is,
    whatever its kind: only what the engine's own reads raise is taken for Valkey.
    """
    now_ms = to_ms(now)
    accounts = 0
    apart: list[Apart] = []
    try:
        async with aclosing(_users(engine)) as found:
            async for user in found:
                user_id = _account(user)
                if user_id is None:
                    logger.warning(
                        "usage: counters under %r, which can't be an account's id, aren't compared",
                        user,
                    )
                    continue
                if accounts == limit:
                    return Compared(accounts, tuple(apart), more=True)
                stored = await _buckets(engine, user)
                if stored is None:
                    continue  # the counters vanished since the scan found them
                apart += await _apart(db, engine, user, user_id, stored, now_ms)
                accounts += 1
    except _NoAnswerError:
        return Compared(accounts, tuple(apart), valkey_away=True)
    return Compared(accounts, tuple(apart))


class _NoAnswerError(Exception):
    """Valkey didn't answer one of the engine's reads."""


async def _users(engine: ValkeyQuota) -> AsyncGenerator[str]:
    """`engine.users()`, raising `_NoAnswerError` when Valkey doesn't answer the scan.

    What the caller's own loop raises doesn't pass through here: an error of the
    database, between two users, stays what it is."""
    try:
        async with aclosing(engine.users()) as found:
            async for user in found:
                yield user
    except (ValkeyError, OSError) as error:
        raise _NoAnswerError from error


async def _buckets(engine: ValkeyQuota, user: str) -> dict[str, BucketState] | None:
    """`engine.buckets(user)`, raising `_NoAnswerError` when Valkey doesn't answer."""
    try:
        return await engine.buckets(user)
    except (ValkeyError, OSError) as error:
        raise _NoAnswerError from error


async def _apart(
    db: AsyncSession,
    engine: ValkeyQuota,
    user: str,
    user_id: uuid.UUID,
    stored: dict[str, BucketState],
    now_ms: int,
) -> list[Apart]:
    """The windows of one account that are apart, from its buckets as they were read."""
    still_open = {name: bucket for name, bucket in stored.items() if now_ms < bucket.reset_ms}
    if not still_open:
        return []
    # A request is written after the window that admitted it opened: the rows since the
    # earliest open window opened are all that its open windows can have admitted. By
    # one clock, which is what a rebuild rests on too (`ledger.since`): a row dated
    # before its window opened would be left out here, and its credits called over.
    since = to_moment(min(bucket.start_ms for bucket in still_open.values()))
    rows = (
        await db.execute(
            select(UsageEntry.credits, UsageEntry.windows, Uncounted.reservation_id.is_not(None))
            .outerjoin(Uncounted, Uncounted.reservation_id == UsageEntry.reservation_id)
            .where(UsageEntry.user_id == user_id, UsageEntry.occurred_at >= since)
        )
    ).all()
    again: dict[str, BucketState] | None = None
    found: list[Apart] = []
    for name, bucket in sorted(still_open.items()):
        admitted = [
            (credits, noted)
            for credits, windows, noted in rows
            if windows.get(name) == bucket.start_ms
        ]
        total = sum(credits for credits, _ in admitted)
        told = sum(credits for credits, noted in admitted if not noted)
        opened_at = to_moment(bucket.start_ms)
        if bucket.used > total:
            found.append(Apart(user_id, name, opened_at, counters=bucket.used, ledger=total))
        elif bucket.used < told:
            if again is None:
                # Once for the account, however many of its windows ask.
                again = await _buckets(engine, user) or {}
            later = again.get(name)
            if later is not None and later.start_ms == bucket.start_ms and later.used < told:
                found.append(Apart(user_id, name, opened_at, counters=later.used, ledger=told))
    return found


def _account(user: str) -> uuid.UUID | None:
    """The account whose counters these are, by the id the engine keeps them under: the
    text of a UUID, as the API writes it. `None` for anything else. Whether an account
    of that id still exists is not asked: a guest's is deleted after a day, and its rows
    of the ledger and its counters stay."""
    try:
        user_id = uuid.UUID(user)
    except ValueError:
        return None
    return user_id if str(user_id) == user else None
