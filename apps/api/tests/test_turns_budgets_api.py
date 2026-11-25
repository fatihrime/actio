"""Turns asked of models whose providers' plans limit what this installation sends them
(ADR 0030): a model with nothing left in its budget for a request is passed over while
another has some, what each request takes is counted, and Valkey being away fails
nothing.

The models are scripted (`lineup`), each with a budget of the test's choosing, counted
in the session's Valkey under the app's own prefix: the test fills a budget there, and
reads back what a turn took, through the engine. The app reads the time from the test's
`clock`, which only the test moves. Every wait has a time limit.
"""

import asyncio
import json
import logging
import math
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
from actio.catalog import Budget
from actio.chat.models import Conversation
from actio.core.budget import Budgets
from actio.core.ids import new_id
from actio.core.resources import Resources, open_resources
from actio.core.settings import Settings
from actio.usage.models import UsageEntry
from actio.usage.plans import Plans
from actio_llm import (
    ConnectionFailedError,
    Finish,
    FinishReason,
    ProviderUnavailableError,
    RateLimitedError,
    ReasoningDelta,
    StreamEvent,
    TextDelta,
    TookTooLongError,
    Usage,
    prompt_tokens,
    tokens_in,
)

pytestmark = pytest.mark.anyio

DONE = Finish(FinishReason.STOP, Usage(input_tokens=60, output_tokens=40))
"""An answer's last event, with its provider's count: 100 tokens in all."""
COUNTED = 100
PATIENCE_S = 5.0
"""How long a test waits for the app to get somewhere. It takes milliseconds; this only
keeps a broken app from hanging the test run."""
NOWHERE_VALKEY = "valkey://127.0.0.1:1/0"
"""Where nothing listens."""
FIGURES = {
    "requests_a_minute": 10,
    "requests_a_day": 100,
    "tokens_a_minute": 1_000,
    "tokens_a_day": 100_000,
}
"""A model's four limits, as a models file states them. Kept: 9, 90, 900 and 90,000, more
than a turn here takes. A millisecond is worth less than a token or a request in each,
so that what a turn took is read back whole. And a token is worth 66 ms of the minute's
at least: the key of a bucket that a turn took a few tokens from lives for seconds of
real time, past the moment the test reads it back."""


# --- Helpers --------------------------------------------------------------------------


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

    def asking(self, text: str = "Hello?", **more: Any) -> asyncio.Task[httpx.Response]:
        """Ask, and go on without waiting: the response comes when its stream has ended.
        `more` goes into the request too, such as the `model` to ask."""
        message = {"id": str(new_id()), "parts": [{"type": "text", "text": text}]}
        return asyncio.create_task(
            self.client.post(f"{self.path}/turns", json={"message": message, **more})
        )

    async def ask(self, text: str = "Hello?", **more: Any) -> httpx.Response:
        """Ask, and wait for the whole response: for a turn that isn't held."""
        return await asyncio.wait_for(self.asking(text, **more), PATIENCE_S)

    async def stop(self, turn_id: str) -> httpx.Response:
        stopping = self.client.post(f"{self.path}/turns/{turn_id}/cancel")
        return await asyncio.wait_for(stopping, PATIENCE_S)

    async def read(self) -> dict[str, Any]:
        response = await self.client.get(self.path)
        assert response.status_code == 200, response.text
        return response.json()  # type: ignore[no-any-return]

    async def running_turn(self) -> str:
        """The id of the turn that is being written, as the conversation names it."""
        unanswered = (await self.read())["unanswered"]
        assert unanswered["status"] == "running"
        return unanswered["id"]  # type: ignore[no-any-return]


async def guest_on(
    start_app: Any, settings: Settings, lineup: Any, plans: Plans | None = None
) -> httpx.AsyncClient:
    """A guest's browser on an app that offers the models of `lineup`."""
    browser: httpx.AsyncClient = await start_app(settings, catalog=lineup.catalog(), plans=plans)
    await browser.post("/api/session/guest")
    return browser


async def answered_by(chat: Chat, response: httpx.Response) -> Any:
    """Which model the turn's record says wrote its answer, as the stream's last
    metadata says it, which the conversation read afterwards says too."""
    streamed = turn_of(response)
    conversation = await chat.read()
    stored = conversation["unanswered"] or conversation["messages"][-1]["metadata"]["turn"]
    assert stored == streamed
    return streamed["answered_by"]


def told(caplog: pytest.LogCaptureFixture, turn_id: str) -> list[str]:
    """What the log says of a turn, in order, but the waits at a gate."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
        and record.getMessage().startswith(f"turn {turn_id}: ")
        and "for the model's server" not in record.getMessage()
    ]


def of_budgets(caplog: pytest.LogCaptureFixture) -> list[str]:
    """What the log says of budgets: of a turn's, and of what a request took."""
    return [message for message in caplog.messages if "budget" in message]


def budget_of(lineup: Any, name: str) -> Budget:
    """The budget the lineup gave the model called `name`."""
    budget = lineup.catalog().get(name).budget
    assert budget is not None
    return budget  # type: ignore[no-any-return]


def no_budget_left(turn_id: str, model: str, seconds: int, limit: str) -> str:
    """The log's line for a model passed over for its budget."""
    return (
        f"turn {turn_id}: model {model} has no budget left for the request for about"
        f" {seconds} s ({limit}): it is passed over"
    )


@pytest.fixture
async def valkey(settings: Settings) -> AsyncIterator[Valkey]:
    async with Valkey.from_url(settings.valkey_url.get_secret_value()) as client:
        yield client


@pytest.fixture
def budgets(valkey: Valkey, settings: Settings) -> Budgets:
    """The budgets of the app under test, under the keys it counts under: for the test
    to fill, and to read back."""
    return Budgets(valkey, prefix=f"{settings.valkey_prefix}:b")


class Spending:
    """Fills a model's budget, and reads back what it holds taken, at the test's time."""

    def __init__(self, budgets: Budgets, clock: Any) -> None:
        self.budgets = budgets
        self.clock = clock

    async def all_of(self, budget: Budget) -> None:
        """Leave nothing in any bucket of the budget."""
        await self.budgets.add(*((b, b.size) for b in budget.buckets), now=self.clock.now)

    async def taken(self, budget: Budget) -> dict[str, int]:
        """What each bucket of the budget holds taken, by its limit in words: read back
        through a look, from how long until it is full again."""
        waits = await self.budgets.look(*((b, b.size) for b in budget.buckets), now=self.clock.now)
        return {
            str(b): round(wait / timedelta(milliseconds=1) * b.size / b.period_ms)
            for b, wait in zip(budget.buckets, waits, strict=True)
        }


@pytest.fixture
def spending(budgets: Budgets, clock: Any) -> Spending:
    return Spending(budgets, clock)


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


def each(count: int) -> dict[str, int]:
    """What a budget of `FIGURES` holds taken after requests that took `count` tokens
    together, by its limits in words, one request each."""
    return {
        "requests a minute": 1,
        "requests a day": 1,
        "tokens a minute": count,
        "tokens a day": count,
    }


# --- Who is asked ---------------------------------------------------------------------


async def test_a_model_with_no_budget_left_is_passed_over_for_its_stand_in_and_never_asked(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    plans_with: Any,
    spending: Spending,
    db: AsyncSession,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Its stand-in answers. The turn is the asked model's all the same, at its price,
    where the stand-in costs nothing, and its record names the stand-in. The log says
    why, with names and numbers alone."""
    assert migrated
    asked = lineup.add(
        "standard", TextDelta("Never."), DONE, then=["second"], budget={"requests_a_day": 1}
    )
    lineup.add("second", TextDelta("From the stand-in."), DONE)
    plans = plans_with(models=["standard"], free_models=["second"])
    chat = Chat(await guest_on(start_app, settings, lineup, plans))
    await spending.all_of(budget_of(lineup, "standard"))

    response = await chat.ask()

    assert said(response) == "From the stand-in."
    turn = turn_of(response)
    assert (turn["status"], turn["model"], turn["credits"]) == ("completed", "standard", COUNTED)
    assert await answered_by(chat, response) == "second"
    assert asked.requests == []
    [entry] = (await db.scalars(select(UsageEntry))).all()
    assert (entry.model, entry.credits) == ("standard", COUNTED)
    assert told(caplog, turn["id"]) == [
        no_budget_left(turn["id"], "standard", 86_400, "requests a day"),
        f"turn {turn['id']}: model second answered in place of standard",
    ]


@pytest.mark.parametrize(
    ("key", "figure", "words"),
    [
        ("requests_a_minute", 30, "requests a minute"),
        ("requests_a_day", 1000, "requests a day"),
        ("tokens_a_minute", 8000, "tokens a minute"),
        ("tokens_a_day", 200_000, "tokens a day"),
    ],
)
async def test_a_model_is_passed_over_by_each_limit_of_its_budget_and_the_line_names_it(
    key: str,
    figure: int,
    words: str,
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    spending: Spending,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The bucket is empty: the wait is for one request, or for the request's prompt by
    Actio's guess, to come back, in whole seconds, rounded up."""
    assert migrated
    asked = lineup.add("standard", TextDelta("Never."), DONE, then=["second"], budget={key: figure})
    stand_in = lineup.add("second", TextDelta("From the stand-in."), DONE)
    chat = Chat(await guest_on(start_app, settings, lineup))
    budget = budget_of(lineup, "standard")
    await spending.all_of(budget)

    response = await chat.ask()

    assert said(response) == "From the stand-in."
    assert asked.requests == []
    [request] = stand_in.requests
    [limit] = budget.buckets
    amount = 1 if limit.counts == "requests" else prompt_tokens(request.messages)
    seconds = max(1, math.ceil(amount * limit.period_ms // limit.size / 1000))
    turn_id = turn_of(response)["id"]
    assert told(caplog, turn_id) == [
        no_budget_left(turn_id, "standard", seconds, words),
        f"turn {turn_id}: model second answered in place of standard",
    ]


async def test_the_log_says_who_is_passed_over_for_its_room_before_who_for_its_budget(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    spending: Spending,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One chain: the asked model without room for the request, the next without budget
    for it, and the third answers."""
    assert migrated
    small = lineup.add(
        "standard", TextDelta("Never."), DONE, then=["second", "third"], tokens_a_minute=40
    )
    spent = lineup.add("second", TextDelta("Never."), DONE, budget={"requests_a_day": 1})
    third = lineup.add("third", TextDelta("From the third."), DONE)
    chat = Chat(await guest_on(start_app, settings, lineup))
    await spending.all_of(budget_of(lineup, "second"))

    response = await chat.ask()

    assert said(response) == "From the third."
    assert (small.requests, spent.requests) == ([], [])
    [request] = third.requests
    turn_id = turn_of(response)["id"]
    assert told(caplog, turn_id) == [
        f"turn {turn_id}: model standard has room for 40 tokens, and the request is about"
        f" {prompt_tokens(request.messages)}: it is passed over",
        no_budget_left(turn_id, "second", 86_400, "requests a day"),
        f"turn {turn_id}: model third answered in place of standard",
    ]


@pytest.mark.usefixtures("budgets_away")
async def test_the_log_says_that_the_budgets_couldnt_be_read_after_the_rooms_line(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert migrated
    small = lineup.add("standard", TextDelta("Never."), DONE, then=["second"], tokens_a_minute=40)
    stand_in = lineup.add("second", TextDelta("From the stand-in."), DONE, budget=FIGURES)
    chat = Chat(await guest_on(start_app, settings, lineup))

    response = await chat.ask()

    assert said(response) == "From the stand-in."
    assert small.requests == []
    [request] = stand_in.requests
    turn_id = turn_of(response)["id"]
    assert told(caplog, turn_id) == [
        f"turn {turn_id}: model standard has room for 40 tokens, and the request is about"
        f" {prompt_tokens(request.messages)}: it is passed over",
        f"turn {turn_id}: the models' budgets couldn't be read (ConnectionError), and none is"
        " passed over for its budget",
        f"turn {turn_id}: model second answered in place of standard",
    ]


async def test_when_no_model_has_budget_left_each_is_asked_as_ever_and_nothing_is_said(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    spending: Spending,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A count never refuses a turn: the asked model is asked first, and answers."""
    assert migrated
    asked = lineup.add(
        "standard", TextDelta("Mine."), DONE, then=["second"], budget={"requests_a_day": 1}
    )
    stand_in = lineup.add("second", TextDelta("Never."), DONE, budget={"tokens_a_minute": 1})
    chat = Chat(await guest_on(start_app, settings, lineup))
    await spending.all_of(budget_of(lineup, "standard"))
    await spending.all_of(budget_of(lineup, "second"))

    response = await chat.ask()

    assert said(response) == "Mine."
    assert (len(asked.requests), stand_in.requests) == (1, [])
    assert told(caplog, turn_of(response)["id"]) == []


async def test_a_model_without_budget_is_asked_while_its_stand_ins_budget_is_spent(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    spending: Spending,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The asked model has no budget, and so always has budget: it is asked, and the
    stand-in that has none left is said, though it would never be asked."""
    assert migrated
    asked = lineup.add("standard", TextDelta("Mine."), DONE, then=["second"])
    lineup.add("second", TextDelta("Never."), DONE, budget={"requests_a_day": 1})
    chat = Chat(await guest_on(start_app, settings, lineup))
    await spending.all_of(budget_of(lineup, "second"))

    response = await chat.ask()

    assert said(response) == "Mine."
    assert len(asked.requests) == 1
    turn_id = turn_of(response)["id"]
    assert told(caplog, turn_id) == [no_budget_left(turn_id, "second", 86_400, "requests a day")]


@pytest.mark.parametrize(
    ("first_reasons", "answered"), [(True, 200), (False, 422)], ids=["it can", "it can't"]
)
async def test_whether_a_turn_may_ask_for_reasoning_is_the_first_ready_models_without_budget_too(
    first_reasons: bool,
    answered: int,
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    spending: Spending,
    db: AsyncSession,
) -> None:
    """The first ready model has no budget left, and its stand-in can reason only when
    the first can't: a count that passes it over changes nothing of what a turn may
    ask."""
    assert migrated
    asked = lineup.add(
        "standard",
        TextDelta("Never."),
        DONE,
        then=["second"],
        thinking=first_reasons,
        budget={"requests_a_day": 1},
    )
    stand_in = lineup.add("second", TextDelta("Fine."), DONE, thinking=not first_reasons)
    chat = Chat(await guest_on(start_app, settings, lineup))
    await spending.all_of(budget_of(lineup, "standard"))

    response = await chat.ask(thinking=True)

    assert response.status_code == answered
    assert asked.requests == []
    if answered == 422:
        assert response.json()["detail"] == "This model can't reason before it answers."
        assert await db.scalar(select(func.count()).select_from(Conversation)) == 0
        assert stand_in.requests == []
    else:
        assert said(response) == "Fine."
        assert [request.thinking for request in stand_in.requests] == [None]


async def test_two_models_that_share_a_budget_take_from_one_another(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One model on one provider, offered twice: a turn to the first takes the one
    request a day there is, and the second then has none left."""
    assert migrated
    first = lineup.add(
        "standard", TextDelta("First."), DONE, on_server="shared", budget={"requests_a_day": 1}
    )
    second = lineup.add(
        "second",
        TextDelta("Never."),
        DONE,
        then=["third"],
        on_server="shared",
        budget={"requests_a_day": 1},
    )
    lineup.add("third", TextDelta("From the third."), DONE)
    browser = await guest_on(start_app, settings, lineup)

    to_the_first = await Chat(browser).ask()
    to_the_second = await Chat(browser).ask(model="second")

    assert (said(to_the_first), said(to_the_second)) == ("First.", "From the third.")
    assert (len(first.requests), second.requests) == (1, [])
    turn_id = turn_of(to_the_second)["id"]
    assert told(caplog, turn_id) == [
        no_budget_left(turn_id, "second", 86_400, "requests a day"),
        f"turn {turn_id}: model third answered in place of second",
    ]


async def test_a_model_is_asked_again_once_its_budget_has_come_back(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    clock: Any,
) -> None:
    """One request a minute: the second question a moment later goes to the stand-in,
    and the third, a minute later, to the model again."""
    assert migrated
    asked = lineup.add(
        "standard", TextDelta("Mine."), DONE, then=["second"], budget={"requests_a_minute": 1}
    )
    lineup.add("second", TextDelta("From the stand-in."), DONE)
    browser = await guest_on(start_app, settings, lineup)

    first = await Chat(browser).ask()
    clock.advance(seconds=1)
    second = await Chat(browser).ask()
    clock.advance(minutes=1)
    third = await Chat(browser).ask()

    assert [said(response) for response in (first, second, third)] == [
        "Mine.",
        "From the stand-in.",
        "Mine.",
    ]
    assert len(asked.requests) == 2


# --- What a request takes -------------------------------------------------------------


async def test_a_turn_takes_one_request_and_what_its_provider_counted(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    spending: Spending,
) -> None:
    """From each bucket: the provider's count put in the place of the guess the request
    was first taken with."""
    assert migrated
    model = lineup.add("standard", TextDelta("Hello."), DONE, budget=FIGURES)
    chat = Chat(await guest_on(start_app, settings, lineup))

    await chat.ask()

    [request] = model.requests
    assert prompt_tokens(request.messages) != COUNTED  # what the test rests on
    assert await spending.taken(budget_of(lineup, "standard")) == each(COUNTED)


async def test_a_model_that_fails_before_its_first_word_keeps_the_request_and_the_guess(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    spending: Spending,
) -> None:
    """It was sent, and may have been counted there: a request, and its prompt by Actio's
    guess. Its stand-in, which has no budget, answers."""
    assert migrated
    asked = lineup.add(
        "standard", ProviderUnavailableError("busy"), then=["second"], budget=FIGURES
    )
    lineup.add("second", TextDelta("From the stand-in."), DONE)
    chat = Chat(await guest_on(start_app, settings, lineup))

    response = await chat.ask()

    assert said(response) == "From the stand-in."
    [request] = asked.requests
    assert await spending.taken(budget_of(lineup, "standard")) == each(
        prompt_tokens(request.messages)
    )


async def test_a_refused_connection_gives_back_what_it_took_and_the_second_try_counts(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    spending: Spending,
    once: Any,
) -> None:
    """Nothing reached the server the first time. The second try is asked, and answers:
    one request, by its provider's count."""
    assert migrated
    model = lineup.add(
        "standard",
        once(ConnectionFailedError("refused")),
        TextDelta("Hello."),
        DONE,
        budget=FIGURES,
    )
    chat = Chat(await guest_on(start_app, settings, lineup))

    response = await chat.ask()

    assert said(response) == "Hello."
    assert len(model.requests) == 2
    assert await spending.taken(budget_of(lineup, "standard")) == each(COUNTED)


async def test_a_second_try_after_a_server_asked_to_wait_is_a_second_request(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    spending: Spending,
    once: Any,
) -> None:
    """The first was sent, and refused: it keeps its request and its guess. The second
    is counted by its provider's count."""
    assert migrated
    model = lineup.add(
        "standard",
        once(RateLimitedError("429", retry_after_s=0.01)),
        TextDelta("Hello."),
        DONE,
        budget=FIGURES,
    )
    chat = Chat(await guest_on(start_app, settings, lineup))

    response = await chat.ask()

    assert said(response) == "Hello."
    first, _ = model.requests
    guess = prompt_tokens(first.messages)
    assert await spending.taken(budget_of(lineup, "standard")) == {
        "requests a minute": 2,
        "requests a day": 2,
        "tokens a minute": guess + COUNTED,
        "tokens a day": guess + COUNTED,
    }


async def test_a_stand_in_that_answers_is_counted_under_its_own_model_and_no_other(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    spending: Spending,
) -> None:
    """The asked model has no request left this minute, and is never asked: its other
    bucket stays full."""
    assert migrated
    lineup.add(
        "standard",
        TextDelta("Never."),
        DONE,
        then=["second"],
        budget={"requests_a_minute": 10, "tokens_a_day": 100_000},
    )
    lineup.add("second", TextDelta("From the stand-in."), DONE, budget=FIGURES)
    chat = Chat(await guest_on(start_app, settings, lineup))
    asked = budget_of(lineup, "standard")
    await spending.budgets.add((asked.buckets[0], 9), now=spending.clock.now)

    response = await chat.ask()

    assert said(response) == "From the stand-in."
    assert await spending.taken(budget_of(lineup, "second")) == each(COUNTED)
    assert await spending.taken(asked) == {"requests a minute": 9, "tokens a day": 0}


async def test_a_turn_stopped_while_its_answer_is_written_takes_the_guess_and_what_was_written(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    spending: Spending,
) -> None:
    """A stream that is closed sends no count: what was handed on, the thought too, is
    counted by guess."""
    assert migrated
    hold = asyncio.Event()
    model = lineup.add(
        "standard",
        ReasoningDelta("Thinking it over."),
        TextDelta("Half an "),
        hold,
        TextDelta("answer."),
        DONE,
        budget=FIGURES,
    )
    chat = Chat(await guest_on(start_app, settings, lineup))
    asking = chat.asking()
    await asyncio.wait_for(model.waiting.wait(), PATIENCE_S)

    stopped = await chat.stop(await chat.running_turn())
    response = await asyncio.wait_for(asking, PATIENCE_S)

    assert (stopped.json()["status"], said(response)) == ("stopped", "Half an ")
    assert model.closed.is_set()
    [request] = model.requests
    written = tokens_in("Thinking it over." + "Half an ")
    assert await spending.taken(budget_of(lineup, "standard")) == each(
        prompt_tokens(request.messages) + written
    )


@pytest.mark.parametrize(
    "end",
    [TookTooLongError("over its time"), Finish(FinishReason.STOP, None)],
    ids=["cut at its server's time", "a finish without a count"],
)
async def test_an_answer_without_its_providers_count_takes_the_guess_and_what_was_written(
    end: StreamEvent | Exception,
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    spending: Spending,
) -> None:
    assert migrated
    model = lineup.add("standard", TextDelta("Some words of it"), end, budget=FIGURES)
    chat = Chat(await guest_on(start_app, settings, lineup))

    response = await chat.ask()

    assert (turn_of(response)["status"], said(response)) == ("completed", "Some words of it")
    [request] = model.requests
    assert await spending.taken(budget_of(lineup, "standard")) == each(
        prompt_tokens(request.messages) + tokens_in("Some words of it")
    )


# --- When Valkey is away --------------------------------------------------------------


@pytest.mark.usefixtures("budgets_away")
async def test_a_lineup_without_budgets_asks_valkey_nothing_for_them(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Its budgets can't be read: had anything asked, the log would say so."""
    assert migrated
    lineup.add("standard", ProviderUnavailableError("busy"), then=["second"])
    lineup.add("second", TextDelta("From the stand-in."), DONE)
    chat = Chat(await guest_on(start_app, settings, lineup))

    response = await chat.ask()

    assert said(response) == "From the stand-in."
    assert of_budgets(caplog) == []


@pytest.mark.usefixtures("budgets_away")
async def test_budgets_that_cant_be_read_or_counted_pass_nobody_over_and_fail_nothing(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The look can't be had: the asked model is asked, as if every model had budget.
    Its request can't be taken: the answer arrives whole, the turn completes, and
    nothing is put right afterwards, so the log says it once."""
    assert migrated
    lineup.add(
        "standard", TextDelta("All of "), TextDelta("it."), DONE, then=["second"], budget=FIGURES
    )
    second = lineup.add("second", TextDelta("Never."), DONE, budget=FIGURES)
    chat = Chat(await guest_on(start_app, settings, lineup))

    response = await chat.ask()

    assert said(response) == "All of it."
    turn = turn_of(response)
    assert (turn["status"], await answered_by(chat, response)) == ("completed", "standard")
    assert second.requests == []
    assert of_budgets(caplog) == [
        f"turn {turn['id']}: the models' budgets couldn't be read (ConnectionError), and none"
        " is passed over for its budget",
        "model standard: what a request to it took of its budget couldn't be counted"
        " (ConnectionError)",
    ]
