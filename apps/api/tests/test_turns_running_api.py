"""Turns that run apart from their request: read along with again, stopped, kept alive
while they are written, and ended by a shutdown. And how many run at once: turns that
wait at the gate in front of a model's server, the answers one account may have being
written at once, and the readers it may have open. And when a turn's first word came,
however it ended.

The app runs in process, and a request's response comes back whole, once its stream has
ended. So a test starts a request as a task, does what it is about while the model is
held half-way (`ScriptedModel`), and looks at the response afterwards. What takes a real
connection, a reader who leaves in the middle, is in `test_turns_live.py`.
"""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import ClientDisconnect
from valkey.asyncio import Valkey

from actio import worker
from actio.api import turns as turn_routes
from actio.api import ui_stream
from actio.catalog import Catalog, Gate
from actio.chat import events as turn_events
from actio.chat import readers as turn_readers
from actio.chat import running, turns
from actio.chat.events import EventsUnavailableError, TurnEvents
from actio.chat.models import Conversation, Message, Turn
from actio.chat.parts import ReasoningPart, TextPart
from actio.core.ids import new_id
from actio.core.resources import open_resources
from actio.core.settings import Settings
from actio.usage.models import UsageEntry
from actio.usage.plans import Plans
from actio_llm import (
    Assistant,
    Finish,
    FinishReason,
    ProviderUnavailableError,
    ReasoningDelta,
    TextDelta,
    ToolCall,
    Usage,
    User,
)

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

    def asking(self, text: str = "Tell me something.", **more: Any) -> asyncio.Task[httpx.Response]:
        """Ask, and go on without waiting: the response comes when its stream has ended.
        `more` goes into the request too, such as the `model` to ask."""
        message = {"id": str(self.question), "parts": [{"type": "text", "text": text}]}
        return asyncio.create_task(
            self.client.post(f"{self.path}/turns", json={"message": message, **more})
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


def writing(turn_id: str) -> asyncio.Task[Any]:
    """The task the app's `TurnRunner` writes a running turn in, found by the name the
    runner gives it (`actio.chat.running`). It ends once the turn is recorded: for a turn
    that nobody reads along with, nothing else says so. Look for it while the turn runs:
    a task that has ended is no longer listed."""
    [task] = [task for task in asyncio.all_tasks() if task.get_name() == f"turn-{turn_id}"]
    return task


async def recorded(task: asyncio.Task[Any]) -> None:
    """Wait until the task a turn is written in has ended: the turn is recorded. The task
    is left alone, whatever happens to the wait."""
    done, _ = await asyncio.wait({task}, timeout=PATIENCE_S)
    assert done, "the turn wasn't recorded in time"


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


async def eventually(happened: Callable[[], Awaitable[bool]]) -> None:
    """Wait until what the database is asked says has happened: it does in a moment."""
    async with asyncio.timeout(PATIENCE_S):
        while True:
            if await happened():
                return
            await asyncio.sleep(0.005)


async def until(condition: Callable[[], bool]) -> None:
    """Wait until `condition` holds: the app gets there in a moment."""
    async with asyncio.timeout(PATIENCE_S):
        while True:
            if condition():
                return
            await asyncio.sleep(0.005)


async def alive_at(db: AsyncSession, turn_id: uuid.UUID) -> datetime:
    """When the turn last said that it is alive."""
    moment = await db.scalar(
        select(Turn.alive_at).where(Turn.id == turn_id).execution_options(populate_existing=True)
    )
    await db.commit()  # the next look starts from what the database has then
    assert moment is not None
    return moment


async def the_draft(db: AsyncSession) -> Any:
    """The turn's draft, as the database has it now: `None` when it has none."""
    draft = await db.scalar(select(Turn.draft))
    await db.commit()  # the next look starts from what the database has then
    return draft


async def drafted(db: AsyncSession, *texts: str) -> bool:
    """Whether the turn's draft holds parts with these texts, in this order."""
    draft = await the_draft(db)
    return draft is not None and [part["text"] for part in draft["parts"]] == list(texts)


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
    turn = writing(turn_id)

    async with Valkey.from_url(valkey_url) as valkey:
        await valkey.delete(f"{settings.valkey_prefix}:turn:{turn_id}")
        [reader] = await ended(asked)
        joining = await chat.read_along(turn_id)
        hold.set()
        await asyncio.wait_for(model.closed.wait(), PATIENCE_S)
        gone_for_good = not await valkey.exists(f"{settings.valkey_prefix}:turn:{turn_id}")
    # Nobody reads along with it now: only its task says when it is recorded.
    await recorded(turn)

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


async def test_readers_dont_keep_the_database_busy(
    scripted: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reader holds its connection for as long as the answer takes. If each also held
    a connection to the database, a dozen readers would leave none for anyone else."""
    monkeypatch.setattr(turn_readers, "READERS_PER_ACCOUNT", 20)  # not what this is about
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


async def test_a_turn_that_ends_after_the_worker_gave_up_on_it_is_left_as_it_was(
    scripted: Any,
    settings: Settings,
    db: AsyncSession,
    clock: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """As above, but nobody looked at the conversation: the worker went through the
    turns that had fallen silent, by the same clock as the app's. The answer that turns
    up afterwards isn't stored, and isn't paid for. Half a minute later the
    conversation takes a new question, and the turn keeps the moment the worker gave up
    on it."""
    hold = asyncio.Event()
    model, browser = await scripted(TextDelta("Sorry "), hold, TextDelta("I'm late."), DONE)
    chat = Chat(browser)
    asked = chat.asking()
    await held(model)
    late = await the_turn(db)

    clock.advance(seconds=60)  # no beat in the meantime: under test they are 15 real seconds apart
    given_up_at = clock.now
    async with open_resources(settings) as resources:
        no_models = Catalog({}, default="standard", thinking={})  # the job asks none
        [giving_up] = [
            job
            for job in worker.listed(resources, no_models, clock=lambda: clock.now)
            if job.name == worker.GIVE_UP
        ]
        await giving_up.run()
    hold.set()
    await ended(asked)
    usage = (await browser.get("/api/usage")).json()
    entries = await count(db, UsageEntry)
    conversation = await chat.read()
    clock.advance(seconds=30)
    model.script = [TextDelta("Here now."), DONE]
    new_question = {"id": str(new_id()), "parts": [{"type": "text", "text": "Still there?"}]}
    # With a time limit: a conversation that was still taken would keep this waiting.
    again = await asyncio.wait_for(
        browser.post(f"{chat.path}/turns", json={"message": new_question}), PATIENCE_S
    )

    assert "it ended after it had been given up on" in caplog.text
    assert [m["role"] for m in conversation["messages"]] == ["user"]
    assert entries == 0
    assert [(w["used"], w["reserved"]) for w in usage["windows"]] == [(0, 0), (0, 0)]
    assert again.status_code == 200
    assert said(again) == "Here now."
    await db.refresh(late)
    assert (late.status, late.finished_at, late.answer_id) == ("interrupted", given_up_at, None)


# --- What a turn whose process died had written ---------------------------------------


async def test_each_beat_saves_what_the_answer_holds_by_then(
    scripted: Any, db: AsyncSession, clock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reasoning and text, as parts, under the id the turn's stream gave the answer, each
    beat replacing the one before. A beat while the model has written nothing saves
    nothing, and the turn that has ended keeps nothing."""
    monkeypatch.setattr(turns, "BEAT", timedelta(milliseconds=10))
    first_word, more, last_word = asyncio.Event(), asyncio.Event(), asyncio.Event()
    model, browser = await scripted(
        first_word,
        ReasoningDelta("Something to sit on."),
        TextDelta("The "),
        more,
        TextDelta("saddle."),
        last_word,
        DONE,
    )
    asked = Chat(browser).asking()
    await held(model)

    clock.advance(seconds=15)
    await beaten(lambda: said_alive_at(db, clock.now))
    before_a_word = await the_draft(db)
    first_word.set()
    await beaten(lambda: drafted(db, "Something to sit on.", "The "))
    first = await the_draft(db)
    more.set()
    await beaten(lambda: drafted(db, "Something to sit on.", "The saddle."))
    second = await the_draft(db)
    last_word.set()
    [answered] = await ended(asked)

    answer_id = chunk(answered, "start")["messageId"]
    assert before_a_word is None
    thought = {"type": "reasoning", "text": "Something to sit on."}
    assert first == {"answer_id": answer_id, "parts": [thought, {"type": "text", "text": "The "}]}
    assert second == {
        "answer_id": answer_id,
        "parts": [thought, {"type": "text", "text": "The saddle."}],
    }
    assert await the_draft(db) is None
    assert said(answered) == "The saddle."


async def test_whoever_looks_finds_what_a_dead_process_had_written_as_its_answer(
    scripted: Any, db: AsyncSession, clock: Any
) -> None:
    """The process was killed in the middle of an answer, and its last beat had saved
    what the answer held. A minute later someone looks. The conversation shows that as
    the question's answer, interrupted, and dated at the beat: the conversation doesn't
    move up its owner's list because someone looked. It costs nothing, and the
    conversation goes on from it: the model of the next question is shown it."""
    model, browser = await scripted(TextDelta("Its rails."), DONE)
    chat = Chat(browser)
    turn = await left_running(db, chat, clock)
    clock.advance(seconds=20)
    draft = turns.Draft(
        answer_id=new_id(),
        parts=[ReasoningPart(text="Something to sit on."), TextPart(text="The saddle, which")],
    )
    await turns.still_alive(db, turn.id, now=clock.now, draft=draft)
    await db.commit()

    clock.advance(seconds=60)
    seen = await chat.read()
    usage = (await browser.get("/api/usage")).json()
    entries = await count(db, UsageEntry)
    still_drafted = await db.scalar(select(Turn.draft.is_not(None)).where(Turn.id == turn.id))
    follow_up = {"id": str(new_id()), "parts": [{"type": "text", "text": "Which part of it?"}]}
    went_on = await asyncio.wait_for(
        browser.post(
            f"{chat.path}/turns", json={"message": follow_up, "parent_id": str(draft.answer_id)}
        ),
        PATIENCE_S,
    )

    _, answer = seen["messages"]
    assert (answer["id"], answer["metadata"]["created_at"]) == (
        str(draft.answer_id),
        "2026-11-10T12:00:20Z",
    )
    assert answer["parts"] == [
        {"type": "reasoning", "text": "Something to sit on."},
        {"type": "text", "text": "The saddle, which"},
    ]
    kept = answer["metadata"]["turn"]
    assert (kept["id"], kept["status"], kept["finished_at"], kept["credits"]) == (
        str(turn.id),
        "interrupted",
        "2026-11-10T12:01:20Z",
        None,
    )
    assert (seen["current_leaf_id"], seen["last_active_at"], seen["unanswered"]) == (
        str(draft.answer_id),
        "2026-11-10T12:00:20Z",
        None,
    )
    assert (still_drafted, entries) == (False, 0)
    assert [(w["used"], w["reserved"]) for w in usage["windows"]] == [(0, 0), (0, 0)]
    assert (went_on.status_code, said(went_on)) == (200, "Its rails.")
    assert model.requests[-1].messages[1:] == [
        User("Asked just before the server died"),
        Assistant("The saddle, which"),
        User("Which part of it?"),
    ]


async def test_a_turn_given_up_on_with_what_it_had_written_is_left_so_when_it_ends_after_all(
    scripted: Any,
    db: AsyncSession,
    clock: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A beat saved what the answer held, and then no beat reached the database for a
    minute, when someone looked: the turn was given up on with that as its answer, under
    the id its stream had named. Its process was only slow, and the answer ends after
    all. The turn is left as it was: nothing more is stored, the question has the one
    answer, and nothing is paid for."""
    monkeypatch.setattr(turns, "BEAT", timedelta(milliseconds=10))
    hold = asyncio.Event()
    model, browser = await scripted(TextDelta("Sorry, "), hold, TextDelta("I'm late."), DONE)
    chat = Chat(browser)
    asked = chat.asking()
    await held(model)
    clock.advance(seconds=20)
    await beaten(lambda: said_alive_at(db, clock.now))  # with what was written by then

    async def lost(*_: Any, **__: Any) -> None:
        """A beat that doesn't reach the database."""

    monkeypatch.setattr(turns, "still_alive", lost)
    clock.advance(seconds=60)
    given_up_on = await chat.read()
    hold.set()
    [reader] = await ended(asked)
    afterwards = await chat.read()
    usage = (await browser.get("/api/usage")).json()

    _, answer = given_up_on["messages"]
    assert (answer["id"], answer["parts"]) == (
        chunk(reader, "start")["messageId"],
        [{"type": "text", "text": "Sorry, "}],
    )
    assert answer["metadata"]["created_at"] == "2026-11-10T12:00:20Z"
    turn = answer["metadata"]["turn"]
    assert (turn["status"], turn["finished_at"]) == ("interrupted", "2026-11-10T12:01:20Z")
    assert afterwards == given_up_on
    assert "it ended after it had been given up on" in caplog.text
    assert (await count(db, Message), await count(db, UsageEntry)) == (2, 0)
    assert [(w["used"], w["reserved"]) for w in usage["windows"]] == [(0, 0), (0, 0)]
    assert said(reader) == "Sorry, I'm late."  # whoever read along saw it written


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


# --- The gate in front of a model's server --------------------------------------------
#
# A server that writes one answer at a time: a turn that comes while it is busy waits
# inside Actio, running like any other, and its model is asked once the turn has passed.


async def guest_on(
    start_app: Any, settings: Settings, lineup: Any, plans: Plans | None = None
) -> httpx.AsyncClient:
    """A guest's browser on an app that offers the models of `lineup`."""
    browser: httpx.AsyncClient = await start_app(settings, catalog=lineup.catalog(), plans=plans)
    await browser.post("/api/session/guest")
    return browser


async def test_a_turn_waits_while_its_models_server_is_busy_and_its_model_is_asked_only_then(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    clock: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two models on one server, which writes one answer at a time. A server that is asked
    anyway keeps the request waiting inside itself, while the client's patience for the
    first word runs out: so it is asked only once it is free."""
    assert migrated
    gate, hold = Gate(1), asyncio.Event()
    first = lineup.add(
        "standard", TextDelta("The first "), hold, TextDelta("answer."), DONE, gate=gate
    )
    second = lineup.add("second", TextDelta("The second answer."), DONE, gate=gate)
    browser = await guest_on(start_app, settings, lineup)
    one, two = Chat(browser), Chat(browser)
    asked_first = one.asking()
    await held(first)

    asked_second = two.asking(model="second")
    await until(lambda: gate.waiting == 1)
    waiting = await two.running_turn()  # it waits: it isn't refused
    clock.advance(seconds=30)
    asked_meanwhile = list(second.requests)
    hold.set()
    first_reader, second_reader = await ended(asked_first, asked_second)

    assert asked_meanwhile == []
    assert first.closed.is_set()
    assert len(second.requests) == 1
    assert said(first_reader) == "The first answer."
    assert said(second_reader) == "The second answer."
    assert chunk(second_reader, "finish")["messageMetadata"]["turn"]["status"] == "completed"
    assert f"turn {waiting}: waited 30.0 s for the model's server" in caplog.text
    assert caplog.text.count("for the model's server") == 1  # the first one didn't wait


async def test_turns_that_wait_pass_in_the_order_they_came(
    start_app: Any, settings: Settings, migrated: str, lineup: Any
) -> None:
    assert migrated
    gate = Gate(1)
    names = ["standard", "second", "third"]
    holds = [asyncio.Event() for _ in names]
    models = [
        lineup.add(
            name, TextDelta(f"{name.capitalize()} "), hold, TextDelta("answered."), DONE, gate=gate
        )
        for name, hold in zip(names, holds, strict=True)
    ]
    browser = await guest_on(start_app, settings, lineup)
    chats = [Chat(browser) for _ in names]
    asked = [chats[0].asking()]
    await held(models[0])
    for chat, name in zip(chats[1:], names[1:], strict=True):
        asked.append(chat.asking(model=name))
        await until(lambda: gate.waiting == len(asked) - 1)  # in line behind the ones before

    holds[0].set()
    await held(models[1])
    third_asked_too_soon = models[2].started.is_set()
    holds[1].set()
    await held(models[2])
    holds[2].set()
    readers = await ended(*asked)

    assert not third_asked_too_soon
    assert [said(reader) for reader in readers] == [
        "Standard answered.",
        "Second answered.",
        "Third answered.",
    ]


async def test_a_busy_server_holds_up_only_the_answers_it_is_to_write(
    start_app: Any, settings: Settings, migrated: str, lineup: Any
) -> None:
    """A model on another server is asked at once, and so is one on a server that takes
    every answer as it comes."""
    assert migrated
    busy, other = Gate(1), Gate(1)
    hold = asyncio.Event()
    first = lineup.add("standard", TextDelta("Busy."), hold, DONE, gate=busy)
    same = lineup.add("same", TextDelta("Same server."), DONE, gate=busy)
    lineup.add("elsewhere", TextDelta("Another server."), DONE, gate=other)
    lineup.add("ungated", TextDelta("Every answer at once."), DONE)
    browser = await guest_on(start_app, settings, lineup)
    asked = Chat(browser).asking()
    await held(first)
    waits = Chat(browser).asking(model="same")
    await until(lambda: busy.waiting == 1)

    on_another, without_a_gate = await ended(
        Chat(browser).asking(model="elsewhere"), Chat(browser).asking(model="ungated")
    )
    waited = not same.started.is_set()
    hold.set()
    await ended(asked, waits)

    assert said(on_another) == "Another server."
    assert said(without_a_gate) == "Every answer at once."
    assert waited


@pytest.mark.parametrize(
    ("ending", "recorded_as"),
    [
        ("finishes", "completed"),
        ("fails", "failed"),
        ("is stopped", "stopped"),
        ("is cancelled", "interrupted"),
    ],
)
async def test_the_next_turn_gets_in_however_the_one_before_it_ended(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    ending: str,
    recorded_as: str,
) -> None:
    assert migrated
    gate, hold = Gate(1), asyncio.Event()
    rest = (
        [ProviderUnavailableError("the server went away")]
        if ending == "fails"
        else [TextDelta(" answer."), DONE]
    )
    first = lineup.add("standard", TextDelta("Half an"), hold, *rest, gate=gate)
    lineup.add("second", TextDelta("The next answer."), DONE, gate=gate)
    browser = await guest_on(start_app, settings, lineup)
    one, two = Chat(browser), Chat(browser)
    asked_first = one.asking()
    await held(first)
    first_turn = await one.running_turn()
    task = writing(first_turn)
    asked_second = two.asking(model="second")
    await until(lambda: gate.waiting == 1)

    match ending:
        case "finishes" | "fails":
            hold.set()
        case "is stopped":
            await asyncio.wait_for(one.stop(first_turn), PATIENCE_S)
        case "is cancelled":
            # From outside, without a halt: a process going down faster than it can say.
            task.cancel()
    [next_reader] = await ended(asked_second)
    await recorded(task)
    if ending == "is cancelled":
        asked_first.cancel()  # its stream never got its last event: nothing would end it
    else:
        await ended(asked_first)

    assert said(next_reader) == "The next answer."
    first_answer = (await one.read())["messages"][1]
    assert first_answer["metadata"]["turn"]["status"] == recorded_as


async def test_the_next_turn_waits_until_the_model_before_it_has_closed_its_stream(
    start_app: Any, settings: Settings, migrated: str, lineup: Any
) -> None:
    """Closing a model's stream takes a moment: the connection to its server is closed,
    and only then does the server stop writing. Until then the server is busy. Here the
    model asks for a tool, which no turn offers it: the turn ends there, with the
    model's stream still open, and closes it."""
    assert migrated
    gate, hold, let_close = Gate(1), asyncio.Event(), asyncio.Event()
    tool = ToolCall("call-1", "get_weather", {"city": "Izmir"})
    first = lineup.add(
        "standard", TextDelta("Half an answer"), hold, tool, DONE, gate=gate, closes_when=let_close
    )
    second = lineup.add("second", TextDelta("The next answer."), DONE, gate=gate)
    browser = await guest_on(start_app, settings, lineup)
    one, two = Chat(browser), Chat(browser)
    asked_first = one.asking()
    await held(first)
    asked_second = two.asking(model="second")
    await until(lambda: gate.waiting == 1)

    hold.set()
    await asyncio.wait_for(first.closing.wait(), PATIENCE_S)
    # A turn let through before the stream began to close was woken before this test
    # was, and has asked its model by now: it does so in its very next step.
    asked_while_closing = list(second.requests)
    let_close.set()
    first_reader, second_reader = await ended(asked_first, asked_second)

    assert asked_while_closing == []
    assert chunk(first_reader, "message-metadata")["messageMetadata"]["turn"]["error"] == (
        "model_error"
    )
    assert said(second_reader) == "The next answer."


async def test_a_turn_stopped_while_it_waits_ends_at_once_and_leaves_the_others_as_they_were(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    db: AsyncSession,
    clock: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Stop takes a turn out of the queue. Nothing was written, so nothing is kept or
    charged, and its model never hears of it. The answer ahead of it goes on, and the
    one behind it doesn't jump ahead. The log says how long the stopped one waited."""
    assert migrated
    gate, hold = Gate(1), asyncio.Event()
    first = lineup.add(
        "standard", TextDelta("The first "), hold, TextDelta("answer."), DONE, gate=gate
    )
    second = lineup.add("second", TextDelta("Never written."), DONE, gate=gate)
    third = lineup.add("third", TextDelta("The third answer."), DONE, gate=gate)
    browser = await guest_on(start_app, settings, lineup)
    one, two, three = Chat(browser), Chat(browser), Chat(browser)
    asked_first = one.asking()
    await held(first)
    asked_second = two.asking(model="second")
    await until(lambda: gate.waiting == 1)
    asked_third = three.asking(model="third")
    await until(lambda: gate.waiting == 2)
    second_turn = await two.running_turn()
    clock.advance(seconds=20)

    # A stop answers once the turn is recorded: never, if the wait can't be ended.
    stopped = await asyncio.wait_for(two.stop(second_turn), PATIENCE_S)
    # No pause is needed to see whether the turn behind slipped through. It would have
    # been let through when the stopped one left the line, and asked its model in its
    # very next step; the stop answers only after the stopped turn was recorded, which
    # takes the database several round trips after that, while other tasks run.
    behind = (gate.waiting, third.started.is_set(), first.closed.is_set())
    [second_reader] = await ended(asked_second)
    hold.set()
    first_reader, third_reader = await ended(asked_first, asked_third)

    assert stopped.json()["status"] == "stopped"
    assert behind == (1, False, False)
    assert (
        f"turn {second_turn}: waited 20.0 s for the model's server, and didn't get its turn"
        in caplog.text
    )
    assert second.requests == []
    assert kinds(second_reader) == ["start", "start-step", "finish-step", "finish", "[DONE]"]
    conversation = await two.read()
    assert [m["role"] for m in conversation["messages"]] == ["user"]
    assert (conversation["unanswered"]["status"], conversation["unanswered"]["credits"]) == (
        "stopped",
        None,
    )
    assert await count(db, UsageEntry) == 2  # the first and the third
    assert said(first_reader) == "The first answer."
    assert said(third_reader) == "The third answer."


async def test_a_turn_that_waits_longer_than_a_minute_keeps_its_place_its_credits_and_its_stream(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    plans_with: Any,
    db: AsyncSession,
    clock: Any,
    valkey_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn that waits says that it is alive, like one that is written: otherwise it
    would be given up on, its credits handed back and its stream deleted, while it waits
    behind a long answer."""
    assert migrated
    monkeypatch.setattr(turns, "BEAT", timedelta(milliseconds=10))
    gate, hold = Gate(1), asyncio.Event()
    first = lineup.add(
        "standard", TextDelta("A long "), hold, TextDelta("answer."), DONE, gate=gate
    )
    lineup.add("second", TextDelta("At last."), DONE, gate=gate)
    lineup.add("elsewhere", TextDelta("At once."), DONE)
    # Each answer holds the guess at its prompt and 512 tokens. With two held, 1,500
    # leave a third less than its whole room.
    plans = plans_with(guest=(1_500, 10_000), models=lineup.names)
    browser = await guest_on(start_app, settings, lineup, plans)
    one, two = Chat(browser), Chat(browser)
    asked_first = one.asking()
    await held(first)
    writing = uuid.UUID(await one.running_turn())
    asked_second = two.asking(model="second")
    await until(lambda: gate.waiting == 1)
    waiting = uuid.UUID(await two.running_turn())

    statuses = []
    for _ in range(4):  # three minutes, in steps shorter than the patience with silence
        clock.advance(seconds=45)
        # Both turns beat: the one that is written must hold its credits too, for what
        # the third question is told at the end to be about the one that waits.
        for turn_id in (writing, waiting):
            await beaten(partial(_said_alive_now, db, turn_id, clock.now))
        statuses.append((await two.read())["unanswered"]["status"])
    async with Valkey.from_url(valkey_url) as valkey:
        # A beat says the turn is alive, renews its hold, and only then keeps its
        # stream. A stream kept again after this was renewed, as if it had been silent
        # for nearly a minute, means a beat at this moment has renewed the hold too.
        for turn_id in (writing, waiting):
            key = f"{settings.valkey_prefix}:turn:{turn_id}"
            await valkey.pexpire(key, 2_000)

            async def kept_again(key: str = key) -> bool:
                return bool(await valkey.pttl(key) > 2_000)

            await beaten(kept_again)
    [beside] = await ended(Chat(browser).asking(model="elsewhere"))
    hold.set()
    _, second_reader = await ended(asked_first, asked_second)

    assert statuses == ["running"] * 4
    # Both holds were still there: the one of the turn that waits too.
    assert chunk(beside, "start")["messageMetadata"]["turn"]["capped_by_usage"] is True
    assert said(second_reader) == "At last."
    assert chunk(second_reader, "finish")["messageMetadata"]["turn"]["status"] == "completed"


async def _said_alive_now(db: AsyncSession, turn_id: uuid.UUID, now: datetime) -> bool:
    return await alive_at(db, turn_id) == now


async def test_the_reader_of_a_turn_that_waits_is_kept_connected_and_then_reads_the_answer(
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert migrated
    monkeypatch.setattr(turn_events, "TICK", timedelta(milliseconds=10))
    monkeypatch.setattr(turn_routes, "KEEP_ALIVE_AFTER", timedelta(milliseconds=30))
    silent: list[uuid.UUID] = []
    follow = TurnEvents.follow

    async def noted(events: TurnEvents, turn_id: uuid.UUID) -> AsyncIterator[bytes | None]:
        """What readers are told, with each tick in which there was nothing new noted."""
        async with aclosing(follow(events, turn_id)) as following:
            async for event in following:
                if event is None:
                    silent.append(turn_id)
                yield event

    monkeypatch.setattr(TurnEvents, "follow", noted)
    gate, hold = Gate(1), asyncio.Event()
    first = lineup.add("standard", TextDelta("First."), hold, DONE, gate=gate)
    lineup.add("second", TextDelta("Second."), DONE, gate=gate)
    browser = await guest_on(start_app, settings, lineup)
    asked_first = Chat(browser).asking()
    await held(first)
    second = Chat(browser)
    asked_second = second.asking(model="second")
    await until(lambda: gate.waiting == 1)
    waiting = uuid.UUID(await second.running_turn())

    # Four silent ticks, 40 ms of them: a line was due after 30.
    await until(lambda: silent.count(waiting) >= 4)
    hold.set()
    _, waited = await ended(asked_first, asked_second)

    lines = waited.text.split("\n\n")
    assert lines.index(": keep-alive") == 2  # after `start` and `start-step`
    assert said(waited) == "Second."
    assert kinds(waited)[-3:] == ["finish-step", "finish", "[DONE]"]


async def test_a_shutdown_ends_the_turns_that_wait_too_and_leaves_the_gate_free(
    open_app: Any,
    start_app: Any,
    settings: Settings,
    migrated: str,
    lineup: Any,
    db: AsyncSession,
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert migrated
    gate, never = Gate(1), asyncio.Event()
    first = lineup.add(
        "standard", TextDelta("Half an "), TextDelta("answer"), never, DONE, gate=gate
    )
    second = lineup.add("second", TextDelta("Never written."), DONE, gate=gate)
    catalog = lineup.catalog()
    one: Chat
    two: Chat
    # A shutdown that can't end the turn that waits would wait for it: in time, or never.
    async with asyncio.timeout(PATIENCE_S), open_app(settings, catalog=catalog) as browser:
        await browser.post("/api/session/guest")
        one, two = Chat(browser), Chat(browser)
        asked_first = one.asking()
        await held(first)
        asked_second = two.asking(model="second")
        await until(lambda: gate.waiting == 1)
        cookies = dict(browser.cookies)
        # Leaving the block is the shutdown.
    first_reader, second_reader = await ended(asked_first, asked_second)

    for reader in (first_reader, second_reader):
        assert chunk(reader, "finish")["messageMetadata"]["turn"]["status"] == "interrupted"
    assert second.requests == []
    assert said(second_reader) == ""
    assert await count(db, UsageEntry) == 0
    waited = chunk(second_reader, "start")["messageMetadata"]["turn"]["id"]
    assert f"turn {waited}: waited 0.0 s for the model's server, and didn't get its turn" in (
        caplog.text
    )

    # The same server, behind the same gate, takes the next answer at once.
    second.script = [TextDelta("Asked again, and answered."), DONE]
    restarted = await start_app(settings, catalog=catalog)
    restarted.cookies.update(cookies)
    again = await asyncio.wait_for(
        restarted.post(
            f"{two.path}/turns", json={"answer_to": str(two.question), "model": "second"}
        ),
        PATIENCE_S,
    )

    assert (again.status_code, said(again)) == (200, "Asked again, and answered.")


# --- Answers at once, per account -----------------------------------------------------


async def test_one_answer_more_than_the_plan_allows_at_once_is_refused_and_leaves_nothing(
    scripted: Any,
    plans_with: Any,
    db: AsyncSession,
    settings: Settings,
    valkey_url: str,
) -> None:
    hold = asyncio.Event()
    model, browser = await scripted(
        TextDelta("Half an "), hold, TextDelta("answer."), DONE, plans=plans_with(answers_at_once=1)
    )
    busy, another = Chat(browser), Chat(browser)
    asked = busy.asking()
    await held(model)
    turn_id = await busy.running_turn()
    before = (await browser.get("/api/usage")).json()

    refused = await asyncio.wait_for(another.asking("And meanwhile?"), PATIENCE_S)

    after = (await browser.get("/api/usage")).json()
    rows = (await count(db, Conversation), await count(db, Message), await count(db, Turn))
    async with Valkey.from_url(valkey_url) as valkey:
        streams = await valkey.keys(f"{settings.valkey_prefix}:turn:*")
    hold.set()
    [answered] = await ended(asked)

    assert refused.status_code == 429
    assert refused.headers["content-type"] == "application/problem+json"
    assert "retry-after" not in refused.headers
    assert refused.json() == {
        "type": "about:blank",
        "title": "Too Many Requests",
        "status": 429,
        "detail": "An answer is already being written for you, and your plan allows one "
        "at a time. Wait for it to finish, or stop it.",
        "at_once": 1,
    }
    # The question was never asked: no conversation, no message, no turn, nothing held,
    # no stream.
    assert (await browser.get(another.path)).status_code == 404
    assert rows == (1, 1, 1)
    assert len(model.requests) == 1
    assert after == before
    assert streams == [f"{settings.valkey_prefix}:turn:{turn_id}".encode()]
    assert said(answered) == "Half an answer."


async def test_an_answer_that_has_ended_makes_room_for_the_next(
    scripted: Any, plans_with: Any
) -> None:
    hold = asyncio.Event()
    model, browser = await scripted(
        TextDelta("An answer."), hold, DONE, plans=plans_with(answers_at_once=2)
    )
    first, second, third = Chat(browser), Chat(browser), Chat(browser)
    asked = [first.asking(), second.asking()]
    await until(lambda: len(model.requests) == 2)

    refused = await asyncio.wait_for(third.asking("A third?"), PATIENCE_S)
    hold.set()
    await ended(*asked)
    model.script = [TextDelta("Now there is room."), DONE]
    [answered] = await ended(third.asking("A third?"))  # the same message: it was never kept

    assert (refused.status_code, refused.json()["at_once"]) == (429, 2)
    assert refused.json()["detail"] == (
        "2 answers are already being written for you, as many as your plan allows at "
        "once. Wait for one to finish, or stop one."
    )
    assert said(answered) == "Now there is room."


async def test_an_account_at_its_limit_keeps_no_other_account_from_asking(
    scripted: Any, plans_with: Any, start_app: Any, settings: Settings
) -> None:
    hold = asyncio.Event()
    model, browser = await scripted(
        TextDelta("Half an "), hold, TextDelta("answer."), DONE, plans=plans_with(answers_at_once=1)
    )
    asked = Chat(browser).asking()
    await held(model)
    mine = await asyncio.wait_for(Chat(browser).asking("Mine, meanwhile?"), PATIENCE_S)
    stranger = await start_app(settings, plans=plans_with(answers_at_once=1))
    await stranger.post("/api/session/guest")

    [theirs] = await ended(Chat(stranger).asking("Theirs, meanwhile?"))
    hold.set()
    await ended(asked)

    assert mine.status_code == 429
    assert (theirs.status_code, said(theirs)) == (200, "Echo: Theirs, meanwhile?")


async def test_a_turn_left_by_a_process_that_died_stops_counting_a_minute_after_it_was_heard_from(
    start_app: Any,
    settings: Settings,
    migrated: str,
    plans_with: Any,
    db: AsyncSession,
    clock: Any,
) -> None:
    """Nothing gives up on it: its conversation isn't looked at. It only stops counting,
    by the rule that would give up on it."""
    assert migrated
    browser = await start_app(settings, plans=plans_with(answers_at_once=1))
    await browser.post("/api/session/guest")
    left = await left_running(db, Chat(browser), clock)

    clock.advance(seconds=59)
    too_early = await asyncio.wait_for(Chat(browser).asking("Is it gone?"), PATIENCE_S)
    clock.advance(seconds=1)
    [in_time] = await ended(Chat(browser).asking("And now?"))
    await db.refresh(left)

    assert too_early.status_code == 429
    assert (in_time.status_code, said(in_time)) == (200, "Echo: And now?")
    assert left.status == "running"


async def test_a_turn_that_waits_for_its_models_server_counts_as_being_written(
    start_app: Any, settings: Settings, migrated: str, lineup: Any, plans_with: Any
) -> None:
    assert migrated
    gate, hold = Gate(1), asyncio.Event()
    model = lineup.add("standard", TextDelta("An answer."), hold, DONE, gate=gate)
    browser = await guest_on(start_app, settings, lineup, plans_with(answers_at_once=2))
    asked = [Chat(browser).asking()]
    await held(model)
    asked.append(Chat(browser).asking())
    await until(lambda: gate.waiting == 1)

    refused = await asyncio.wait_for(Chat(browser).asking(), PATIENCE_S)
    hold.set()
    await ended(*asked)

    assert (refused.status_code, refused.json()["at_once"]) == (429, 2)


async def test_another_answer_to_an_old_question_counts_like_a_new_question(
    scripted: Any, plans_with: Any
) -> None:
    model, browser = await scripted(
        TextDelta("An answer."), DONE, plans=plans_with(answers_at_once=1)
    )
    answered = Chat(browser)
    await ended(answered.asking())
    hold = asyncio.Event()
    model.script = [TextDelta("Half"), hold, DONE]
    busy = Chat(browser)
    asked = busy.asking()
    await held(model)

    again = {"answer_to": str(answered.question)}
    refused = await asyncio.wait_for(browser.post(f"{answered.path}/turns", json=again), PATIENCE_S)
    hold.set()
    await ended(asked)
    model.script = [TextDelta("Another answer."), DONE]
    afterwards = await asyncio.wait_for(
        browser.post(f"{answered.path}/turns", json=again), PATIENCE_S
    )

    assert (refused.status_code, refused.json()["at_once"]) == (429, 1)
    assert (afterwards.status_code, said(afterwards)) == (200, "Another answer.")


async def waits_for_a_lock(db: AsyncSession) -> bool:
    """Whether a transaction on this test's database has asked for an advisory lock and
    waits for it, as Postgres says."""
    waiting = await db.scalar(
        text(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted "
            "AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
        )
    )
    await db.commit()  # the next look starts from what the database has then
    return bool(waiting)


async def test_two_questions_at_the_same_moment_do_not_both_pass_a_limit_of_one(
    scripted: Any, plans_with: Any, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turn that is being started is in nobody's count until its transaction is
    committed. Two questions counted side by side would each find none, and both pass.
    Here the first is held between its count and its commit, and the second comes: it
    must wait for the first's transaction to end before it counts."""
    _, browser = await scripted(TextDelta("An answer."), DONE, plans=plans_with(answers_at_once=1))
    counted, go_on = asyncio.Event(), asyncio.Event()
    opening = TurnEvents.open

    async def held_before_its_commit(events: TurnEvents, turn_id: uuid.UUID, first: bytes) -> None:
        if not counted.is_set():
            counted.set()
            await go_on.wait()
        await opening(events, turn_id, first)

    monkeypatch.setattr(TurnEvents, "open", held_before_its_commit)
    first = Chat(browser).asking("One")
    try:
        await asyncio.wait_for(counted.wait(), PATIENCE_S)
        second = Chat(browser).asking("Two, at the same moment")
        # The second question waits for the first's transaction, before it counts.
        await eventually(lambda: waits_for_a_lock(db))
    finally:
        go_on.set()  # whatever happened: the first one goes on, and the test can end
    answered, refused = await ended(first, second)

    assert (answered.status_code, refused.status_code) == (200, 429)


async def test_a_plan_that_says_nothing_of_answers_at_once_lets_an_account_have_many(
    scripted: Any,
) -> None:
    hold = asyncio.Event()
    model, browser = await scripted(TextDelta("An answer."), hold, DONE)
    asked = [Chat(browser).asking() for _ in range(4)]
    await until(lambda: len(model.requests) == 4)
    hold.set()
    responses = await ended(*asked)

    assert {response.status_code for response in responses} == {200}


# --- Readers, per account -------------------------------------------------------------


@pytest.fixture
def following(monkeypatch: pytest.MonkeyPatch) -> list[uuid.UUID]:
    """The turns readers have begun to follow, an entry for each reader. A reader is
    counted against its account before it follows: one that is here has its place."""
    followed: list[uuid.UUID] = []
    follow = TurnEvents.follow

    def noted(events: TurnEvents, turn_id: uuid.UUID) -> Any:
        followed.append(turn_id)
        return follow(events, turn_id)

    monkeypatch.setattr(TurnEvents, "follow", noted)
    return followed


TOO_MANY_READERS = (
    "You already have 6 windows or tabs reading answers as they are written, and an "
    "account may have 6 at once. Close one of them, and try again."
)


async def test_an_account_reads_along_in_six_places_at_once_and_a_seventh_is_refused(
    scripted: Any, following: list[uuid.UUID]
) -> None:
    hold = asyncio.Event()
    model, browser = await scripted(TextDelta("Half an "), hold, TextDelta("answer."), DONE)
    chat = Chat(browser)
    asked = chat.asking()  # the first reader: the question's own
    await held(model)
    turn_id = await chat.running_turn()
    joined = [chat.reading_along(turn_id) for _ in range(5)]
    await until(lambda: len(following) == 6)

    seventh = await chat.read_along(turn_id)
    hold.set()
    readers = await ended(asked, *joined)

    assert seventh.status_code == 429
    assert seventh.json()["detail"] == TOO_MANY_READERS
    assert "retry-after" not in seventh.headers
    assert [said(reader) for reader in readers] == ["Half an answer."] * 6


async def test_the_readers_of_one_account_keep_no_other_account_from_reading_along(
    scripted: Any, following: list[uuid.UUID]
) -> None:
    """Both accounts on the same app: readers are counted in the process."""
    hold = asyncio.Event()
    model, browser = await scripted(TextDelta("Half an "), hold, TextDelta("answer."), DONE)
    mine = Chat(browser)
    asked_mine = mine.asking()
    await held(model)
    my_turn = await mine.running_turn()
    joined_mine = [mine.reading_along(my_turn) for _ in range(5)]
    await until(lambda: len(following) == 6)  # all six sent, and reading

    # Another account, in the same browser: a session of its own.
    browser.cookies.clear()
    await browser.post("/api/session/guest")
    theirs = Chat(browser)
    asked_theirs = theirs.asking("Theirs")
    await until(lambda: len(following) == 7)
    joined_theirs = theirs.reading_along(await theirs.running_turn())
    await until(lambda: len(following) == 8)  # it reads: it wasn't refused
    hold.set()
    readers = await ended(asked_mine, *joined_mine, asked_theirs, joined_theirs)

    assert [said(reader) for reader in readers] == ["Half an answer."] * 8


async def test_a_reader_that_has_ended_makes_room(
    start_app: Any, settings: Settings, migrated: str, lineup: Any, following: list[uuid.UUID]
) -> None:
    assert migrated
    first_hold, second_hold = asyncio.Event(), asyncio.Event()
    first = lineup.add("standard", TextDelta("One."), first_hold, DONE)
    second = lineup.add("second", TextDelta("Two."), second_hold, DONE)
    browser = await guest_on(start_app, settings, lineup)
    one, two = Chat(browser), Chat(browser)
    asked_first = one.asking()
    await held(first)
    asked_second = two.asking(model="second")
    await until(lambda: len(second.requests) == 1)
    first_turn, second_turn = await one.running_turn(), await two.running_turn()
    first_joined = [one.reading_along(first_turn) for _ in range(2)]
    second_joined = [two.reading_along(second_turn) for _ in range(2)]
    await until(lambda: len(following) == 6)
    full = await one.read_along(first_turn)

    first_hold.set()
    await ended(asked_first, *first_joined)  # three readers have ended
    more = [two.reading_along(second_turn) for _ in range(3)]
    await until(lambda: len(following) == 9)
    full_again = await two.read_along(second_turn)
    second_hold.set()
    readers = await ended(asked_second, *second_joined, *more)

    assert (full.status_code, full_again.status_code) == (429, 429)
    assert [said(reader) for reader in readers] == ["Two."] * 6


async def test_the_reader_of_a_turn_just_started_is_never_refused_and_counts(
    start_app: Any, settings: Settings, migrated: str, lineup: Any, following: list[uuid.UUID]
) -> None:
    assert migrated
    first_hold, second_hold = asyncio.Event(), asyncio.Event()
    first = lineup.add("standard", TextDelta("One."), first_hold, DONE)
    second = lineup.add("second", TextDelta("Two."), second_hold, DONE)
    browser = await guest_on(start_app, settings, lineup)
    one, two = Chat(browser), Chat(browser)
    asked_first = one.asking()
    await held(first)
    first_turn = await one.running_turn()
    first_joined = [one.reading_along(first_turn) for _ in range(5)]
    await until(lambda: len(following) == 6)

    asked_second = two.asking(model="second")  # a seventh reader
    await until(lambda: len(following) == 7)
    past_the_limit = await one.read_along(first_turn)
    first_hold.set()
    await ended(asked_first, *first_joined)  # six have ended: the second question's is left
    second_turn = await two.running_turn()
    second_joined = [two.reading_along(second_turn) for _ in range(5)]
    await until(lambda: len(following) == 12)
    full = await two.read_along(second_turn)
    second_hold.set()
    readers = await ended(asked_second, *second_joined)

    assert len(second.requests) == 1
    assert past_the_limit.json()["detail"] == (
        "You already have 7 windows or tabs reading answers as they are written, and an "
        "account may have 6 at once. Close 2 of them, and try again."
    )
    assert full.status_code == 429  # 1 + 5: the question's own reader counts
    assert [said(reader) for reader in readers] == ["Two."] * 6


async def test_nothing_to_read_along_with_takes_no_room(
    scripted: Any, following: list[uuid.UUID]
) -> None:
    """A turn that has ended (204), or none at all (404): nothing is being read."""
    model, browser = await scripted(TextDelta("An answer."), DONE)
    done = Chat(browser)
    [answered] = await ended(done.asking())
    ended_turn = chunk(answered, "start")["messageMetadata"]["turn"]["id"]
    hold = asyncio.Event()
    model.script = [TextDelta("Half an "), hold, TextDelta("answer."), DONE]
    chat = Chat(browser)
    asked = chat.asking()
    await held(model)
    turn_id = await chat.running_turn()
    already = len(following)
    joined = [chat.reading_along(turn_id) for _ in range(4)]  # five readers
    await until(lambda: len(following) == already + 4)

    nothing = [await done.read_along(ended_turn) for _ in range(3)]
    no_such = [await chat.read_along(new_id()) for _ in range(3)]
    joined.append(chat.reading_along(turn_id))  # the sixth
    await until(lambda: len(following) == already + 5)
    hold.set()
    readers = await ended(asked, *joined)

    assert [response.status_code for response in nothing] == [204] * 3
    assert [response.status_code for response in no_such] == [404] * 3
    assert [said(reader) for reader in readers] == ["Half an answer."] * 6


async def test_a_reader_whose_response_could_send_nothing_is_given_back(valkey_url: str) -> None:
    """A web server that knows the client is gone says so at the response's first send
    (ASGI 2.4). Then the stream the response would read is never begun, and only the
    response itself can give the reader back. Uvicorn tells it otherwise, and the stream
    always begins: `test_turns_live.py` has that."""
    readers = turn_readers.TurnReaders()
    account = new_id()

    async def gone(message: Any) -> None:
        raise OSError(f"the client has gone, and {message['type']} can't be sent")

    async def nothing_more() -> dict[str, Any]:
        return {"type": "http.disconnect"}

    async with Valkey.from_url(valkey_url) as valkey:
        response = turn_routes._following(TurnEvents(valkey), new_id(), readers.join(account))
        with pytest.raises(ClientDisconnect):
            await response({"type": "http", "asgi": {"spec_version": "2.4"}}, nothing_more, gone)

    places = [readers.join(account) for _ in range(turn_readers.READERS_PER_ACCOUNT)]
    with pytest.raises(turn_readers.TooManyReadersError):
        readers.join(account)
    assert len(places) == 6


# --- When the first word came ---------------------------------------------------------
#
# The model's script moves the test's clock between the question, its pieces and the
# end (`clock.advance`), so that each moment is told apart from the others.


def moments(turn: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """When a turn, as the API says it, began, had its first word, and ended."""
    return turn["started_at"], turn["first_word_at"], turn["finished_at"]


async def test_a_turn_says_when_its_first_word_came_not_when_it_began_or_ended(
    scripted: Any, clock: Any
) -> None:
    """An empty piece is no word. The stream's first chunk knows of none yet, and the
    conversation, read afterwards, says the moment its last chunk said."""
    _, browser = await scripted(
        TextDelta(""),
        lambda: clock.advance(seconds=2),
        TextDelta("The "),
        lambda: clock.advance(seconds=3),
        TextDelta("saddle."),
        lambda: clock.advance(seconds=1),
        DONE,
    )
    chat = Chat(browser)

    [answered] = await ended(chat.asking())

    finished = chunk(answered, "finish")["messageMetadata"]["turn"]
    assert moments(finished) == (
        "2026-11-10T12:00:00Z",
        "2026-11-10T12:00:02Z",
        "2026-11-10T12:00:06Z",
    )
    assert chunk(answered, "start")["messageMetadata"]["turn"]["first_word_at"] is None
    stored = (await chat.read())["messages"][1]["metadata"]["turn"]
    assert stored["first_word_at"] == finished["first_word_at"]


async def test_reasoning_that_comes_first_is_the_first_word(scripted: Any, clock: Any) -> None:
    """The first sign that the model is at it, whatever kind of piece it is."""
    _, browser = await scripted(
        lambda: clock.advance(seconds=1),
        ReasoningDelta("Something to sit on."),
        lambda: clock.advance(seconds=4),
        TextDelta("The saddle."),
        DONE,
    )

    [answered] = await ended(Chat(browser).asking())

    turn = chunk(answered, "finish")["messageMetadata"]["turn"]
    assert moments(turn)[1] == "2026-11-10T12:00:01Z"


async def test_a_turn_whose_model_failed_before_a_word_has_no_first_word(
    scripted: Any, clock: Any
) -> None:
    _, browser = await scripted(
        lambda: clock.advance(seconds=1), ProviderUnavailableError("the server went away")
    )
    chat = Chat(browser)

    [answered] = await ended(chat.asking())

    turn = chunk(answered, "message-metadata")["messageMetadata"]["turn"]
    assert turn["status"] == "failed"
    assert moments(turn) == ("2026-11-10T12:00:00Z", None, "2026-11-10T12:00:01Z")
    assert (await chat.read())["unanswered"]["first_word_at"] is None


async def test_a_turn_stopped_while_it_waited_for_its_models_server_has_no_first_word(
    start_app: Any, settings: Settings, migrated: str, lineup: Any, clock: Any
) -> None:
    assert migrated
    gate, hold = Gate(1), asyncio.Event()
    first = lineup.add(
        "standard", TextDelta("The first "), hold, TextDelta("answer."), DONE, gate=gate
    )
    lineup.add("second", TextDelta("Never written."), DONE, gate=gate)
    browser = await guest_on(start_app, settings, lineup)
    one, two = Chat(browser), Chat(browser)
    asked_first = one.asking()
    await held(first)
    asked_second = two.asking(model="second")
    await until(lambda: gate.waiting == 1)
    clock.advance(seconds=5)

    stopped = await asyncio.wait_for(two.stop(await two.running_turn()), PATIENCE_S)
    hold.set()
    await ended(asked_first, asked_second)

    assert stopped.json()["status"] == "stopped"
    assert moments(stopped.json()) == ("2026-11-10T12:00:00Z", None, "2026-11-10T12:00:05Z")
    assert (await two.read())["unanswered"]["first_word_at"] is None


@pytest.mark.parametrize("ending", ["stopped", "failed"])
async def test_a_turn_that_ends_after_its_first_words_keeps_when_they_came(
    scripted: Any, clock: Any, ending: str
) -> None:
    """Stopped by whoever asked, or failed by its model, after some words."""
    hold = asyncio.Event()
    rest = (
        [ProviderUnavailableError("the server went away")]
        if ending == "failed"
        else [TextDelta("that nobody will read."), DONE]
    )
    model, browser = await scripted(
        lambda: clock.advance(seconds=1), TextDelta("Half an "), hold, *rest
    )
    chat = Chat(browser)
    asked = chat.asking()
    await held(model)
    clock.advance(seconds=5)

    if ending == "stopped":
        await chat.stop(await chat.running_turn())
    else:
        hold.set()
    await ended(asked)

    turn = (await chat.read())["messages"][1]["metadata"]["turn"]
    assert turn["status"] == ending
    assert moments(turn) == (
        "2026-11-10T12:00:00Z",
        "2026-11-10T12:00:01Z",
        "2026-11-10T12:00:06Z",
    )


async def test_an_answer_a_shutdown_interrupted_keeps_when_its_first_word_came(
    open_app: Any, offer: Any, settings: Settings, db: AsyncSession, clock: Any
) -> None:
    never = asyncio.Event()
    model, catalog = offer(lambda: clock.advance(seconds=1), TextDelta("Half an "), never, DONE)
    async with open_app(settings, catalog=catalog) as browser:
        await browser.post("/api/session/guest")
        asked = Chat(browser).asking()
        await held(model)
        clock.advance(seconds=5)
        # Leaving the block is the shutdown: it halts the turn.
    await ended(asked)

    turn = await the_turn(db)
    noon = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
    assert (turn.status, turn.first_word_at, turn.finished_at) == (
        "interrupted",
        noon + timedelta(seconds=1),
        noon + timedelta(seconds=6),
    )


async def test_a_turn_cancelled_from_outside_keeps_when_its_first_word_came(
    scripted: Any, clock: Any
) -> None:
    """Without a halt: a process going down faster than it can say so."""
    never = asyncio.Event()
    model, browser = await scripted(
        lambda: clock.advance(seconds=1), TextDelta("Half an "), never, DONE
    )
    chat = Chat(browser)
    asked = chat.asking()
    await held(model)
    task = writing(await chat.running_turn())
    clock.advance(seconds=5)

    task.cancel()
    await recorded(task)
    asked.cancel()  # its stream never got its last event: nothing would end it

    turn = (await chat.read())["messages"][1]["metadata"]["turn"]
    assert turn["status"] == "interrupted"
    assert moments(turn) == (
        "2026-11-10T12:00:00Z",
        "2026-11-10T12:00:01Z",
        "2026-11-10T12:00:06Z",
    )


async def test_a_turn_that_failed_inside_actio_keeps_when_its_first_word_came(
    scripted: Any, clock: Any
) -> None:
    """A mistake of Actio's own after some words, and not the model's: recorded as a
    failure like any other, with what had come and when."""
    _, browser = await scripted(
        lambda: clock.advance(seconds=1),
        TextDelta("Half an "),
        lambda: clock.advance(seconds=5),
        RuntimeError("something of Actio's own"),
    )
    chat = Chat(browser)

    await ended(chat.asking())

    turn = (await chat.read())["messages"][1]["metadata"]["turn"]
    assert (turn["status"], turn["error"]) == ("failed", "internal_error")
    assert moments(turn) == (
        "2026-11-10T12:00:00Z",
        "2026-11-10T12:00:01Z",
        "2026-11-10T12:00:06Z",
    )


async def test_a_turn_given_up_on_says_nothing_of_its_first_word(
    scripted: Any, db: AsyncSession, clock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its process noted when the first word came, and then fell silent: what it noted
    went with it. Its beats saved what the answer held, and nothing of that moment."""
    monkeypatch.setattr(turns, "BEAT", timedelta(milliseconds=10))
    hold = asyncio.Event()
    model, browser = await scripted(
        lambda: clock.advance(seconds=1), TextDelta("Sorry, "), hold, TextDelta("I'm late."), DONE
    )
    chat = Chat(browser)
    asked = chat.asking()
    await held(model)
    clock.advance(seconds=20)
    await beaten(lambda: said_alive_at(db, clock.now))  # with what was written by then

    async def lost(*_: Any, **__: Any) -> None:
        """A beat that doesn't reach the database."""

    monkeypatch.setattr(turns, "still_alive", lost)
    clock.advance(seconds=60)
    given_up_on = (await chat.read())["messages"][1]["metadata"]["turn"]
    hold.set()
    await ended(asked)

    assert given_up_on["status"] == "interrupted"
    assert moments(given_up_on) == ("2026-11-10T12:00:00Z", None, "2026-11-10T12:01:21Z")


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
