"""Guest sessions over HTTP: the cookie, who you are, and logging out."""

from http.cookies import SimpleCookie
from typing import Any

import httpx
import pytest

from actio.core.settings import Settings

pytestmark = pytest.mark.anyio

PROBLEM = "application/problem+json"


def cookie_of(response: httpx.Response) -> tuple[str, Any]:
    """The one cookie a response sets: its name, and its attributes."""
    jar: SimpleCookie = SimpleCookie(response.headers["set-cookie"])
    [(name, morsel)] = jar.items()
    return name, morsel


async def test_a_guest_session_starts_with_a_cookie(api: httpx.AsyncClient, clock: Any) -> None:
    response = await api.post("/api/session/guest")

    assert response.status_code == 201
    body = response.json()
    assert body["is_guest"] is True
    assert body["session_expires_at"] == "2026-11-11T12:00:00Z"  # a day after the clock's noon
    name, cookie = cookie_of(response)
    assert name == "actio_session"
    assert len(cookie.value) >= 43
    assert cookie["httponly"] is True  # page scripts can't read it
    assert cookie["samesite"] == "lax"
    assert cookie["path"] == "/"
    assert cookie["max-age"] == "86400"
    assert cookie["domain"] == ""  # this host only
    assert clock.now.isoformat() == "2026-11-10T12:00:00+00:00"


async def test_the_cookie_says_who_you_are(api: httpx.AsyncClient) -> None:
    started = (await api.post("/api/session/guest")).json()

    response = await api.get("/api/me")  # the client sends the cookie back, as a browser does

    assert response.status_code == 200
    assert response.json() == started


async def test_without_a_session_you_are_nobody(api: httpx.AsyncClient) -> None:
    response = await api.get("/api/me")

    assert response.status_code == 401
    assert response.headers["content-type"] == PROBLEM
    assert response.json() == {
        "type": "about:blank",
        "title": "Unauthorized",
        "status": 401,
        "detail": "This needs a session. Start one with POST /api/session/guest.",
    }


@pytest.mark.parametrize("forged", ["", "guess", "A" * 43])
async def test_a_made_up_cookie_is_nobody(api: httpx.AsyncClient, forged: str) -> None:
    api.cookies.set("actio_session", forged)

    response = await api.get("/api/me")

    assert response.status_code == 401


async def test_opening_the_page_twice_keeps_one_guest(api: httpx.AsyncClient) -> None:
    first = await api.post("/api/session/guest")
    second = await api.post("/api/session/guest")

    assert (first.status_code, second.status_code) == (201, 200)
    assert second.json() == first.json()
    assert "set-cookie" not in second.headers  # the cookie it has is still the one


async def test_two_browsers_are_two_guests(
    start_app: Any, settings: Settings, migrated: str
) -> None:
    assert migrated
    one, other = await start_app(settings), await start_app(settings)

    first = (await one.post("/api/session/guest")).json()
    second = (await other.post("/api/session/guest")).json()

    assert first["id"] != second["id"]
    assert (await one.get("/api/me")).json() == first
    assert (await other.get("/api/me")).json() == second


async def test_a_session_ends_when_its_day_is_over(api: httpx.AsyncClient, clock: Any) -> None:
    await api.post("/api/session/guest")

    clock.advance(hours=23, minutes=59)
    assert (await api.get("/api/me")).status_code == 200
    clock.advance(minutes=1)
    assert (await api.get("/api/me")).status_code == 401

    # And the same browser can simply start again, as a new guest.
    assert (await api.post("/api/session/guest")).status_code == 201


async def test_logging_out_ends_the_session_and_clears_the_cookie(api: httpx.AsyncClient) -> None:
    await api.post("/api/session/guest")
    token = api.cookies["actio_session"]

    response = await api.delete("/api/session")

    assert response.status_code == 204
    name, cookie = cookie_of(response)
    assert (name, cookie.value, cookie["max-age"]) == ("actio_session", "", "0")
    assert (await api.get("/api/me")).status_code == 401
    # The old token is dead on the server too, not just forgotten by this browser.
    api.cookies.set("actio_session", token)
    assert (await api.get("/api/me")).status_code == 401


async def test_logging_out_twice_is_not_an_error(api: httpx.AsyncClient) -> None:
    assert (await api.delete("/api/session")).status_code == 204
    assert (await api.delete("/api/session")).status_code == 204


async def test_over_https_the_cookie_is_secure_and_bound_to_the_host(
    start_app: Any, settings: Settings, migrated: str
) -> None:
    """`__Host-` and `Secure`: only this exact host gets the cookie, and only over HTTPS."""
    assert migrated
    https = await start_app(settings.model_copy(update={"public_url": "https://chat.example.com"}))

    response = await https.post("/api/session/guest")

    name, cookie = cookie_of(response)
    assert name == "__Host-actio_session"
    assert cookie["secure"] is True
    assert (cookie["path"], cookie["domain"], cookie["httponly"]) == ("/", "", True)
    assert (await https.get("/api/me")).status_code == 200


async def test_the_routes_are_in_the_api_description(api: httpx.AsyncClient) -> None:
    paths = (await api.get("/api/openapi.json")).json()["paths"]

    assert set(paths["/api/session/guest"]) == {"post"}
    assert set(paths["/api/session"]) == {"delete"}
    assert set(paths["/api/me"]) == {"get"}
