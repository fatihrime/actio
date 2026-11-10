"""Errors, whoever raises them, come out in one format (RFC 9457)."""

import httpx
import pytest
from fastapi import FastAPI

from actio.api import problems
from actio.api.problems import Problem

pytestmark = pytest.mark.anyio

PROBLEM = "application/problem+json"


async def test_an_unknown_path_is_a_problem_document(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/no-such-thing")

    assert response.status_code == 404
    assert response.headers["content-type"] == PROBLEM
    assert response.json() == {
        "type": "about:blank",
        "title": "Not Found",
        "status": 404,
        "detail": "Not Found",
    }


async def test_a_wrong_method_says_which_ones_work(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/healthz")

    assert response.status_code == 405
    assert response.headers["content-type"] == PROBLEM
    assert response.headers["allow"] == "GET"
    assert response.json()["title"] == "Method Not Allowed"


@pytest.fixture
async def toy() -> httpx.AsyncClient:
    """A tiny app with the error format installed, to raise each kind of error on demand."""
    app = FastAPI()
    problems.install(app)

    @app.get("/items/{number}")
    async def item(number: int) -> dict[str, int]:
        if number == 13:
            raise Problem(409, "Item 13 is taken.", item=13)
        return {"number": number}

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://toy")


async def test_an_invalid_request_lists_what_is_wrong_and_where(toy: httpx.AsyncClient) -> None:
    async with toy:
        response = await toy.get("/items/many")

    assert response.status_code == 422
    assert response.headers["content-type"] == PROBLEM
    body = response.json()
    assert (body["title"], body["detail"]) == (
        "Unprocessable Content",
        "The request doesn't have the expected form.",
    )
    assert [error["where"] for error in body["errors"]] == ["path.number"]


async def test_a_problem_raised_in_a_route_keeps_its_extra_members(toy: httpx.AsyncClient) -> None:
    async with toy:
        response = await toy.get("/items/13")

    assert response.status_code == 409
    assert response.json() == {
        "type": "about:blank",
        "title": "Conflict",
        "status": 409,
        "detail": "Item 13 is taken.",
        "item": 13,
    }
