"""Sessions: starting one, recognizing a token, ending one.

A session token is 32 random bytes, shown once: it goes into the browser's cookie, and
the database keeps only its SHA-256. A fast hash is enough here, unlike for passwords:
the token is random, so there is nothing to guess faster.

These functions change rows but never commit: the caller decides when the unit of work
is done. Time comes in as `now`, so tests can move it.
"""

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from actio.core.ids import new_id
from actio.identity.models import BrowserSession, User

GUEST_LIFETIME = timedelta(hours=24)
"""How long a guest account and its session last, from the moment they are created."""

ACCOUNT_IDLE_LIFETIME = timedelta(days=30)
"""An account's session ends after this long without a request. Each use renews it."""

SEEN_RESOLUTION = timedelta(minutes=1)
"""A session's row is written at most this often, so reading a session isn't a write."""

SWEEP_BATCH = 100
"""How many expired guests, and how many expired sessions, one sweep deletes at most."""


@dataclass(frozen=True, slots=True)
class Principal:
    """Who is asking, as established from a session token."""

    user_id: uuid.UUID
    is_guest: bool
    email: str | None
    session_id: uuid.UUID
    session_expires_at: datetime


def hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


async def start_session(db: AsyncSession, user: User, *, now: datetime) -> tuple[str, Principal]:
    """Start a session for `user`. Returns its token, shown this once, and who it is."""
    token = secrets.token_urlsafe(32)
    session = BrowserSession(
        id=new_id(),
        user_id=user.id,
        token_hash=hash_token(token),
        created_at=now,
        last_seen_at=now,
        expires_at=user.expires_at or now + ACCOUNT_IDLE_LIFETIME,
    )
    db.add(session)
    await db.flush()
    return token, _principal(user, session)


async def start_guest_session(db: AsyncSession, *, now: datetime) -> tuple[str, Principal]:
    """Create a guest account with its session. Both end when the guest's day is over."""
    user = User(id=new_id(), is_guest=True, created_at=now, expires_at=now + GUEST_LIFETIME)
    db.add(user)
    await db.flush()  # the account first: the session's row points at it
    return await start_session(db, user, now=now)


async def authenticate(db: AsyncSession, token: str, *, now: datetime) -> Principal | None:
    """Who holds `token`, or `None` if no live session has it."""
    found = (
        await db.execute(
            select(BrowserSession, User)
            .join(User, User.id == BrowserSession.user_id)
            .where(BrowserSession.token_hash == hash_token(token), BrowserSession.expires_at > now)
        )
    ).one_or_none()
    if found is None:
        return None
    session, user = found
    if now - session.last_seen_at >= SEEN_RESOLUTION:
        session.last_seen_at = now
        if not user.is_guest:  # a guest's session ends with the guest, used or not
            session.expires_at = now + ACCOUNT_IDLE_LIFETIME
        await db.flush()
    return _principal(user, session)


async def end_session(db: AsyncSession, session_id: uuid.UUID) -> None:
    """End a session at once: its token stops working. A guest's account goes with it,
    because nothing could ever log in to it again."""
    user_id = await db.scalar(
        delete(BrowserSession)
        .where(BrowserSession.id == session_id)
        .returning(BrowserSession.user_id)
    )
    if user_id is not None:
        await db.execute(delete(User).where(User.id == user_id, User.is_guest))


async def end_all_sessions(db: AsyncSession, user_id: uuid.UUID) -> None:
    """Log an account out everywhere: every browser's token stops working."""
    await db.execute(delete(BrowserSession).where(BrowserSession.user_id == user_id))
    await db.execute(delete(User).where(User.id == user_id, User.is_guest))


async def delete_expired(
    db: AsyncSession, *, now: datetime, limit: int = SWEEP_BATCH
) -> tuple[int, int]:
    """Delete what nobody can use any more: guests whose day is over, with everything
    they own, and sessions that have expired. Returns how many of each were deleted.

    Expired rows already can't be used; this only frees their space. So it works in
    batches of `limit`, oldest first, and skips rows another transaction holds instead
    of waiting for them: any number of sweeps can run at once.
    """
    guests = (
        select(User.id)
        .where(User.is_guest, User.expires_at <= now)
        .order_by(User.expires_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    deleted_guests = await db.scalars(delete(User).where(User.id.in_(guests)).returning(User.id))
    # What is left after the guests' own sessions went with them: accounts' sessions
    # that weren't used for too long.
    expired = (
        select(BrowserSession.id)
        .where(BrowserSession.expires_at <= now)
        .order_by(BrowserSession.expires_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    deleted_sessions = await db.scalars(
        delete(BrowserSession).where(BrowserSession.id.in_(expired)).returning(BrowserSession.id)
    )
    return len(deleted_guests.all()), len(deleted_sessions.all())


def _principal(user: User, session: BrowserSession) -> Principal:
    return Principal(user.id, user.is_guest, user.email, session.id, session.expires_at)
