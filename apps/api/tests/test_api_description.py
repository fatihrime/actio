"""The API's description of itself (OpenAPI), kept as a file the web app builds on.

`apps/api/openapi.json` is what `GET /api/openapi.json` answers, recorded. The web app's
types are generated from that file (`apps/web/lib/api/schema.d.ts`), and one of its
tests fails when they no longer match it. So the two sides can't drift apart silently:

- change a route or a schema, and the comparison here fails;
- record the file again, and the web app's test fails until its types are generated
  again, after which the TypeScript compiler points at every call that no longer fits.

To record both again, after a change to the API that is meant:

    make openapi

and review the difference in the files.
"""

import json
import os
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.anyio

RECORDED = Path(__file__).parents[1] / "openapi.json"
RECORD = os.environ.get("ACTIO_RECORD_OPENAPI") == "1"

PROBLEM = {"$ref": "#/components/schemas/Problem"}
METHODS = {"get", "put", "post", "delete", "patch"}


@pytest.fixture
async def description(client: httpx.AsyncClient) -> dict[str, Any]:
    response = await client.get("/api/openapi.json")
    assert response.status_code == 200
    document: dict[str, Any] = response.json()
    return document


def operations(description: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Every operation, under a name such as `POST /api/session`."""
    return {
        f"{method.upper()} {path}": operation
        for path, methods in description["paths"].items()
        for method, operation in methods.items()
        if method in METHODS
    }


async def test_the_description_is_the_recorded_one(description: dict[str, Any]) -> None:
    produced = json.dumps(description, indent=2, ensure_ascii=False) + "\n"

    if RECORD:
        RECORDED.write_text(produced)
        return
    assert RECORDED.exists(), f"{RECORDED} is missing. Record it: make openapi"
    assert produced == RECORDED.read_text(), (
        f"The API no longer matches {RECORDED.name}. If the change is meant, record "
        "the file and the web app's types again: make openapi"
    )


async def test_operations_are_named_after_their_routes(description: dict[str, Any]) -> None:
    names = {name: operation["operationId"] for name, operation in operations(description).items()}

    assert names["POST /api/session/guest"] == "start_guest_session"
    assert names["POST /api/conversations/{conversation_id}/turns"] == "start_turn"
    assert names["GET /api/models"] == "list_models"
    assert len(set(names.values())) == len(names)


async def test_every_error_is_described_as_a_problem_document(
    description: dict[str, Any],
) -> None:
    # /api/readyz answers 503 with its own report of what is missing.
    own_body = {("GET /api/readyz", "503")}
    errors = {
        (name, status): response
        for name, operation in operations(description).items()
        for status, response in operation["responses"].items()
        if status[0] in "45" and (name, status) not in own_body
    }

    assert len(errors) > 10
    for where, response in errors.items():
        assert response["content"] == {"application/problem+json": {"schema": PROBLEM}}, where
        assert response["description"], where


async def test_an_invalid_request_is_described_as_the_problem_it_gets(
    description: dict[str, Any],
) -> None:
    turn = operations(description)["POST /api/conversations/{conversation_id}/turns"]

    assert turn["responses"]["422"]["content"]["application/problem+json"]["schema"] == PROBLEM
    # The framework's own description of a 422, which this API never sends.
    assert "HTTPValidationError" not in description["components"]["schemas"]
    assert "ValidationError" not in description["components"]["schemas"]


async def test_a_route_that_needs_a_session_says_it_answers_401_without_one(
    description: dict[str, Any], client: httpx.AsyncClient
) -> None:
    # Asked without a session cookie, a route that needs one answers 401 before it
    # looks at anything else.
    guarded = []
    for name, operation in operations(description).items():
        method, path = name.split(" ")
        response = await client.request(
            method, path.replace("{conversation_id}", str(uuid.uuid4())), json={}
        )
        if response.status_code == 401:
            guarded.append((name, operation))

    assert len(guarded) > 8
    silent = [name for name, operation in guarded if "401" not in operation["responses"]]
    assert silent == [], "Give these routes `responses=NO_SESSION` (actio.api.dependencies)."


async def test_a_real_error_has_what_the_description_promises(
    description: dict[str, Any], client: httpx.AsyncClient
) -> None:
    problem = description["components"]["schemas"]["Problem"]

    no_session = (await client.get("/api/me")).json()
    malformed = (await client.post("/api/session", json={"email": 7})).json()

    assert set(problem["required"]) == {"type", "title", "status", "detail"}
    assert problem["additionalProperties"] is True  # routes add members of their own
    for body in (no_session, malformed):
        assert set(problem["required"]) <= set(body)
    assert "errors" in malformed
