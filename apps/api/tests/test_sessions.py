"""Sessions below the API, against a real Postgres."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from actio.identity import sessions
from actio.identity.models import BrowserSession, User
from actio.identity.sessions import GUEST_LIFETIME

pytestmark = pytest.mark.anyio

NOON = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


async def count(db: AsyncSession, model: type) -> int:
    return await db.scalar(select(func.count()).select_from(model)) or 0


async def test_a_guest_gets_an_account_and_a_session_that_last_a_day(db: AsyncSession) -> None:
    token, principal = await sessions.start_guest_session(db, now=NOON)
    await db.commit()

    assert principal.is_guest
    assert principal.session_expires_at == NOON + timedelta(hours=24) == NOON + GUEST_LIFETIME
    user = await db.get(User, principal.user_id)
    assert user is not None
    assert (user.is_guest, user.created_at, user.expires_at) == (
        True,
        NOON,
        NOON + GUEST_LIFETIME,
    )
    assert await sessions.authenticate(db, token, now=NOON) == principal


async def test_the_token_itself_is_never_stored(db: AsyncSession) -> None:
    """A copy of the database must not be enough to log in as someone."""
    token, _ = await sessions.start_guest_session(db, now=NOON)
    await db.commit()

    stored = (await db.execute(text("SELECT token_hash FROM sessions"))).scalar_one()
    assert bytes(stored) == sessions.hash_token(token)
    assert len(token) >= 43  # 32 random bytes, as text
    everything = (
        await db.execute(text("SELECT sessions::text || users::text FROM sessions, users"))
    ).scalar_one()
    assert token not in everything


async def test_two_guests_are_two_accounts(db: AsyncSession) -> None:
    first_token, first = await sessions.start_guest_session(db, now=NOON)
    second_token, second = await sessions.start_guest_session(db, now=NOON)

    assert first.user_id != second.user_id
    assert first_token != second_token
    assert await sessions.authenticate(db, second_token, now=NOON) == second


@pytest.mark.parametrize("token", ["", "not-a-token", "A" * 43])
async def test_an_unknown_token_is_nobody(db: AsyncSession, token: str) -> None:
    await sessions.start_guest_session(db, now=NOON)

    assert await sessions.authenticate(db, token, now=NOON) is None


async def test_a_session_stops_working_the_moment_it_expires(db: AsyncSession) -> None:
    token, _ = await sessions.start_guest_session(db, now=NOON)
    last_moment = NOON + GUEST_LIFETIME - timedelta(microseconds=1)

    assert await sessions.authenticate(db, token, now=last_moment) is not None
    assert await sessions.authenticate(db, token, now=NOON + GUEST_LIFETIME) is None


async def test_last_seen_is_written_at_most_once_a_minute(db: AsyncSession) -> None:
    """Otherwise every request that reads a session would also write a row."""
    token, principal = await sessions.start_guest_session(db, now=NOON)

    async def last_seen(at: datetime) -> datetime:
        await sessions.authenticate(db, token, now=at)
        session = await db.get(BrowserSession, principal.session_id)
        assert session is not None
        return session.last_seen_at

    assert await last_seen(NOON + timedelta(seconds=59)) == NOON
    assert await last_seen(NOON + timedelta(seconds=60)) == NOON + timedelta(seconds=60)
    assert await last_seen(NOON + timedelta(seconds=90)) == NOON + timedelta(seconds=60)


async def test_ending_a_guests_session_deletes_the_guest(db: AsyncSession) -> None:
    """Nothing could ever log in to that account again, so nothing of it is kept."""
    token, principal = await sessions.start_guest_session(db, now=NOON)
    _, other = await sessions.start_guest_session(db, now=NOON)

    await sessions.end_session(db, principal.session_id)
    await db.commit()

    assert await sessions.authenticate(db, token, now=NOON) is None
    assert await db.get(User, principal.user_id) is None
    assert await db.get(User, other.user_id) is not None  # only that guest
    assert (await count(db, User), await count(db, BrowserSession)) == (1, 1)


async def test_ending_a_session_twice_is_harmless(db: AsyncSession) -> None:
    _, principal = await sessions.start_guest_session(db, now=NOON)

    await sessions.end_session(db, principal.session_id)
    await sessions.end_session(db, principal.session_id)

    assert await count(db, BrowserSession) == 0


async def test_deleting_an_account_deletes_its_sessions(db: AsyncSession) -> None:
    """The database enforces it, so no code path can leave a session without an account."""
    token, principal = await sessions.start_guest_session(db, now=NOON)
    await db.commit()

    await db.execute(text("DELETE FROM users WHERE id = :id"), {"id": principal.user_id})
    await db.commit()

    assert await sessions.authenticate(db, token, now=NOON) is None
    assert await count(db, BrowserSession) == 0
