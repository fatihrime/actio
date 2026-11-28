"""The developer gateway over HTTP: the routes under `/v1`, which take an API key and
answer in the form OpenAI's API has, and its errors, which are OpenAI's whoever raised
them (ADR 0034). What OpenAI's own SDK makes of them is `test_gateway_sdk.py`'s.

No key is written in this file: each is made when the test runs, through the routes,
or drawn as a key is, for one that no row has."""

import secrets
import string
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from actio.api import problems
from actio.api.problems import Problem
from actio.catalog import Catalog
from actio.core.settings import Settings
from actio.identity import accounts

pytestmark = pytest.mark.anyio

JSON = "application/json"
PROBLEM = "application/problem+json"
DENIZ = {"email": "deniz@example.com", "password": "a long password"}
DEV = {"dev": {"title": "Developer", "api_keys": 2}}
"""A plan beside the tests' two, `guest` and `member`, which allow no keys: two at once."""
PATHS = ["/v1/models", "/v1/models/standard"]
"""The gateway's two routes, as a program asks them of the tests' one model."""

NO_KEY = (
    "This needs an API key, sent as Authorization: Bearer followed by the key, or in"
    " x-api-key. An account makes one with POST /api/keys."
)
UNKNOWN = "This API key isn't known: it may be mistyped, or revoked."
TWO_KEYS = (
    "This request carries two different API keys, one in Authorization and one in"
    " x-api-key. Send one of them."
)
NO_SUCH_MODEL = "No model is called that. GET /v1/models lists them."


def never_made() -> str:
    """A text of a key's form that no key has: drawn now, as a key is."""
    drawn = string.ascii_letters + string.digits

    def run(length: int) -> str:
        return "".join(secrets.choice(drawn) for _ in range(length))

    return f"act_{run(8)}_{run(43)}"


def bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def openais(message: str, *, kind: str = "invalid_request_error", code: str | None) -> Any:
    """OpenAI's error, as a program reads it: always the four members."""
    return {"error": {"message": message, "type": kind, "param": None, "code": code}}


def listed(name: str) -> dict[str, Any]:
    """A model on offer, as the gateway gives it."""
    return {"id": name, "object": "model", "created": 0, "owned_by": "actio"}


@dataclass(frozen=True)
class Opened:
    browser: httpx.AsyncClient
    """Logged in to an account on `dev`, which has made `key`."""
    program: httpx.AsyncClient
    """Has no cookie."""
    key: str


class Gateway:
    """Apps on this test's database, its clock and its plans: an account signs up in a
    browser and makes a key there, and a program carries it, with no cookie."""

    def __init__(
        self, start_app: Any, settings: Settings, plans_with: Any, db: AsyncSession
    ) -> None:
        self.start_app = start_app
        self.settings = settings
        self.plans_with = plans_with
        self.db = db

    async def opened(self, catalog: Catalog | None = None) -> Opened:
        """Apps that offer `catalog`, or the tests' one model, `standard`."""
        names = ["standard"] if catalog is None else [offered.name for offered in catalog]
        plans = self.plans_with(plans=DEV, models=names)
        browser: httpx.AsyncClient = await self.start_app(
            self.settings, catalog=catalog, plans=plans
        )
        signed_up = await browser.post("/api/account", json=DENIZ)
        assert signed_up.status_code == 201, signed_up.text
        await self.put_on("dev")
        made = await browser.post("/api/keys", json={"name": "Script"})
        assert made.status_code == 201, made.text
        program: httpx.AsyncClient = await self.start_app(
            self.settings, catalog=catalog, plans=plans
        )
        return Opened(browser, program, made.json()["key"])

    async def put_on(self, plan: str | None) -> None:
        """Put the account on `plan` of its own, as `actio plan` does, or with `None`
        take its own plan away."""
        await accounts.put_on_plan(self.db, email=DENIZ["email"], plan=plan)
        await self.db.commit()


@pytest.fixture
def gateway(
    start_app: Any, settings: Settings, migrated: str, plans_with: Any, db: AsyncSession
) -> Gateway:
    assert migrated == settings.database_url.get_secret_value()
    return Gateway(start_app, settings, plans_with, db)


# --- The key at the door --------------------------------------------------------------


@pytest.mark.parametrize("path", PATHS)
async def test_a_request_without_a_key_that_works_is_refused_in_openais_form(
    gateway: Gateway, path: str
) -> None:
    """In the words `GET /api/key` says, with `invalid_api_key`, and never the key sent."""
    opened = await gateway.opened()
    unknown = never_made()
    sent = {
        "no key": ({}, NO_KEY),
        "a key no row has": (bearer(unknown), UNKNOWN),
        "two different keys": (bearer(opened.key) | {"x-api-key": unknown}, TWO_KEYS),
    }

    for why, (headers, said) in sent.items():
        response = await opened.program.get(path, headers=headers)
        at_the_api = await opened.program.get("/api/key", headers=headers)

        assert not opened.program.cookies
        assert response.status_code == 401, why
        assert response.headers["content-type"] == JSON, why
        assert response.headers["www-authenticate"] == "Bearer", why
        assert response.json() == openais(said, code="invalid_api_key"), why
        assert at_the_api.json()["detail"] == said, why
        for key in (unknown, opened.key):
            assert key[12:] not in response.text, why


async def test_a_key_whose_accounts_plan_allows_none_is_refused_with_no_code(
    gateway: Gateway,
) -> None:
    opened = await gateway.opened()
    await gateway.put_on(None)  # the accounts' plan, `member`

    for path in PATHS:
        response = await opened.program.get(path, headers=bearer(opened.key))

        assert response.status_code == 403
        assert response.headers["content-type"] == JSON
        assert "www-authenticate" not in response.headers
        assert response.json() == openais('Your plan, "Member", allows no API keys.', code=None)


async def test_a_sessions_cookie_is_no_key_here(gateway: Gateway) -> None:
    opened = await gateway.opened()
    assert (await opened.browser.get("/api/me")).status_code == 200

    for path in PATHS:
        response = await opened.browser.get(path)

        assert response.status_code == 401
        assert response.json() == openais(NO_KEY, code="invalid_api_key")


async def test_a_key_in_x_api_key_works_as_one_sent_as_a_bearer(gateway: Gateway) -> None:
    opened = await gateway.opened()

    for path in PATHS:
        as_a_bearer = await opened.program.get(path, headers=bearer(opened.key))
        in_x_api_key = await opened.program.get(path, headers={"x-api-key": opened.key})

        assert (as_a_bearer.status_code, in_x_api_key.status_code) == (200, 200)
        assert in_x_api_key.json() == as_a_bearer.json()


# --- The models -----------------------------------------------------------------------


async def test_every_model_on_offer_is_listed_by_its_name_in_the_files_order(
    gateway: Gateway, lineup: Any
) -> None:
    """The one that names conversations too, and one whose server isn't there: as
    `GET /api/models` names them, for whichever plan."""
    lineup.add("standard")
    lineup.add("local")
    lineup.add("away", state="unreachable")
    lineup.add("namer")
    opened = await gateway.opened(lineup.catalog(titles="namer"))

    response = await opened.program.get("/v1/models", headers=bearer(opened.key))
    for_the_page = (await opened.browser.get("/api/models")).json()

    assert response.status_code == 200
    assert response.headers["content-type"] == JSON
    assert response.json() == {
        "object": "list",
        "data": [listed("standard"), listed("local"), listed("away"), listed("namer")],
    }
    assert [model["id"] for model in for_the_page["models"]] == [
        model["id"] for model in response.json()["data"]
    ]


async def test_no_models_server_is_asked_for_the_list_or_for_a_model(
    gateway: Gateway, lineup: Any, asks_ended: Any
) -> None:
    """Not for a model whose server isn't there, nor for one that isn't on its server:
    the catalog alone says what is on offer."""
    lineup.add("standard")
    lineup.add("away", state="unreachable")
    lineup.add("missing", state="missing")
    opened = await gateway.opened(lineup.catalog())

    the_list = await opened.program.get("/v1/models", headers=bearer(opened.key))
    one = await opened.program.get("/v1/models/away", headers=bearer(opened.key))
    another = await opened.program.get("/v1/models/missing", headers=bearer(opened.key))
    await asks_ended()

    assert [the_list.status_code, one.status_code, another.status_code] == [200, 200, 200]
    assert (one.json(), another.json()) == (listed("away"), listed("missing"))
    assert lineup.asked == []


async def test_one_model_is_read_by_its_name_and_one_nobody_offers_is_not_found(
    gateway: Gateway, lineup: Any
) -> None:
    """Never by its name on its server, and the id sent is not said back: a path's text
    has no bound."""
    lineup.add("standard")
    lineup.add("local")
    opened = await gateway.opened(lineup.catalog())

    one = await opened.program.get("/v1/models/local", headers=bearer(opened.key))
    refused = {
        sent: await opened.program.get(f"/v1/models/{sent}", headers=bearer(opened.key))
        for sent in ("zeppelin", "scripted-local")
    }

    assert one.status_code == 200
    assert one.json() == listed("local")
    for sent, response in refused.items():
        assert response.status_code == 404, sent
        assert response.headers["content-type"] == JSON, sent
        assert response.json() == openais(NO_SUCH_MODEL, code="model_not_found"), sent
        assert sent not in response.text, sent


@pytest.mark.parametrize(
    "sent",
    ["openai/gpt-oss-20b", "openai%2Fgpt-oss-20b", "standard/local", "standard/", "a/b/c", ""],
)
async def test_all_that_follows_in_the_path_is_the_models_id_and_none_is_no_model(
    gateway: Gateway, lineup: Any, sent: str
) -> None:
    """An id with a slash in it, encoded or not, is one id, and a model nobody offers:
    never the framework's own 404 for a path no route has, which has no code. And a path
    that ends at `/v1/models/` names no model: not the default one."""
    lineup.add("standard")
    lineup.add("local")
    opened = await gateway.opened(lineup.catalog())

    response = await opened.program.get(f"/v1/models/{sent}", headers=bearer(opened.key))

    assert response.status_code == 404
    assert response.headers["content-type"] == JSON
    assert response.json() == openais(NO_SUCH_MODEL, code="model_not_found")


# --- Errors on the gateway, and beside it ---------------------------------------------


async def test_a_path_or_a_method_no_route_has_under_v1_is_refused_in_openais_form(
    client: httpx.AsyncClient,
) -> None:
    missing = await client.get("/v1/nothing/here")
    not_taken = await client.post("/v1/models")

    assert missing.status_code == 404
    assert missing.headers["content-type"] == JSON
    assert missing.json() == openais("Not Found", code=None)
    assert not_taken.status_code == 405
    assert not_taken.headers["content-type"] == JSON
    assert not_taken.headers["allow"] == "GET"
    assert not_taken.json() == openais("Method Not Allowed", code=None)


@pytest.mark.parametrize("path", ["/v10/models", "/api/no-such-thing", "/api/v1/models"])
async def test_beside_the_gateway_an_error_is_a_problem_document_as_ever(
    client: httpx.AsyncClient, path: str
) -> None:
    """The gateway is a path that starts with `/v1/`: not one that starts with `/v1`,
    nor one that has `/v1/` further in."""
    response = await client.get(path)

    assert response.status_code == 404
    assert response.headers["content-type"] == PROBLEM
    assert response.json() == {
        "type": "about:blank",
        "title": "Not Found",
        "status": 404,
        "detail": "Not Found",
    }


@pytest.fixture
async def toy() -> AsyncIterator[httpx.AsyncClient]:
    """A tiny app with the error formats installed, to raise each kind of error on
    demand, on the gateway and beside it."""
    app = FastAPI()
    problems.install(app)

    @app.get("/v1/raised/{status}")
    async def raised_on_the_gateway(status: int) -> None:
        raise Problem(status, "Something happened.")

    @app.get("/v1/refused-with-a-code")
    async def refused_with_a_code_of_its_own() -> None:
        raise Problem(401, "Something happened.", code="its_own")

    @app.get("/v1/too-fast")
    async def too_fast_on_the_gateway() -> None:
        raise Problem(
            429, "Too fast.", headers={"Retry-After": "7"}, code="slow_down", retry_after=7
        )

    @app.get("/api/too-fast")
    async def too_fast_beside_it() -> None:
        raise Problem(
            429, "Too fast.", headers={"Retry-After": "7"}, code="slow_down", retry_after=7
        )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://toy"
    ) as client:
        yield client


TYPES = {
    400: "invalid_request_error",
    401: "invalid_request_error",
    403: "invalid_request_error",
    404: "invalid_request_error",
    405: "invalid_request_error",
    409: "invalid_request_error",
    413: "invalid_request_error",
    422: "invalid_request_error",
    429: "rate_limit_error",
    500: "server_error",
    502: "server_error",
    503: "service_unavailable_error",
}
"""An error's `type` by its status, as the gateway says it."""


@pytest.mark.parametrize("status", TYPES)
async def test_an_errors_type_goes_by_its_status_and_a_401s_code_is_the_keys(
    toy: httpx.AsyncClient, status: int
) -> None:
    response = await toy.get(f"/v1/raised/{status}")

    assert response.status_code == status
    assert response.headers["content-type"] == JSON
    assert response.json() == openais(
        "Something happened.",
        kind=TYPES[status],
        code="invalid_api_key" if status == 401 else None,
    )


async def test_a_code_its_raiser_gave_is_said_on_a_401_too(toy: httpx.AsyncClient) -> None:
    """`invalid_api_key` is for a 401 whose raiser gave none: never in a given one's
    place."""
    response = await toy.get("/v1/refused-with-a-code")

    assert response.status_code == 401
    assert response.json() == openais("Something happened.", code="its_own")


async def test_a_problems_headers_go_out_on_both_surfaces_and_each_says_what_is_its_own(
    toy: httpx.AsyncClient,
) -> None:
    """Its code on the gateway alone, and its extra members beside it alone."""
    on_the_gateway = await toy.get("/v1/too-fast")
    beside_it = await toy.get("/api/too-fast")

    assert on_the_gateway.status_code == 429
    assert on_the_gateway.headers["content-type"] == JSON
    assert on_the_gateway.headers["retry-after"] == "7"
    assert on_the_gateway.json() == openais("Too fast.", kind="rate_limit_error", code="slow_down")
    assert beside_it.status_code == 429
    assert beside_it.headers["content-type"] == PROBLEM
    assert beside_it.headers["retry-after"] == "7"
    assert beside_it.json() == {
        "type": "about:blank",
        "title": "Too Many Requests",
        "status": 429,
        "detail": "Too fast.",
        "retry_after": 7,
    }
