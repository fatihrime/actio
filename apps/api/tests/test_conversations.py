"""Conversations and their message tree, below the API, against a real Postgres.

The examples pin down the rules. The property test at the end plays random sequences of
questions, answers and branch changes to the database and to a tree kept in plain
Python, and requires both to show the same branch after every step.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from actio.chat import conversations, tree
from actio.chat.conversations import Cursor
from actio.chat.models import Conversation, Message
from actio.chat.parts import ReasoningPart, TextPart, from_stored
from actio.chat.tree import NoSuchMessageError, OutOfTurnError
from actio.core.database import create_engine, create_session_factory
from actio.core.ids import new_id
from actio.identity import sessions
from actio.identity.models import User

pytestmark = pytest.mark.anyio

NOON = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


def later(minutes: float) -> datetime:
    return NOON + timedelta(minutes=minutes)


async def someone(db: AsyncSession) -> uuid.UUID:
    """A new account, to own conversations."""
    _, principal = await sessions.start_guest_session(db, now=NOON)
    return principal.user_id


async def started(db: AsyncSession, user_id: uuid.UUID, *, at: datetime = NOON) -> Conversation:
    return await conversations.create(db, new_id(), user_id=user_id, now=at)


class Talk:
    """Adds messages to one conversation, a minute apart, to keep the tests short."""

    def __init__(self, db: AsyncSession, conversation: Conversation) -> None:
        self.db = db
        self.conversation = conversation
        self.minute = 0

    async def say(self, role: str, text: str, after: Message | None) -> Message:
        self.minute += 1
        return await tree.append(
            self.db,
            self.conversation,
            message_id=new_id(),
            parent_id=after.id if after else None,
            role=role,  # type: ignore[arg-type]  # tests also pass roles that don't exist
            parts=[TextPart(text=text)],
            now=later(self.minute),
        )

    async def ask(self, text: str, after: Message | None = None) -> Message:
        return await self.say("user", text, after)

    async def answer(self, text: str, after: Message) -> Message:
        return await self.say("assistant", text, after)

    async def shown(self) -> list[str]:
        """The text of the branch that is shown, first message first."""
        branch = await tree.shown_branch(self.db, self.conversation)
        return [message.parts[0]["text"] for message in branch]


@pytest.fixture
async def talk(db: AsyncSession) -> Talk:
    return Talk(db, await started(db, await someone(db)))


async def count(db: AsyncSession, model: type) -> int:
    return await db.scalar(select(func.count()).select_from(model)) or 0


# --- Conversations --------------------------------------------------------------------


async def test_a_conversation_starts_empty_under_the_id_its_creator_chose(
    db: AsyncSession,
) -> None:
    user_id, chosen = await someone(db), new_id()

    conversation = await conversations.create(db, chosen, user_id=user_id, now=NOON)

    assert conversation.id == chosen
    assert (conversation.title, conversation.current_leaf_id) == (None, None)
    assert conversation.created_at == conversation.last_active_at == NOON
    assert await tree.shown_branch(db, conversation) == []


async def test_an_id_can_be_used_once(db: AsyncSession) -> None:
    mine, yours = await someone(db), await someone(db)
    taken = (await started(db, mine)).id
    await db.commit()
    db.expunge_all()  # as in another request, which knows nothing of the first

    with pytest.raises(IntegrityError, match="pk_conversations"):
        await conversations.create(db, taken, user_id=yours, now=NOON)


async def test_a_conversation_is_found_only_by_its_owner(db: AsyncSession) -> None:
    mine, yours = await someone(db), await someone(db)
    conversation = await started(db, mine)

    assert await conversations.get(db, conversation.id, user_id=mine) is conversation
    assert await conversations.get(db, conversation.id, user_id=yours) is None
    assert await conversations.get(db, new_id(), user_id=mine) is None


async def test_the_list_shows_the_most_recently_active_first(db: AsyncSession) -> None:
    mine, yours = await someone(db), await someone(db)
    monday = await started(db, mine, at=later(1))
    tuesday = await started(db, mine, at=later(2))
    wednesday = await started(db, mine, at=later(3))
    await started(db, yours, at=later(4))
    await tree.append(  # the oldest one gets a message, and moves to the top
        db,
        monday,
        message_id=new_id(),
        parent_id=None,
        role="user",
        parts=[TextPart(text="Still there?")],
        now=later(10),
    )

    page, more = await conversations.list_page(db, user_id=mine, limit=10)

    assert [c.id for c in page] == [monday.id, wednesday.id, tuesday.id]
    assert more is None


async def test_the_list_comes_in_pages_that_neither_repeat_nor_skip(db: AsyncSession) -> None:
    mine = await someone(db)
    made = [await started(db, mine, at=later(minute)) for minute in range(7)]
    newest_first = [c.id for c in reversed(made)]

    first, after_first = await conversations.list_page(db, user_id=mine, limit=3)
    second, after_second = await conversations.list_page(
        db, user_id=mine, limit=3, after=after_first
    )
    third, after_third = await conversations.list_page(
        db, user_id=mine, limit=3, after=after_second
    )

    assert [c.id for c in first + second + third] == newest_first
    assert [len(first), len(second), len(third)] == [3, 3, 1]
    assert after_first == Cursor(first[-1].last_active_at, first[-1].id)
    assert after_third is None


async def test_a_page_that_ends_exactly_at_the_end_says_so(db: AsyncSession) -> None:
    mine = await someone(db)
    for minute in range(3):
        await started(db, mine, at=later(minute))

    page, more = await conversations.list_page(db, user_id=mine, limit=3)

    assert len(page) == 3
    assert more is None  # no empty last page to fetch


async def test_conversations_active_at_the_same_moment_still_page_cleanly(
    db: AsyncSession,
) -> None:
    """The cursor is the moment and the id together, so a tie in time doesn't lose rows."""
    mine = await someone(db)
    made = {(await started(db, mine, at=NOON)).id for _ in range(5)}

    seen: list[uuid.UUID] = []
    after: Cursor | None = None
    for _ in range(3):
        page, after = await conversations.list_page(db, user_id=mine, limit=2, after=after)
        seen += [c.id for c in page]

    assert len(seen) == 5
    assert set(seen) == made
    assert after is None


async def test_something_that_becomes_active_between_pages_is_not_shown_twice(
    db: AsyncSession,
) -> None:
    mine = await someone(db)
    made = [await started(db, mine, at=later(minute)) for minute in range(4)]
    first, after = await conversations.list_page(db, user_id=mine, limit=2)

    made[0].last_active_at = later(60)  # the oldest gets a new message: it jumps to the top
    await db.flush()
    second, _ = await conversations.list_page(db, user_id=mine, limit=2, after=after)

    assert [c.id for c in first] == [made[3].id, made[2].id]
    assert [c.id for c in second] == [made[1].id]  # counting rows to skip would repeat one


async def test_renaming_keeps_the_place_in_the_list(db: AsyncSession) -> None:
    conversation = await started(db, await someone(db))

    conversations.rename(conversation, "  Bike repair  ")

    assert conversation.title == "Bike repair"
    assert conversation.last_active_at == NOON


@pytest.mark.parametrize("title", ["", "   ", "x" * 201])
async def test_a_title_has_1_to_200_characters(db: AsyncSession, title: str) -> None:
    conversation = await started(db, await someone(db))

    with pytest.raises(ValueError, match="1 to 200 characters"):
        conversations.rename(conversation, title)


async def test_deleting_a_conversation_deletes_its_messages_on_every_branch(
    db: AsyncSession, talk: Talk
) -> None:
    question = await talk.ask("How do I fix a flat tyre?")
    await talk.answer("Take the wheel off first.", question)
    await talk.answer("Find the hole first.", question)  # another version of the answer
    other = Talk(db, await started(db, talk.conversation.user_id))
    await other.ask("Something else")
    await db.commit()

    await conversations.delete(db, talk.conversation)
    await db.commit()

    assert await count(db, Conversation) == 1
    assert await other.shown() == ["Something else"]
    assert await count(db, Message) == 1


async def test_deleting_an_account_deletes_its_conversations(db: AsyncSession, talk: Talk) -> None:
    """The database enforces it: this is what removes a guest's messages after a day."""
    question = await talk.ask("How do I fix a flat tyre?")
    await talk.answer("Take the wheel off first.", question)
    await db.commit()

    await db.execute(text("DELETE FROM users WHERE id = :id"), {"id": talk.conversation.user_id})
    await db.commit()

    assert (await count(db, Conversation), await count(db, Message)) == (0, 0)


@pytest.fixture
async def elsewhere(migrated: str) -> AsyncIterator[AsyncSession]:
    """A second session on the same database: someone else, at the same time."""
    engine = create_engine(migrated)
    async with create_session_factory(engine)() as session:
        yield session
    await engine.dispose()


async def test_a_conversation_held_still_is_brought_up_to_date(
    db: AsyncSession, elsewhere: AsyncSession, talk: Talk
) -> None:
    await db.commit()
    theirs = await conversations.get(
        elsewhere, talk.conversation.id, user_id=talk.conversation.user_id
    )
    assert theirs is not None
    conversations.rename(theirs, "Flat tyres")
    await elsewhere.commit()
    assert talk.conversation.title is None  # what this session read earlier

    assert await conversations.hold_still(db, talk.conversation)

    assert talk.conversation.title == "Flat tyres"


async def test_a_conversation_held_still_keeps_writers_out_until_the_reader_is_done(
    db: AsyncSession, elsewhere: AsyncSession, talk: Talk
) -> None:
    """A turn locks the conversation's row before it writes. While someone is reading
    the conversation whole, that lock isn't given."""
    await db.commit()
    owner = talk.conversation.user_id
    await elsewhere.execute(text("SET lock_timeout = '200ms'"))

    assert await conversations.hold_still(db, talk.conversation)
    with pytest.raises(OperationalError, match="lock timeout"):
        await conversations.get(elsewhere, talk.conversation.id, user_id=owner, to_change=True)
    await elsewhere.rollback()
    # Another reader isn't kept out.
    seen = await conversations.get(elsewhere, talk.conversation.id, user_id=owner)
    assert seen is not None
    assert await conversations.hold_still(elsewhere, seen)
    await elsewhere.rollback()

    await db.commit()  # the reader is done

    assert await conversations.get(elsewhere, talk.conversation.id, user_id=owner, to_change=True)


async def test_a_conversation_deleted_meanwhile_cannot_be_held_still(
    db: AsyncSession, elsewhere: AsyncSession, talk: Talk
) -> None:
    await db.commit()
    theirs = await conversations.get(
        elsewhere, talk.conversation.id, user_id=talk.conversation.user_id
    )
    assert theirs is not None
    await conversations.delete(elsewhere, theirs)
    await elsewhere.commit()

    assert not await conversations.hold_still(db, talk.conversation)


async def test_a_guests_conversations_can_be_handed_to_an_account(db: AsyncSession) -> None:
    guest, account, bystander = await someone(db), await someone(db), await someone(db)
    as_guest = Talk(db, await started(db, guest))
    await as_guest.ask("Asked before logging in")
    kept = await started(db, account)
    untouched = await started(db, bystander)

    await conversations.move_all(db, from_user=guest, to_user=account)
    await db.execute(text("DELETE FROM users WHERE id = :id"), {"id": guest})
    await db.commit()

    page, _ = await conversations.list_page(db, user_id=account, limit=10)
    assert {c.id for c in page} == {as_guest.conversation.id, kept.id}
    assert await as_guest.shown() == ["Asked before logging in"]  # it outlived the guest
    assert await conversations.get(db, untouched.id, user_id=bystander) is not None
    assert await count(db, User) == 2


# --- The tree -------------------------------------------------------------------------


async def test_questions_and_answers_form_the_branch_that_is_shown(
    db: AsyncSession, talk: Talk
) -> None:
    question = await talk.ask("How do I fix a flat tyre?")
    answer = await talk.answer("Take the wheel off first.", question)
    follow_up = await talk.ask("And then?", answer)

    branch = await tree.shown_branch(db, talk.conversation)

    assert [message.id for message in branch] == [question.id, answer.id, follow_up.id]
    assert [message.role for message in branch] == ["user", "assistant", "user"]
    assert talk.conversation.current_leaf_id == follow_up.id
    assert talk.conversation.last_active_at == later(3)  # each message was a minute apart
    assert talk.conversation.created_at == NOON


async def test_a_message_keeps_its_parts_in_order(db: AsyncSession, talk: Talk) -> None:
    question = await talk.ask("Why is the sky blue?")
    parts: list[TextPart | ReasoningPart] = [
        ReasoningPart(text="Scattering."),
        TextPart(text="Air scatters blue light most."),
    ]

    answer = await tree.append(
        db,
        talk.conversation,
        message_id=new_id(),
        parent_id=question.id,
        role="assistant",
        parts=parts,
        now=later(5),
    )
    await db.commit()

    stored = await db.execute(text("SELECT parts FROM messages WHERE id = :id"), {"id": answer.id})
    assert stored.scalar_one() == [
        {"type": "reasoning", "text": "Scattering."},
        {"type": "text", "text": "Air scatters blue light most."},
    ]
    assert from_stored(answer.parts) == parts


@pytest.mark.parametrize(
    "stored",
    [
        [{"type": "video", "url": "https://example.com/a.mp4"}],  # a kind nobody defined
        [{"type": "text"}],
        [{"type": "text", "text": "hello", "colour": "red"}],
        [{"text": "hello"}],
    ],
)
def test_a_part_the_code_does_not_know_is_refused_not_passed_along(
    stored: list[dict[str, str]],
) -> None:
    with pytest.raises(ValidationError):
        from_stored(stored)


async def test_writing_a_question_again_starts_a_branch_beside_the_old_one(
    db: AsyncSession, talk: Talk
) -> None:
    question = await talk.ask("How do I fix a flat tire?")
    answer = await talk.answer("Take the wheel off first.", question)
    follow_up = await talk.ask("And then?", answer)
    await talk.answer("Lever the tyre off the rim.", follow_up)

    again = await talk.ask("And after the wheel is off?", after=answer)

    assert await talk.shown() == [
        "How do I fix a flat tire?",
        "Take the wheel off first.",
        "And after the wheel is off?",
    ]
    assert await count(db, Message) == 5  # nothing was overwritten
    branch = await tree.shown_branch(db, talk.conversation)
    versions = await tree.versions(db, talk.conversation, branch)
    assert versions == {
        question.id: [question.id],
        answer.id: [answer.id],
        again.id: [follow_up.id, again.id],  # oldest first
    }


async def test_the_first_message_can_be_written_again_too(db: AsyncSession, talk: Talk) -> None:
    first = await talk.ask("Helo")
    await talk.answer("Hello! Did you mean hello?", first)

    again = await talk.ask("Hello")

    assert await talk.shown() == ["Hello"]
    branch = await tree.shown_branch(db, talk.conversation)
    assert await tree.versions(db, talk.conversation, branch) == {again.id: [first.id, again.id]}


async def test_asking_for_another_answer_adds_a_version_of_the_answer(
    db: AsyncSession, talk: Talk
) -> None:
    question = await talk.ask("Name a bike part.")
    first = await talk.answer("The saddle.", question)
    second = await talk.answer("The chain.", question)
    third = await talk.answer("The bell.", question)

    branch = await tree.shown_branch(db, talk.conversation)

    assert await talk.shown() == ["Name a bike part.", "The bell."]
    versions = await tree.versions(db, talk.conversation, branch)
    assert versions[third.id] == [first.id, second.id, third.id]


@pytest.mark.parametrize(
    ("first", "then", "allowed"),
    [
        (None, "user", True),
        (None, "assistant", False),  # nothing to answer
        ("user", "assistant", True),
        ("user", "user", False),  # a second question before the answer
        ("assistant", "user", True),
        ("assistant", "assistant", False),  # an answer to an answer
    ],
)
async def test_questions_and_answers_take_turns(
    db: AsyncSession, talk: Talk, first: str | None, then: str, allowed: bool
) -> None:
    last: Message | None = None
    if first is not None:
        last = await talk.ask("A question")
        if first == "assistant":
            last = await talk.answer("An answer", last)
    before = (await count(db, Message), talk.conversation.current_leaf_id)

    if allowed:
        await talk.say(then, "Next", last)
        assert await count(db, Message) == before[0] + 1
    else:
        with pytest.raises(OutOfTurnError):
            await talk.say(then, "Next", last)
        assert (await count(db, Message), talk.conversation.current_leaf_id) == before


async def test_a_message_can_only_follow_one_of_the_same_conversation(
    db: AsyncSession, talk: Talk
) -> None:
    elsewhere = Talk(db, await started(db, talk.conversation.user_id))
    theirs = await elsewhere.ask("A question in another conversation")
    never_written = await db.get(Message, new_id())
    assert never_written is None

    with pytest.raises(NoSuchMessageError):
        await talk.answer("An answer in the wrong place", theirs)
    with pytest.raises(NoSuchMessageError):
        await tree.append(
            db,
            talk.conversation,
            message_id=new_id(),
            parent_id=new_id(),  # no such message anywhere
            role="assistant",
            parts=[TextPart(text="An answer to nothing")],
            now=NOON,
        )

    assert await talk.shown() == []


# --- Showing another branch -----------------------------------------------------------


async def test_showing_a_branch_through_an_earlier_version(db: AsyncSession, talk: Talk) -> None:
    question = await talk.ask("Name a bike part.")
    saddle = await talk.answer("The saddle.", question)
    why = await talk.ask("Why that one?", saddle)
    await talk.answer("You sit on it.", why)
    await talk.answer("The chain.", question)
    assert await talk.shown() == ["Name a bike part.", "The chain."]
    active = talk.conversation.last_active_at

    await tree.show_branch_through(db, talk.conversation, saddle.id)

    assert await talk.shown() == [
        "Name a bike part.",
        "The saddle.",
        "Why that one?",
        "You sit on it.",
    ]
    assert talk.conversation.last_active_at == active  # looking isn't activity


async def test_below_the_chosen_message_the_newest_versions_are_shown(
    db: AsyncSession, talk: Talk
) -> None:
    question = await talk.ask("Name a bike part.")
    saddle = await talk.answer("The saddle.", question)
    old = await talk.ask("Why that one?", saddle)
    await talk.answer("You sit on it.", old)
    new = await talk.ask("Is it comfortable?", saddle)
    await talk.answer("It depends.", new)
    await talk.answer("On a long ride, no.", new)  # the newest answer to the newest question
    await talk.answer("The chain.", question)

    await tree.show_branch_through(db, talk.conversation, saddle.id)

    assert await talk.shown() == [
        "Name a bike part.",
        "The saddle.",
        "Is it comfortable?",
        "On a long ride, no.",
    ]


async def test_showing_the_branch_through_a_last_message_shows_exactly_it(
    db: AsyncSession, talk: Talk
) -> None:
    question = await talk.ask("Name a bike part.")
    saddle = await talk.answer("The saddle.", question)
    await talk.answer("The chain.", question)

    await tree.show_branch_through(db, talk.conversation, saddle.id)

    assert talk.conversation.current_leaf_id == saddle.id


async def test_a_branch_of_another_conversation_cannot_be_shown(
    db: AsyncSession, talk: Talk
) -> None:
    mine = await talk.ask("Mine")
    elsewhere = Talk(db, await started(db, talk.conversation.user_id))
    theirs = await elsewhere.ask("Theirs")

    for message_id in (theirs.id, new_id()):
        with pytest.raises(NoSuchMessageError):
            await tree.show_branch_through(db, talk.conversation, message_id)

    assert talk.conversation.current_leaf_id == mine.id


# --- What the database itself refuses -------------------------------------------------


async def test_the_database_refuses_a_message_that_follows_another_conversations(
    db: AsyncSession, talk: Talk
) -> None:
    """Whatever code writes the row: a tree can't reach into another conversation."""
    elsewhere = Talk(db, await started(db, talk.conversation.user_id))
    theirs = await elsewhere.ask("A question in another conversation")
    await db.commit()

    with pytest.raises(IntegrityError, match="fk_messages_conversation_id_parent_id_messages"):
        await db.execute(
            text(
                "INSERT INTO messages (id, conversation_id, parent_id, role, parts, created_at)"
                " VALUES (:id, :conversation, :parent, 'assistant', '[]', now())"
            ),
            {"id": new_id(), "conversation": talk.conversation.id, "parent": theirs.id},
        )


async def test_the_database_refuses_to_show_another_conversations_message(
    db: AsyncSession, talk: Talk
) -> None:
    elsewhere = Talk(db, await started(db, talk.conversation.user_id))
    theirs = await elsewhere.ask("A question in another conversation")
    await db.commit()

    with pytest.raises(IntegrityError, match="fk_conversations_id_current_leaf_id_messages"):
        await db.execute(
            text("UPDATE conversations SET current_leaf_id = :leaf WHERE id = :id"),
            {"leaf": theirs.id, "id": talk.conversation.id},
        )


async def test_the_database_knows_only_two_kinds_of_author(db: AsyncSession, talk: Talk) -> None:
    await db.commit()

    with pytest.raises(IntegrityError, match="ck_messages_role_is_known"):
        await db.execute(
            text(
                "INSERT INTO messages (id, conversation_id, role, parts, created_at)"
                " VALUES (:id, :conversation, 'system', '[]', now())"
            ),
            {"id": new_id(), "conversation": talk.conversation.id},
        )


# --- The database against a plain tree ------------------------------------------------


class PlainTree:
    """A conversation's tree as Python would keep it, to test the queries against."""

    def __init__(self) -> None:
        self.parent: dict[uuid.UUID, uuid.UUID | None] = {}
        self.role: dict[uuid.UUID, str] = {}
        self.in_order: list[uuid.UUID] = []  # oldest first
        self.leaf: uuid.UUID | None = None

    def add(self, message_id: uuid.UUID, parent: uuid.UUID | None, role: str) -> None:
        self.parent[message_id] = parent
        self.role[message_id] = role
        self.in_order.append(message_id)
        self.leaf = message_id

    def followers(self, message_id: uuid.UUID | None) -> list[uuid.UUID]:
        return [m for m in self.in_order if self.parent[m] == message_id]

    def branch(self) -> list[uuid.UUID]:
        path: list[uuid.UUID] = []
        at = self.leaf
        while at is not None:
            path.append(at)
            at = self.parent[at]
        return path[::-1]

    def show_through(self, message_id: uuid.UUID) -> None:
        while followers := self.followers(message_id):
            message_id = followers[-1]
        self.leaf = message_id

    def of_role(self, role: str) -> list[uuid.UUID]:
        return [m for m in self.in_order if self.role[m] == role]


# A step is what to do, and a number that picks where: which message to follow or show.
STEPS = st.lists(
    st.tuples(st.sampled_from(["ask", "ask", "answer", "answer", "show"]), st.integers(0, 10_000)),
    min_size=1,
    max_size=40,
)


async def play(
    open_session: async_sessionmaker[AsyncSession], steps: list[tuple[str, int]]
) -> None:
    async with open_session() as db:
        conversation = await started(db, await someone(db))
        plain = PlainTree()
        for number, (action, pick) in enumerate(steps):
            answers, questions = plain.of_role("assistant"), plain.of_role("user")
            if action == "ask":
                # After any answer, or as a first message: one more choice than answers.
                parent = [None, *answers][pick % (len(answers) + 1)]
                role = "user"
            elif action == "answer" and questions:
                parent, role = questions[pick % len(questions)], "assistant"
            elif action == "show" and plain.in_order:
                through = plain.in_order[pick % len(plain.in_order)]
                await tree.show_branch_through(db, conversation, through)
                plain.show_through(through)
                parent = role = None
            else:
                continue
            if role is not None:
                message = await tree.append(
                    db,
                    conversation,
                    message_id=new_id(),
                    parent_id=parent,
                    role=role,  # type: ignore[arg-type]  # one of the two, chosen above
                    parts=[TextPart(text=str(number))],
                    now=later(number),
                )
                plain.add(message.id, parent, role)

            branch = await tree.shown_branch(db, conversation)
            assert [message.id for message in branch] == plain.branch()
            assert conversation.current_leaf_id == plain.leaf
            assert await tree.versions(db, conversation, branch) == {
                message.id: plain.followers(message.parent_id) for message in branch
            }
        await db.rollback()  # leave nothing behind for the next example


def test_the_queries_agree_with_a_plain_tree(migrated: str) -> None:
    with asyncio.Runner() as runner:
        engine = create_engine(migrated)
        open_session = create_session_factory(engine)

        @settings(deadline=None)  # an example is dozens of round trips to a server
        @given(steps=STEPS)
        def check(steps: list[tuple[str, int]]) -> None:
            runner.run(play(open_session, steps))

        try:
            check()
        finally:
            runner.run(engine.dispose())
