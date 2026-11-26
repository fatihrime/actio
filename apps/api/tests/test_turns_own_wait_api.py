"""Turns asked of a model whose provider said to wait (ADR 0030, revision "a provider's own
wait is kept, and until it is over its model has no budget"): a 429 that names a wait
longer than the walk waits out itself is kept, for each model on a provider, until it is
over, and meanwhile the model counts as one with no budget left. It is asked after the
models of its chain that have budget, and a turn that no model with room for it has
budget for is refused for now.

The models are scripted (`lineup`), and each keeps the wait its provider named for as
long as the lineup lives. The app reads the time from the test's `clock`, which only the
test moves: a wait is over when the test says so, and no test sleeps through one. Every
wait has a time limit.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from valkey.asyncio import Valkey

import actio.app
from actio.api.rate_limits import in_words
from actio.chat.models import Conversation
from actio.core.budget import Budgets
from actio.core.ids import new_id
from actio.core.resources import Resources, open_resources
from actio.core.settings import Settings
from actio_llm import (
    Finish,
    FinishReason,
    ProviderUnavailableError,
    RateLimitedError,
    TextDelta,
    Usage,
    prompt_tokens,
)

pytestmark = pytest.mark.anyio

DONE = Finish(FinishReason.STOP, Usage(input_tokens=5, output_tokens=4))
PATIENCE_S = 5.0
"""How long a test waits for the app to get somewhere. It takes milliseconds; this only
keeps a broken app from hanging the test run."""
SECRET = "my door code is 4471"
"""What an error's text may quote: never in the log."""
QUESTION = "Which tyre pressure suits a gravel bike?"
NOWHERE_VALKEY = "valkey://127.0.0.1:1/0"
"""Where nothing listens."""
OWN = "its provider's own wait"
"""The limit in the way of a model whose provider said to wait, in the log's words."""
FIGURES = {"requests_a_minute": 10, "requests_a_day": 100}
"""A budget a turn here never spends."""


# --- Helpers --------------------------------------------------------------------------


def told_to_wait(seconds: float | None = 49) -> RateLimitedError:
    """A provider's 429 that names a wait, 49 s unless given, in words that must stay
    out of the log."""
    return RateLimitedError(f"429: slow down, {SECRET}", retry_after_s=seconds)


def busy() -> ProviderUnavailableError:
    """A server too busy to answer, in words that must stay out of the log."""
    return ProviderUnavailableError(f"busy with: {SECRET}")


def chunks(response: httpx.Response) -> list[Any]:
    """The chunks of a stream, in order, without the lines that are no events. The last
    one is the text `[DONE]`."""
    events = [event for event in response.text.split("\n\n") if event.startswith("data: ")]
    payloads = [event.removeprefix("data: ") for event in events]
    return [payload if payload == "[DONE]" else json.loads(payload) for payload in payloads]


def said(response: httpx.Response) -> str:
    """The text of an answer's stream."""
    return "".join(c["delta"] for c in chunks(response)[:-1] if c["type"] == "text-delta")


def turn_of(response: httpx.Response) -> dict[str, Any]:
    """The turn as the stream's last metadata says it was recorded."""
    ending = [c for c in chunks(response)[:-1] if c["type"] in ("finish", "message-metadata")]
    return ending[-1]["messageMetadata"]["turn"]  # type: ignore[no-any-return]


class Chat:
    """One conversation, as a browser would hold it."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.id = new_id()
        self.path = f"/api/conversations/{self.id}"

    async def ask(self, text: str = "Hello?", **more: Any) -> httpx.Response:
        """Ask, and wait for the whole response. `more` goes into the request too, such
        as the `model` to ask."""
        message = {"id": str(new_id()), "parts": [{"type": "text", "text": text}]}
        asking = self.client.post(f"{self.path}/turns", json={"message": message, **more})
        return await asyncio.wait_for(asking, PATIENCE_S)

    async def read(self) -> dict[str, Any]:
        response = await self.client.get(self.path)
        assert response.status_code == 200, response.text
        return response.json()  # type: ignore[no-any-return]


async def guest_on(start_app: Any, settings: Settings, lineup: Any) -> httpx.AsyncClient:
    """A guest's browser on an app that offers the models of `lineup`."""
    browser: httpx.AsyncClient = await start_app(settings, catalog=lineup.catalog())
    await browser.post("/api/session/guest")
    return browser


def told(caplog: pytest.LogCaptureFixture, turn_id: str) -> list[str]:
    """What the log says of a turn, in warnings, in order."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING and record.getMessage().startswith(f"turn {turn_id}: ")
    ]


def of_model(caplog: pytest.LogCaptureFixture, name: str) -> list[str]:
    """What the log says of a model itself, in order: of its breaker, its budget and its
    provider's wait."""
    return [message for message in caplog.messages if message.startswith(f"model {name}: ")]


def refusals(caplog: pytest.LogCaptureFixture, conversation_id: Any) -> list[str]:
    """What the log says, in warnings, of a conversation whose question was refused for
    now: by the conversation's id, since no turn of it was kept."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
        and record.getMessage().startswith(f"conversation {conversation_id}: ")
    ]


def noted(model: str, seconds: int) -> str:
    """The log's line for a model whose provider said to wait, and is believed."""
    return (
        f"model {model}: its provider said to wait {seconds} s, and until then it counts as"
        " having no budget left"
    )


def ended_early(model: str) -> str:
    """The log's line for a model that answered while its provider's wait was running."""
    return (
        f"model {model}: it answered before its provider's wait was over, and counts as having"
        " budget again"
    )


def asked_last(turn_id: str, model: str, seconds: int, limit: str = OWN) -> str:
    """The log's line for a model without budget left for the request, asked after those
    that have some."""
    return (
        f"turn {turn_id}: model {model} has no budget left for the request for about"
        f" {seconds} s ({limit}): it is asked after those that have"
    )


def answered_for(turn_id: str, stand_in: str, model: str = "standard") -> str:
    return f"turn {turn_id}: model {stand_in} answered in place of {model}"


def refused_for(conversation_id: Any, seconds: int, *waits: tuple[str, int, str]) -> str:
    """The log's line for a question refused for now, by its conversation."""
    each = ", ".join(f"{model} for about {wait} s ({limit})" for model, wait, limit in waits)
    return (
        f"conversation {conversation_id}: no model has budget left for the request, which is"
        f" refused for about {seconds} s: {each}"
    )


def refusal(wait: str) -> str:
    """What the API says of a question no model has budget for: `wait` in words."""
    return (
        "No model can take this message right now: this installation has reached the"
        f" limits of its models' providers. Try again in {wait}."
    )


class Plan:
    """In a scripted model's script: what each try does where it stands, in order, the
    last for every try after them. An error is raised there; `None` goes on."""

    def __init__(self, *tries: BaseException | None) -> None:
        self.tries = tries
        self.count = 0

    def __call__(self) -> None:
        then = self.tries[min(self.count, len(self.tries) - 1)]
        self.count += 1
        if then is not None:
            raise then


def told_now(lineup: Any, name: str, clock: Any, seconds: float = 49) -> None:
    """Have the provider of the model `name` say, at the test's time, to wait `seconds`,
    as if a turn's try had heard it."""
    lineup.catalog().get(name).own_wait.told(clock.now + timedelta(seconds=seconds))


@pytest.fixture
async def budgets(settings: Settings) -> AsyncIterator[Budgets]:
    """The budgets of the app under test, under the keys it counts under: for the test
    to fill."""
    async with Valkey.from_url(settings.valkey_url.get_secret_value()) as client:
        yield Budgets(client, prefix=f"{settings.valkey_prefix}:b")


@pytest.fixture
async def budgets_away(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """The app's budgets on an address where nothing listens, and the rest of its Valkey
    the test's own: a turn needs Valkey for its rate limit and its usage."""
    async with Valkey.from_url(NOWHERE_VALKEY) as nowhere:

        @asynccontextmanager
        async def opening(settings: Settings) -> AsyncIterator[Resources]:
            async with open_resources(settings) as resources:
                yield replace(resources, budgets=Budgets(nowhere))

        monkeypatch.setattr(actio.app, "open_resources", opening)
        yield


# --- A model in a chain ---------------------------------------------------------------


async def test_a_model_told_to_wait_is_asked_after_its_stand_in_until_the_wait_is_over(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    clock: Any,
    once: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The turn that meets the 429 is answered by the stand-in, as before. The next is
    asked of the stand-in first, and the told model is sent nothing, for as long as is
    left of the wait by the test's clock; at its very moment, the model is asked first
    again, and answers. Names and numbers alone in the log, never a word of the question
    or of the error."""
    assert migrated
    model = lineup.add("standard", once(told_to_wait()), TextDelta("Mine."), DONE, then=["second"])
    stand_in = lineup.add("second", TextDelta("From the stand-in."), DONE)
    browser = await guest_on(start_app, settings, lineup)

    met = await Chat(browser).ask(QUESTION)
    after = await Chat(browser).ask(QUESTION)
    clock.advance(seconds=48)
    nearly = await Chat(browser).ask(QUESTION)
    clock.advance(seconds=1)
    at_its_moment = await Chat(browser).ask(QUESTION)

    assert [said(response) for response in (met, after, nearly, at_its_moment)] == [
        *["From the stand-in."] * 3,
        "Mine.",
    ]
    assert (len(model.requests), len(stand_in.requests)) == (2, 3)
    first = turn_of(met)
    assert (first["status"], first["model"], first["answered_by"]) == (
        "completed",
        "standard",
        "second",
    )
    met_id, after_id, nearly_id, at_id = (
        turn_of(response)["id"] for response in (met, after, nearly, at_its_moment)
    )
    assert [
        message
        for message in caplog.messages
        if message.startswith((f"turn {met_id}: ", "model standard: "))
    ] == [
        noted("standard", 49),
        f"turn {met_id}: model standard failed (RateLimitedError), and second is asked next",
        answered_for(met_id, "second"),
    ]
    assert told(caplog, after_id) == [
        asked_last(after_id, "standard", 49),
        answered_for(after_id, "second"),
    ]
    assert told(caplog, nearly_id) == [
        asked_last(nearly_id, "standard", 1),
        answered_for(nearly_id, "second"),
    ]
    assert told(caplog, at_id) == []
    assert turn_of(at_its_moment)["answered_by"] == "standard"
    assert SECRET not in caplog.text
    for word in ("tyre", "pressure", "gravel"):
        assert word not in caplog.text


async def test_a_providers_wait_is_dated_when_the_try_that_heard_it_ended(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    clock: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The model's server takes 10 s by the test's clock to refuse: the next turn, at
    that moment, says the whole 49 s, which a wait dated from the request would have
    said as 39."""
    assert migrated
    lineup.add("standard", lambda: clock.advance(seconds=10), told_to_wait(), then=["second"])
    lineup.add("second", TextDelta("From the stand-in."), DONE)
    browser = await guest_on(start_app, settings, lineup)

    await Chat(browser).ask()
    after = await Chat(browser).ask()

    after_id = turn_of(after)["id"]
    assert told(caplog, after_id)[0] == asked_last(after_id, "standard", 49)


async def test_a_429_that_names_just_more_than_the_walk_waits_is_kept(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """2.001 s: not waited out, the stand-in is asked at once, and the next turn asks the
    model after it."""
    assert migrated
    model = lineup.add("standard", told_to_wait(2.001), then=["second"])
    lineup.add("second", TextDelta("From the stand-in."), DONE)
    browser = await guest_on(start_app, settings, lineup)

    met = await Chat(browser).ask()
    after = await Chat(browser).ask()

    met_id, after_id = turn_of(met)["id"], turn_of(after)["id"]
    assert told(caplog, met_id) == [
        f"turn {met_id}: model standard failed (RateLimitedError), and second is asked next",
        answered_for(met_id, "second"),
    ]
    assert told(caplog, after_id) == [
        asked_last(after_id, "standard", 3),
        answered_for(after_id, "second"),
    ]
    assert len(model.requests) == 1


@pytest.mark.parametrize("stand_in", [False, True], ids=["alone", "with a stand-in"])
async def test_a_429_whose_wait_the_walk_waits_out_is_not_kept_and_the_next_turn_asks_it(
    stand_in: bool,
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Asked once more after its 0.01 s, and refused again: alone, its turn fails; with a
    stand-in, that answers. The next turn asks the model first, as before this step,
    nothing is refused, and nothing is said of a provider's wait."""
    assert migrated
    model = lineup.add(
        "standard",
        Plan(told_to_wait(0.01), told_to_wait(0.01), None),
        TextDelta("Mine."),
        DONE,
        then=["second"] if stand_in else [],
    )
    if stand_in:
        lineup.add("second", TextDelta("From the stand-in."), DONE)
    browser = await guest_on(start_app, settings, lineup)

    met = await Chat(browser).ask()
    after = await Chat(browser).ask()

    expected = ("completed", "second") if stand_in else ("failed", None)
    assert (turn_of(met)["status"], turn_of(met)["answered_by"]) == expected
    assert (after.status_code, said(after), turn_of(after)["answered_by"]) == (
        200,
        "Mine.",
        "standard",
    )
    assert len(model.requests) == 3
    assert told(caplog, turn_of(after)["id"]) == []
    assert "its provider" not in caplog.text


async def test_a_second_try_that_names_a_longer_wait_has_it_kept(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    once: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The first 429 names a wait the walk waits out, and the second, at the model's
    second try, 30 s: that one is kept, from the end of the second try."""
    assert migrated
    model = lineup.add("standard", once(told_to_wait(0.01)), told_to_wait(30), then=["second"])
    lineup.add("second", TextDelta("From the stand-in."), DONE)
    browser = await guest_on(start_app, settings, lineup)

    met = await Chat(browser).ask()
    after = await Chat(browser).ask()

    met_id, after_id = turn_of(met)["id"], turn_of(after)["id"]
    assert [
        message
        for message in caplog.messages
        if message.startswith((f"turn {met_id}: ", "model standard: "))
    ] == [
        f"turn {met_id}: model standard failed (RateLimitedError), and is asked once more"
        " after 0.01 s",
        noted("standard", 30),
        f"turn {met_id}: model standard failed (RateLimitedError), and second is asked next",
        answered_for(met_id, "second"),
    ]
    assert told(caplog, after_id) == [
        asked_last(after_id, "standard", 30),
        answered_for(after_id, "second"),
    ]
    assert len(model.requests) == 2


async def test_a_429_that_names_no_wait_changes_nothing(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert migrated
    model = lineup.add(
        "standard", Plan(told_to_wait(None), None), TextDelta("Mine."), DONE, then=["second"]
    )
    lineup.add("second", TextDelta("From the stand-in."), DONE)
    browser = await guest_on(start_app, settings, lineup)

    met = await Chat(browser).ask()
    after = await Chat(browser).ask()

    assert (said(met), said(after)) == ("From the stand-in.", "Mine.")
    assert len(model.requests) == 2
    assert told(caplog, turn_of(after)["id"]) == []
    assert "its provider" not in caplog.text


# --- A model alone --------------------------------------------------------------------


async def test_a_lone_model_told_to_wait_has_its_turns_refused_for_what_is_left_of_the_wait(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    clock: Any,
    once: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The turn that meets the 429 fails, as before. A question after it is refused for
    now, for what is left of the provider's wait at its own moment, rounded up, with the
    model asked and no window; once the wait is over by the test's clock, the model is
    asked, and answers."""
    assert migrated
    model = lineup.add("standard", once(told_to_wait(49.5)), TextDelta("Mine."), DONE)
    browser = await guest_on(start_app, settings, lineup)

    met = await Chat(browser).ask()
    chat = Chat(browser)
    refused = await chat.ask()
    clock.advance(seconds=20)
    later = await chat.ask()
    clock.advance(seconds=29.5)
    answered = await chat.ask()

    turn = turn_of(met)
    assert (met.status_code, turn["status"], turn["error"]) == (200, "failed", "model_unavailable")
    assert told(caplog, turn["id"]) == [
        f"turn {turn['id']}: the model failed (RateLimitedError), recorded as model_unavailable"
    ]
    assert refused.status_code == 429
    assert refused.headers["content-type"] == "application/problem+json"
    assert refused.headers["retry-after"] == "50"
    assert refused.json() == {
        "type": "about:blank",
        "title": "Too Many Requests",
        "status": 429,
        "detail": refusal("50 seconds"),
        "retry_after": 50,
        "model": "standard",
    }
    assert (later.status_code, later.headers["retry-after"], later.json()["retry_after"]) == (
        429,
        "30",
        30,
    )
    assert said(answered) == "Mine."
    assert len(model.requests) == 2
    assert refusals(caplog, chat.id) == [
        refused_for(chat.id, 50, ("standard", 50, OWN)),
        refused_for(chat.id, 30, ("standard", 30, OWN)),
    ]
    assert SECRET not in caplog.text


@pytest.mark.parametrize("named", [86_400, 1e308], ids=["a day", "1e308 s"])
async def test_a_wait_of_a_day_or_more_refuses_a_question_for_a_day_said_in_hours(
    named: float,
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    once: Any,
) -> None:
    assert migrated
    lineup.add("standard", once(told_to_wait(named)), TextDelta("Mine."), DONE)
    browser = await guest_on(start_app, settings, lineup)
    await Chat(browser).ask()

    response = await Chat(browser).ask()

    assert response.status_code == 429
    assert response.headers["retry-after"] == "86400"
    body = response.json()
    assert (body["retry_after"], body["model"], "window" in body) == (86_400, "standard", False)
    assert body["detail"] == refusal("24 hours")
    assert in_words(86_400) == "24 hours"


async def test_a_question_refused_for_its_providers_wait_leaves_nothing_behind(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    db: AsyncSession,
) -> None:
    """A first question's conversation doesn't exist afterwards, a later question's
    reads back as it was, the usage reads as before, and the model was sent nothing more
    than the two questions before the refusals."""
    assert migrated
    model = lineup.add("standard", Plan(None, told_to_wait()), TextDelta("Mine."), DONE)
    browser = await guest_on(start_app, settings, lineup)
    chat = Chat(browser)
    await chat.ask()  # its first try, answered
    await Chat(browser).ask()  # its second, refused by its provider
    before = await chat.read()
    usage = (await browser.get("/api/usage")).json()

    later = await chat.ask("And another?", parent_id=before["messages"][-1]["id"])
    first = Chat(browser)
    refused_first = await first.ask()

    assert (later.status_code, refused_first.status_code) == (429, 429)
    assert await chat.read() == before
    assert [message["role"] for message in before["messages"]] == ["user", "assistant"]
    assert (await browser.get(first.path)).status_code == 404
    assert await db.scalar(select(func.count()).select_from(Conversation)) == 2
    assert (await browser.get("/api/usage")).json() == usage
    assert len(model.requests) == 2


# --- Without budgets, and with budgets that can't be read -----------------------------


@pytest.mark.usefixtures("budgets_away")
async def test_models_without_a_budget_are_ordered_and_refused_by_it_and_valkey_is_asked_nothing(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The lineup states no limits, and the budgets are where nothing listens: had the
    look asked, the log would say that they couldn't be read. A model told to wait is
    asked after its stand-in, and a lone one's question is refused."""
    assert migrated
    chained = lineup.add("standard", told_to_wait(), then=["second"])
    lineup.add("second", TextDelta("From the stand-in."), DONE)
    alone = lineup.add("alone", told_to_wait())
    browser = await guest_on(start_app, settings, lineup)

    await Chat(browser).ask()
    after = await Chat(browser).ask()
    await Chat(browser).ask(model="alone")
    chat = Chat(browser)
    refused = await chat.ask(model="alone")

    assert said(after) == "From the stand-in."
    after_id = turn_of(after)["id"]
    assert told(caplog, after_id) == [
        asked_last(after_id, "standard", 49),
        answered_for(after_id, "second"),
    ]
    assert (refused.status_code, refused.json()["model"]) == (429, "alone")
    assert refusals(caplog, chat.id) == [refused_for(chat.id, 49, ("alone", 49, OWN))]
    assert (len(chained.requests), len(alone.requests)) == (1, 1)
    assert not [message for message in caplog.messages if "couldn't be" in message]


@pytest.mark.usefixtures("budgets_away")
async def test_with_the_budgets_unreadable_a_providers_own_wait_still_orders_and_refuses(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    once: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Each model has a budget, which can't be read: each is taken to have some left,
    but the one its provider told to wait. In a chain it is asked last, after the line
    that says the budgets couldn't be read; alone, its question is refused."""
    assert migrated
    lineup.add(
        "standard",
        once(told_to_wait()),
        TextDelta("Never."),
        DONE,
        then=["second"],
        budget=FIGURES,
    )
    lineup.add("second", TextDelta("From the stand-in."), DONE)
    lineup.add("alone", once(told_to_wait()), TextDelta("Never."), DONE, budget=FIGURES)
    browser = await guest_on(start_app, settings, lineup)

    await Chat(browser).ask()
    after = await Chat(browser).ask()
    await Chat(browser).ask(model="alone")
    chat = Chat(browser)
    refused = await chat.ask(model="alone")

    after_id = turn_of(after)["id"]
    assert told(caplog, after_id) == [
        f"turn {after_id}: the models' budgets couldn't be read (ConnectionError), and each is"
        " taken to have some left",
        asked_last(after_id, "standard", 49),
        answered_for(after_id, "second"),
    ]
    assert (refused.status_code, refused.json()["retry_after"]) == (429, 49)
    assert refusals(caplog, chat.id) == [refused_for(chat.id, 49, ("alone", 49, OWN))]


@pytest.mark.parametrize(
    ("provider_s", "seconds", "limit"),
    [(30, 60, "requests a minute"), (90, 90, OWN), (60, 60, OWN)],
    ids=["the count's longer", "the provider's longer", "the two as long"],
)
async def test_of_the_counts_wait_and_the_providers_own_the_longer_is_said(
    provider_s: float,
    seconds: int,
    limit: str,
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    clock: Any,
    budgets: Budgets,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The model's one request a minute is spent at the test's moment, a minute to wait
    by the count, and its provider said to wait too: of two as long, the provider's."""
    assert migrated
    model = lineup.add(
        "standard", TextDelta("Never."), DONE, then=["second"], budget={"requests_a_minute": 1}
    )
    lineup.add("second", TextDelta("From the stand-in."), DONE)
    budget = lineup.catalog().get("standard").budget
    await budgets.add(*((bucket, bucket.size) for bucket in budget.buckets), now=clock.now)
    told_now(lineup, "standard", clock, provider_s)
    browser = await guest_on(start_app, settings, lineup)

    response = await Chat(browser).ask()

    assert said(response) == "From the stand-in."
    assert model.requests == []
    turn_id = turn_of(response)["id"]
    assert told(caplog, turn_id) == [
        asked_last(turn_id, "standard", seconds, limit),
        answered_for(turn_id, "second"),
    ]


# --- The rule's edges -----------------------------------------------------------------


async def test_a_model_told_to_wait_and_passed_over_for_its_room_is_named_for_its_room_alone(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    clock: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Its shorter wait is never the refusal's either: with its stand-in told to wait
    longer, a question is refused for the stand-in's wait, which alone is named."""
    assert migrated
    small = lineup.add("standard", TextDelta("Never."), DONE, then=["second"], tokens_a_minute=40)
    stand_in = lineup.add("second", TextDelta("From the stand-in."), DONE)
    told_now(lineup, "standard", clock, 10)
    browser = await guest_on(start_app, settings, lineup)

    response = await Chat(browser).ask()
    told_now(lineup, "second", clock, 49)
    chat = Chat(browser)
    refused = await chat.ask()

    assert said(response) == "From the stand-in."
    assert small.requests == []
    [request] = stand_in.requests
    turn_id = turn_of(response)["id"]
    assert told(caplog, turn_id) == [
        f"turn {turn_id}: model standard has room for 40 tokens, and the request is about"
        f" {prompt_tokens(request.messages)}: it is passed over",
        answered_for(turn_id, "second"),
    ]
    assert (refused.status_code, refused.json()["retry_after"]) == (429, 49)
    assert refusals(caplog, chat.id) == [refused_for(chat.id, 49, ("second", 49, OWN))]


async def test_when_no_model_has_room_each_is_asked_told_to_wait_or_not_and_none_is_named(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    clock: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A guess never refuses: the asked model is asked first, and answers, which ends
    its wait."""
    assert migrated
    lineup.add("standard", TextDelta("Mine."), DONE, then=["second"], tokens_a_minute=40)
    stand_in = lineup.add("second", TextDelta("Never."), DONE, tokens_a_minute=40)
    told_now(lineup, "standard", clock)
    told_now(lineup, "second", clock)
    browser = await guest_on(start_app, settings, lineup)

    response = await Chat(browser).ask()

    assert said(response) == "Mine."
    assert stand_in.requests == []
    assert told(caplog, turn_of(response)["id"]) == []
    assert of_model(caplog, "standard") == [ended_early("standard")]


async def test_a_model_told_to_wait_that_isnt_ready_is_never_named_nor_refused_for_it(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    clock: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """In a chain it is passed over for not being ready, as before; alone, its question
    is answered 503 for that, and not refused for its wait."""
    assert migrated
    lineup.add("standard", TextDelta("Never."), DONE, then=["second"], state="unreachable")
    lineup.add("second", TextDelta("From the stand-in."), DONE)
    lineup.add("alone", TextDelta("Never."), DONE, state="unreachable")
    told_now(lineup, "standard", clock)
    told_now(lineup, "alone", clock)
    browser = await guest_on(start_app, settings, lineup)

    response = await Chat(browser).ask()
    chat = Chat(browser)
    away = await chat.ask(model="alone")

    turn_id = turn_of(response)["id"]
    assert told(caplog, turn_id) == [
        f"turn {turn_id}: model standard is unreachable, and is passed over",
        answered_for(turn_id, "second"),
    ]
    assert (away.status_code, away.json()["model_status"]) == (503, "unreachable")
    assert refusals(caplog, chat.id) == []


@pytest.mark.parametrize(
    ("first_reasons", "answered"), [(True, 200), (False, 422)], ids=["it can", "it can't"]
)
async def test_whether_a_turn_may_ask_for_reasoning_is_the_first_ready_models_told_or_not(
    first_reasons: bool,
    answered: int,
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    clock: Any,
) -> None:
    assert migrated
    model = lineup.add(
        "standard", TextDelta("Never."), DONE, then=["second"], thinking=first_reasons
    )
    lineup.add("second", TextDelta("Fine."), DONE, thinking=not first_reasons)
    told_now(lineup, "standard", clock)
    browser = await guest_on(start_app, settings, lineup)

    response = await Chat(browser).ask(thinking=True)

    assert response.status_code == answered
    assert model.requests == []
    if answered == 200:
        assert said(response) == "Fine."


# --- What ends a wait before its time -------------------------------------------------


async def test_a_model_told_to_wait_that_answers_as_a_last_resort_is_asked_first_again(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Asked after its stand-in, which fails before its first word, it answers: the log
    says that its wait is over, and the next question is asked of it first."""
    assert migrated
    model = lineup.add(
        "standard", Plan(told_to_wait(), None), TextDelta("Mine."), DONE, then=["second"]
    )
    stand_in = lineup.add("second", Plan(None, busy(), None), TextDelta("From the stand-in."), DONE)
    browser = await guest_on(start_app, settings, lineup)

    met = await Chat(browser).ask()
    last_resort = await Chat(browser).ask()
    after = await Chat(browser).ask()

    assert [said(response) for response in (met, last_resort, after)] == [
        "From the stand-in.",
        "Mine.",
        "Mine.",
    ]
    assert (len(model.requests), len(stand_in.requests)) == (3, 2)
    last_resort_id = turn_of(last_resort)["id"]
    assert told(caplog, last_resort_id) == [
        asked_last(last_resort_id, "standard", 49),
        f"turn {last_resort_id}: model second failed (ProviderUnavailableError), and standard"
        " is asked next",
    ]
    assert of_model(caplog, "standard") == [noted("standard", 49), ended_early("standard")]
    assert told(caplog, turn_of(after)["id"]) == []


async def test_an_answer_that_was_being_written_when_the_provider_said_to_wait_leaves_the_wait(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two questions at once, as the Gemini API was seen to take them: the first is
    being written when the second is refused, with a wait of 49 s. The first one's
    answer ends after that, and its request was sent before it: it says nothing of the
    wait, which stays. The question after it is refused for now, and the model is sent
    nothing."""
    assert migrated
    held = asyncio.Event()
    model = lineup.add("standard", Plan(None, told_to_wait()), held, TextDelta("Mine."), DONE)
    browser = await guest_on(start_app, settings, lineup)

    being_written = asyncio.create_task(Chat(browser).ask())
    await asyncio.wait_for(model.waiting.wait(), PATIENCE_S)
    refused_by_its_provider = await Chat(browser).ask()
    held.set()
    written = await asyncio.wait_for(being_written, PATIENCE_S)
    chat = Chat(browser)
    after = await chat.ask()

    assert turn_of(refused_by_its_provider)["status"] == "failed"
    assert (said(written), turn_of(written)["answered_by"]) == ("Mine.", "standard")
    assert (after.status_code, after.json()["retry_after"]) == (429, 49)
    assert len(model.requests) == 2
    assert of_model(caplog, "standard") == [noted("standard", 49)]
    assert refusals(caplog, chat.id) == [refused_for(chat.id, 49, ("standard", 49, OWN))]


async def test_a_failure_of_another_kind_leaves_the_wait_as_it_was(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Asked as a last resort, it is too busy: the turn fails, and the next is asked of
    its stand-in first, for the rest of the wait."""
    assert migrated
    lineup.add("standard", Plan(told_to_wait(), busy()), then=["second"])
    lineup.add("second", Plan(None, busy(), None), TextDelta("From the stand-in."), DONE)
    browser = await guest_on(start_app, settings, lineup)

    await Chat(browser).ask()
    failed = await Chat(browser).ask()
    after = await Chat(browser).ask()

    assert (turn_of(failed)["status"], said(after)) == ("failed", "From the stand-in.")
    after_id = turn_of(after)["id"]
    assert told(caplog, after_id) == [
        asked_last(after_id, "standard", 49),
        answered_for(after_id, "second"),
    ]
    assert of_model(caplog, "standard") == [noted("standard", 49)]


async def test_a_later_429_replaces_the_wait_with_a_later_one_and_a_sooner_one(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Asked as a last resort while its wait runs, its provider names another: 300 s,
    and then 5 s. Each turn after says the newest."""
    assert migrated
    lineup.add(
        "standard", Plan(told_to_wait(49), told_to_wait(300), told_to_wait(5)), then=["second"]
    )
    lineup.add("second", Plan(None, busy(), busy(), None), TextDelta("From the stand-in."), DONE)
    browser = await guest_on(start_app, settings, lineup)

    responses = [await Chat(browser).ask() for _ in range(4)]

    first, second, third, fourth = (turn_of(response)["id"] for response in responses)
    assert [turn_of(response)["status"] for response in responses] == [
        "completed",
        "failed",
        "failed",
        "completed",
    ]
    assert [told(caplog, turn_id)[0] for turn_id in (second, third, fourth)] == [
        asked_last(second, "standard", 49),
        asked_last(third, "standard", 300),
        asked_last(fourth, "standard", 5),
    ]
    assert of_model(caplog, "standard") == [
        noted("standard", 49),
        noted("standard", 300),
        noted("standard", 5),
    ]
    assert told(caplog, first)[0].startswith(f"turn {first}: model standard failed")


# --- Whose wait -----------------------------------------------------------------------


async def test_a_429_through_one_name_of_a_model_has_its_other_name_asked_last_too(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two models that are one model on one provider share the wait its provider names;
    another model has its own. (The same model on two providers: `build`'s test.)"""
    assert migrated
    lineup.add("standard", told_to_wait(), then=["second"], on_server="shared")
    again = lineup.add("again", TextDelta("Never."), DONE, then=["second"], on_server="shared")
    lineup.add("second", TextDelta("From the stand-in."), DONE)
    elsewhere = lineup.add("elsewhere", TextDelta("Mine."), DONE, then=["second"])
    browser = await guest_on(start_app, settings, lineup)
    await Chat(browser).ask()

    through_again = await Chat(browser).ask(model="again")
    through_elsewhere = await Chat(browser).ask(model="elsewhere")

    assert (said(through_again), said(through_elsewhere)) == ("From the stand-in.", "Mine.")
    assert (again.requests, len(elsewhere.requests)) == ([], 1)
    turn_id = turn_of(through_again)["id"]
    assert told(caplog, turn_id) == [
        asked_last(turn_id, "again", 49),
        answered_for(turn_id, "second", "again"),
    ]
    assert told(caplog, turn_of(through_elsewhere)["id"]) == []
