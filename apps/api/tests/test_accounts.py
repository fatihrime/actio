"""Lasting accounts below the API, against a real Postgres."""

from datetime import UTC, datetime, timedelta

import pytest
from argon2 import PasswordHasher
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from actio.identity import accounts, sessions
from actio.identity.accounts import EmailTakenError, NotAGuestError, WrongPasswordError
from actio.identity.models import BrowserSession, User
from actio.identity.passwords import Passwords
from actio.identity.sessions import ACCOUNT_IDLE_LIFETIME

pytestmark = pytest.mark.anyio

NOON = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
EMAIL = "deniz@example.com"
PASSWORD = "correct horse battery staple"


async def sign_up(
    db: AsyncSession,
    passwords: Passwords,
    email: str = EMAIL,
    current: sessions.Principal | None = None,
) -> tuple[str, sessions.Principal]:
    return await accounts.sign_up(
        db, passwords, email=email, password=PASSWORD, current=current, now=NOON
    )


async def test_signing_up_creates_an_account_and_logs_in(
    db: AsyncSession, passwords: Passwords
) -> None:
    token, principal = await sign_up(db, passwords)
    await db.commit()

    assert (principal.is_guest, principal.email) == (False, EMAIL)
    assert principal.session_expires_at == NOON + ACCOUNT_IDLE_LIFETIME
    assert await sessions.authenticate(db, token, now=NOON) == principal
    user = await db.get(User, principal.user_id)
    assert user is not None
    assert user.expires_at is None  # a lasting account doesn't expire
    assert user.password_hash is not None
    assert user.password_hash.startswith("$argon2id$")
    assert PASSWORD not in user.password_hash


async def test_a_guest_who_signs_up_stays_the_same_account(
    db: AsyncSession, passwords: Passwords
) -> None:
    """The same row, so whatever the guest owns stays theirs."""
    guest_token, guest = await sessions.start_guest_session(db, now=NOON)

    token, principal = await sign_up(db, passwords, current=guest)
    await db.commit()

    assert principal.user_id == guest.user_id
    assert (principal.is_guest, principal.email) == (False, EMAIL)
    assert principal.session_id != guest.session_id
    # The guest's token is dead: what its holder may do has changed, so the session is new.
    assert await sessions.authenticate(db, guest_token, now=NOON) is None
    assert await sessions.authenticate(db, token, now=NOON) == principal
    assert await db.scalar(select(func.count()).select_from(User)) == 1


async def test_one_email_is_one_account_however_it_is_typed(
    db: AsyncSession, passwords: Passwords
) -> None:
    await sign_up(db, passwords, email="Deniz@Example.com")

    with pytest.raises(EmailTakenError):
        await sign_up(db, passwords, email="  deniz@EXAMPLE.com ")

    assert await db.scalar(select(func.count()).select_from(User)) == 1
    assert await db.scalar(select(User.email)) == "deniz@example.com"


async def test_a_guest_refused_for_a_taken_email_is_still_a_guest(
    db: AsyncSession, passwords: Passwords
) -> None:
    await sign_up(db, passwords)
    guest_token, guest = await sessions.start_guest_session(db, now=NOON)
    await db.commit()

    with pytest.raises(EmailTakenError):
        await sign_up(db, passwords, current=guest)
    await db.commit()

    assert await sessions.authenticate(db, guest_token, now=NOON) == guest
    refreshed = await db.get(User, guest.user_id, populate_existing=True)
    assert refreshed is not None
    assert (refreshed.is_guest, refreshed.email, refreshed.password_hash) == (True, None, None)


async def test_an_account_cannot_sign_up_again(db: AsyncSession, passwords: Passwords) -> None:
    _, principal = await sign_up(db, passwords)

    with pytest.raises(NotAGuestError):
        await sign_up(db, passwords, email="other@example.com", current=principal)


async def test_logging_in_starts_a_new_session(db: AsyncSession, passwords: Passwords) -> None:
    first_token, first = await sign_up(db, passwords)

    started = await accounts.log_in(
        db, passwords, email="DENIZ@example.com", password=PASSWORD, now=NOON
    )

    assert started is not None
    token, principal = started
    assert principal.user_id == first.user_id
    assert (token, principal.session_id) != (first_token, first.session_id)
    # Two browsers, both logged in.
    assert await sessions.authenticate(db, first_token, now=NOON) == first
    assert await sessions.authenticate(db, token, now=NOON) == principal


@pytest.mark.parametrize(
    ("email", "password"),
    [
        (EMAIL, "wrong horse battery staple"),
        (EMAIL, ""),
        ("nobody@example.com", PASSWORD),
        ("not an address", PASSWORD),
    ],
)
async def test_a_wrong_password_and_an_unknown_email_look_the_same(
    db: AsyncSession, passwords: Passwords, email: str, password: str
) -> None:
    await sign_up(db, passwords)

    assert await accounts.log_in(db, passwords, email=email, password=password, now=NOON) is None
    assert await db.scalar(select(func.count()).select_from(BrowserSession)) == 1


async def test_a_guest_cannot_be_logged_in_to(db: AsyncSession, passwords: Passwords) -> None:
    """A guest has no email and no password, so no credentials can match it."""
    await sessions.start_guest_session(db, now=NOON)

    assert await accounts.log_in(db, passwords, email="", password="", now=NOON) is None


async def test_logging_in_upgrades_a_hash_made_with_old_parameters(
    db: AsyncSession, passwords: Passwords
) -> None:
    _, principal = await sign_up(db, passwords)
    user = await db.get(User, principal.user_id)
    assert user is not None
    old_hash = user.password_hash
    stronger = Passwords(PasswordHasher(time_cost=2, memory_cost=16, parallelism=1))

    assert await accounts.log_in(db, stronger, email=EMAIL, password=PASSWORD, now=NOON)

    assert user.password_hash != old_hash
    assert not stronger.needs_rehash(user.password_hash or "")
    assert await stronger.verify(user.password_hash, PASSWORD)


async def test_changing_the_password_logs_every_other_browser_out(
    db: AsyncSession, passwords: Passwords
) -> None:
    here_token, here = await sign_up(db, passwords)
    elsewhere = await accounts.log_in(db, passwords, email=EMAIL, password=PASSWORD, now=NOON)
    assert elsewhere is not None

    new_token, principal = await accounts.change_password(
        db,
        passwords,
        user_id=here.user_id,
        current_password=PASSWORD,
        new_password="a brand new long password",
        now=NOON,
    )

    assert await sessions.authenticate(db, here_token, now=NOON) is None
    assert await sessions.authenticate(db, elsewhere[0], now=NOON) is None
    assert await sessions.authenticate(db, new_token, now=NOON) == principal
    assert await accounts.log_in(db, passwords, email=EMAIL, password=PASSWORD, now=NOON) is None
    assert await accounts.log_in(
        db, passwords, email=EMAIL, password="a brand new long password", now=NOON
    )


async def test_the_password_changes_only_for_who_knows_the_current_one(
    db: AsyncSession, passwords: Passwords
) -> None:
    token, principal = await sign_up(db, passwords)

    with pytest.raises(WrongPasswordError):
        await accounts.change_password(
            db,
            passwords,
            user_id=principal.user_id,
            current_password="a guess",
            new_password="a brand new long password",
            now=NOON,
        )

    assert await sessions.authenticate(db, token, now=NOON) == principal
    assert await accounts.log_in(db, passwords, email=EMAIL, password=PASSWORD, now=NOON)


async def test_an_accounts_session_lives_while_it_is_used(
    db: AsyncSession, passwords: Passwords
) -> None:
    """30 days without a request ends it; each use starts the 30 days again."""
    token, _ = await sign_up(db, passwords)

    day_20 = NOON + timedelta(days=20)
    seen = await sessions.authenticate(db, token, now=day_20)
    assert seen is not None
    assert seen.session_expires_at == day_20 + ACCOUNT_IDLE_LIFETIME

    day_45 = NOON + timedelta(days=45)  # 25 days after the last use: still alive
    assert await sessions.authenticate(db, token, now=day_45) is not None
    day_76 = day_45 + timedelta(days=30, seconds=1)  # more than 30 days idle
    assert await sessions.authenticate(db, token, now=day_76) is None


async def test_a_guests_session_is_not_renewed_by_use(db: AsyncSession) -> None:
    token, guest = await sessions.start_guest_session(db, now=NOON)

    seen = await sessions.authenticate(db, token, now=NOON + timedelta(hours=23))

    assert seen is not None
    assert seen.session_expires_at == guest.session_expires_at == NOON + timedelta(hours=24)


async def test_logging_out_everywhere_ends_every_session_and_keeps_the_account(
    db: AsyncSession, passwords: Passwords
) -> None:
    token, principal = await sign_up(db, passwords)
    other = await accounts.log_in(db, passwords, email=EMAIL, password=PASSWORD, now=NOON)
    assert other is not None

    await sessions.end_all_sessions(db, principal.user_id)

    assert await sessions.authenticate(db, token, now=NOON) is None
    assert await sessions.authenticate(db, other[0], now=NOON) is None
    assert await db.get(User, principal.user_id) is not None
    assert await accounts.log_in(db, passwords, email=EMAIL, password=PASSWORD, now=NOON)


@pytest.mark.parametrize(
    "broken_row",
    [
        # A guest with an email address.
        "INSERT INTO users (id, is_guest, created_at, expires_at, email)"
        " VALUES (gen_random_uuid(), true, now(), now(), 'a@example.com')",
        # A guest that never expires.
        "INSERT INTO users (id, is_guest, created_at) VALUES (gen_random_uuid(), true, now())",
        # An account without a password.
        "INSERT INTO users (id, is_guest, created_at, email)"
        " VALUES (gen_random_uuid(), false, now(), 'a@example.com')",
        # An account that expires.
        "INSERT INTO users (id, is_guest, created_at, expires_at, email, password_hash)"
        " VALUES (gen_random_uuid(), false, now(), now(), 'a@example.com', 'x')",
    ],
)
async def test_the_database_refuses_a_row_that_is_half_guest_half_account(
    db: AsyncSession, broken_row: str
) -> None:
    """Whatever code writes the table, a row is wholly one kind or the other."""
    with pytest.raises(IntegrityError, match="ck_users_guest_or_account"):
        await db.execute(text(broken_row))
