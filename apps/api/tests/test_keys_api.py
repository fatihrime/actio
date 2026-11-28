"""API keys over HTTP: making, listing and revoking them in a session, and a request that
carries one recognized as its account, as long as the account's plan allows keys.

No key is written in this file: each is made when the test runs, through the routes,
or drawn as a key is, for one that no row has."""

import asyncio
import re
import secrets
import string
import uuid
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from actio.core.database import create_engine, create_session_factory
from actio.core.ids import new_id
from actio.core.settings import Settings
from actio.identity import accounts, keys
from actio.identity.models import ApiKey
from actio.usage.plans import Plans

pytestmark = pytest.mark.anyio

PROBLEM = "application/problem+json"
PATIENCE_S = 5.0
DENIZ = {"email": "deniz@example.com", "password": "a long password"}
ECE = {"email": "ece@example.com", "password": "another long password"}
DEV = {"dev": {"title": "Developer", "api_keys": 2}}
"""A plan beside the tests' two, `guest` and `member`, which allow no keys: two at once."""
GUESTS_WITH_KEYS = {"title": "Guest", "api_keys": 2}
"""The guests' plan, in the place of the tests' own, as a file that gives guests keys."""

FORM = re.compile(r"act_[0-9A-Za-z]{8}_[0-9A-Za-z]{43}")
"""A key's form, as ADR 0033 states it."""

UNKNOWN = "This API key isn't known: it may be mistyped, or revoked."
NO_KEY = (
    "This needs an API key, sent as Authorization: Bearer followed by the key, or in"
    " x-api-key. An account makes one with POST /api/keys."
)
TWO_KEYS = (
    "This request carries two different API keys, one in Authorization and one in"
    " x-api-key. Send one of them."
)


def never_made() -> str:
    """A text of a key's form that no key has: drawn now, as a key is."""
    drawn = string.ascii_letters + string.digits

    def run(length: int) -> str:
        return "".join(secrets.choice(drawn) for _ in range(length))

    return f"act_{run(8)}_{run(43)}"


class Keys:
    """Browsers and programs on apps that share this test's database, its clock and its
    plans: an account signs up in a browser, and a program carries a key, with no
    cookie."""

    def __init__(self, start_app: Any, settings: Settings, plans: Plans, db: AsyncSession) -> None:
        self.start_app = start_app
        self.settings = settings
        self.plans = plans
        self.db = db
        self._program: httpx.AsyncClient | None = None

    async def browser(self) -> httpx.AsyncClient:
        client: httpx.AsyncClient = await self.start_app(self.settings, plans=self.plans)
        return client

    async def account(
        self, who: dict[str, str] = DENIZ, plan: str | None = "dev"
    ) -> httpx.AsyncClient:
        """A browser logged in to a new account, on `plan` of its own, unless `None`."""
        browser = await self.browser()
        signed_up = await browser.post("/api/account", json=who)
        assert signed_up.status_code == 201, signed_up.text
        if plan is not None:
            await self.put_on(who, plan)
        return browser

    async def put_on(self, who: dict[str, str], plan: str | None) -> None:
        """Put the account on `plan` of its own, as `actio plan` does, or with `None`
        take its own plan away."""
        await accounts.put_on_plan(self.db, email=who["email"], plan=plan)
        await self.db.commit()

    async def made(self, browser: httpx.AsyncClient, name: str = "Script") -> dict[str, Any]:
        response = await browser.post("/api/keys", json={"name": name})
        assert response.status_code == 201, response.text
        made: dict[str, Any] = response.json()
        return made

    async def listed(self, browser: httpx.AsyncClient) -> dict[str, Any]:
        response = await browser.get("/api/keys")
        assert response.status_code == 200, response.text
        body: dict[str, Any] = response.json()
        return body

    async def use(self, key: str) -> httpx.Response:
        """`GET /api/key` from a program that has no cookie, with `key` as a bearer."""
        return await self.sent({"Authorization": f"Bearer {key}"})

    async def sent(self, headers: dict[str, str]) -> httpx.Response:
        """`GET /api/key` from a program that has no cookie, with these headers."""
        if self._program is None:
            self._program = await self.browser()
        assert not self._program.cookies
        return await self._program.get("/api/key", headers=headers)

    async def count(self) -> int:
        found = await self.db.scalar(select(func.count()).select_from(ApiKey))
        await self.db.commit()
        return found or 0


@pytest.fixture
def lot(
    start_app: Any, settings: Settings, migrated: str, plans_with: Any, db: AsyncSession
) -> Keys:
    """An app whose plan `dev` allows two keys, on this test's migrated database."""
    assert migrated == settings.database_url.get_secret_value()
    return Keys(start_app, settings, plans_with(plans=DEV), db)


# --- Making, listing and revoking -----------------------------------------------------


async def test_a_key_is_made_and_shown_whole_this_once(
    lot: Keys, server_ids: Any, clock: Any
) -> None:
    browser = await lot.account()

    response = await browser.post("/api/keys", json={"name": "  Nightly export  "})
    listed = await lot.listed(browser)

    assert response.status_code == 201
    made = response.json()
    key = made.pop("key")
    assert FORM.fullmatch(key), key[:12]
    assert (
        made
        == {
            "id": str(server_ids.number(1)),
            "name": "Nightly export",  # without the spaces around it
            "prefix": key[:12],
            "created_at": "2026-11-10T12:00:00Z",
            "last_used_at": None,
        }
    )
    assert clock.now.isoformat() == "2026-11-10T12:00:00+00:00"
    assert listed == {"keys": [made], "at_most": 2}
    assert key[12:] not in str(listed)


async def test_keys_are_listed_oldest_first_and_two_may_share_a_name(lot: Keys, clock: Any) -> None:
    browser = await lot.account()
    first = await lot.made(browser)
    clock.advance(minutes=5)
    second = await lot.made(browser)

    listed = await lot.listed(browser)

    assert [key["id"] for key in listed["keys"]] == [first["id"], second["id"]]
    assert [key["name"] for key in listed["keys"]] == ["Script", "Script"]
    assert first["key"] != second["key"]


@pytest.mark.parametrize(
    "name", ["", "   ", "x" * 65, 7], ids=["empty", "spaces", "too long", "a number"]
)
async def test_a_key_needs_a_name_of_1_to_64_characters(lot: Keys, name: object) -> None:
    browser = await lot.account()

    refused = await browser.post("/api/keys", json={"name": name})
    longest = await browser.post("/api/keys", json={"name": "x" * 64})

    assert refused.status_code == 422
    assert [error["where"] for error in refused.json()["errors"]] == ["body.name"]
    assert longest.status_code == 201
    assert await lot.count() == 1


async def test_a_revoked_key_is_answered_as_one_that_never_was(lot: Keys) -> None:
    browser = await lot.account()
    made = await lot.made(browser)
    worked = await lot.use(made["key"])

    revoked = await browser.delete(f"/api/keys/{made['id']}")
    after = await lot.use(made["key"])
    never = await lot.use(never_made())
    again = await browser.delete(f"/api/keys/{made['id']}")

    assert worked.status_code == 200
    assert revoked.status_code == 204
    assert (after.status_code, after.json()["detail"]) == (401, UNKNOWN)
    assert after.json() == never.json()
    assert (again.status_code, again.json()["detail"]) == (
        404,
        "You have no API key with this id.",
    )
    assert (await lot.listed(browser))["keys"] == []


async def test_another_accounts_key_is_not_revoked_and_still_works(lot: Keys) -> None:
    deniz = await lot.account(DENIZ)
    ece = await lot.account(ECE)
    made = await lot.made(deniz)

    refused = await ece.delete(f"/api/keys/{made['id']}")
    still = await lot.use(made["key"])

    assert (refused.status_code, refused.headers["content-type"]) == (404, PROBLEM)
    assert still.status_code == 200
    assert (await lot.listed(ece))["keys"] == []
    assert [key["id"] for key in (await lot.listed(deniz))["keys"]] == [made["id"]]


# --- Recognizing a key ----------------------------------------------------------------


async def test_a_key_says_what_it_is_whose_and_on_which_plan(lot: Keys, clock: Any) -> None:
    browser = await lot.account()
    me = (await browser.get("/api/me")).json()
    made = await lot.made(browser, "Nightly export")
    clock.advance(hours=1)

    response = await lot.use(made["key"])

    assert response.status_code == 200
    assert response.json() == {
        "id": made["id"],
        "name": "Nightly export",
        "prefix": made["key"][:12],
        "created_at": "2026-11-10T12:00:00Z",
        "last_used_at": "2026-11-10T13:00:00Z",  # this very use
        "account": {"id": me["id"], "email": "deniz@example.com"},
        "plan": {"id": "dev", "title": "Developer"},
    }


SENT: dict[str, Callable[[str], dict[str, str]]] = {
    "as a bearer": lambda key: {"Authorization": f"Bearer {key}"},
    "as a bearer, in lower case": lambda key: {"authorization": f"bearer {key}"},
    "as a bearer, in capitals": lambda key: {"Authorization": f"BEARER {key}"},
    "as a bearer, with spaces around it": lambda key: {"Authorization": f"Bearer   {key}  "},
    "in x-api-key": lambda key: {"x-api-key": key},
    "in x-api-key, with spaces around it": lambda key: {"x-api-key": f"  {key} "},
    "in both": lambda key: {"Authorization": f"Bearer {key}", "x-api-key": key},
    "in both, with spaces around one": lambda key: {
        "Authorization": f"Bearer {key}",
        "x-api-key": f" {key}  ",
    },
    "in x-api-key, beside another scheme": lambda key: {
        "Authorization": "Basic not-a-key",
        "x-api-key": key,
    },
}


@pytest.mark.parametrize("way", SENT)
async def test_a_key_is_recognized_however_it_is_sent(lot: Keys, way: str) -> None:
    browser = await lot.account()
    made = await lot.made(browser)

    response = await lot.sent(SENT[way](made["key"]))

    assert response.status_code == 200, response.text
    assert response.json()["id"] == made["id"]


async def test_two_different_keys_in_the_two_headers_are_refused(lot: Keys) -> None:
    """Though one of them works, and whichever it is."""
    browser = await lot.account()
    made = await lot.made(browser)

    refused = [
        await lot.sent({"Authorization": f"Bearer {made['key']}", "x-api-key": other})
        for other in (never_made(), "something else")
    ] + [await lot.sent({"Authorization": f"Bearer {never_made()}", "x-api-key": made["key"]})]

    for response in refused:
        assert response.status_code == 401
        assert response.json()["detail"] == TWO_KEYS


def not_of_a_keys_form() -> list[str]:
    """Texts that aren't a key's form, made from one that is."""
    key = never_made()
    return [
        "act_",
        "Bearer",
        key[:-1],  # a character short
        key + "x",  # one too many
        "Act_" + key[4:],
        key[:20] + "-" + key[21:],  # a sign a key never has
        key[:20] + "é" + key[21:],  # a letter that isn't ASCII
        key[:12] + "-" + key[13:],
    ]


async def test_a_text_that_isnt_a_keys_form_is_refused_without_the_database(
    client: httpx.AsyncClient,
) -> None:
    """The app of `client` has no database: a query would fail the request. Each text is
    sent as its UTF-8 bytes: a header's text is ASCII."""
    for sent in not_of_a_keys_form():
        for headers in (
            {b"x-api-key": sent.encode()},
            {b"Authorization": f"Bearer {sent}".encode()},
        ):
            response = await client.get("/api/key", headers=headers)

            assert (response.status_code, response.json()["detail"]) == (401, UNKNOWN), sent[:12]
            assert response.headers["www-authenticate"] == "Bearer"


async def test_a_sessions_cookie_is_no_key_and_a_key_is_no_session(lot: Keys) -> None:
    browser = await lot.account()
    made = await lot.made(browser)
    program = await lot.browser()
    as_bearer = {"Authorization": f"Bearer {made['key']}"}

    with_a_cookie = await browser.get("/api/key")
    with_a_key = [
        await program.post("/api/keys", json={"name": "Another"}, headers=as_bearer),
        await program.get("/api/keys", headers=as_bearer),
        await program.delete(f"/api/keys/{made['id']}", headers=as_bearer),
        await program.get("/api/keys", headers={"x-api-key": made["key"]}),
    ]

    assert (with_a_cookie.status_code, with_a_cookie.json()["detail"]) == (401, NO_KEY)
    for response in with_a_key:
        assert response.status_code == 401
        assert response.json()["detail"] == (
            "This needs a session. Start one with POST /api/session/guest."
        )
    assert [key["id"] for key in (await lot.listed(browser))["keys"]] == [made["id"]]


async def test_a_key_outlives_a_change_of_password_and_a_log_out_everywhere(lot: Keys) -> None:
    """Both end the account's sessions, and neither touches its keys: a key is revoked
    by itself (ADR 0010, revision "an API key outlives a change of password")."""
    browser = await lot.account()
    made = await lot.made(browser)

    changed = await browser.put(
        "/api/account/password",
        json={"current_password": DENIZ["password"], "new_password": "a longer password"},
    )
    after_the_change = await lot.use(made["key"])
    logged_out = await browser.delete("/api/sessions")
    after_the_log_out = await lot.use(made["key"])

    assert (changed.status_code, logged_out.status_code) == (204, 204)
    assert (after_the_change.status_code, after_the_log_out.status_code) == (200, 200)
    assert (await browser.get("/api/keys")).status_code == 401  # no session is left
    assert await lot.count() == 1


async def test_every_refusal_of_a_key_says_bearer_and_never_repeats_the_key(
    lot: Keys,
) -> None:
    browser = await lot.account()
    made = await lot.made(browser)
    revoked = await lot.made(browser, "Revoked")
    await browser.delete(f"/api/keys/{revoked['id']}")
    unknown, malformed = never_made(), made["key"][:-1] + "-"

    unauthorized = {
        "no key": await lot.sent({}),
        "an empty one": await lot.sent({"x-api-key": "  ", "Authorization": "Bearer"}),
        "one of no key's form": await lot.sent({"x-api-key": malformed}),
        "one no row has": await lot.use(unknown),
        "one revoked": await lot.use(revoked["key"]),
        "two different": await lot.sent(
            {"Authorization": f"Bearer {made['key']}", "x-api-key": unknown}
        ),
    }
    await lot.put_on(DENIZ, None)
    forbidden = await lot.use(made["key"])

    for why, response in unauthorized.items():
        assert response.status_code == 401, why
        assert response.headers["content-type"] == PROBLEM, why
        assert response.headers["www-authenticate"] == "Bearer", why
    assert forbidden.status_code == 403
    for response in [*unauthorized.values(), forbidden]:
        for sent in (made["key"], revoked["key"], unknown, malformed):
            assert sent[12:] not in response.text


# --- Who may have keys ----------------------------------------------------------------


async def test_a_guest_has_no_keys_and_cannot_make_one(
    start_app: Any, settings: Settings, migrated: str, plans_with: Any, db: AsyncSession
) -> None:
    """Whatever its plan says: here the guests' plan allows two."""
    assert migrated
    lot = Keys(start_app, settings, plans_with(plans={"guest": GUESTS_WITH_KEYS}), db)
    guest = await lot.browser()
    await guest.post("/api/session/guest")

    refused = await guest.post("/api/keys", json={"name": "Script"})
    listed = await lot.listed(guest)

    assert (refused.status_code, refused.json()["detail"]) == (
        403,
        "A guest can't make API keys. Sign up first, with POST /api/account.",
    )
    assert listed == {"keys": [], "at_most": 0}
    assert await lot.count() == 0


async def test_a_key_a_guests_row_was_given_by_hand_is_no_key(
    start_app: Any,
    settings: Settings,
    migrated: str,
    plans_with: Any,
    db: AsyncSession,
    clock: Any,
) -> None:
    """No route makes one, and it is answered as a key that isn't known all the same,
    whatever the guests' plan says: here it allows two."""
    assert migrated
    lot = Keys(start_app, settings, plans_with(plans={"guest": GUESTS_WITH_KEYS}), db)
    guest = await lot.browser()
    me = (await guest.post("/api/session/guest")).json()
    key, _ = await keys.make(
        db, user_id=uuid.UUID(me["id"]), name="By hand", at_most=2, key_id=new_id(), now=clock.now
    )
    await db.commit()

    refused = await lot.use(key)

    assert (refused.status_code, refused.json()["detail"]) == (401, UNKNOWN)
    assert refused.headers["www-authenticate"] == "Bearer"


async def test_an_account_whose_plan_allows_none_cannot_make_one(lot: Keys) -> None:
    browser = await lot.account(plan=None)  # the accounts' plan, `member`

    refused = await browser.post("/api/keys", json={"name": "Script"})
    listed = await lot.listed(browser)

    assert (refused.status_code, refused.json()["detail"]) == (
        403,
        'Your plan, "Member", allows no API keys.',
    )
    assert listed == {"keys": [], "at_most": 0}
    assert await lot.count() == 0


async def test_an_account_at_its_plans_number_makes_no_more(lot: Keys) -> None:
    browser = await lot.account()
    made = [await lot.made(browser), await lot.made(browser)]

    refused = await browser.post("/api/keys", json={"name": "A third"})

    assert (refused.status_code, refused.json()["detail"]) == (
        409,
        'You have 2 API keys, as many as your plan, "Developer", allows. Revoke one to make'
        " another.",
    )
    assert [key["id"] for key in (await lot.listed(browser))["keys"]] == [key["id"] for key in made]
    assert await lot.count() == 2


async def test_an_account_with_the_one_key_its_plan_allows_is_told_of_one_key(
    start_app: Any, settings: Settings, migrated: str, plans_with: Any, db: AsyncSession
) -> None:
    assert migrated
    one = Keys(
        start_app, settings, plans_with(plans={"dev": {"title": "Developer", "api_keys": 1}}), db
    )
    browser = await one.account()
    await one.made(browser)

    refused = await browser.post("/api/keys", json={"name": "A second"})

    assert (refused.status_code, refused.json()["detail"]) == (
        409,
        'You have 1 API key, as many as your plan, "Developer", allows. Revoke one to make'
        " another.",
    )
    assert await one.count() == 1


async def test_an_account_past_its_plans_number_is_told_how_many_to_revoke(
    start_app: Any, settings: Settings, migrated: str, plans_with: Any, db: AsyncSession
) -> None:
    """As after the plan's number was lowered under keys that were made before."""
    assert migrated
    roomy = Keys(
        start_app, settings, plans_with(plans={"dev": {"title": "Big", "api_keys": 4}}), db
    )
    browser = await roomy.account()
    for _ in range(4):
        await roomy.made(browser)
    lowered = Keys(
        start_app, settings, plans_with(plans={"dev": {"title": "Small", "api_keys": 1}}), db
    )
    again = await lowered.browser()  # the same session, on an app that read the new file
    again.cookies.set("actio_session", browser.cookies["actio_session"])

    refused = await again.post("/api/keys", json={"name": "A fifth"})

    assert (refused.status_code, refused.json()["detail"]) == (
        409,
        'You have 4 API keys, and your plan, "Small", allows 1. Revoke 4 to make another.',
    )
    assert (await lowered.listed(again))["at_most"] == 1
    assert await roomy.count() == 4


async def test_an_account_taken_off_its_plan_has_its_keys_refused_listed_and_revocable(
    lot: Keys,
) -> None:
    """The plan is asked at every use of a key: taken off it, the account's keys stop
    working at their next request, and work again once it is put back."""
    browser = await lot.account()
    first, second = await lot.made(browser, "First"), await lot.made(browser, "Second")

    await lot.put_on(DENIZ, None)
    refused = await lot.use(first["key"])
    listed = await lot.listed(browser)
    revoked = await browser.delete(f"/api/keys/{second['id']}")
    await lot.put_on(DENIZ, "dev")
    back = await lot.use(first["key"])

    assert (refused.status_code, refused.json()["detail"]) == (
        403,
        'Your plan, "Member", allows no API keys.',
    )
    assert "www-authenticate" not in refused.headers
    assert [key["name"] for key in listed["keys"]] == ["First", "Second"]
    assert listed["at_most"] == 0
    assert revoked.status_code == 204
    assert back.status_code == 200
    assert back.json()["plan"] == {"id": "dev", "title": "Developer"}
    assert [key["name"] for key in (await lot.listed(browser))["keys"]] == ["First"]


async def test_an_account_on_a_plan_the_file_does_not_have_is_held_to_the_accounts_plan(
    lot: Keys,
) -> None:
    """The file was changed, and the account's row was not: the accounts' plan, which
    allows no keys, decides for its keys too."""
    browser = await lot.account()
    made = await lot.made(browser)

    await lot.put_on(DENIZ, "gold")
    used = await lot.use(made["key"])
    another = await browser.post("/api/keys", json={"name": "Another"})

    assert (used.status_code, used.json()["detail"]) == (
        403,
        'Your plan, "Member", allows no API keys.',
    )
    assert (another.status_code, another.json()["detail"]) == (
        403,
        'Your plan, "Member", allows no API keys.',
    )
    assert (await lot.listed(browser))["at_most"] == 0


async def until_two_wait_for_a_lock(db: AsyncSession) -> None:
    """Until two transactions on this test's database wait for a lock another holds, as
    Postgres says. With a time limit: the test fails if they never do."""
    async with asyncio.timeout(PATIENCE_S):
        while True:
            waiting = await db.scalar(
                text(
                    "SELECT count(DISTINCT pid) FROM pg_locks WHERE NOT granted AND pid IN"
                    " (SELECT pid FROM pg_stat_activity WHERE datname = current_database())"
                )
            )
            await db.commit()  # the next look starts from what the database has then
            if waiting == 2:
                return
            await asyncio.sleep(0.01)


async def test_two_keys_made_at_once_one_short_of_the_plans_number_make_one(
    lot: Keys, migrated: str
) -> None:
    """Each request counts the account's keys after it has taken the account's row: the
    second counts once the first has committed, and finds its key."""
    browser = await lot.account()
    await lot.made(browser, "First")
    engine = create_engine(migrated)
    try:
        async with create_session_factory(engine)() as holder:
            await holder.execute(
                text("SELECT id FROM users WHERE email = :email FOR UPDATE"),
                {"email": DENIZ["email"]},
            )
            asked = [
                asyncio.create_task(browser.post("/api/keys", json={"name": name}))
                for name in ("Second", "Also second")
            ]
            try:
                await until_two_wait_for_a_lock(lot.db)
                assert not any(task.done() for task in asked)
            finally:
                await holder.rollback()  # whatever happened: the row is let go
            async with asyncio.timeout(PATIENCE_S):
                answered = await asyncio.gather(*asked)
    finally:
        await engine.dispose()

    assert sorted(response.status_code for response in answered) == [201, 409]
    assert await lot.count() == 2


# --- When a key was last used ---------------------------------------------------------


async def test_last_used_is_empty_until_used_and_written_at_most_once_a_minute(
    lot: Keys, clock: Any
) -> None:
    browser = await lot.account()
    made = await lot.made(browser)

    async def last_used() -> str | None:
        [key] = (await lot.listed(browser))["keys"]
        used: str | None = key["last_used_at"]
        return used

    seen = [await last_used()]
    clock.advance(minutes=10)
    await lot.use(made["key"])
    seen.append(await last_used())
    clock.advance(seconds=59)
    await lot.use(made["key"])
    seen.append(await last_used())
    clock.advance(seconds=1)
    await lot.use(made["key"])
    seen.append(await last_used())

    assert seen == [
        None,
        "2026-11-10T12:10:00Z",
        "2026-11-10T12:10:00Z",  # within the minute
        "2026-11-10T12:11:00Z",
    ]


# --- The API's description ------------------------------------------------------------


async def test_the_routes_are_in_the_api_description(client: httpx.AsyncClient) -> None:
    description = (await client.get("/api/openapi.json")).json()
    paths = description["paths"]

    named = {
        (path, method): operation["operationId"]
        for path, methods in paths.items()
        for method, operation in methods.items()
        if path.startswith(("/api/keys", "/api/key"))
    }
    get_key = paths["/api/key"]["get"]

    assert named == {
        ("/api/keys", "post"): "make_key",
        ("/api/keys", "get"): "list_keys",
        ("/api/keys/{key_id}", "delete"): "revoke_key",
        ("/api/key", "get"): "get_key",
    }
    assert description["components"]["securitySchemes"] == {
        "bearer": {
            "type": "http",
            "scheme": "bearer",
            "description": "An API key, sent as `Authorization: Bearer act_…`.",
        },
        "x-api-key": {
            "type": "apiKey",
            "in": "header",
            "name": "x-api-key",
            "description": "An API key, sent in the `x-api-key` header.",
        },
    }
    assert get_key["security"] == [{"bearer": []}, {"x-api-key": []}]
    assert {"401", "403"} <= set(get_key["responses"])
    for made_with_a_session in ("post", "get"):
        assert "security" not in paths["/api/keys"][made_with_a_session]
    assert "security" not in paths["/api/keys/{key_id}"]["delete"]
    assert (
        "the only one that ever holds it"
        in (description["components"]["schemas"]["NewKey"]["properties"]["key"]["description"])
    )
