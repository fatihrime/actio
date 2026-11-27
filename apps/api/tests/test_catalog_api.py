"""`GET /api/models` over HTTP: what can be chosen, whether it can answer now, and what
the shortest question to it would meet now."""

import asyncio
import json
import logging
import tomllib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

import httpx
import httpx2
import pytest
from valkey.asyncio import Valkey

import actio.app
from actio import catalog as catalogs
from actio.catalog import Budget, CatalogFile
from actio.core.budget import Bucket, Budgets
from actio.core.ids import new_id
from actio.core.resources import Resources, open_resources
from actio.core.settings import Settings
from actio_llm import ConnectionFailedError, Failing, Finish, FinishReason, TextDelta, Usage

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
                # Nothing in its way: the shortest question to it is its own.
                "asked_first": "standard",
                "failing": False,
                "retry_after": None,
                # The tests' plans: a credit a token, read or written.
                "price": {"input": 1_000_000, "output": 1_000_000, "cached_input": 1_000_000},
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


async def test_each_model_is_listed_with_its_price_as_the_plans_state_it(
    start_app: Any, settings: Settings, migrated: str, lineup: Any, plans_with: Any
) -> None:
    """In credits per million tokens: for what it reads, what it writes, and what it
    reads again from its server's cache, which is what reading costs where the plans
    file states nothing of it, as a turn is counted. A cache whose reads the file
    states as costing nothing is listed so, and not as left out. A model that costs
    nothing says nothing but zeros."""
    assert migrated
    lineup.add("standard")
    lineup.add("plain")
    lineup.add("cached")
    lineup.add("local")
    plans = plans_with(
        prices={
            "standard": {"input": 250_000, "output": 1_000_000, "cached_input": 25_000},
            "plain": {"input": 2_000_000, "output": 3_000_000},
            "cached": {"input": 500_000, "output": 1_000_000, "cached_input": 0},
            "local": {"input": 0, "output": 0},
        }
    )
    browser = await start_app(settings, catalog=lineup.catalog(), plans=plans)
    await browser.post("/api/session/guest")

    listed = (await browser.get("/api/models")).json()["models"]

    assert {m["id"]: m["price"] for m in listed} == {
        "standard": {"input": 250_000, "output": 1_000_000, "cached_input": 25_000},
        "plain": {"input": 2_000_000, "output": 3_000_000, "cached_input": 2_000_000},
        "cached": {"input": 500_000, "output": 1_000_000, "cached_input": 0},
        "local": {"input": 0, "output": 0, "cached_input": 0},
    }


async def test_a_model_is_listed_at_its_own_price_while_one_asked_in_its_place_would_answer(
    start_app: Any, settings: Settings, migrated: str, lineup: Any, plans_with: Any
) -> None:
    """A question to it costs its price, whichever model of its chain answers it (ADR
    0028): never the price of the one that would be asked first."""
    assert migrated
    lineup.add("standard", state="unreachable", then=["second"])
    lineup.add("second")
    plans = plans_with(
        prices={
            "standard": {"input": 750_000, "output": 3_000_000, "cached_input": 75_000},
            "second": {"input": 100_000, "output": 400_000},
        }
    )
    browser = await start_app(settings, catalog=lineup.catalog(), plans=plans)
    await browser.post("/api/session/guest")

    listed = {m["id"]: m for m in (await browser.get("/api/models")).json()["models"]}

    assert listed["standard"]["status"] == "ready"  # through "second", which would answer
    assert listed["standard"]["price"] == {
        "input": 750_000,
        "output": 3_000_000,
        "cached_input": 75_000,
    }
    assert listed["second"]["price"] == {
        "input": 100_000,
        "output": 400_000,
        "cached_input": 100_000,
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


# --- What the shortest question to each model would meet now ----------------------------

PRICE = {"input": 1_000_000, "output": 1_000_000, "cached_input": 1_000_000}
"""What the tests' plans make every model cost: a credit a token."""


def meets(response: httpx.Response) -> dict[str, tuple[Any, ...]]:
    """Each model of a list, by its id: its status, whether a question to it can reason,
    the models asked in its place, and what the shortest question to it would meet now:
    the model it is given to first, whether that one keeps failing, and for how many
    seconds it would be refused for now. Each model's price is held here too: the
    tests' own, whatever the rest says."""
    assert response.status_code == 200, response.text
    models = response.json()["models"]
    assert [m["price"] for m in models] == [PRICE] * len(models)
    return {
        m["id"]: (
            m["status"],
            m["thinking"],
            m["then"],
            m["asked_first"],
            m["failing"],
            m["retry_after"],
        )
        for m in models
    }


async def guest_on(start_app: Any, settings: Settings, lineup: Any) -> httpx.AsyncClient:
    """A guest's browser on an app that offers the models of `lineup`."""
    browser: httpx.AsyncClient = await start_app(settings, catalog=lineup.catalog())
    await browser.post("/api/session/guest")
    return browser


def asking(browser: httpx.AsyncClient, text: str = "Hello?") -> asyncio.Task[httpx.Response]:
    """Ask the default model, in a conversation of its own, and go on without waiting:
    the response comes when its stream has ended."""
    message = {"id": str(new_id()), "parts": [{"type": "text", "text": text}]}
    return asyncio.create_task(
        browser.post(f"/api/conversations/{new_id()}/turns", json={"message": message})
    )


async def ask(browser: httpx.AsyncClient, text: str = "Hello?") -> httpx.Response:
    """Ask, and wait for the whole response: for a turn that isn't held."""
    return await asyncio.wait_for(asking(browser, text), PATIENCE_S)


def opened(lineup: Any, name: str, clock: Any) -> None:
    """Open the breaker of the model `name` at the test's time, by five failed tries, as
    if they had come through turns."""
    breaker = lineup.catalog().get(name).breaker
    for _ in range(5):
        breaker.begins(clock.now).ends("failed", clock.now)
    assert breaker.failing(clock.now) == Failing(timedelta(seconds=30))


def told_now(lineup: Any, name: str, clock: Any, seconds: float) -> None:
    """Have the provider of the model `name` say, at the test's time, to wait `seconds`,
    as if a turn's try had heard it."""
    lineup.catalog().get(name).own_wait.told(clock.now + timedelta(seconds=seconds))


def budget_of(lineup: Any, name: str) -> Budget:
    """The budget the lineup gave the model called `name`."""
    budget = lineup.catalog().get(name).budget
    assert budget is not None
    return budget  # type: ignore[no-any-return]


@pytest.fixture
async def budgets(settings: Settings) -> AsyncIterator[Budgets]:
    """The budgets of the app under test, under the keys it counts under: for the test
    to fill, and to read back."""
    async with Valkey.from_url(settings.valkey_url.get_secret_value()) as client:
        yield Budgets(client, prefix=f"{settings.valkey_prefix}:b")


async def spend_all(budgets: Budgets, budget: Budget, clock: Any) -> None:
    """Leave nothing in any bucket of the budget, at the test's time."""
    await budgets.add(*((b, b.size) for b in budget.buckets), now=clock.now)


async def taken(budgets: Budgets, budget: Budget, clock: Any) -> dict[str, int]:
    """What each bucket of the budget holds taken, by its limit in words, at the test's
    time: read back through a look, from how long until it is full again."""
    waits = await budgets.look(*((b, b.size) for b in budget.buckets), now=clock.now)
    return {
        str(b): round(wait / timedelta(milliseconds=1) * b.size / b.period_ms)
        for b, wait in zip(budget.buckets, waits, strict=True)
    }


@pytest.fixture
async def budgets_away(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """The app's budgets on an address where nothing listens, and the rest of its Valkey
    the test's own."""
    async with Valkey.from_url("valkey://127.0.0.1:1/0") as nowhere:

        @asynccontextmanager
        async def opening(settings: Settings) -> AsyncIterator[Resources]:
            async with open_resources(settings) as resources:
                yield replace(resources, budgets=Budgets(nowhere))

        monkeypatch.setattr(actio.app, "open_resources", opening)
        yield


@pytest.fixture
def looks(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """The keys of the buckets of each look at the budgets from now on, in the order
    looked: one question to Valkey for each look that has any."""
    noted: list[list[str]] = []
    look = Budgets.look

    async def noting(
        budgets: Budgets, *amounts: tuple[Bucket, int], now: datetime
    ) -> list[timedelta]:
        noted.append([budgets.key(bucket) for bucket, _ in amounts])
        return await look(budgets, *amounts, now=now)

    monkeypatch.setattr(Budgets, "look", noting)
    return noted


def from_now(caplog: pytest.LogCaptureFixture) -> None:
    """Have the log keep what Actio says from here on, at every level, and nothing of
    what it said before: for a test that holds that a read of the list says nothing."""
    caplog.set_level(logging.DEBUG, logger="actio")
    caplog.clear()


def said_by_actio(caplog: pytest.LogCaptureFixture) -> list[str]:
    """What Actio's own code said in the log, at any level."""
    return [
        f"{record.levelname} {record.name}: {record.getMessage()}"
        for record in caplog.records
        if record.name == "actio" or record.name.startswith("actio.")
    ]


async def test_a_model_that_cant_answer_itself_is_given_the_question_by_the_first_that_can(
    start_app: Any, settings: Settings, migrated: str, lineup: Any
) -> None:
    """Its status is `ready` through that model, as it was: the list now says which one
    it is. None of a chain ready: nobody is given it, and nothing is refused for now,
    whatever its own status says."""
    assert migrated
    lineup.add("standard", state="unreachable", then=["missing", "plain", "reasons"])
    lineup.add("missing", state="missing")
    lineup.add("plain")
    lineup.add("reasons", thinking=True)
    lineup.add("gone", state="unreachable", then=["missing"])
    lineup.add("known", state="missing", thinking=True)
    browser = await guest_on(start_app, settings, lineup)

    [response] = await lists(browser)

    assert meets(response) == {
        "standard": ("ready", False, ["missing", "plain", "reasons"], "plain", False, None),
        "missing": ("missing", False, [], None, False, None),
        "plain": ("ready", False, [], "plain", False, None),
        "reasons": ("ready", True, [], "reasons", False, None),
        "gone": ("unreachable", False, ["missing"], None, False, None),
        "known": ("missing", True, [], None, False, None),
    }


async def test_a_model_whose_budget_is_spent_is_given_after_its_stand_in_and_alone_is_refused(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    budgets: Budgets,
    clock: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Its one request a minute was taken at this very moment: its stand-in, which has
    budget, is given the shortest question first, and a model alone is refused it for a
    minute. The log says nothing of it."""
    assert migrated
    lineup.add("standard", then=["second"], budget={"requests_a_minute": 1})
    lineup.add("second", budget={"requests_a_day": 10})
    lineup.add("alone", budget={"requests_a_minute": 1})
    browser = await guest_on(start_app, settings, lineup)
    await spend_all(budgets, budget_of(lineup, "standard"), clock)
    await spend_all(budgets, budget_of(lineup, "alone"), clock)
    from_now(caplog)

    [response] = await lists(browser)

    assert meets(response) == {
        "standard": ("ready", False, ["second"], "second", False, None),
        "second": ("ready", False, [], "second", False, None),
        "alone": ("ready", False, [], None, False, 60),
    }
    assert said_by_actio(caplog) == []


async def test_the_wait_said_is_the_one_a_question_asked_in_the_same_moment_is_refused_for(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    budgets: Budgets,
    clock: Any,
) -> None:
    """By a limit of requests, of which a question of any length takes one: 17.25 s after
    the minute's one request was taken, 43 s, rounded up, in the list and in the
    refusal of a question."""
    assert migrated
    model = lineup.add("standard", TextDelta("Never."), DONE, budget={"requests_a_minute": 1})
    browser = await guest_on(start_app, settings, lineup)
    await spend_all(budgets, budget_of(lineup, "standard"), clock)
    clock.advance(seconds=17.25)

    [listed] = await lists(browser)
    refused = await ask(browser)

    assert meets(listed)["standard"][3:] == (None, False, 43)
    assert refused.status_code == 429, refused.text
    assert (refused.json()["retry_after"], refused.headers["retry-after"]) == (43, "43")
    assert model.requests == []


async def test_a_bucket_of_tokens_with_one_left_has_room_for_the_shortest_question_alone(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    budgets: Budgets,
    clock: Any,
) -> None:
    """The list says what the shortest question would meet: nothing in its way. A real
    question, which takes more than a token, is refused for now in the same moment."""
    assert migrated
    model = lineup.add("standard", TextDelta("Never."), DONE, budget={"tokens_a_minute": 1_000})
    browser = await guest_on(start_app, settings, lineup)
    [tokens] = budget_of(lineup, "standard").buckets
    await budgets.add((tokens, tokens.size - 1), now=clock.now)

    [listed] = await lists(browser)
    refused = await ask(browser)

    assert meets(listed)["standard"][3:] == ("standard", False, None)
    assert refused.status_code == 429, refused.text
    assert model.requests == []


async def test_a_model_whose_provider_said_to_wait_is_given_after_its_stand_in_until_it_is_over(
    start_app: Any, settings: Settings, migrated: str, lineup: Any, clock: Any
) -> None:
    """None of them has a budget: the provider's word is enough. A model alone is
    refused the shortest question until then. Once the clock is past it, nothing is
    said."""
    assert migrated
    lineup.add("standard", then=["second"])
    lineup.add("second")
    lineup.add("alone")
    browser = await guest_on(start_app, settings, lineup)
    told_now(lineup, "standard", clock, 49)
    told_now(lineup, "alone", clock, 49)

    [during] = await lists(browser)
    clock.advance(seconds=49)
    [after] = await lists(browser)

    assert meets(during) == {
        "standard": ("ready", False, ["second"], "second", False, None),
        "second": ("ready", False, [], "second", False, None),
        "alone": ("ready", False, [], None, False, 49),
    }
    assert meets(after) == {
        "standard": ("ready", False, ["second"], "standard", False, None),
        "second": ("ready", False, [], "second", False, None),
        "alone": ("ready", False, [], "alone", False, None),
    }


async def test_a_model_that_keeps_failing_is_given_after_a_stand_in_that_doesnt_or_said_to_fail(
    start_app: Any, settings: Settings, migrated: str, lineup: Any, clock: Any
) -> None:
    """With a stand-in that isn't failing, the stand-in first. Alone, or with every
    model of its chain failing, the first of the chain all the same, and it keeps
    failing. Once the breakers' wait is over by the test's clock, nothing is said.

    Whether a question can reason stays the first ready model's to say, whichever is
    given the question first: `standard` can and its stand-in can't, `plain` can't and
    its stand-in can, and neither changes with who is asked first."""
    assert migrated
    lineup.add("standard", thinking=True, then=["second"])
    lineup.add("second")
    lineup.add("alone")
    lineup.add("both", then=["other"])
    lineup.add("other")
    lineup.add("plain", then=["reasons"])
    lineup.add("reasons", thinking=True)
    for name in ("standard", "alone", "both", "other", "plain"):
        opened(lineup, name, clock)
    browser = await guest_on(start_app, settings, lineup)

    [during] = await lists(browser)
    clock.advance(seconds=30)
    [after] = await lists(browser)

    assert meets(during) == {
        "standard": ("ready", True, ["second"], "second", False, None),
        "second": ("ready", False, [], "second", False, None),
        "alone": ("ready", False, [], "alone", True, None),
        "both": ("ready", False, ["other"], "both", True, None),
        "other": ("ready", False, [], "other", True, None),
        "plain": ("ready", False, ["reasons"], "reasons", False, None),
        "reasons": ("ready", True, [], "reasons", False, None),
    }
    assert meets(after) == {
        "standard": ("ready", True, ["second"], "standard", False, None),
        "second": ("ready", False, [], "second", False, None),
        "alone": ("ready", False, [], "alone", False, None),
        "both": ("ready", False, ["other"], "both", False, None),
        "other": ("ready", False, [], "other", False, None),
        "plain": ("ready", False, ["reasons"], "plain", False, None),
        "reasons": ("ready", True, [], "reasons", False, None),
    }


async def test_while_one_request_tries_a_model_that_kept_failing_the_list_says_it_keeps_failing(
    start_app: Any, settings: Settings, migrated: str, lineup: Any, clock: Any
) -> None:
    """Its breaker's wait is over: before the question that tries it, nothing is said;
    while that question is held, the model keeps failing for everyone else; once it has
    answered, nothing again."""
    assert migrated
    hold = asyncio.Event()
    model = lineup.add("standard", hold, TextDelta("Mine."), DONE)
    opened(lineup, "standard", clock)
    browser = await guest_on(start_app, settings, lineup)
    clock.advance(seconds=30)

    [before] = await lists(browser)
    trying = asking(browser)
    await asyncio.wait_for(model.waiting.wait(), PATIENCE_S)
    [during] = await lists(browser)
    hold.set()
    answered = await asyncio.wait_for(trying, PATIENCE_S)
    [after] = await lists(browser)

    assert meets(before)["standard"][3:] == ("standard", False, None)
    assert meets(during)["standard"][3:] == ("standard", True, None)
    assert turn_of(answered)["answered_by"] == "standard"
    assert meets(after)["standard"][3:] == ("standard", False, None)


async def test_reading_the_list_tries_no_model_and_takes_nothing_of_a_budget(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    budgets: Budgets,
    clock: Any,
) -> None:
    """After a breaker's wait is over, three reads of the list leave the next question
    the one that tries its model: asked of it in its place, which answers. What its
    budget holds taken reads back as it was before the reads."""
    assert migrated
    model = lineup.add(
        "standard",
        TextDelta("Mine."),
        DONE,
        then=["second"],
        budget={"requests_a_minute": 10, "tokens_a_minute": 1_000},
    )
    stand_in = lineup.add("second", TextDelta("Never."), DONE)
    opened(lineup, "standard", clock)
    breaker = lineup.catalog().get("standard").breaker
    budget = budget_of(lineup, "standard")
    browser = await guest_on(start_app, settings, lineup)
    clock.advance(seconds=30)
    requests, tokens = budget.buckets
    await budgets.add((requests, 3), (tokens, 300), now=clock.now)
    before = await taken(budgets, budget, clock)

    read = [meets(response)["standard"][3:] for response in await lists(browser, at_once=3)]
    after = await taken(budgets, budget, clock)
    trying = breaker.trying
    response = await ask(browser)

    assert read == [("standard", False, None)] * 3
    assert before == after == {"requests a minute": 3, "tokens a minute": 300}
    assert trying == 0
    assert turn_of(response)["answered_by"] == "standard"
    assert (len(model.requests), stand_in.requests) == (1, [])


async def test_one_read_of_the_list_asks_valkey_once_for_every_chain_and_each_model_once(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    looks: list[list[str]],
) -> None:
    """Five chains, three of whose models have a budget and are ready, one in three
    chains and one in two: one look, at the buckets of each of the three once. A model
    that isn't ready isn't looked at."""
    assert migrated
    lineup.add("standard", then=["second"], budget={"requests_a_minute": 1})
    lineup.add("second", budget={"requests_a_day": 10})
    lineup.add("third", then=["second", "standard"], budget={"tokens_a_minute": 1_000})
    lineup.add("plain", then=["away"])
    lineup.add("away", state="unreachable", budget={"requests_a_day": 1})
    browser = await guest_on(start_app, settings, lineup)

    [response] = await lists(browser)

    assert response.status_code == 200, response.text
    under = f"{settings.valkey_prefix}:b"
    assert [sorted(keys) for keys in looks if keys] == [
        [
            f"{under}:requests-a-day:scripted:scripted-second",
            f"{under}:requests-a-minute:scripted:scripted-standard",
            f"{under}:tokens-a-minute:scripted:scripted-third",
        ]
    ]


async def test_a_read_of_the_list_asks_valkey_nothing_when_no_ready_model_has_a_budget(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    looks: list[list[str]],
) -> None:
    """As with the models file that is shipped, which states no budget: the one model
    that has one isn't ready."""
    assert migrated
    lineup.add("standard", then=["away"])
    lineup.add("away", state="unreachable", budget={"requests_a_day": 1})
    browser = await guest_on(start_app, settings, lineup)

    [response] = await lists(browser)

    assert meets(response)["standard"][3:] == ("standard", False, None)
    assert [keys for keys in looks if keys] == []


@pytest.mark.usefixtures("budgets_away")
async def test_with_valkey_away_no_budget_is_said_spent_and_the_rest_is_said_as_ever(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    budgets: Budgets,
    clock: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The look can't be had: the list answers as if nothing were spent, and says a
    provider's own wait and a breaker all the same. Under the app's keys in the test's
    own Valkey, the budget is spent, and a look there would refuse the question. The log
    says nothing: a page reads the list every few seconds."""
    assert migrated
    lineup.add("standard", budget={"requests_a_minute": 1})
    lineup.add("told")
    lineup.add("failing")
    browser = await guest_on(start_app, settings, lineup)
    await spend_all(budgets, budget_of(lineup, "standard"), clock)
    told_now(lineup, "told", clock, 49)
    opened(lineup, "failing", clock)
    from_now(caplog)

    [response] = await lists(browser)

    assert meets(response) == {
        "standard": ("ready", False, [], "standard", False, None),
        "told": ("ready", False, [], None, False, 49),
        "failing": ("ready", False, [], "failing", True, None),
    }
    assert said_by_actio(caplog) == []


async def test_two_names_of_one_model_on_one_provider_say_the_same_of_its_breaker_and_budget(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    budgets: Budgets,
    clock: Any,
) -> None:
    """The breaker opened, and then the budget spent, through the first name alone."""
    assert migrated
    lineup.add("standard", budget={"requests_a_minute": 1}, on_server="shared")
    lineup.add("again", budget={"requests_a_minute": 1}, on_server="shared")
    browser = await guest_on(start_app, settings, lineup)
    opened(lineup, "standard", clock)

    [failing] = await lists(browser)
    await spend_all(budgets, budget_of(lineup, "standard"), clock)
    [spent] = await lists(browser)

    assert meets(failing) == {
        "standard": ("ready", False, [], "standard", True, None),
        "again": ("ready", False, [], "again", True, None),
    }
    assert meets(spent) == {
        "standard": ("ready", False, [], None, False, 60),
        "again": ("ready", False, [], None, False, 60),
    }


async def test_the_route_is_in_the_api_description(api: httpx.AsyncClient) -> None:
    paths = (await api.get("/api/openapi.json")).json()["paths"]

    assert set(paths["/api/models"]) == {"get"}


async def test_what_the_shortest_question_would_meet_is_described_member_by_member(
    api: httpx.AsyncClient,
) -> None:
    """For a reader of the API, in its description, which the web app's types are
    generated from (test_api_description.py holds the recorded file to it)."""
    schemas = (await api.get("/api/openapi.json")).json()["components"]["schemas"]
    model = schemas["ModelOut"]
    members = model["properties"]

    assert {"asked_first", "failing", "retry_after"} <= set(model["required"])
    assert members["asked_first"]["anyOf"] == [{"type": "string"}, {"type": "null"}]
    assert members["failing"]["type"] == "boolean"
    assert members["retry_after"]["anyOf"] == [{"type": "integer"}, {"type": "null"}]
    for name in ("asked_first", "failing", "retry_after"):
        assert members[name]["description"], name
    assert "shortest question" in members["asked_first"]["description"]
    assert "shortest question" in members["retry_after"]["description"]


async def test_a_models_price_and_each_of_its_numbers_are_described(
    api: httpx.AsyncClient,
) -> None:
    """For a reader of the API, in its description, which the web app's types are
    generated from (test_api_description.py holds the recorded file to it)."""
    schemas = (await api.get("/api/openapi.json")).json()["components"]["schemas"]
    model, price = schemas["ModelOut"], schemas["PriceOut"]

    assert "price" in model["required"]
    assert model["properties"]["price"]["$ref"] == "#/components/schemas/PriceOut"
    assert model["properties"]["price"]["description"]
    assert "credits per million tokens" in price["description"]
    assert set(price["required"]) == {"input", "output", "cached_input"}
    assert set(price["properties"]) == {"input", "output", "cached_input"}
    for name, member in price["properties"].items():
        assert member["type"] == "integer", name
        assert member["description"], name
