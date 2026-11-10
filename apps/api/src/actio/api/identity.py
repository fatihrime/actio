"""Routes for sessions: start one as a guest, see who you are, end it."""

import uuid
from datetime import datetime

from fastapi import APIRouter, Response
from pydantic import BaseModel

from actio.api.dependencies import (
    AppSettings,
    CurrentPrincipal,
    Database,
    Now,
    PrincipalIfAny,
    clear_session_cookie,
    set_session_cookie,
)
from actio.identity import sessions
from actio.identity.sessions import Principal

router = APIRouter(tags=["identity"])


class Me(BaseModel):
    """Who the session belongs to."""

    id: uuid.UUID
    is_guest: bool
    session_expires_at: datetime

    @classmethod
    def of(cls, principal: Principal) -> "Me":
        return cls(
            id=principal.user_id,
            is_guest=principal.is_guest,
            session_expires_at=principal.session_expires_at,
        )


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
    set_session_cookie(response, settings, token, expires_at=principal.session_expires_at, now=now)
    return Me.of(principal)


@router.get("/api/me")
async def me(principal: CurrentPrincipal) -> Me:
    """Who you are, according to your session cookie. 401 without a live session."""
    return Me.of(principal)


@router.delete("/api/session", status_code=204)
async def end_session(
    response: Response, current: PrincipalIfAny, db: Database, settings: AppSettings
) -> None:
    """End the session and clear its cookie. A guest's account is deleted with it.

    Always 204: ending a session that is already gone is not an error.
    """
    if current is not None:
        await sessions.end_session(db, current.session_id)
        await db.commit()
    clear_session_cookie(response, settings)
