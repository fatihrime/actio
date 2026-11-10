"""The check that keeps other sites' pages from acting as a visitor (cross-site request forgery)."""

import httpx
import pytest

pytestmark = pytest.mark.anyio

HERE = "http://localhost:8080"  # the default public address, and the test client's own


@pytest.mark.parametrize(
    "headers",
    [
        {"Sec-Fetch-Site": "same-origin"},  # a page of Actio itself
        {"Sec-Fetch-Site": "none"},  # the person typed the address
        {"Origin": HERE},  # a browser that sends no Sec-Fetch-Site: plain HTTP on a LAN
        {},  # not a browser at all: a script, a command line
        {"Sec-Fetch-Site": "same-origin", "Origin": "http://evil.example"},  # the first decides
    ],
)
async def test_our_own_pages_and_non_browsers_may_change_things(
    api: httpx.AsyncClient, headers: dict[str, str]
) -> None:
    response = await api.post("/api/session/guest", headers=headers)

    assert response.status_code == 201


@pytest.mark.parametrize(
    "headers",
    [
        {"Sec-Fetch-Site": "cross-site"},  # another website's page
        {"Sec-Fetch-Site": "same-site"},  # a sibling subdomain is not us either
        {"Sec-Fetch-Site": "cross-site", "Origin": HERE},  # the browser's verdict wins
        {"Origin": "http://evil.example"},
        {"Origin": "http://localhost:9999"},  # same host, another port: another origin
        {"Origin": "https://localhost:8080"},  # another scheme too
        {"Origin": "null"},  # a sandboxed page, or a redirect that hid where it came from
    ],
)
async def test_another_sites_page_may_not(api: httpx.AsyncClient, headers: dict[str, str]) -> None:
    response = await api.post("/api/session/guest", headers=headers)

    assert response.status_code == 403
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json() == {
        "type": "about:blank",
        "title": "Forbidden",
        "status": 403,
        "detail": "This request was sent by a page on another site, so it was refused.",
    }
    assert "set-cookie" not in response.headers


async def test_logging_someone_out_from_another_site_is_refused_too(api: httpx.AsyncClient) -> None:
    await api.post("/api/session/guest")

    response = await api.delete("/api/session", headers={"Sec-Fetch-Site": "cross-site"})

    assert response.status_code == 403
    assert (await api.get("/api/me")).status_code == 200  # still logged in


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
async def test_reading_is_never_refused(api: httpx.AsyncClient, method: str) -> None:
    """A link from another site to Actio must work: reading changes nothing."""
    response = await api.request(method, "/api/healthz", headers={"Sec-Fetch-Site": "cross-site"})

    assert response.status_code != 403
