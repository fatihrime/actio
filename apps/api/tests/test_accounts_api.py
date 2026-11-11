"""Lasting accounts over HTTP: signing up, logging in, the password, logging out everywhere."""

from http.cookies import SimpleCookie
from typing import Any

import httpx
import pytest

from actio.core.settings import Settings

pytestmark = pytest.mark.anyio

DENIZ = {"email": "deniz@example.com", "password": "correct horse battery staple"}
PROBLEM = "application/problem+json"


def max_age(response: httpx.Response) -> int:
    jar: SimpleCookie = SimpleCookie(response.headers["set-cookie"])
    [morsel] = jar.values()
    return int(morsel["max-age"])


async def test_signing_up_creates_an_account_and_logs_in(api: httpx.AsyncClient) -> None:
    response = await api.post("/api/account", json=DENIZ)

    assert response.status_code == 201
    body = response.json()
    assert (body["is_guest"], body["email"]) == (False, "deniz@example.com")
    assert body["session_expires_at"] == "2026-12-10T12:00:00Z"  # 30 days after the clock's noon
    assert max_age(response) == 400 * 24 * 3600  # the cookie outlives the session it carries
    assert "password" not in response.text
    assert (await api.get("/api/me")).json() == body


async def test_a_guest_who_signs_up_keeps_the_same_account(api: httpx.AsyncClient) -> None:
    guest = (await api.post("/api/session/guest")).json()
    guest_token = api.cookies["actio_session"]

    account = (await api.post("/api/account", json=DENIZ)).json()

    assert account["id"] == guest["id"]
    assert (account["is_guest"], account["email"]) == (False, "deniz@example.com")
    assert api.cookies["actio_session"] != guest_token  # a new session for the new standing
    assert (await api.get("/api/me")).json() == account


async def test_an_email_can_only_be_registered_once(
    start_app: Any, settings: Settings, migrated: str
) -> None:
    assert migrated
    one, other = await start_app(settings), await start_app(settings)
    await one.post("/api/account", json=DENIZ)

    response = await other.post("/api/account", json=DENIZ | {"email": "Deniz@Example.COM"})

    assert response.status_code == 409
    assert response.headers["content-type"] == PROBLEM
    assert response.json()["detail"] == "An account with this email address already exists."
    assert "set-cookie" not in response.headers


async def test_someone_with_an_account_cannot_sign_up_again(api: httpx.AsyncClient) -> None:
    await api.post("/api/account", json=DENIZ)

    response = await api.post("/api/account", json=DENIZ | {"email": "other@example.com"})

    assert response.status_code == 409
    assert response.json()["detail"] == "You already have an account. Log out to create another."


@pytest.mark.parametrize(
    ("body", "where"),
    [
        ({"email": "deniz@example.com", "password": "too short"}, "body.password"),
        ({"email": "deniz@example.com", "password": "x" * 257}, "body.password"),
        ({"email": "not an address", "password": "correct horse battery staple"}, "body.email"),
        ({"email": "deniz@example.com"}, "body.password"),
    ],
)
async def test_signing_up_needs_a_real_address_and_a_long_enough_password(
    api: httpx.AsyncClient, body: dict[str, str], where: str
) -> None:
    response = await api.post("/api/account", json=body)

    assert response.status_code == 422
    assert response.headers["content-type"] == PROBLEM
    assert [error["where"] for error in response.json()["errors"]] == [where]
    assert "x" * 257 not in response.text  # what was sent as a password is never echoed


async def test_logging_in_from_another_browser(
    start_app: Any, settings: Settings, migrated: str
) -> None:
    assert migrated
    laptop, phone = await start_app(settings), await start_app(settings)
    account = (await laptop.post("/api/account", json=DENIZ)).json()

    response = await phone.post("/api/session", json=DENIZ)

    assert response.status_code == 200
    assert response.json()["id"] == account["id"]
    assert phone.cookies["actio_session"] != laptop.cookies["actio_session"]
    assert (await phone.get("/api/me")).status_code == 200
    assert (await laptop.get("/api/me")).status_code == 200  # the laptop stays logged in


@pytest.mark.parametrize(
    "attempt",
    [
        {"email": "deniz@example.com", "password": "wrong horse battery staple"},
        {"email": "nobody@example.com", "password": "correct horse battery staple"},
        {"email": "not an address", "password": "x"},
    ],
)
async def test_a_wrong_password_and_an_unknown_email_get_the_same_answer(
    start_app: Any, settings: Settings, migrated: str, attempt: dict[str, str]
) -> None:
    assert migrated
    await (await start_app(settings)).post("/api/account", json=DENIZ)
    stranger = await start_app(settings)

    response = await stranger.post("/api/session", json=attempt)

    assert response.status_code == 401
    assert response.json() == {
        "type": "about:blank",
        "title": "Unauthorized",
        "status": 401,
        "detail": "Wrong email address or password.",
    }
    assert "set-cookie" not in response.headers


async def test_logging_in_ends_the_guest_session_the_browser_had(
    start_app: Any, settings: Settings, migrated: str
) -> None:
    assert migrated
    await (await start_app(settings)).post("/api/account", json=DENIZ)
    browser = await start_app(settings)
    guest = (await browser.post("/api/session/guest")).json()
    guest_token = browser.cookies["actio_session"]

    logged_in = (await browser.post("/api/session", json=DENIZ)).json()

    assert logged_in["id"] != guest["id"]
    browser.cookies.set("actio_session", guest_token)
    assert (await browser.get("/api/me")).status_code == 401  # the guest is gone


async def test_a_failed_login_leaves_the_guest_session_alone(
    api: httpx.AsyncClient,
) -> None:
    guest = (await api.post("/api/session/guest")).json()

    response = await api.post("/api/session", json=DENIZ)  # no such account

    assert response.status_code == 401
    assert (await api.get("/api/me")).json() == guest


async def test_changing_the_password(start_app: Any, settings: Settings, migrated: str) -> None:
    assert migrated
    laptop, phone = await start_app(settings), await start_app(settings)
    await laptop.post("/api/account", json=DENIZ)
    await phone.post("/api/session", json=DENIZ)
    change = {"current_password": DENIZ["password"], "new_password": "a brand new long password"}

    response = await laptop.put("/api/account/password", json=change)

    assert response.status_code == 204
    assert (await laptop.get("/api/me")).status_code == 200  # this browser got a new cookie
    assert (await phone.get("/api/me")).status_code == 401  # every other one is logged out
    assert (await phone.post("/api/session", json=DENIZ)).status_code == 401  # the old password
    renewed = DENIZ | {"password": "a brand new long password"}
    assert (await phone.post("/api/session", json=renewed)).status_code == 200


async def test_the_password_changes_only_with_the_current_one(api: httpx.AsyncClient) -> None:
    await api.post("/api/account", json=DENIZ)
    change = {"current_password": "a guess", "new_password": "a brand new long password"}

    response = await api.put("/api/account/password", json=change)

    assert response.status_code == 403
    assert response.json()["detail"] == "The current password is wrong."
    assert (await api.get("/api/me")).status_code == 200


async def test_a_guest_has_no_password_to_change(api: httpx.AsyncClient) -> None:
    await api.post("/api/session/guest")
    change = {"current_password": "", "new_password": "a brand new long password"}

    response = await api.put("/api/account/password", json=change)

    assert response.status_code == 409
    assert response.json()["detail"] == "A guest has no password. Sign up with POST /api/account."


async def test_nobody_has_no_password_to_change(api: httpx.AsyncClient) -> None:
    change = {"current_password": "x", "new_password": "a brand new long password"}

    assert (await api.put("/api/account/password", json=change)).status_code == 401


async def test_logging_out_everywhere(start_app: Any, settings: Settings, migrated: str) -> None:
    assert migrated
    laptop, phone = await start_app(settings), await start_app(settings)
    await laptop.post("/api/account", json=DENIZ)
    await phone.post("/api/session", json=DENIZ)

    response = await phone.delete("/api/sessions")

    assert response.status_code == 204
    assert (await phone.get("/api/me")).status_code == 401
    assert (await laptop.get("/api/me")).status_code == 401
    assert (await laptop.post("/api/session", json=DENIZ)).status_code == 200  # the account remains


async def test_logging_out_here_leaves_other_browsers_logged_in(
    start_app: Any, settings: Settings, migrated: str
) -> None:
    assert migrated
    laptop, phone = await start_app(settings), await start_app(settings)
    await laptop.post("/api/account", json=DENIZ)
    await phone.post("/api/session", json=DENIZ)

    await phone.delete("/api/session")

    assert (await phone.get("/api/me")).status_code == 401
    assert (await laptop.get("/api/me")).status_code == 200
    assert (await phone.post("/api/session", json=DENIZ)).status_code == 200  # still an account


async def test_an_accounts_session_is_renewed_by_use(api: httpx.AsyncClient, clock: Any) -> None:
    await api.post("/api/account", json=DENIZ)

    clock.advance(days=29)
    assert (await api.get("/api/me")).json()["session_expires_at"] == "2027-01-08T12:00:00Z"
    clock.advance(days=29)
    assert (await api.get("/api/me")).status_code == 200  # 58 days in, used on day 29
    clock.advance(days=31)
    assert (await api.get("/api/me")).status_code == 401  # 31 days without a request


@pytest.mark.parametrize("path", ["/api/account", "/api/session"])
async def test_another_sites_page_cannot_sign_up_or_log_in_for_the_visitor(
    api: httpx.AsyncClient, path: str
) -> None:
    """Otherwise a page could log its visitor in to the attacker's account without their knowing."""
    response = await api.post(path, json=DENIZ, headers={"Sec-Fetch-Site": "cross-site"})

    assert response.status_code == 403
