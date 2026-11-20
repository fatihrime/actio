"""Turns: starting one, and recording how it ended.

A turn is one attempt to answer a question. Starting it writes everything that must
exist before a model is asked anything, in one transaction:

- the conversation, if this is its first question;
- the question, unless an existing one is being answered again;
- the turn itself, marked `running`.

So a question never exists without the turn that is answering it. Finishing a turn
writes the answer, if the model produced anything, and how the turn ended.

One turn runs at a time in a conversation. While it runs, the process that writes its
answer says every few seconds that it is still at it, and saves what the answer holds so
far, its draft (`still_alive`). A turn whose process died would otherwise block its
conversation for ever, so one that has been silent for too long is given up on, as
`interrupted`, with its draft as its answer: the next time someone looks at its
conversation (`give_up_on_stale`), and by the worker, which goes through the turns of
every conversation, those nobody looks at too (`give_up_on_stale_everywhere`).

An account may also have only so many answers being written at once, in all its
conversations together, when its plan says so: see `being_written`. A turn that would
be one too many is refused before anything of it is kept.

These functions never commit, and take the time as `now`.
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import BigInteger, ColumnElement, and_, func, literal, not_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from actio.chat import conversations, tree
from actio.chat.models import Conversation, Message, Turn
from actio.chat.parts import Part, TextPart
from actio_llm import Timings, Usage

logger = logging.getLogger(__name__)

BEAT = timedelta(seconds=15)
"""How often a running turn says that it is alive, and saves its draft."""

STALE_AFTER = timedelta(seconds=60)
"""A running turn that hasn't said it is alive for this long is taken for dead. Four
beats: a process that is only slow, or a database that was away for a moment, misses
one or two. It is also how long the credits held for a turn stay held without being
renewed, and how long what it streams is kept."""

STALE_BATCH = 500
"""How many stale turns one run of `give_up_on_stale_everywhere` gives up on at most."""

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


class TooManyAnswersError(Exception):
    """The account has as many answers being written as its plan allows at once."""

    def __init__(self, running: int, at_once: int) -> None:
        super().__init__(f"{running} of {at_once}")
        self.running = running
        """How many are being written."""
        self.at_once = at_once
        """How many the plan allows at once."""


class AlreadyAskedError(Exception):
    """This very message was sent before: the request is a repeat, not a new question."""

    def __init__(self, turn: Turn | None) -> None:
        super().__init__()
        self.turn = turn
        """The last turn that answered it, or tried to."""


class IdTakenError(Exception):
    """The id chosen for a new conversation or a new message belongs to something else."""


type Recorded = Literal["recorded", "left", "gone"]
"""What became of a turn's outcome when it was written: see `finish`."""


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

    status: Literal["completed", "stopped", "failed", "interrupted"]
    """`stopped`: whoever asked stopped it. `interrupted`: the process that wrote it was
    shutting down."""
    parts: list[Part] = field(default_factory=list)
    """What the model wrote, whole or not. Empty when it wrote nothing."""
    finish_reason: str | None = None
    error: str | None = None
    usage: Usage | None = None
    timings: Timings | None = None


class Draft(BaseModel):
    """What a running turn's answer holds so far, saved on the turn's row with each beat
    (`still_alive`), as one JSON object. A turn whose process died is given up on with
    it as its answer.

    Read and written through this class only, which checks its shape both ways. The
    parts are in their stored form (`actio.chat.parts`)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    answer_id: uuid.UUID
    """The id the answer's message will have: the one the turn's stream has named to its
    readers."""
    parts: list[Part]
    """What the model had written by the beat, reasoning and text."""


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
    answers_at_once: int | None = None,
) -> Begun:
    """Add a question to a conversation and start the turn that answers it.

    The conversation and the question get the ids their sender chose. A conversation
    that doesn't exist yet is created by its first question, one with no `parent_id`.

    Raises `AlreadyAskedError` when this message was sent before, so that sending a
    request twice never asks twice. Raises `TooManyAnswersError` when the account
    already has `answers_at_once` answers being written, the most its plan allows;
    `None`: the plan has no such limit.
    """
    conversation = await conversations.get(db, conversation_id, user_id=user_id, to_change=True)
    if conversation is None:
        if parent_id is not None:
            raise NoSuchConversationError
        conversation = await _create(db, conversation_id, user_id=user_id, parts=parts, now=now)
    else:
        # Before the message's id is looked up: a stale turn is given up on with its
        # draft as its answer, which is a message too, and may have the very id this
        # one comes under.
        await give_up_on_stale(db, conversation.id, now=now)
    earlier = await db.get(Message, message_id)
    if earlier is not None:
        if earlier.conversation_id == conversation.id and earlier.role == "user":
            raise AlreadyAskedError(await latest_for(db, conversation, earlier.id))
        raise IdTakenError
    await _require_free(db, conversation)
    await _require_room(db, user_id, answers_at_once, now=now)
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
    answers_at_once: int | None = None,
) -> Begun:
    """Start a turn that answers an existing question once more.

    The new answer will stand beside the earlier ones, as another version. Meanwhile
    the conversation shows the branch that ends at the question. `answers_at_once` is
    as for `ask`.
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
    await give_up_on_stale(db, conversation.id, now=now)
    await _require_free(db, conversation)
    await _require_room(db, user_id, answers_at_once, now=now)
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
    credits: int | None = None,
) -> Recorded:
    """Record how a turn ended, and write its answer if the model produced anything.

    `credits` is what the turn cost its account, when it cost something: the same
    number the usage ledger gets, in the same transaction.

    Says what became of it:

    - `recorded`: the turn has its outcome now;
    - `left`: the turn was no longer `running`, because it was given up on. It is left
      as it is: its conversation may have moved on since, and an answer that turns up
      after all is not slipped in behind everyone's back;
    - `gone`: its conversation was deleted meanwhile, and the turn with it.
    """
    # The conversation first, then the turn: the same order in which a new turn takes
    # them, so the two can't wait for each other.
    conversation = await _hold(db, conversation_id)
    if conversation is None:
        return "gone"
    turn = await db.scalar(
        select(Turn)
        .where(Turn.id == turn_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if turn is None:
        return "gone"
    if turn.status != "running":
        return "left"
    await _end(db, conversation, turn, ending, answer_id=answer_id, now=now, credits=credits)
    return "recorded"


async def still_alive(
    db: AsyncSession, turn_id: uuid.UUID, *, now: datetime, draft: Draft | None = None
) -> None:
    """Say that a running turn is still being written, and save what its answer holds
    so far: `draft`, or `None` while nothing is written. Whoever writes the turn calls
    this every `BEAT`, so that nobody gives up on it, and so that a process that dies
    leaves its answer as it was at its last beat. One statement does both."""
    stored = None if draft is None else draft.model_dump(mode="json")
    await db.execute(
        update(Turn)
        .where(Turn.id == turn_id, Turn.status == "running")
        .values(alive_at=now, draft=stored)
    )


async def give_up_on_stale(db: AsyncSession, conversation_id: uuid.UUID, *, now: datetime) -> None:
    """Mark the conversation's turn as `interrupted` if it is stale: running, and it
    hasn't said so for `STALE_AFTER`. The process that was writing it is gone, and its
    draft becomes its answer.

    Every read of a conversation calls this. So it first looks, holding nothing and
    changing nothing, whether there is a stale turn at all. Only then does it take the
    conversation's row and the turn's, and wait for them if it has to: whoever holds
    them is about to be done."""
    stale = await db.scalar(
        select(Turn.id).where(Turn.conversation_id == conversation_id, _stale(now))
    )
    if stale is None:
        return
    # The conversation first, then the turn, as `ask` and `finish` take them. Then the
    # question again: the turn may have said that it is alive, or ended, meanwhile.
    conversation = await _hold(db, conversation_id)
    if conversation is None:
        return
    turn = await db.scalar(
        select(Turn)
        .where(Turn.id == stale, _stale(now))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if turn is not None:
        await _give_up(db, conversation, turn, now=now)


async def give_up_on_stale_everywhere(
    db: AsyncSession, *, now: datetime, limit: int = STALE_BATCH
) -> list[uuid.UUID]:
    """Mark stale turns as `interrupted`, in every conversation, as `give_up_on_stale`
    does for one: those in conversations nobody looks at too. Returns their ids.

    It works in batches of `limit`, the longest silent first, and never waits for a
    row's lock. It skips a turn another transaction holds, and a turn whose conversation
    another transaction holds: someone reads it, asks in it, or the turn is being
    recorded at that moment by a process that was only slow, and keeps what it is
    given. A turn skipped is left for the next run. Any number of runs can go at once.

    One wait is left, for no row it takes: an answer it keeps is written under the id
    the draft names, and if another transaction is adding a message under that id at
    that moment, the insert waits for it to end, and then finds the id taken.
    """
    # The batch is taken and held by a statement of its own. As the subquery of an
    # update, as it once was, it grew past `limit`: the plan read it once for each turn
    # it looked at, and each time skipped the turns the update had changed already.
    held = await db.scalars(
        select(Turn)
        .where(_stale(now))
        .order_by(Turn.alive_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    batch = list(held.all())
    if not batch:
        return []
    # Then their conversations, which giving up on a turn changes. Everyone else takes a
    # conversation first, then its turn. The worker holds the turns already: if it waited
    # for a conversation, it could be waiting for someone who waits for one of its turns.
    # It takes neither by waiting, so it is never part of such a circle.
    free = await db.scalars(
        select(Conversation)
        .where(Conversation.id.in_([turn.conversation_id for turn in batch]))
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    taken = {conversation.id: conversation for conversation in free.all()}
    given_up: list[uuid.UUID] = []
    for turn in batch:
        conversation = taken.get(turn.conversation_id)
        if conversation is not None:
            await _give_up(db, conversation, turn, now=now)
            given_up.append(turn.id)
    return given_up


async def being_written(db: AsyncSession, user_id: uuid.UUID, *, now: datetime) -> int:
    """How many answers the account has being written, in all its conversations.

    A turn counts while it is `running` and was heard from within `STALE_AFTER`: every
    running turn that the rule for giving up on turns (`_stale`) leaves out. So a turn
    whose process died stops counting a minute after it was last heard from, whether or
    not anyone has given up on it. A turn that waits for its model's server is running,
    and counts."""
    found = await db.scalar(
        select(func.count())
        .select_from(Turn)
        .join(Conversation, Turn.conversation_id == Conversation.id)
        .where(Conversation.user_id == user_id, Turn.status == "running", not_(_stale(now)))
    )
    return found or 0


async def get(db: AsyncSession, conversation: Conversation, turn_id: uuid.UUID) -> Turn | None:
    """The conversation's turn with this id, as the database has it now."""
    found: Turn | None = await db.scalar(
        select(Turn)
        .where(Turn.conversation_id == conversation.id, Turn.id == turn_id)
        .execution_options(populate_existing=True)
    )
    return found


async def running_in(db: AsyncSession, conversation: Conversation) -> uuid.UUID | None:
    """The turn that is being written in this conversation, if one is."""
    found: uuid.UUID | None = await db.scalar(
        select(Turn.id).where(Turn.conversation_id == conversation.id, Turn.status == "running")
    )
    return found


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


def _stale(now: datetime) -> ColumnElement[bool]:
    """The rule by which a turn is taken for dead at `now`: it is `running`, and hasn't
    said that it is alive for `STALE_AFTER`. Written here only."""
    return and_(Turn.status == "running", Turn.alive_at <= now - STALE_AFTER)


async def _hold(db: AsyncSession, conversation_id: uuid.UUID) -> Conversation | None:
    """Lock the conversation's row until the transaction ends, to write in it, and read
    it as it is then. `None` if it is gone."""
    found: Conversation | None = await db.scalar(
        select(Conversation)
        .where(Conversation.id == conversation_id)
        .with_for_update()
        .execution_options(populate_existing=True)  # what this session holds may be old
    )
    return found


async def _give_up(
    db: AsyncSession, conversation: Conversation, turn: Turn, *, now: datetime
) -> None:
    """Give up on a stale turn, whose row and whose conversation's row the caller holds:
    it becomes `interrupted`, ended at `now`, with its draft as its answer, if it has
    one. Whoever gives up on turns does it through this.

    The answer is dated at the turn's last beat, when the draft was saved, and so is the
    conversation's last activity: not at the moment someone, or the worker, noticed. It
    costs nothing, as any interrupted turn.

    A draft that can't be kept never keeps its turn from being given up on. The id its
    answer was to have may be taken: readers are told that id while the answer is
    written, and whoever sends a message chooses the message's id. Then the turn is
    given up on without an answer, and the log says so."""
    draft = _draft_of(turn)
    if draft is not None:
        turn_id = turn.id
        try:
            async with db.begin_nested():  # an answer that can't be written undoes only itself
                await _end(
                    db,
                    conversation,
                    turn,
                    Ending("interrupted", draft.parts),
                    answer_id=draft.answer_id,
                    now=now,
                    answered_at=turn.alive_at,
                )
            return
        except IntegrityError as error:
            if getattr(error.orig, "sqlstate", None) != _UNIQUE_VIOLATION:
                raise
            logger.warning(
                "turn %s: its draft couldn't be kept, a message already has its answer's id",
                turn_id,
            )
            # As they are in the database, after what was undone: both are still held.
            await db.refresh(conversation)
            await db.refresh(turn)
    await _end(db, conversation, turn, Ending("interrupted"), answer_id=None, now=now)


def _draft_of(turn: Turn) -> Draft | None:
    """The turn's draft. `None` when it has none, or one this code can't read, written
    by another version of it: then the turn is given up on without it, and the log says
    so. Anything else that goes wrong is raised."""
    if turn.draft is None:
        return None
    try:
        return Draft.model_validate(turn.draft)
    except ValidationError:
        # Not the error itself: it quotes what the model wrote, and people's words stay
        # out of the log.
        logger.warning("turn %s: its draft couldn't be read, and nothing of it is kept", turn.id)
        return None


async def _end(
    db: AsyncSession,
    conversation: Conversation,
    turn: Turn,
    ending: Ending,
    *,
    answer_id: uuid.UUID | None,
    now: datetime,
    answered_at: datetime | None = None,
    credits: int | None = None,
) -> None:
    """Write how a running turn ended, whose row and whose conversation's row the
    caller holds, and its answer if it has one. Every ending is written through this:
    `finish`, and giving up.

    The answer is a message under `answer_id`, shown by the conversation, and dated
    `answered_at`, or `now` when that isn't given. The turn ends at `now`. Its draft is
    emptied in the statement that writes the outcome."""
    if ending.parts:
        assert answer_id is not None  # noqa: S101 - whoever has parts has their message's id
        await tree.append(
            db,
            conversation,
            message_id=answer_id,
            parent_id=turn.question_id,
            role="assistant",
            parts=ending.parts,
            now=now if answered_at is None else answered_at,
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
    turn.credits = credits
    turn.draft = None
    turn.finished_at = now
    await db.flush()


async def _require_free(db: AsyncSession, conversation: Conversation) -> None:
    """Raise `StillAnsweringError` if a turn is running in the conversation. Whoever
    asks has given up on a stale one before this (`give_up_on_stale`)."""
    running = await running_in(db, conversation)
    if running is not None:
        raise StillAnsweringError(running)


async def _require_room(
    db: AsyncSession, user_id: uuid.UUID, at_once: int | None, *, now: datetime
) -> None:
    """Refuse a turn that would be one more answer than the account may have being
    written at once.

    Two questions of one account that arrive together would each count the same turns,
    and both pass: a turn that is being started is in no count until its transaction is
    committed. So the account's admissions take a lock first, and hold it until the
    transaction ends: they are counted one after the other. Other accounts take other
    locks, and never wait for this one.
    """
    if at_once is None:
        return
    key = literal(_admissions_of(user_id), BigInteger)
    await db.execute(select(func.pg_advisory_xact_lock(key)))
    running = await being_written(db, user_id, now=now)
    if running >= at_once:
        raise TooManyAnswersError(running, at_once)


def _admissions_of(user_id: uuid.UUID) -> int:
    """The key of the advisory lock that an account's admissions take: the last 64 bits
    of its id, as Postgres's signed `bigint`. In the ids Actio makes they are random,
    and the bits that mark the id's variant make them negative: never the key of the
    migrations' lock (`actio.migrations.env`), which is positive."""
    return int.from_bytes(user_id.bytes[8:], signed=True)


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
        alive_at=now,
    )
    db.add(turn)
    await db.flush()
    return Begun(conversation, turn, history, list(earlier_answers.all()))
