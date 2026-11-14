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
    db: AsyncSession, conversation_id: uuid.UUID, *, user_id: uuid.UUID, to_change: bool = False
) -> Conversation | None:
    """The conversation, if it exists and belongs to `user_id`.

    `to_change` also locks its row until the transaction ends. Whoever asks next waits,
    then sees what this transaction did: two turns can't start in one conversation at
    the same moment and both find it free.
    """
    query = select(Conversation).where(
        Conversation.id == conversation_id, Conversation.user_id == user_id
    )
    if to_change:
        query = query.with_for_update()
    found: Conversation | None = await db.scalar(query)
    return found


async def hold_still(db: AsyncSession, conversation: Conversation) -> bool:
    """Bring `conversation` up to date, and keep it from changing until the transaction
    ends. False if it was deleted meanwhile.

    For reading a conversation whole. That takes several questions to the database: its
    row, its branch, the turns behind it. A turn that ended between two of them would be
    shown as half of each state. Every turn locks the conversation's row before it
    writes (`to_change` above), so holding the row shared here keeps turns out until the
    reader is done, and lets other readers in.
    """
    query = (
        select(Conversation)
        .where(Conversation.id == conversation.id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)  # overwrite what this session holds
    )
    return await db.scalar(query) is not None


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
