"""Conversations: creating, finding, listing, renaming, deleting.

Every lookup takes the id of the account that is asking, and finds only what belongs to
it. Someone else's conversation and one that doesn't exist look the same: not found.

Like the rest of the domain modules, these functions never commit, and take the time as
`now`.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from actio.chat.models import Conversation

TITLE_MAX = 200
"""The longest title a conversation can have, in characters."""


@dataclass(frozen=True, slots=True)
class Cursor:
    """Where a page of the list ended. The next page starts after this conversation."""

    last_active_at: datetime
    id: uuid.UUID


async def create(
    db: AsyncSession, conversation_id: uuid.UUID, *, user_id: uuid.UUID, now: datetime
) -> Conversation:
    """Start an empty conversation for `user_id`, under the id the caller chose."""
    conversation = Conversation(
        id=conversation_id, user_id=user_id, created_at=now, last_active_at=now
    )
    db.add(conversation)
    await db.flush()
    return conversation


async def get(
    db: AsyncSession, conversation_id: uuid.UUID, *, user_id: uuid.UUID
) -> Conversation | None:
    """The conversation, if it exists and belongs to `user_id`."""
    found: Conversation | None = await db.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id, Conversation.user_id == user_id
        )
    )
    return found


async def list_page(
    db: AsyncSession, *, user_id: uuid.UUID, limit: int, after: Cursor | None = None
) -> tuple[list[Conversation], Cursor | None]:
    """One page of an account's conversations, most recently active first.

    Returns the page, and the cursor to pass as `after` for the next one: `None` when
    this was the last. A page is found by where the previous one ended, not by counting
    rows to skip, so a conversation that moves to the top meanwhile is neither shown
    twice nor skipped.
    """
    query = select(Conversation).where(Conversation.user_id == user_id)
    if after is not None:
        query = query.where(
            tuple_(Conversation.last_active_at, Conversation.id)
            < tuple_(after.last_active_at, after.id)
        )
    newest_first = (Conversation.last_active_at.desc(), Conversation.id.desc())
    found = (await db.scalars(query.order_by(*newest_first).limit(limit + 1))).all()
    page = list(found[:limit])
    more = len(found) > limit
    return page, Cursor(page[-1].last_active_at, page[-1].id) if more else None


def rename(conversation: Conversation, title: str) -> None:
    """Give the conversation a title. Its place in the list doesn't change."""
    title = title.strip()
    if not title or len(title) > TITLE_MAX:
        raise ValueError(f"a title has 1 to {TITLE_MAX} characters")
    conversation.title = title


async def delete(db: AsyncSession, conversation: Conversation) -> None:
    """Delete the conversation and every message in it. There is no way back."""
    await db.delete(conversation)
    await db.flush()


async def move_all(db: AsyncSession, *, from_user: uuid.UUID, to_user: uuid.UUID) -> None:
    """Hand every conversation of one account to another: a guest's, to the account
    the guest turned out to have."""
    await db.execute(
        update(Conversation).where(Conversation.user_id == from_user).values(user_id=to_user)
    )
