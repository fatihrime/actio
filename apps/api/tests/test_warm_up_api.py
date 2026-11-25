"""`POST /api/models/{model_id}/warm-up` over HTTP: a model loaded into its server's
memory before the first question to it (ADR 0026).

A model that can be loaded is either the Ollama adapter itself, talking to a server
written here (`FakeOllama`), or a scripted model whose server can load it too (`Loads`),
whose loads a test can hold, or make fail. A load runs apart from the request, which
answers at once: a test that wants to see what became of it waits for that, with a
time limit, and so does every request that could wait for ever on a broken app.
"""

import asyncio
import json
import logging
from collections.abc import AsyncGenerator, Callable
from typing import Any

import httpx
import httpx2
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from actio import catalog as catalogs
from actio.catalog import Catalog, CatalogFile, Gate, OfferedModel
from actio.core.ids import new_id
from actio.core.rate_limit import Rate
from actio.core.settings import Settings
from actio.usage.models import UsageEntry
from actio_llm import (
    ChatRequest,
    Finish,
    FinishReason,
    ProviderUnavailableError,
    StreamEvent,
    TextDelta,
    Usage,
)

pytestmark = pytest.mark.anyio

PROBLEM = "application/problem+json"
DONE = Finish(FinishReason.STOP, Usage(input_tokens=5, output_tokens=4))
PATIENCE_S = 5.0
"""How long a test waits for the app to get somewhere. It takes milliseconds; this only
keeps a broken app from hanging the test run."""

ON_TWO_SERVERS = CatalogFile.model_validate(
    {
        "default": "standard",
        "providers": {
            "ollama": {"kind": "ollama"},
            "lab": {"kind": "openai-compatible", "url": "http://lab.internal:8000/v1"},
        },
        "models": {
            "standard": {
                "title": "Qwen 3.5 4B",
                "provider": "ollama",
                "model": "qwen3.5:4b-q4_K_M",
                "context_window": 8192,
                "max_output_tokens": 2048,
            },
            "big": {
                "title": "The lab's large model",
                "provider": "lab",
                "model": "big-one",
                "context_window": 32768,
                "max_output_tokens": 4096,
            },
        },
    }
)


class FakeOllama:
    """An Ollama server that has every model it is asked about, and loads one when it is
    asked to, as Ollama 0.40.16 answers that. Remembers each request to `/api/chat`."""

    def __init__(self) -> None:
        self.chats: list[dict[str, Any]] = []
        self.transport = httpx2.MockTransport(self._answer)

    def _answer(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/api/version":
            return httpx2.Response(200, json={"version": "0.40.16"})
        if request.url.path == "/api/show":
            return httpx2.Response(200, json={"capabilities": ["completion", "thinking"]})
        body = json.loads(request.content)
        self.chats.append(body)
        loaded = {"role": "assistant", "content": ""}
        return httpx2.Response(
            200,
            json={"model": body["model"], "message": loaded, "done": True, "done_reason": "load"},
        )


class Lab:
    """A server that speaks OpenAI's API, and has `big-one`. Remembers every request."""

    def __init__(self) -> None:
        self.asked: list[str] = []
        self.transport = httpx2.MockTransport(self._answer)

    def _answer(self, request: httpx2.Request) -> httpx2.Response:
        self.asked.append(f"{request.method} {request.url.path}")
        return httpx2.Response(200, json={"object": "list", "data": [{"id": "big-one"}]})


class Loads:
    """A model whose server can be asked to load it. It answers as `answers`, a
    `ScriptedModel`, does, and remembers each load it was asked for. A load waits for
    `hold` while that isn't set, then fails with `fails`, if given."""

    def __init__(
        self, answers: Any, *, hold: asyncio.Event | None = None, fails: Exception | None = None
    ) -> None:
        self.answers = answers
        self.hold = hold
        self.fails = fails
        self.loads: list[tuple[str, int | None]] = []
        """Each load's model and context window, in the order asked."""
        self.loading = asyncio.Event()
        """Set while a load waits for `hold`."""
        self.ended = 0
        """How many loads have ended, however they ended."""
        self.cancelled = 0
        """How many of them were cancelled."""

    def stream(self, request: ChatRequest) -> AsyncGenerator[StreamEvent]:
        return self.answers.stream(request)  # type: ignore[no-any-return]

    async def load(self, model: str, *, context_window: int | None = None) -> None:
        self.loads.append((model, context_window))
        try:
            if self.hold is not None:
                self.loading.set()
                await self.hold.wait()
            if self.fails is not None:
                raise self.fails
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        finally:
            self.loading.clear()
            self.ended += 1


def offering(model: Any) -> Catalog:
    """A catalog that offers `model` as `standard`, on a server that takes every answer
    at once. A test of the gate offers its model through `lineup` instead."""
    offered = OfferedModel(
        name="standard",
        title="Scripted",
        kind="ollama",
        model="scripted-1",
        context_window=4096,
        max_output_tokens=512,
        chat=model,
    )
    return Catalog({"standard": offered}, default="standard", thinking={"standard": False})


async def guest_on(start_app: Any, settings: Settings, catalog: Catalog) -> httpx.AsyncClient:
    """A guest's browser on an app that offers the models of `catalog`."""
    browser: httpx.AsyncClient = await start_app(settings, catalog=catalog)
    await browser.post("/api/session/guest")
    return browser


async def warm_up(browser: httpx.AsyncClient, model: str = "standard") -> httpx.Response:
    return await asyncio.wait_for(browser.post(f"/api/models/{model}/warm-up"), PATIENCE_S)


def asking(browser: httpx.AsyncClient, text: str) -> asyncio.Task[httpx.Response]:
    """Ask in a new conversation, and go on without waiting: the response comes when
    its stream has ended."""
    message = {"id": str(new_id()), "parts": [{"type": "text", "text": text}]}
    return asyncio.create_task(
        browser.post(f"/api/conversations/{new_id()}/turns", json={"message": message})
    )


async def until(condition: Callable[[], bool]) -> None:
    """Wait until `condition` holds: the app gets there in a moment."""
    async with asyncio.timeout(PATIENCE_S):
        while True:
            if condition():
                return
            await asyncio.sleep(0.005)


# --- What is loaded, and when ---------------------------------------------------------


async def test_a_model_on_ollama_is_loaded_with_the_window_a_turn_asks_it_with(
    start_app: Any, settings: Settings, migrated: str
) -> None:
    """Through the Ollama adapter, as the stack does it: a chat request with no
    messages, with the window a turn asks the model with."""
    assert migrated
    ollama, lab = FakeOllama(), Lab()
    transports = {"ollama": ollama.transport, "lab": lab.transport}
    async with catalogs.build(
        ON_TWO_SERVERS, ollama_url="http://ollama.internal:11434", transports=transports
    ) as catalog:
        browser = await guest_on(start_app, settings, catalog)

        response = await warm_up(browser)
        await until(lambda: len(ollama.chats) == 1)

    assert response.status_code == 202
    assert response.content == b""
    [loaded] = ollama.chats
    assert (loaded["model"], loaded["messages"]) == ("qwen3.5:4b-q4_K_M", [])
    assert loaded["options"]["num_ctx"] == 8192  # the model's window in the file


async def test_a_model_whose_server_cant_load_ahead_has_nothing_to_load(
    start_app: Any, settings: Settings, migrated: str
) -> None:
    """A server that speaks OpenAI's API has no call for it. It is asked whether it has
    the model, as for a turn, and nothing more."""
    assert migrated
    ollama, lab = FakeOllama(), Lab()
    transports = {"ollama": ollama.transport, "lab": lab.transport}
    async with catalogs.build(
        ON_TWO_SERVERS, ollama_url="http://ollama.internal:11434", transports=transports
    ) as catalog:
        browser = await guest_on(start_app, settings, catalog)

        first, again = await warm_up(browser, "big"), await warm_up(browser, "big")

    assert (first.status_code, again.status_code) == (204, 204)
    assert first.content == b""
    assert lab.asked == ["GET /v1/models"]  # once: the status is reused
    assert ollama.chats == []


async def test_nothing_is_loaded_while_the_server_writes_as_many_answers_as_it_takes(
    start_app: Any, settings: Settings, migrated: str, offer: Any, lineup: Any
) -> None:
    """The model that writes is in memory already, and a load of another one waited
    for the answer to end, and then had its model unloaded (measured, ADR 0026)."""
    assert migrated
    hold = asyncio.Event()
    answers, _ = offer(TextDelta("Half an "), hold, TextDelta("answer."), DONE)
    model = Loads(answers)
    lineup.offer("standard", model, gate=Gate(1))
    browser = await guest_on(start_app, settings, lineup.catalog())
    asked = asking(browser, "A long question")
    await asyncio.wait_for(answers.waiting.wait(), PATIENCE_S)

    busy = await warm_up(browser)
    loads_while_busy = list(model.loads)
    hold.set()
    answered = await asyncio.wait_for(asked, PATIENCE_S)
    free = await warm_up(browser)  # the control: once it has room, it is loaded
    await until(lambda: model.ended == 1)

    assert busy.status_code == 204
    assert loads_while_busy == []
    assert answered.status_code == 200
    assert free.status_code == 202
    assert model.loads == [("scripted-standard", 4096)]


async def test_a_load_under_way_is_said_also_while_the_server_writes_an_answer(
    start_app: Any, settings: Settings, migrated: str, offer: Any, lineup: Any
) -> None:
    """A question passed the gate while its model loads. Asked again then, the load is
    under way all the same: 202, and no second one."""
    assert migrated
    loaded, hold = asyncio.Event(), asyncio.Event()
    answers, _ = offer(TextDelta("Half an "), hold, TextDelta("answer."), DONE)
    model = Loads(answers, hold=loaded)
    lineup.offer("standard", model, gate=Gate(1))
    browser = await guest_on(start_app, settings, lineup.catalog())
    started = await warm_up(browser)
    await asyncio.wait_for(model.loading.wait(), PATIENCE_S)
    asked = asking(browser, "A long question")
    await asyncio.wait_for(answers.waiting.wait(), PATIENCE_S)

    meanwhile = await warm_up(browser)
    loads_meanwhile = len(model.loads)
    loaded.set()
    hold.set()
    answered = await asyncio.wait_for(asked, PATIENCE_S)
    await until(lambda: model.ended == 1)

    assert (started.status_code, meanwhile.status_code) == (202, 202)
    assert loads_meanwhile == 1
    assert answered.status_code == 200
    assert model.loads == [("scripted-standard", 4096)]


async def test_a_question_asked_while_its_model_loads_is_asked_at_once(
    start_app: Any, settings: Settings, migrated: str, offer: Any, lineup: Any
) -> None:
    """A load takes no place at the gate: the server takes the question, and starts it
    once its load is over. Measured on Ollama 0.40.16, for a question to the model that
    is being loaded (ADR 0026)."""
    assert migrated
    loaded = asyncio.Event()
    answers, _ = offer(TextDelta("At once."), DONE)
    model = Loads(answers, hold=loaded)
    lineup.offer("standard", model, gate=Gate(1))
    browser = await guest_on(start_app, settings, lineup.catalog())
    started = await warm_up(browser)
    await asyncio.wait_for(model.loading.wait(), PATIENCE_S)

    # A question that waited for the load at the gate would wait here for ever.
    answered = await asyncio.wait_for(asking(browser, "Hello"), PATIENCE_S)
    still_loading = model.loading.is_set()
    loaded.set()
    await until(lambda: model.ended == 1)

    assert started.status_code == 202
    assert answered.status_code == 200
    assert '"delta":"At once."' in answered.text
    assert still_loading


async def test_a_model_is_loaded_once_at_a_time_and_again_once_its_load_has_ended(
    start_app: Any, settings: Settings, migrated: str, offer: Any
) -> None:
    """A model that is loaded already is answered in milliseconds, and the time its
    server keeps it starts again: so a load that has ended is no reason not to load."""
    assert migrated
    loaded = asyncio.Event()
    answers, _ = offer()
    model = Loads(answers, hold=loaded)
    browser = await guest_on(start_app, settings, offering(model))

    first = await warm_up(browser)
    await asyncio.wait_for(model.loading.wait(), PATIENCE_S)
    meanwhile = await warm_up(browser)
    loads_meanwhile = len(model.loads)
    loaded.set()
    await until(lambda: model.ended == 1)
    later = await warm_up(browser)
    await until(lambda: model.ended == 2)

    assert (first.status_code, meanwhile.status_code, later.status_code) == (202, 202, 202)
    assert loads_meanwhile == 1
    assert model.loads == [("scripted-1", 4096), ("scripted-1", 4096)]


async def test_a_load_that_fails_is_said_in_the_log_by_its_kind_and_nothing_else(
    start_app: Any,
    settings: Settings,
    migrated: str,
    offer: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The route has answered by then. The error's text is a model server's, and stays
    out of the log, as for every error of a model's server."""
    assert migrated
    answers, _ = offer()
    model = Loads(answers, fails=ProviderUnavailableError("words the server wrote"))
    browser = await guest_on(start_app, settings, offering(model))

    response = await warm_up(browser)
    await until(lambda: model.ended == 1)
    then = await warm_up(browser)  # and the next is let through
    await until(lambda: model.ended == 2)

    assert (response.status_code, then.status_code) == (202, 202)
    told = [record for record in caplog.records if record.name == "actio.catalog"]
    assert [(record.levelno, record.getMessage()) for record in told] == [
        (logging.WARNING, "model standard: it wasn't loaded ahead (ProviderUnavailableError)")
    ] * 2
    assert "words the server wrote" not in caplog.text


async def test_a_load_that_fails_in_actios_own_code_is_logged_with_its_trace(
    start_app: Any,
    settings: Settings,
    migrated: str,
    offer: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Not a model server's error, but a mistake to find: nobody else hears of it."""
    assert migrated
    answers, _ = offer()
    model = Loads(answers, fails=RuntimeError("a mistake of Actio's"))
    browser = await guest_on(start_app, settings, offering(model))

    response = await warm_up(browser)
    await until(lambda: model.ended == 1)

    assert response.status_code == 202
    [told] = [record for record in caplog.records if record.name == "actio.catalog"]
    assert (told.levelno, told.getMessage()) == (
        logging.ERROR,
        "model standard: loading it ahead failed",
    )
    assert told.exc_info is not None
    assert isinstance(told.exc_info[1], RuntimeError)


async def test_a_load_costs_nothing(
    start_app: Any, settings: Settings, migrated: str, offer: Any, db: AsyncSession
) -> None:
    """Nothing is held, recorded or counted for it: a load is no answer."""
    assert migrated
    answers, _ = offer()
    model = Loads(answers)
    browser = await guest_on(start_app, settings, offering(model))
    before = (await browser.get("/api/usage")).json()

    response = await warm_up(browser)
    await until(lambda: model.ended == 1)

    assert response.status_code == 202
    assert (await browser.get("/api/usage")).json() == before
    assert await db.scalar(select(func.count()).select_from(UsageEntry)) == 0


async def test_a_load_under_way_is_cancelled_when_the_app_shuts_down(
    open_app: Any, settings: Settings, migrated: str, offer: Any
) -> None:
    """Also for a catalog the app was handed, which it doesn't close: nothing is left
    pending once the app has shut down."""
    assert migrated
    answers, _ = offer()
    model = Loads(answers, hold=asyncio.Event())  # a load that never ends by itself
    # With a time limit: an app that waited for the load would never shut down.
    async with (
        asyncio.timeout(PATIENCE_S),
        open_app(settings, catalog=offering(model)) as browser,
    ):
        await browser.post("/api/session/guest")
        response = await warm_up(browser)
        await asyncio.wait_for(model.loading.wait(), PATIENCE_S)

    assert response.status_code == 202
    assert (model.ended, model.cancelled) == (1, 1)
    assert [task for task in asyncio.all_tasks() if task.get_name().startswith("load-")] == []


# --- What is refused ------------------------------------------------------------------


async def test_loading_a_model_needs_a_session(api: httpx.AsyncClient) -> None:
    response = await api.post("/api/models/standard/warm-up")

    assert response.status_code == 401
    assert response.headers["content-type"] == PROBLEM


async def test_a_model_nobody_offers_is_not_found(api: httpx.AsyncClient) -> None:
    await api.post("/api/session/guest")

    response = await api.post("/api/models/largest/warm-up")

    assert response.status_code == 404
    assert response.headers["content-type"] == PROBLEM
    assert response.json()["detail"] == "No model is called that. GET /api/models lists them."


async def test_a_model_nobody_offers_is_refused_before_it_is_counted(
    start_app: Any, settings: Settings, migrated: str
) -> None:
    """Nothing was asked of any server, so nothing is taken from what the account may
    ask to have loaded."""
    assert migrated
    one_an_hour = settings.model_copy(update={"limit_warm_ups_per_user": Rate.parse("1/hour")})
    browser = await start_app(one_an_hour)
    await browser.post("/api/session/guest")

    unknown = [(await warm_up(browser, "largest")).status_code for _ in range(3)]

    assert unknown == [404, 404, 404]
    assert (await warm_up(browser)).status_code == 204


async def test_a_model_that_cant_answer_is_refused_as_a_question_to_it_is(
    start_app: Any,
    settings: Settings,
    migrated: str,
    catalog_without_its_model: Catalog,
    catalog_without_its_server: Catalog,
) -> None:
    """In the same words, with the same members, for the page to say what to do."""
    assert migrated
    refused = {}
    for state, catalog in (
        ("missing", catalog_without_its_model),
        ("unreachable", catalog_without_its_server),
    ):
        browser = await guest_on(start_app, settings, catalog)
        warm = await warm_up(browser)
        asked = await asyncio.wait_for(asking(browser, "Hi"), PATIENCE_S)
        refused[state] = (warm.status_code, warm.json(), asked.json())

    for state, (status, warm, asked) in refused.items():
        assert status == 503
        assert warm == asked
        assert (warm["model"], warm["model_status"]) == ("standard", state)
    assert refused["missing"][1]["detail"] == "This model isn't on its server yet."
    assert refused["unreachable"][1]["detail"] == "This model's server can't be reached."


async def test_a_model_that_can_answer_is_loaded_itself_and_never_a_model_that_stands_in_for_it(
    start_app: Any, settings: Settings, migrated: str, offer: Any, lineup: Any
) -> None:
    """A stand-in is asked only when the model can't answer: loaded ahead, it would push
    the model out of its server's memory, or load for nothing (ADR 0028)."""
    assert migrated
    answers, _ = offer()
    itself, stand_in = Loads(answers), Loads(answers)
    lineup.offer("standard", itself, then=["second"])
    lineup.offer("second", stand_in)
    browser = await guest_on(start_app, settings, lineup.catalog())

    response = await warm_up(browser)
    await until(lambda: itself.ended == 1)

    assert response.status_code == 202
    assert itself.loads == [("scripted-standard", 4096)]
    assert stand_in.loads == []


async def test_a_model_that_cant_answer_while_a_stand_in_can_has_nothing_to_load(
    start_app: Any, settings: Settings, migrated: str, offer: Any, lineup: Any
) -> None:
    """A question to it would be answered by the stand-in, which is never loaded ahead:
    not refused, and nothing loaded."""
    assert migrated
    answers, _ = offer()
    itself, stand_in = Loads(answers), Loads(answers)
    lineup.offer("standard", itself, state="unreachable", then=["second"])
    lineup.offer("second", stand_in)
    browser = await guest_on(start_app, settings, lineup.catalog())

    response = await warm_up(browser)

    assert response.status_code == 204
    assert response.content == b""
    assert (itself.loads, stand_in.loads) == ([], [])


async def test_a_model_none_of_whose_stand_ins_can_answer_is_refused_as_a_question_to_it_is(
    start_app: Any, settings: Settings, migrated: str, offer: Any, lineup: Any
) -> None:
    """With the model's own state, not a stand-in's."""
    assert migrated
    answers, _ = offer()
    lineup.offer("standard", Loads(answers), state="missing", then=["second"])
    lineup.offer("second", Loads(answers), state="unreachable")
    browser = await guest_on(start_app, settings, lineup.catalog())

    warm = await warm_up(browser)
    asked = await asyncio.wait_for(asking(browser, "Hi"), PATIENCE_S)

    assert warm.status_code == 503
    assert warm.json() == asked.json()
    assert (warm.json()["model"], warm.json()["model_status"]) == ("standard", "missing")


async def test_loads_are_limited_per_account(
    start_app: Any, settings: Settings, migrated: str, clock: Any
) -> None:
    """A guest is an account too. The page asks once a minute at most."""
    assert migrated
    two_an_hour = settings.model_copy(update={"limit_warm_ups_per_user": Rate.parse("2/hour")})
    mine, yours = await start_app(two_an_hour), await start_app(two_an_hour)
    await mine.post("/api/session/guest")
    await yours.post("/api/session/guest")
    allowed = [(await warm_up(mine)).status_code for _ in range(2)]

    refused = await warm_up(mine)

    assert allowed == [204, 204]  # the tests' model is on a server that can't load ahead
    assert refused.status_code == 429
    assert refused.headers["content-type"] == PROBLEM
    assert refused.headers["retry-after"] == "1800"
    assert refused.json()["detail"] == "Too many requests to load a model. Try again in 30 minutes."
    assert (await warm_up(yours)).status_code == 204  # each account has its own
    clock.advance(minutes=30)
    assert (await warm_up(mine)).status_code == 204


async def test_loads_take_nothing_from_what_an_account_may_ask_in_messages(
    start_app: Any, settings: Settings, migrated: str
) -> None:
    """Each is counted by itself: a page that asks for its model ahead doesn't use up
    its reader's questions."""
    assert migrated
    two_of_each = settings.model_copy(
        update={
            "limit_warm_ups_per_user": Rate.parse("2/hour"),
            "limit_turns_per_user": Rate.parse("2/hour"),
        }
    )
    browser = await start_app(two_of_each)
    await browser.post("/api/session/guest")
    loads = [(await warm_up(browser)).status_code for _ in range(3)]

    asked = await asyncio.wait_for(asking(browser, "Hello"), PATIENCE_S)

    assert loads == [204, 204, 429]
    assert asked.status_code == 200


async def test_the_route_is_in_the_api_description(api: httpx.AsyncClient) -> None:
    paths = (await api.get("/api/openapi.json")).json()["paths"]

    assert set(paths["/api/models/{model_id}/warm-up"]) == {"post"}
