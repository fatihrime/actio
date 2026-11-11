"""`GET /api/models` over HTTP: what can be chosen, and whether it can answer now."""

import json
import tomllib
from typing import Any

import httpx
import httpx2
import pytest

from actio import catalog as catalogs
from actio.catalog import CatalogFile
from actio.core.settings import Settings

pytestmark = pytest.mark.anyio

PROBLEM = "application/problem+json"

TWO_SERVERS = CatalogFile.model_validate(
    tomllib.loads(
        """
default = "standard"

[providers.ollama]
kind = "ollama"

[providers.lab]
kind = "openai-compatible"
url = "http://lab.internal:8000/v1"

[models.standard]
title = "Qwen 3.5 4B"
provider = "ollama"
model = "qwen3.5:4b-q4_K_M"
context_window = 8192
max_output_tokens = 2048

[models.big]
title = "The lab's large model"
provider = "lab"
model = "big-one"
context_window = 32768
max_output_tokens = 4096
"""
    )
)


class Ollama:
    """Stands in for an Ollama server. `pulled` says which models it has; while `up` is
    false it refuses connections."""

    def __init__(self) -> None:
        self.pulled: set[str] = set()
        self.up = True
        self.asked = 0
        self.transport = httpx2.MockTransport(self._answer)

    def _answer(self, request: httpx2.Request) -> httpx2.Response:
        self.asked += 1
        if not self.up:
            raise httpx2.ConnectError("connection refused", request=request)
        name = json.loads(request.content)["model"]
        if name not in self.pulled:
            return httpx2.Response(404, json={"error": f"model '{name}' not found"})
        return httpx2.Response(200, json={"capabilities": ["completion", "tools", "thinking"]})


def lab_serving(*models: str) -> httpx2.MockTransport:
    listed = {"object": "list", "data": [{"id": name, "object": "model"} for name in models]}
    return httpx2.MockTransport(lambda _request: httpx2.Response(200, json=listed))


async def test_the_models_need_a_session(api: httpx.AsyncClient) -> None:
    response = await api.get("/api/models")

    assert response.status_code == 401
    assert response.headers["content-type"] == PROBLEM


async def test_the_models_on_offer_and_the_default(api: httpx.AsyncClient) -> None:
    await api.post("/api/session/guest")

    response = await api.get("/api/models")

    assert response.status_code == 200
    assert response.json() == {
        "default": "standard",
        "models": [
            {
                "id": "standard",
                "title": "Fake",
                "provider": "openai-compatible",
                "model": "fake-chat",
                "context_window": 8192,
                "max_output_tokens": 1024,
                "status": "ready",
                "thinking": True,
            }
        ],
    }


async def test_each_model_says_whether_it_can_answer_now(
    start_app: Any, settings: Settings, migrated: str, clock: Any
) -> None:
    """The page that offers the models can say "run `ollama pull …`" or "start Ollama"
    instead of failing at the first message."""
    assert migrated
    ollama = Ollama()
    transports = {"ollama": ollama.transport, "lab": lab_serving("small-one")}
    async with catalogs.build(
        TWO_SERVERS, ollama_url="http://ollama.internal:11434", transports=transports
    ) as catalog:
        browser = await start_app(settings, catalog=catalog)
        await browser.post("/api/session/guest")

        def states(body: dict[str, Any]) -> dict[str, tuple[str, bool]]:
            return {m["id"]: (m["status"], m["thinking"]) for m in body["models"]}

        not_pulled = states((await browser.get("/api/models")).json())
        ollama.pulled.add("qwen3.5:4b-q4_K_M")
        clock.advance(seconds=15)
        pulled = states((await browser.get("/api/models")).json())
        ollama.up = False
        clock.advance(seconds=15)
        stopped = await browser.get("/api/models")

    assert not_pulled == {"standard": ("missing", False), "big": ("missing", False)}
    assert pulled == {"standard": ("ready", True), "big": ("missing", False)}
    assert stopped.status_code == 200  # a server that is away is news, not an error
    assert states(stopped.json()) == {"standard": ("unreachable", False), "big": ("missing", False)}
    assert [m["id"] for m in stopped.json()["models"]] == ["standard", "big"]  # the file's order


async def test_asking_often_does_not_question_the_providers_each_time(
    start_app: Any, settings: Settings, migrated: str, clock: Any
) -> None:
    assert migrated
    ollama = Ollama()
    transports = {"ollama": ollama.transport, "lab": lab_serving("big-one")}
    async with catalogs.build(
        TWO_SERVERS, ollama_url="http://ollama.internal:11434", transports=transports
    ) as catalog:
        browser = await start_app(settings, catalog=catalog)
        await browser.post("/api/session/guest")

        for _ in range(5):
            await browser.get("/api/models")
        after_five = ollama.asked
        clock.advance(seconds=15)
        await browser.get("/api/models")

    assert after_five == 1
    assert ollama.asked == 2


async def test_the_route_is_in_the_api_description(api: httpx.AsyncClient) -> None:
    paths = (await api.get("/api/openapi.json")).json()["paths"]

    assert set(paths["/api/models"]) == {"get"}
