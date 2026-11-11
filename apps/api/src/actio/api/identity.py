"""Routes for sessions and accounts.

- A guest: `POST /api/session/guest`.
- A lasting account: `POST /api/account` to sign up, `POST /api/session` to log in,
  `PUT /api/account/password` to change the password.
- Either: `GET /api/me`, `DELETE /api/session` to log out here, `DELETE /api/sessions`
  to log out everywhere.
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Response
from pydantic import BaseModel, EmailStr, Field

from actio.api.dependencies import (
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
from actio.identity import accounts, sessions
from actio.identity.passwords import MAX_LENGTH, MIN_LENGTH
from actio.identity.sessions import Principal

router = APIRouter(tags=["identity"])


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


@router.post("/api/session/guest", status_code=201, responses={200: {"model": Me}})
async def start_guest_session(
    response: Response, current: PrincipalIfAny, db: Database, now: Now, settings: AppSettings
) -> Me:
    """Start a session as a new guest, and set its cookie.

    A browser that already has a live session keeps it and gets 200: opening the page
    twice doesn't create two guests.
    """
    if current is not None:
        response.status_code = 200
        return Me.of(current)
    token, principal = await sessions.start_guest_session(db, now=now)
    await db.commit()
    set_session_cookie(response, settings, token, principal, now=now)
    return Me.of(principal)


@router.post("/api/account", status_code=201)
async def sign_up(
    body: SignUp,
    response: Response,
    current: PrincipalIfAny,
    db: Database,
    passwords: PasswordHashing,
    now: Now,
    settings: AppSettings,
) -> Me:
    """Create a lasting account, and log in to it.

    A guest who signs up keeps the same account id and everything it owns. 409 when the
    address is already registered, or when this session already has an account.
    """
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


@router.post("/api/session")
async def log_in(
    body: LogIn,
    response: Response,
    current: PrincipalIfAny,
    db: Database,
    passwords: PasswordHashing,
    now: Now,
    settings: AppSettings,
) -> Me:
    """Log in with an email address and a password, and set a new session's cookie.

    401 for an unknown address and for a wrong password alike. The session this browser
    had before, if any, ends: a guest's is deleted with the guest.
    """
    started = await accounts.log_in(
        db, passwords, email=body.email, password=body.password, now=now
    )
    if started is None:
        raise Problem(401, "Wrong email address or password.")
    token, principal = started
    if current is not None:
        await sessions.end_session(db, current.session_id)
    await db.commit()
    set_session_cookie(response, settings, token, principal, now=now)
    return Me.of(principal)


@router.put("/api/account/password", status_code=204)
async def change_password(
    body: PasswordChange,
    response: Response,
    current: CurrentPrincipal,
    db: Database,
    passwords: PasswordHashing,
    now: Now,
    settings: AppSettings,
) -> None:
    """Change the password. Every other browser is logged out; this one gets a new cookie.

    403 when the current password is wrong; 409 for a guest, who has none.
    """
    if current.is_guest:
        raise Problem(409, "A guest has no password. Sign up with POST /api/account.")
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


@router.get("/api/me")
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


@router.delete("/api/sessions", status_code=204)
async def end_all_sessions(
    response: Response, current: CurrentPrincipal, db: Database, settings: AppSettings
) -> None:
    """Log out of every browser at once, this one included."""
    await sessions.end_all_sessions(db, current.user_id)
    await db.commit()
    clear_session_cookie(response, settings)
