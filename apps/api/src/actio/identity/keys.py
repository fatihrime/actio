"""API keys: making one, an account's keys, revoking one, recognizing one.

A key is `act_`, eight characters, `_`, and 43 more, 56 in all: digits and ASCII
letters, drawn with `secrets`. The 43 are a little over 256 bits. With no other sign in
it, a double-click selects the whole key. It is shown once, when it is made, and the
database keeps only its SHA-256, by which it is found: as a session's token, a fast hash
is enough, since a random key leaves nothing to guess faster (ADR 0033). Its first
twelve characters, `act_` and the eight, are its prefix, which a list shows so that
whoever holds the key can tell which row it is: no secret, and not unique.

Revoking a key deletes its row: from the next request it is answered as a key that
never was. How many keys an account may have is its plan's to say, which the caller
asks: nothing here reads the plans.

Like `sessions`, these functions change rows but never commit, and take the time as
`now`.
"""

import hashlib
import re
import secrets
import string
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from actio.identity.models import ApiKey, User

BEGINS = "act_"
"""What every key begins with, so that it can be told for one of Actio's."""

ALPHABET = string.ascii_letters + string.digits
"""What a key's two runs are drawn from: 62 characters."""

NAMED_BY = 8
"""How many characters follow `act_` and, with it, make the key's prefix."""

SECRET = 43
"""How many characters make the rest, after the second `_`."""

PREFIX_LENGTH = len(BEGINS) + NAMED_BY
"""The key's first twelve characters: what a list shows of it."""

_FORM = re.compile(f"{BEGINS}[0-9A-Za-z]{{{NAMED_BY}}}_[0-9A-Za-z]{{{SECRET}}}")

USED_RESOLUTION = timedelta(minutes=1)
"""A key's row is written at most this often, so that using a key isn't a write each
time: as a session's `last_seen_at` is."""


class TooManyKeysError(Exception):
    """The account has as many keys as it may have, or more."""

    def __init__(self, held: int, at_most: int) -> None:
        super().__init__(f"{held} keys, and {at_most} at most")
        self.held = held
        """How many keys the account has."""
        self.at_most = at_most
        """How many it may have."""


@dataclass(frozen=True, slots=True)
class KeyHolder:
    """Who is asking, as established from an API key: the key, and its account."""

    key_id: uuid.UUID
    name: str
    prefix: str
    created_at: datetime
    last_used_at: datetime | None
    """As it is after this use: written at most once every `USED_RESOLUTION`."""
    user_id: uuid.UUID
    email: str
    """The account's address. An account has one: a guest has none, and holds no key."""
    own_plan: str | None
    """The plan the account is on of its own, by its name in the plans file, or `None`
    for the one the file names for its kind: read with the key, from the account's row,
    so a change holds from its next request."""


def hash_key(text: str) -> bytes:
    return hashlib.sha256(text.encode()).digest()


def has_a_keys_form(text: str) -> bool:
    """Whether `text` has a key's form. One that hasn't is no key, without asking the
    database."""
    return _FORM.fullmatch(text) is not None


async def make(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    name: str,
    at_most: int,
    key_id: uuid.UUID,
    now: datetime,
) -> tuple[str, ApiKey]:
    """Make a key called `name` for the account `user_id`, which may have `at_most`
    keys. Returns the key, shown this once, and its row. Raises `TooManyKeysError` when
    the account has `at_most` keys already, or more.

    The account's row is held until the transaction ends, before its keys are counted:
    two keys made at once are counted one after the other, and the second sees the
    first."""
    await db.execute(select(User.id).where(User.id == user_id).with_for_update())
    held = await db.scalar(
        select(func.count()).select_from(ApiKey).where(ApiKey.user_id == user_id)
    )
    if (held or 0) >= at_most:
        raise TooManyKeysError(held or 0, at_most)
    text = f"{BEGINS}{_drawn(NAMED_BY)}_{_drawn(SECRET)}"
    key = ApiKey(
        id=key_id,
        user_id=user_id,
        name=name,
        prefix=text[:PREFIX_LENGTH],
        key_hash=hash_key(text),
        created_at=now,
        last_used_at=None,
    )
    db.add(key)
    await db.flush()
    return text, key


async def of_account(db: AsyncSession, user_id: uuid.UUID) -> list[ApiKey]:
    """The account's keys, oldest first."""
    found = select(ApiKey).where(ApiKey.user_id == user_id).order_by(ApiKey.created_at, ApiKey.id)
    return list((await db.scalars(found)).all())


async def revoke(db: AsyncSession, *, key_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    """Revoke the account's key `key_id`: its row is deleted, and it stops working at
    the next request. Whether the account had such a key: another account's is left
    alone, as one that isn't there."""
    revoked = await db.scalar(
        delete(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == user_id).returning(ApiKey.id)
    )
    return revoked is not None


async def recognize(db: AsyncSession, text: str, *, now: datetime) -> KeyHolder | None:
    """Who holds the key `text`, or `None`: for a text that hasn't a key's form, without
    a query, and for one that no row has. The key is read with its account's row, in
    one statement.

    A key is an account's. No route gives a guest one, and a key that a guest's row was
    given by hand is nobody's here: a guest is the row without an address."""
    if not has_a_keys_form(text):
        return None
    found = (
        await db.execute(
            select(ApiKey, User)
            .join(User, User.id == ApiKey.user_id)
            .where(ApiKey.key_hash == hash_key(text))
        )
    ).one_or_none()
    if found is None:
        return None
    key, user = found
    if user.email is None:
        return None
    if key.last_used_at is None or now - key.last_used_at >= USED_RESOLUTION:
        key.last_used_at = now
        await db.flush()
    return KeyHolder(
        key_id=key.id,
        name=key.name,
        prefix=key.prefix,
        created_at=key.created_at,
        last_used_at=key.last_used_at,
        user_id=user.id,
        email=user.email,
        own_plan=user.plan,
    )


def _drawn(length: int) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(length))
