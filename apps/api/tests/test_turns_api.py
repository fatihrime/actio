"""Turns over HTTP: a question goes in, the answer streams back, and both are kept.

The model is fakellm, in process. It echoes the question, and directives in the question
script the rest: `[[think: …]]` reasons first, `[[fail: 503]]` fails, `[[drop: 2]]`
breaks off after two words. Where a test needs to see what the model was sent, or to
hold an answer half-way, a `ScriptedModel` stands in instead.

A response here is read after it is complete. What happens when the reader leaves in
the middle is in `test_turns_live.py`, which runs a real server.
"""

import asyncio
import json
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from actio.chat import turns
from actio.chat.models import Conversation, Message, Turn
from actio.chat.parts import TextPart
from actio.core.ids import new_id
from actio.core.rate_limit import Rate
from actio.core.settings import Settings
from actio_llm import (
    Assistant,
    ContextOverflowError,
    Finish,
    FinishReason,
    ModelNotFoundError,
    ProviderError,
    ReasoningDelta,
    RequestRejectedError,
    System,
    TextDelta,
    ToolCall,
    Usage,
    User,
)

pytestmark = pytest.mark.anyio

PROBLEM = "application/problem+json"
TODAY = System("Today's date is 10 November 2026.")  # the test clock's day


# --- Helpers --------------------------------------------------------------------------


def chunks(response: httpx.Response) -> list[Any]:
    """The chunks of a streamed answer, in order. The last one is the text `[DONE]`."""
    events = [event for event in response.text.split("\n\n") if event]
    assert all(event.startswith("data: ") for event in events), response.text
    payloads = [event.removeprefix("data: ") for event in events]
    return [payload if payload == "[DONE]" else json.loads(payload) for payload in payloads]


def kinds(response: httpx.Response) -> list[str]:
    return [chunk if isinstance(chunk, str) else chunk["type"] for chunk in chunks(response)]


def said(response: httpx.Response, kind: str = "text") -> str:
    """The text of an answer's stream, or its reasoning."""
    return "".join(c["delta"] for c in chunks(response)[:-1] if c["type"] == f"{kind}-delta")


def chunk(response: httpx.Response, kind: str) -> dict[str, Any]:
    [found] = [c for c in chunks(response)[:-1] if c["type"] == kind]
    return found  # type: ignore[no-any-return]


def asking(text: str, *, after: Any = None, **more: Any) -> dict[str, Any]:
    """The body of a request that asks `text`, under a new message id. `after` is the
    answer the question follows: its id, or the response that streamed it."""
    if isinstance(after, httpx.Response):
        after = chunk(after, "start")["messageId"]
    message = {"id": str(new_id()), "parts": [{"type": "text", "text": text}]}
    return {"message": message, "parent_id": after, **more}


async def guest(client: httpx.AsyncClient) -> uuid.UUID:
    return uuid.UUID((await client.post("/api/session/guest")).json()["id"])


class Chat:
    """One conversation, as a browser would hold it: an id of its own choosing."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.id = new_id()
        self.path = f"/api/conversations/{self.id}"

    async def turn(self, body: dict[str, Any]) -> httpx.Response:
        return await self.client.post(f"{self.path}/turns", json=body)

    async def ask(self, text: str, **more: Any) -> httpx.Response:
        return await self.turn(asking(text, **more))

    async def read(self) -> dict[str, Any]:
        response = await self.client.get(self.path)
        assert response.status_code == 200, response.text
        return response.json()  # type: ignore[no-any-return]

    async def texts(self) -> list[str]:
        return ["".join(p["text"] for p in m["parts"]) for m in (await self.read())["messages"]]


@pytest.fixture
async def chat(api: httpx.AsyncClient) -> Chat:
    """A guest's browser, about to start a conversation."""
    await guest(api)
    return Chat(api)


async def count(db: AsyncSession, model: type) -> int:
    return await db.scalar(select(func.count()).select_from(model)) or 0


# --- The answer, as a stream ----------------------------------------------------------


async def test_a_question_is_answered_as_a_stream(chat: Chat, server_ids: Any) -> None:
    question = asking("hello there")

    response = await chat.turn(question)

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/event-stream; charset=utf-8"
    assert response.headers["x-vercel-ai-ui-message-stream"] == "v1"
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    assert kinds(response) == [
        "start",
        "start-step",
        "text-start",
        "text-delta",
        "text-delta",
        "text-delta",
        "text-end",
        "finish-step",
        "finish",
        "[DONE]",
    ]
    assert said(response) == "Echo: hello there"
    turn_id, answer_id = (str(made) for made in server_ids.made)
    start, finish = chunk(response, "start"), chunk(response, "finish")
    assert start["messageId"] == answer_id
    assert start["messageMetadata"]["parent_id"] == question["message"]["id"]
    assert start["messageMetadata"]["versions"] == [answer_id]
    assert start["messageMetadata"]["turn"]["id"] == turn_id
    assert start["messageMetadata"]["turn"]["status"] == "running"
    assert finish["finishReason"] == "stop"
    assert finish["messageMetadata"]["turn"] == {
        "id": turn_id,
        "model": "standard",
        "thinking": False,
        "status": "completed",
        "finish_reason": "stop",
        "error": None,
        "input_tokens": 8,  # six for the line that gives the date, two for the question
        "cached_input_tokens": 0,
        "output_tokens": 3,
        "load_ms": None,
        "prompt_ms": None,
        "generation_ms": None,
        "started_at": "2026-11-10T12:00:00Z",
        "finished_at": "2026-11-10T12:00:00Z",
    }


async def test_the_first_question_creates_the_conversation_and_gives_it_a_name(
    chat: Chat,
) -> None:
    question = asking("How do I fix a flat tyre?")
    assert (await chat.client.get(chat.path)).status_code == 404

    response = await chat.turn(question)

    conversation = await chat.read()
    assert conversation["id"] == str(chat.id)  # the id the browser chose
    assert conversation["title"] == "How do I fix a flat tyre?"
    assert [m["role"] for m in conversation["messages"]] == ["user", "assistant"]
    asked, answered = conversation["messages"]
    assert asked["id"] == question["message"]["id"]  # and the id it chose for its message
    assert asked["parts"] == [{"type": "text", "text": "How do I fix a flat tyre?"}]
    assert answered["parts"] == [{"type": "text", "text": "Echo: How do I fix a flat tyre?"}]
    assert conversation["current_leaf_id"] == answered["id"]
    assert conversation["unanswered"] is None
    # What was stored is what the stream said, to the letter.
    assert answered["id"] == chunk(response, "start")["messageId"]
    assert answered["metadata"] == chunk(response, "finish")["messageMetadata"]
    listed = (await chat.client.get("/api/conversations")).json()["items"]
    assert [item["title"] for item in listed] == ["How do I fix a flat tyre?"]


@pytest.mark.parametrize(
    ("question", "title"),
    [
        ("  Why   is the\tsky blue?  ", "Why is the sky blue?"),
        ("First line\nSecond line", "First line"),
        (
            "Tell me everything there is to know about the long history of the bicycle",
            "Tell me everything there is to know about the long history…",
        ),
        ("x" * 80, "x" * 60 + "…"),
    ],
)
async def test_a_conversation_is_named_after_its_first_question(
    chat: Chat, question: str, title: str
) -> None:
    await chat.ask(question)
    await chat.ask("And something else entirely", after=(await chat.read())["current_leaf_id"])

    assert (await chat.read())["title"] == title  # and later questions don't rename it


async def test_reasoning_streams_before_the_answer_and_is_kept_apart(chat: Chat) -> None:
    response = await chat.ask(
        "[[think: Blue light scatters most.]] Why is the sky blue?", thinking=True
    )

    assert kinds(response) == [
        "start",
        "start-step",
        "reasoning-start",
        *["reasoning-delta"] * 4,
        "reasoning-end",
        "text-start",
        *["text-delta"] * 6,
        "text-end",
        "finish-step",
        "finish",
        "[DONE]",
    ]
    assert said(response, "reasoning") == "Blue light scatters most."
    assert said(response) == "Echo: Why is the sky blue?"
    ids = {c["id"] for c in chunks(response)[:-1] if "id" in c}
    assert ids == {"reasoning-0", "text-1"}  # each part of the answer has its own
    answered = (await chat.read())["messages"][1]
    assert answered["parts"] == [
        {"type": "reasoning", "text": "Blue light scatters most."},
        {"type": "text", "text": "Echo: Why is the sky blue?"},
    ]
    assert answered["metadata"]["turn"]["thinking"] is True


async def test_an_answer_cut_off_at_the_limit_says_so(scripted: Any) -> None:
    model, browser = await scripted(
        TextDelta("One two"), Finish(FinishReason.LENGTH, Usage(input_tokens=9, output_tokens=2))
    )
    chat = Chat(browser)

    response = await chat.ask("Count to a hundred")

    assert chunk(response, "finish")["finishReason"] == "length"
    turn = (await chat.read())["messages"][1]["metadata"]["turn"]
    assert (turn["status"], turn["finish_reason"]) == ("completed", "length")
    assert model.requests[0].max_output_tokens == 512  # the catalog's limit for the model


# --- What the model is shown ----------------------------------------------------------


async def test_the_model_is_sent_the_conversation_so_far_by_the_server(scripted: Any) -> None:
    """The browser sends one new message. The history comes from the database, so a
    client can't put words into a conversation that were never said."""
    model, browser = await scripted(
        ReasoningDelta("Thinking about bikes."),
        TextDelta("The saddle."),
        Finish(FinishReason.STOP, Usage(input_tokens=12, output_tokens=6)),
    )
    chat = Chat(browser)
    first = await chat.ask("Name a bike part.", thinking=True)

    await chat.ask("Why that one?", after=first)

    assert [request.messages for request in model.requests] == [
        [TODAY, User("Name a bike part.")],
        # The earlier answer goes back without its reasoning, which was never said.
        [TODAY, User("Name a bike part."), Assistant("The saddle."), User("Why that one?")],
    ]
    asked = model.requests[0]
    assert (asked.model, asked.context_window, asked.max_output_tokens) == ("scripted-1", 4096, 512)
    assert asked.tools == ()


async def test_only_the_branch_that_leads_to_the_question_is_sent(scripted: Any) -> None:
    model, browser = await scripted(TextDelta("An answer."), Finish(FinishReason.STOP, None))
    chat = Chat(browser)
    first = await chat.ask("First question")
    await chat.ask("A follow-up that will be reworded", after=first)

    await chat.ask("The follow-up, reworded", after=first)

    assert model.requests[-1].messages == [
        TODAY,
        User("First question"),
        Assistant("An answer."),
        User("The follow-up, reworded"),
    ]


@pytest.mark.parametrize(
    ("can_reason", "asked_to", "sent"),
    [
        (True, True, True),
        (True, False, False),  # a model that reasons by default has to be told not to
        (False, False, None),  # a model that can't must not be told anything
    ],
)
async def test_reasoning_is_asked_for_only_from_a_model_that_can(
    scripted: Any, can_reason: bool, asked_to: bool, sent: bool | None
) -> None:
    model, browser = await scripted(
        TextDelta("Fine."), Finish(FinishReason.STOP, None), thinking=can_reason
    )

    await Chat(browser).ask("How are you?", thinking=asked_to)

    assert model.requests[0].thinking is sent


# --- Asking again, in other words or for another answer -------------------------------


async def test_rewording_a_question_starts_a_branch_beside_the_old_one(chat: Chat) -> None:
    first = await chat.ask("Name a bike part.")
    old = asking("Why that one", after=first)
    await chat.turn(old)

    new = asking("Why that one, of all parts?", after=first)
    response = await chat.turn(new)

    assert response.status_code == 200
    assert await chat.texts() == [
        "Name a bike part.",
        "Echo: Name a bike part.",
        "Why that one, of all parts?",
        "Echo: Why that one, of all parts?",
    ]
    reworded = (await chat.read())["messages"][2]
    assert reworded["metadata"]["versions"] == [old["message"]["id"], new["message"]["id"]]


async def test_the_first_question_can_be_reworded_too(chat: Chat) -> None:
    await chat.ask("Helo")

    await chat.ask("Hello")  # no `after`: another first question

    assert await chat.texts() == ["Hello", "Echo: Hello"]
    assert len((await chat.read())["messages"][0]["metadata"]["versions"]) == 2
    assert (await chat.read())["title"] == "Helo"  # named once


async def test_a_question_can_be_answered_again(chat: Chat, db: AsyncSession) -> None:
    question = asking("Name a bike part.")
    first = await chat.turn(question)

    second = await chat.turn({"answer_to": question["message"]["id"]})

    assert second.status_code == 200
    first_id, second_id = chunk(first, "start")["messageId"], chunk(second, "start")["messageId"]
    assert chunk(second, "start")["messageMetadata"]["versions"] == [first_id, second_id]
    conversation = await chat.read()
    assert [m["id"] for m in conversation["messages"]] == [question["message"]["id"], second_id]
    assert conversation["messages"][1]["metadata"]["versions"] == [first_id, second_id]
    assert (await count(db, Message), await count(db, Turn)) == (3, 2)  # one question, two answers


async def test_an_earlier_question_can_be_answered_again_from_any_branch(chat: Chat) -> None:
    question = asking("Name a bike part.")
    first = await chat.turn(question)
    await chat.ask("Why that one?", after=first)

    again = await chat.turn({"answer_to": question["message"]["id"]})

    assert again.status_code == 200
    assert await chat.texts() == ["Name a bike part.", "Echo: Name a bike part."]
    assert (await chat.read())["current_leaf_id"] == chunk(again, "start")["messageId"]


# --- When the model fails -------------------------------------------------------------


async def test_a_model_that_cannot_be_reached_fails_the_turn_inside_the_stream(
    chat: Chat, db: AsyncSession
) -> None:
    question = asking("[[fail: 503]] Hello?")

    response = await chat.turn(question)

    assert response.status_code == 200  # the turn had started: what follows is in the stream
    assert kinds(response) == ["start", "start-step", "message-metadata", "error", "[DONE]"]
    assert chunk(response, "error")["errorText"] == (
        "The model's server can't be reached, or is too busy. Try again in a moment."
    )
    turn = chunk(response, "message-metadata")["messageMetadata"]["turn"]
    assert (turn["status"], turn["error"], turn["output_tokens"]) == (
        "failed",
        "model_unavailable",
        None,
    )
    # The question is kept, without an answer, and the conversation says why.
    conversation = await chat.read()
    assert [m["id"] for m in conversation["messages"]] == [question["message"]["id"]]
    assert conversation["unanswered"] == turn
    assert await count(db, Message) == 1


async def test_an_answer_that_breaks_off_keeps_what_had_arrived(chat: Chat) -> None:
    response = await chat.ask("[[drop: 2]] one two three four")

    assert kinds(response) == [
        "start",
        "start-step",
        "text-start",
        "text-delta",
        "text-delta",
        "text-end",
        "message-metadata",
        "error",
        "[DONE]",
    ]
    assert said(response) == "Echo: one "
    assert chunk(response, "error")["errorText"] == (
        "The answer broke off: the connection to the model was lost."
    )
    answered = (await chat.read())["messages"][1]
    assert answered["parts"] == [{"type": "text", "text": "Echo: one "}]
    assert (answered["metadata"]["turn"]["status"], answered["metadata"]["turn"]["error"]) == (
        "failed",
        "answer_interrupted",
    )


@pytest.mark.parametrize(
    ("failure", "code", "explanation"),
    [
        (
            ContextOverflowError("request (9000 tokens) exceeds the context size (4096 tokens)"),
            "context_overflow",
            "This conversation has become too long for the model. "
            "Start a new one, or ask again in fewer words.",
        ),
        (ModelNotFoundError("scripted-1"), "model_missing", "The model isn't on its server."),
        (
            RequestRejectedError("no such option"),
            "model_refused",
            "The model's server refused the request.",
        ),
        (ProviderError("HTTP 500"), "model_error", "The model's server failed."),
        (
            ToolCall("call-1", "get_weather", {"city": "Izmir"}),  # nothing offered it tools
            "model_error",
            "The model's server failed.",
        ),
    ],
)
async def test_each_kind_of_failure_has_a_code_and_words_for_a_person(
    scripted: Any, failure: Any, code: str, explanation: str
) -> None:
    _, browser = await scripted(failure)
    chat = Chat(browser)

    response = await chat.ask("Hello?")

    assert chunk(response, "error")["errorText"] == explanation
    assert (await chat.read())["unanswered"]["error"] == code
    assert "9000 tokens" not in response.text  # the server's own words stay in its log


async def test_a_question_that_got_no_answer_can_be_answered_again(chat: Chat) -> None:
    question = asking("[[fail: 503]] Hello?")
    await chat.turn(question)

    # The same question, asked of a model that is back. Here it fails again, as scripted.
    again = await chat.turn({"answer_to": question["message"]["id"]})
    reworded = await chat.ask("Hello?")

    assert kinds(again)[-3:] == ["message-metadata", "error", "[DONE]"]
    assert await chat.texts() == ["Hello?", "Echo: Hello?"]
    assert kinds(reworded)[-2:] == ["finish", "[DONE]"]


# --- Refused before a model is asked --------------------------------------------------


async def test_a_turn_needs_a_session(api: httpx.AsyncClient) -> None:
    response = await Chat(api).ask("Hello")

    assert response.status_code == 401
    assert response.headers["content-type"] == PROBLEM


async def test_another_sites_page_cannot_ask_for_a_visitor(chat: Chat) -> None:
    response = await chat.client.post(
        f"{chat.path}/turns", json=asking("Hello"), headers={"Sec-Fetch-Site": "cross-site"}
    )

    assert response.status_code == 403


async def test_nobody_can_ask_in_someone_elses_conversation(
    start_app: Any, settings: Settings, migrated: str, db: AsyncSession
) -> None:
    assert migrated
    owner, stranger = Chat(await start_app(settings)), Chat(await start_app(settings))
    await guest(owner.client)
    await guest(stranger.client)
    first = await owner.ask("A private question")
    stranger.id, stranger.path = owner.id, owner.path  # the stranger learned the id

    continuing = await stranger.ask("What did they ask?", after=first)
    again = await stranger.turn({"answer_to": (await owner.read())["messages"][0]["id"]})
    starting = await stranger.ask("A first question, under their id")

    assert (continuing.status_code, again.status_code) == (404, 404)
    assert continuing.json()["detail"] == again.json()["detail"] == "No such conversation."
    assert starting.status_code == 409
    assert starting.json()["detail"] == "This id is already in use. Choose another."
    assert await owner.texts() == ["A private question", "Echo: A private question"]
    assert await count(db, Conversation) == 1


async def test_a_question_follows_an_answer_of_the_same_conversation(
    chat: Chat, db: AsyncSession
) -> None:
    question = asking("Name a bike part.")
    await chat.turn(question)
    other = Chat(chat.client)
    elsewhere = await other.ask("Another conversation")

    after_a_question = await chat.ask("And?", after=question["message"]["id"])
    after_nothing_known = await chat.ask("And?", after=str(new_id()))
    after_another_conversations = await chat.ask("And?", after=elsewhere)
    answering_an_answer = await chat.turn({"answer_to": (await chat.read())["current_leaf_id"]})
    answering_nothing_known = await chat.turn({"answer_to": str(new_id())})

    assert after_a_question.status_code == answering_an_answer.status_code == 409
    assert "take turns" in after_a_question.json()["detail"]
    for response in (after_nothing_known, after_another_conversations, answering_nothing_known):
        assert response.status_code == 404
        assert response.json()["detail"] == "This conversation has no such message."
    assert await chat.texts() == ["Name a bike part.", "Echo: Name a bike part."]
    assert await count(db, Turn) == 2  # one in each conversation: nothing refused left a trace


async def test_a_conversation_that_does_not_exist_cannot_be_continued(chat: Chat) -> None:
    continuing = await chat.ask("And then?", after=str(new_id()))
    again = await chat.turn({"answer_to": str(new_id())})

    assert (continuing.status_code, again.status_code) == (404, 404)
    assert (await chat.client.get(chat.path)).status_code == 404  # and wasn't created


async def test_sending_a_message_twice_does_not_ask_twice(chat: Chat, db: AsyncSession) -> None:
    """A browser that didn't hear back sends its request again. The message's id says
    it is the same message."""
    question = asking("Name a bike part.")
    first = await chat.turn(question)

    again = await chat.turn(question)

    assert again.status_code == 409
    assert again.headers["content-type"] == PROBLEM
    turn = chunk(first, "start")["messageMetadata"]["turn"]["id"]
    assert again.json() == {
        "type": "about:blank",
        "title": "Conflict",
        "status": 409,
        "detail": "This message was already sent.",
        "turn_id": turn,
        "turn_status": "completed",
    }
    assert (await count(db, Message), await count(db, Turn)) == (2, 1)


async def test_an_id_that_belongs_to_another_message_cannot_be_reused(chat: Chat) -> None:
    question = asking("Name a bike part.")
    first = await chat.turn(question)
    answer_id = chunk(first, "start")["messageId"]
    other = Chat(chat.client)

    as_an_answers_id = await chat.turn(
        asking("And?", after=first)
        | {"message": {"id": answer_id, "parts": [{"type": "text", "text": "And?"}]}}
    )
    in_another_conversation = await other.turn(question)

    for response in (as_an_answers_id, in_another_conversation):
        assert response.status_code == 409
        assert response.json()["detail"] == "This id is already in use. Choose another."
    assert (await other.client.get(other.path)).status_code == 404  # not created for nothing


@pytest.mark.parametrize(
    ("body", "where"),
    [
        ({}, "body"),
        (
            {
                "answer_to": str(uuid.uuid4()),
                "message": {"id": str(uuid.uuid4()), "parts": [{"type": "text", "text": "x"}]},
            },
            "body",
        ),
        ({"answer_to": str(uuid.uuid4()), "parent_id": str(uuid.uuid4())}, "body"),
        ({"message": {"id": str(uuid.uuid4()), "parts": []}}, "body.message.parts"),
        (
            {"message": {"id": str(uuid.uuid4()), "parts": [{"type": "text", "text": "  \n "}]}},
            "body.message",
        ),
        (
            {
                "message": {
                    "id": str(uuid.uuid4()),
                    "parts": [{"type": "text", "text": "x" * 100_001}],
                }
            },
            "body.message",
        ),
        (
            {"message": {"id": str(uuid.uuid4()), "parts": [{"type": "reasoning", "text": "x"}]}},
            "body.message.parts.0.type",
        ),
        ({"message": {"id": "one", "parts": [{"type": "text", "text": "x"}]}}, "body.message.id"),
        (
            {
                "message": {
                    "id": str(uuid.uuid4()),
                    "role": "assistant",
                    "parts": [{"type": "text", "text": "x"}],
                }
            },
            "body.message.role",
        ),
        (
            {
                "message": {"id": str(uuid.uuid4()), "parts": [{"type": "text", "text": "x"}]},
                "history": [],
            },
            "body.history",
        ),
    ],
)
async def test_a_request_that_is_not_one_question_is_refused(
    chat: Chat, db: AsyncSession, body: dict[str, Any], where: str
) -> None:
    response = await chat.turn(body)

    assert response.status_code == 422
    assert response.headers["content-type"] == PROBLEM
    assert response.json()["errors"][0]["where"] == where
    assert await count(db, Conversation) == 0


async def test_a_model_is_asked_for_by_its_name_in_the_catalog(
    chat: Chat, db: AsyncSession
) -> None:
    by_name = await chat.ask("Hello", model="standard")
    unknown = await Chat(chat.client).ask("Hello", model="largest")

    assert by_name.status_code == 200
    assert unknown.status_code == 422
    assert unknown.json()["detail"] == "No model is called 'largest'. See GET /api/models."
    assert await count(db, Conversation) == 1


async def test_a_model_that_cannot_reason_is_not_asked_to(scripted: Any, db: AsyncSession) -> None:
    model, browser = await scripted(
        TextDelta("Hi."), Finish(FinishReason.STOP, None), thinking=False
    )

    response = await Chat(browser).ask("Hello", thinking=True)

    assert response.status_code == 422
    assert response.json()["detail"] == "This model can't reason before it answers."
    assert model.requests == []
    assert await count(db, Conversation) == 0


async def test_a_model_that_is_not_on_its_server_is_not_asked(
    start_app: Any,
    settings: Settings,
    migrated: str,
    db: AsyncSession,
    catalog_without_its_model: Any,
) -> None:
    """Better an answer that says what to do than a question stored without one."""
    assert migrated
    browser = await start_app(settings, catalog=catalog_without_its_model)
    await guest(browser)

    response = await Chat(browser).ask("Hello")

    assert response.status_code == 503
    assert response.headers["content-type"] == PROBLEM
    assert response.json() == {
        "type": "about:blank",
        "title": "Service Unavailable",
        "status": 503,
        "detail": "This model isn't on its server yet.",
        "model": "standard",
        "model_status": "missing",
    }
    assert await count(db, Conversation) == 0


async def test_a_model_whose_server_is_away_is_not_asked(
    start_app: Any,
    settings: Settings,
    migrated: str,
    db: AsyncSession,
    catalog_without_its_server: Any,
) -> None:
    assert migrated
    browser = await start_app(settings, catalog=catalog_without_its_server)
    await guest(browser)

    response = await Chat(browser).ask("Hello")

    assert response.status_code == 503
    assert response.json()["detail"] == "This model's server can't be reached."
    assert response.json()["model_status"] == "unreachable"
    assert await count(db, Conversation) == 0


async def test_messages_are_limited_per_account(
    start_app: Any, settings: Settings, migrated: str, clock: Any
) -> None:
    assert migrated
    two_an_hour = settings.model_copy(update={"limit_turns_per_user": Rate.parse("2/hour")})
    mine, yours = Chat(await start_app(two_an_hour)), Chat(await start_app(two_an_hour))
    await guest(mine.client)
    await guest(yours.client)
    first = await mine.ask("One")
    second = await mine.ask("Two", after=first)

    refused = await mine.ask("Three", after=second)

    assert refused.status_code == 429
    assert refused.headers["retry-after"] == "1800"
    assert refused.json()["detail"] == "Too many messages in a short time. Try again in 30 minutes."
    assert (await yours.ask("One")).status_code == 200  # each account has its own allowance
    assert await mine.texts() == ["One", "Echo: One", "Two", "Echo: Two"]  # nothing half-written
    clock.advance(minutes=30)
    assert (await mine.ask("Three", after=second)).status_code == 200


# --- One answer at a time -------------------------------------------------------------


async def test_a_conversation_is_answered_one_question_at_a_time(scripted: Any) -> None:
    hold = asyncio.Event()
    model, browser = await scripted(
        TextDelta("Half an "), hold, TextDelta("answer."), Finish(FinishReason.STOP, None)
    )
    chat, elsewhere = Chat(browser), Chat(browser)
    question = asking("A long question")
    first = asyncio.create_task(chat.turn(question))
    await model.started.wait()

    meanwhile = await chat.ask("Another, before the answer came")
    again = await chat.turn({"answer_to": question["message"]["id"]})
    running = await chat.read()
    in_another_conversation = asyncio.create_task(elsewhere.ask("A question somewhere else"))
    hold.set()
    answered = await first

    for refused in (meanwhile, again):
        assert refused.status_code == 409
        assert refused.json()["detail"] == "This conversation is still being answered."
        assert refused.json()["turn_id"] == running["unanswered"]["id"]
    assert running["unanswered"]["status"] == "running"
    assert said(answered) == "Half an answer."
    assert (await in_another_conversation).status_code == 200  # other conversations go on
    assert await chat.texts() == ["A long question", "Half an answer."]
    assert (await chat.ask("Another, now", after=answered)).status_code == 200


async def test_a_turn_whose_server_died_is_given_up_on_after_ten_minutes(
    chat: Chat, db: AsyncSession, clock: Any
) -> None:
    """Nothing finishes a turn when the process that ran it is killed. Without this the
    conversation could never be answered again."""
    me = uuid.UUID((await chat.client.get("/api/me")).json()["id"])
    question = new_id()
    left_running = await turns.ask(
        db,
        user_id=me,
        conversation_id=chat.id,
        message_id=question,
        parent_id=None,
        parts=[TextPart(text="Asked just before the server died")],
        model="standard",
        thinking=False,
        turn_id=new_id(),
        now=clock.now,
    )
    await db.commit()

    clock.advance(minutes=9, seconds=59)
    too_early = await chat.turn({"answer_to": str(question)})
    still = (await chat.read())["unanswered"]["status"]
    clock.advance(seconds=1)
    noticed = (await chat.read())["unanswered"]
    again = await chat.turn({"answer_to": str(question)})

    assert too_early.status_code == 409
    assert still == "running"
    assert (noticed["id"], noticed["status"]) == (str(left_running.turn.id), "interrupted")
    assert noticed["finished_at"] == "2026-11-10T12:10:00Z"
    assert again.status_code == 200
    assert await chat.texts() == [
        "Asked just before the server died",
        "Echo: Asked just before the server died",
    ]


async def test_the_route_is_in_the_api_description(api: httpx.AsyncClient) -> None:
    paths = (await api.get("/api/openapi.json")).json()["paths"]

    turn = paths["/api/conversations/{conversation_id}/turns"]["post"]
    assert "text/event-stream" in turn["responses"]["200"]["content"]
    assert set(turn["responses"]) >= {"200", "404", "409", "422", "429", "503"}
