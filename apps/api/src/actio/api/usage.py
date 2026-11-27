"""The route that says how much of its usage limits an account has used.

`GET /api/usage` answers with the caller's plan and its windows: the session, which
opens with the first question and lasts some hours, and the week. What a question may
cost, and when it is refused, is decided where turns start (`actio.api.turns`); this
only reads.
"""

from datetime import datetime
from typing import Literal

from fastapi import APIRouter

from actio.api.dependencies import NO_SESSION, CurrentPrincipal, Database, Now, Usage
from actio.api.described import Described
from actio.api.problems import Problem
from actio.usage.limits import Account, UsageUnavailableError

router = APIRouter(tags=["usage"], responses=NO_SESSION)

UNAVAILABLE = "Usage can't be counted right now. Try again in a moment."


class PlanOut(Described):
    id: str
    title: str
    """What to call it, such as `Free`."""


class WindowOut(Described):
    """One limit: so many credits, until the window starts over."""

    name: Literal["session", "week"]
    limit: int
    """How many credits the window allows."""
    used: int
    """How many are used up. It can pass `limit` by a little: an answer is counted for
    what it really used, which is known only when it has been written."""
    reserved: int
    """How many are held for answers that are being written right now. They are not
    available either, until those answers end and are counted."""
    resets_at: datetime | None
    """When the window starts over, with nothing used. Empty for a session that isn't
    open: the next question that costs something opens it."""


class UsageOut(Described):
    plan: PlanOut
    windows: list[WindowOut]
    """The limits of the plan, each of which a question has to fit in. A plan without
    limits has none."""


@router.get(
    "/api/usage",
    responses={503: {"description": "Usage can't be read right now."}},
)
async def get_usage(principal: CurrentPrincipal, db: Database, usage: Usage, now: Now) -> UsageOut:
    """Your plan, and how much of each of its limits you have used."""
    account = Account(principal.user_id, is_guest=principal.is_guest, own_plan=principal.own_plan)
    try:
        status = await usage.status(db, account, now=now)
    except UsageUnavailableError:
        raise Problem(503, UNAVAILABLE) from None
    return UsageOut(
        plan=PlanOut(id=status.plan.name, title=status.plan.title),
        windows=[
            WindowOut(
                name=window.name,
                limit=window.limit,
                used=window.used,
                reserved=window.reserved,
                resets_at=window.resets_at,
            )
            for window in status.windows
        ],
    )
