"""Turns asked of a model that others stand in for (ADR 0028): who answers, with what
request, what the turn records and costs, the gates on the way, a stop, a model asked
once more, models that aren't ready, models whose room is smaller than the request (ADR
0029), and what the log says.

The models are scripted (`lineup`), each behind a gate of the test's choosing. The app
runs in process, so a request's response comes back whole once its stream has ended: a
test that acts while a turn waits or writes starts the request as a task, and looks at
the response afterwards. Every wait has a time limit.
"""

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from actio.catalog import Gate
from actio.chat.models import Conversation
from actio.core.ids import new_id
from actio.core.settings import Settings
from actio.usage.models import UsageEntry
from actio.usage.plans import Plans
from actio_llm import (
    ChatRequest,
    ConnectionFailedError,
    ContextOverflowError,
    Finish,
    FinishReason,
    LLMError,
    ProviderUnavailableError,
    RateLimitedError,
    ReasoningDelta,
    StreamInterruptedError,
    System,
    TextDelta,
    TookTooLongError,
    Usage,
    User,
    prompt_tokens,
)

pytestmark = pytest.mark.anyio

DONE = Finish(FinishReason.STOP, Usage(input_tokens=5, output_tokens=4))
PATIENCE_S = 5.0
"""How long a test waits for the app to get somewhere. It takes milliseconds; this only
keeps a broken app from hanging the test run."""
SECRET = "my door code is 4471"
"""What an error's text may quote: never in the log."""


# --- Helpers --------------------------------------------------------------------------


def chunks(response: httpx.Response) -> list[Any]:
    """The chunks of a stream, in order, without the lines that are no events. The last
    one is the text `[DONE]`."""
    events = [event for event in response.text.split("\n\n") if event.startswith("data: ")]
    payloads = [event.removeprefix("data: ") for event in events]
    return [payload if payload == "[DONE]" else json.loads(payload) for payload in payloads]


def chunk(response: httpx.Response, kind: str) -> dict[str, Any]:
    [found] = [c for c in chunks(response)[:-1] if c["type"] == kind]
    return found  # type: ignore[no-any-return]


def said(response: httpx.Response, kind: str = "text") -> str:
    """The text of an answer's stream, or its reasoning."""
    return "".join(c["delta"] for c in chunks(response)[:-1] if c["type"] == f"{kind}-delta")


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


async def another_guest(browser: httpx.AsyncClient) -> None:
    """Make `browser` a new guest's, on the same app: another account, with a session of
    its own. A request sent before goes on as whoever sent it: wait until it has reached
    the app before this."""
    browser.cookies.clear()
    await browser.post("/api/session/guest")


async def held(model: Any) -> None:
    """Wait until the model has yielded everything before its hold, and waits there."""
    await asyncio.wait_for(model.waiting.wait(), PATIENCE_S)


async def ended(*requests: asyncio.Task[httpx.Response]) -> list[httpx.Response]:
    async with asyncio.timeout(PATIENCE_S):
        return list(await asyncio.gather(*requests))


async def until(condition: Callable[[], bool]) -> None:
    """Wait until `condition` holds: the app gets there in a moment."""
    async with asyncio.timeout(PATIENCE_S):
        while True:
            if condition():
                return
            await asyncio.sleep(0.005)


async def answered_by(chat: Chat, response: httpx.Response) -> Any:
    """Which model the turn's record says wrote its answer, as the stream's last
    metadata says it, which the conversation read afterwards says too: the turn of its
    answer, or of the question it left open. Asserted equal, the whole turn."""
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


# --- Who answers, and what the turn is ------------------------------------------------


async def test_a_model_that_fails_before_its_first_word_is_answered_for_by_its_stand_in(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    plans_with: Any,
    db: AsyncSession,
) -> None:
    """The answer is streamed and stored as any. The turn is the asked model's, and so
    is the price: the stand-in here costs nothing, and the turn a credit a token."""
    assert migrated
    asked = lineup.add("standard", ProviderUnavailableError("busy"), then=["second"])
    stand_in = lineup.add("second", TextDelta("From the stand-in."), DONE)
    plans = plans_with(models=["standard"], free_models=["second"])
    chat = Chat(await guest_on(start_app, settings, lineup, plans))

    response = await chat.ask()

    assert said(response) == "From the stand-in."
    turn = turn_of(response)
    assert (turn["status"], turn["model"], turn["error"], turn["credits"]) == (
        "completed",
        "standard",
        None,
        5 + 4,
    )
    conversation = await chat.read()
    answer = conversation["messages"][1]
    assert answer["parts"] == [{"type": "text", "text": "From the stand-in."}]
    assert answer["metadata"]["turn"] == turn
    assert (len(asked.requests), len(stand_in.requests)) == (1, 1)
    [entry] = (await db.scalars(select(UsageEntry))).all()
    assert (entry.model, entry.credits) == ("standard", 9)


@pytest.mark.parametrize(
    ("its_longest", "allowance"),
    [(400, (300, 10_000)), (64, None)],
    ids=["less was held than either may write", "the stand-in may write less"],
)
async def test_a_stand_in_is_asked_with_a_request_of_its_own_and_no_more_than_was_held(
    its_longest: int,
    allowance: tuple[int, int] | None,
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    plans_with: Any,
) -> None:
    """The same messages, with its own name on its server and its own window; told
    nothing of reasoning when it can't reason; and never told to write more than was
    held for the asked model, nor more than its own longest answer."""
    assert migrated
    asked = lineup.add("standard", ProviderUnavailableError("busy"), then=["second"], thinking=True)
    stand_in = lineup.add(
        "second",
        TextDelta("Fine."),
        DONE,
        context_window=2048,
        max_output_tokens=its_longest,
    )
    plans = plans_with(guest=allowance, models=["standard", "second"]) if allowance else None
    chat = Chat(await guest_on(start_app, settings, lineup, plans))

    response = await chat.ask(thinking=True)

    [asked_first], [asked_then] = asked.requests, stand_in.requests
    held_for_it = asked_first.max_output_tokens
    assert held_for_it is not None
    assert (asked_first.model, asked_first.context_window, asked_first.thinking) == (
        "scripted-standard",
        4096,
        True,
    )
    assert asked_then == ChatRequest(
        model="scripted-second",
        messages=asked_first.messages,
        max_output_tokens=min(held_for_it, its_longest),
        context_window=2048,
        thinking=None,
    )
    if allowance is not None:
        assert held_for_it < its_longest < 512
        assert turn_of(response)["capped_by_usage"] is True
    else:
        assert held_for_it == 512
        assert asked_then.max_output_tokens == its_longest
    assert turn_of(response)["thinking"] is True


@pytest.mark.parametrize(
    ("written", "failure", "code", "kept"),
    [
        (
            TextDelta("Half an "),
            ProviderUnavailableError("busy"),
            "model_unavailable",
            {"type": "text", "text": "Half an "},
        ),
        (
            ReasoningDelta("Let me think"),
            StreamInterruptedError("broke off"),
            "answer_interrupted",
            {"type": "reasoning", "text": "Let me think"},
        ),
    ],
    ids=["after a word", "after a thought alone"],
)
async def test_a_model_that_fails_after_its_first_word_fails_the_turn_and_no_stand_in_is_asked(
    written: Any,
    failure: LLMError,
    code: str,
    kept: dict[str, str],
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
) -> None:
    """A page that has shown a word, or a thought, can't take it back: the answer is
    that model's, and its failure the turn's, with what had arrived."""
    assert migrated
    lineup.add("standard", written, failure, then=["second"])
    stand_in = lineup.add("second", TextDelta("Never."), DONE)
    chat = Chat(await guest_on(start_app, settings, lineup))

    response = await chat.ask()

    turn = turn_of(response)
    assert (turn["status"], turn["error"]) == ("failed", code)
    assert (await chat.read())["messages"][1]["parts"] == [kept]
    assert stand_in.requests == []


@pytest.mark.parametrize(
    ("written", "kept"),
    [
        (TextDelta("Half an "), {"type": "text", "text": "Half an "}),
        (ReasoningDelta("Let me think"), {"type": "reasoning", "text": "Let me think"}),
    ],
    ids=["after a word", "after a thought alone"],
)
async def test_an_answer_cut_at_its_servers_time_is_the_turns_and_no_stand_in_is_asked(
    written: Any,
    kept: dict[str, str],
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
) -> None:
    """What was shown is that model's answer, which reached a limit: the turn completes
    with it, and the model that stands in isn't asked."""
    assert migrated
    asked = lineup.add("standard", written, TookTooLongError("still answering"), then=["second"])
    stand_in = lineup.add("second", TextDelta("Never."), DONE)
    chat = Chat(await guest_on(start_app, settings, lineup))

    response = await chat.ask()

    turn = turn_of(response)
    assert (turn["status"], turn["finish_reason"], turn["error"]) == (
        "completed",
        "time_limit",
        None,
    )
    assert (await chat.read())["messages"][1]["parts"] == [kept]
    assert (len(asked.requests), stand_in.requests) == (1, [])


async def test_a_model_that_wrote_nothing_before_its_servers_time_was_over_is_answered_for(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Its server sent lines with no word in them until its time for an answer was over:
    nothing was shown, and the model that stands in answers, as after any failure before
    the first word. It isn't asked once more. The log names the error's kind, never its
    text."""
    assert migrated
    asked = lineup.add(
        "standard",
        TextDelta(""),
        TookTooLongError(f"still answering, {SECRET}"),
        then=["second"],
    )
    stand_in = lineup.add("second", TextDelta("From the stand-in."), DONE)
    chat = Chat(await guest_on(start_app, settings, lineup))

    response = await chat.ask()

    assert said(response) == "From the stand-in."
    turn = turn_of(response)
    assert (turn["status"], turn["finish_reason"], turn["error"]) == ("completed", "stop", None)
    assert (len(asked.requests), len(stand_in.requests)) == (1, 1)
    assert told(caplog, turn["id"]) == [
        f"turn {turn['id']}: model standard failed (TookTooLongError), and second is asked next",
        f"turn {turn['id']}: model second answered in place of standard",
    ]
    assert SECRET not in caplog.text


async def test_a_stand_ins_answer_cut_at_its_servers_time_is_said_of_the_stand_in(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The time that was over is the server's of the model that was writing. The turn is
    the asked model's, as ever, and the log names the one that answered."""
    assert migrated
    lineup.add("standard", ProviderUnavailableError("busy"), then=["second"])
    lineup.add("second", TextDelta("Half an "), TookTooLongError(f"still answering, {SECRET}"))
    chat = Chat(await guest_on(start_app, settings, lineup))

    response = await chat.ask()

    turn = turn_of(response)
    assert (turn["model"], turn["status"], turn["finish_reason"]) == (
        "standard",
        "completed",
        "time_limit",
    )
    assert told(caplog, turn["id"]) == [
        f"turn {turn['id']}: model standard failed (ProviderUnavailableError),"
        " and second is asked next",
        f"turn {turn['id']}: model second was still writing when its server's time for an"
        " answer was over, and the answer stops here",
        f"turn {turn['id']}: model second answered in place of standard",
    ]
    assert SECRET not in caplog.text


@pytest.mark.parametrize(
    ("failures", "code"),
    [
        (
            (ProviderUnavailableError("busy"), ContextOverflowError("too long")),
            "model_unavailable",
        ),
        (
            (ContextOverflowError("too long"), ProviderUnavailableError("busy")),
            "model_unavailable",
        ),
        (
            (ContextOverflowError("too long"), ContextOverflowError("too long too")),
            "context_overflow",
        ),
    ],
    ids=["busy, then too long", "too long, then busy", "too long twice"],
)
async def test_when_every_model_failed_the_turn_records_the_error_another_try_may_not_meet(
    failures: tuple[LLMError, LLMError],
    code: str,
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
) -> None:
    """A model too busy may answer the same question a moment later: that is what the
    reader is told, whichever model failed so."""
    assert migrated
    asked = lineup.add("standard", failures[0], then=["second"])
    stand_in = lineup.add("second", failures[1])
    chat = Chat(await guest_on(start_app, settings, lineup))

    response = await chat.ask()

    turn = turn_of(response)
    assert (turn["status"], turn["error"]) == ("failed", code)
    assert (await chat.read())["unanswered"]["error"] == code
    assert (len(asked.requests), len(stand_in.requests)) == (1, 1)


async def test_a_stand_in_that_reaches_the_end_of_its_own_window_says_so(
    start_app: Any, settings: Settings, migrated: str, lineup: Any
) -> None:
    """Its own window, 2,048 here, not the asked model's 4,096: what was read and written
    together is at its end."""
    assert migrated
    lineup.add("standard", ProviderUnavailableError("busy"), then=["second"])
    lineup.add(
        "second",
        TextDelta("One two"),
        Finish(FinishReason.LENGTH, Usage(input_tokens=2_000, output_tokens=48)),
        context_window=2048,
    )
    chat = Chat(await guest_on(start_app, settings, lineup))

    response = await chat.ask()

    assert turn_of(response)["finish_reason"] == "context_window"
    assert chunk(response, "finish")["finishReason"] == "length"


# --- Which model answered, in the turn's record ---------------------------------------


@pytest.mark.parametrize(
    ("asked", "stand_in", "answered", "has_answer"),
    [
        pytest.param(
            [TextDelta("Mine."), DONE],
            [TextDelta("Never."), DONE],
            "standard",
            True,
            id="the asked model's own answer",
        ),
        pytest.param(
            [ProviderUnavailableError("busy")],
            [TextDelta("From the stand-in."), DONE],
            "second",
            True,
            id="a stand-in's answer",
        ),
        pytest.param(
            None,
            [TextDelta("From the stand-in."), DONE],
            "second",
            True,
            id="the asked model passed over, not ready",
        ),
        pytest.param(
            [ProviderUnavailableError("busy")],
            [ContextOverflowError("too long")],
            None,
            False,
            id="every model failed before a word",
        ),
        pytest.param(
            [ProviderUnavailableError("busy")],
            [TextDelta("Half an "), ProviderUnavailableError("gone")],
            "second",
            True,
            id="a stand-in that failed after a word",
        ),
        pytest.param(
            [ProviderUnavailableError("busy")],
            [ReasoningDelta("Let me think"), StreamInterruptedError("broke off")],
            "second",
            True,
            id="a stand-in that failed after a thought alone",
        ),
        pytest.param(
            [ProviderUnavailableError("busy")],
            [TextDelta("Half an "), TookTooLongError("still answering")],
            "second",
            True,
            id="a stand-in's answer cut at its time",
        ),
        pytest.param(
            [ProviderUnavailableError("busy")],
            [Finish(FinishReason.STOP, Usage(input_tokens=5, output_tokens=0))],
            "second",
            False,
            id="a stand-in that finished and wrote nothing",
        ),
    ],
)
async def test_a_turns_record_names_the_model_that_wrote_its_answer(
    asked: list[Any] | None,
    stand_in: list[Any],
    answered: str | None,
    has_answer: bool,
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
) -> None:
    """Whichever ending it is recorded with: the model that handed something on, a word,
    a thought or its last event, and none when nothing was. The stream's last metadata
    says it, and so does the conversation read afterwards. The stream's first said none:
    nobody had answered yet. The turn is the asked model's all the same."""
    assert migrated
    if asked is None:
        lineup.add("standard", state="unreachable", then=["second"])
    else:
        lineup.add("standard", *asked, then=["second"])
    lineup.add("second", *stand_in)
    chat = Chat(await guest_on(start_app, settings, lineup))

    response = await chat.ask()

    assert await answered_by(chat, response) == answered
    assert turn_of(response)["model"] == "standard"
    assert chunk(response, "start")["messageMetadata"]["turn"]["answered_by"] is None
    conversation = await chat.read()
    assert (conversation["unanswered"] is None) == has_answer


# --- Gates ----------------------------------------------------------------------------


def two_servers(lineup: Any, *stand_in_script: Any) -> tuple[Gate, Gate, Any, Any, asyncio.Event]:
    """The asked model on one server, which fails it at once, and its stand-in on
    another, which is busy with a long answer of `other` until the event is set. Each
    server writes one answer at a time. Returns the two gates, the stand-in, `other`,
    and the event."""
    first_gate, second_gate, hold = Gate(1), Gate(1), asyncio.Event()
    lineup.add("standard", ProviderUnavailableError("busy"), then=["second"], gate=first_gate)
    stand_in = lineup.add("second", *stand_in_script, gate=second_gate)
    other = lineup.add(
        "other", TextDelta("A long "), hold, TextDelta("answer."), DONE, gate=second_gate
    )
    return first_gate, second_gate, stand_in, other, hold


async def test_a_turn_gives_its_place_at_the_first_gate_back_before_it_waits_at_the_next(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two turns whose models' chains cross would otherwise each keep one server while
    they wait for the other. Here a third model, on the first server, is answered at
    once while the turn waits at the second."""
    assert migrated
    first_gate, second_gate, stand_in, other, hold = two_servers(
        lineup, TextDelta("At last."), DONE
    )
    lineup.add("beside", TextDelta("At once."), DONE, gate=first_gate)
    browser = await guest_on(start_app, settings, lineup)
    asked_other = Chat(browser).asking(model="other")
    await held(other)
    asked = Chat(browser).asking()
    await until(lambda: second_gate.waiting == 1)

    first_free = not first_gate.full
    [beside] = await ended(Chat(browser).asking(model="beside"))
    asked_meanwhile = list(stand_in.requests)
    hold.set()
    _, response = await ended(asked_other, asked)

    assert first_free
    assert said(beside) == "At once."
    assert asked_meanwhile == []
    assert said(response) == "At last."
    turn_id = chunk(response, "start")["messageMetadata"]["turn"]["id"]
    assert f"turn {turn_id}: waited 0.0 s for the model's server" in caplog.text


async def test_a_turn_stopped_while_it_waits_at_its_stand_ins_gate_is_stopped(
    start_app: Any, settings: Settings, migrated: str, lineup: Any, db: AsyncSession
) -> None:
    """Nothing was written, and nothing is charged: the stand-in never hears of it."""
    assert migrated
    _, second_gate, stand_in, other, hold = two_servers(lineup, TextDelta("Never."), DONE)
    browser = await guest_on(start_app, settings, lineup)
    asked_other = Chat(browser).asking(model="other")
    await held(other)
    chat = Chat(browser)
    asked = chat.asking()
    await until(lambda: second_gate.waiting == 1)

    stopped = await chat.stop(await chat.running_turn())
    waiting_afterwards = second_gate.waiting
    hold.set()
    _, response = await ended(asked_other, asked)

    assert (stopped.json()["status"], stopped.json()["credits"]) == ("stopped", None)
    assert waiting_afterwards == 0
    assert stand_in.requests == []
    assert said(response) == ""
    assert await db.scalar(select(func.count()).select_from(UsageEntry)) == 1  # `other`'s
    # Nobody had answered: the record names no model, in the stop's answer too.
    assert stopped.json()["answered_by"] is None
    assert await answered_by(chat, response) is None


async def test_a_turn_waits_at_its_stand_ins_gate_as_the_answer_of_the_account_that_asked_it(
    start_app: Any, settings: Settings, migrated: str, lineup: Any
) -> None:
    """A guest's long answer holds the stand-in's server, and the guest's next turn,
    whose model fails at once, waits there for the stand-in. Another guest asks a model
    on that server after it, and is answered first once the long answer ends: the turn
    waits as the answer of its guest, who was given a place there, and the other guest
    never was. Waiting as anyone else's, as an account never given a place, it would
    pass first, having come first."""
    assert migrated
    order: list[str] = []
    _, second_gate, _, other, hold = two_servers(
        lineup, lambda: order.append("the stand-in"), TextDelta("At last."), DONE
    )
    lineup.add(
        "beside", lambda: order.append("theirs"), TextDelta("Theirs."), DONE, gate=second_gate
    )
    browser = await guest_on(start_app, settings, lineup)
    asked_other = Chat(browser).asking(model="other")
    await held(other)
    asked = Chat(browser).asking()
    await until(lambda: second_gate.waiting == 1)
    await another_guest(browser)
    asked_theirs = Chat(browser).asking(model="beside")
    await until(lambda: second_gate.waiting == 2)

    hold.set()
    _, response, theirs = await ended(asked_other, asked, asked_theirs)

    assert order == ["theirs", "the stand-in"]
    assert said(response) == "At last."
    assert said(theirs) == "Theirs."


async def test_a_turn_stopped_while_its_stand_in_writes_keeps_what_had_arrived(
    start_app: Any, settings: Settings, migrated: str, lineup: Any
) -> None:
    assert migrated
    hold = asyncio.Event()
    lineup.add("standard", ProviderUnavailableError("busy"), then=["second"])
    stand_in = lineup.add("second", TextDelta("Half an "), hold, TextDelta("answer."), DONE)
    browser = await guest_on(start_app, settings, lineup)
    chat = Chat(browser)
    asked = chat.asking()
    await held(stand_in)

    stopped = await chat.stop(await chat.running_turn())
    [response] = await ended(asked)

    assert stopped.json()["status"] == "stopped"
    assert said(response) == "Half an "
    assert (await chat.read())["messages"][1]["parts"] == [{"type": "text", "text": "Half an "}]
    assert stand_in.closed.is_set()
    # What had arrived is the stand-in's, and the record says so, in the stop's answer too.
    assert stopped.json()["answered_by"] == "second"
    assert await answered_by(chat, response) == "second"


# --- A model asked once more ----------------------------------------------------------


@pytest.mark.parametrize(
    ("failure", "after"),
    [
        (ConnectionFailedError(f"can't reach standard: {SECRET}"), ""),
        (RateLimitedError(f"429: {SECRET}", retry_after_s=0.01), " after 0.01 s"),
    ],
    ids=["a connection that failed at once", "a server that asks for a short wait"],
)
async def test_a_model_whose_failure_may_be_gone_in_a_moment_is_asked_once_more_and_answers(
    failure: LLMError,
    after: str,
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    once: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """With the same request, before anything else: the answer is streamed and stored
    once, and the turn completes. The log says that it was asked once more, by the
    error's kind, and says of no failure."""
    assert migrated
    model = lineup.add("standard", once(failure), TextDelta("Hello."), DONE)
    chat = Chat(await guest_on(start_app, settings, lineup))

    response = await chat.ask()

    turn = turn_of(response)
    assert (turn["status"], turn["model"], turn["error"]) == ("completed", "standard", None)
    assert said(response) == "Hello."
    assert [c["type"] for c in chunks(response)[:-1]].count("text-start") == 1
    messages = (await chat.read())["messages"]
    assert [message["parts"] for message in messages[1:]] == [[{"type": "text", "text": "Hello."}]]
    first, second = model.requests
    assert first == second
    assert told(caplog, turn["id"]) == [
        f"turn {turn['id']}: model standard failed ({type(failure).__name__}), and is asked"
        f" once more{after}"
    ]
    assert SECRET not in caplog.text


async def test_a_model_that_fails_so_twice_fails_the_turn_when_none_stands_in(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Asked twice, and no more: the second failure is the turn's."""
    assert migrated
    model = lineup.add("standard", ConnectionFailedError("can't reach standard"))
    chat = Chat(await guest_on(start_app, settings, lineup))

    response = await chat.ask()

    turn = turn_of(response)
    assert (turn["status"], turn["error"]) == ("failed", "model_unavailable")
    assert len(model.requests) == 2
    assert told(caplog, turn["id"]) == [
        f"turn {turn['id']}: model standard failed (ConnectionFailedError), and is asked once more",
        f"turn {turn['id']}: the model failed (ConnectionFailedError), recorded as"
        " model_unavailable",
    ]


async def test_a_model_that_fails_so_twice_is_answered_for_by_its_stand_in(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert migrated
    asked = lineup.add("standard", RateLimitedError("429", retry_after_s=0.01), then=["second"])
    stand_in = lineup.add("second", TextDelta("From the stand-in."), DONE)
    chat = Chat(await guest_on(start_app, settings, lineup))

    response = await chat.ask()

    turn = turn_of(response)
    assert (turn["status"], turn["model"]) == ("completed", "standard")
    assert said(response) == "From the stand-in."
    assert (len(asked.requests), len(stand_in.requests)) == (2, 1)
    assert told(caplog, turn["id"]) == [
        f"turn {turn['id']}: model standard failed (RateLimitedError), and is asked once more"
        " after 0.01 s",
        f"turn {turn['id']}: model standard failed (RateLimitedError), and second is asked next",
        f"turn {turn['id']}: model second answered in place of standard",
    ]


async def test_a_turn_stopped_while_its_model_waits_to_be_asked_once_more_is_stopped(
    start_app: Any, settings: Settings, migrated: str, lineup: Any
) -> None:
    """The server asked for two seconds, the most that is waited: the stop doesn't wait
    them out. The model isn't asked again, and nothing is charged."""
    assert migrated
    model = lineup.add(
        "standard", RateLimitedError("429", retry_after_s=2), TextDelta("Never."), DONE
    )
    chat = Chat(await guest_on(start_app, settings, lineup))
    asked = chat.asking()
    await asyncio.wait_for(model.closed.wait(), PATIENCE_S)

    async with asyncio.timeout(0.9):
        stopped = await chat.stop(await chat.running_turn())
        [response] = await ended(asked)

    assert (stopped.json()["status"], stopped.json()["credits"]) == ("stopped", None)
    assert turn_of(response)["status"] == "stopped"
    assert len(model.requests) == 1


# --- Models that aren't ready ---------------------------------------------------------


async def test_a_model_that_isnt_ready_is_passed_over_for_its_stand_in(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    plans_with: Any,
    db: AsyncSession,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Not refused: the stand-in answers, and the model that isn't ready is never asked.
    The turn is the asked model's all the same, though that one was never among those
    asked: its price, where the stand-in costs nothing, and its name in the log."""
    assert migrated
    asked = lineup.add("standard", TextDelta("Never."), DONE, state="unreachable", then=["second"])
    lineup.add("second", TextDelta("From the stand-in."), DONE)
    plans = plans_with(models=["standard"], free_models=["second"])
    chat = Chat(await guest_on(start_app, settings, lineup, plans))

    response = await chat.ask()

    assert response.status_code == 200
    assert said(response) == "From the stand-in."
    turn = turn_of(response)
    assert (turn["status"], turn["model"], turn["credits"]) == ("completed", "standard", 5 + 4)
    assert asked.requests == []
    [entry] = (await db.scalars(select(UsageEntry))).all()
    assert (entry.model, entry.credits) == ("standard", 9)
    assert told(caplog, turn["id"]) == [
        f"turn {turn['id']}: model standard is unreachable, and is passed over",
        f"turn {turn['id']}: model second answered in place of standard",
    ]


async def test_a_stand_in_that_isnt_ready_is_passed_over_for_the_next(
    start_app: Any, settings: Settings, migrated: str, lineup: Any
) -> None:
    assert migrated
    lineup.add("standard", ProviderUnavailableError("busy"), then=["second", "third"])
    missing = lineup.add("second", TextDelta("Never."), DONE, state="missing")
    lineup.add("third", TextDelta("From the third."), DONE)
    chat = Chat(await guest_on(start_app, settings, lineup))

    response = await chat.ask()

    assert said(response) == "From the third."
    assert missing.requests == []


async def test_when_none_of_the_models_is_ready_the_turn_is_refused_with_the_asked_ones_state(
    start_app: Any, settings: Settings, migrated: str, lineup: Any, db: AsyncSession
) -> None:
    assert migrated
    lineup.add("standard", state="missing", then=["second"])
    lineup.add("second", state="unreachable")
    chat = Chat(await guest_on(start_app, settings, lineup))

    response = await chat.ask()

    assert response.status_code == 503
    assert response.json() == {
        "type": "about:blank",
        "title": "Service Unavailable",
        "status": 503,
        "detail": "This model isn't on its server yet.",
        "model": "standard",
        "model_status": "missing",
    }
    assert await db.scalar(select(func.count()).select_from(Conversation)) == 0


@pytest.mark.parametrize(
    ("stand_in_reasons", "answered"), [(True, 200), (False, 422)], ids=["can", "can't"]
)
async def test_whether_a_turn_may_ask_for_reasoning_is_the_first_ready_models_to_say(
    stand_in_reasons: bool,
    answered: int,
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    db: AsyncSession,
) -> None:
    """The asked model isn't ready here, and could reason: its stand-in is asked first."""
    assert migrated
    lineup.add("standard", state="unreachable", thinking=True, then=["second"])
    stand_in = lineup.add("second", TextDelta("Fine."), DONE, thinking=stand_in_reasons)
    chat = Chat(await guest_on(start_app, settings, lineup))

    response = await chat.ask(thinking=True)

    assert response.status_code == answered
    if answered == 422:
        assert response.json()["detail"] == "This model can't reason before it answers."
        assert response.json()["model"] == "standard"
        assert await db.scalar(select(func.count()).select_from(Conversation)) == 0
    else:
        assert [request.thinking for request in stand_in.requests] == [True]


async def test_a_turn_may_not_ask_for_reasoning_because_a_later_model_could_reason(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    db: AsyncSession,
) -> None:
    """Both are ready here. The asked one can't reason, and is asked first: that its
    stand-in could doesn't make the turn one that may ask for reasoning."""
    assert migrated
    asked = lineup.add("standard", TextDelta("Fine."), DONE, then=["second"])
    stand_in = lineup.add("second", TextDelta("Fine."), DONE, thinking=True)
    chat = Chat(await guest_on(start_app, settings, lineup))

    response = await chat.ask(thinking=True)

    assert response.status_code == 422
    assert response.json()["detail"] == "This model can't reason before it answers."
    assert await db.scalar(select(func.count()).select_from(Conversation)) == 0
    assert (asked.requests, stand_in.requests) == ([], [])


# --- Models whose room is smaller than the request ------------------------------------

LONG = f"Remember: {SECRET}. " + "spoke " * 1500
"""A question of about 1,900 tokens by Actio's guess, with the instructions: more than a
room of 1,000, and less than one of 3,000."""


def room_line(turn_id: str, model: str, room: int, request: ChatRequest) -> str:
    """The log's line for a model passed over for its room, by the guess at the request
    as it was sent to the model that was asked."""
    return (
        f"turn {turn_id}: model {model} has room for {room} tokens, and the request is about"
        f" {prompt_tokens(request.messages)}: it is passed over"
    )


@pytest.mark.parametrize(
    "too_small",
    [{"tokens_a_minute": 1000}, {"context_window": 1000}],
    ids=["by what its server takes in a minute", "by its window alone"],
)
async def test_a_model_whose_room_is_smaller_than_the_request_is_passed_over_for_one_with_room(
    too_small: dict[str, int],
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    plans_with: Any,
    db: AsyncSession,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Never asked: its stand-in answers. The turn is the asked model's all the same, at
    its price, where the stand-in costs nothing. The stand-in is asked with its own
    window, not its room. The log says it with numbers alone: the guess is made of the
    very messages the stand-in was sent, the instructions with the question."""
    assert migrated
    asked = lineup.add("standard", TextDelta("Never."), DONE, then=["second"], **too_small)
    stand_in = lineup.add("second", TextDelta("From the stand-in."), DONE, tokens_a_minute=3000)
    plans = plans_with(models=["standard"], free_models=["second"])
    chat = Chat(await guest_on(start_app, settings, lineup, plans))

    response = await chat.ask(LONG)

    assert said(response) == "From the stand-in."
    turn = turn_of(response)
    assert (turn["status"], turn["model"], turn["credits"]) == ("completed", "standard", 5 + 4)
    assert await answered_by(chat, response) == "second"
    assert asked.requests == []
    [entry] = (await db.scalars(select(UsageEntry))).all()
    assert (entry.model, entry.credits) == ("standard", 9)
    [request] = stand_in.requests
    assert request == ChatRequest(
        model="scripted-second",
        messages=request.messages,
        max_output_tokens=512,
        context_window=4096,
        thinking=None,
    )
    assert [type(message) for message in request.messages] == [System, User]
    assert told(caplog, turn["id"]) == [
        room_line(turn["id"], "standard", 1000, request),
        f"turn {turn['id']}: model second answered in place of standard",
    ]
    assert SECRET not in caplog.text


@pytest.mark.parametrize(
    ("asked_script", "stand_in_script", "outcome", "asks"),
    [
        (
            [TextDelta("Mine."), DONE],
            [TextDelta("Never."), DONE],
            ("completed", None, "standard"),
            (1, 0),
        ),
        (
            [ContextOverflowError("too long")],
            [ContextOverflowError("too long too")],
            ("failed", "context_overflow", None),
            (1, 1),
        ),
        ([ContextOverflowError("too long")], None, ("failed", "context_overflow", None), (1, 0)),
    ],
    ids=["the asked model answers", "each refuses it", "a model alone"],
)
async def test_when_no_model_has_room_for_the_request_each_is_asked_as_ever(
    asked_script: list[Any],
    stand_in_script: list[Any] | None,
    outcome: tuple[str, str | None, str | None],
    asks: tuple[int, int],
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A guess never refuses a turn: the ready models are asked in order, as if none had
    a room, and what they do is the turn's outcome. The log says nothing of rooms."""
    assert migrated
    asked = lineup.add(
        "standard",
        *asked_script,
        then=[] if stand_in_script is None else ["second"],
        tokens_a_minute=1000,
    )
    stand_in = lineup.add("second", *(stand_in_script or []), context_window=1000)
    chat = Chat(await guest_on(start_app, settings, lineup))

    response = await chat.ask(LONG)

    turn = turn_of(response)
    assert (turn["status"], turn["error"], turn["answered_by"]) == outcome
    assert (len(asked.requests), len(stand_in.requests)) == asks
    assert [line for line in told(caplog, turn["id"]) if "has room for" in line] == []


async def test_a_stand_in_without_room_is_passed_over_and_the_asked_models_failure_is_the_turns(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The asked model has room, and fails before its first word: no other is left to
    ask, and the stand-in never hears of the question."""
    assert migrated
    asked = lineup.add("standard", ProviderUnavailableError(f"busy, {SECRET}"), then=["second"])
    stand_in = lineup.add("second", TextDelta("Never."), DONE, tokens_a_minute=1000)
    chat = Chat(await guest_on(start_app, settings, lineup))

    response = await chat.ask(LONG)

    turn = turn_of(response)
    assert (turn["status"], turn["error"], turn["answered_by"]) == (
        "failed",
        "model_unavailable",
        None,
    )
    [request] = asked.requests
    assert stand_in.requests == []
    assert told(caplog, turn["id"]) == [
        room_line(turn["id"], "second", 1000, request),
        f"turn {turn['id']}: the model failed (ProviderUnavailableError), recorded as"
        " model_unavailable",
    ]
    assert SECRET not in caplog.text


async def test_of_three_models_the_one_without_room_is_never_asked_and_the_next_answers(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The first fails before its first word, and the walk goes on to the third, as if
    the middle one weren't in the chain."""
    assert migrated
    first = lineup.add("standard", ProviderUnavailableError("busy"), then=["second", "third"])
    middle = lineup.add("second", TextDelta("Never."), DONE, tokens_a_minute=1000)
    third = lineup.add("third", TextDelta("From the third."), DONE)
    chat = Chat(await guest_on(start_app, settings, lineup))

    response = await chat.ask(LONG)

    assert said(response) == "From the third."
    assert await answered_by(chat, response) == "third"
    assert (len(first.requests), middle.requests, len(third.requests)) == (1, [], 1)
    turn_id = turn_of(response)["id"]
    assert told(caplog, turn_id) == [
        room_line(turn_id, "second", 1000, third.requests[0]),
        f"turn {turn_id}: model standard failed (ProviderUnavailableError), and third is asked"
        " next",
        f"turn {turn_id}: model third answered in place of standard",
    ]


async def test_a_model_with_room_for_the_request_alone_is_asked_whatever_its_answer_may_need(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The guess is of the prompt alone. The asked model has room for the request, and
    not for the request with the longest answer it may write, 512 tokens here: it is
    asked all the same, though its stand-in has room for both."""
    assert migrated
    asked = lineup.add("standard", TextDelta("Mine."), DONE, then=["second"], tokens_a_minute=2200)
    stand_in = lineup.add("second", TextDelta("Never."), DONE)
    chat = Chat(await guest_on(start_app, settings, lineup))

    response = await chat.ask(LONG)

    assert said(response) == "Mine."
    [request] = asked.requests
    assert stand_in.requests == []
    # What the test rests on: the request fits the room, and not with the answer's room.
    guess = prompt_tokens(request.messages)
    assert guess <= 2200 < guess + request.max_output_tokens
    assert request.max_output_tokens == 512
    assert told(caplog, turn_of(response)["id"]) == []


@pytest.mark.parametrize(
    ("first_reasons", "answered"), [(True, 200), (False, 422)], ids=["it can", "it can't"]
)
async def test_whether_a_turn_may_ask_for_reasoning_is_the_first_ready_models_without_room_too(
    first_reasons: bool,
    answered: int,
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    db: AsyncSession,
) -> None:
    """The first ready model has no room here, and its stand-in can reason only when the
    first can't. When the first can, the turn starts, and the stand-in answers, told
    nothing of reasoning. When it can't, the 422, as for a model that is asked: that a
    guess passes it over changes nothing of what a turn may ask."""
    assert migrated
    asked = lineup.add(
        "standard",
        TextDelta("Never."),
        DONE,
        then=["second"],
        thinking=first_reasons,
        tokens_a_minute=1000,
    )
    stand_in = lineup.add("second", TextDelta("Fine."), DONE, thinking=not first_reasons)
    chat = Chat(await guest_on(start_app, settings, lineup))

    response = await chat.ask(LONG, thinking=True)

    assert response.status_code == answered
    assert asked.requests == []
    if answered == 422:
        assert response.json()["detail"] == "This model can't reason before it answers."
        assert response.json()["model"] == "standard"
        assert await db.scalar(select(func.count()).select_from(Conversation)) == 0
        assert stand_in.requests == []
    else:
        assert said(response) == "Fine."
        assert [request.thinking for request in stand_in.requests] == [None]
        assert turn_of(response)["thinking"] is True


async def test_a_model_that_isnt_ready_and_one_without_room_are_each_said_and_the_third_answers(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert migrated
    asked = lineup.add(
        "standard", TextDelta("Never."), DONE, state="unreachable", then=["second", "third"]
    )
    middle = lineup.add("second", TextDelta("Never."), DONE, tokens_a_minute=1000)
    third = lineup.add("third", TextDelta("From the third."), DONE)
    chat = Chat(await guest_on(start_app, settings, lineup))

    response = await chat.ask(LONG)

    assert said(response) == "From the third."
    assert (asked.requests, middle.requests, len(third.requests)) == ([], [], 1)
    turn_id = turn_of(response)["id"]
    assert told(caplog, turn_id) == [
        f"turn {turn_id}: model standard is unreachable, and is passed over",
        room_line(turn_id, "second", 1000, third.requests[0]),
        f"turn {turn_id}: model third answered in place of standard",
    ]
    assert SECRET not in caplog.text


# --- The log --------------------------------------------------------------------------


async def test_the_log_says_which_model_was_passed_over_and_which_answered_never_an_errors_text(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    once: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An error's text can quote the question, or name the account at its provider: the
    log names its kind, also of a model asked once more."""
    assert migrated
    lineup.add("standard", ConnectionFailedError(f"refused, {SECRET}"), then=["second", "third"])
    lineup.add("second", state="missing")
    lineup.add(
        "third",
        once(RateLimitedError(f"slow down, {SECRET}", retry_after_s=0.01)),
        TextDelta("From the third."),
        DONE,
    )
    chat = Chat(await guest_on(start_app, settings, lineup))

    response = await chat.ask(f"Remember: {SECRET}")

    turn_id = turn_of(response)["id"]
    assert told(caplog, turn_id) == [
        f"turn {turn_id}: model second is missing, and is passed over",
        f"turn {turn_id}: model standard failed (ConnectionFailedError), and is asked once more",
        f"turn {turn_id}: model standard failed (ConnectionFailedError), and third is asked next",
        f"turn {turn_id}: model third failed (RateLimitedError), and is asked once more after"
        " 0.01 s",
        f"turn {turn_id}: model third answered in place of standard",
    ]
    assert SECRET not in caplog.text


@pytest.mark.parametrize("chain", [1, 2], ids=["one model", "a model and its stand-in"])
async def test_the_log_says_each_model_that_failed_and_how_the_turn_was_recorded(
    chain: int,
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A model with no stand-in says it as ever, in one line."""
    assert migrated
    lineup.add(
        "standard",
        ProviderUnavailableError(f"busy, {SECRET}"),
        then=["second"] if chain == 2 else [],
    )
    lineup.add("second", ContextOverflowError(f"too long, {SECRET}"))
    chat = Chat(await guest_on(start_app, settings, lineup))

    response = await chat.ask()

    turn_id = turn_of(response)["id"]
    recorded = f"turn {turn_id}: the model failed (ProviderUnavailableError), recorded as model_unavailable"
    assert told(caplog, turn_id) == (
        [recorded]
        if chain == 1
        else [
            f"turn {turn_id}: model standard failed (ProviderUnavailableError), and second is"
            " asked next",
            f"turn {turn_id}: model second failed (ContextOverflowError), and no other is left"
            " to ask",
            recorded,
        ]
    )
    assert SECRET not in caplog.text
