"""What routes ask for: a database session, the time, and who is asking, by a session's
cookie or by an API key."""

import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import Depends, Request, Response
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from actio.api.problems import Problem
from actio.catalog import Catalog
from actio.chat.events import TurnEvents
from actio.chat.readers import TurnReaders
from actio.chat.running import TurnRunner
from actio.core.ids import new_id
from actio.core.resources import Resources
from actio.core.settings import Settings
from actio.identity import keys, sessions
from actio.identity.keys import KeyHolder
from actio.identity.passwords import Passwords
from actio.identity.sessions import Principal
from actio.usage.limits import UsageLimits
from actio.usage.plans import Plan, Plans


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


def get_clock() -> Callable[[], datetime]:
    """Something to read the time from, as often as needed. A dependency, so tests can
    set the time."""
    return _utc_now


def _utc_now() -> datetime:
    return datetime.now(UTC)


def get_now(clock: Annotated[Callable[[], datetime], Depends(get_clock)]) -> datetime:
    """The time the request arrived: one moment for everything the request does."""
    return clock()


def get_new_id() -> Callable[[], uuid.UUID]:
    """Something that makes ids for new rows. A dependency, so tests can know them
    beforehand."""
    return new_id


def get_passwords(request: Request) -> Passwords:
    passwords: Passwords = request.app.state.passwords
    return passwords


def get_catalog(request: Request) -> Catalog:
    catalog: Catalog = request.app.state.catalog
    return catalog


def get_usage_limits(request: Request) -> UsageLimits:
    usage: UsageLimits = request.app.state.usage
    return usage


def get_turn_events(request: Request) -> TurnEvents:
    events: TurnEvents = request.app.state.turn_events
    return events


def get_turn_runner(request: Request) -> TurnRunner:
    runner: TurnRunner = request.app.state.turn_runner
    return runner


def get_turn_readers(request: Request) -> TurnReaders:
    readers: TurnReaders = request.app.state.turn_readers
    return readers


Models = Annotated[Catalog, Depends(get_catalog)]
Events = Annotated[TurnEvents, Depends(get_turn_events)]
"""What running turns stream, to append to and to follow."""
Runner = Annotated[TurnRunner, Depends(get_turn_runner)]
"""The turns this process is writing: to start one, and to stop one."""
Readers = Annotated[TurnReaders, Depends(get_turn_readers)]
"""The responses this process has open that read along with a turn, per account."""
Usage = Annotated[UsageLimits, Depends(get_usage_limits)]
"""The usage limits: what an answer may cost its account. Not to be confused with
`Limits` (`actio.api.rate_limits`), which counts how often something is tried."""
Database = Annotated[AsyncSession, Depends(get_database)]
Now = Annotated[datetime, Depends(get_now)]
Clock = Annotated[Callable[[], datetime], Depends(get_clock)]
"""For a route that lasts: a streamed answer ends later than it began."""
NewId = Annotated[Callable[[], uuid.UUID], Depends(get_new_id)]
AppSettings = Annotated[Settings, Depends(get_settings)]
AppResources = Annotated[Resources, Depends(get_resources)]
PasswordHashing = Annotated[Passwords, Depends(get_passwords)]


# --- The session cookie ---------------------------------------------------------------


def cookie_name(settings: Settings) -> str:
    """`__Host-` makes a browser accept the cookie only from this exact host, over HTTPS,
    for the whole site. Browsers refuse the prefix over plain HTTP, so it is used only
    when Actio is served over HTTPS."""
    return "__Host-actio_session" if _is_https(settings) else "actio_session"


COOKIE_MAX_AGE_ACCOUNT = timedelta(days=400)
"""How long a browser keeps an account's cookie: the longest browsers allow. The
session itself ends sooner, on the server, after 30 days without a request."""


def set_session_cookie(
    response: Response, settings: Settings, token: str, principal: Principal, *, now: datetime
) -> None:
    """Hand the browser a session's token.

    A guest's cookie lasts exactly as long as the guest. An account's session is renewed
    on the server each time it is used, so its cookie just has to outlive it.
    """
    lifetime = principal.session_expires_at - now if principal.is_guest else COOKIE_MAX_AGE_ACCOUNT
    response.set_cookie(
        cookie_name(settings),
        token,
        max_age=max(0, int(lifetime.total_seconds())),
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

NO_SESSION: dict[int | str, dict[str, Any]] = {
    401: {"description": "No session, or it has ended. Start one with `POST /api/session/guest`."}
}
"""For the description of a route that takes `CurrentPrincipal`: pass it as the route's
`responses`, or its router's. A test fails when such a route doesn't say so."""


# --- Who is asking, by an API key -----------------------------------------------------


def keys_allowed(plans: Plans, *, is_guest: bool, own_plan: str | None) -> tuple[Plan, int]:
    """The plan of a guest, or of an account on `own_plan` of its own or on none, and
    how many API keys it may have at once on that plan: none for a guest, whatever its
    plan says. Asked when a key is made, when an account's keys are listed, and at every
    use of a key, so that a change of plan holds from the next request."""
    plan = plans.plan_for(is_guest=is_guest, own_plan=own_plan)
    return plan, 0 if is_guest else plan.api_keys


@dataclass(frozen=True, slots=True)
class KeyUse:
    """A request that carries an API key: who holds the key, and the plan that lets it
    be used."""

    holder: KeyHolder
    plan: Plan


_BEARER = HTTPBearer(
    scheme_name="bearer",
    description="An API key, sent as `Authorization: Bearer act_…`.",
    auto_error=False,
)
_X_API_KEY = APIKeyHeader(
    name="x-api-key",
    scheme_name="x-api-key",
    description="An API key, sent in the `x-api-key` header.",
    auto_error=False,
)

_CHALLENGE = {"WWW-Authenticate": "Bearer"}
"""What a 401 of a route that takes a key says it takes."""

UNKNOWN_KEY = "This API key isn't known: it may be mistyped, or revoked."


async def get_key_use(
    bearer: Annotated[HTTPAuthorizationCredentials | None, Depends(_BEARER)],
    in_header: Annotated[str | None, Depends(_X_API_KEY)],
    db: Database,
    now: Now,
    usage: Usage,
) -> KeyUse:
    """Who the request's API key belongs to, and on which plan. The key is read from
    `Authorization: Bearer`, the scheme in any case of its letters, or from `x-api-key`,
    without the spaces around it in either. A session's cookie is not looked at.

    Answers 401, with `WWW-Authenticate: Bearer`, for no key, for a text that isn't a
    key's form or that no key has, in the same words, and for two different keys in the
    two headers; 403 when the plan of the key's account allows no keys, asked at every
    use. Then commits, which keeps the key's `last_used_at` when it was due."""
    sent = {
        text.strip()
        for text in (bearer.credentials if bearer is not None else None, in_header)
        if text is not None and text.strip()
    }
    if not sent:
        raise Problem(
            401,
            "This needs an API key, sent as Authorization: Bearer followed by the key, or in"
            " x-api-key. An account makes one with POST /api/keys.",
            headers=_CHALLENGE,
        )
    if len(sent) > 1:
        raise Problem(
            401,
            "This request carries two different API keys, one in Authorization and one in"
            " x-api-key. Send one of them.",
            headers=_CHALLENGE,
        )
    [text] = sent
    holder = await keys.recognize(db, text, now=now)
    if holder is None:
        raise Problem(401, UNKNOWN_KEY, headers=_CHALLENGE)
    # Never a guest's: `keys.recognize` gives nobody for a key on a guest's row.
    plan, at_most = keys_allowed(usage.plans, is_guest=False, own_plan=holder.own_plan)
    if at_most == 0:
        raise Problem(403, no_keys_on(plan))
    await db.commit()  # keeps `last_used_at`, when it was due
    return KeyUse(holder, plan)


def no_keys_on(plan: Plan) -> str:
    """Why a plan's account can't make or use an API key."""
    return f'Your plan, "{plan.title}", allows no API keys.'


CurrentKey = Annotated[KeyUse, Depends(get_key_use)]
"""For a route that takes an API key instead of a session: `GET /api/key` is its one
user."""

NO_KEY: dict[int | str, dict[str, Any]] = {
    401: {
        "description": "No API key, one that isn't known, mistyped or revoked, or two "
        "different keys in `Authorization` and `x-api-key`. `WWW-Authenticate` says `Bearer`."
    },
    403: {"description": "The plan of the key's account allows no API keys."},
}
"""For the description of a route that takes `CurrentKey`: pass it as the route's
`responses`."""
