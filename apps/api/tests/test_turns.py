"""Turns below the API, against a real Postgres: what the HTTP tests can't reach.

Starting and finishing turns in the ordinary way is covered through the API, in
`test_turns_api.py`. Here: a turn that finishes after it was given up on, one whose
conversation is gone, the turns that nobody looks at and the worker gives up on, and
what the database itself refuses.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from actio.chat import conversations, tree, turns
from actio.chat.models import Conversation, Message, Turn
from actio.chat.parts import TextPart
from actio.chat.turns import STALE_AFTER, Begun, Ending
from actio.core.database import create_engine, create_session_factory
from actio.core.ids import new_id
from actio.identity import sessions
from actio_llm import Timings, Usage

pytestmark = pytest.mark.anyio

NOON = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


async def asked(
    db: AsyncSession, question: str = "Name a bike part.", *, by: uuid.UUID | None = None
) -> Begun:
    """The first question of a new conversation, with its turn running: a new account's,
    or the account `by` names."""
    if by is None:
        _, principal = await sessions.start_guest_session(db, now=NOON)
        by = principal.user_id
    begun = await turns.ask(
        db,
        user_id=by,
        conversation_id=new_id(),
        message_id=new_id(),
        parent_id=None,
        parts=[TextPart(text=question)],
        model="standard",
        thinking=False,
        turn_id=new_id(),
        now=NOON,
    )
    await db.commit()
    return begun


async def count(db: AsyncSession, table: type) -> int:
    return await db.scalar(select(func.count()).select_from(table)) or 0


async def test_starting_a_turn_writes_the_question_and_the_turn_together(
    db: AsyncSession,
) -> None:
    begun = await asked(db)

    assert begun.conversation.title == "Name a bike part."
    assert [m.role for m in begun.history] == ["user"]
    assert begun.earlier_answers == []
    turn = begun.turn
    assert (turn.status, turn.model, turn.thinking) == ("running", "standard", False)
    assert (turn.question_id, turn.answer_id) == (begun.history[0].id, None)
    assert (turn.created_at, turn.finished_at) == (NOON, None)
    assert begun.conversation.current_leaf_id == turn.question_id


async def test_finishing_a_turn_writes_the_answer_and_what_it_cost(db: AsyncSession) -> None:
    begun = await asked(db)
    answer_id, later = new_id(), NOON + timedelta(seconds=4)
    ending = Ending(
        "completed",
        [TextPart(text="The saddle.")],
        finish_reason="stop",
        usage=Usage(input_tokens=11, output_tokens=3, cached_input_tokens=6),
        timings=Timings(load_ms=1800, prompt_ms=120, generation_ms=75, total_ms=2000),
    )

    recorded = await turns.finish(
        db, begun.conversation.id, begun.turn.id, ending, answer_id=answer_id, now=later
    )
    await db.commit()

    turn = begun.turn
    assert recorded == "recorded"
    assert (turn.status, turn.finish_reason, turn.error) == ("completed", "stop", None)
    assert (turn.answer_id, turn.finished_at) == (answer_id, later)
    assert (turn.input_tokens, turn.cached_input_tokens, turn.output_tokens) == (11, 6, 3)
    assert (turn.load_ms, turn.prompt_ms, turn.generation_ms) == (1800, 120, 75)
    answer = await db.get(Message, answer_id)
    assert answer is not None
    assert (answer.role, answer.parent_id, answer.created_at) == (
        "assistant",
        turn.question_id,
        later,
    )
    await db.refresh(begun.conversation)
    assert begun.conversation.current_leaf_id == answer_id
    assert begun.conversation.last_active_at == later


async def test_a_turn_that_produced_nothing_writes_no_answer(db: AsyncSession) -> None:
    begun = await asked(db)

    recorded = await turns.finish(
        db,
        begun.conversation.id,
        begun.turn.id,
        Ending("failed", error="model_unavailable"),
        answer_id=new_id(),
        now=NOON,
    )

    turn = begun.turn
    assert recorded == "recorded"
    assert (turn.status, turn.error, turn.answer_id) == ("failed", "model_unavailable", None)
    assert await count(db, Message) == 1  # the question, still without an answer


async def test_a_turn_that_was_given_up_on_stays_given_up_on(db: AsyncSession) -> None:
    """Its process was taken for dead, and the conversation may have moved on since. An
    answer that turns up after all is not slipped in behind everyone's back."""
    begun = await asked(db)
    much_later = NOON + STALE_AFTER
    await turns.give_up_on_stale(db, begun.conversation.id, now=much_later)
    await db.commit()

    recorded = await turns.finish(
        db,
        begun.conversation.id,
        begun.turn.id,
        Ending("completed", [TextPart(text="Sorry I'm late.")], finish_reason="stop"),
        answer_id=new_id(),
        now=much_later + timedelta(minutes=1),
    )

    turn = begun.turn
    assert recorded == "left"
    assert (turn.status, turn.finished_at, turn.answer_id) == ("interrupted", much_later, None)
    assert await count(db, Message) == 1


async def test_a_turn_is_given_up_on_only_after_a_minute_of_silence(db: AsyncSession) -> None:
    begun = await asked(db)

    await turns.give_up_on_stale(db, begun.conversation.id, now=NOON + timedelta(seconds=59))
    await db.refresh(begun.turn)
    before = begun.turn.status
    await turns.give_up_on_stale(db, begun.conversation.id, now=NOON + timedelta(seconds=60))
    await db.refresh(begun.turn)

    assert (before, begun.turn.status) == ("running", "interrupted")


async def test_a_turn_that_says_it_is_alive_is_not_given_up_on_however_long_it_takes(
    db: AsyncSession,
) -> None:
    begun = await asked(db)

    for minute in range(1, 31):
        now = NOON + timedelta(minutes=minute)
        await turns.still_alive(db, begun.turn.id, now=now - timedelta(seconds=20))
        await turns.give_up_on_stale(db, begun.conversation.id, now=now)
    await db.refresh(begun.turn)

    assert begun.turn.status == "running"
    assert begun.turn.alive_at == NOON + timedelta(minutes=29, seconds=40)


async def test_a_turn_that_has_ended_no_longer_says_it_is_alive(db: AsyncSession) -> None:
    """A beat that comes after the outcome must not touch it."""
    begun = await asked(db)
    await turns.finish(
        db, begun.conversation.id, begun.turn.id, Ending("stopped"), answer_id=new_id(), now=NOON
    )

    await turns.still_alive(db, begun.turn.id, now=NOON + timedelta(seconds=15))
    await db.refresh(begun.turn)

    assert (begun.turn.status, begun.turn.alive_at) == ("stopped", NOON)


async def test_finishing_a_turn_whose_conversation_is_gone_does_nothing(db: AsyncSession) -> None:
    begun = await asked(db)
    await conversations.delete(db, begun.conversation)
    await db.commit()

    recorded = await turns.finish(
        db,
        begun.conversation.id,
        begun.turn.id,
        Ending("completed", [TextPart(text="For nobody.")], finish_reason="stop"),
        answer_id=new_id(),
        now=NOON,
    )

    assert recorded == "gone"
    assert (await count(db, Conversation), await count(db, Message), await count(db, Turn)) == (
        0,
        0,
        0,
    )


async def test_the_last_turn_of_a_question_is_the_one_that_is_shown(db: AsyncSession) -> None:
    begun = await asked(db)
    await turns.finish(
        db,
        begun.conversation.id,
        begun.turn.id,
        Ending("failed", error="model_unavailable"),
        answer_id=new_id(),
        now=NOON,
    )
    again = await turns.answer_again(
        db,
        user_id=begun.conversation.user_id,
        conversation_id=begun.conversation.id,
        question_id=begun.turn.question_id,
        model="standard",
        thinking=True,
        turn_id=new_id(),
        now=NOON + timedelta(minutes=1),
    )

    latest = await turns.latest_for(db, begun.conversation, begun.turn.question_id)

    assert latest is not None
    assert (latest.id, latest.status, latest.thinking) == (again.turn.id, "running", True)


# --- Turns nobody looks at, which the worker gives up on ------------------------------


async def test_a_turn_nobody_looks_at_is_given_up_on_after_a_minute_of_silence(
    db: AsyncSession,
) -> None:
    """No conversation is named, and none is read. A turn heard from a microsecond after
    the other is a microsecond short of a minute's silence, and still running."""
    silent, heard_from_later = await asked(db), await asked(db)
    await turns.still_alive(db, heard_from_later.turn.id, now=NOON + timedelta(microseconds=1))
    await db.commit()
    now = NOON + STALE_AFTER

    given_up = await turns.give_up_on_stale_everywhere(db, now=now)
    await db.commit()

    assert given_up == [silent.turn.id]
    await db.refresh(silent.turn)
    await db.refresh(heard_from_later.turn)
    assert (silent.turn.status, silent.turn.finished_at) == ("interrupted", now)
    assert (heard_from_later.turn.status, heard_from_later.turn.finished_at) == ("running", None)


async def test_turns_that_ended_or_were_heard_from_are_left_alone(db: AsyncSession) -> None:
    """Each was last heard from an hour ago, or nearly: a turn that has ended says
    nothing more. Only the running one that was heard from 20 seconds ago did."""
    ended_at = NOON + timedelta(seconds=5)
    ended: list[Turn] = []
    for ending in (Ending("completed"), Ending("stopped"), Ending("failed"), Ending("interrupted")):
        begun = await asked(db)
        await turns.finish(
            db, begun.conversation.id, begun.turn.id, ending, answer_id=new_id(), now=ended_at
        )
        ended.append(begun.turn)
    alive = await asked(db)
    an_hour_later = NOON + timedelta(hours=1)
    await turns.still_alive(db, alive.turn.id, now=an_hour_later - timedelta(seconds=20))
    await db.commit()

    given_up = await turns.give_up_on_stale_everywhere(db, now=an_hour_later)
    await db.commit()

    assert given_up == []
    for turn in [*ended, alive.turn]:
        await db.refresh(turn)
    assert [(turn.status, turn.finished_at) for turn in ended] == [
        ("completed", ended_at),
        ("stopped", ended_at),
        ("failed", ended_at),
        ("interrupted", ended_at),
    ]
    assert (alive.turn.status, alive.turn.finished_at) == ("running", None)


async def test_the_stale_turns_of_every_account_and_conversation_go_in_one_run(
    db: AsyncSession,
) -> None:
    first, second = await asked(db), await asked(db)
    first_again = await asked(db, "A second conversation", by=first.conversation.user_id)

    given_up = await turns.give_up_on_stale_everywhere(db, now=NOON + STALE_AFTER)
    await db.commit()

    assert set(given_up) == {first.turn.id, second.turn.id, first_again.turn.id}
    assert len(given_up) == 3
    running = select(func.count()).select_from(Turn).where(Turn.status == "running")
    assert await db.scalar(running) == 0


async def test_one_run_gives_up_on_a_batch_at_most_the_longest_silent_first(
    db: AsyncSession,
) -> None:
    begun = [await asked(db) for _ in range(5)]
    for seconds, one in zip((30, 0, 20, 10, 40), begun, strict=True):
        await turns.still_alive(db, one.turn.id, now=NOON + timedelta(seconds=seconds))
    await db.commit()
    all_stale = NOON + timedelta(hours=1)

    async def a_run() -> set[uuid.UUID]:
        given_up = await turns.give_up_on_stale_everywhere(db, now=all_stale, limit=2)
        await db.commit()
        return set(given_up)

    # Silent since 0 and 10 seconds past noon, then 20 and 30, then 40, then none left.
    assert await a_run() == {begun[1].turn.id, begun[3].turn.id}
    assert await a_run() == {begun[2].turn.id, begun[0].turn.id}
    assert await a_run() == {begun[4].turn.id}
    assert await a_run() == set()


async def test_a_stale_turn_that_is_being_recorded_is_skipped_not_waited_for(
    db: AsyncSession, migrated: str
) -> None:
    """Its process was only slow, and is recording the turn when the worker comes: the
    turn's row is held until that is committed. The worker doesn't wait for it, and
    leaves the turn with the outcome it is given. Waiting would never end here, since
    the transaction that holds the row waits for the worker: the time limit fails the
    test instead."""
    late, dead = await asked(db), await asked(db)
    now = NOON + STALE_AFTER
    engine = create_engine(migrated)
    try:
        async with create_session_factory(engine)() as other:
            recorded = await turns.finish(
                other,
                late.conversation.id,
                late.turn.id,
                Ending("completed", [TextPart(text="Sorry I'm late.")], finish_reason="stop"),
                answer_id=new_id(),
                now=now,
            )
            async with asyncio.timeout(5):
                given_up = await turns.give_up_on_stale_everywhere(db, now=now)
                await db.commit()
            await other.commit()

        assert recorded == "recorded"
        assert given_up == [dead.turn.id]
        # The other transaction is over: the turn has its outcome, and nothing is left.
        assert await turns.give_up_on_stale_everywhere(db, now=now) == []
        await db.refresh(late.turn)
        assert (late.turn.status, late.turn.finished_at) == ("completed", now)
        assert late.turn.answer_id is not None
    finally:
        await engine.dispose()


# --- What the database itself refuses -------------------------------------------------

INSERT_TURN = text(
    "INSERT INTO turns (id, conversation_id, question_id, model, thinking, status, created_at,"
    " alive_at, finished_at) VALUES (:id, :conversation, :question, 'standard', false, :status,"
    " now(), now(), :finished)"
)


async def test_the_database_allows_one_running_turn_per_conversation(db: AsyncSession) -> None:
    """Whatever code starts a turn, and however many at the same moment."""
    begun = await asked(db)
    second = {
        "id": new_id(),
        "conversation": begun.conversation.id,
        "question": begun.turn.question_id,
        "status": "running",
        "finished": None,
    }

    with pytest.raises(IntegrityError, match="uq_turns_conversation_id_running"):
        await db.execute(INSERT_TURN, second)
    await db.rollback()

    # A finished one beside it is fine: a question can have any number of those.
    await db.execute(INSERT_TURN, second | {"status": "failed", "finished": NOON})
    assert await count(db, Turn) == 2


@pytest.mark.parametrize(
    ("status", "finished", "broken"),
    [
        ("completed", None, "ck_turns_running_or_finished"),  # over, but never ended
        ("paused", NOON, "ck_turns_status_is_known"),
    ],
)
async def test_the_database_refuses_a_turn_in_a_state_that_makes_no_sense(
    db: AsyncSession, status: str, finished: datetime | None, broken: str
) -> None:
    begun = await asked(db)
    await turns.finish(
        db, begun.conversation.id, begun.turn.id, Ending("stopped"), answer_id=new_id(), now=NOON
    )
    await db.commit()

    with pytest.raises(IntegrityError, match=broken):
        await db.execute(
            INSERT_TURN,
            {
                "id": new_id(),
                "conversation": begun.conversation.id,
                "question": begun.turn.question_id,
                "status": status,
                "finished": finished,
            },
        )


async def test_the_database_keeps_a_turn_inside_its_conversation(db: AsyncSession) -> None:
    here, elsewhere = await asked(db), await asked(db, "Another conversation")
    await turns.finish(
        db, here.conversation.id, here.turn.id, Ending("stopped"), answer_id=new_id(), now=NOON
    )
    await db.commit()

    with pytest.raises(IntegrityError, match="fk_turns_conversation_id_question_id_messages"):
        await db.execute(
            INSERT_TURN,
            {
                "id": new_id(),
                "conversation": here.conversation.id,
                "question": elsewhere.turn.question_id,  # a question of the other one
                "status": "failed",
                "finished": NOON,
            },
        )


async def test_a_question_can_only_be_answered_again_if_it_is_a_question(db: AsyncSession) -> None:
    begun = await asked(db)
    answer_id = new_id()
    await turns.finish(
        db,
        begun.conversation.id,
        begun.turn.id,
        Ending("completed", [TextPart(text="The saddle.")], finish_reason="stop"),
        answer_id=answer_id,
        now=NOON,
    )
    shared = {
        "user_id": begun.conversation.user_id,
        "conversation_id": begun.conversation.id,
        "model": "standard",
        "thinking": False,
        "turn_id": new_id(),
        "now": NOON,
    }

    with pytest.raises(tree.OutOfTurnError):
        await turns.answer_again(db, question_id=answer_id, **shared)  # type: ignore[arg-type]
    with pytest.raises(tree.NoSuchMessageError):
        await turns.answer_again(db, question_id=uuid.uuid4(), **shared)  # type: ignore[arg-type]
