"""OpenAI's official Python SDK reads the gateway's answers: proof that their form is
OpenAI's (ADR 0034). The SDK sends its requests straight to the app, in this process,
and tries nothing twice.

The SDK chooses its exception's class by the status alone, and reads `message`, `type`,
`param` and `code` from the body's `error` member: so a refusal is one of its own kind,
with what it says. What the gateway answers, member by member, is `test_gateway_api.py`'s.

No key is written in this file: each is made when the test runs, through the routes,
or drawn as a key is, for one that no row has."""

import secrets
import string
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI
from openai import AsyncOpenAI, AuthenticationError, NotFoundError, PermissionDeniedError
from sqlalchemy.ext.asyncio import AsyncSession

from actio.app import create_app
from actio.core.settings import Settings
from actio.identity import accounts
from actio.identity.passwords import Passwords

pytestmark = pytest.mark.anyio

DENIZ = {"email": "deniz@example.com", "password": "a long password"}
DEV = {"dev": {"title": "Developer", "api_keys": 2}}
"""A plan beside the tests' two, `guest` and `member`, which allow no keys: two at once."""
OFFERED = ["standard", "local", "away"]


def never_made() -> str:
    """A text of a key's form that no key has: drawn now, as a key is."""
    drawn = string.ascii_letters + string.digits

    def run(length: int) -> str:
        return "".join(secrets.choice(drawn) for _ in range(length))

    return f"act_{run(8)}_{run(43)}"


@dataclass(frozen=True)
class Gateway:
    """An app that offers three models, one of which has no server, and an account on
    `dev` that has made `key`."""

    app: FastAPI
    settings: Settings
    key: str
    db: AsyncSession

    def sdk(self, key: str) -> AsyncOpenAI:
        """The SDK's async client, with `key`, sending its requests straight to the app:
        to be used in `async with`, which closes it."""
        return AsyncOpenAI(
            api_key=key,
            base_url=f"{self.settings.public_url}/v1",
            http_client=httpx2.AsyncClient(transport=httpx2.ASGITransport(app=self.app)),
            max_retries=0,
        )

    async def take_the_plan_away(self) -> None:
        """Take the account's own plan away, as `actio plan` does: it is on the
        accounts' plan, `member`, which allows no keys."""
        await accounts.put_on_plan(self.db, email=DENIZ["email"], plan=None)
        await self.db.commit()


@pytest.fixture
async def gateway(
    settings: Settings,
    migrated: str,
    passwords: Passwords,
    plans_with: Any,
    lineup: Any,
    db: AsyncSession,
) -> AsyncIterator[Gateway]:
    assert migrated == settings.database_url.get_secret_value()
    lineup.add("standard")
    lineup.add("local")
    lineup.add("away", state="unreachable")
    app = create_app(
        settings,
        passwords=passwords,
        catalog=lineup.catalog(),
        plans=plans_with(plans=DEV, models=lineup.names),
    )
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url=settings.public_url
        ) as browser,
    ):
        signed_up = await browser.post("/api/account", json=DENIZ)
        assert signed_up.status_code == 201, signed_up.text
        await accounts.put_on_plan(db, email=DENIZ["email"], plan="dev")
        await db.commit()
        made = await browser.post("/api/keys", json={"name": "The SDK"})
        assert made.status_code == 201, made.text
        yield Gateway(app, settings, made.json()["key"], db)


async def test_the_sdk_lists_the_models_and_reads_one(gateway: Gateway) -> None:
    async with gateway.sdk(gateway.key) as sdk:
        models = [model async for model in sdk.models.list()]
        one = await sdk.models.retrieve("local")

    assert [model.to_dict() for model in models] == [
        {"id": name, "object": "model", "created": 0, "owned_by": "actio"} for name in OFFERED
    ]
    assert one.to_dict() == {"id": "local", "object": "model", "created": 0, "owned_by": "actio"}


async def test_a_key_no_row_has_is_the_sdks_authentication_error(gateway: Gateway) -> None:
    async with gateway.sdk(never_made()) as sdk:
        with pytest.raises(AuthenticationError) as raised:
            await sdk.models.list()

    assert raised.value.status_code == 401
    assert (raised.value.type, raised.value.code, raised.value.param) == (
        "invalid_request_error",
        "invalid_api_key",
        None,
    )
    assert raised.value.message == (
        "Error code: 401 - This API key isn't known: it may be mistyped, or revoked."
    )


async def test_a_key_whose_plan_allows_none_is_the_sdks_permission_error(
    gateway: Gateway,
) -> None:
    await gateway.take_the_plan_away()

    async with gateway.sdk(gateway.key) as sdk:
        with pytest.raises(PermissionDeniedError) as raised:
            await sdk.models.retrieve("standard")

    assert raised.value.status_code == 403
    assert (raised.value.type, raised.value.code, raised.value.param) == (
        "invalid_request_error",
        None,
        None,
    )
    assert raised.value.message == 'Error code: 403 - Your plan, "Member", allows no API keys.'


@pytest.mark.parametrize("asked", ["zeppelin", "openai/gpt-oss-20b"])
async def test_a_model_nobody_offers_is_the_sdks_not_found_error(
    gateway: Gateway, asked: str
) -> None:
    """Also an id with a slash in it, which the SDK sends as one id, the slash encoded:
    the same refusal, with its code, and not the framework's own for a path no route
    has."""
    async with gateway.sdk(gateway.key) as sdk:
        with pytest.raises(NotFoundError) as raised:
            await sdk.models.retrieve(asked)

    assert raised.value.status_code == 404
    assert (raised.value.type, raised.value.code, raised.value.param) == (
        "invalid_request_error",
        "model_not_found",
        None,
    )
    assert raised.value.message == (
        "Error code: 404 - No model is called that. GET /v1/models lists them."
    )
