"""API keys below the API, against a real Postgres: making one, an account's keys,
revoking one and recognizing one. No key is written in this file: each is made when the
test runs."""

import hashlib
import re
import secrets
import string
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from actio.core.database import create_engine, create_session_factory
from actio.core.ids import new_id
from actio.identity import keys
from actio.identity.models import ApiKey, User

pytestmark = pytest.mark.anyio

NOON = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
NOWHERE_DATABASE = "postgresql://actio:unused@127.0.0.1:1/actio"  # nothing listens on port 1

FORM = re.compile(r"act_[0-9A-Za-z]{8}_[0-9A-Za-z]{43}")
"""A key's form, as ADR 0033 states it: written here apart from the module's own."""


async def an_account(db: AsyncSession, email: str = "deniz@example.com") -> uuid.UUID:
    user = User(id=new_id(), is_guest=False, created_at=NOON, email=email, password_hash="a hash")
    db.add(user)
    await db.flush()
    return user.id


async def made(
    db: AsyncSession, user_id: uuid.UUID, name: str = "Script", *, at_most: int = 10
) -> tuple[str, ApiKey]:
    return await keys.make(
        db, user_id=user_id, name=name, at_most=at_most, key_id=new_id(), now=NOON
    )


def never_made() -> str:
    """A text of a key's form that no key has: drawn now, as a key is."""
    drawn = string.ascii_letters + string.digits

    def run(length: int) -> str:
        return "".join(secrets.choice(drawn) for _ in range(length))

    return f"act_{run(8)}_{run(43)}"


async def count(db: AsyncSession) -> int:
    return await db.scalar(select(func.count()).select_from(ApiKey)) or 0


async def test_a_key_has_its_form_and_two_keys_differ(db: AsyncSession) -> None:
    user_id = await an_account(db)

    first, _ = await made(db, user_id)
    second, _ = await made(db, user_id)

    assert FORM.fullmatch(first), first[:12]
    assert FORM.fullmatch(second), second[:12]
    assert len(first) == 56
    assert first != second
    assert first[:12] != second[:12]  # 62 ** 8 prefixes: two alike would be a fault


async def test_the_key_itself_is_never_stored(db: AsyncSession) -> None:
    """A copy of the database must not be enough to use a key: its row holds the key's
    SHA-256 and its first twelve characters, and no column holds the rest."""
    user_id = await an_account(db)
    key, row = await made(db, user_id)
    await db.commit()

    stored = (await db.execute(text("SELECT key_hash, prefix FROM api_keys"))).one()
    everything = (await db.execute(text("SELECT api_keys::text FROM api_keys"))).scalar_one()

    assert bytes(stored.key_hash) == hashlib.sha256(key.encode()).digest()
    assert stored.prefix == row.prefix == key[:12]
    assert key not in everything
    assert key[13:] not in everything  # the 43 after the prefix's `_`
    assert key[12:] not in everything


async def test_a_key_is_recognized_with_its_account(db: AsyncSession) -> None:
    user_id = await an_account(db)
    key, row = await made(db, user_id, "Nightly export")
    await db.commit()

    holder = await keys.recognize(db, key, now=NOON + timedelta(hours=1))

    assert holder == keys.KeyHolder(
        key_id=row.id,
        name="Nightly export",
        prefix=key[:12],
        created_at=NOON,
        last_used_at=NOON + timedelta(hours=1),
        user_id=user_id,
        email="deniz@example.com",
        own_plan=None,
    )


async def test_a_key_on_a_guests_row_is_nobodys(db: AsyncSession) -> None:
    """No route gives a guest a key. One that its row was given by hand, as here, is
    recognized as nobody, and isn't marked as used."""
    guest = User(id=new_id(), is_guest=True, created_at=NOON, expires_at=NOON + timedelta(days=1))
    db.add(guest)
    await db.flush()
    key, row = await made(db, guest.id)
    await db.commit()

    holder = await keys.recognize(db, key, now=NOON + timedelta(hours=1))
    await db.commit()
    await db.refresh(row)

    assert holder is None
    assert row.last_used_at is None


async def test_a_key_carries_its_accounts_own_plan(db: AsyncSession) -> None:
    user_id = await an_account(db)
    key, _ = await made(db, user_id)
    await db.execute(text("UPDATE users SET plan = 'dev' WHERE id = :id"), {"id": user_id})
    await db.commit()

    holder = await keys.recognize(db, key, now=NOON)

    assert holder is not None
    assert holder.own_plan == "dev"


async def test_a_key_that_no_row_has_is_nobody(db: AsyncSession) -> None:
    user_id = await an_account(db)
    await made(db, user_id)

    assert await keys.recognize(db, never_made(), now=NOON) is None


def not_of_a_keys_form() -> list[str]:
    """Texts that aren't a key's form, made from one that is."""
    key = never_made()
    return [
        "",
        "act_",
        key[:-1],  # a character short
        key + "x",  # one too many
        key.upper().replace("ACT_", "Act_"),
        key[:20] + "-" + key[21:],  # a sign a key never has
        key[:20] + "é" + key[21:],  # a letter that isn't ASCII
        key[:12] + "-" + key[13:],  # another sign where the `_` goes
        " " + key,  # spaces are the caller's to take off
        key + "\n",
        "Bearer " + key,
    ]


async def test_a_text_that_isnt_a_keys_form_is_nobody_without_asking_the_database() -> None:
    """The session below has no database behind it: a query would fail."""
    engine = create_engine(NOWHERE_DATABASE)
    try:
        async with create_session_factory(engine)() as nowhere:
            for sent in not_of_a_keys_form():
                assert await keys.recognize(nowhere, sent, now=NOON) is None, repr(sent[:12])
    finally:
        await engine.dispose()
    assert all(not keys.has_a_keys_form(sent) for sent in not_of_a_keys_form())
    assert keys.has_a_keys_form(never_made())


async def test_last_used_is_written_at_most_once_a_minute(db: AsyncSession) -> None:
    """Otherwise every request that carries a key would also write a row."""
    user_id = await an_account(db)
    key, row = await made(db, user_id)
    await db.commit()

    async def last_used(at: datetime) -> datetime | None:
        await keys.recognize(db, key, now=at)
        await db.commit()
        await db.refresh(row)
        return row.last_used_at

    assert row.last_used_at is None
    assert await last_used(NOON) == NOON
    assert await last_used(NOON + timedelta(seconds=59)) == NOON
    assert await last_used(NOON + timedelta(seconds=60)) == NOON + timedelta(seconds=60)
    assert await last_used(NOON + timedelta(seconds=90)) == NOON + timedelta(seconds=60)


async def test_an_account_has_at_most_as_many_keys_as_it_is_told(db: AsyncSession) -> None:
    user_id = await an_account(db)
    await made(db, user_id, at_most=2)
    await made(db, user_id, at_most=2)

    with pytest.raises(keys.TooManyKeysError) as refused:
        await made(db, user_id, at_most=2)
    with pytest.raises(keys.TooManyKeysError) as fewer:
        await made(db, user_id, at_most=1)  # as after its plan was changed

    assert (refused.value.held, refused.value.at_most) == (2, 2)
    assert (fewer.value.held, fewer.value.at_most) == (2, 1)
    assert await count(db) == 2


async def test_another_accounts_keys_dont_count_against_an_account(db: AsyncSession) -> None:
    deniz = await an_account(db)
    ece = await an_account(db, "ece@example.com")
    await made(db, ece, at_most=2)
    await made(db, ece, at_most=2)

    await made(db, deniz, at_most=1)

    assert [len(await keys.of_account(db, who)) for who in (deniz, ece)] == [1, 2]


async def test_an_accounts_keys_are_its_own_and_oldest_first(db: AsyncSession) -> None:
    """Oldest by when each was made: here the newer one is written first, and has the
    smaller id."""
    deniz = await an_account(db)
    ece = await an_account(db, "ece@example.com")
    _, newer = await keys.make(
        db, user_id=deniz, name="Script", at_most=10, key_id=new_id(), now=NOON + timedelta(days=1)
    )
    await made(db, ece, "Ece's")
    _, older = await keys.make(
        db, user_id=deniz, name="Script", at_most=10, key_id=new_id(), now=NOON
    )

    listed = await keys.of_account(db, deniz)

    assert newer.id < older.id
    assert [key.id for key in listed] == [older.id, newer.id]
    assert [key.name for key in listed] == ["Script", "Script"]  # two may share a name


async def test_revoking_deletes_the_key_and_only_the_accounts_own(db: AsyncSession) -> None:
    deniz = await an_account(db)
    ece = await an_account(db, "ece@example.com")
    key, row = await made(db, deniz)
    others, other_row = await made(db, ece)
    await db.commit()

    by_another = await keys.revoke(db, key_id=row.id, user_id=ece)
    revoked = await keys.revoke(db, key_id=row.id, user_id=deniz)
    again = await keys.revoke(db, key_id=row.id, user_id=deniz)
    await db.commit()

    assert (by_another, revoked, again) == (False, True, False)
    assert await keys.recognize(db, key, now=NOON) is None
    assert await keys.recognize(db, others, now=NOON) is not None
    assert [key.id for key in await keys.of_account(db, ece)] == [other_row.id]


async def test_the_database_refuses_two_keys_with_one_hash(db: AsyncSession) -> None:
    user_id = await an_account(db)
    key, _ = await made(db, user_id)
    await db.commit()

    db.add(
        ApiKey(
            id=new_id(),
            user_id=user_id,
            name="Copy",
            prefix=key[:12],
            key_hash=keys.hash_key(key),
            created_at=NOON,
        )
    )
    with pytest.raises(IntegrityError, match="uq_api_keys_key_hash"):
        await db.flush()


async def test_deleting_an_account_deletes_its_keys(db: AsyncSession) -> None:
    """The database enforces it, so no code path can leave a key without an account."""
    user_id = await an_account(db)
    key, _ = await made(db, user_id)
    await db.commit()

    await db.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
    await db.commit()

    assert await keys.recognize(db, key, now=NOON) is None
    assert await count(db) == 0
