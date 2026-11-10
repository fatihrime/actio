"""Sessions: starting one for a guest, recognizing a token, ending one.

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
"""How long a guest account and its session last."""

SEEN_RESOLUTION = timedelta(minutes=1)
"""`last_seen_at` is written at most this often, so reading a session isn't a write."""


@dataclass(frozen=True, slots=True)
class Principal:
    """Who is asking, as established from a session token."""

    user_id: uuid.UUID
    is_guest: bool
    session_id: uuid.UUID
    session_expires_at: datetime


def hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


async def start_guest_session(db: AsyncSession, *, now: datetime) -> tuple[str, Principal]:
    """Create a guest account with its session. Returns the session's token and who it is."""
    expires_at = now + GUEST_LIFETIME
    user = User(id=new_id(), is_guest=True, created_at=now, expires_at=expires_at)
    token = secrets.token_urlsafe(32)
    session = BrowserSession(
        id=new_id(),
        user_id=user.id,
        token_hash=hash_token(token),
        created_at=now,
        last_seen_at=now,
        expires_at=expires_at,
    )
    db.add(user)
    await db.flush()  # the account first: the session's row points at it
    db.add(session)
    await db.flush()
    return token, Principal(user.id, user.is_guest, session.id, session.expires_at)


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
        await db.flush()
    return Principal(user.id, user.is_guest, session.id, session.expires_at)


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
