"""Deleting what has expired: guests whose day is over, and sessions nobody used."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from actio.core.database import create_engine, create_session_factory
from actio.identity import accounts, sessions
from actio.identity.models import BrowserSession, User
from actio.identity.passwords import Passwords
from actio.identity.sessions import ACCOUNT_IDLE_LIFETIME, GUEST_LIFETIME

pytestmark = pytest.mark.anyio

NOON = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
DENIZ = {"email": "deniz@example.com", "password": "correct horse battery staple"}


async def count(db: AsyncSession, model: type) -> int:
    return await db.scalar(select(func.count()).select_from(model)) or 0


async def test_expired_guests_are_deleted_with_their_sessions(db: AsyncSession) -> None:
    _, old = await sessions.start_guest_session(db, now=NOON)
    _, new = await sessions.start_guest_session(db, now=NOON + timedelta(hours=1))
    await db.commit()

    deleted = await sessions.delete_expired(db, now=NOON + GUEST_LIFETIME)
    await db.commit()

    assert deleted == (1, 0)  # the guest's session went with the guest
    assert await db.get(User, old.user_id) is None
    assert await db.get(User, new.user_id) is not None
    assert (await count(db, User), await count(db, BrowserSession)) == (1, 1)


async def test_a_guest_is_kept_until_the_moment_it_expires(db: AsyncSession) -> None:
    """The same moment at which its session stops working: never deleted while usable."""
    token, _ = await sessions.start_guest_session(db, now=NOON)
    last_moment = NOON + GUEST_LIFETIME - timedelta(microseconds=1)

    assert await sessions.delete_expired(db, now=last_moment) == (0, 0)
    assert await sessions.authenticate(db, token, now=last_moment) is not None

    assert await sessions.authenticate(db, token, now=NOON + GUEST_LIFETIME) is None
    assert await sessions.delete_expired(db, now=NOON + GUEST_LIFETIME) == (1, 0)


async def test_an_account_outlives_its_expired_sessions(
    db: AsyncSession, passwords: Passwords
) -> None:
    _, on_the_laptop = await accounts.sign_up(db, passwords, **DENIZ, current=None, now=NOON)
    later = NOON + timedelta(days=10)
    logged_in = await accounts.log_in(db, passwords, **DENIZ, now=later)
    assert logged_in is not None
    phone_token, on_the_phone = logged_in
    await db.commit()

    # 30 days after the laptop was last used; the phone was used 10 days later.
    deleted = await sessions.delete_expired(db, now=NOON + ACCOUNT_IDLE_LIFETIME)
    await db.commit()

    assert deleted == (0, 1)
    assert await db.get(BrowserSession, on_the_laptop.session_id) is None
    assert await db.get(BrowserSession, on_the_phone.session_id) is not None
    assert await db.get(User, on_the_laptop.user_id) is not None
    assert await sessions.authenticate(db, phone_token, now=NOON + ACCOUNT_IDLE_LIFETIME)


async def test_a_guest_who_signed_up_is_not_swept_as_a_guest(
    db: AsyncSession, passwords: Passwords
) -> None:
    _, guest = await sessions.start_guest_session(db, now=NOON)
    _, account = await accounts.sign_up(db, passwords, **DENIZ, current=guest, now=NOON)
    await db.commit()

    deleted = await sessions.delete_expired(db, now=NOON + GUEST_LIFETIME)

    assert deleted == (0, 0)
    assert account.user_id == guest.user_id
    assert await db.get(User, guest.user_id) is not None


async def test_a_sweep_deletes_a_batch_at_most_and_the_oldest_first(db: AsyncSession) -> None:
    guests = [
        (await sessions.start_guest_session(db, now=NOON + timedelta(minutes=minute)))[1]
        for minute in (30, 0, 20, 10, 40)
    ]
    await db.commit()
    all_expired = NOON + timedelta(days=2)

    # Two guests go, with their sessions. Then two more expired sessions go on their own:
    # those of guests left for the next sweep.
    assert await sessions.delete_expired(db, now=all_expired, limit=2) == (2, 2)
    await db.commit()

    left = set((await db.scalars(select(User.id))).all())
    assert left == {guests[0].user_id, guests[2].user_id, guests[4].user_id}  # the newest three
    assert await count(db, BrowserSession) == 1
    assert await sessions.delete_expired(db, now=all_expired, limit=2) == (2, 1)
    assert await sessions.delete_expired(db, now=all_expired, limit=2) == (1, 0)
    assert await sessions.delete_expired(db, now=all_expired, limit=2) == (0, 0)
    assert (await count(db, User), await count(db, BrowserSession)) == (0, 0)


async def test_a_sweep_skips_rows_another_transaction_holds(
    db: AsyncSession, migrated: str
) -> None:
    """Two sweeps at once, or a sweep and a guest signing up at that moment, don't wait
    for each other: a held row is left for the next sweep."""
    _, held = await sessions.start_guest_session(db, now=NOON)
    await sessions.start_guest_session(db, now=NOON)  # one that nothing holds
    await db.commit()
    engine = create_engine(migrated)
    try:
        async with create_session_factory(engine)() as other:
            await other.execute(select(User).where(User.id == held.user_id).with_for_update())

            guests, _ = await sessions.delete_expired(db, now=NOON + GUEST_LIFETIME)
            await db.commit()

            assert guests == 1
            assert set((await db.scalars(select(User.id))).all()) == {held.user_id}
        # The other transaction is over, and left the row as it was: now it goes too.
        assert await sessions.delete_expired(db, now=NOON + GUEST_LIFETIME) == (1, 0)
        assert await count(db, User) == 0
    finally:
        await engine.dispose()
