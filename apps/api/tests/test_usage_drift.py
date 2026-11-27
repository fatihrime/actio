"""The comparison of the usage counters with the ledger (`actio.usage.drift`), against a
real Valkey and a real Postgres.

The engine's own reads are tested in `packages/quota`. Here: which accounts are
compared, which of their windows are apart and by how much, what isn't a difference,
what an answer that ends while a run reads changes, and that a run writes nothing.

In these tests a credit is a token, read or written, so the numbers can be followed. An
answer here reads 100 tokens.
"""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from valkey.asyncio import Valkey
from valkey.exceptions import ConnectionError as ValkeyConnectionError

from actio.core.database import create_engine, create_session_factory
from actio.core.ids import new_id
from actio.usage import drift
from actio.usage.drift import Apart, Compared, Totals
from actio.usage.limits import Account, Reserved, UsageLimits, to_ms
from actio.usage.missed import COUNTED_WITHIN, Repeated, count_missed
from actio.usage.models import Uncounted, UsageEntry
from actio.usage.plans import Plans
from actio_quota import LedgerEntry
from actio_quota.engine import ValkeyQuota
from actio_quota.state import BucketState

pytestmark = pytest.mark.anyio

NOON = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)  # a Tuesday
MONDAY = datetime(2026, 11, 9, 0, 0, tzinfo=UTC)  # when that week opened
HOLD = timedelta(minutes=10)
DAY_MS = 24 * 3_600_000


@dataclass
class Counters:
    """Usage limits with keys of their own on the session's Valkey server, their engine,
    and a way to lose what Valkey holds for them."""

    limits: UsageLimits
    engine: ValkeyQuota
    valkey: Valkey
    prefix: str

    async def lose(self) -> None:
        """What a restart does to a Valkey that keeps nothing on disk."""
        await self.valkey.delete(*await self.valkey.keys(f"{self.prefix}:*"))


def counters_under(valkey: Valkey, prefix: str, plans: Plans) -> Counters:
    engine = ValkeyQuota(valkey, prefix=prefix)
    return Counters(UsageLimits(engine, plans), engine, valkey, prefix)


@pytest.fixture
async def valkey(valkey_url: str) -> AsyncIterator[Valkey]:
    async with Valkey.from_url(valkey_url) as client:
        yield client


@pytest.fixture
def counters(valkey: Valkey, plans_with: Any) -> Counters:
    """Limits far out of reach, at a credit a token."""
    return counters_under(valkey, f"test-{uuid.uuid4().hex[:16]}", plans_with())


@pytest.fixture
async def nowhere() -> AsyncIterator[Valkey]:
    """A Valkey that doesn't answer: nothing listens on port 1."""
    async with Valkey.from_url("valkey://127.0.0.1:1/0", socket_connect_timeout=0.5) as client:
        yield client


@pytest.fixture
def guest() -> Account:
    return Account(new_id(), is_guest=True, own_plan=None)


@pytest.fixture
async def elsewhere(migrated: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Sessions on this test's database through a pool of their own: what another
    process commits while a run reads."""
    engine = create_engine(migrated)
    yield create_session_factory(engine)
    await engine.dispose()


async def held(
    counters: Counters, db: AsyncSession, account: Account, *, now: datetime = NOON
) -> Reserved:
    """An answer being written: the credits it may cost are held."""
    return await counters.limits.reserve(
        db,
        account,
        new_id(),
        model="standard",
        prompt_tokens=80,
        max_output_tokens=100,
        hold_for=HOLD,
        now=now,
    )


async def ended(
    counters: Counters,
    db: AsyncSession,
    reserved: Reserved,
    *,
    written: int,
    now: datetime = NOON,
) -> None:
    """The answer ends as a turn's does: what it used is recorded and committed, the
    counters are told, and the note that they weren't is taken away."""
    credits = counters.limits.record(
        db, reserved, input_tokens=100, output_tokens=written, estimated=False, now=now
    )
    await db.commit()
    assert await counters.limits.settle(reserved, credits, now=now) is not None
    await counters.limits.counted(db, reserved)
    await db.commit()


async def answered(
    counters: Counters,
    db: AsyncSession,
    account: Account,
    *,
    written: int,
    now: datetime = NOON,
) -> None:
    """An answer, from the moment its credits are held to its end. It costs 100 credits
    more than it wrote."""
    await ended(counters, db, await held(counters, db, account, now=now), written=written, now=now)


async def written_while_lost(counters: Counters, db: AsyncSession, account: Account) -> None:
    """The account's first answer costs 150. Its second is being written when the
    counters are lost, and another question rebuilds them, from the ledger, which has the
    first alone. Then the second ends, for 200: its settle is answered `unknown`, and its
    note goes. The counters miss it, in the session and in the week."""
    await answered(counters, db, account, written=50)
    being_written = await held(counters, db, account)
    await counters.lose()
    await held(counters, db, account)
    await ended(counters, db, being_written, written=100)


async def used(counters: Counters, account: Account) -> dict[str, int]:
    """What the account's counters say was used, in each of their windows."""
    stored = await counters.engine.buckets(str(account.user_id))
    assert stored is not None
    return {name: bucket.used for name, bucket in stored.items()}


async def uncounted(db: AsyncSession) -> set[uuid.UUID]:
    """The rows of the ledger that are noted as not counted yet."""
    return set((await db.scalars(select(Uncounted.reservation_id))).all())


async def in_the_ledger(db: AsyncSession) -> set[tuple[uuid.UUID, int]]:
    """The rows of the ledger, each its id and its credits."""
    rows = await db.execute(select(UsageEntry.reservation_id, UsageEntry.credits))
    return {(reservation_id, credits) for reservation_id, credits in rows}


@contextmanager
def statements(db: AsyncSession) -> Iterator[list[str]]:
    """The statements `db` sends to the database meanwhile, as they are sent."""
    sent: list[str] = []

    def note(*args: Any) -> None:
        sent.append(args[2])  # before_cursor_execute(conn, cursor, statement, …)

    bind = db.get_bind()
    event.listen(bind, "before_cursor_execute", note)
    try:
        yield sent
    finally:
        event.remove(bind, "before_cursor_execute", note)


class Watched(ValkeyQuota):
    """The engine of `counters`, which does something of the test's before or after each
    read of an account's buckets, and notes whose it read."""

    def __init__(
        self,
        counters: Counters,
        *,
        before: Callable[[str], Awaitable[None]] | None = None,
        after: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__(counters.valkey, prefix=counters.prefix)
        self.before = before
        self.after = after
        self.read: list[str] = []

    async def buckets(self, user_id: str) -> dict[str, BucketState] | None:
        if self.before is not None:
            await self.before(user_id)
        stored = await super().buckets(user_id)
        self.read.append(user_id)
        if self.after is not None:
            await self.after(user_id)
        return stored


# --- What is apart, and what isn't -----------------------------------------------------


async def test_answers_that_were_counted_are_not_apart(
    counters: Counters, db: AsyncSession, guest: Account
) -> None:
    await answered(counters, db, guest, written=50)
    await answered(counters, db, guest, written=70)

    compared = await drift.compare(db, counters.engine, now=NOON)

    assert compared == Compared(accounts=1)
    assert await used(counters, guest) == {"session": 320, "week": 320}


async def test_an_answer_whose_settle_valkey_missed_is_not_apart_before_or_after_it_is_counted(
    counters: Counters, nowhere: Valkey, db: AsyncSession, guest: Account
) -> None:
    """Its note is still there: it waits to be counted, and the counters may have it or
    not. Here they don't, until the worker's job counts it."""
    away = UsageLimits(ValkeyQuota(nowhere, prefix="away"), counters.limits.plans)
    await answered(counters, db, guest, written=50)
    missed = await held(counters, db, guest)
    credits = counters.limits.record(
        db, missed, input_tokens=100, output_tokens=100, estimated=False, now=NOON
    )
    await db.commit()
    async with asyncio.timeout(10):
        assert await away.settle(missed, credits, now=NOON) is None
    later = NOON + COUNTED_WITHIN

    before = await drift.compare(db, counters.engine, now=NOON)
    held_back = await used(counters, guest)
    assert await count_missed(db, counters.engine, now=later) == Repeated(taken=1)
    await db.commit()
    after = await drift.compare(db, counters.engine, now=later)

    assert before == Compared(accounts=1)
    assert held_back == {"session": 150, "week": 150}
    assert after == Compared(accounts=1)
    assert await used(counters, guest) == {"session": 350, "week": 350}


async def test_an_answer_counted_whose_note_wasnt_taken_away_is_not_apart(
    counters: Counters, db: AsyncSession, guest: Account
) -> None:
    """In particular not over: the counters have it, and the ledger has it as waiting to
    be counted."""
    reserved = await held(counters, db, guest)
    credits = counters.limits.record(
        db, reserved, input_tokens=100, output_tokens=100, estimated=False, now=NOON
    )
    await db.commit()
    assert await counters.limits.settle(reserved, credits, now=NOON) == ()

    compared = await drift.compare(db, counters.engine, now=NOON)

    assert compared == Compared(accounts=1)
    assert await uncounted(db) == {reserved.id}
    assert await used(counters, guest) == {"session": 200, "week": 200}


async def test_an_answer_written_while_the_counters_were_lost_and_rebuilt_is_short_in_both_windows(
    counters: Counters, db: AsyncSession, guest: Account
) -> None:
    """What the comparison is for: requests in flight while the counters are lost go
    uncounted, in the account's favour, and are in the ledger (ADR 0006). Once the
    counters are next rebuilt, nothing is apart."""
    await written_while_lost(counters, db, guest)

    compared = await drift.compare(db, counters.engine, now=NOON)
    await counters.lose()
    await counters.limits.status(db, guest, now=NOON)  # rebuilds them, from both answers
    rebuilt = await drift.compare(db, counters.engine, now=NOON)

    assert compared == Compared(
        accounts=1,
        apart=(
            Apart(guest.user_id, "session", NOON, counters=150, ledger=350),
            Apart(guest.user_id, "week", MONDAY, counters=150, ledger=350),
        ),
    )
    assert [(found.short, found.over) for found in compared.apart] == [(200, 0), (200, 0)]
    assert compared.totals() == {"session": Totals(short=200), "week": Totals(short=200)}
    assert rebuilt == Compared(accounts=1)


async def test_when_the_answer_written_meanwhile_was_the_first_of_the_week_it_is_seen_after_the_next(
    counters: Counters, db: AsyncSession, guest: Account
) -> None:
    """The rebuild stores no window: the ledger had nothing. So no window is open to
    compare until the account's next answer opens them again. Then the week is short,
    and the session is not: the answer belongs to a session the counters no longer
    know, and usage counts toward the window that admitted it."""
    being_written = await held(counters, db, guest)
    await counters.lose()
    await counters.limits.status(db, guest, now=NOON)  # rebuilds them, from nothing
    await ended(counters, db, being_written, written=100)  # 200
    nothing_open = await drift.compare(db, counters.engine, now=NOON)
    a_minute_on = NOON + timedelta(minutes=1)
    await answered(counters, db, guest, written=50, now=a_minute_on)  # 150, a new session

    compared = await drift.compare(db, counters.engine, now=a_minute_on)

    assert nothing_open == Compared(accounts=1)
    assert compared == Compared(
        accounts=1, apart=(Apart(guest.user_id, "week", MONDAY, counters=150, ledger=350),)
    )
    assert await used(counters, guest) == {"session": 150, "week": 150}


async def test_credits_the_counters_were_told_of_that_the_ledger_has_no_row_for_are_over(
    counters: Counters, db: AsyncSession, guest: Account
) -> None:
    """A settle without its row in the ledger: what writing the ledger first is there to
    prevent."""
    await answered(counters, db, guest, written=50)
    never_recorded = await held(counters, db, guest)
    await counters.engine.settle(str(guest.user_id), str(never_recorded.id), 77, now_ms=to_ms(NOON))

    compared = await drift.compare(db, counters.engine, now=NOON)

    assert compared == Compared(
        accounts=1,
        apart=(
            Apart(guest.user_id, "session", NOON, counters=227, ledger=150),
            Apart(guest.user_id, "week", MONDAY, counters=227, ledger=150),
        ),
    )
    assert [(found.short, found.over) for found in compared.apart] == [(0, 77), (0, 77)]
    assert compared.totals() == {"session": Totals(over=77), "week": Totals(over=77)}


async def test_what_is_over_is_what_the_counters_hold_beyond_every_row_those_waiting_too(
    counters: Counters, db: AsyncSession, guest: Account
) -> None:
    """The counters have an answer whose note is still there, 200 credits, and 77 that
    the ledger has no row for. They are over by the 77: said against all the ledger
    holds, 350, not against the 150 it has as counted."""
    await answered(counters, db, guest, written=50)
    waiting = await held(counters, db, guest)
    credits = counters.limits.record(
        db, waiting, input_tokens=100, output_tokens=100, estimated=False, now=NOON
    )
    await db.commit()
    assert await counters.limits.settle(waiting, credits, now=NOON) == ()
    never_recorded = await held(counters, db, guest)
    await counters.engine.settle(str(guest.user_id), str(never_recorded.id), 77, now_ms=to_ms(NOON))

    compared = await drift.compare(db, counters.engine, now=NOON)

    assert compared == Compared(
        accounts=1,
        apart=(
            Apart(guest.user_id, "session", NOON, counters=427, ledger=350),
            Apart(guest.user_id, "week", MONDAY, counters=427, ledger=350),
        ),
    )
    assert compared.totals() == {"session": Totals(over=77), "week": Totals(over=77)}


async def test_an_answer_recorded_at_the_very_moment_its_windows_opened_is_read(
    counters: Counters, db: AsyncSession, guest: Account
) -> None:
    """The week opens on Monday at midnight, and so does the session of an answer asked
    and ended at that moment, as a test's clock has it. Its row is dated when both
    opened: left out, its credits would be over."""
    await answered(counters, db, guest, written=50, now=MONDAY)

    assert await drift.compare(db, counters.engine, now=MONDAY) == Compared(accounts=1)
    assert await used(counters, guest) == {"session": 150, "week": 150}


async def test_once_the_session_is_over_only_the_week_is_compared_and_once_both_are_nothing_is(
    counters: Counters, db: AsyncSession, guest: Account
) -> None:
    """What was short in both windows is short in the week alone once the session is
    over: from the very moment it starts over, five hours after it opened. A week later,
    with nothing asked meanwhile, the account is compared all the same, and nothing is
    apart: what was missed in a window that has closed is no longer seen. Nor is the
    database asked then."""
    await written_while_lost(counters, db, guest)
    in_the_week_alone = Compared(
        accounts=1, apart=(Apart(guest.user_id, "week", MONDAY, counters=150, ledger=350),)
    )

    a_moment_before = await drift.compare(
        db, counters.engine, now=NOON + timedelta(hours=5) - timedelta(milliseconds=1)
    )
    five_hours_on = await drift.compare(db, counters.engine, now=NOON + timedelta(hours=5))
    six_hours_on = await drift.compare(db, counters.engine, now=NOON + timedelta(hours=6))
    with statements(db) as sent:
        a_week_on = await drift.compare(db, counters.engine, now=NOON + timedelta(days=7))

    assert [found.window for found in a_moment_before.apart] == ["session", "week"]
    assert five_hours_on == in_the_week_alone
    assert six_hours_on == in_the_week_alone
    assert a_week_on == Compared(accounts=1)
    assert sent == []


# --- Answers that end while a run reads ------------------------------------------------


async def test_an_answer_that_ends_between_the_read_of_the_counters_and_of_the_ledger_is_not_apart(
    counters: Counters,
    elsewhere: async_sessionmaker[AsyncSession],
    db: AsyncSession,
    guest: Account,
) -> None:
    """It ends as a turn's does, in a transaction of its own, after the counters were
    read and before the ledger is. The ledger then has it as counted, and the counters,
    as they were read, don't. Read again, they do: without the second read it would be
    short."""
    await answered(counters, db, guest, written=50)
    being_written = await held(counters, db, guest)

    async def ends_now(_user: str) -> None:
        if len(watched.read) == 1:
            async with elsewhere() as other:
                await ended(counters, other, being_written, written=100)

    watched = Watched(counters, after=ends_now)
    async with asyncio.timeout(10):  # a run that held what the answer writes would wait
        compared = await drift.compare(db, watched, now=NOON)

    assert compared == Compared(accounts=1)
    assert watched.read == [str(guest.user_id)] * 2
    assert await used(counters, guest) == {"session": 350, "week": 350}


async def test_an_answer_that_ends_after_the_runs_first_statement_is_not_apart_in_the_next_account(
    counters: Counters, elsewhere: async_sessionmaker[AsyncSession], db: AsyncSession
) -> None:
    """It is the answer of the account compared second, and ends before that account's
    counters are read: they have it, and the ledger, read after them, must have it too.
    In particular they are not over, as they would be if the run read the ledger as it
    stood at its first statement. Each account has an answer being written, since either
    may be compared second."""
    being_written: dict[str, Reserved] = {}
    for _ in range(2):
        account = Account(new_id(), is_guest=True, own_plan=None)
        await answered(counters, db, account, written=50)
        being_written[str(account.user_id)] = await held(counters, db, account)
    ended_now: list[str] = []

    async def ends_now(user: str) -> None:
        if watched.read and user != watched.read[0] and not ended_now:
            ended_now.append(user)
            async with elsewhere() as other:
                await ended(counters, other, being_written[user], written=100)

    watched = Watched(counters, before=ends_now)
    async with asyncio.timeout(10):
        compared = await drift.compare(db, watched, now=NOON)

    assert compared == Compared(accounts=2)
    assert len(ended_now) == 1


async def test_what_is_short_is_said_as_the_counters_stand_when_they_are_read_again(
    counters: Counters,
    elsewhere: async_sessionmaker[AsyncSession],
    db: AsyncSession,
    guest: Account,
) -> None:
    """The counters miss an answer of 200 credits, written while they were lost. Another,
    of 130, ends between the read of the counters and the read of the ledger. It is no
    part of what is short: the counters are short by the 200, with the 130 on both
    sides."""
    await answered(counters, db, guest, written=50)  # 150
    while_lost = await held(counters, db, guest)
    await counters.lose()
    being_written = await held(counters, db, guest)  # rebuilds them, from the first
    await ended(counters, db, while_lost, written=100)  # 200, which they miss

    async def ends_now(_user: str) -> None:
        if len(watched.read) == 1:
            async with elsewhere() as other:
                await ended(counters, other, being_written, written=30)  # 130

    watched = Watched(counters, after=ends_now)
    async with asyncio.timeout(10):
        compared = await drift.compare(db, watched, now=NOON)

    assert compared == Compared(
        accounts=1,
        apart=(
            Apart(guest.user_id, "session", NOON, counters=280, ledger=480),
            Apart(guest.user_id, "week", MONDAY, counters=280, ledger=480),
        ),
    )
    assert compared.totals() == {"session": Totals(short=200), "week": Totals(short=200)}


async def test_a_window_that_opened_anew_between_the_two_reads_is_not_said_to_be_short(
    counters: Counters, db: AsyncSession, guest: Account
) -> None:
    """Both windows seem short when the counters are first read. Before they are read
    again, they are written anew, as a rebuild a minute on would write them from a
    ledger that had the week whole and a session that opened then, with 10 credits. The
    week has caught up. The session holds less than the first one's rows, and says
    nothing of them: it is another window."""
    await written_while_lost(counters, db, guest)  # 150 counted, 350 in the ledger
    user = str(guest.user_id)
    a_minute_on = to_ms(NOON) + 60_000
    plans = counters.limits.plans
    windows = plans.windows(plans.plan_for(is_guest=True, own_plan=None), now_ms=a_minute_on)

    async def opens_anew(_user: str) -> None:
        if len(watched.read) == 1:
            assert await counters.engine.rebuild(
                user,
                windows,
                [
                    LedgerEntry("the-week", 350, {"week": to_ms(MONDAY)}),
                    LedgerEntry("a-new-session", 10, {"session": a_minute_on}),
                ],
                now_ms=a_minute_on,
                if_epoch=await counters.engine.epoch(user),
            )

    watched = Watched(counters, after=opens_anew)
    compared = await drift.compare(db, watched, now=NOON)

    assert compared == Compared(accounts=1)
    assert watched.read == [user] * 2
    assert await used(counters, guest) == {"session": 10, "week": 350}


async def test_counters_that_are_lost_between_the_two_reads_are_not_said_to_be_short(
    counters: Counters, db: AsyncSession, guest: Account
) -> None:
    """They seemed short, and are gone when they are read again: the next rebuild counts
    what they missed. The account was compared, and nothing is said."""
    await written_while_lost(counters, db, guest)

    async def lost_now(_user: str) -> None:
        if len(watched.read) == 1:
            await counters.lose()

    watched = Watched(counters, after=lost_now)
    compared = await drift.compare(db, watched, now=NOON)

    assert compared == Compared(accounts=1)
    assert watched.read == [str(guest.user_id)] * 2


# --- Which accounts -------------------------------------------------------------------


async def test_an_account_whose_counters_vanish_after_the_scan_found_them_is_not_compared(
    counters: Counters, db: AsyncSession, guest: Account
) -> None:
    await answered(counters, db, guest, written=50)

    async def lost_now(_user: str) -> None:
        await counters.lose()

    watched = Watched(counters, before=lost_now)

    assert await drift.compare(db, watched, now=NOON) == Compared()
    assert watched.read == [str(guest.user_id)]


async def test_an_account_whose_counters_were_lost_is_not_compared(
    counters: Counters, db: AsyncSession, guest: Account
) -> None:
    """Its counters are rebuilt from the ledger when they are next needed: there is
    nothing to compare the ledger with."""
    await answered(counters, db, guest, written=50)
    await counters.lose()

    assert await drift.compare(db, counters.engine, now=NOON) == Compared()


async def test_an_account_whose_plan_has_no_limits_is_compared_and_nothing_is_apart(
    counters: Counters, plans_with: Any, db: AsyncSession
) -> None:
    counters.limits.plans = plans_with(account=None)
    staff = Account(new_id(), is_guest=False, own_plan=None)
    await answered(counters, db, staff, written=50)

    with statements(db) as sent:
        compared = await drift.compare(db, counters.engine, now=NOON)

    assert compared == Compared(accounts=1)
    assert await counters.engine.buckets(str(staff.user_id)) == {}
    assert sent == []  # no window to compare: the database isn't asked


async def test_another_installations_counters_on_the_same_valkey_are_not_compared(
    counters: Counters, valkey: Valkey, plans_with: Any, db: AsyncSession, guest: Account
) -> None:
    """Those of another prefix, whose account is over by 77 credits there."""
    another = counters_under(valkey, f"{counters.prefix}-other", plans_with())
    theirs = Account(new_id(), is_guest=True, own_plan=None)
    never_recorded = await held(another, db, theirs)
    await another.engine.settle(str(theirs.user_id), str(never_recorded.id), 77, now_ms=to_ms(NOON))
    await answered(counters, db, guest, written=50)

    assert await drift.compare(db, counters.engine, now=NOON) == Compared(accounts=1)
    assert await drift.compare(db, another.engine, now=NOON) == Compared(
        accounts=1,
        apart=(
            Apart(theirs.user_id, "session", NOON, counters=77, ledger=0),
            Apart(theirs.user_id, "week", MONDAY, counters=77, ledger=0),
        ),
    )


@pytest.mark.parametrize(("limit", "accounts", "more"), [(2, 2, True), (3, 3, False)])
async def test_a_run_compares_as_many_accounts_as_it_is_given_and_says_whether_there_were_more(
    limit: int, accounts: int, more: bool, counters: Counters, db: AsyncSession
) -> None:
    """Three here. By default, 2,000."""
    for _ in range(3):
        await answered(counters, db, Account(new_id(), is_guest=True, own_plan=None), written=50)

    compared = await drift.compare(db, counters.engine, now=NOON, limit=limit)

    assert (compared.accounts, compared.more) == (accounts, more)
    assert drift.ACCOUNTS == 2_000


async def test_an_id_under_the_prefix_that_is_no_accounts_is_left_out_with_a_warning(
    counters: Counters, db: AsyncSession, guest: Account, caplog: pytest.LogCaptureFixture
) -> None:
    """Nobody who uses the API can bring one about: the ids are the server's own. One is
    no UUID, the other is one, but not as the API writes it. The others are compared all
    the same."""
    await answered(counters, db, guest, written=50)
    strays = ["not-an-account", new_id().hex]
    for stray in strays:
        assert await counters.engine.rebuild(stray, [], [], now_ms=to_ms(NOON), if_epoch=None)

    compared = await drift.compare(db, counters.engine, now=NOON)

    assert compared == Compared(accounts=1)
    said = [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if record.name == "actio.usage.drift"
    ]
    assert sorted(said) == sorted(
        (
            logging.WARNING,
            f"usage: counters under {stray!r}, which can't be an account's id, aren't compared",
        )
        for stray in strays
    )


# --- When Valkey is away --------------------------------------------------------------


async def test_without_valkey_nothing_is_compared_and_nothing_raised(
    nowhere: Valkey, db: AsyncSession
) -> None:
    async with asyncio.timeout(10):
        compared = await drift.compare(db, ValkeyQuota(nowhere, prefix="away"), now=NOON)

    assert compared == Compared(valkey_away=True)


async def test_when_valkey_stops_answering_at_the_second_account_what_the_first_found_is_kept(
    counters: Counters, db: AsyncSession
) -> None:
    """Each account's counters are over by 77 credits."""
    for _ in range(2):
        account = Account(new_id(), is_guest=True, own_plan=None)
        never_recorded = await held(counters, db, account)
        await counters.engine.settle(
            str(account.user_id), str(never_recorded.id), 77, now_ms=to_ms(NOON)
        )

    async def goes_away(_user: str) -> None:
        if watched.read:
            raise ValkeyConnectionError("Connection refused")

    watched = Watched(counters, before=goes_away)
    compared = await drift.compare(db, watched, now=NOON)

    [first] = watched.read
    assert (compared.accounts, compared.more, compared.valkey_away) == (1, False, True)
    assert [(found.user_id, found.window) for found in compared.apart] == [
        (uuid.UUID(first), "session"),
        (uuid.UUID(first), "week"),
    ]


async def test_an_account_at_whose_second_read_valkey_stops_answering_is_not_counted(
    counters: Counters, db: AsyncSession, guest: Account
) -> None:
    """Its windows seemed short, and whether they are was never read: nothing is kept of
    an account whose comparison didn't end."""
    await written_while_lost(counters, db, guest)

    async def goes_away(_user: str) -> None:
        if watched.read:
            raise ValkeyConnectionError("Connection refused")

    watched = Watched(counters, before=goes_away)
    compared = await drift.compare(db, watched, now=NOON)

    assert compared == Compared(valkey_away=True)
    assert watched.read == [str(guest.user_id)]


class Refusing:
    """A database session that raises what it is given, at any statement."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    async def execute(self, *_args: Any, **_kwargs: Any) -> Any:
        raise self.error


@pytest.mark.parametrize(
    "error",
    [
        OperationalError("SELECT", {}, Exception("the connection is closed")),
        OSError("Network is unreachable"),
    ],
    ids=["sqlalchemy", "system"],
)
async def test_an_error_of_the_database_is_raised_as_it_is(
    error: Exception, counters: Counters, db: AsyncSession, guest: Account
) -> None:
    """Whatever its kind: Valkey answered, so the run doesn't say that it didn't. The
    job fails, and its error is in the log."""
    await answered(counters, db, guest, written=50)

    with pytest.raises(type(error)) as raised:
        await drift.compare(cast(AsyncSession, Refusing(error)), counters.engine, now=NOON)

    assert raised.value is error


# --- It writes nothing ----------------------------------------------------------------


async def test_a_run_writes_nothing(
    counters: Counters, valkey: Valkey, db: AsyncSession, guest: Account
) -> None:
    """Neither to Valkey, not even to push back when the counters' keys expire, nor to
    the database: every statement it sends reads, and its session has nothing to commit.
    With an account whose counters are short, and one whose agree, which was answered
    after the other's counters were lost. The keys are made to expire at a moment that
    no write would set."""
    await written_while_lost(counters, db, guest)
    await answered(counters, db, Account(new_id(), is_guest=True, own_plan=None), written=50)
    seconds, microseconds = await valkey.time()
    at = seconds * 1000 + microseconds // 1000 + DAY_MS + 1
    keys = sorted(await valkey.keys(f"{counters.prefix}:*"))
    for key in keys:
        assert await valkey.pexpireat(key, at)
    ledger = await in_the_ledger(db)
    notes = await uncounted(db)
    await db.commit()

    with statements(db) as sent:
        compared = await drift.compare(db, counters.engine, now=NOON)
    nothing_to_commit = not (db.new or db.dirty or db.deleted)
    await db.rollback()

    assert compared.accounts == 2
    assert {found.user_id for found in compared.apart} == {guest.user_id}
    assert len(sent) == 2
    assert all(statement.lstrip().startswith("SELECT") for statement in sent)
    assert nothing_to_commit
    assert sorted(await valkey.keys(f"{counters.prefix}:*")) == keys
    # The client types it as the blocking client's: here it is awaited.
    expire_at = [await valkey.pexpiretime(key) for key in keys]  # type: ignore[misc]
    assert expire_at == [at] * len(keys)
    assert await in_the_ledger(db) == ledger
    assert await uncounted(db) == notes
