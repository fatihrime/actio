"""Turns asked of models whose providers' plans limit what this installation sends them
(ADR 0030): a model with nothing left in its budget for a request is asked after those
that have some, a turn that no model with room for it has budget for is refused for now,
what each request takes is counted, and Valkey being away fails and refuses nothing.

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
from actio.api.rate_limits import in_words
from actio.catalog import Budget
from actio.chat import context
from actio.chat.models import Conversation
from actio.core.budget import Bucket, Budgets
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
    System,
    TextDelta,
    TookTooLongError,
    Usage,
    User,
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

    def asking(
        self, text: str = "Hello?", *, message_id: str | None = None, **more: Any
    ) -> asyncio.Task[httpx.Response]:
        """Ask, and go on without waiting: the response comes when its stream has ended.
        `message_id` is the question's id, a new one unless given. `more` goes into the
        request too, such as the `model` to ask."""
        message = {"id": message_id or str(new_id()), "parts": [{"type": "text", "text": text}]}
        return asyncio.create_task(
            self.client.post(f"{self.path}/turns", json={"message": message, **more})
        )

    async def ask(
        self, text: str = "Hello?", *, message_id: str | None = None, **more: Any
    ) -> httpx.Response:
        """Ask, and wait for the whole response: for a turn that isn't held."""
        return await asyncio.wait_for(self.asking(text, message_id=message_id, **more), PATIENCE_S)

    async def answer_again(self, question_id: str) -> httpx.Response:
        """Have the question answered once more, and wait for the whole response."""
        again = self.client.post(f"{self.path}/turns", json={"answer_to": question_id})
        return await asyncio.wait_for(again, PATIENCE_S)

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


def refusals(caplog: pytest.LogCaptureFixture, conversation_id: Any) -> list[str]:
    """What the log says, in warnings, of a conversation whose question was refused for
    now: by the conversation's id, since no turn of it was kept."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
        and record.getMessage().startswith(f"conversation {conversation_id}: ")
    ]


def of_budgets(caplog: pytest.LogCaptureFixture) -> list[str]:
    """What the log says of budgets: of a turn's, and of what a request took."""
    return [message for message in caplog.messages if "budget" in message]


def budget_of(lineup: Any, name: str) -> Budget:
    """The budget the lineup gave the model called `name`."""
    budget = lineup.catalog().get(name).budget
    assert budget is not None
    return budget  # type: ignore[no-any-return]


def asked_last(turn_id: str, model: str, seconds: int, limit: str) -> str:
    """The log's line for a model whose budget has nothing left for the request, asked
    after those that have some."""
    return (
        f"turn {turn_id}: model {model} has no budget left for the request for about"
        f" {seconds} s ({limit}): it is asked after those that have"
    )


def refusal(wait: str) -> str:
    """What the API says of a question no model has budget for: `wait` is how long, in
    words."""
    return (
        "No model can take this message right now: this installation has reached the"
        f" limits of its models' providers. Try again in {wait}."
    )


def refused_for(conversation_id: Any, seconds: int, *waits: tuple[str, int, str]) -> str:
    """The log's line for a question refused for now, by its conversation: refused for
    `seconds`, and each model of `waits`, its name, its own wait and its limit in
    words."""
    each = ", ".join(f"{model} for about {wait} s ({limit})" for model, wait, limit in waits)
    return (
        f"conversation {conversation_id}: no model has budget left for the request, which is"
        f" refused for about {seconds} s: {each}"
    )


def guess_at(question: str, clock: Any) -> int:
    """Actio's guess at the prompt of a conversation's first question, asked at the
    test's time: the instructions and the question, as they go to a model."""
    return prompt_tokens([System(context.instructions(clock.now)), User(question)])


def waited_for(limit: Bucket, amount: int) -> int:
    """How many whole seconds an empty bucket makes `amount` wait, rounded up and 1 at
    the least: as long as `amount` moves its moment, in whole milliseconds rounded down,
    as the budgets' script counts it."""
    assert amount <= limit.size  # more than a bucket holds waits for a full one
    return max(1, math.ceil(amount * limit.period_ms // limit.size / 1000))


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


async def test_a_model_with_no_budget_left_is_asked_after_its_stand_in_which_answers_alone(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    plans_with: Any,
    spending: Spending,
    db: AsyncSession,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Its stand-in, which has budget, is asked first, and answers: the asked model is
    sent nothing. The turn is the asked model's all the same, at its price, where the
    stand-in costs nothing, and its record names the stand-in. The log says why, with
    names and numbers alone."""
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
        asked_last(turn["id"], "standard", 86_400, "requests a day"),
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
async def test_a_model_is_asked_last_by_each_limit_of_its_budget_and_the_line_names_it(
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
        asked_last(turn_id, "standard", seconds, words),
        f"turn {turn_id}: model second answered in place of standard",
    ]


async def test_the_log_says_who_is_passed_over_for_its_room_before_who_is_asked_last(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    spending: Spending,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One chain: the asked model without room for the request, the next without budget
    for it, asked after the third, and the third answers."""
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
        asked_last(turn_id, "second", 86_400, "requests a day"),
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
        f"turn {turn_id}: the models' budgets couldn't be read (ConnectionError), and each is"
        " taken to have some left",
        f"turn {turn_id}: model second answered in place of standard",
    ]


async def test_when_no_model_has_room_nor_budget_each_is_asked_as_ever_and_nothing_is_said(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    spending: Spending,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A request too large for every model by the guess is asked all the same, and so is
    one that no model has budget for then: nothing is refused, the asked model is asked
    first and answers, and nothing is said of rooms or budgets."""
    assert migrated
    asked = lineup.add(
        "standard",
        TextDelta("Mine."),
        DONE,
        then=["second"],
        tokens_a_minute=40,
        budget={"requests_a_day": 1},
    )
    stand_in = lineup.add(
        "second", TextDelta("Never."), DONE, tokens_a_minute=40, budget={"requests_a_minute": 1}
    )
    chat = Chat(await guest_on(start_app, settings, lineup))
    await spending.all_of(budget_of(lineup, "standard"))
    await spending.all_of(budget_of(lineup, "second"))

    response = await chat.ask()

    assert said(response) == "Mine."
    assert (len(asked.requests), stand_in.requests) == (1, [])
    assert prompt_tokens(asked.requests[0].messages) > 40  # what the test rests on
    assert told(caplog, turn_of(response)["id"]) == []
    assert of_budgets(caplog) == []


async def test_a_model_without_budget_is_asked_while_its_stand_ins_budget_is_spent(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    spending: Spending,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The asked model has no budget, and so always has budget: it is asked first, and
    answers. The stand-in that has none left is said, to be asked after it."""
    assert migrated
    asked = lineup.add("standard", TextDelta("Mine."), DONE, then=["second"])
    stand_in = lineup.add("second", TextDelta("Never."), DONE, budget={"requests_a_day": 1})
    chat = Chat(await guest_on(start_app, settings, lineup))
    await spending.all_of(budget_of(lineup, "second"))

    response = await chat.ask()

    assert said(response) == "Mine."
    assert (len(asked.requests), stand_in.requests) == (1, [])
    turn_id = turn_of(response)["id"]
    assert told(caplog, turn_id) == [asked_last(turn_id, "second", 86_400, "requests a day")]


async def test_a_model_without_budget_is_asked_last_and_answers_when_the_one_with_budget_fails(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    spending: Spending,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The asked model has nothing left of its budget, and its stand-in, which has
    budget, fails before its first word: the asked model is asked after it, and its
    answer is the turn's, by its own name. Actio keeps a tenth of each figure back, so a
    model its count calls spent may still answer."""
    assert migrated
    asked = lineup.add(
        "standard",
        TextDelta("Mine, after all."),
        DONE,
        then=["second"],
        budget={"requests_a_day": 1},
    )
    stand_in = lineup.add("second", ProviderUnavailableError("busy"))
    chat = Chat(await guest_on(start_app, settings, lineup))
    await spending.all_of(budget_of(lineup, "standard"))

    response = await chat.ask()

    assert said(response) == "Mine, after all."
    assert (len(stand_in.requests), len(asked.requests)) == (1, 1)
    turn = turn_of(response)
    assert (turn["status"], await answered_by(chat, response)) == ("completed", "standard")
    assert told(caplog, turn["id"]) == [
        asked_last(turn["id"], "standard", 86_400, "requests a day"),
        f"turn {turn['id']}: model second failed (ProviderUnavailableError), and standard is"
        " asked next",
    ]


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
    the first can't: a count that has it asked last changes nothing of what a turn may
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
        asked_last(turn_id, "second", 86_400, "requests a day"),
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


# --- When no model has budget ---------------------------------------------------------


async def test_a_question_no_model_has_budget_for_is_refused_for_now_with_how_long_to_wait(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    spending: Spending,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One request a minute, spent at this very moment: a minute to wait. The refusal
    names the model asked, as the 503 does, and nothing of a usage limit, nor of answers
    at once. The log says it, by the conversation."""
    assert migrated
    asked = lineup.add("standard", TextDelta("Never."), DONE, budget={"requests_a_minute": 1})
    chat = Chat(await guest_on(start_app, settings, lineup))
    await spending.all_of(budget_of(lineup, "standard"))

    response = await chat.ask()

    assert response.status_code == 429
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["retry-after"] == "60"
    assert response.json() == {
        "type": "about:blank",
        "title": "Too Many Requests",
        "status": 429,
        "detail": "No model can take this message right now: this installation has reached"
        " the limits of its models' providers. Try again in 60 seconds.",
        "retry_after": 60,
        "model": "standard",
    }
    assert asked.requests == []
    assert refusals(caplog, chat.id) == [
        refused_for(chat.id, 60, ("standard", 60, "requests a minute"))
    ]


async def test_a_first_question_refused_for_now_leaves_nothing_behind(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    spending: Spending,
    db: AsyncSession,
) -> None:
    """Its conversation doesn't exist afterwards, the usage reads as before, no model
    was sent anything, and no bucket of either budget holds more taken than before: each
    has one limit spent, and one with room, which a request taken would show."""
    assert migrated
    asked = lineup.add(
        "standard",
        TextDelta("Never."),
        DONE,
        then=["second"],
        budget={"requests_a_minute": 1, "tokens_a_day": 100_000},
    )
    stand_in = lineup.add(
        "second", TextDelta("Never."), DONE, budget={"requests_a_day": 1, "tokens_a_minute": 1_000}
    )
    browser = await guest_on(start_app, settings, lineup)
    chat = Chat(browser)
    budgets = [budget_of(lineup, "standard"), budget_of(lineup, "second")]
    # The first bucket of each: its requests.
    await spending.budgets.add(*((b.buckets[0], 1) for b in budgets), now=spending.clock.now)
    usage = (await browser.get("/api/usage")).json()
    taken = [await spending.taken(budget) for budget in budgets]

    response = await chat.ask()

    assert response.status_code == 429
    assert (response.json()["model"], "window" in response.json()) == ("standard", False)
    assert (await browser.get(chat.path)).status_code == 404
    assert await db.scalar(select(func.count()).select_from(Conversation)) == 0
    assert (await browser.get("/api/usage")).json() == usage
    assert (asked.requests, stand_in.requests) == ([], [])
    assert [await spending.taken(budget) for budget in budgets] == taken
    assert taken == [
        {"requests a minute": 1, "tokens a day": 0},
        {"requests a day": 1, "tokens a minute": 0},
    ]


async def test_a_later_question_refused_for_now_leaves_its_conversation_as_it_was(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    spending: Spending,
    db: AsyncSession,
) -> None:
    """The first question takes the minute's one request, and the second, at the same
    moment, is refused: the conversation reads back as it was, without it, the usage as
    before, the model was asked once, and its budget holds what the first took alone."""
    assert migrated
    model = lineup.add(
        "standard",
        TextDelta("Mine."),
        DONE,
        budget={"requests_a_minute": 1, "tokens_a_day": 100_000},
    )
    browser = await guest_on(start_app, settings, lineup)
    chat = Chat(browser)
    await chat.ask()
    before = await chat.read()
    usage = (await browser.get("/api/usage")).json()
    budget = budget_of(lineup, "standard")
    taken = await spending.taken(budget)

    response = await chat.ask("And another?", parent_id=before["messages"][-1]["id"])

    assert response.status_code == 429
    assert (response.json()["model"], "window" in response.json()) == ("standard", False)
    assert await chat.read() == before
    assert [message["role"] for message in before["messages"]] == ["user", "assistant"]
    assert await db.scalar(select(func.count()).select_from(UsageEntry)) == 1
    assert (await browser.get("/api/usage")).json() == usage
    assert len(model.requests) == 1
    assert await spending.taken(budget) == taken
    assert taken == {"requests a minute": 1, "tokens a day": COUNTED}


async def test_a_question_answered_again_that_is_refused_for_now_keeps_its_earlier_answer(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
) -> None:
    """The minute's one request went to the first answer: another answer, asked for at
    the same moment, is refused, and the conversation still shows the first."""
    assert migrated
    model = lineup.add("standard", TextDelta("Mine."), DONE, budget={"requests_a_minute": 1})
    chat = Chat(await guest_on(start_app, settings, lineup))
    await chat.ask()
    before = await chat.read()

    response = await chat.answer_again(before["messages"][0]["id"])

    assert response.status_code == 429
    assert response.json()["retry_after"] == 60
    after = await chat.read()
    assert after == before
    assert after["messages"][-1]["parts"] == [{"type": "text", "text": "Mine."}]
    assert after["messages"][-1]["metadata"]["versions"] == [after["messages"][-1]["id"]]
    assert len(model.requests) == 1


async def test_once_the_wait_is_over_the_same_question_under_the_same_id_is_answered(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    spending: Spending,
    clock: Any,
) -> None:
    """By the test's clock, moved on by the wait the refusal named."""
    assert migrated
    model = lineup.add("standard", TextDelta("Mine."), DONE, budget={"requests_a_minute": 1})
    chat = Chat(await guest_on(start_app, settings, lineup))
    await spending.all_of(budget_of(lineup, "standard"))
    question_id = str(new_id())

    refused = await chat.ask(message_id=question_id)
    clock.advance(seconds=refused.json()["retry_after"])
    answered = await chat.ask(message_id=question_id)

    assert refused.status_code == 429
    assert said(answered) == "Mine."
    assert len(model.requests) == 1
    assert (await chat.read())["messages"][0]["id"] == question_id


@pytest.mark.parametrize(
    ("key", "figure", "words"),
    [
        ("requests_a_minute", 1, "requests a minute"),
        ("requests_a_day", 1, "requests a day"),
        ("tokens_a_minute", 1_000, "tokens a minute"),
        ("tokens_a_day", 100, "tokens a day"),
    ],
)
async def test_each_limit_of_a_budget_refuses_a_question_for_its_own_wait(
    key: str,
    figure: int,
    words: str,
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    spending: Spending,
    clock: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The bucket is empty: the wait is for one request, or for the question's prompt by
    Actio's guess, to come back, in whole seconds, rounded up, and in words in the
    refusal. A day's is said in hours."""
    assert migrated
    model = lineup.add("standard", TextDelta("Never."), DONE, budget={key: figure})
    chat = Chat(await guest_on(start_app, settings, lineup))
    budget = budget_of(lineup, "standard")
    await spending.all_of(budget)
    [limit] = budget.buckets
    seconds = waited_for(limit, 1 if limit.counts == "requests" else guess_at("Hello?", clock))

    response = await chat.ask("Hello?")

    assert response.status_code == 429
    assert (response.headers["retry-after"], response.json()["retry_after"]) == (
        str(seconds),
        seconds,
    )
    assert response.json()["detail"] == refusal(in_words(seconds))
    if limit.period == "day":
        assert in_words(seconds).endswith(" hours")
    assert model.requests == []
    assert refusals(caplog, chat.id) == [
        refused_for(chat.id, seconds, ("standard", seconds, words))
    ]


@pytest.mark.parametrize(
    "sooner", ["standard", "second"], ids=["the asked model sooner", "its stand-in sooner"]
)
async def test_of_two_models_without_budget_the_refusal_waits_for_the_one_that_has_some_sooner(
    sooner: str,
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    spending: Spending,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One request a minute and one a day: a minute, whichever comes first in the chain.
    The log's line names both, in the chain's order, each with its own wait, and no word
    of the question is anywhere in the log."""
    assert migrated
    figures = {"requests_a_minute": 1} if sooner == "standard" else {"requests_a_day": 1}
    others = {"requests_a_day": 1} if sooner == "standard" else {"requests_a_minute": 1}
    asked = lineup.add("standard", TextDelta("Never."), DONE, then=["second"], budget=figures)
    stand_in = lineup.add("second", TextDelta("Never."), DONE, budget=others)
    chat = Chat(await guest_on(start_app, settings, lineup))
    await spending.all_of(budget_of(lineup, "standard"))
    await spending.all_of(budget_of(lineup, "second"))

    response = await chat.ask("Which tyre pressure suits a gravel bike?")

    assert response.status_code == 429
    assert (response.json()["retry_after"], response.json()["model"]) == (60, "standard")
    assert (asked.requests, stand_in.requests) == ([], [])
    minute, day = (60, "requests a minute"), (86_400, "requests a day")
    waits = (minute, day) if sooner == "standard" else (day, minute)
    assert refusals(caplog, chat.id) == [
        refused_for(chat.id, 60, ("standard", *waits[0]), ("second", *waits[1]))
    ]
    for word in ("tyre", "pressure", "gravel"):
        assert word not in caplog.text


@pytest.mark.parametrize("away", ["standard", "second"])
async def test_a_question_whose_one_ready_model_has_no_budget_is_refused_for_now_not_as_away(
    away: str,
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    spending: Spending,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The other model of the chain isn't ready: it counts for nothing, and the ready one
    has nothing left. A 429 for the model asked, not the 503 of a model away, and nobody
    is asked."""
    assert migrated
    asked = lineup.add(
        "standard",
        TextDelta("Never."),
        DONE,
        then=["second"],
        state="unreachable" if away == "standard" else "ready",
        budget={"requests_a_minute": 1},
    )
    stand_in = lineup.add(
        "second",
        TextDelta("Never."),
        DONE,
        state="unreachable" if away == "second" else "ready",
        budget={"requests_a_minute": 1},
    )
    chat = Chat(await guest_on(start_app, settings, lineup))
    await spending.all_of(budget_of(lineup, "standard"))
    await spending.all_of(budget_of(lineup, "second"))

    response = await chat.ask()

    assert response.status_code == 429
    assert (response.json()["retry_after"], response.json()["model"]) == (60, "standard")
    assert "model_status" not in response.json()
    assert (asked.requests, stand_in.requests) == ([], [])
    ready = "second" if away == "standard" else "standard"
    assert refusals(caplog, chat.id) == [refused_for(chat.id, 60, (ready, 60, "requests a minute"))]


async def test_the_refusal_for_now_comes_before_the_credits_whose_limit_is_reached_too(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    plans_with: Any,
    clock: Any,
    db: AsyncSession,
) -> None:
    """The first question takes the minute's one request, and 990 of the session's 1,000
    credits. The second, at the same moment, is past both: it is told of the models'
    budgets, and nothing of it is held or counted. A minute later, with the budget back,
    the same question meets the usage limit, which was there all along."""
    assert migrated
    lineup.add(
        "standard",
        TextDelta("A long answer."),
        Finish(FinishReason.STOP, Usage(input_tokens=600, output_tokens=390)),
        budget={"requests_a_minute": 1},
    )
    browser = await guest_on(start_app, settings, lineup, plans_with(guest=(1_000, 10_000)))
    chat = Chat(browser)
    await chat.ask()
    usage = (await browser.get("/api/usage")).json()
    after = (await chat.read())["messages"][-1]["id"]

    for_now = await chat.ask("And one more?", parent_id=after)
    usage_after = (await browser.get("/api/usage")).json()
    clock.advance(minutes=1)
    at_the_limit = await chat.ask("And one more?", parent_id=after)

    assert for_now.status_code == 429
    assert (for_now.json()["model"], "window" in for_now.json()) == ("standard", False)
    assert usage_after == usage
    assert await db.scalar(select(func.count()).select_from(UsageEntry)) == 1
    assert at_the_limit.status_code == 429
    assert (at_the_limit.json()["window"], "model" in at_the_limit.json()) == ("session", False)


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
async def test_budgets_that_cant_be_read_or_counted_refuse_nobody_and_fail_nothing(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    spending: Spending,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The look can't be had: the asked model is asked, as if every model had budget.
    Under the app's keys in the test's own Valkey, both budgets are spent, and a look
    there would refuse the question: nothing is refused for a look that can't be had.
    Its request can't be taken: the answer arrives whole, the turn completes, and
    nothing is put right afterwards, so the log says it once."""
    assert migrated
    lineup.add(
        "standard", TextDelta("All of "), TextDelta("it."), DONE, then=["second"], budget=FIGURES
    )
    second = lineup.add("second", TextDelta("Never."), DONE, budget=FIGURES)
    chat = Chat(await guest_on(start_app, settings, lineup))
    await spending.all_of(budget_of(lineup, "standard"))
    await spending.all_of(budget_of(lineup, "second"))

    response = await chat.ask()

    assert said(response) == "All of it."
    turn = turn_of(response)
    assert (turn["status"], await answered_by(chat, response)) == ("completed", "standard")
    assert second.requests == []
    assert of_budgets(caplog) == [
        f"turn {turn['id']}: the models' budgets couldn't be read (ConnectionError), and each"
        " is taken to have some left",
        "model standard: what a request to it took of its budget couldn't be counted"
        " (ConnectionError)",
    ]
