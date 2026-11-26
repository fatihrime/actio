"""`GET /api/models` over HTTP: what can be chosen, and whether it can answer now."""

import asyncio
import json
import tomllib
from typing import Any

import httpx
import httpx2
import pytest

from actio import catalog as catalogs
from actio.catalog import CatalogFile
from actio.core.ids import new_id
from actio.core.settings import Settings
from actio_llm import ConnectionFailedError, Finish, FinishReason, TextDelta, Usage

pytestmark = pytest.mark.anyio

PROBLEM = "application/problem+json"
DONE = Finish(FinishReason.STOP, Usage(input_tokens=5, output_tokens=4))
PATIENCE_S = 5.0
"""How long a test waits for the app to get somewhere. It takes milliseconds; this only
keeps an app that waits for an answer held for ever from hanging the run."""

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


def listed(response: httpx.Response) -> dict[str, tuple[str, bool]]:
    """Each model of a list, by its id, with its status and whether it can reason."""
    assert response.status_code == 200, response.text
    return {m["id"]: (m["status"], m["thinking"]) for m in response.json()["models"]}


async def lists(browser: httpx.AsyncClient, at_once: int = 1) -> list[httpx.Response]:
    """The list of models, asked for `at_once` times at the same moment, each within
    a time limit that fails a list that waits for an answer held."""
    async with asyncio.timeout(PATIENCE_S):
        return list(await asyncio.gather(*(browser.get("/api/models") for _ in range(at_once))))


def listing(browser: httpx.AsyncClient) -> asyncio.Task[httpx.Response]:
    """Ask for the list of models, and go on without waiting for it."""
    return asyncio.create_task(browser.get("/api/models"))


async def settled() -> None:
    """Let every task that can go on do so, a few steps."""
    for _ in range(5):
        await asyncio.sleep(0)


async def test_a_model_whose_server_went_away_is_listed_unreachable_by_asking_it_again(
    start_app: Any, settings: Settings, migrated: str, lineup: Any, clock: Any
) -> None:
    """20 s after it was last seen ready, its server's answer held: two lists at once
    wait for it, through one ask, and say what it says now. Nobody waits for the list,
    and the page shows a notice by it."""
    assert migrated
    lineup.add("standard", thinking=True)
    browser = await start_app(settings, catalog=lineup.catalog())
    await browser.post("/api/session/guest")
    [before] = await lists(browser)
    lineup.becomes("standard", "unreachable")
    clock.advance(seconds=20)
    held = lineup.hold("standard")

    async with asyncio.timeout(PATIENCE_S):
        at_once = [listing(browser), listing(browser)]
        await held.asked.wait()
        await settled()
        done_while_held = [each.done() for each in at_once]
        held.answers.set()
        after = await asyncio.gather(*at_once)

    assert listed(before) == {"standard": ("ready", True)}
    assert done_while_held == [False, False]
    assert [listed(each) for each in after] == [{"standard": ("unreachable", False)}] * 2
    assert lineup.asked == ["standard", "standard"]


@pytest.mark.parametrize("state", ["unreachable", "missing"])
async def test_a_model_last_seen_not_ready_is_asked_again_before_it_is_listed_so(
    state: str, start_app: Any, settings: Settings, migrated: str, lineup: Any, clock: Any
) -> None:
    """`missing` and `unreachable` are never said from an old status: a lone model, back
    now, is listed `ready` by the answer the list waited for, two lists at once sharing
    one ask."""
    assert migrated
    lineup.add("standard", state=state, thinking=True)
    browser = await start_app(settings, catalog=lineup.catalog())
    await browser.post("/api/session/guest")
    [before] = await lists(browser)
    lineup.becomes("standard", "ready")
    clock.advance(seconds=20)

    after = await lists(browser, at_once=2)

    assert listed(before)["standard"][0] == state
    assert [listed(each) for each in after] == [{"standard": ("ready", True)}] * 2
    assert lineup.asked == ["standard", "standard"]


async def test_a_model_listed_through_its_chain_waits_for_its_own_server_and_its_stand_ins(
    start_app: Any, settings: Settings, migrated: str, lineup: Any, clock: Any
) -> None:
    """It was last seen unreachable, and its stand-in ready, 20 s ago. Both servers'
    answers held, the list waits for both, and says what they say now: it is back, and
    answers first, without reasoning. The stand-in, in both chains, is asked once."""
    assert migrated
    lineup.add("standard", state="unreachable", then=["second"])
    lineup.add("second", thinking=True)
    browser = await start_app(settings, catalog=lineup.catalog())
    await browser.post("/api/session/guest")
    [before] = await lists(browser)
    clock.advance(seconds=20)
    lineup.becomes("standard", "ready")
    held = [lineup.hold("standard"), lineup.hold("second")]

    async with asyncio.timeout(PATIENCE_S):
        during = listing(browser)
        for each in held:
            await each.asked.wait()
        await settled()
        done_while_held = during.done()
        for each in held:
            each.answers.set()
        after = await during

    assert listed(before) == {"standard": ("ready", True), "second": ("ready", True)}
    assert not done_while_held
    assert listed(after) == {"standard": ("ready", False), "second": ("ready", True)}
    assert lineup.asked == ["standard", "second", "standard", "second"]


def turn_of(response: httpx.Response) -> dict[str, Any]:
    """A turn's record, as the last metadata of its stream says it."""
    events = [event for event in response.text.split("\n\n") if event.startswith("data: {")]
    chunks = [json.loads(event.removeprefix("data: ")) for event in events]
    ending = [c for c in chunks if c["type"] in ("finish", "message-metadata")]
    return ending[-1]["messageMetadata"]["turn"]  # type: ignore[no-any-return]


async def test_the_list_says_unreachable_where_a_question_in_that_moment_starts_its_turn(
    start_app: Any, settings: Settings, migrated: str, lineup: Any, clock: Any
) -> None:
    """20 s after the model was last seen ready, its server gone since, and its answer
    held: a question asked in that moment starts its turn on the old `ready`, which
    fails as a turn does whose model's server can't be reached, while the list waits.
    The two share one ask, and the list says `unreachable`."""
    assert migrated
    model = lineup.add("standard", TextDelta("Hi"), DONE)
    browser = await start_app(settings, catalog=lineup.catalog())
    await browser.post("/api/session/guest")
    [before] = await lists(browser)
    model.script = [ConnectionFailedError("can't reach the model's server")]
    lineup.becomes("standard", "unreachable")
    clock.advance(seconds=20)
    held = lineup.hold("standard")
    message = {"id": str(new_id()), "parts": [{"type": "text", "text": "Hello?"}]}

    async with asyncio.timeout(PATIENCE_S):
        listed_then = listing(browser)
        asked = await browser.post(
            f"/api/conversations/{new_id()}/turns", json={"message": message}
        )
        await held.asked.wait()
        await settled()
        done_while_held = listed_then.done()
        held.answers.set()
        after = await listed_then

    assert listed(before) == {"standard": ("ready", False)}
    assert asked.status_code == 200, asked.text
    assert (turn_of(asked)["status"], turn_of(asked)["error"]) == ("failed", "model_unavailable")
    assert not done_while_held
    assert listed(after) == {"standard": ("unreachable", False)}
    assert lineup.asked == ["standard", "standard"]


class HeldLab(httpx2.AsyncBaseTransport):
    """A server that speaks OpenAI's API, and has `big-one`: it answers at once, until
    a test holds its answers (`held`). Says whether its client had been closed by the
    time an answer it held was given up on."""

    def __init__(self) -> None:
        self.held: asyncio.Event | None = None
        self.holding = asyncio.Event()
        """Set once an answer is held."""
        self.given_up = 0
        self.closed = False
        self.closed_before_given_up: bool | None = None
        """`None` while no answer it held has been given up on."""

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/v1/models"
        if self.held is not None:
            self.holding.set()
            try:
                await self.held.wait()
            except asyncio.CancelledError:
                self.given_up += 1
                self.closed_before_given_up = self.closed
                raise
        return httpx2.Response(200, json={"object": "list", "data": [{"id": "big-one"}]})

    async def aclose(self) -> None:
        self.closed = True


async def test_an_ask_under_way_is_given_up_on_when_the_app_shuts_down(
    open_app: Any, settings: Settings, migrated: str, clock: Any
) -> None:
    """Also for a catalog the app was handed, which it doesn't close: the ask left behind
    a turn's start, whose server never answers, is cancelled and waited for while the
    server's client is still open, and nothing is left pending once the app has shut
    down. With a time limit: an app that waited for the ask would never shut down."""
    assert migrated
    ollama, lab = Ollama(), HeldLab()
    ollama.pulled.add("qwen3.5:4b-q4_K_M")
    transports = {"ollama": ollama.transport, "lab": lab}
    async with (
        asyncio.timeout(PATIENCE_S),
        catalogs.build(
            TWO_SERVERS, ollama_url="http://ollama.internal:11434", transports=transports
        ) as catalog,
    ):
        async with open_app(settings, catalog=catalog) as browser:
            await browser.post("/api/session/guest")
            [before] = await lists(browser)
            clock.advance(seconds=20)
            lab.held = asyncio.Event()  # nothing sets it: only a cancellation ends the ask
            # As a turn's start asks: the old `ready` at once, the server asked behind.
            during = await catalog.chain_as_known("big", now=clock.now)
            await lab.holding.wait()
        given_up_on_with_the_app = (lab.given_up, lab.closed_before_given_up)
        left = [task for task in asyncio.all_tasks() if task.get_name().startswith("status-")]

    assert listed(before) == {"standard": ("ready", True), "big": ("ready", False)}
    assert during.status.state == "ready"
    assert given_up_on_with_the_app == (1, False)
    assert left == []


async def test_the_route_is_in_the_api_description(api: httpx.AsyncClient) -> None:
    paths = (await api.get("/api/openapi.json")).json()["paths"]

    assert set(paths["/api/models"]) == {"get"}
