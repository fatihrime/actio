"""Usage limits below the API, against a real Valkey and a real Postgres.

The engine's own rules are tested in `packages/quota`. Here: how the application uses
it. Which windows a plan gives, what is held and what is counted, the ledger, and what
happens when the counters are lost, or Valkey or the database is away.

In these tests a credit is a token, read or written, so the numbers can be followed.
"""

import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from valkey.asyncio import Valkey

from actio.catalog import OfferedModel
from actio.chat import answering
from actio.core.database import create_engine, create_session_factory
from actio.core.ids import new_id
from actio.usage.limits import (
    Account,
    LimitReachedError,
    Reserved,
    UsageLimits,
    UsageUnavailableError,
    held_for_prompt,
)
from actio.usage.models import UsageEntry
from actio.usage.plans import Plans, PlansFile
from actio_llm import ChatRequest, Finish, FinishReason, StreamEvent, TextDelta, Usage, User
from actio_quota.engine import ValkeyQuota

pytestmark = pytest.mark.anyio

NOON = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)  # a Tuesday
WEEK_ENDS = datetime(2026, 11, 16, 0, 0, tzinfo=UTC)
HOLD = timedelta(minutes=10)


def plans(**changes: Any) -> Plans:
    """A guest may use 1,000 credits a session and 3,000 a week; an account ten times
    that. `standard` costs a credit a token, `local` nothing."""
    file: dict[str, Any] = {
        "guests": "guest",
        "accounts": "member",
        "session_hours": 5,
        "week_resets": "MON 00:00 UTC",
        "min_answer_tokens": 100,
        "prices": {
            "standard": {"input": 1_000_000, "output": 1_000_000, "cached_input": 100_000},
            "local": {"input": 0, "output": 0},
        },
        "plans": {
            "guest": {"title": "Guest", "session_credits": 1_000, "week_credits": 3_000},
            "member": {"title": "Member", "session_credits": 10_000, "week_credits": 30_000},
            "staff": {"title": "Staff"},
        },
    }
    return Plans(PlansFile.model_validate(file | changes))


@dataclass
class Counters:
    """The limits under test, and a way to lose what Valkey holds for them."""

    limits: UsageLimits
    lose: Callable[[], Awaitable[None]]


@pytest.fixture
async def valkey(valkey_url: str) -> AsyncIterator[Valkey]:
    async with Valkey.from_url(valkey_url) as client:
        yield client


@pytest.fixture
def counters(valkey: Valkey) -> Counters:
    """Limits with keys of their own on the session's Valkey server."""
    prefix = f"test-{uuid.uuid4().hex[:16]}"

    async def lose() -> None:
        # What a restart does to a Valkey that keeps nothing on disk.
        await valkey.delete(*await valkey.keys(f"{prefix}:*"))

    return Counters(UsageLimits(ValkeyQuota(valkey, prefix=prefix), plans()), lose)


@pytest.fixture
def limits(counters: Counters) -> UsageLimits:
    return counters.limits


@pytest.fixture
def guest() -> Account:
    return Account(new_id(), is_guest=True)


async def reserve(
    limits: UsageLimits,
    db: AsyncSession,
    account: Account,
    *,
    prompt: int = 80,
    answer: int = 400,
    model: str = "standard",
    now: datetime = NOON,
    hold_for: timedelta = HOLD,
) -> Reserved:
    """Hold credits for a request with a prompt of `prompt` tokens that asks for up to
    `answer` tokens. What is held for the prompt is a quarter more: 80 holds 100."""
    return await limits.reserve(
        db,
        account,
        new_id(),
        model=model,
        prompt_tokens=prompt,
        max_output_tokens=answer,
        hold_for=hold_for,
        now=now,
    )


async def finish(
    limits: UsageLimits,
    db: AsyncSession,
    reserved: Reserved,
    *,
    read: int,
    written: int,
    now: datetime = NOON,
) -> int:
    """Record and settle what a request really used, as a finished turn does."""
    credits = limits.record(
        db, reserved, input_tokens=read, output_tokens=written, estimated=False, now=now
    )
    await db.commit()
    await limits.settle(reserved, credits, now=now)
    return credits


async def windows(
    limits: UsageLimits, db: AsyncSession, account: Account, now: datetime = NOON
) -> dict[str, tuple[int, int, datetime | None]]:
    """Each window as (used, reserved, resets_at)."""
    status = await limits.status(db, account, now=now)
    return {w.name: (w.used, w.reserved, w.resets_at) for w in status.windows}


# --- Holding and counting -------------------------------------------------------------


def test_a_quarter_more_than_the_guess_is_held_for_a_prompt() -> None:
    # Rounded up: a short guess is what the margin is for.
    assert [held_for_prompt(tokens) for tokens in (0, 1, 4, 80, 81)] == [0, 2, 5, 100, 102]


async def test_a_new_account_has_its_plans_windows_and_nothing_used(
    limits: UsageLimits, db: AsyncSession, guest: Account
) -> None:
    status = await limits.status(db, guest, now=NOON)

    assert (status.plan.name, status.plan.title) == ("guest", "Guest")
    assert [(w.name, w.limit, w.used, w.reserved) for w in status.windows] == [
        ("session", 1_000, 0, 0),
        ("week", 3_000, 0, 0),
    ]
    session, week = status.windows
    assert session.resets_at is None  # not open: the first request opens it
    assert week.resets_at == WEEK_ENDS


async def test_an_account_has_another_plan_than_a_guest(
    limits: UsageLimits, db: AsyncSession
) -> None:
    status = await limits.status(db, Account(new_id(), is_guest=False), now=NOON)

    assert status.plan.name == "member"
    assert [w.limit for w in status.windows] == [10_000, 30_000]


async def test_a_request_holds_its_largest_cost_until_it_ends(
    limits: UsageLimits, db: AsyncSession, guest: Account
) -> None:
    reserved = await reserve(limits, db, guest, prompt=80, answer=400)

    assert (reserved.max_output_tokens, reserved.capped) == (400, False)
    assert reserved.prompt_tokens == 80  # the guess itself, without the margin
    assert set(reserved.windows) == {"session", "week"}
    opens = NOON + timedelta(hours=5)
    assert await windows(limits, db, guest) == {
        "session": (0, 500, opens),  # 100 for the prompt, 400 for the answer
        "week": (0, 500, WEEK_ENDS),
    }


async def test_what_a_request_used_is_counted_and_the_rest_comes_back(
    limits: UsageLimits, db: AsyncSession, guest: Account
) -> None:
    reserved = await reserve(limits, db, guest, prompt=80, answer=400)

    credits = await finish(limits, db, reserved, read=90, written=150)

    assert credits == 240
    assert await windows(limits, db, guest) == {
        "session": (240, 0, NOON + timedelta(hours=5)),
        "week": (240, 0, WEEK_ENDS),
    }


async def test_what_was_read_from_the_cache_costs_its_own_price(
    limits: UsageLimits, db: AsyncSession, guest: Account
) -> None:
    reserved = await reserve(limits, db, guest)

    credits = limits.record(
        db,
        reserved,
        input_tokens=100,
        cached_input_tokens=60,
        output_tokens=10,
        estimated=False,
        now=NOON,
    )

    assert credits == 40 + 6 + 10  # 40 read anew, 60 from the cache at a tenth


async def test_a_providers_count_that_makes_no_sense_still_gets_recorded(
    limits: UsageLimits, db: AsyncSession, guest: Account
) -> None:
    reserved = await reserve(limits, db, guest)

    credits = limits.record(
        db,
        reserved,
        input_tokens=100,
        cached_input_tokens=250,  # more from the cache than was read at all
        output_tokens=10,
        estimated=False,
        now=NOON,
    )
    await db.commit()

    assert credits == 10 + 10  # all 100 at the cached price
    entry = await db.get(UsageEntry, reserved.id)
    assert entry is not None
    assert entry.cached_input_tokens == 100


async def test_a_request_that_used_nothing_is_released(
    limits: UsageLimits, db: AsyncSession, guest: Account
) -> None:
    reserved = await reserve(limits, db, guest)

    await limits.release(reserved, now=NOON)

    assert await windows(limits, db, guest) == {
        "session": (0, 0, NOON + timedelta(hours=5)),  # the session it opened stays open
        "week": (0, 0, WEEK_ENDS),
    }
    assert (await db.scalars(select(UsageEntry))).all() == []


async def test_a_prompt_larger_than_was_held_is_counted_as_it_was(
    limits: UsageLimits, db: AsyncSession, guest: Account
) -> None:
    """The one way to use more than was reserved: the guess at the prompt was short."""
    reserved = await reserve(limits, db, guest, prompt=80, answer=100)

    credits = await finish(limits, db, reserved, read=700, written=100)

    assert credits == 800  # 200 were held
    assert (await windows(limits, db, guest))["session"][:2] == (800, 0)


# --- Near a limit, and at it ----------------------------------------------------------


async def test_near_a_limit_an_answer_gets_less_room(
    limits: UsageLimits, db: AsyncSession, guest: Account
) -> None:
    first = await reserve(limits, db, guest)
    await finish(limits, db, first, read=100, written=500)  # 400 of the session's 1,000 left

    reserved = await reserve(limits, db, guest, prompt=80, answer=400)

    assert (reserved.max_output_tokens, reserved.capped) == (300, True)  # 100 go to the prompt
    assert (await windows(limits, db, guest))["session"][:2] == (600, 400)


async def test_with_too_little_left_for_an_answer_a_request_is_refused(
    limits: UsageLimits, db: AsyncSession, guest: Account
) -> None:
    first = await reserve(limits, db, guest)
    await finish(limits, db, first, read=100, written=701)  # 199 left: 100 for a prompt, 99 more
    before = await windows(limits, db, guest)

    with pytest.raises(LimitReachedError) as refused:
        await reserve(limits, db, guest, prompt=80, answer=400)

    assert refused.value.window == "session"
    assert refused.value.resets_at == NOON + timedelta(hours=5)
    assert refused.value.alternatives == ["local"]
    assert await windows(limits, db, guest) == before  # a refusal changes nothing


async def test_a_refused_request_opens_no_session(
    limits: UsageLimits, db: AsyncSession, guest: Account
) -> None:
    with pytest.raises(LimitReachedError) as refused:
        await reserve(limits, db, guest, prompt=2_000, answer=400)

    assert refused.value.window == "session"
    # More than a whole session allows: waiting for the session to start over won't help.
    assert refused.value.resets_at is None
    assert (await windows(limits, db, guest))["session"] == (0, 0, None)


async def test_the_session_starts_over_five_hours_after_it_opened_and_the_week_does_not(
    limits: UsageLimits, db: AsyncSession, guest: Account
) -> None:
    first = await reserve(limits, db, guest)
    await finish(limits, db, first, read=100, written=800)
    later = NOON + timedelta(hours=5)

    with pytest.raises(LimitReachedError):
        await reserve(limits, db, guest, now=later - timedelta(seconds=1))
    again = await reserve(limits, db, guest, now=later)

    assert (again.max_output_tokens, again.capped) == (400, False)
    assert await windows(limits, db, guest, later) == {
        "session": (0, 500, later + timedelta(hours=5)),  # a new session, opened just now
        "week": (900, 500, WEEK_ENDS),
    }


async def test_when_the_week_is_full_the_refusal_names_the_week(
    limits: UsageLimits, db: AsyncSession, guest: Account
) -> None:
    now = NOON
    for _ in range(3):  # three sessions use the week's 3,000 up, to the last 300
        reserved = await reserve(limits, db, guest, now=now)
        await finish(limits, db, reserved, read=100, written=800, now=now)
        now += timedelta(hours=5)

    with pytest.raises(LimitReachedError) as refused:
        await reserve(limits, db, guest, prompt=240, answer=400, now=now)  # the session is new

    assert refused.value.window == "week"
    assert refused.value.resets_at == WEEK_ENDS


async def test_when_both_are_full_the_refusal_names_the_one_that_starts_over_last(
    limits: UsageLimits, db: AsyncSession
) -> None:
    """A session that is open across the week's reset ends after the week does."""
    one_session_a_week = {"title": "Guest", "session_credits": 1_000, "week_credits": 1_000}
    limits.plans = plans(accounts="guest", plans={"guest": one_session_a_week})
    sunday_evening = WEEK_ENDS - timedelta(hours=2)
    on_sunday, guest = Account(new_id(), is_guest=True), Account(new_id(), is_guest=True)
    for account, now in ((guest, NOON), (on_sunday, sunday_evening)):
        reserved = await reserve(limits, db, account, now=now)
        await finish(limits, db, reserved, read=100, written=800, now=now)

    with pytest.raises(LimitReachedError) as midweek:
        await reserve(limits, db, guest, now=NOON + timedelta(hours=1))
    with pytest.raises(LimitReachedError) as before_the_reset:
        await reserve(limits, db, on_sunday, now=sunday_evening + timedelta(hours=1))

    assert (midweek.value.window, midweek.value.resets_at) == ("week", WEEK_ENDS)
    assert before_the_reset.value.window == "session"
    assert before_the_reset.value.resets_at == sunday_evening + timedelta(hours=5)


async def test_a_free_model_is_never_refused_and_uses_nothing_up(
    limits: UsageLimits, db: AsyncSession, guest: Account
) -> None:
    first = await reserve(limits, db, guest)
    await finish(limits, db, first, read=100, written=900)  # the session is full

    reserved = await reserve(limits, db, guest, model="local", prompt=5_000, answer=400)
    credits = await finish(limits, db, reserved, read=5_000, written=400)

    assert (reserved.max_output_tokens, reserved.capped) == (400, False)
    assert credits == 0
    assert (await windows(limits, db, guest))["session"][:2] == (1_000, 0)
    free = await db.get(UsageEntry, reserved.id)
    assert free is not None  # what it used is still on record
    assert (free.model, free.credits, free.input_tokens) == ("local", 0, 5_000)


async def test_a_plan_without_limits_limits_nothing(limits: UsageLimits, db: AsyncSession) -> None:
    limits.plans = plans(accounts="staff")
    staff = Account(new_id(), is_guest=False)

    reserved = await reserve(limits, db, staff, prompt=400_000, answer=4_000)
    credits = await finish(limits, db, reserved, read=400_000, written=4_000)

    assert (reserved.max_output_tokens, reserved.capped) == (4_000, False)
    assert reserved.windows == {}
    assert credits == 404_000
    assert (await limits.status(db, staff, now=NOON)).windows == []


async def test_requests_at_the_same_moment_cannot_spend_the_same_credits(
    limits: UsageLimits, db: AsyncSession, guest: Account
) -> None:
    """Two answers being written at once: the second fits only in what the first left."""
    first = await reserve(limits, db, guest, prompt=80, answer=700)

    second = await reserve(limits, db, guest, prompt=80, answer=700)

    assert first.max_output_tokens == 700
    assert (second.max_output_tokens, second.capped) == (100, True)  # 800 of 1,000 were held
    with pytest.raises(LimitReachedError):
        await reserve(limits, db, guest)


async def test_a_hold_runs_out_by_itself_when_its_request_never_ends(
    limits: UsageLimits, db: AsyncSession, guest: Account
) -> None:
    """A process that dies in the middle of an answer settles nothing."""
    await reserve(limits, db, guest, prompt=80, answer=900)

    with pytest.raises(LimitReachedError):
        await reserve(limits, db, guest, now=NOON + HOLD - timedelta(seconds=1))
    after = await reserve(limits, db, guest, prompt=80, answer=900, now=NOON + HOLD)

    assert (after.max_output_tokens, after.capped) == (900, False)


# --- The ledger -----------------------------------------------------------------------


async def test_the_ledger_keeps_what_a_request_used_and_the_windows_that_admitted_it(
    limits: UsageLimits, db: AsyncSession, guest: Account
) -> None:
    reserved = await reserve(limits, db, guest)
    later = NOON + timedelta(minutes=2)

    limits.record(db, reserved, input_tokens=90, output_tokens=150, estimated=True, now=later)
    await db.commit()

    [entry] = (await db.scalars(select(UsageEntry))).all()
    assert entry.reservation_id == reserved.id
    assert (entry.user_id, entry.model, entry.credits) == (guest.user_id, "standard", 240)
    assert (entry.input_tokens, entry.cached_input_tokens, entry.output_tokens) == (90, 0, 150)
    assert entry.estimated is True
    assert entry.occurred_at == later
    assert entry.windows == {
        "session": int(NOON.timestamp() * 1000),  # when the request was admitted
        "week": int((WEEK_ENDS - timedelta(days=7)).timestamp() * 1000),
    }


async def test_a_request_is_never_in_the_ledger_twice(
    limits: UsageLimits, db: AsyncSession, guest: Account
) -> None:
    reserved = await reserve(limits, db, guest)
    await finish(limits, db, reserved, read=90, written=150)

    limits.record(db, reserved, input_tokens=90, output_tokens=150, estimated=False, now=NOON)
    with pytest.raises(DBAPIError, match="pk_usage_ledger"):
        await db.commit()


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE usage_ledger SET credits = 0",
        "DELETE FROM usage_ledger",
        "TRUNCATE usage_ledger",
    ],
)
async def test_the_database_refuses_to_change_or_delete_what_is_in_the_ledger(
    limits: UsageLimits, db: AsyncSession, guest: Account, change: str
) -> None:
    reserved = await reserve(limits, db, guest)
    await finish(limits, db, reserved, read=90, written=150)

    with pytest.raises(DBAPIError, match="usage_ledger only takes new rows"):
        await db.execute(text(change))
    await db.rollback()

    [entry] = (await db.scalars(select(UsageEntry))).all()
    assert entry.credits == 240


# --- When the counters are lost -------------------------------------------------------


async def test_lost_counters_are_rebuilt_from_the_ledger(
    counters: Counters, db: AsyncSession, guest: Account
) -> None:
    limits = counters.limits
    first = await reserve(limits, db, guest)
    await finish(limits, db, first, read=100, written=500)
    before = await windows(limits, db, guest)

    await counters.lose()

    assert await windows(limits, db, guest) == before  # reading rebuilds them
    await counters.lose()
    reserved = await reserve(limits, db, guest, prompt=80, answer=400)  # and so does asking
    assert (reserved.max_output_tokens, reserved.capped) == (300, True)


async def test_a_rebuild_counts_only_the_windows_that_are_still_open(
    counters: Counters, db: AsyncSession, guest: Account
) -> None:
    limits = counters.limits
    first = await reserve(limits, db, guest)
    await finish(limits, db, first, read=100, written=500)
    next_session, next_week = NOON + timedelta(hours=6), WEEK_ENDS + timedelta(hours=1)

    await counters.lose()
    in_the_week = await windows(limits, db, guest, next_session)
    await counters.lose()
    a_week_on = await windows(limits, db, guest, next_week)

    assert in_the_week == {"session": (0, 0, None), "week": (600, 0, WEEK_ENDS)}
    assert a_week_on == {"session": (0, 0, None), "week": (0, 0, WEEK_ENDS + timedelta(days=7))}


async def test_a_request_settled_after_the_counters_were_lost_is_counted_once(
    counters: Counters, db: AsyncSession, guest: Account
) -> None:
    """Why the ledger is written first: the rebuild finds the request there, and the
    settle that arrives afterwards has nothing left to add."""
    limits = counters.limits
    reserved = await reserve(limits, db, guest)
    credits = limits.record(
        db, reserved, input_tokens=100, output_tokens=500, estimated=False, now=NOON
    )
    await db.commit()

    await counters.lose()
    rebuilt = await windows(limits, db, guest)
    await limits.settle(reserved, credits, now=NOON)

    assert rebuilt["session"][:2] == (600, 0)
    assert await windows(limits, db, guest) == rebuilt


async def test_a_request_running_while_the_counters_are_lost_goes_uncounted_until_the_next_rebuild(
    counters: Counters, db: AsyncSession, guest: Account
) -> None:
    """The accepted gap: in the account's favour, and never lost from the ledger."""
    limits = counters.limits
    reserved = await reserve(limits, db, guest)

    await counters.lose()
    assert (await windows(limits, db, guest))["session"] == (0, 0, None)  # rebuilt, empty
    await finish(limits, db, reserved, read=100, written=500)

    assert (await windows(limits, db, guest))["session"][:2] == (0, 0)
    await counters.lose()
    assert (await windows(limits, db, guest))["session"][:2] == (600, 0)


# --- When Valkey is away --------------------------------------------------------------


@pytest.fixture
async def away() -> AsyncIterator[UsageLimits]:
    """Limits whose Valkey doesn't answer: nothing listens on port 1."""
    async with Valkey.from_url("valkey://127.0.0.1:1/0", socket_connect_timeout=0.5) as nowhere:
        yield UsageLimits(ValkeyQuota(nowhere, prefix="away"), plans())


async def test_without_valkey_nothing_is_admitted_or_shown(
    away: UsageLimits, db: AsyncSession, guest: Account
) -> None:
    with pytest.raises(UsageUnavailableError):
        await reserve(away, db, guest)
    with pytest.raises(UsageUnavailableError):
        await away.status(db, guest, now=NOON)


class OneAnswer:
    """A model that writes three words, and says what that took."""

    async def stream(self, request: ChatRequest) -> AsyncGenerator[StreamEvent]:
        assert request.messages
        yield TextDelta("A derailleur, mostly.")
        yield Finish(FinishReason.STOP, Usage(input_tokens=40, output_tokens=3))


async def test_a_turn_whose_outcome_cannot_be_written_is_not_counted(
    limits: UsageLimits, db: AsyncSession, guest: Account, caplog: pytest.LogCaptureFixture
) -> None:
    """The ledger comes first. A turn that the database never heard the end of is not
    in the ledger, so the counters must not have it either: its hold is given back."""
    reserved = await reserve(limits, db, guest)
    offered = OfferedModel("standard", "Scripted", "openai-compatible", "m", 4096, 512, OneAnswer())
    nowhere = create_engine("postgresql://actio:unused@127.0.0.1:1/actio")

    async def to_nobody(step: answering.Step) -> None:
        pass

    with pytest.raises(SQLAlchemyError):
        await answering.answer(
            offered,
            ChatRequest(model="m", messages=[User("What moves the chain?")]),
            conversation_id=new_id(),
            turn_id=reserved.id,
            answer_id=new_id(),
            open_session=create_session_factory(nowhere),
            clock=lambda: NOON,
            usage=limits,
            reserved=reserved,
            emit=to_nobody,
            halt=answering.Halt(),
        )
    await nowhere.dispose()

    assert "its outcome couldn't be recorded" in caplog.text
    assert await windows(limits, db, guest) == {
        "session": (0, 0, NOON + timedelta(hours=5)),
        "week": (0, 0, WEEK_ENDS),
    }
    assert (await db.scalars(select(UsageEntry))).all() == []


async def test_without_valkey_a_request_that_ended_is_still_in_the_ledger(
    limits: UsageLimits,
    away: UsageLimits,
    db: AsyncSession,
    guest: Account,
    caplog: pytest.LogCaptureFixture,
) -> None:
    reserved = await reserve(limits, db, guest)

    credits = away.record(
        db, reserved, input_tokens=100, output_tokens=500, estimated=False, now=NOON
    )
    await db.commit()
    assert await away.settle(reserved, credits, now=NOON) == ()  # no error, and it says so
    await away.release(reserved, now=NOON)

    assert "couldn't be settled in Valkey" in caplog.text
    assert "couldn't be released in Valkey" in caplog.text
    entry = await db.get(UsageEntry, reserved.id)
    assert entry is not None
    assert entry.credits == 600
