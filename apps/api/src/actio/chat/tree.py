"""The tree of a conversation's messages: adding to it, and reading one branch.

A message follows its parent. Messages with the same parent are versions of each other:
a question asked again in other words, or another answer to the same question. So a
conversation is a tree, and what is shown is one branch of it, from a first message
down to the conversation's `current_leaf_id`.

The rules, which `append` keeps:

- a person's message is first, or follows a model's answer;
- a model's answer follows a person's message;
- a message follows a message of the same conversation. The database enforces this one
  too, whatever code writes the row.

These functions never commit, and take the time as `now`.
"""

import uuid
from datetime import datetime

from sqlalchemy import ColumnElement, Uuid, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from actio.chat.models import Conversation, Message, Role
from actio.chat.parts import Part, to_stored


class NoSuchMessageError(Exception):
    """The conversation has no message with this id."""


class OutOfTurnError(Exception):
    """The message can't follow that one: questions and answers take turns."""


async def append(
    db: AsyncSession,
    conversation: Conversation,
    *,
    message_id: uuid.UUID,
    parent_id: uuid.UUID | None,
    role: Role,
    parts: list[Part],
    now: datetime,
) -> Message:
    """Add a message after `parent_id`, or as a first message, and show its branch.

    Adding after a message that already has a follower starts a new branch there. The
    conversation now shows the branch that ends at the new message.
    """
    follows: str | None = None
    if parent_id is not None:
        follows = await db.scalar(
            select(Message.role).where(
                Message.conversation_id == conversation.id, Message.id == parent_id
            )
        )
        if follows is None:
            raise NoSuchMessageError
    if (role == "user" and follows == "user") or (role == "assistant" and follows != "user"):
        raise OutOfTurnError
    message = Message(
        id=message_id,
        conversation_id=conversation.id,
        parent_id=parent_id,
        role=role,
        parts=to_stored(parts),
        created_at=now,
    )
    db.add(message)
    await db.flush()  # the message first: the conversation's row points at it
    conversation.current_leaf_id = message.id
    conversation.last_active_at = now
    await db.flush()
    return message


async def shown_branch(db: AsyncSession, conversation: Conversation) -> list[Message]:
    """The messages of the branch that is shown, from the first one to the last."""
    if conversation.current_leaf_id is None:
        return []
    # Walk up from the last message, one parent at a time, inside the database.
    up = (
        select(Message.id, Message.parent_id, literal(0).label("height"))
        .where(
            Message.conversation_id == conversation.id,
            Message.id == conversation.current_leaf_id,
        )
        .cte("up", recursive=True)
    )
    up = up.union_all(
        select(Message.id, Message.parent_id, up.c.height + 1).where(
            Message.conversation_id == conversation.id, Message.id == up.c.parent_id
        )
    )
    found = await db.scalars(
        select(Message).join(up, Message.id == up.c.id).order_by(up.c.height.desc())
    )
    return list(found.all())


async def versions(
    db: AsyncSession, conversation: Conversation, messages: list[Message]
) -> dict[uuid.UUID, list[uuid.UUID]]:
    """For each of `messages`, the ids of every version of it, oldest first.

    The versions of a message are the messages with the same parent, itself included.
    A message that was never written again has one version.
    """
    parents = {message.parent_id for message in messages}
    same_parent: ColumnElement[bool] = Message.parent_id.in_(
        [parent for parent in parents if parent is not None]
    )
    if None in parents:  # first messages are each other's versions
        same_parent = or_(same_parent, Message.parent_id.is_(None))
    found = await db.execute(
        select(Message.id, Message.parent_id)
        .where(Message.conversation_id == conversation.id, same_parent)
        .order_by(Message.created_at, Message.id)
    )
    by_parent: dict[uuid.UUID | None, list[uuid.UUID]] = {}
    for message_id, parent_id in found:
        by_parent.setdefault(parent_id, []).append(message_id)
    return {message.id: by_parent[message.parent_id] for message in messages}


async def show_branch_through(
    db: AsyncSession, conversation: Conversation, message_id: uuid.UUID
) -> None:
    """Show the branch that passes through `message_id`.

    Below that message the tree may fork again. At each fork, the newest version is
    followed, down to a message that nothing follows.
    """
    exists = await db.scalar(
        select(Message.id).where(
            Message.conversation_id == conversation.id, Message.id == message_id
        )
    )
    if exists is None:
        raise NoSuchMessageError
    # Walk down from the message, to its newest follower each time, inside the database.
    down = select(literal(message_id, type_=Uuid).label("id"), literal(0).label("depth")).cte(
        "down", recursive=True
    )
    newest_follower = (
        select(Message.id)
        .where(Message.conversation_id == conversation.id, Message.parent_id == down.c.id)
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(1)
        .correlate(down)
        .scalar_subquery()
    )
    down = down.union_all(select(newest_follower, down.c.depth + 1).where(down.c.id.is_not(None)))
    leaf = await db.scalar(
        select(down.c.id).where(down.c.id.is_not(None)).order_by(down.c.depth.desc()).limit(1)
    )
    conversation.current_leaf_id = leaf
    await db.flush()
