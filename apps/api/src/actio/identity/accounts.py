"""Lasting accounts: signing up, logging in, changing the password.

There is no email sending here, so no "confirm your address" and no "forgot my
password" link: whoever knows the password owns the account.

Like `sessions`, these functions never commit, and take the time as `now`.
"""

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from actio.core.ids import new_id
from actio.identity import sessions
from actio.identity.models import User
from actio.identity.passwords import Passwords
from actio.identity.sessions import Principal

_UNIQUE_VIOLATION = "23505"  # Postgres's code for "this value is already taken"


class EmailTakenError(Exception):
    """Another account already uses this email address."""


class NotAGuestError(Exception):
    """Signing up is for guests and newcomers: this session already has an account."""


class WrongPasswordError(Exception):
    """The current password, given in order to change it, was wrong."""


def normalize_email(email: str) -> str:
    """One address is one account, however it was typed."""
    return email.strip().lower()


async def sign_up(
    db: AsyncSession,
    passwords: Passwords,
    *,
    email: str,
    password: str,
    current: Principal | None,
    now: datetime,
) -> tuple[str, Principal]:
    """Create a lasting account and log in to it. Returns the new session's token.

    A guest who signs up becomes the account: the same row, so everything the guest
    owns stays theirs. Either way the session is new, because what its holder may do
    has changed.
    """
    if current is not None and not current.is_guest:
        raise NotAGuestError
    password_hash = await passwords.hash(password)
    try:
        async with db.begin_nested():  # a taken address undoes only this attempt
            user = await db.get(User, current.user_id) if current is not None else None
            if user is None:
                user = User(id=new_id(), created_at=now)
                db.add(user)
            user.is_guest = False
            user.expires_at = None
            user.email = normalize_email(email)
            user.password_hash = password_hash
            await db.flush()
    except IntegrityError as error:
        if getattr(error.orig, "sqlstate", None) == _UNIQUE_VIOLATION:
            raise EmailTakenError from error
        raise
    await sessions.end_all_sessions(db, user.id)  # the guest's old token stops working
    return await sessions.start_session(db, user, now=now)


async def log_in(
    db: AsyncSession, passwords: Passwords, *, email: str, password: str, now: datetime
) -> tuple[str, Principal] | None:
    """Start a session for the account with this email and password, or `None`.

    An unknown address and a wrong password give the same answer, after the same work.
    """
    user = await db.scalar(select(User).where(User.email == normalize_email(email)))
    known_hash = user.password_hash if user is not None else None
    if not await passwords.verify(known_hash, password) or user is None or known_hash is None:
        return None
    if passwords.needs_rehash(known_hash):
        user.password_hash = await passwords.hash(password)
    return await sessions.start_session(db, user, now=now)


async def change_password(
    db: AsyncSession,
    passwords: Passwords,
    *,
    user_id: uuid.UUID,
    current_password: str,
    new_password: str,
    now: datetime,
) -> tuple[str, Principal]:
    """Replace an account's password. Returns a new session's token.

    Every other session ends too: if the old password had leaked, whoever used it is
    logged out by the change.
    """
    user = await db.get(User, user_id)
    known_hash = user.password_hash if user is not None else None
    if not await passwords.verify(known_hash, current_password) or user is None:
        raise WrongPasswordError
    user.password_hash = await passwords.hash(new_password)
    await db.flush()
    await sessions.end_all_sessions(db, user.id)
    return await sessions.start_session(db, user, now=now)
