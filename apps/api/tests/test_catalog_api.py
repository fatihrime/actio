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
                "then": [],
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


async def test_a_model_with_stand_ins_lists_them_and_can_answer_when_one_of_them_can(
    start_app: Any, settings: Settings, migrated: str, lineup: Any
) -> None:
    """Its kind of server, its name there, its window and its longest answer stay its
    own: each stand-in is listed by itself too. Whether a question to it can ask for
    reasoning is the first ready one's to say, the one that would be asked first."""
    assert migrated
    lineup.add("standard", state="unreachable", then=["missing", "plain", "reasons"])
    lineup.add("missing", state="missing")
    lineup.add("plain", context_window=2048, max_output_tokens=256)  # can't reason
    lineup.add("reasons", thinking=True)
    lineup.add("away", state="unreachable", then=["reasons"])
    lineup.add("gone", state="unreachable", then=["missing"])
    lineup.add("lost", state="missing", thinking=True, then=["missing"])
    lineup.add("known", state="missing", thinking=True)
    browser = await start_app(settings, catalog=lineup.catalog())
    await browser.post("/api/session/guest")

    listed = {m["id"]: m for m in (await browser.get("/api/models")).json()["models"]}

    def said(name: str) -> tuple[str, bool, list[str]]:
        return (listed[name]["status"], listed[name]["thinking"], listed[name]["then"])

    assert said("standard") == ("ready", False, ["missing", "plain", "reasons"])
    assert said("away") == ("ready", True, ["reasons"])
    # None of its chain can answer: its own state, and its own word on reasoning, which
    # is none while its server can't be asked, and the file's when the server hasn't
    # got the model. As for a model that has no stand-in.
    assert said("gone") == ("unreachable", False, ["missing"])
    assert said("lost") == ("missing", True, ["missing"])
    assert said("known") == ("missing", True, [])
    assert said("missing") == ("missing", False, [])
    assert said("plain") == ("ready", False, [])
    standard = listed["standard"]
    assert (
        standard["provider"],
        standard["model"],
        standard["context_window"],
        standard["max_output_tokens"],
    ) == ("openai-compatible", "scripted-standard", 4096, 512)


async def test_a_models_window_is_listed_as_its_room(
    start_app: Any, settings: Settings, migrated: str, lineup: Any
) -> None:
    """What its server takes from the installation's account in a minute, when that is
    less than its window, as for a cloud provider's free plan: the page divides a
    conversation's tokens by it. Its longest answer stays its own."""
    assert migrated
    lineup.add("standard", context_window=131_072, tokens_a_minute=8_000)
    lineup.add("wide", context_window=4096, max_output_tokens=1024, tokens_a_minute=250_000)
    lineup.add("local", context_window=8192)
    browser = await start_app(settings, catalog=lineup.catalog())
    await browser.post("/api/session/guest")

    listed = (await browser.get("/api/models")).json()["models"]

    assert {m["id"]: (m["context_window"], m["max_output_tokens"]) for m in listed} == {
        "standard": (8_000, 512),
        "wide": (4096, 1024),
        "local": (8192, 512),
    }


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
