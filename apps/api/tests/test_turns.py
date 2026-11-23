"""Turns below the API, against a real Postgres: what the HTTP tests can't reach.

Starting and finishing turns in the ordinary way is covered through the API, in
`test_turns_api.py`. Here: a turn that finishes after it was given up on, one whose
conversation is gone, the draft a turn keeps while it runs, the turns that nobody looks
at and the worker gives up on, the locks giving up takes, and what the database itself
refuses.
"""

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from actio.chat import conversations, tree, turns
from actio.chat.models import Conversation, Message, Turn
from actio.chat.parts import TextPart
from actio.chat.turns import BEAT, STALE_AFTER, Begun, Draft, Ending
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


def a_draft(text: str = "Half an answer") -> Draft:
    """What a turn's answer holds at a beat, under the id its message will have."""
    return Draft(answer_id=new_id(), parts=[TextPart(text=text)])


async def has_draft(db: AsyncSession, turn_id: uuid.UUID) -> bool:
    """Whether the turn's row holds a draft, as the database has it now."""
    found = await db.scalar(select(Turn.draft.is_not(None)).where(Turn.id == turn_id))
    return bool(found)


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
        first_word_at=NOON + timedelta(seconds=1),
    )

    recorded = await turns.finish(
        db, begun.conversation.id, begun.turn.id, ending, answer_id=answer_id, now=later
    )
    await db.commit()

    turn = begun.turn
    assert recorded == "recorded"
    assert (turn.status, turn.finish_reason, turn.error) == ("completed", "stop", None)
    assert (turn.answer_id, turn.finished_at) == (answer_id, later)
    assert turn.first_word_at == NOON + timedelta(seconds=1)
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
    # Whoever gives up on a turn doesn't know when its first word came, if one did.
    assert begun.turn.first_word_at is None


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
    """A beat that comes after the outcome must not touch it, nor leave a draft."""
    begun = await asked(db)
    await turns.finish(
        db, begun.conversation.id, begun.turn.id, Ending("stopped"), answer_id=new_id(), now=NOON
    )

    await turns.still_alive(db, begun.turn.id, now=NOON + timedelta(seconds=15), draft=a_draft())
    await db.refresh(begun.turn)

    assert (begun.turn.status, begun.turn.alive_at) == ("stopped", NOON)
    assert not await has_draft(db, begun.turn.id)


@pytest.mark.parametrize(
    "ending",
    [
        Ending("completed", [TextPart(text="The saddle.")], finish_reason="stop"),
        Ending("stopped", [TextPart(text="The sad")]),
        Ending("failed", [TextPart(text="The")], error="answer_interrupted"),
        Ending("interrupted", [TextPart(text="The sadd")]),  # by a shutdown
    ],
    ids=lambda ending: ending.status,
)
async def test_a_turn_that_ends_keeps_no_draft(db: AsyncSession, ending: Ending) -> None:
    """Whatever ends it, its answer is recorded as it ended, and the draft a beat had
    saved is gone with the outcome."""
    begun = await asked(db)
    await turns.still_alive(db, begun.turn.id, now=NOON + BEAT, draft=a_draft("The"))
    await db.commit()
    drafted = await has_draft(db, begun.turn.id)

    recorded = await turns.finish(
        db,
        begun.conversation.id,
        begun.turn.id,
        ending,
        answer_id=new_id(),
        now=NOON + timedelta(seconds=20),
    )
    await db.commit()

    assert (drafted, recorded) == (True, "recorded")
    assert not await has_draft(db, begun.turn.id)
    await db.refresh(begun.turn)
    assert begun.turn.status == ending.status


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


@pytest.mark.parametrize("held", ["shared", "to change"])
async def test_a_stale_turn_whose_conversation_someone_holds_is_left_for_the_next_run(
    db: AsyncSession, migrated: str, held: str
) -> None:
    """Keeping a turn's draft changes its conversation, so the worker takes the
    conversation's row too. Someone holds it: shared, as whoever reads the conversation
    does, or to change it, as a question asked in it does. The worker doesn't wait for
    them. It leaves that turn as it is, draft and all, and gives up on it in its next
    run. Waiting would never end here, since the other transaction waits for the worker:
    the time limit fails the test instead."""
    busy, quiet = await asked(db), await asked(db)
    drafts = {busy.turn.id: a_draft("Busy"), quiet.turn.id: a_draft("Quiet")}
    for begun in (busy, quiet):
        await turns.still_alive(db, begun.turn.id, now=NOON + BEAT, draft=drafts[begun.turn.id])
    await db.commit()
    now = NOON + BEAT + STALE_AFTER
    engine = create_engine(migrated)
    try:
        async with create_session_factory(engine)() as other:
            if held == "shared":
                assert await conversations.hold_still(other, busy.conversation)
            else:
                found = await conversations.get(
                    other, busy.conversation.id, user_id=busy.conversation.user_id, to_change=True
                )
                assert found is not None
            async with asyncio.timeout(5):
                first_run = await turns.give_up_on_stale_everywhere(db, now=now)
                await db.commit()
            await db.refresh(busy.turn)
            left = (busy.turn.status, busy.turn.draft, await count(db, Message))
            await other.rollback()

        second_run = await turns.give_up_on_stale_everywhere(db, now=now)
        await db.commit()
    finally:
        await engine.dispose()

    assert first_run == [quiet.turn.id]
    # The two questions, and the quiet turn's answer.
    assert left == ("running", drafts[busy.turn.id].model_dump(mode="json"), 3)
    assert second_run == [busy.turn.id]
    await db.refresh(busy.turn)
    assert (busy.turn.status, busy.turn.answer_id) == (
        "interrupted",
        drafts[busy.turn.id].answer_id,
    )


async def test_a_look_at_a_conversation_without_a_stale_turn_holds_nothing_and_changes_nothing(
    db: AsyncSession, migrated: str
) -> None:
    """Every read of a conversation looks for a stale turn first, and nearly always
    finds none: here a turn that is being written, heard from 59 seconds ago. That look
    takes no lock. Another transaction holds the conversation's row to change it, and
    the look doesn't wait for it: the time limit fails the test if it does."""
    begun = await asked(db)
    draft = a_draft()
    await turns.still_alive(db, begun.turn.id, now=NOON + BEAT, draft=draft)
    await db.commit()
    nearly_stale = NOON + BEAT + STALE_AFTER - timedelta(seconds=1)
    engine = create_engine(migrated)
    try:
        async with create_session_factory(engine)() as other:
            conversation = await conversations.get(
                other, begun.conversation.id, user_id=begun.conversation.user_id, to_change=True
            )
            assert conversation is not None
            async with asyncio.timeout(5):
                await turns.give_up_on_stale(db, begun.conversation.id, now=nearly_stale)
                await db.commit()
            await other.rollback()
    finally:
        await engine.dispose()

    turn = await turns.get(db, begun.conversation, begun.turn.id)
    assert turn is not None
    assert (turn.status, turn.alive_at) == ("running", NOON + BEAT)
    assert turn.draft == draft.model_dump(mode="json")
    await db.refresh(begun.conversation)
    assert (begun.conversation.current_leaf_id, begun.conversation.last_active_at) == (
        begun.turn.question_id,
        NOON,
    )
    assert await count(db, Message) == 1


async def until_someone_waits(db: AsyncSession) -> None:
    """Wait until a transaction waits for a lock that another one holds, as Postgres
    says. With a time limit: the test fails if nobody ever does."""
    async with asyncio.timeout(5):
        while True:
            waiting = await db.scalar(text("SELECT count(*) FROM pg_locks WHERE NOT granted"))
            await db.commit()  # the next look starts from what the database has then
            if waiting:
                return
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("meanwhile", ["its process recorded it", "it said that it is alive"])
async def test_whoever_looks_asks_again_once_it_holds_the_rows(
    db: AsyncSession, migrated: str, meanwhile: str
) -> None:
    """Someone looks, and finds a stale turn. But the turn's process was only slow, and
    holds the conversation's row at that moment: it is recording the answer, or saying
    that it is alive. The look waits for the row. Once it has it, it asks again whether
    the turn is stale, and it isn't any more: a recorded answer is not overwritten as
    interrupted, and a turn that is alive is not given up on."""
    begun = await asked(db)
    now = NOON + STALE_AFTER
    engine = create_engine(migrated)
    looking: asyncio.Task[None] | None = None
    try:
        open_session = create_session_factory(engine)
        async with open_session() as other, open_session() as looker:
            # What the other one does is not committed yet: whoever looks still finds
            # the turn stale, and then has to wait for the conversation's row.
            if meanwhile == "its process recorded it":
                recorded = await turns.finish(
                    other,
                    begun.conversation.id,
                    begun.turn.id,
                    Ending("completed", [TextPart(text="Sorry I'm late.")], finish_reason="stop"),
                    answer_id=new_id(),
                    now=now,
                    credits=7,
                )
                assert recorded == "recorded"
            else:
                held = await conversations.get(
                    other, begun.conversation.id, user_id=begun.conversation.user_id, to_change=True
                )
                assert held is not None
                await turns.still_alive(other, begun.turn.id, now=now)
            looking = asyncio.create_task(
                turns.give_up_on_stale(looker, begun.conversation.id, now=now)
            )
            await until_someone_waits(db)
            await other.commit()
            async with asyncio.timeout(5):
                await looking
            await looker.commit()
    finally:
        if looking is not None:
            looking.cancel()
        await engine.dispose()

    await db.refresh(begun.turn)
    if meanwhile == "its process recorded it":
        assert (begun.turn.status, begun.turn.finished_at, begun.turn.credits) == (
            "completed",
            now,
            7,
        )
        assert begun.turn.answer_id is not None
    else:
        assert (begun.turn.status, begun.turn.alive_at, begun.turn.finished_at) == (
            "running",
            now,
            None,
        )
    assert await count(db, Message) == (2 if meanwhile == "its process recorded it" else 1)


async def test_whoever_looks_takes_the_conversation_before_the_turn(
    db: AsyncSession, migrated: str
) -> None:
    """The order in which a question that is being asked takes them, and a turn that is
    being recorded. Someone asks in the conversation, and holds its row. Whoever looks
    waits for that row with nothing in its hands: the turn's row is still free for the
    one who asks. Taken the other way round, each would wait for the other."""
    begun = await asked(db)
    await turns.still_alive(db, begun.turn.id, now=NOON, draft=a_draft())
    await db.commit()
    now = NOON + STALE_AFTER
    engine = create_engine(migrated)
    looking: asyncio.Task[None] | None = None
    try:
        open_session = create_session_factory(engine)
        async with open_session() as other, open_session() as looker:
            held = await conversations.get(
                other, begun.conversation.id, user_id=begun.conversation.user_id, to_change=True
            )
            assert held is not None
            looking = asyncio.create_task(
                turns.give_up_on_stale(looker, begun.conversation.id, now=now)
            )
            await until_someone_waits(db)
            # Whoever holds the conversation gets the turn's row at once, without
            # waiting: `nowait` raises if someone else has it.
            free = await other.scalar(
                select(Turn.id).where(Turn.id == begun.turn.id).with_for_update(nowait=True)
            )
            assert free == begun.turn.id
            await other.commit()
            async with asyncio.timeout(5):
                await looking
            await looker.commit()
    finally:
        if looking is not None:
            looking.cancel()
        await engine.dispose()

    await db.refresh(begun.turn)
    assert (begun.turn.status, begun.turn.finished_at) == ("interrupted", now)
    assert begun.turn.answer_id is not None


async def test_only_a_taken_id_counts_as_a_draft_that_cant_be_kept(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whatever else the database refuses while an answer is kept is no reader's doing,
    and is raised, not passed over: here a refusal with another code than "this value
    is already taken"."""
    begun = await asked(db)
    await turns.still_alive(db, begun.turn.id, now=NOON, draft=a_draft())
    await db.commit()

    class RefusedError(Exception):
        sqlstate = "23503"  # Postgres's code for "the row this points at is missing"

    async def refuse(*_: Any, **__: Any) -> None:
        raise IntegrityError("INSERT INTO messages", {}, RefusedError())

    monkeypatch.setattr(tree, "append", refuse)

    with pytest.raises(IntegrityError):
        await turns.give_up_on_stale(db, begun.conversation.id, now=NOON + STALE_AFTER)
    await db.rollback()

    await db.refresh(begun.turn)
    assert (begun.turn.status, begun.turn.finished_at) == ("running", None)


@pytest.mark.parametrize("who", ["someone who looks", "the worker"])
async def test_a_draft_that_cant_be_read_is_given_up_on_without_an_answer(
    db: AsyncSession, who: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Written by another version of the code, with a kind of part this one doesn't
    know. The turn mustn't stay `running` for it: it is given up on as if it had no
    draft, and the log says so once, naming the turn."""
    begun = await asked(db)
    unknown = {"answer_id": str(new_id()), "parts": [{"type": "picture", "url": "saddle.png"}]}
    await db.execute(update(Turn).where(Turn.id == begun.turn.id).values(draft=unknown))
    await db.commit()
    now = NOON + STALE_AFTER

    if who == "the worker":
        assert await turns.give_up_on_stale_everywhere(db, now=now) == [begun.turn.id]
    else:
        await turns.give_up_on_stale(db, begun.conversation.id, now=now)
    await db.commit()

    await db.refresh(begun.turn)
    assert (begun.turn.status, begun.turn.finished_at) == ("interrupted", now)
    assert begun.turn.answer_id is None
    assert not await has_draft(db, begun.turn.id)
    assert await count(db, Message) == 1
    # Without the error itself, which quotes what the draft holds: a model's words.
    said = [
        (record.levelno, record.getMessage(), record.exc_info)
        for record in caplog.records
        if record.name == "actio.chat.turns"
    ]
    assert said == [
        (
            logging.WARNING,
            f"turn {begun.turn.id}: its draft couldn't be read, and nothing of it is kept",
            None,
        )
    ]


@pytest.mark.parametrize("who", ["someone who looks", "the worker"])
async def test_a_draft_whose_id_a_message_already_has_is_given_up_on_without_an_answer(
    db: AsyncSession, who: str, caplog: pytest.LogCaptureFixture
) -> None:
    """The id an answer will have is named to its readers while it is written, and
    whoever sends a message chooses that message's id. So a reader can give a message of
    its own the id its answer was going to have, and that answer can't be written any
    more. The turn mustn't stay `running` for it, and the worker mustn't stop at it: the
    turn is given up on without an answer, the log says so once, and the turn beside it
    in the batch keeps its draft."""
    taken, beside = await asked(db), await asked(db)
    its_id_is_taken = Draft(answer_id=beside.turn.question_id, parts=[TextPart(text="Half")])
    # The one whose answer can't be written was heard from first, so the worker comes
    # to it first, and to the other one after it.
    await turns.still_alive(db, taken.turn.id, now=NOON, draft=its_id_is_taken)
    await turns.still_alive(db, beside.turn.id, now=NOON + timedelta(seconds=1), draft=a_draft())
    await db.commit()
    # Whoever gives up doesn't have the other conversation's message at hand: the
    # database is what refuses the second message under that id.
    db.expunge_all()
    now = NOON + timedelta(seconds=1) + STALE_AFTER

    if who == "the worker":
        given_up = await turns.give_up_on_stale_everywhere(db, now=now)
        assert set(given_up) == {taken.turn.id, beside.turn.id}
    else:
        await turns.give_up_on_stale(db, taken.conversation.id, now=now)
    await db.commit()

    turn = await db.get(Turn, taken.turn.id, populate_existing=True)
    conversation = await db.get(Conversation, taken.conversation.id, populate_existing=True)
    assert turn is not None
    assert conversation is not None
    assert (turn.status, turn.finished_at, turn.answer_id) == ("interrupted", now, None)
    assert not await has_draft(db, turn.id)
    # The conversation is as it was: it shows its question, last active when it was asked.
    assert (conversation.current_leaf_id, conversation.last_active_at) == (turn.question_id, NOON)
    assert await count(db, Message) == (3 if who == "the worker" else 2)
    if who == "the worker":
        other = await db.get(Turn, beside.turn.id, populate_existing=True)
        assert other is not None
        assert (other.status, other.answer_id is None) == ("interrupted", False)
    said = [
        (record.levelno, record.getMessage(), record.exc_info)
        for record in caplog.records
        if record.name == "actio.chat.turns"
    ]
    assert said == [
        (
            logging.WARNING,
            f"turn {taken.turn.id}: its draft couldn't be kept, a message already has"
            " its answer's id",
            None,
        )
    ]


async def test_a_question_under_the_id_of_a_kept_answer_is_refused_as_taken(
    db: AsyncSession,
) -> None:
    """A reader is told the id its answer will have, and can send its next question
    under it. If the answer's process has died meanwhile, asking first gives up on the
    turn, which keeps the answer under that id. The question is then refused as any
    message whose id is taken is, before anything of it is written: not by the database,
    as an error nobody expected."""
    begun = await asked(db)
    conversation_id, user_id, turn_id = (
        begun.conversation.id,
        begun.conversation.user_id,
        begun.turn.id,
    )
    draft = a_draft()
    await turns.still_alive(db, turn_id, now=NOON, draft=draft)
    await db.commit()
    now = NOON + STALE_AFTER

    with pytest.raises(turns.IdTakenError):
        await turns.ask(
            db,
            user_id=user_id,
            conversation_id=conversation_id,
            message_id=draft.answer_id,
            parent_id=None,
            parts=[TextPart(text="And another thing.")],
            model="standard",
            thinking=False,
            turn_id=new_id(),
            now=now,
        )
    await db.rollback()  # as the route does when it refuses

    # Nothing of the attempt is left, and the turn is still there to be given up on.
    await turns.give_up_on_stale(db, conversation_id, now=now)
    await db.commit()
    turn = await db.get(Turn, turn_id, populate_existing=True)
    assert turn is not None
    assert (turn.status, turn.answer_id) == ("interrupted", draft.answer_id)
    assert await count(db, Message) == 2
    assert await count(db, Turn) == 1


async def test_asking_for_another_answer_gives_up_on_a_stale_turn_first(db: AsyncSession) -> None:
    """Nobody looked at the conversation in between: asking for another answer is what
    notices that the turn's process died. The turn is given up on with its draft as its
    answer, and the new turn starts beside it, for a question that has one answer
    already."""
    begun = await asked(db)
    draft = a_draft()
    await turns.still_alive(db, begun.turn.id, now=NOON, draft=draft)
    await db.commit()
    now = NOON + STALE_AFTER

    again = await turns.answer_again(
        db,
        user_id=begun.conversation.user_id,
        conversation_id=begun.conversation.id,
        question_id=begun.turn.question_id,
        model="standard",
        thinking=False,
        turn_id=new_id(),
        now=now,
    )
    await db.commit()

    await db.refresh(begun.turn)
    assert (begun.turn.status, begun.turn.answer_id) == ("interrupted", draft.answer_id)
    assert again.earlier_answers == [draft.answer_id]
    assert (again.turn.id == begun.turn.id, again.turn.status) == (False, "running")


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


async def test_the_database_refuses_a_draft_on_a_turn_that_has_ended(db: AsyncSession) -> None:
    """Whatever code ends a turn, or writes a draft."""
    begun = await asked(db)
    await turns.finish(
        db, begun.conversation.id, begun.turn.id, Ending("stopped"), answer_id=new_id(), now=NOON
    )
    await db.commit()

    with pytest.raises(IntegrityError, match="ck_turns_draft_only_while_running"):
        await db.execute(
            text("UPDATE turns SET draft = CAST(:draft AS jsonb) WHERE id = :id"),
            {"draft": a_draft().model_dump_json(), "id": begun.turn.id},
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
