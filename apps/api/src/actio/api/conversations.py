"""Routes for reading and organizing conversations.

- `GET /api/conversations`: the caller's conversations, most recently active first;
- `GET /api/conversations/{id}`: one conversation, with the messages of the branch
  that is shown;
- `PATCH /api/conversations/{id}`: rename it;
- `PUT /api/conversations/{id}/branch`: show another branch;
- `DELETE /api/conversations/{id}`: delete it, with its messages.

Every route needs a session, a guest's included, and sees only the caller's own
conversations: someone else's answers 404, like one that doesn't exist.

Messages are added by turns, which have their own routes.
"""

import base64
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, StringConstraints

from actio.api.dependencies import CurrentPrincipal, Database
from actio.api.problems import Problem
from actio.chat import conversations, tree
from actio.chat.conversations import TITLE_MAX, Cursor
from actio.chat.models import Conversation, Message, Role
from actio.chat.parts import Part, from_stored

router = APIRouter(prefix="/api/conversations", tags=["conversations"])

PAGE_DEFAULT, PAGE_MAX = 30, 100


class ConversationSummary(BaseModel):
    id: uuid.UUID
    title: str | None
    """Empty until the conversation is named."""
    created_at: datetime
    last_active_at: datetime
    """When a message was last added. The list is ordered by it."""

    @classmethod
    def of(cls, conversation: Conversation) -> "ConversationSummary":
        return cls(
            id=conversation.id,
            title=conversation.title,
            created_at=conversation.created_at,
            last_active_at=conversation.last_active_at,
        )


class ConversationList(BaseModel):
    items: list[ConversationSummary]
    next_cursor: str | None
    """Pass it back as `cursor` for the next page. Empty on the last page."""


class MessageMetadata(BaseModel):
    parent_id: uuid.UUID | None
    """The message this one follows. Empty for a first message."""
    created_at: datetime
    versions: list[uuid.UUID]
    """Every version of this message, oldest first, itself included: the messages
    that follow the same one. Show another with `PUT …/branch`."""


class MessageOut(BaseModel):
    """A message, in the shape the web app's chat library reads: `id`, `role`, `parts`,
    and everything of Actio's own under `metadata`."""

    id: uuid.UUID
    role: Role
    parts: list[Part]
    metadata: MessageMetadata


class ConversationOut(ConversationSummary):
    current_leaf_id: uuid.UUID | None
    """The last message of the branch that is shown."""
    messages: list[MessageOut]
    """That branch, from its first message to its last."""


class Rename(BaseModel):
    title: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=TITLE_MAX)
    ]


class ShowBranch(BaseModel):
    message_id: uuid.UUID = Field(description="Any message of the branch to show.")


async def get_own_conversation(
    conversation_id: uuid.UUID, principal: CurrentPrincipal, db: Database
) -> Conversation:
    """The conversation in the path, if it is the caller's. Otherwise 404."""
    conversation = await conversations.get(db, conversation_id, user_id=principal.user_id)
    if conversation is None:
        raise Problem(404, "No such conversation.")
    return conversation


OwnConversation = Annotated[Conversation, Depends(get_own_conversation)]


@router.get("")
async def list_conversations(
    principal: CurrentPrincipal,
    db: Database,
    limit: Annotated[int, Query(ge=1, le=PAGE_MAX)] = PAGE_DEFAULT,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
) -> ConversationList:
    """Your conversations, most recently active first, a page at a time."""
    page, more = await conversations.list_page(
        db,
        user_id=principal.user_id,
        limit=limit,
        after=_read_cursor(cursor) if cursor is not None else None,
    )
    return ConversationList(
        items=[ConversationSummary.of(conversation) for conversation in page],
        next_cursor=_write_cursor(more) if more is not None else None,
    )


@router.get("/{conversation_id}")
async def get_conversation(conversation: OwnConversation, db: Database) -> ConversationOut:
    """One conversation, with the messages of the branch that is shown."""
    return await _with_messages(db, conversation)


@router.patch("/{conversation_id}")
async def rename_conversation(
    body: Rename, conversation: OwnConversation, db: Database
) -> ConversationSummary:
    """Give the conversation a title. Its place in the list doesn't change."""
    conversations.rename(conversation, body.title)
    await db.commit()
    return ConversationSummary.of(conversation)


@router.put("/{conversation_id}/branch")
async def show_branch(
    body: ShowBranch, conversation: OwnConversation, db: Database
) -> ConversationOut:
    """Show the branch that passes through a message: another version of a question,
    or of an answer, and what followed it. Where that branch forks again further down,
    the newest version is shown."""
    try:
        await tree.show_branch_through(db, conversation, body.message_id)
    except tree.NoSuchMessageError:
        raise Problem(404, "This conversation has no such message.") from None
    await db.commit()
    return await _with_messages(db, conversation)


@router.delete("/{conversation_id}", status_code=204)
async def delete_conversation(conversation: OwnConversation, db: Database) -> None:
    """Delete the conversation and every message in it, on every branch. There is no
    way back."""
    await conversations.delete(db, conversation)
    await db.commit()


async def _with_messages(db: Database, conversation: Conversation) -> ConversationOut:
    branch = await tree.shown_branch(db, conversation)
    versions = await tree.versions(db, conversation, branch)
    return ConversationOut(
        id=conversation.id,
        title=conversation.title,
        created_at=conversation.created_at,
        last_active_at=conversation.last_active_at,
        current_leaf_id=conversation.current_leaf_id,
        messages=[_message(message, versions[message.id]) for message in branch],
    )


def _message(message: Message, versions: list[uuid.UUID]) -> MessageOut:
    return MessageOut(
        id=message.id,
        role=message.role,
        parts=from_stored(message.parts),
        metadata=MessageMetadata(
            parent_id=message.parent_id, created_at=message.created_at, versions=versions
        ),
    )


# --- The list's cursor ----------------------------------------------------------------
# Opaque to clients: the place where a page ended, as text that survives a URL.


def _write_cursor(cursor: Cursor) -> str:
    plain = f"{cursor.last_active_at.isoformat()}|{cursor.id}"
    return base64.urlsafe_b64encode(plain.encode()).decode().rstrip("=")


def _read_cursor(text: str) -> Cursor:
    try:
        plain = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)).decode()
        moment, _, conversation_id = plain.partition("|")
        cursor = Cursor(datetime.fromisoformat(moment), uuid.UUID(conversation_id))
        if cursor.last_active_at.tzinfo is None:
            raise ValueError("a moment without its time zone")
    except ValueError:  # also what bad base64 and bad UTF-8 raise
        raise Problem(422, "This cursor didn't come from this API.") from None
    return cursor
