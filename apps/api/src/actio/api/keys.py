"""Routes for API keys.

- `POST /api/keys`: make a key, shown whole in this answer alone;
- `GET /api/keys`: the account's keys, and how many its plan allows;
- `DELETE /api/keys/{key_id}`: revoke one;
- `GET /api/key`: what the key a request carries is, whose, and on which plan.

The first three take a session, never a key: a key can't make, list or revoke keys. The
fourth takes a key, and ignores a session's cookie (`CurrentKey`). How many keys an
account may have is its plan's to say, asked when a key is made and at every use of
one: a guest has none, whatever its plan says (ADR 0033).
"""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter
from pydantic import StringConstraints

from actio.api.body import Body
from actio.api.dependencies import (
    NO_KEY,
    NO_SESSION,
    CurrentKey,
    CurrentPrincipal,
    Database,
    NewId,
    Now,
    Usage,
    keys_allowed,
    no_keys_on,
)
from actio.api.described import Described
from actio.api.problems import Problem
from actio.api.usage import PlanOut
from actio.identity import keys
from actio.identity.models import ApiKey

router = APIRouter(tags=["keys"])

NAME_MAX = 64
"""The longest name a key may have, in characters."""


class KeyOut(Described):
    """An API key, as its account sees it in a list: never the key itself."""

    id: uuid.UUID
    """The id to revoke it by: `DELETE /api/keys/{key_id}`."""
    name: str
    """What its account called it when it was made."""
    prefix: str
    """The key's first twelve characters, such as `act_7Qm2xZp9`: enough for whoever
    holds the key to tell which one it is, and not enough to use it."""
    created_at: datetime
    last_used_at: datetime | None
    """When a request last carried the key, written at most once a minute: used again
    within the minute, it keeps the time of the use before. Empty until a request
    has carried it."""

    @classmethod
    def of(cls, key: ApiKey) -> "KeyOut":
        return cls(
            id=key.id,
            name=key.name,
            prefix=key.prefix,
            created_at=key.created_at,
            last_used_at=key.last_used_at,
        )


class NewKey(KeyOut):
    """A key that was just made, with the key itself."""

    key: str
    """The whole key, to send as `Authorization: Bearer …` or in `x-api-key`. This
    answer is the only one that ever holds it: Actio keeps only what recognizes it, and
    can't show it again. Keep it as a password is kept."""


class KeyList(Described):
    keys: list[KeyOut]
    """Your keys, oldest first."""
    at_most: int
    """How many keys your plan lets you have at once: 0 for a guest, and for a plan that
    allows none. A key of an account whose plan allows none stops working, and is
    still listed, to be revoked."""


class KeyAccount(Described):
    """The account a key belongs to."""

    id: uuid.UUID
    email: str
    """Its email address."""


class KeyInUse(KeyOut):
    """The key a request carries: what it is, whose it is, and the plan that lets it be
    used."""

    account: KeyAccount
    plan: PlanOut
    """The plan of the key's account, as `GET /api/usage` names it."""


class MakeKey(Body):
    name: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=NAME_MAX)
    ]
    """What to call the key, so that you can tell it from your others: 1 to 64
    characters, without the spaces around them. Two keys may have the same name."""


@router.post(
    "/api/keys",
    status_code=201,
    responses={
        **NO_SESSION,
        403: {"description": "A guest can't make API keys, nor an account whose plan allows none."},
        409: {"description": "You have as many API keys as your plan allows, or more."},
    },
)
async def make_key(
    body: MakeKey,
    principal: CurrentPrincipal,
    db: Database,
    usage: Usage,
    now: Now,
    new_id: NewId,
) -> NewKey:
    """Make an API key. The answer holds the whole key, `key`, and no other answer ever
    will: keep it.

    403 for a guest, and for an account whose plan allows no keys; 409 when the account
    has as many keys as its plan allows, or more. Nothing is made then."""
    if principal.is_guest:
        raise Problem(403, "A guest can't make API keys. Sign up first, with POST /api/account.")
    plan, at_most = keys_allowed(
        usage.plans, is_guest=principal.is_guest, own_plan=principal.own_plan
    )
    if at_most == 0:
        raise Problem(403, no_keys_on(plan))
    try:
        text, made = await keys.make(
            db,
            user_id=principal.user_id,
            name=body.name,
            at_most=at_most,
            key_id=new_id(),
            now=now,
        )
    except keys.TooManyKeysError as error:
        raise Problem(409, _too_many(error.held, error.at_most, plan.title)) from None
    await db.commit()
    return NewKey(**KeyOut.of(made).model_dump(), key=text)


@router.get("/api/keys", responses=NO_SESSION)
async def list_keys(principal: CurrentPrincipal, db: Database, usage: Usage) -> KeyList:
    """Your API keys, oldest first, each by its prefix and never whole, and how many
    your plan lets you have."""
    _, at_most = keys_allowed(usage.plans, is_guest=principal.is_guest, own_plan=principal.own_plan)
    found = await keys.of_account(db, principal.user_id)
    return KeyList(keys=[KeyOut.of(key) for key in found], at_most=at_most)


@router.delete(
    "/api/keys/{key_id}",
    status_code=204,
    responses={**NO_SESSION, 404: {"description": "You have no API key with this id."}},
)
async def revoke_key(key_id: uuid.UUID, principal: CurrentPrincipal, db: Database) -> None:
    """Revoke an API key: from the next request it is answered as one that never was.
    There is no way back."""
    if not await keys.revoke(db, key_id=key_id, user_id=principal.user_id):
        raise Problem(404, "You have no API key with this id.")
    await db.commit()


@router.get("/api/key", responses=NO_KEY)
async def get_key(used: CurrentKey) -> KeyInUse:
    """What the API key this request carries is, whose it is, and the plan that lets it
    be used. It takes a key, in `Authorization: Bearer …` or in `x-api-key`, and no
    session: a session's cookie is not looked at."""
    holder = used.holder
    return KeyInUse(
        id=holder.key_id,
        name=holder.name,
        prefix=holder.prefix,
        created_at=holder.created_at,
        last_used_at=holder.last_used_at,
        account=KeyAccount(id=holder.user_id, email=holder.email),
        plan=PlanOut(id=used.plan.name, title=used.plan.title),
    )


def _too_many(held: int, at_most: int, title: str) -> str:
    """Why a key isn't made: the account has as many keys as its plan allows, or more,
    since a plan can be changed under keys that were made before."""
    if held == at_most:
        return (
            f'You have {_keys(held)}, as many as your plan, "{title}", allows. Revoke one to'
            " make another."
        )
    to_revoke = held - at_most + 1
    return (
        f'You have {_keys(held)}, and your plan, "{title}", allows {at_most}. Revoke'
        f" {to_revoke} to make another."
    )


def _keys(count: int) -> str:
    return "1 API key" if count == 1 else f"{count} API keys"
