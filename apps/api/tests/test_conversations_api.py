"""Conversations over HTTP: listing, reading, renaming, showing a branch, deleting.

Turns, which add messages, come in the next step. Until then these tests put the
messages into the database through `actio.chat`, and read them back through the API.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from actio.chat import conversations, tree
from actio.chat.models import Conversation, Message
from actio.chat.parts import ReasoningPart, TextPart
from actio.core.ids import new_id
from actio.core.settings import Settings

pytestmark = pytest.mark.anyio

PROBLEM = "application/problem+json"
NOON = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
DENIZ = {"email": "deniz@example.com", "password": "correct horse battery staple"}


def later(minutes: float) -> datetime:
    return NOON + timedelta(minutes=minutes)


async def guest(client: httpx.AsyncClient) -> uuid.UUID:
    """Start a guest session in this browser, and say whose it is."""
    return uuid.UUID((await client.post("/api/session/guest")).json()["id"])


async def started(
    db: AsyncSession, user_id: uuid.UUID, *, at: datetime = NOON, title: str | None = None
) -> Conversation:
    conversation = await conversations.create(db, new_id(), user_id=user_id, now=at)
    conversation.title = title
    await db.commit()
    return conversation


async def said(
    db: AsyncSession,
    conversation: Conversation,
    role: str,
    text: str,
    after: Message | None,
    *,
    at: datetime,
) -> Message:
    message = await tree.append(
        db,
        conversation,
        message_id=new_id(),
        parent_id=after.id if after else None,
        role=role,  # type: ignore[arg-type]  # "user" or "assistant", by the callers below
        parts=[TextPart(text=text)],
        now=at,
    )
    await db.commit()
    return message


class BikeParts:
    """A conversation with two versions of an answer, and a follow-up under the first:

    Name a bike part. ── The saddle. ── Why that one? ── You sit on it.
                      └─ The chain.                          (shown)
    """

    conversation: Conversation
    question: Message
    saddle: Message
    why: Message
    sit: Message
    chain: Message

    @classmethod
    async def of(cls, db: AsyncSession, user_id: uuid.UUID) -> "BikeParts":
        made = cls()
        made.conversation = c = await started(db, user_id)
        made.question = await said(db, c, "user", "Name a bike part.", None, at=later(1))
        made.saddle = await said(db, c, "assistant", "The saddle.", made.question, at=later(2))
        made.why = await said(db, c, "user", "Why that one?", made.saddle, at=later(3))
        made.sit = await said(db, c, "assistant", "You sit on it.", made.why, at=later(4))
        made.chain = await said(db, c, "assistant", "The chain.", made.question, at=later(5))
        return made


def texts(conversation: dict[str, Any]) -> list[str]:
    return [message["parts"][0]["text"] for message in conversation["messages"]]


# --- Who may ask ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/conversations"),
        ("GET", "/api/conversations/{id}"),
        ("PATCH", "/api/conversations/{id}"),
        ("PUT", "/api/conversations/{id}/branch"),
        ("DELETE", "/api/conversations/{id}"),
    ],
)
async def test_conversations_need_a_session(api: httpx.AsyncClient, method: str, path: str) -> None:
    response = await api.request(method, path.format(id=new_id()), json={"title": "x"})

    assert response.status_code == 401
    assert response.headers["content-type"] == PROBLEM


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/api/conversations/{id}", None),
        ("PATCH", "/api/conversations/{id}", {"title": "Mine now"}),
        ("PUT", "/api/conversations/{id}/branch", {"message_id": str(new_id())}),
        ("DELETE", "/api/conversations/{id}", None),
    ],
)
async def test_someone_elses_conversation_looks_like_one_that_does_not_exist(
    start_app: Any,
    settings: Settings,
    db: AsyncSession,
    method: str,
    path: str,
    body: dict[str, str] | None,
) -> None:
    owner, stranger = await start_app(settings), await start_app(settings)
    theirs = await started(db, await guest(owner), title="Private")
    await guest(stranger)

    for_theirs = await stranger.request(method, path.format(id=theirs.id), json=body)
    for_nothing = await stranger.request(method, path.format(id=new_id()), json=body)

    assert for_theirs.status_code == for_nothing.status_code == 404
    assert for_theirs.json() == for_nothing.json()
    assert for_theirs.json()["detail"] == "No such conversation."
    assert (await owner.get(f"/api/conversations/{theirs.id}")).json()["title"] == "Private"


async def test_an_id_that_is_not_an_id_is_an_invalid_request(api: httpx.AsyncClient) -> None:
    await guest(api)

    response = await api.get("/api/conversations/not-an-id")

    assert response.status_code == 422
    assert response.json()["errors"][0]["where"] == "path.conversation_id"


async def test_another_sites_page_cannot_delete_a_visitors_conversation(
    api: httpx.AsyncClient, db: AsyncSession
) -> None:
    conversation = await started(db, await guest(api))

    response = await api.delete(
        f"/api/conversations/{conversation.id}", headers={"Sec-Fetch-Site": "cross-site"}
    )

    assert response.status_code == 403
    assert (await api.get(f"/api/conversations/{conversation.id}")).status_code == 200


# --- The list -------------------------------------------------------------------------


async def test_a_newcomer_has_no_conversations(api: httpx.AsyncClient) -> None:
    await guest(api)

    response = await api.get("/api/conversations")

    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}


async def test_the_list_shows_your_conversations_most_recently_active_first(
    start_app: Any, settings: Settings, db: AsyncSession
) -> None:
    mine, yours = await start_app(settings), await start_app(settings)
    me = await guest(mine)
    older = await started(db, me, at=later(1), title="Flat tyre")
    newer = await started(db, me, at=later(2))
    await started(db, await guest(yours), at=later(3), title="Not mine")

    response = await mine.get("/api/conversations")

    assert response.json() == {
        "items": [
            {
                "id": str(newer.id),
                "title": None,
                "created_at": "2026-11-10T12:02:00Z",
                "last_active_at": "2026-11-10T12:02:00Z",
            },
            {
                "id": str(older.id),
                "title": "Flat tyre",
                "created_at": "2026-11-10T12:01:00Z",
                "last_active_at": "2026-11-10T12:01:00Z",
            },
        ],
        "next_cursor": None,
    }


async def test_the_list_comes_in_pages(api: httpx.AsyncClient, db: AsyncSession) -> None:
    me = await guest(api)
    made = [await started(db, me, at=later(minute)) for minute in range(5)]

    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while pages == 0 or cursor is not None:
        params = {"limit": "2"} | ({"cursor": cursor} if cursor else {})
        page = (await api.get("/api/conversations", params=params)).json()
        seen += [item["id"] for item in page["items"]]
        cursor = page["next_cursor"]
        pages += 1

    assert seen == [str(c.id) for c in reversed(made)]
    assert pages == 3


@pytest.mark.parametrize("limit", ["0", "101", "-1", "many"])
async def test_a_page_has_1_to_100_conversations(api: httpx.AsyncClient, limit: str) -> None:
    await guest(api)

    response = await api.get("/api/conversations", params={"limit": limit})

    assert response.status_code == 422
    assert response.json()["errors"][0]["where"] == "query.limit"


@pytest.mark.parametrize(
    "cursor",
    [
        "made-up",
        "!!!",
        "MjAyNi0xMS0xMFQxMjowMDowMCswMDowMA",  # a moment, and no id
        "MjAyNi0xMS0xMFQxMjowMDowMHwwMTlhNmUyZS0wMDAwLTcwMDAtODAwMC0wMDAwMDAwMDAwMDA",  # no zone
        "/w",  # not text at all
    ],
)
async def test_a_cursor_that_this_api_never_gave_out_is_refused(
    api: httpx.AsyncClient, cursor: str
) -> None:
    await guest(api)

    response = await api.get("/api/conversations", params={"cursor": cursor})

    assert response.status_code == 422
    assert response.headers["content-type"] == PROBLEM
    assert response.json()["detail"] == "This cursor didn't come from this API."


# --- One conversation -----------------------------------------------------------------


async def test_a_conversation_comes_with_the_branch_that_is_shown(
    api: httpx.AsyncClient, db: AsyncSession
) -> None:
    c = await started(db, await guest(api), title="Sky")
    question = await said(db, c, "user", "Why is the sky blue?", None, at=later(1))
    answer = await tree.append(
        db,
        c,
        message_id=new_id(),
        parent_id=question.id,
        role="assistant",
        parts=[ReasoningPart(text="Scattering."), TextPart(text="Air scatters blue light most.")],
        now=later(2),
    )
    await db.commit()

    response = await api.get(f"/api/conversations/{c.id}")

    assert response.status_code == 200
    assert response.json() == {
        "id": str(c.id),
        "title": "Sky",
        "created_at": "2026-11-10T12:00:00Z",
        "last_active_at": "2026-11-10T12:02:00Z",
        "current_leaf_id": str(answer.id),
        "messages": [
            {
                "id": str(question.id),
                "role": "user",
                "parts": [{"type": "text", "text": "Why is the sky blue?"}],
                "metadata": {
                    "parent_id": None,
                    "created_at": "2026-11-10T12:01:00Z",
                    "versions": [str(question.id)],
                    "turn": None,
                },
            },
            {
                "id": str(answer.id),
                "role": "assistant",
                "parts": [
                    {"type": "reasoning", "text": "Scattering."},
                    {"type": "text", "text": "Air scatters blue light most."},
                ],
                "metadata": {
                    "parent_id": str(question.id),
                    "created_at": "2026-11-10T12:02:00Z",
                    "versions": [str(answer.id)],
                    "turn": None,  # written by this test, not by a model
                },
            },
        ],
        "unanswered": None,
    }


async def test_an_empty_conversation_has_no_messages(
    api: httpx.AsyncClient, db: AsyncSession
) -> None:
    conversation = await started(db, await guest(api))

    body = (await api.get(f"/api/conversations/{conversation.id}")).json()

    assert (body["current_leaf_id"], body["messages"]) == (None, [])


async def test_only_one_branch_is_shown_and_each_message_names_its_versions(
    api: httpx.AsyncClient, db: AsyncSession
) -> None:
    bike = await BikeParts.of(db, await guest(api))

    body = (await api.get(f"/api/conversations/{bike.conversation.id}")).json()

    assert texts(body) == ["Name a bike part.", "The chain."]
    assert body["current_leaf_id"] == str(bike.chain.id)
    assert body["messages"][1]["metadata"]["versions"] == [str(bike.saddle.id), str(bike.chain.id)]


# --- Renaming -------------------------------------------------------------------------


async def test_renaming_a_conversation(api: httpx.AsyncClient, db: AsyncSession) -> None:
    me = await guest(api)
    older = await started(db, me, at=later(1))
    newer = await started(db, me, at=later(2))

    response = await api.patch(f"/api/conversations/{older.id}", json={"title": "  Flat tyre "})

    assert response.status_code == 200
    assert response.json() == {
        "id": str(older.id),
        "title": "Flat tyre",
        "created_at": "2026-11-10T12:01:00Z",
        "last_active_at": "2026-11-10T12:01:00Z",
    }
    listed = (await api.get("/api/conversations")).json()["items"]
    assert [(item["id"], item["title"]) for item in listed] == [
        (str(newer.id), None),
        (str(older.id), "Flat tyre"),  # still in its place: renaming isn't activity
    ]


@pytest.mark.parametrize("body", [{"title": ""}, {"title": "   "}, {"title": "x" * 201}, {}])
async def test_a_title_has_1_to_200_characters(
    api: httpx.AsyncClient, db: AsyncSession, body: dict[str, str]
) -> None:
    conversation = await started(db, await guest(api), title="Before")

    response = await api.patch(f"/api/conversations/{conversation.id}", json=body)

    assert response.status_code == 422
    assert response.json()["errors"][0]["where"] == "body.title"
    assert (await api.get(f"/api/conversations/{conversation.id}")).json()["title"] == "Before"


# --- Showing another branch -----------------------------------------------------------


async def test_showing_the_branch_through_another_version(
    api: httpx.AsyncClient, db: AsyncSession
) -> None:
    bike = await BikeParts.of(db, await guest(api))
    path = f"/api/conversations/{bike.conversation.id}"

    response = await api.put(f"{path}/branch", json={"message_id": str(bike.saddle.id)})

    assert response.status_code == 200
    assert texts(response.json()) == [
        "Name a bike part.",
        "The saddle.",
        "Why that one?",
        "You sit on it.",
    ]
    assert response.json()["current_leaf_id"] == str(bike.sit.id)
    assert response.json()["last_active_at"] == "2026-11-10T12:05:00Z"  # looking isn't activity
    assert (await api.get(path)).json() == response.json()  # and it stays shown


@pytest.mark.parametrize("which", ["unknown", "of another conversation"])
async def test_a_message_the_conversation_does_not_have_cannot_be_shown(
    api: httpx.AsyncClient, db: AsyncSession, which: str
) -> None:
    me = await guest(api)
    bike, other = await BikeParts.of(db, me), await BikeParts.of(db, me)
    message_id = new_id() if which == "unknown" else other.saddle.id
    path = f"/api/conversations/{bike.conversation.id}"

    response = await api.put(f"{path}/branch", json={"message_id": str(message_id)})

    assert response.status_code == 404
    assert response.json()["detail"] == "This conversation has no such message."
    assert (await api.get(path)).json()["current_leaf_id"] == str(bike.chain.id)


# --- Deleting -------------------------------------------------------------------------


async def count(db: AsyncSession, model: type) -> int:
    return await db.scalar(select(func.count()).select_from(model)) or 0


async def test_deleting_a_conversation(api: httpx.AsyncClient, db: AsyncSession) -> None:
    me = await guest(api)
    bike, kept = await BikeParts.of(db, me), await BikeParts.of(db, me)
    path = f"/api/conversations/{bike.conversation.id}"

    response = await api.delete(path)

    assert response.status_code == 204
    assert (await api.get(path)).status_code == 404
    assert (await api.delete(path)).status_code == 404
    listed = (await api.get("/api/conversations")).json()["items"]
    assert [item["id"] for item in listed] == [str(kept.conversation.id)]
    assert await count(db, Message) == 5  # every branch of the deleted one is gone


# --- Guests, accounts, and what is theirs ---------------------------------------------


async def test_a_guest_who_signs_up_keeps_their_conversations(
    api: httpx.AsyncClient, db: AsyncSession
) -> None:
    bike = await BikeParts.of(db, await guest(api))

    assert (await api.post("/api/account", json=DENIZ)).status_code == 201

    listed = (await api.get("/api/conversations")).json()["items"]
    assert [item["id"] for item in listed] == [str(bike.conversation.id)]


async def test_a_guest_who_logs_in_brings_their_conversations_along(
    start_app: Any, settings: Settings, db: AsyncSession
) -> None:
    """Someone tries Actio as a guest, then remembers they have an account. Logging in
    deletes the guest; what the guest started must not go with it."""
    at_home, at_work = await start_app(settings), await start_app(settings)
    account = uuid.UUID((await at_home.post("/api/account", json=DENIZ)).json()["id"])
    old = await started(db, account, at=later(1), title="From before")
    as_guest = await BikeParts.of(db, await guest(at_work))

    assert (await at_work.post("/api/session", json=DENIZ)).status_code == 200

    listed = (await at_work.get("/api/conversations")).json()["items"]
    assert [item["id"] for item in listed] == [str(as_guest.conversation.id), str(old.id)]
    shown = (await at_work.get(f"/api/conversations/{as_guest.conversation.id}")).json()
    assert texts(shown) == ["Name a bike part.", "The chain."]
    assert (await at_home.get("/api/conversations")).json()["items"] == listed  # one account


async def test_logging_in_from_an_account_takes_nothing_from_that_account(
    start_app: Any, settings: Settings, db: AsyncSession
) -> None:
    browser = await start_app(settings)
    first = uuid.UUID((await browser.post("/api/account", json=DENIZ)).json()["id"])
    theirs = await started(db, first, title="Stays with the first account")
    other = await start_app(settings)
    selin = DENIZ | {"email": "selin@example.com"}
    await other.post("/api/account", json=selin)

    assert (await browser.post("/api/session", json=selin)).status_code == 200  # switch account

    assert (await browser.get("/api/conversations")).json()["items"] == []
    assert (await browser.get(f"/api/conversations/{theirs.id}")).status_code == 404
    assert await conversations.get(db, theirs.id, user_id=first) is not None


async def test_a_guest_who_logs_out_takes_their_conversations_with_them(
    api: httpx.AsyncClient, db: AsyncSession
) -> None:
    await BikeParts.of(db, await guest(api))

    assert (await api.delete("/api/session")).status_code == 204

    assert (await count(db, Conversation), await count(db, Message)) == (0, 0)


async def test_the_routes_are_in_the_api_description(api: httpx.AsyncClient) -> None:
    paths = (await api.get("/api/openapi.json")).json()["paths"]

    assert set(paths["/api/conversations"]) == {"get"}
    assert set(paths["/api/conversations/{conversation_id}"]) == {"get", "patch", "delete"}
    assert set(paths["/api/conversations/{conversation_id}/branch"]) == {"put"}
