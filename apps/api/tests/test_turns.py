"""Turns below the API, against a real Postgres: what the HTTP tests can't reach.

Starting and finishing turns in the ordinary way is covered through the API, in
`test_turns_api.py`. Here: a turn that finishes after it was given up on, one whose
conversation is gone, and what the database itself refuses.
"""

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
from actio.core.ids import new_id
from actio.identity import sessions
from actio_llm import Timings, Usage

pytestmark = pytest.mark.anyio

NOON = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


async def asked(db: AsyncSession, question: str = "Name a bike part.") -> Begun:
    """A new account's first question, with its turn running."""
    _, principal = await sessions.start_guest_session(db, now=NOON)
    begun = await turns.ask(
        db,
        user_id=principal.user_id,
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

    turn = await turns.finish(
        db, begun.conversation.id, begun.turn.id, ending, answer_id=answer_id, now=later
    )
    await db.commit()

    assert turn is not None
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

    turn = await turns.finish(
        db,
        begun.conversation.id,
        begun.turn.id,
        Ending("failed", error="model_unavailable"),
        answer_id=new_id(),
        now=NOON,
    )

    assert turn is not None
    assert (turn.status, turn.error, turn.answer_id) == ("failed", "model_unavailable", None)
    assert await count(db, Message) == 1  # the question, still without an answer


async def test_a_turn_that_was_given_up_on_stays_given_up_on(db: AsyncSession) -> None:
    """Its process was taken for dead, and the conversation may have moved on since. An
    answer that turns up after all is not slipped in behind everyone's back."""
    begun = await asked(db)
    much_later = NOON + STALE_AFTER
    await turns.give_up_on_stale(db, begun.conversation.id, now=much_later)
    await db.commit()

    turn = await turns.finish(
        db,
        begun.conversation.id,
        begun.turn.id,
        Ending("completed", [TextPart(text="Sorry I'm late.")], finish_reason="stop"),
        answer_id=new_id(),
        now=much_later + timedelta(minutes=1),
    )

    assert turn is not None
    assert (turn.status, turn.finished_at, turn.answer_id) == ("interrupted", much_later, None)
    assert await count(db, Message) == 1


async def test_a_turn_is_given_up_on_only_after_ten_minutes(db: AsyncSession) -> None:
    begun = await asked(db)

    await turns.give_up_on_stale(
        db, begun.conversation.id, now=NOON + timedelta(minutes=9, seconds=59)
    )
    await db.refresh(begun.turn)
    before = begun.turn.status
    await turns.give_up_on_stale(db, begun.conversation.id, now=NOON + timedelta(minutes=10))
    await db.refresh(begun.turn)

    assert (before, begun.turn.status) == ("running", "interrupted")


async def test_finishing_a_turn_whose_conversation_is_gone_does_nothing(db: AsyncSession) -> None:
    begun = await asked(db)
    await conversations.delete(db, begun.conversation)
    await db.commit()

    turn = await turns.finish(
        db,
        begun.conversation.id,
        begun.turn.id,
        Ending("completed", [TextPart(text="For nobody.")], finish_reason="stop"),
        answer_id=new_id(),
        now=NOON,
    )

    assert turn is None
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


# --- What the database itself refuses -------------------------------------------------

INSERT_TURN = text(
    "INSERT INTO turns (id, conversation_id, question_id, model, thinking, status, created_at,"
    " finished_at) VALUES (:id, :conversation, :question, 'standard', false, :status, now(),"
    " :finished)"
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
