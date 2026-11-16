"""Turns over a real connection: answers that arrive piece by piece, and readers who
leave before the end, or come back.

The app runs under uvicorn on a port of this machine, and the test reads the response
as it comes. The model is a `ScriptedModel`, which a test can hold half-way through
its answer.

A turn runs apart from the request that started it. What that means with nobody
leaving, a second reader, a stop, a shutdown, is in `test_turns_running_api.py`, without
a server. Here is what only a connection that closes can show.
"""

import asyncio
import os
import signal
from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from typing import Any

import httpx2
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from actio.chat import running
from actio.chat.models import Conversation, Message, Turn
from actio.core.ids import new_id
from actio_llm import Finish, FinishReason, ReasoningDelta, TextDelta, Usage

pytestmark = pytest.mark.anyio

DONE = Finish(FinishReason.STOP, Usage(input_tokens=5, output_tokens=4))
PATIENCE_S = 5.0
"""How long a test waits for the server to notice something. It takes milliseconds;
this only keeps a broken server from hanging the test run."""


class Reader:
    """A guest's browser, reading one conversation from a served app."""

    def __init__(self, client: httpx2.AsyncClient, address: str) -> None:
        self.client = client
        self.question = new_id()
        self.path = f"{address}/api/conversations/{new_id()}"

    def asking(self, text: str) -> Any:
        """An open request that asks `text`: use it with `async with`."""
        message = {"id": str(self.question), "parts": [{"type": "text", "text": text}]}
        return self.client.stream("POST", f"{self.path}/turns", json={"message": message})

    def reading_along(self, turn_id: str) -> Any:
        """An open request that reads along with a turn: use it with `async with`."""
        return self.client.stream("GET", f"{self.path}/turns/{turn_id}/stream")

    async def conversation_once(self, ready: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
        """The conversation, as soon as `ready` says it is in the state to look at."""
        async with asyncio.timeout(PATIENCE_S):
            while True:
                found: dict[str, Any] = (await self.client.get(self.path)).json()
                if ready(found):
                    return found
                await asyncio.sleep(0.01)

    async def running_turn(self) -> str:
        """The id of the turn that is being written."""
        unanswered = (await self.client.get(self.path)).json()["unanswered"]
        assert unanswered["status"] == "running"
        return unanswered["id"]  # type: ignore[no-any-return]


_incoming: dict[int, tuple[AsyncIterator[str], list[str]]] = {}
"""For each response being read: where its reader is, and what has arrived so far. A
response's content can be iterated only once, so the iterator is kept between reads."""


async def read_until(response: httpx2.Response, marker: str) -> str:
    """Read on until `marker` has arrived, and return everything that came so far."""
    pieces, seen = _incoming.setdefault(id(response), (response.aiter_text(), []))
    async with asyncio.timeout(PATIENCE_S):
        while marker not in "".join(seen):
            try:
                seen.append(await anext(pieces))
            except StopAsyncIteration:
                raise AssertionError(f"the response ended without {marker!r}: {seen!r}") from None
    return "".join(seen)


@pytest.fixture
async def browser() -> AsyncIterator[httpx2.AsyncClient]:
    async with httpx2.AsyncClient(timeout=PATIENCE_S) as client:
        yield client


async def guest_at(browser: httpx2.AsyncClient, address: str) -> Reader:
    assert (await browser.post(f"{address}/api/session/guest")).status_code == 201
    return Reader(browser, address)


def settled(conversation: dict[str, Any]) -> bool:
    """Whether the conversation's last turn is over: nothing is being written any more."""
    last = conversation["messages"][-1]
    turn = conversation["unanswered"] or last["metadata"]["turn"]
    return turn is not None and turn["status"] != "running"


async def count(db: AsyncSession, table: type) -> int:
    return await db.scalar(select(func.count()).select_from(table)) or 0


async def test_an_answer_arrives_while_it_is_still_being_written(
    served: Any, browser: httpx2.AsyncClient
) -> None:
    hold = asyncio.Event()
    model, address = await served(
        TextDelta("The first half, "), hold, TextDelta("then the rest."), DONE
    )
    reader = await guest_at(browser, address)

    async with reader.asking("Tell me something") as response:
        early = await read_until(response, "The first half, ")
        written_so_far = model.closed.is_set()
        hold.set()
        rest = await read_until(response, "[DONE]")

    assert response.status_code == 200
    assert not written_so_far  # the model was still at it when the first half was read
    assert "then the rest." not in early
    assert '"type":"finish"' in rest
    conversation = await reader.conversation_once(settled)
    assert conversation["messages"][1]["parts"] == [
        {"type": "text", "text": "The first half, then the rest."}
    ]


async def test_leaving_in_the_middle_ends_nothing(
    served: Any, browser: httpx2.AsyncClient, db: AsyncSession
) -> None:
    """Closing the tab, losing the network, opening another conversation: to the server
    they are the same, and none of them stops the answer. It is written to its end, and
    is there when its reader comes back."""
    hold = asyncio.Event()
    model, address = await served(
        ReasoningDelta("Let me think."),
        TextDelta("Half an "),
        TextDelta("answer"),
        hold,
        TextDelta(" that is read later."),
        DONE,
    )
    reader = await guest_at(browser, address)

    async with reader.asking("Tell me something long") as response:
        await read_until(response, '"delta":"answer"')
        # Leaving the block closes the connection, with the answer unfinished.
    await asyncio.sleep(0.2)  # long enough for a server that minded to have minded
    meanwhile = (await browser.get(reader.path)).json()
    still_asked = not model.closed.is_set()
    hold.set()

    conversation = await reader.conversation_once(settled)
    assert still_asked
    assert meanwhile["unanswered"]["status"] == "running"
    assert conversation["messages"][1]["parts"] == [
        {"type": "reasoning", "text": "Let me think."},
        {"type": "text", "text": "Half an answer that is read later."},
    ]
    turn = conversation["messages"][1]["metadata"]["turn"]
    assert (turn["status"], turn["finish_reason"], turn["output_tokens"]) == (
        "completed",
        "stop",
        4,
    )
    assert (await count(db, Message), await count(db, Turn)) == (2, 1)


async def test_a_reader_who_left_can_read_along_again_from_the_beginning(
    served: Any, browser: httpx2.AsyncClient
) -> None:
    hold = asyncio.Event()
    _, address = await served(
        TextDelta("The first half, "), hold, TextDelta("then the rest."), DONE
    )
    reader = await guest_at(browser, address)
    async with reader.asking("Tell me something") as response:
        before = await read_until(response, "The first half, ")

    async with reader.reading_along(await reader.running_turn()) as again:
        early = await read_until(again, "The first half, ")
        hold.set()
        whole = await read_until(again, "[DONE]")

    assert again.status_code == 200
    assert again.headers["content-type"] == "text/event-stream; charset=utf-8"
    assert early == before  # what was read before is read again, from `start`
    assert whole.startswith('data: {"type":"start",')
    assert '"delta":"then the rest."' in whole
    assert whole.endswith("data: [DONE]\n\n")
    assert '"type":"finish"' in whole


async def test_one_reader_leaving_ends_nothing_for_another(
    served: Any, browser: httpx2.AsyncClient
) -> None:
    """Two windows on one conversation."""
    hold = asyncio.Event()
    _, address = await served(
        TextDelta("The first half, "), hold, TextDelta("then the rest."), DONE
    )
    reader = await guest_at(browser, address)

    async with reader.asking("Tell me something") as first:
        await read_until(first, "The first half, ")
        other_window = reader.reading_along(await reader.running_turn())
        second = await other_window.__aenter__()
        await read_until(second, "The first half, ")
    # The first window is closed. The second reads on.
    hold.set()
    rest = await read_until(second, "[DONE]")
    await other_window.__aexit__(None, None, None)

    assert '"delta":"then the rest."' in rest
    assert '"type":"finish"' in rest


async def test_stopping_ends_the_answer_for_whoever_is_reading_along(
    served: Any, browser: httpx2.AsyncClient
) -> None:
    never = asyncio.Event()
    model, address = await served(TextDelta("Half an "), TextDelta("answer"), never, DONE)
    reader = await guest_at(browser, address)

    async with reader.asking("Tell me something long") as response:
        await read_until(response, '"delta":"answer"')
        stopped = await browser.post(
            f"{reader.path}/turns/{await reader.running_turn()}/cancel",
            headers={"Origin": "http://localhost:8080"},
        )
        rest = await read_until(response, "[DONE]")

    assert (stopped.status_code, stopped.json()["status"]) == (200, "stopped")
    assert model.closed.is_set()
    assert '"status":"stopped"' in rest
    assert '"type":"error"' not in rest
    conversation = await reader.conversation_once(settled)
    assert conversation["messages"][1]["parts"] == [{"type": "text", "text": "Half an answer"}]


async def test_told_to_stop_the_process_ends_an_answer_while_its_reader_is_still_there(
    served: Any, browser: httpx2.AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stop as Docker sends it, all the way: the signal is heard before the web server
    acts on it; the answer gets its moment, and is halted; its reader is told, in the
    stream; and only then is the server gone. The server alone would have waited for
    the reader, who was waiting for the answer."""
    monkeypatch.setattr(running, "GRACE", timedelta(milliseconds=300))
    handled: list[int] = []
    # Whoever started the server gets the signal back when it has stopped. Without a
    # handler of the test's own, that would end the test run.
    before = signal.signal(signal.SIGTERM, lambda received, _: handled.append(received))
    try:
        never = asyncio.Event()
        model, address = await served(TextDelta("Half an "), TextDelta("answer"), never, DONE)
        reader = await guest_at(browser, address)

        async with reader.asking("Tell me something long") as response:
            await read_until(response, '"delta":"answer"')
            os.kill(os.getpid(), signal.SIGTERM)
            rest = await read_until(response, "[DONE]")
        async with asyncio.timeout(PATIENCE_S):
            while True:
                if handled:  # the server has stopped, and handed the signal on
                    break
                await asyncio.sleep(0.01)
    finally:
        signal.signal(signal.SIGTERM, before)

    assert '"status":"interrupted"' in rest
    assert '"type":"error"' not in rest
    assert model.closed.is_set()
    assert handled == [signal.SIGTERM]
    [turn] = (await db.scalars(select(Turn))).all()
    assert (turn.status, turn.credits) == ("interrupted", None)
    answer = await db.get(Message, turn.answer_id)
    assert answer is not None
    assert answer.parts == [{"type": "text", "text": "Half an answer"}]
    with pytest.raises(httpx2.ConnectError):  # nothing listens there any more
        await browser.get(reader.path)


async def test_deleting_a_conversation_while_it_is_being_answered_is_not_an_error(
    served: Any, browser: httpx2.AsyncClient, db: AsyncSession
) -> None:
    never = asyncio.Event()
    model, address = await served(TextDelta("Half an "), never, TextDelta("answer."), DONE)
    reader = await guest_at(browser, address)

    async with reader.asking("Tell me something") as response:
        await read_until(response, "Half an ")
        deleted = await browser.delete(reader.path, headers={"Origin": "http://localhost:8080"})
        rest = await read_until(response, "[DONE]")

    assert deleted.status_code == 204
    assert '"type":"error"' not in rest  # whoever was still reading was told it had stopped
    assert model.closed.is_set()
    assert (await count(db, Conversation), await count(db, Message), await count(db, Turn)) == (
        0,
        0,
        0,
    )
