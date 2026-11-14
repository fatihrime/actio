"""Routes for sessions and accounts.

- A guest: `POST /api/session/guest`.
- A lasting account: `POST /api/account` to sign up, `POST /api/session` to log in,
  `PUT /api/account/password` to change the password.
- Either: `GET /api/me`, `DELETE /api/session` to log out here, `DELETE /api/sessions`
  to log out everywhere.

The routes that create something or try a password count the attempt first, against
the limits in `actio.api.rate_limits`, and answer 429 when there is no room.
"""

import logging
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.exc import SQLAlchemyError

from actio.api.dependencies import (
    NO_SESSION,
    AppResources,
    AppSettings,
    CurrentPrincipal,
    Database,
    Now,
    PasswordHashing,
    PrincipalIfAny,
    clear_session_cookie,
    set_session_cookie,
)
from actio.api.problems import Problem
from actio.api.rate_limits import Limits
from actio.chat import conversations
from actio.core.resources import Resources
from actio.identity import accounts, sessions
from actio.identity.passwords import MAX_LENGTH, MIN_LENGTH
from actio.identity.sessions import Principal

logger = logging.getLogger(__name__)
router = APIRouter(tags=["identity"])

TOO_MANY: dict[int | str, dict[str, Any]] = {
    429: {"description": "Too many attempts. `Retry-After` says how long to wait."},
    503: {"description": "Attempts can't be counted right now, so this one is refused."},
}


class Me(BaseModel):
    """Who the session belongs to."""

    id: uuid.UUID
    is_guest: bool
    email: str | None
    session_expires_at: datetime

    @classmethod
    def of(cls, principal: Principal) -> "Me":
        return cls(
            id=principal.user_id,
            is_guest=principal.is_guest,
            email=principal.email,
            session_expires_at=principal.session_expires_at,
        )


class SignUp(BaseModel):
    email: EmailStr = Field(max_length=254)
    password: str = Field(min_length=MIN_LENGTH, max_length=MAX_LENGTH)


class LogIn(BaseModel):
    # No rules about their form here: a malformed address is simply a wrong login.
    email: str = Field(max_length=254)
    password: str = Field(max_length=MAX_LENGTH)


class PasswordChange(BaseModel):
    current_password: str = Field(max_length=MAX_LENGTH)
    new_password: str = Field(min_length=MIN_LENGTH, max_length=MAX_LENGTH)


@router.post("/api/session/guest", status_code=201, responses={200: {"model": Me}, **TOO_MANY})
async def start_guest_session(
    response: Response,
    background: BackgroundTasks,
    current: PrincipalIfAny,
    db: Database,
    limits: Limits,
    resources: AppResources,
    now: Now,
    settings: AppSettings,
) -> Me:
    """Start a session as a new guest, and set its cookie.

    A browser that already has a live session keeps it and gets 200: opening the page
    twice doesn't create two guests. 429 when too many guests were created from the
    client's address.
    """
    if current is not None:
        response.status_code = 200
        return Me.of(current)
    await limits.new_guest()
    token, principal = await sessions.start_guest_session(db, now=now)
    await db.commit()
    set_session_cookie(response, settings, token, principal, now=now)
    # Each new guest makes room by clearing out some expired ones, so their rows can't
    # pile up. After the response: the newcomer doesn't wait for it.
    background.add_task(_delete_expired, resources, now)
    return Me.of(principal)


async def _delete_expired(resources: Resources, now: datetime) -> None:
    """Delete a batch of expired guests and sessions, as its own unit of work."""
    try:
        async with resources.database() as db:
            await sessions.delete_expired(db, now=now)
            await db.commit()
    except (SQLAlchemyError, OSError) as error:
        # Nothing is lost: what wasn't deleted now is still expired at the next sweep.
        logger.warning("deleting expired guests failed (%s)", type(error).__name__)


@router.post(
    "/api/account",
    status_code=201,
    responses={
        409: {"description": "The email address has an account, or you are logged in to one."},
        **TOO_MANY,
    },
)
async def sign_up(
    body: SignUp,
    response: Response,
    current: PrincipalIfAny,
    db: Database,
    passwords: PasswordHashing,
    limits: Limits,
    now: Now,
    settings: AppSettings,
) -> Me:
    """Create a lasting account, and log in to it.

    A guest who signs up keeps the same account id and everything it owns. 409 when the
    address is already registered, or when this session already has an account. 429
    when too many sign-ups were tried from the client's address.
    """
    await limits.sign_up()
    try:
        token, principal = await accounts.sign_up(
            db, passwords, email=body.email, password=body.password, current=current, now=now
        )
    except accounts.EmailTakenError:
        raise Problem(409, "An account with this email address already exists.") from None
    except accounts.NotAGuestError:
        raise Problem(409, "You already have an account. Log out to create another.") from None
    await db.commit()
    set_session_cookie(response, settings, token, principal, now=now)
    return Me.of(principal)


@router.post(
    "/api/session",
    responses={401: {"description": "Wrong email address or password."}, **TOO_MANY},
)
async def log_in(
    body: LogIn,
    response: Response,
    current: PrincipalIfAny,
    db: Database,
    passwords: PasswordHashing,
    limits: Limits,
    now: Now,
    settings: AppSettings,
) -> Me:
    """Log in with an email address and a password, and set a new session's cookie.

    401 for an unknown address and for a wrong password alike. The session this browser
    had before, if any, ends. A guest's is deleted with the guest, whose conversations
    move to the account.

    429 after too many attempts from the client's address, or for this email address
    from anywhere. Every attempt counts, right or wrong, and a refused one isn't checked
    at all: the right password gets a 429 too.
    """
    await limits.log_in(body.email)
    started = await accounts.log_in(
        db, passwords, email=body.email, password=body.password, now=now
    )
    if started is None:
        raise Problem(401, "Wrong email address or password.")
    token, principal = started
    if current is not None:
        if current.is_guest:
            # The guest turned out to have an account: what they started as a guest
            # goes with them, before the guest is deleted.
            await conversations.move_all(db, from_user=current.user_id, to_user=principal.user_id)
        await sessions.end_session(db, current.session_id)
    await db.commit()
    set_session_cookie(response, settings, token, principal, now=now)
    return Me.of(principal)


@router.put(
    "/api/account/password",
    status_code=204,
    responses={
        **NO_SESSION,
        403: {"description": "The current password is wrong."},
        409: {"description": "A guest has no password to change."},
        **TOO_MANY,
    },
)
async def change_password(
    body: PasswordChange,
    response: Response,
    current: CurrentPrincipal,
    db: Database,
    passwords: PasswordHashing,
    limits: Limits,
    now: Now,
    settings: AppSettings,
) -> None:
    """Change the password. Every other browser is logged out; this one gets a new cookie.

    403 when the current password is wrong; 409 for a guest, who has none. 429 after
    too many password attempts for this account, logins included.
    """
    if current.email is None:
        raise Problem(409, "A guest has no password. Sign up with POST /api/account.")
    await limits.change_password(current.email)
    try:
        token, principal = await accounts.change_password(
            db,
            passwords,
            user_id=current.user_id,
            current_password=body.current_password,
            new_password=body.new_password,
            now=now,
        )
    except accounts.WrongPasswordError:
        raise Problem(403, "The current password is wrong.") from None
    await db.commit()
    set_session_cookie(response, settings, token, principal, now=now)


@router.get("/api/me", responses=NO_SESSION)
async def me(principal: CurrentPrincipal) -> Me:
    """Who you are, according to your session cookie. 401 without a live session."""
    return Me.of(principal)


@router.delete("/api/session", status_code=204)
async def end_session(
    response: Response, current: PrincipalIfAny, db: Database, settings: AppSettings
) -> None:
    """Log out of this browser, and clear its cookie. A guest's account is deleted.

    Always 204: ending a session that is already gone is not an error.
    """
    if current is not None:
        await sessions.end_session(db, current.session_id)
        await db.commit()
    clear_session_cookie(response, settings)


@router.delete("/api/sessions", status_code=204, responses=NO_SESSION)
async def end_all_sessions(
    response: Response, current: CurrentPrincipal, db: Database, settings: AppSettings
) -> None:
    """Log out of every browser at once, this one included."""
    await sessions.end_all_sessions(db, current.user_id)
    await db.commit()
    clear_session_cookie(response, settings)
