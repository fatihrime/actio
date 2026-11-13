"""Turns over a real connection: answers that arrive piece by piece, and readers who
leave before the end.

The app runs under uvicorn on a port of this machine, and the test reads the response
as it comes. The model is a `ScriptedModel`, which a test can hold half-way through
its answer.
"""

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx2
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

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

    async def conversation_once(self, ready: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
        """The conversation, as soon as `ready` says it is in the state to look at."""
        async with asyncio.timeout(PATIENCE_S):
            while True:
                found: dict[str, Any] = (await self.client.get(self.path)).json()
                if ready(found):
                    return found
                await asyncio.sleep(0.01)


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


async def test_leaving_in_the_middle_stops_the_model_and_keeps_what_had_arrived(
    served: Any, browser: httpx2.AsyncClient, db: AsyncSession
) -> None:
    """Pressing "stop", closing the tab, losing the network: to the server they are the
    same. The model is told to stop, and the half answer is kept, marked as stopped."""
    never = asyncio.Event()
    model, address = await served(
        ReasoningDelta("Let me think."),
        TextDelta("Half an "),
        TextDelta("answer"),
        never,
        TextDelta(" that nobody will read."),
        DONE,
    )
    reader = await guest_at(browser, address)

    async with reader.asking("Tell me something long") as response:
        await read_until(response, '"delta":"answer"')
        # Leaving the block closes the connection, with the answer unfinished.

    await asyncio.wait_for(model.closed.wait(), PATIENCE_S)  # the model's stream was closed
    conversation = await reader.conversation_once(settled)
    assert conversation["messages"][1]["parts"] == [
        {"type": "reasoning", "text": "Let me think."},
        {"type": "text", "text": "Half an answer"},
    ]
    turn = conversation["messages"][1]["metadata"]["turn"]
    assert (turn["status"], turn["finish_reason"], turn["error"]) == ("stopped", None, None)
    assert turn["output_tokens"] is None  # the model never got to report them
    assert turn["finished_at"] is not None
    assert conversation["unanswered"] is None
    assert (await count(db, Message), await count(db, Turn)) == (2, 1)


async def test_leaving_before_the_model_said_anything_leaves_the_question_unanswered(
    served: Any, browser: httpx2.AsyncClient, db: AsyncSession
) -> None:
    never = asyncio.Event()
    model, address = await served(never, TextDelta("Too late."), DONE)
    reader = await guest_at(browser, address)

    async with reader.asking("Hello?") as response:
        await read_until(response, '"type":"start-step"')
        await asyncio.wait_for(model.started.wait(), PATIENCE_S)

    await asyncio.wait_for(model.closed.wait(), PATIENCE_S)
    conversation = await reader.conversation_once(settled)
    assert [m["role"] for m in conversation["messages"]] == ["user"]
    assert conversation["unanswered"]["status"] == "stopped"
    assert await count(db, Message) == 1


async def test_a_conversation_can_go_on_after_its_answer_was_stopped(
    served: Any, browser: httpx2.AsyncClient
) -> None:
    """A turn that was stopped must not leave its conversation looking busy."""
    never = asyncio.Event()
    model, address = await served(TextDelta("Half"), never, DONE)
    reader = await guest_at(browser, address)
    async with reader.asking("First question") as response:
        await read_until(response, '"delta":"Half"')
    await reader.conversation_once(settled)
    model.script = [TextDelta("A whole answer."), DONE]

    again = await browser.post(f"{reader.path}/turns", json={"answer_to": str(reader.question)})

    assert again.status_code == 200
    assert '"delta":"A whole answer."' in again.text
    conversation = await reader.conversation_once(settled)
    assert len(conversation["messages"][1]["metadata"]["versions"]) == 2  # the half, and this one


async def test_deleting_a_conversation_while_it_is_being_answered_is_not_an_error(
    served: Any, browser: httpx2.AsyncClient, db: AsyncSession
) -> None:
    hold = asyncio.Event()
    model, address = await served(TextDelta("Half an "), hold, TextDelta("answer."), DONE)
    reader = await guest_at(browser, address)

    async with reader.asking("Tell me something") as response:
        await read_until(response, "Half an ")
        deleted = await browser.delete(reader.path, headers={"Origin": "http://localhost:8080"})
        hold.set()
        rest = await read_until(response, "[DONE]")

    assert deleted.status_code == 204
    assert '"type":"error"' not in rest  # the answer went to whoever was still reading
    assert model.closed.is_set()
    assert (await count(db, Conversation), await count(db, Message), await count(db, Turn)) == (
        0,
        0,
        0,
    )
