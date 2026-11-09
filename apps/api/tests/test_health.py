import httpx
import pytest

from actio import __version__

pytestmark = pytest.mark.anyio


async def test_healthz_reports_ok_and_version(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}


async def test_openapi_schema_is_served_under_the_api_prefix(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/openapi.json")

    assert response.status_code == 200
    assert "/api/healthz" in response.json()["paths"]
