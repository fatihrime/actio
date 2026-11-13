"""Turns: starting one, and recording how it ended.

A turn is one attempt to answer a question. Starting it writes everything that must
exist before a model is asked anything, in one transaction:

- the conversation, if this is its first question;
- the question, unless an existing one is being answered again;
- the turn itself, marked `running`.

So a question never exists without the turn that is answering it. Finishing a turn
writes the answer, if the model produced anything, and how the turn ended.

One turn runs at a time in a conversation. A turn whose process died would otherwise
block its conversation for ever, so one that has been `running` for longer than any
answer takes is given up on, as `interrupted`, the next time someone looks.

These functions never commit, and take the time as `now`.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from actio.chat import conversations, tree
from actio.chat.models import Conversation, Message, Turn
from actio.chat.parts import Part, TextPart
from actio_llm import Timings, Usage

STALE_AFTER = timedelta(minutes=10)
"""A turn still `running` after this long is taken for dead. Longer than any answer
takes: a model gets 90 seconds to start, and the longest answer allowed is a few
thousand tokens."""

TITLE_LENGTH = 60
"""A new conversation is named after its first question, cut to about this many
characters."""

_UNIQUE_VIOLATION = "23505"  # Postgres's code for "this value is already taken"


class NoSuchConversationError(Exception):
    """The account has no conversation with this id."""


class StillAnsweringError(Exception):
    """The conversation already has a turn that is running."""

    def __init__(self, turn_id: uuid.UUID) -> None:
        super().__init__(str(turn_id))
        self.turn_id = turn_id


class AlreadyAskedError(Exception):
    """This very message was sent before: the request is a repeat, not a new question."""

    def __init__(self, turn: Turn | None) -> None:
        super().__init__()
        self.turn = turn
        """The last turn that answered it, or tried to."""


class IdTakenError(Exception):
    """The id chosen for a new conversation or a new message belongs to something else."""


@dataclass(frozen=True, slots=True)
class Begun:
    """A turn that has started, and what a model needs to answer it."""

    conversation: Conversation
    turn: Turn
    history: list[Message]
    """The branch that ends at the question, first message first: what the model is
    shown. Nothing from the branches beside it."""
    earlier_answers: list[uuid.UUID]
    """The answers the question already has, oldest first."""


@dataclass(frozen=True, slots=True)
class Ending:
    """How a turn ended, and what it produced."""

    status: Literal["completed", "stopped", "failed"]
    parts: list[Part] = field(default_factory=list)
    """What the model wrote, whole or not. Empty when it wrote nothing."""
    finish_reason: str | None = None
    error: str | None = None
    usage: Usage | None = None
    timings: Timings | None = None


async def ask(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    parent_id: uuid.UUID | None,
    parts: list[Part],
    model: str,
    thinking: bool,
    turn_id: uuid.UUID,
    now: datetime,
) -> Begun:
    """Add a question to a conversation and start the turn that answers it.

    The conversation and the question get the ids their sender chose. A conversation
    that doesn't exist yet is created by its first question, one with no `parent_id`.

    Raises `AlreadyAskedError` when this message was sent before, so that sending a
    request twice never asks twice.
    """
    conversation = await conversations.get(db, conversation_id, user_id=user_id, to_change=True)
    if conversation is None:
        if parent_id is not None:
            raise NoSuchConversationError
        conversation = await _create(db, conversation_id, user_id=user_id, parts=parts, now=now)
    earlier = await db.get(Message, message_id)
    if earlier is not None:
        if earlier.conversation_id == conversation.id and earlier.role == "user":
            raise AlreadyAskedError(await latest_for(db, conversation, earlier.id))
        raise IdTakenError
    await _require_free(db, conversation, now=now)
    question = await tree.append(
        db,
        conversation,
        message_id=message_id,
        parent_id=parent_id,
        role="user",
        parts=parts,
        now=now,
    )
    return await _begin(db, conversation, question, model, thinking, turn_id, now)


async def answer_again(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    question_id: uuid.UUID,
    model: str,
    thinking: bool,
    turn_id: uuid.UUID,
    now: datetime,
) -> Begun:
    """Start a turn that answers an existing question once more.

    The new answer will stand beside the earlier ones, as another version. Meanwhile
    the conversation shows the branch that ends at the question.
    """
    conversation = await conversations.get(db, conversation_id, user_id=user_id, to_change=True)
    if conversation is None:
        raise NoSuchConversationError
    question = await db.scalar(
        select(Message).where(Message.conversation_id == conversation.id, Message.id == question_id)
    )
    if question is None:
        raise tree.NoSuchMessageError
    if question.role != "user":
        raise tree.OutOfTurnError
    await _require_free(db, conversation, now=now)
    conversation.current_leaf_id = question.id
    conversation.last_active_at = now
    return await _begin(db, conversation, question, model, thinking, turn_id, now)


async def finish(
    db: AsyncSession,
    conversation_id: uuid.UUID,
    turn_id: uuid.UUID,
    ending: Ending,
    *,
    answer_id: uuid.UUID,
    now: datetime,
) -> Turn | None:
    """Record how a turn ended, and write its answer if the model produced anything.

    Returns the turn, or `None` if its conversation was deleted meanwhile. A turn that
    is no longer `running`, because it was given up on, is left as it is.
    """
    # The conversation first, then the turn: the same order in which a new turn takes
    # them, so the two can't wait for each other.
    conversation = await db.scalar(
        select(Conversation).where(Conversation.id == conversation_id).with_for_update()
    )
    if conversation is None:
        return None
    turn = await db.scalar(select(Turn).where(Turn.id == turn_id).with_for_update())
    if turn is None or turn.status != "running":
        return turn
    if ending.parts:
        await tree.append(
            db,
            conversation,
            message_id=answer_id,
            parent_id=turn.question_id,
            role="assistant",
            parts=ending.parts,
            now=now,
        )
        turn.answer_id = answer_id
    turn.status = ending.status
    turn.finish_reason = ending.finish_reason
    turn.error = ending.error
    if ending.usage is not None:
        turn.input_tokens = ending.usage.input_tokens
        turn.cached_input_tokens = ending.usage.cached_input_tokens
        turn.output_tokens = ending.usage.output_tokens
    if ending.timings is not None:
        turn.load_ms = ending.timings.load_ms
        turn.prompt_ms = ending.timings.prompt_ms
        turn.generation_ms = ending.timings.generation_ms
    turn.finished_at = now
    await db.flush()
    return turn


async def give_up_on_stale(db: AsyncSession, conversation_id: uuid.UUID, *, now: datetime) -> None:
    """Mark the conversation's turn as `interrupted` if it has been running for longer
    than any answer takes: the process that was writing it is gone."""
    await db.execute(
        update(Turn)
        .where(
            Turn.conversation_id == conversation_id,
            Turn.status == "running",
            Turn.created_at <= now - STALE_AFTER,
        )
        .values(status="interrupted", finished_at=now)
    )


async def that_wrote(
    db: AsyncSession, conversation: Conversation, answers: list[uuid.UUID]
) -> dict[uuid.UUID, Turn]:
    """For each of these answers, the turn that wrote it."""
    found = await db.scalars(
        select(Turn).where(Turn.conversation_id == conversation.id, Turn.answer_id.in_(answers))
    )
    return {turn.answer_id: turn for turn in found if turn.answer_id is not None}


async def latest_for(
    db: AsyncSession, conversation: Conversation, question_id: uuid.UUID
) -> Turn | None:
    """The most recent turn that answered this question, or tried to."""
    found: Turn | None = await db.scalar(
        select(Turn)
        .where(Turn.conversation_id == conversation.id, Turn.question_id == question_id)
        .order_by(Turn.created_at.desc(), Turn.id.desc())
        .limit(1)
    )
    return found


def title_from(parts: list[Part]) -> str | None:
    """A title for a conversation, from its first question: its first line, cut at a
    word when it is long. `None` if the question has no text to take one from."""
    text = "".join(part.text for part in parts if isinstance(part, TextPart)).strip()
    first_line = " ".join(text.split("\n", 1)[0].split())
    if not first_line:
        return None
    if len(first_line) <= TITLE_LENGTH:
        return first_line
    cut = first_line[:TITLE_LENGTH].rsplit(" ", 1)[0] or first_line[:TITLE_LENGTH]
    return cut.rstrip(" ,;:.") + "…"


async def _create(
    db: AsyncSession,
    conversation_id: uuid.UUID,
    *,
    user_id: uuid.UUID,
    parts: list[Part],
    now: datetime,
) -> Conversation:
    try:
        async with db.begin_nested():  # a taken id undoes only this attempt
            conversation = await conversations.create(db, conversation_id, user_id=user_id, now=now)
    except IntegrityError as error:
        if getattr(error.orig, "sqlstate", None) == _UNIQUE_VIOLATION:
            raise IdTakenError from error
        raise
    conversation.title = title_from(parts)
    return conversation


async def _require_free(db: AsyncSession, conversation: Conversation, *, now: datetime) -> None:
    await give_up_on_stale(db, conversation.id, now=now)
    running = await db.scalar(
        select(Turn.id).where(Turn.conversation_id == conversation.id, Turn.status == "running")
    )
    if running is not None:
        raise StillAnsweringError(running)


async def _begin(
    db: AsyncSession,
    conversation: Conversation,
    question: Message,
    model: str,
    thinking: bool,
    turn_id: uuid.UUID,
    now: datetime,
) -> Begun:
    history = await tree.branch_to(db, conversation, question.id)
    earlier_answers = await db.scalars(
        select(Message.id)
        .where(Message.conversation_id == conversation.id, Message.parent_id == question.id)
        .order_by(Message.created_at, Message.id)
    )
    turn = Turn(
        id=turn_id,
        conversation_id=conversation.id,
        question_id=question.id,
        model=model,
        thinking=thinking,
        status="running",
        created_at=now,
    )
    db.add(turn)
    await db.flush()
    return Begun(conversation, turn, history, list(earlier_answers.all()))
