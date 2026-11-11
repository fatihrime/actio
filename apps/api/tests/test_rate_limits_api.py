"""Rate limits over HTTP: new guests, sign-ups and password attempts.

Each test tightens the limits it is about, so a handful of requests reaches them. The
app counts in a real Valkey, under a key prefix of the test's own.
"""

import logging
from datetime import UTC, datetime
from typing import Any

import argon2.profiles
import pytest
from argon2 import PasswordHasher
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from actio.api.identity import _delete_expired
from actio.api.rate_limits import client_address
from actio.core.rate_limit import Rate
from actio.core.resources import open_resources
from actio.core.settings import Settings
from actio.identity.models import BrowserSession, User
from actio.identity.passwords import Passwords

pytestmark = pytest.mark.anyio

PROBLEM = "application/problem+json"
DENIZ = {"email": "deniz@example.com", "password": "correct horse battery staple"}
WRONG = {"email": "deniz@example.com", "password": "wrong horse battery staple"}
HOME, OFFICE, CAFE, LIBRARY = "203.0.113.7", "198.51.100.20", "192.0.2.33", "192.0.2.99"


def limited(settings: Settings, **rates: str) -> Settings:
    """These settings with some limits changed: `guests_per_address="2/hour"`."""
    changes = {f"limit_{name}": Rate.parse(rate) for name, rate in rates.items()}
    return settings.model_copy(update=changes)


# --- Whose attempts are counted together ----------------------------------------------


@pytest.mark.parametrize(
    ("host", "counted_as"),
    [
        ("203.0.113.7", "203.0.113.7"),
        ("2001:db8:1:2:aaaa:bbbb:cccc:dddd", "2001:db8:1:2::/64"),  # the whole network
        ("2001:db8:1:2::1", "2001:db8:1:2::/64"),
        ("2001:db8:1:3::1", "2001:db8:1:3::/64"),
        ("::ffff:203.0.113.7", "203.0.113.7"),  # an IPv4 client, written the IPv6 way
        ("fe80::1%en0", "fe80::/64"),
        ("::1", "::/64"),
        ("not an address", "unknown"),
        ("", "unknown"),
    ],
)
def test_attempts_are_counted_per_network_address(host: str, counted_as: str) -> None:
    request = Request({"type": "http", "headers": [], "client": (host, 50_000)})

    assert client_address(request) == counted_as


def test_a_request_with_no_address_at_all_shares_one_bucket() -> None:
    assert client_address(Request({"type": "http", "headers": [], "client": None})) == "unknown"


# --- New guests -----------------------------------------------------------------------


async def test_new_guests_from_one_address_are_limited(
    start_app: Any, settings: Settings, migrated: str, clock: Any
) -> None:
    assert migrated
    two_an_hour = limited(settings, guests_per_address="2/hour")
    first, second, third = [await start_app(two_an_hour, address=HOME) for _ in range(3)]
    assert (await first.post("/api/session/guest")).status_code == 201
    assert (await second.post("/api/session/guest")).status_code == 201

    refused = await third.post("/api/session/guest")

    assert refused.status_code == 429
    assert refused.headers["content-type"] == PROBLEM
    assert refused.headers["retry-after"] == "1800"
    assert refused.json() == {
        "type": "about:blank",
        "title": "Too Many Requests",
        "status": 429,
        "detail": "Too many new guests from your network. Try again in 30 minutes.",
        "retry_after": 1800,
    }
    assert "set-cookie" not in refused.headers
    assert (await third.get("/api/me")).status_code == 401

    clock.advance(minutes=29, seconds=59)
    assert (await third.post("/api/session/guest")).headers["retry-after"] == "1"
    clock.advance(seconds=1)
    assert (await third.post("/api/session/guest")).status_code == 201


@pytest.mark.parametrize(
    ("rate", "later", "wait", "in_words"),
    [
        ("1/day", {}, 86_400, "24 hours"),
        ("1/day", {"hours": 21, "minutes": 30}, 9_000, "3 hours"),
        ("1/hour", {"minutes": 1}, 3_540, "59 minutes"),
        ("1/hour", {"minutes": 58}, 120, "2 minutes"),
        ("1/hour", {"minutes": 58, "seconds": 1}, 119, "119 seconds"),
        ("1/hour", {"minutes": 59, "seconds": 59}, 1, "1 second"),
        ("1/hour", {"minutes": 59, "seconds": 59.5}, 1, "1 second"),  # never "0 seconds"
    ],
)
async def test_a_refusal_says_how_long_to_wait_in_words_and_in_seconds(
    start_app: Any,
    settings: Settings,
    migrated: str,
    clock: Any,
    rate: str,
    later: dict[str, float],
    wait: int,
    in_words: str,
) -> None:
    assert migrated
    tight = limited(settings, guests_per_address=rate)
    await (await start_app(tight)).post("/api/session/guest")
    clock.advance(**later)

    refused = await (await start_app(tight)).post("/api/session/guest")

    assert refused.status_code == 429
    assert refused.headers["retry-after"] == str(wait)  # rounded up: waiting that long is enough
    assert refused.json()["retry_after"] == wait
    assert refused.json()["detail"].endswith(f"Try again in {in_words}.")


async def test_a_browser_that_has_a_session_asks_again_for_free(
    start_app: Any, settings: Settings, migrated: str
) -> None:
    assert migrated
    browser = await start_app(limited(settings, guests_per_address="1/hour"))
    guest = (await browser.post("/api/session/guest")).json()

    again = [await browser.post("/api/session/guest") for _ in range(5)]

    assert [response.status_code for response in again] == [200] * 5
    assert all(response.json() == guest for response in again)


async def test_each_address_has_its_own_allowance(
    start_app: Any, settings: Settings, migrated: str
) -> None:
    assert migrated
    one_an_hour = limited(settings, guests_per_address="1/hour")
    at_home, also_at_home = [await start_app(one_an_hour, address=HOME) for _ in range(2)]
    at_the_office = await start_app(one_an_hour, address=OFFICE)
    assert (await at_home.post("/api/session/guest")).status_code == 201

    assert (await also_at_home.post("/api/session/guest")).status_code == 429
    assert (await at_the_office.post("/api/session/guest")).status_code == 201


async def test_one_ipv6_network_is_one_address(
    start_app: Any, settings: Settings, migrated: str
) -> None:
    """A home or a machine is usually given a whole /64: 18 billion billion addresses.
    Counting each one apart would be no limit at all."""
    assert migrated
    one_an_hour = limited(settings, guests_per_address="1/hour")
    first = await start_app(one_an_hour, address="2001:db8:1:2::1")
    same_network = await start_app(one_an_hour, address="2001:db8:1:2:ffff:ffff:ffff:ffff")
    next_network = await start_app(one_an_hour, address="2001:db8:1:3::1")
    assert (await first.post("/api/session/guest")).status_code == 201

    assert (await same_network.post("/api/session/guest")).status_code == 429
    assert (await next_network.post("/api/session/guest")).status_code == 201


# --- Sign-ups -------------------------------------------------------------------------


async def test_sign_ups_from_one_address_are_limited(
    start_app: Any, settings: Settings, migrated: str, clock: Any
) -> None:
    assert migrated
    two_an_hour = limited(settings, signups_per_address="2/hour")
    browsers = [await start_app(two_an_hour, address=HOME) for _ in range(3)]
    for number, browser in enumerate(browsers[:2]):
        signup = DENIZ | {"email": f"person{number}@example.com"}
        assert (await browser.post("/api/account", json=signup)).status_code == 201

    refused = await browsers[2].post("/api/account", json=DENIZ)

    assert refused.status_code == 429
    assert refused.headers["retry-after"] == "1800"
    assert refused.json()["detail"] == (
        "Too many sign-up attempts from your network. Try again in 30 minutes."
    )
    # Nothing was created: the address is still free once there is room again.
    clock.advance(minutes=30)
    assert (await browsers[2].post("/api/account", json=DENIZ)).status_code == 201


async def test_a_sign_up_that_was_never_tried_does_not_count(
    start_app: Any, settings: Settings, migrated: str
) -> None:
    """A form that is refused for its shape costs the server nothing, so it isn't counted:
    a typing mistake shouldn't use up someone's attempts."""
    assert migrated
    browser = await start_app(limited(settings, signups_per_address="1/hour"))
    for _ in range(5):
        too_short = await browser.post("/api/account", json=DENIZ | {"password": "short"})
        assert too_short.status_code == 422

    assert (await browser.post("/api/account", json=DENIZ)).status_code == 201


async def test_a_refused_sign_up_says_nothing_about_the_address(
    start_app: Any, settings: Settings, migrated: str
) -> None:
    """Signing up tells whether an address is registered (409). The limit is what keeps
    that from being asked about a whole list of addresses."""
    assert migrated
    one_an_hour = limited(settings, signups_per_address="1/hour")
    owner, stranger = (
        await start_app(one_an_hour, address=HOME),
        await start_app(one_an_hour, address=OFFICE),
    )
    await owner.post("/api/account", json=DENIZ)
    first_question = await stranger.post("/api/account", json=DENIZ)

    registered = await stranger.post("/api/account", json=DENIZ)
    unregistered = await stranger.post("/api/account", json=DENIZ | {"email": "nobody@example.com"})

    assert first_question.status_code == 409
    assert registered.status_code == unregistered.status_code == 429
    assert registered.json() == unregistered.json()


# --- Password attempts ----------------------------------------------------------------


async def test_password_attempts_for_one_account_are_limited_across_addresses(
    start_app: Any, settings: Settings, migrated: str, clock: Any
) -> None:
    assert migrated
    three_an_hour = limited(settings, logins_per_email="3/hour")
    owner = await start_app(three_an_hour, address=HOME)
    await owner.post("/api/account", json=DENIZ)
    await owner.delete("/api/session")
    guessers = [await start_app(three_an_hour, address=a) for a in (OFFICE, CAFE, LIBRARY)]
    for guesser in guessers:
        assert (await guesser.post("/api/session", json=WRONG)).status_code == 401

    refused = await guessers[0].post("/api/session", json=WRONG)

    assert refused.status_code == 429
    assert refused.headers["retry-after"] == "1200"
    assert refused.json() == {
        "type": "about:blank",
        "title": "Too Many Requests",
        "status": 429,
        "detail": "Too many login attempts. Try again in 20 minutes.",
        "retry_after": 1200,
    }
    # The cost of the limit: for those 20 minutes the right password is refused as
    # well, from the owner's own address. Otherwise a guess that is right would get in.
    assert (await owner.post("/api/session", json=DENIZ)).status_code == 429
    clock.advance(minutes=20)
    assert (await owner.post("/api/session", json=DENIZ)).status_code == 200


async def test_every_login_counts_whether_it_was_right_or_wrong(
    start_app: Any, settings: Settings, migrated: str
) -> None:
    """Counting only failures would need a second step after the password check, and
    attempts arriving together could all pass before any of them was counted."""
    assert migrated
    browser = await start_app(limited(settings, logins_per_email="2/hour"))
    await browser.post("/api/account", json=DENIZ)

    answers = [(await browser.post("/api/session", json=DENIZ)).status_code for _ in range(3)]

    assert answers == [200, 200, 429]


class CountingPasswords(Passwords):
    """The cheap hasher, counting how much work it was asked to do."""

    def __init__(self) -> None:
        super().__init__(PasswordHasher.from_parameters(argon2.profiles.CHEAPEST))
        self.checked = 0

    async def verify(self, password_hash: str | None, password: str) -> bool:
        self.checked += 1
        return await super().verify(password_hash, password)


async def test_a_refused_attempt_never_reaches_the_password_check(
    start_app: Any, settings: Settings, migrated: str
) -> None:
    """Checking a password is the expensive part: 64 MiB and tens of milliseconds in
    production. A refusal must cost less than what it refuses."""
    assert migrated
    passwords = CountingPasswords()
    browser = await start_app(limited(settings, logins_per_email="2/hour"), passwords=passwords)

    answers = [(await browser.post("/api/session", json=WRONG)).status_code for _ in range(10)]

    assert answers == [401, 401] + [429] * 8
    assert passwords.checked == 2


@pytest.mark.parametrize(
    "typed", ["Deniz@Example.COM", "  deniz@example.com ", "DENIZ@example.com"]
)
async def test_one_address_typed_differently_is_still_one_account(
    start_app: Any, settings: Settings, migrated: str, typed: str
) -> None:
    assert migrated
    browser = await start_app(limited(settings, logins_per_email="1/hour"))
    assert (await browser.post("/api/session", json=WRONG)).status_code == 401

    assert (await browser.post("/api/session", json=WRONG | {"email": typed})).status_code == 429


async def test_a_limit_is_reached_the_same_way_for_an_address_nobody_has(
    start_app: Any, settings: Settings, migrated: str
) -> None:
    """Otherwise the 429 would tell which addresses have an account."""
    assert migrated
    two_an_hour = limited(settings, logins_per_email="2/hour")
    browser = await start_app(two_an_hour)
    await browser.post("/api/account", json=DENIZ)
    await browser.delete("/api/session")
    nobody = WRONG | {"email": "nobody@example.com"}

    for_an_account = [await browser.post("/api/session", json=WRONG) for _ in range(3)]
    for_nobody = [await browser.post("/api/session", json=nobody) for _ in range(3)]

    assert [r.status_code for r in for_an_account] == [401, 401, 429]
    assert [r.status_code for r in for_nobody] == [401, 401, 429]
    assert for_an_account[2].json() == for_nobody[2].json()
    assert for_an_account[2].headers["retry-after"] == for_nobody[2].headers["retry-after"]


async def test_logins_from_one_address_are_limited_across_accounts(
    start_app: Any, settings: Settings, migrated: str
) -> None:
    """Trying one leaked password on many accounts never reaches any account's own limit."""
    assert migrated
    three_an_hour = limited(settings, logins_per_address="3/hour")
    cafe, library = (
        await start_app(three_an_hour, address=CAFE),
        await start_app(three_an_hour, address=LIBRARY),
    )
    for number in range(3):
        attempt = WRONG | {"email": f"person{number}@example.com"}
        assert (await cafe.post("/api/session", json=attempt)).status_code == 401

    someone_else = WRONG | {"email": "person3@example.com"}
    assert (await cafe.post("/api/session", json=someone_else)).status_code == 429
    assert (await library.post("/api/session", json=someone_else)).status_code == 401


async def test_an_attempt_refused_for_its_address_does_not_count_against_the_account(
    start_app: Any, settings: Settings, migrated: str
) -> None:
    """Someone already refused can't use those refusals to shut an account's owner out."""
    assert migrated
    tight = limited(settings, logins_per_address="1/hour", logins_per_email="2/hour")
    cafe, library, office = [await start_app(tight, address=a) for a in (CAFE, LIBRARY, OFFICE)]
    assert (await cafe.post("/api/session", json=WRONG)).status_code == 401  # the account's 1st

    for _ in range(5):
        assert (await cafe.post("/api/session", json=WRONG)).status_code == 429

    assert (await library.post("/api/session", json=WRONG)).status_code == 401  # only its 2nd
    assert (await office.post("/api/session", json=WRONG)).status_code == 429


async def test_changing_the_password_counts_as_an_attempt_at_it(
    start_app: Any, settings: Settings, migrated: str
) -> None:
    """Whoever holds a stolen session can't use the password form to find the password."""
    assert migrated
    three_an_hour = limited(settings, logins_per_email="3/hour")
    thief, owner = (
        await start_app(three_an_hour, address=CAFE),
        await start_app(three_an_hour, address=HOME),
    )
    await thief.post("/api/account", json=DENIZ)  # signing up is not a password attempt
    guess = {"current_password": "a guess at the password", "new_password": "something else long"}
    for _ in range(3):
        assert (await thief.put("/api/account/password", json=guess)).status_code == 403

    refused = await thief.put("/api/account/password", json=guess)

    assert refused.status_code == 429
    assert refused.json()["detail"] == "Too many password attempts. Try again in 20 minutes."
    # One allowance for one password, wherever it is tried.
    assert (await owner.post("/api/session", json=DENIZ)).status_code == 429
    assert (await thief.get("/api/me")).status_code == 200  # the session itself still works


# --- Without Valkey -------------------------------------------------------------------


async def test_without_valkey_nothing_goes_uncounted(
    start_app: Any, settings: Settings, migrated: str, caplog: pytest.LogCaptureFixture
) -> None:
    """If attempts can't be counted they aren't allowed: an outage must not be the
    moment every limit is off. What needs no counting keeps working."""
    assert migrated
    browser = await start_app(settings)
    await browser.post("/api/account", json=DENIZ)
    cut_off = settings.model_copy(update={"valkey_url": SecretStr("valkey://127.0.0.1:1/0")})
    visitor, member = await start_app(cut_off), await start_app(cut_off)
    member.cookies.set("actio_session", browser.cookies["actio_session"])

    with caplog.at_level(logging.WARNING, logger="actio.api.rate_limits"):
        answers = [
            await visitor.post("/api/session/guest"),
            await visitor.post("/api/account", json=DENIZ | {"email": "new@example.com"}),
            await visitor.post("/api/session", json=DENIZ),
        ]

    for answer in answers:
        assert answer.status_code == 503
        assert answer.headers["content-type"] == PROBLEM
        assert answer.json()["detail"] == "This can't be done right now. Try again in a moment."
        assert "set-cookie" not in answer.headers
    assert "Valkey is unreachable (ConnectionError)" in caplog.text
    # Sessions live in Postgres: who you are, and logging out, need no Valkey.
    assert (await member.get("/api/me")).json()["email"] == DENIZ["email"]
    assert (await member.delete("/api/session")).status_code == 204


# --- Expired guests -------------------------------------------------------------------


async def count(db: AsyncSession, model: type) -> int:
    return await db.scalar(select(func.count()).select_from(model)) or 0


async def test_a_new_guest_clears_out_the_expired_ones(
    start_app: Any, settings: Settings, db: AsyncSession, clock: Any
) -> None:
    yesterday = [await start_app(settings) for _ in range(3)]
    for browser in yesterday:
        await browser.post("/api/session/guest")
    member = await start_app(settings)
    await member.post("/api/account", json=DENIZ)
    assert (await count(db, User), await count(db, BrowserSession)) == (4, 4)

    clock.advance(hours=24)
    newcomer = await start_app(settings)
    response = await newcomer.post("/api/session/guest")

    assert response.status_code == 201
    assert (await count(db, User), await count(db, BrowserSession)) == (2, 2)
    assert (await newcomer.get("/api/me")).json()["is_guest"] is True
    assert (await member.get("/api/me")).json()["email"] == DENIZ["email"]


async def test_a_sweep_that_cannot_reach_the_database_is_logged_not_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """It runs after the guest already has an answer, and what it couldn't delete is
    still expired next time."""
    nowhere = Settings(
        database_url=SecretStr("postgresql://actio:unused@127.0.0.1:1/actio"),
        valkey_url=SecretStr("valkey://127.0.0.1:1/0"),
    )
    async with open_resources(nowhere) as resources:
        with caplog.at_level(logging.WARNING, logger="actio.api.identity"):
            await _delete_expired(resources, datetime(2026, 11, 10, 12, 0, tzinfo=UTC))

    assert "deleting expired guests failed (OperationalError)" in caplog.text
