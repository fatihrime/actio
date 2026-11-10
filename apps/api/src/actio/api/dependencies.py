"""What routes ask for: a database session, the time, and who is asking."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from actio.api.problems import Problem
from actio.core.resources import Resources
from actio.core.settings import Settings
from actio.identity import sessions
from actio.identity.sessions import Principal


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_resources(request: Request) -> Resources:
    resources: Resources = request.app.state.resources
    return resources


async def get_database(
    resources: Annotated[Resources, Depends(get_resources)],
) -> AsyncIterator[AsyncSession]:
    """One database session for the request. The route commits when its work is done;
    what isn't committed is rolled back when the session closes."""
    async with resources.database() as db:
        yield db


def get_now() -> datetime:
    """The current time. A dependency, so tests can set it."""
    return datetime.now(UTC)


Database = Annotated[AsyncSession, Depends(get_database)]
Now = Annotated[datetime, Depends(get_now)]
AppSettings = Annotated[Settings, Depends(get_settings)]


# --- The session cookie ---------------------------------------------------------------


def cookie_name(settings: Settings) -> str:
    """`__Host-` makes a browser accept the cookie only from this exact host, over HTTPS,
    for the whole site. Browsers refuse the prefix over plain HTTP, so it is used only
    when Actio is served over HTTPS."""
    return "__Host-actio_session" if _is_https(settings) else "actio_session"


def set_session_cookie(
    response: Response, settings: Settings, token: str, *, expires_at: datetime, now: datetime
) -> None:
    response.set_cookie(
        cookie_name(settings),
        token,
        max_age=max(0, int((expires_at - now).total_seconds())),
        path="/",
        secure=_is_https(settings),
        httponly=True,  # page scripts can't read it
        samesite="lax",  # other sites' pages can't send it with a request that changes something
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        cookie_name(settings), path="/", secure=_is_https(settings), httponly=True, samesite="lax"
    )


def _is_https(settings: Settings) -> bool:
    return settings.public_url.startswith("https://")


# --- Who is asking --------------------------------------------------------------------


async def get_principal_if_any(
    request: Request, db: Database, now: Now, settings: AppSettings
) -> Principal | None:
    """Who the request's session cookie belongs to, or `None`."""
    token = request.cookies.get(cookie_name(settings))
    if not token:
        return None
    principal = await sessions.authenticate(db, token, now=now)
    await db.commit()  # keeps `last_seen_at`, when it was due
    return principal


async def get_principal(
    principal: Annotated[Principal | None, Depends(get_principal_if_any)],
) -> Principal:
    """Who is asking. Answers 401 when the request has no live session."""
    if principal is None:
        raise Problem(401, "This needs a session. Start one with POST /api/session/guest.")
    return principal


CurrentPrincipal = Annotated[Principal, Depends(get_principal)]
PrincipalIfAny = Annotated[Principal | None, Depends(get_principal_if_any)]
