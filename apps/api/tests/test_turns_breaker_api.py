"""Turns asked of a model that keeps failing (ADR 0031): after enough failed tries it is
asked after the models of its chain that aren't failing, for a while; once that while is
over it is asked in its place again; and it is never refused, nor dropped from a chain.

The models are scripted (`lineup`), and each keeps its breaker for as long as the
lineup lives. The app reads the time from the test's `clock`, which only the test
moves: a wait is over when the test says so, and no test sleeps through one. Every wait
has a time limit.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from typing import Any

import httpx
import pytest
from valkey.asyncio import Valkey

from actio.catalog import Gate
from actio.core.budget import Budgets
from actio.core.ids import new_id
from actio.core.settings import Settings
from actio_llm import (
    ConnectionFailedError,
    ContextOverflowError,
    Failing,
    Finish,
    FinishReason,
    ProviderUnavailableError,
    TextDelta,
    Usage,
)

pytestmark = pytest.mark.anyio

DONE = Finish(FinishReason.STOP, Usage(input_tokens=5, output_tokens=4))
PATIENCE_S = 5.0
"""How long a test waits for the app to get somewhere. It takes milliseconds; this only
keeps a broken app from hanging the test run."""
SECRET = "my door code is 4471"
"""What an error's text may quote: never in the log."""


# --- Helpers --------------------------------------------------------------------------


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


async def guest_on(start_app: Any, settings: Settings, lineup: Any) -> httpx.AsyncClient:
    """A guest's browser on an app that offers the models of `lineup`."""
    browser: httpx.AsyncClient = await start_app(settings, catalog=lineup.catalog())
    await browser.post("/api/session/guest")
    return browser


async def asked(browser: httpx.AsyncClient, times: int, **more: Any) -> list[httpx.Response]:
    """`times` questions, each in a conversation of its own, one after the other."""
    return [await Chat(browser).ask(**more) for _ in range(times)]


async def held(model: Any) -> None:
    """Wait until the model has yielded everything before its hold, and waits there."""
    await asyncio.wait_for(model.waiting.wait(), PATIENCE_S)


async def until(condition: Callable[[], bool]) -> None:
    """Wait until `condition` holds: the app gets there in a moment."""
    async with asyncio.timeout(PATIENCE_S):
        while not condition():  # noqa: ASYNC110 - what is waited for is counted by a gate
            await asyncio.sleep(0.005)


def told(caplog: pytest.LogCaptureFixture, turn_id: str) -> list[str]:
    """What the log says of a turn, in order, but the waits at a gate."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
        and record.getMessage().startswith(f"turn {turn_id}: ")
        and "for the model's server" not in record.getMessage()
    ]


def of_breakers(caplog: pytest.LogCaptureFixture) -> list[str]:
    """What the log says of the models' breakers, in order."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "actio.catalog" and "budget" not in record.getMessage()
    ]


def failing(turn_id: str, model: str, seconds: int | None) -> str:
    """The log's line for a model that keeps failing, asked after those that aren't."""
    if seconds is None:
        return (
            f"turn {turn_id}: model {model} is failing, and it is asked after models that"
            " aren't while one request tries it"
        )
    return (
        f"turn {turn_id}: model {model} is failing, and for about {seconds} s more it is asked"
        " after models that aren't"
    )


OPENED = (
    "model standard: 5 of its last 5 requests failed, and for 30 s it is asked after models"
    " that aren't failing"
)
"""The breaker's line when five failed tries of `standard` opened it."""


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


def opened(lineup: Any, name: str, clock: Any) -> None:
    """Open the breaker of the model `name` at the test's time, by five failed tries, as
    if they had come through turns."""
    breaker = lineup.catalog().get(name).breaker
    for _ in range(5):
        breaker.begins(clock.now).ends("failed", clock.now)
    assert breaker.failing(clock.now) == Failing(timedelta(seconds=30))


# --- After five failed tries ----------------------------------------------------------


async def test_after_five_failed_questions_the_stand_in_is_asked_first_and_the_model_nothing(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Each of the five fails at the asked model and is answered by its stand-in. The
    fifth failure opens its breaker, which the log says by the model's name; the sixth
    question is asked of the stand-in first, which answers, and the asked model is sent
    nothing. The turn is the asked model's all the same, and its record names the
    stand-in. Names and numbers alone in the log."""
    assert migrated
    model = lineup.add("standard", busy(), then=["second"])
    stand_in = lineup.add("second", TextDelta("From the stand-in."), DONE)
    browser = await guest_on(start_app, settings, lineup)

    *_, fifth = await asked(browser, 5)
    sixth = await Chat(browser).ask()

    assert [said(fifth), said(sixth)] == ["From the stand-in."] * 2
    assert (len(model.requests), len(stand_in.requests)) == (5, 6)
    turn = turn_of(sixth)
    assert (turn["status"], turn["model"], turn["answered_by"]) == (
        "completed",
        "standard",
        "second",
    )
    fifth_id, sixth_id = turn_of(fifth)["id"], turn["id"]
    assert [
        message
        for message in caplog.messages
        if message.startswith((f"turn {fifth_id}: ", f"turn {sixth_id}: ", "model standard: "))
    ] == [
        OPENED,
        f"turn {fifth_id}: model standard failed (ProviderUnavailableError), and second is"
        " asked next",
        f"turn {fifth_id}: model second answered in place of standard",
        failing(sixth_id, "standard", 30),
        f"turn {sixth_id}: model second answered in place of standard",
    ]
    assert SECRET not in caplog.text


async def test_the_wait_is_said_as_what_is_left_of_it_when_the_turn_starts(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    clock: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert migrated
    lineup.add("standard", busy(), then=["second"])
    lineup.add("second", TextDelta("From the stand-in."), DONE)
    browser = await guest_on(start_app, settings, lineup)
    await asked(browser, 5)

    clock.advance(seconds=17.5)
    response = await Chat(browser).ask()

    turn_id = turn_of(response)["id"]
    assert told(caplog, turn_id)[0] == failing(turn_id, "standard", 13)


# --- Once the wait is over ------------------------------------------------------------


async def test_once_the_wait_is_over_it_is_asked_in_its_place_and_failing_waits_a_minute(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    clock: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """At the end of the 30 seconds by the test's clock, the next question asks it first,
    as the one request that tries it. It fails, and is asked after its stand-in for a
    minute: the next question says so, and isn't asked of it."""
    assert migrated
    model = lineup.add("standard", busy(), then=["second"])
    stand_in = lineup.add("second", TextDelta("From the stand-in."), DONE)
    browser = await guest_on(start_app, settings, lineup)
    await asked(browser, 5)

    clock.advance(seconds=30)
    tried = await Chat(browser).ask()
    after = await Chat(browser).ask()

    assert (len(model.requests), len(stand_in.requests)) == (6, 7)
    tried_id, after_id = turn_of(tried)["id"], turn_of(after)["id"]
    assert told(caplog, tried_id) == [
        f"turn {tried_id}: model standard failed (ProviderUnavailableError), and second is"
        " asked next",
        f"turn {tried_id}: model second answered in place of standard",
    ]
    assert told(caplog, after_id) == [
        failing(after_id, "standard", 60),
        f"turn {after_id}: model second answered in place of standard",
    ]
    assert of_breakers(caplog) == [
        OPENED,
        "model standard: it failed again, and for 60 s more it is asked after models that"
        " aren't failing",
    ]


async def test_once_the_wait_is_over_a_model_that_answers_is_asked_first_from_then_on(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    clock: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert migrated
    model = lineup.add(
        "standard", Plan(*[busy()] * 5, None), TextDelta("Mine."), DONE, then=["second"]
    )
    stand_in = lineup.add("second", TextDelta("From the stand-in."), DONE)
    browser = await guest_on(start_app, settings, lineup)
    await asked(browser, 5)

    clock.advance(seconds=30)
    tried = await Chat(browser).ask()
    after = await Chat(browser).ask()

    assert [said(tried), said(after)] == ["Mine.", "Mine."]
    assert (len(model.requests), len(stand_in.requests)) == (7, 5)
    assert turn_of(tried)["answered_by"] == "standard"
    assert told(caplog, turn_of(after)["id"]) == []
    assert of_breakers(caplog) == [
        OPENED,
        "model standard: it answered, and is asked in its place again",
    ]


async def test_while_one_request_tries_it_the_others_ask_its_stand_in_first(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    clock: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The request that tries it is being written when another question comes: that one
    is asked of the stand-in first, and the log says that one request tries it."""
    assert migrated
    hold = asyncio.Event()
    model = lineup.add(
        "standard", Plan(*[busy()] * 5, None), hold, TextDelta("Mine."), DONE, then=["second"]
    )
    stand_in = lineup.add("second", TextDelta("From the stand-in."), DONE)
    browser = await guest_on(start_app, settings, lineup)
    await asked(browser, 5)

    clock.advance(seconds=30)
    trying = Chat(browser).asking()
    await held(model)
    meanwhile = await Chat(browser).ask()
    hold.set()
    tried = await asyncio.wait_for(trying, PATIENCE_S)

    assert [said(tried), said(meanwhile)] == ["Mine.", "From the stand-in."]
    assert (len(model.requests), len(stand_in.requests)) == (6, 6)
    meanwhile_id = turn_of(meanwhile)["id"]
    assert told(caplog, meanwhile_id) == [
        failing(meanwhile_id, "standard", None),
        f"turn {meanwhile_id}: model second answered in place of standard",
    ]


async def test_a_failing_model_answers_as_the_last_resort_when_its_stand_in_fails_and_is_back(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Before its wait is over: asked after its stand-in, which fails before its first
    word, it answers, and the next question is asked of it first."""
    assert migrated
    model = lineup.add(
        "standard", Plan(*[busy()] * 5, None), TextDelta("Mine."), DONE, then=["second"]
    )
    stand_in = lineup.add(
        "second", Plan(*[None] * 5, busy(), None), TextDelta("From the stand-in."), DONE
    )
    browser = await guest_on(start_app, settings, lineup)
    await asked(browser, 5)

    last_resort = await Chat(browser).ask()
    after = await Chat(browser).ask()

    assert [said(last_resort), said(after)] == ["Mine.", "Mine."]
    assert turn_of(last_resort)["answered_by"] == "standard"
    assert (len(model.requests), len(stand_in.requests)) == (7, 6)
    last_resort_id = turn_of(last_resort)["id"]
    assert told(caplog, last_resort_id) == [
        failing(last_resort_id, "standard", 30),
        f"turn {last_resort_id}: model second failed (ProviderUnavailableError), and standard"
        " is asked next",
    ]
    assert told(caplog, turn_of(after)["id"]) == []
    assert of_breakers(caplog) == [
        OPENED,
        "model standard: it answered, and is asked in its place again",
    ]


# --- Never refused, never dropped ------------------------------------------------------


async def test_a_chains_only_model_that_failed_ten_turns_is_asked_at_the_eleventh_as_before(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Its turn answers 200 and fails as it did, and its log says what it did: nothing
    of a model asked after another, since none is."""
    assert migrated
    model = lineup.add("standard", busy())
    browser = await guest_on(start_app, settings, lineup)

    *_, tenth, eleventh = await asked(browser, 11)

    assert len(model.requests) == 11
    assert [response.status_code for response in (tenth, eleventh)] == [200, 200]
    turn = turn_of(eleventh)
    assert (turn["status"], turn["error"]) == ("failed", "model_unavailable")
    assert told(caplog, turn["id"]) == [
        f"turn {turn['id']}: the model failed (ProviderUnavailableError), recorded as"
        " model_unavailable"
    ]
    assert of_breakers(caplog) == [OPENED]


async def test_a_chain_whose_every_model_is_failing_asks_each_in_its_order(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert migrated
    model = lineup.add("standard", busy(), then=["second"])
    stand_in = lineup.add("second", busy())
    browser = await guest_on(start_app, settings, lineup)

    *_, sixth = await asked(browser, 6)

    assert (len(model.requests), len(stand_in.requests)) == (6, 6)
    turn_id = turn_of(sixth)["id"]
    assert told(caplog, turn_id) == [
        f"turn {turn_id}: model standard failed (ProviderUnavailableError), and second is"
        " asked next",
        f"turn {turn_id}: model second failed (ProviderUnavailableError), and no other is left"
        " to ask",
        f"turn {turn_id}: the model failed (ProviderUnavailableError), recorded as"
        " model_unavailable",
    ]
    assert of_breakers(caplog) == [OPENED, OPENED.replace("standard", "second")]


@pytest.mark.parametrize(
    ("first_reasons", "answered"), [(True, 200), (False, 422)], ids=["it can", "it can't"]
)
async def test_whether_a_turn_may_ask_for_reasoning_is_the_first_ready_models_failing_or_not(
    first_reasons: bool,
    answered: int,
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    clock: Any,
) -> None:
    """As before: asked after its stand-in, the asked model still says it, whatever its
    stand-in can do."""
    assert migrated
    lineup.add("standard", TextDelta("Never."), DONE, then=["second"], thinking=first_reasons)
    lineup.add("second", TextDelta("From the stand-in."), DONE, thinking=not first_reasons)
    opened(lineup, "standard", clock)
    browser = await guest_on(start_app, settings, lineup)

    response = await Chat(browser).ask(thinking=True)

    assert response.status_code == answered
    if answered == 200:
        assert said(response) == "From the stand-in."


# --- Whose fault a failure is ---------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [ContextOverflowError(f"too large: {SECRET}"), busy()],
    ids=["a request too large", "a server too busy"],
)
async def test_a_request_too_large_ten_times_running_never_has_its_model_asked_last(
    error: Exception,
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Anyone can send one: the request's own fault, not the model's. The same ten times
    with a server too busy has its model asked last from the sixth."""
    assert migrated
    model = lineup.add("standard", error, then=["second"])
    lineup.add("second", TextDelta("From the stand-in."), DONE)
    browser = await guest_on(start_app, settings, lineup)

    *_, last = await asked(browser, 11)

    turn_id = turn_of(last)["id"]
    if isinstance(error, ContextOverflowError):
        assert len(model.requests) == 11
        assert told(caplog, turn_id) == [
            f"turn {turn_id}: model standard failed (ContextOverflowError), and second is"
            " asked next",
            f"turn {turn_id}: model second answered in place of standard",
        ]
        assert of_breakers(caplog) == []
    else:
        assert len(model.requests) == 5
        assert told(caplog, turn_id)[0] == failing(turn_id, "standard", 30)


@pytest.mark.parametrize(
    "before_the_hold", [(), (TextDelta("Half an "),)], ids=["before a word", "after one"]
)
async def test_turns_stopped_while_their_model_writes_never_have_it_asked_last(
    before_the_hold: tuple[Any, ...],
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    clock: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Five turns stopped, before the model's first word or after it: a stop is whoever
    asked's, not the model's failure."""
    assert migrated
    model = lineup.add("standard", *before_the_hold, asyncio.Event(), then=["second"])
    lineup.add("second", TextDelta("From the stand-in."), DONE)
    browser = await guest_on(start_app, settings, lineup)

    for _ in range(5):
        chat = Chat(browser)
        asking = chat.asking()
        await held(model)
        model.waiting.clear()
        stopped = await chat.stop(await chat.running_turn())
        await asyncio.wait_for(asking, PATIENCE_S)
        assert stopped.json()["status"] == "stopped"

    assert len(model.requests) == 5
    assert lineup.catalog().get("standard").breaker.failing(clock.now) is None
    assert of_breakers(caplog) == []


async def test_a_model_asked_once_more_that_fails_again_has_failed_twice(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A refused connection is asked once more at once, and each try is one of the five:
    the third question's first try opens the breaker, and the fourth question is asked
    of the stand-in first."""
    assert migrated
    model = lineup.add("standard", ConnectionFailedError(f"refused: {SECRET}"), then=["second"])
    lineup.add("second", TextDelta("From the stand-in."), DONE)
    browser = await guest_on(start_app, settings, lineup)

    *_, third, fourth = await asked(browser, 4)

    assert len(model.requests) == 6
    third_id, fourth_id = turn_of(third)["id"], turn_of(fourth)["id"]
    assert [
        message
        for message in caplog.messages
        if message.startswith((f"turn {third_id}: ", "model standard: "))
    ] == [
        OPENED,
        f"turn {third_id}: model standard failed (ConnectionFailedError), and is asked once more",
        f"turn {third_id}: model standard failed (ConnectionFailedError), and second is asked next",
        f"turn {third_id}: model second answered in place of standard",
    ]
    assert told(caplog, fourth_id)[0] == failing(fourth_id, "standard", 30)
    assert SECRET not in caplog.text


# --- Whose breaker, and when the order is decided --------------------------------------


async def test_failures_through_one_name_of_a_model_have_its_other_name_asked_last(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two models of the file that are one model on one provider: the first's five
    failures open the breaker they share, and a question to the second is asked of its
    stand-in first."""
    assert migrated
    lineup.add("standard", busy(), then=["second"], on_server="shared")
    again = lineup.add("again", TextDelta("Never."), DONE, then=["second"], on_server="shared")
    lineup.add("second", TextDelta("From the stand-in."), DONE)
    browser = await guest_on(start_app, settings, lineup)
    await asked(browser, 5)

    response = await Chat(browser).ask(model="again")

    assert said(response) == "From the stand-in."
    assert again.requests == []
    turn_id = turn_of(response)["id"]
    assert told(caplog, turn_id) == [
        failing(turn_id, "again", 30),
        f"turn {turn_id}: model second answered in place of again",
    ]


@pytest.fixture
async def budgets(settings: Settings) -> AsyncIterator[Budgets]:
    """The budgets of the app under test, under the keys it counts under: for the test
    to fill."""
    async with Valkey.from_url(settings.valkey_url.get_secret_value()) as client:
        yield Budgets(client, prefix=f"{settings.valkey_prefix}:b")


async def test_with_budgets_each_of_their_groups_asks_the_failing_models_after_the_others(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    clock: Any,
    budgets: Budgets,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """With budget and not failing, with budget and failing, without and not failing,
    without and failing: each fails before its first word but the last, and the log
    says who was asked after whom."""
    assert migrated
    model = lineup.add(
        "standard",
        TextDelta("Mine, after all."),
        DONE,
        then=["second", "third", "fourth"],
        budget={"requests_a_day": 1},
    )
    lineup.add("second", busy(), budget={"requests_a_day": 1})
    lineup.add("third", busy())
    lineup.add("fourth", busy())
    for spent in ("standard", "second"):
        budget = lineup.catalog().get(spent).budget
        await budgets.add(*((bucket, bucket.size) for bucket in budget.buckets), now=clock.now)
    opened(lineup, "standard", clock)
    opened(lineup, "third", clock)
    browser = await guest_on(start_app, settings, lineup)

    response = await Chat(browser).ask()

    assert said(response) == "Mine, after all."
    assert len(model.requests) == 1
    turn_id = turn_of(response)["id"]
    asked_last = "has no budget left for the request for about 86400 s (requests a day): it is"
    assert told(caplog, turn_id) == [
        f"turn {turn_id}: model standard {asked_last} asked after those that have",
        f"turn {turn_id}: model second {asked_last} asked after those that have",
        failing(turn_id, "third", 30),
        failing(turn_id, "standard", 30),
        f"turn {turn_id}: model fourth failed (ProviderUnavailableError), and third is asked next",
        f"turn {turn_id}: model third failed (ProviderUnavailableError), and second is asked next",
        f"turn {turn_id}: model second failed (ProviderUnavailableError), and standard is"
        " asked next",
    ]


async def test_the_order_is_decided_when_the_turn_starts_and_kept_while_it_waits(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    clock: Any,
) -> None:
    """A turn waits at its model's gate, behind another answer, while that model's
    breaker opens: it asks its models in the order it had when it started."""
    assert migrated
    gate, hold = Gate(1), asyncio.Event()
    model = lineup.add("standard", TextDelta("Mine."), DONE, then=["second"], gate=gate)
    stand_in = lineup.add("second", TextDelta("Never."), DONE)
    other = lineup.add("other", TextDelta("A long "), hold, TextDelta("answer."), DONE, gate=gate)
    browser = await guest_on(start_app, settings, lineup)
    asked_other = Chat(browser).asking(model="other")
    await held(other)
    waiting = Chat(browser).asking()
    await until(lambda: gate.waiting == 1)

    opened(lineup, "standard", clock)
    hold.set()
    async with asyncio.timeout(PATIENCE_S):
        _, response = await asyncio.gather(asked_other, waiting)

    assert said(response) == "Mine."
    assert (len(model.requests), stand_in.requests) == (1, [])


async def test_a_turn_stopped_while_the_request_that_tries_its_model_writes_ends_that_try(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    clock: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """What had arrived is recorded, as for any stop, and the try has ended: as an
    answer, which closes the breaker, and with nobody left trying the model."""
    assert migrated
    model = lineup.add("standard", TextDelta("Half an "), asyncio.Event(), then=["second"])
    lineup.add("second", TextDelta("Never."), DONE)
    opened(lineup, "standard", clock)
    breaker = lineup.catalog().get("standard").breaker
    browser = await guest_on(start_app, settings, lineup)
    clock.advance(seconds=30)
    chat = Chat(browser)
    asking = chat.asking()
    await held(model)
    trying, said_meanwhile = breaker.trying, breaker.failing(clock.now)

    stopped = await chat.stop(await chat.running_turn())
    response = await asyncio.wait_for(asking, PATIENCE_S)

    assert (trying, said_meanwhile) == (1, Failing(None))
    assert stopped.json()["status"] == "stopped"
    assert said(response) == "Half an "
    assert (await chat.read())["messages"][1]["parts"] == [{"type": "text", "text": "Half an "}]
    assert (breaker.trying, breaker.failing(clock.now)) == (0, None)
    assert of_breakers(caplog) == ["model standard: it answered, and is asked in its place again"]
