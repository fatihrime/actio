"""Turns that run apart from their request: read along with again, stopped, kept alive
while they are written, and ended by a shutdown.

The app runs in process, and a request's response comes back whole, once its stream has
ended. So a test starts a request as a task, does what it is about while the model is
held half-way (`ScriptedModel`), and looks at the response afterwards. What takes a real
connection, a reader who leaves in the middle, is in `test_turns_live.py`.
"""

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from valkey.asyncio import Valkey

from actio.api import turns as turn_routes
from actio.api import ui_stream
from actio.chat import events as turn_events
from actio.chat import running, turns
from actio.chat.events import EventsUnavailableError, TurnEvents
from actio.chat.models import Conversation, Message, Turn
from actio.chat.parts import TextPart
from actio.core.ids import new_id
from actio.core.settings import Settings
from actio.usage.models import UsageEntry
from actio_llm import Finish, FinishReason, ReasoningDelta, TextDelta, Usage

pytestmark = pytest.mark.anyio

DONE = Finish(FinishReason.STOP, Usage(input_tokens=5, output_tokens=4))
PATIENCE_S = 5.0
"""How long a test waits for the app to get somewhere. It takes milliseconds; this only
keeps a broken app from hanging the test run."""


# --- Helpers --------------------------------------------------------------------------


def chunks(response: httpx.Response) -> list[Any]:
    """The chunks of a stream, in order, without the lines that are no events. The last
    one is the text `[DONE]`."""
    events = [event for event in response.text.split("\n\n") if event.startswith("data: ")]
    payloads = [event.removeprefix("data: ") for event in events]
    return [payload if payload == "[DONE]" else json.loads(payload) for payload in payloads]


def kinds(response: httpx.Response) -> list[str]:
    return [chunk if isinstance(chunk, str) else chunk["type"] for chunk in chunks(response)]


def chunk(response: httpx.Response, kind: str) -> dict[str, Any]:
    [found] = [c for c in chunks(response)[:-1] if c["type"] == kind]
    return found  # type: ignore[no-any-return]


def said(response: httpx.Response) -> str:
    return "".join(c["delta"] for c in chunks(response)[:-1] if c["type"] == "text-delta")


class Chat:
    """One conversation, as a browser would hold it."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.id = new_id()
        self.path = f"/api/conversations/{self.id}"
        self.question = new_id()

    def asking(self, text: str = "Tell me something.") -> asyncio.Task[httpx.Response]:
        """Ask, and go on without waiting: the response comes when its stream has ended."""
        message = {"id": str(self.question), "parts": [{"type": "text", "text": text}]}
        return asyncio.create_task(
            self.client.post(f"{self.path}/turns", json={"message": message})
        )

    def reading_along(self, turn_id: str) -> asyncio.Task[httpx.Response]:
        return asyncio.create_task(self.client.get(f"{self.path}/turns/{turn_id}/stream"))

    async def read_along(self, turn_id: object) -> httpx.Response:
        """What reading along answers, for a turn whose stream isn't expected to go on:
        a refusal, or a stream that ends at once. One that went on would never return."""
        reading = self.client.get(f"{self.path}/turns/{turn_id}/stream")
        return await asyncio.wait_for(reading, PATIENCE_S)

    async def stop(self, turn_id: str) -> httpx.Response:
        return await self.client.post(f"{self.path}/turns/{turn_id}/cancel")

    async def read(self) -> dict[str, Any]:
        response = await self.client.get(self.path)
        assert response.status_code == 200, response.text
        return response.json()  # type: ignore[no-any-return]

    async def running_turn(self) -> str:
        """The id of the turn that is being written, as the conversation names it."""
        unanswered = (await self.read())["unanswered"]
        assert unanswered["status"] == "running"
        return unanswered["id"]  # type: ignore[no-any-return]


async def held(model: Any) -> None:
    """Wait until the model has yielded everything before its hold, and waits there."""
    await asyncio.wait_for(model.waiting.wait(), PATIENCE_S)


async def ended(*requests: asyncio.Task[httpx.Response]) -> list[httpx.Response]:
    async with asyncio.timeout(PATIENCE_S):
        return list(await asyncio.gather(*requests))


async def count(db: AsyncSession, table: type) -> int:
    return await db.scalar(select(func.count()).select_from(table)) or 0


async def the_turn(db: AsyncSession) -> Turn:
    [turn] = (await db.scalars(select(Turn).execution_options(populate_existing=True))).all()
    return turn


async def said_alive_at(db: AsyncSession, moment: datetime) -> bool:
    """Whether the turn last said it was alive at `moment`."""
    alive_at = (await the_turn(db)).alive_at
    await db.commit()  # the next look starts from what the database has then
    return alive_at == moment


async def beaten(happened: Callable[[], Awaitable[bool]]) -> None:
    """Wait for what the turn's next beat does."""
    async with asyncio.timeout(PATIENCE_S):
        while True:
            if await happened():
                return
            await asyncio.sleep(0.005)


async def left_running(db: AsyncSession, chat: Chat, clock: Any) -> Turn:
    """A turn that is marked `running` and that no process is writing: what a process
    leaves behind when it is killed."""
    me = uuid.UUID((await chat.client.get("/api/me")).json()["id"])
    begun = await turns.ask(
        db,
        user_id=me,
        conversation_id=chat.id,
        message_id=chat.question,
        parent_id=None,
        parts=[TextPart(text="Asked just before the server died")],
        model="standard",
        thinking=False,
        turn_id=new_id(),
        now=clock.now,
    )
    await db.commit()
    return begun.turn


# --- Reading along --------------------------------------------------------------------


async def test_an_answer_that_is_being_written_can_be_read_along_with_from_its_beginning(
    scripted: Any,
) -> None:
    hold = asyncio.Event()
    model, browser = await scripted(TextDelta("Half an "), hold, TextDelta("answer."), DONE)
    chat = Chat(browser)
    asked = chat.asking()
    await held(model)

    joined = chat.reading_along(await chat.running_turn())
    again = chat.reading_along(await chat.running_turn())
    await asyncio.sleep(0.05)  # both are reading before the rest is written
    hold.set()
    first, second, third = await ended(asked, joined, again)

    assert second.status_code == 200
    assert second.headers["content-type"] == "text/event-stream; charset=utf-8"
    assert second.headers["x-vercel-ai-ui-message-stream"] == "v1"
    assert kinds(first)[:3] == ["start", "start-step", "text-start"]
    assert said(first) == "Half an answer."
    # Whoever joins gets what the first reader got, byte for byte, from `start` on.
    assert first.text == second.text == third.text
    assert chunk(first, "finish")["messageMetadata"]["turn"]["status"] == "completed"


async def test_an_answer_is_written_once_however_many_read_along(scripted: Any) -> None:
    hold = asyncio.Event()
    model, browser = await scripted(TextDelta("An answer."), hold, DONE)
    chat = Chat(browser)
    asked = chat.asking()
    await held(model)

    readers = [chat.reading_along(await chat.running_turn()) for _ in range(3)]
    hold.set()
    await ended(asked, *readers)

    assert len(model.requests) == 1
    assert [m["role"] for m in (await chat.read())["messages"]] == ["user", "assistant"]


async def test_a_turn_that_has_ended_has_nothing_to_read_along_with(scripted: Any) -> None:
    _, browser = await scripted(TextDelta("An answer."), DONE)
    chat = Chat(browser)
    [answered] = await ended(chat.asking())
    turn_id = chunk(answered, "start")["messageMetadata"]["turn"]["id"]

    late = await chat.read_along(turn_id)

    assert late.status_code == 204
    assert late.content == b""


async def test_only_its_own_reader_reads_along_with_a_turn(
    start_app: Any, scripted: Any, settings: Settings
) -> None:
    hold = asyncio.Event()
    model, browser = await scripted(TextDelta("Half an "), hold, DONE)
    mine, other = Chat(browser), Chat(browser)
    asked = mine.asking()
    await held(model)
    turn_id = await mine.running_turn()
    stranger = await start_app(settings)
    await stranger.post("/api/session/guest")
    nobody = await start_app(settings)

    theirs = await stranger.get(f"{mine.path}/turns/{turn_id}/stream")
    elsewhere = await browser.get(f"{other.path}/turns/{turn_id}/stream")
    unknown = await browser.get(f"{mine.path}/turns/{new_id()}/stream")
    without_a_session = await nobody.get(f"{mine.path}/turns/{turn_id}/stream")
    hold.set()
    await ended(asked)

    assert (theirs.status_code, theirs.json()["detail"]) == (404, "No such conversation.")
    assert elsewhere.status_code == 404
    assert (unknown.status_code, unknown.json()["detail"]) == (
        404,
        "This conversation has no such turn.",
    )
    assert without_a_session.status_code == 401


async def test_a_reader_is_told_when_the_stream_is_lost_and_the_turn_runs_on(
    scripted: Any, settings: Settings, valkey_url: str
) -> None:
    """Valkey is restarted while an answer is written. It keeps nothing on disk, so the
    stream is gone. The answer is not: it is written to its end, and stored."""
    hold = asyncio.Event()
    model, browser = await scripted(TextDelta("Half an "), hold, TextDelta("answer."), DONE)
    chat = Chat(browser)
    asked = chat.asking()
    await held(model)
    turn_id = await chat.running_turn()

    async with Valkey.from_url(valkey_url) as valkey:
        await valkey.delete(f"{settings.valkey_prefix}:turn:{turn_id}")
        [reader] = await ended(asked)
        joining = await chat.read_along(turn_id)
        hold.set()
        await asyncio.wait_for(model.closed.wait(), PATIENCE_S)
        gone_for_good = not await valkey.exists(f"{settings.valkey_prefix}:turn:{turn_id}")

    for response in (reader, joining):
        assert response.status_code == 200
        assert chunks(response)[-2:] == [{"type": "error", "errorText": ui_stream.LOST}, "[DONE]"]
    assert said(reader) == "Half an "
    assert kinds(joining) == ["error", "[DONE]"]
    assert gone_for_good  # the rest of the answer didn't start a stream without a beginning
    conversation = await chat.read()
    assert conversation["messages"][1]["parts"] == [{"type": "text", "text": "Half an answer."}]
    assert conversation["messages"][1]["metadata"]["turn"]["status"] == "completed"


async def test_a_turn_that_could_not_be_read_along_with_is_not_started(
    scripted: Any, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    model, browser = await scripted(TextDelta("An answer."), DONE)
    chat = Chat(browser)

    async def away(events: TurnEvents, turn_id: uuid.UUID, first: bytes) -> None:
        raise EventsUnavailableError(events.key(turn_id), first)

    with monkeypatch.context() as patched:
        patched.setattr(TurnEvents, "open", away)
        [refused] = await ended(chat.asking())
    [again] = await ended(chat.asking())  # the same question, sent again unchanged
    usage = (await browser.get("/api/usage")).json()

    assert refused.status_code == 503
    assert refused.json()["detail"] == "Answers can't be written right now. Try again in a moment."
    assert again.status_code == 200
    assert len(model.requests) == 1
    assert (await count(db, Message), await count(db, Turn)) == (2, 1)
    assert [window["reserved"] for window in usage["windows"]] == [0, 0]


async def test_a_reader_is_sent_a_line_while_the_model_is_silent(
    start_app: Any, settings: Settings, migrated: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A model that is being loaded says nothing for a while, and a proxy on the way
    may close a connection that is silent. The line is no event: readers skip it."""
    assert migrated
    monkeypatch.setattr(turn_events, "TICK", timedelta(milliseconds=10))
    monkeypatch.setattr(turn_routes, "KEEP_ALIVE_AFTER", timedelta(milliseconds=30))
    browser = await start_app(settings)
    await browser.post("/api/session/guest")
    chat = Chat(browser)

    [waited] = await ended(chat.asking("[[delay: 1]] Hello"))
    monkeypatch.setattr(turn_routes, "KEEP_ALIVE_AFTER", timedelta(seconds=15))
    other = Chat(browser)
    [at_once] = await ended(other.asking("Hello"))

    lines = waited.text.split("\n\n")
    assert lines.count(": keep-alive") >= 2
    assert lines.index(": keep-alive") == 2  # after `start` and `start-step`: the model is silent
    assert ": keep-alive" not in at_once.text
    assert kinds(waited) == kinds(at_once)
    assert said(waited) == "Echo: Hello"


async def test_readers_dont_keep_the_database_busy(scripted: Any) -> None:
    """A reader holds its connection for as long as the answer takes. If each also held
    a connection to the database, a dozen readers would leave none for anyone else."""
    hold = asyncio.Event()
    model, browser = await scripted(TextDelta("Half an "), hold, DONE)
    chat = Chat(browser)
    asked = chat.asking()
    await held(model)
    turn_id = await chat.running_turn()

    readers = [chat.reading_along(turn_id) for _ in range(12)]  # the pool has ten connections
    await asyncio.sleep(0.2)
    async with asyncio.timeout(PATIENCE_S):
        meanwhile = await browser.get("/api/me")
        conversation = await browser.get(chat.path)
    hold.set()
    responses = await ended(asked, *readers)

    assert (meanwhile.status_code, conversation.status_code) == (200, 200)
    assert {response.status_code for response in responses} == {200}


# --- Stopping -------------------------------------------------------------------------


async def test_a_turn_can_be_stopped_and_keeps_what_had_arrived(
    scripted: Any, db: AsyncSession
) -> None:
    never = asyncio.Event()
    model, browser = await scripted(
        ReasoningDelta("Let me think."),
        TextDelta("Half an "),
        TextDelta("answer"),
        never,
        TextDelta(" that nobody will read."),
        DONE,
    )
    chat = Chat(browser)
    asked = chat.asking("Tell me something long")
    await held(model)

    stopped = await chat.stop(await chat.running_turn())
    conversation = await chat.read()  # asked right after: the stop has been recorded
    [reader] = await ended(asked)

    assert stopped.status_code == 200
    assert (stopped.json()["status"], stopped.json()["finished_at"]) == (
        "stopped",
        "2026-11-10T12:00:00Z",
    )
    assert model.closed.is_set()  # the model's stream was closed: it stops generating
    assert conversation["messages"][1]["parts"] == [
        {"type": "reasoning", "text": "Let me think."},
        {"type": "text", "text": "Half an answer"},
    ]
    turn = conversation["messages"][1]["metadata"]["turn"]
    assert turn == stopped.json()
    assert (turn["finish_reason"], turn["error"]) == (None, None)
    assert turn["output_tokens"] is None  # the model never got to report them
    assert conversation["unanswered"] is None
    assert (await count(db, Message), await count(db, Turn)) == (2, 1)
    # Whoever was reading along is told how it ended, in the stream itself.
    assert kinds(reader)[-5:] == ["text-delta", "text-end", "finish-step", "finish", "[DONE]"]
    assert chunk(reader, "finish")["finishReason"] == "other"
    assert chunk(reader, "finish")["messageMetadata"]["turn"] == turn
    assert said(reader) == "Half an answer"


async def test_a_stop_never_parts_what_is_stored_from_what_was_streamed(
    scripted: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stop comes while a piece of the answer is on its way to the readers. That
    piece is part of the answer already, and will be stored. So it is streamed too:
    whoever read along has seen exactly what is kept."""
    on_its_way, let_through = asyncio.Event(), asyncio.Event()
    append = TurnEvents.append

    async def slowly(events: TurnEvents, turn_id: uuid.UUID, event: bytes) -> bool:
        if b"the piece in question" in event:
            on_its_way.set()
            await let_through.wait()
        return await append(events, turn_id, event)

    monkeypatch.setattr(TurnEvents, "append", slowly)
    never = asyncio.Event()
    _, browser = await scripted(
        TextDelta("Before it, "), TextDelta("the piece in question"), never, DONE
    )
    chat = Chat(browser)
    asked = chat.asking()
    await asyncio.wait_for(on_its_way.wait(), PATIENCE_S)

    stopping = asyncio.create_task(chat.stop(await chat.running_turn()))
    await asyncio.sleep(0.05)  # the stop has been asked for, and has to wait
    waited = not stopping.done()
    let_through.set()
    stopped = await asyncio.wait_for(stopping, PATIENCE_S)
    [reader] = await ended(asked)

    assert waited
    assert stopped.json()["status"] == "stopped"
    assert said(reader) == "Before it, the piece in question"
    assert (await chat.read())["messages"][1]["parts"] == [
        {"type": "text", "text": "Before it, the piece in question"}
    ]


async def test_a_turn_stopped_before_the_model_said_anything_leaves_its_question_unanswered(
    scripted: Any, db: AsyncSession
) -> None:
    never = asyncio.Event()
    model, browser = await scripted(never, TextDelta("Too late."), DONE)
    chat = Chat(browser)
    asked = chat.asking("Hello?")
    await held(model)

    stopped = await chat.stop(await chat.running_turn())
    [reader] = await ended(asked)

    conversation = await chat.read()
    assert stopped.json()["status"] == "stopped"
    assert [m["role"] for m in conversation["messages"]] == ["user"]
    assert conversation["unanswered"] == stopped.json()
    assert await count(db, Message) == 1
    assert kinds(reader) == ["start", "start-step", "finish-step", "finish", "[DONE]"]
    # No answer was written, so the question has no version of one more.
    assert chunk(reader, "finish")["messageMetadata"]["versions"] == []


async def test_a_conversation_can_go_on_after_its_answer_was_stopped(scripted: Any) -> None:
    never = asyncio.Event()
    model, browser = await scripted(TextDelta("Half"), never, DONE)
    chat = Chat(browser)
    asked = chat.asking("First question")
    await held(model)
    await chat.stop(await chat.running_turn())
    await ended(asked)
    model.script = [TextDelta("A whole answer."), DONE]

    again = await browser.post(f"{chat.path}/turns", json={"answer_to": str(chat.question)})

    assert again.status_code == 200
    assert said(again) == "A whole answer."
    conversation = await chat.read()
    assert len(conversation["messages"][1]["metadata"]["versions"]) == 2  # the half, and this one


async def test_stopping_a_turn_that_has_ended_changes_nothing(scripted: Any) -> None:
    _, browser = await scripted(TextDelta("An answer."), DONE)
    chat = Chat(browser)
    [answered] = await ended(chat.asking())
    turn = chunk(answered, "finish")["messageMetadata"]["turn"]

    too_late = await chat.stop(turn["id"])
    once_more = await chat.stop(turn["id"])

    assert (too_late.status_code, once_more.status_code) == (200, 200)
    assert too_late.json() == once_more.json() == turn
    assert turn["status"] == "completed"


async def test_a_stop_that_comes_twice_stops_once(scripted: Any, db: AsyncSession) -> None:
    never = asyncio.Event()
    model, browser = await scripted(TextDelta("Half an answer"), never, DONE)
    chat = Chat(browser)
    asked = chat.asking()
    await held(model)
    turn_id = await chat.running_turn()

    first, second = await asyncio.gather(chat.stop(turn_id), chat.stop(turn_id))
    await ended(asked)

    assert first.json() == second.json()
    assert first.json()["status"] == "stopped"
    assert (await count(db, Message), await count(db, UsageEntry)) == (2, 1)


async def test_only_its_own_reader_stops_a_turn(
    start_app: Any, scripted: Any, settings: Settings
) -> None:
    hold = asyncio.Event()
    model, browser = await scripted(TextDelta("Half an "), hold, TextDelta("answer."), DONE)
    mine, other = Chat(browser), Chat(browser)
    asked = mine.asking()
    await held(model)
    turn_id = await mine.running_turn()
    stranger = await start_app(settings)
    await stranger.post("/api/session/guest")
    nobody = await start_app(settings)

    theirs = await stranger.post(f"{mine.path}/turns/{turn_id}/cancel")
    elsewhere = await browser.post(f"{other.path}/turns/{turn_id}/cancel")
    unknown = await mine.stop(str(new_id()))
    without_a_session = await nobody.post(f"{mine.path}/turns/{turn_id}/cancel")
    from_another_site = await browser.post(
        f"{mine.path}/turns/{turn_id}/cancel", headers={"Sec-Fetch-Site": "cross-site"}
    )
    still_running = not model.closed.is_set()
    hold.set()
    [answered] = await ended(asked)

    assert (theirs.status_code, elsewhere.status_code, unknown.status_code) == (404, 404, 404)
    assert unknown.json()["detail"] == "This conversation has no such turn."
    assert (without_a_session.status_code, from_another_site.status_code) == (401, 403)
    assert still_running
    assert said(answered) == "Half an answer."


async def test_a_turn_that_no_process_is_writing_cannot_be_stopped_only_given_up_on(
    api: httpx.AsyncClient, db: AsyncSession, clock: Any
) -> None:
    """The process that was writing it was killed. There is nothing to stop, and nothing
    says so until the turn has been silent for a minute."""
    await api.post("/api/session/guest")
    chat = Chat(api)
    turn = await left_running(db, chat, clock)

    clock.advance(seconds=59)
    too_early = await chat.stop(str(turn.id))
    lost = await chat.read_along(turn.id)
    clock.advance(seconds=1)
    given_up_on = await chat.stop(str(turn.id))
    nothing_to_read = await chat.read_along(turn.id)

    assert (too_early.status_code, too_early.json()["status"]) == (200, "running")
    assert kinds(lost) == ["error", "[DONE]"]  # running, as far as anyone knows, and unreadable
    assert (given_up_on.json()["status"], given_up_on.json()["finished_at"]) == (
        "interrupted",
        "2026-11-10T12:01:00Z",
    )
    assert nothing_to_read.status_code == 204


async def test_deleting_a_conversation_stops_the_answer_that_is_being_written_in_it(
    scripted: Any, db: AsyncSession
) -> None:
    never = asyncio.Event()
    model, browser = await scripted(TextDelta("Half an "), never, TextDelta("answer."), DONE)
    chat = Chat(browser)
    asked = chat.asking()
    await held(model)

    deleted = await browser.delete(chat.path)
    [reader] = await ended(asked)

    assert deleted.status_code == 204
    assert model.closed.is_set()  # nobody could read the rest
    assert chunk(reader, "finish")["messageMetadata"]["turn"]["status"] == "stopped"
    assert (await count(db, Conversation), await count(db, Message), await count(db, Turn)) == (
        0,
        0,
        0,
    )


# --- Kept alive while it is written ---------------------------------------------------


async def test_a_turn_that_is_being_written_is_not_given_up_on_however_long_it_takes(
    scripted: Any, db: AsyncSession, clock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(turns, "BEAT", timedelta(milliseconds=10))
    hold = asyncio.Event()
    model, browser = await scripted(TextDelta("A long "), hold, TextDelta("answer."), DONE)
    chat = Chat(browser)
    asked = chat.asking()
    await held(model)

    for _ in range(20):  # a quarter of an hour, in steps shorter than the patience with it
        clock.advance(seconds=45)
        await beaten(lambda: said_alive_at(db, clock.now))
        assert (await chat.read())["unanswered"]["status"] == "running"
    hold.set()
    [answered] = await ended(asked)

    assert said(answered) == "A long answer."
    turn = chunk(answered, "finish")["messageMetadata"]["turn"]
    assert (turn["status"], turn["started_at"], turn["finished_at"]) == (
        "completed",
        "2026-11-10T12:00:00Z",
        "2026-11-10T12:15:00Z",
    )


async def test_what_a_turn_streams_is_kept_while_it_is_written(
    scripted: Any, settings: Settings, valkey_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(turns, "BEAT", timedelta(milliseconds=10))
    hold = asyncio.Event()
    model, browser = await scripted(TextDelta("A long "), hold, TextDelta("answer."), DONE)
    chat = Chat(browser)
    asked = chat.asking()
    await held(model)
    key = f"{settings.valkey_prefix}:turn:{await chat.running_turn()}"

    async with Valkey.from_url(valkey_url) as valkey:
        await valkey.pexpire(key, 2_000)  # as if it had been silent for nearly a minute

        async def kept_again() -> bool:
            return bool(await valkey.pttl(key) > 2_000)

        await beaten(kept_again)
        renewed = await valkey.pttl(key)
    hold.set()
    [answered] = await ended(asked)

    assert 58_000 < renewed <= 60_000
    assert said(answered) == "A long answer."


async def test_a_turn_that_ends_after_it_was_given_up_on_is_left_as_it_was(
    scripted: Any, db: AsyncSession, clock: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Its process was only slow: it said nothing for a minute, and someone looked. The
    conversation may have moved on since, so the answer that turns up isn't slipped in.
    An answer nobody gets is not paid for."""
    hold = asyncio.Event()
    model, browser = await scripted(TextDelta("Sorry "), hold, TextDelta("I'm late."), DONE)
    chat = Chat(browser)
    asked = chat.asking()
    await held(model)

    clock.advance(seconds=60)  # no beat in the meantime: under test they are 15 real seconds apart
    given_up_on = (await chat.read())["unanswered"]
    hold.set()
    [reader] = await ended(asked)
    usage = (await browser.get("/api/usage")).json()

    assert given_up_on["status"] == "interrupted"
    conversation = await chat.read()
    assert [m["role"] for m in conversation["messages"]] == ["user"]
    assert conversation["unanswered"] == given_up_on
    assert "it ended after it had been given up on" in caplog.text
    assert await count(db, UsageEntry) == 0
    assert [(w["used"], w["reserved"]) for w in usage["windows"]] == [(0, 0), (0, 0)]
    assert said(reader) == "Sorry I'm late."  # whoever read along saw it written


# --- A shutdown -----------------------------------------------------------------------


async def test_a_shutdown_records_what_was_being_written_as_interrupted_and_free(
    open_app: Any, start_app: Any, offer: Any, settings: Settings, db: AsyncSession
) -> None:
    never = asyncio.Event()
    model, catalog = offer(TextDelta("Half an "), TextDelta("answer"), never, DONE)
    chat: Chat
    async with open_app(settings, catalog=catalog) as browser:
        await browser.post("/api/session/guest")
        chat = Chat(browser)
        asked = chat.asking()
        await held(model)
        cookies = dict(browser.cookies)
        # Leaving the block is the shutdown.
    [reader] = await ended(asked)

    assert model.closed.is_set()
    turn = await the_turn(db)
    assert (turn.status, turn.credits, turn.error) == ("interrupted", None, None)
    assert turn.answer_id is not None
    answer = await db.get(Message, turn.answer_id)
    assert answer is not None
    assert answer.parts == [{"type": "text", "text": "Half an answer"}]
    assert await count(db, UsageEntry) == 0
    # Whoever was still reading along was told, before the process was gone.
    assert chunk(reader, "finish")["messageMetadata"]["turn"]["status"] == "interrupted"

    # The next process finds nothing held, and a question that can be answered again.
    model.script = [TextDelta("A whole answer."), DONE]
    restarted = await start_app(settings, catalog=catalog)
    restarted.cookies.update(cookies)
    usage = (await restarted.get("/api/usage")).json()
    again = await restarted.post(f"{chat.path}/turns", json={"answer_to": str(chat.question)})

    assert [(w["used"], w["reserved"]) for w in usage["windows"]] == [(0, 0), (0, 0)]
    assert (again.status_code, said(again)) == (200, "A whole answer.")


async def test_a_shutdown_gives_an_answer_a_moment_to_finish(
    open_app: Any,
    offer: Any,
    settings: Settings,
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(running, "GRACE", timedelta(seconds=PATIENCE_S))
    hold = asyncio.Event()
    model, catalog = offer(TextDelta("Nearly "), hold, TextDelta("done."), DONE)

    async def finish_soon() -> None:
        await asyncio.sleep(0.1)  # the shutdown has begun, and is waiting
        hold.set()

    async with open_app(settings, catalog=catalog) as browser:
        await browser.post("/api/session/guest")
        asked = Chat(browser).asking()
        await held(model)
        finishing = asyncio.create_task(finish_soon())
    await finishing
    [reader] = await ended(asked)

    turn = await the_turn(db)
    assert (turn.status, turn.credits) == ("completed", 9)
    assert said(reader) == "Nearly done."


# --- The description ------------------------------------------------------------------


async def test_the_routes_are_in_the_api_description(api: httpx.AsyncClient) -> None:
    paths = (await api.get("/api/openapi.json")).json()["paths"]

    stream = paths["/api/conversations/{conversation_id}/turns/{turn_id}/stream"]["get"]
    cancel = paths["/api/conversations/{conversation_id}/turns/{turn_id}/cancel"]["post"]
    assert "text/event-stream" in stream["responses"]["200"]["content"]
    assert set(stream["responses"]) >= {"200", "204", "401", "404", "422"}
    assert set(cancel["responses"]) >= {"200", "401", "404", "422"}
    turn = cancel["responses"]["200"]["content"]["application/json"]["schema"]
    assert turn == {"$ref": "#/components/schemas/TurnOut"}
