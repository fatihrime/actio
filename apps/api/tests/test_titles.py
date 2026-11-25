"""Titles a model writes for a conversation, against a real Postgres: when one is asked
for, what the model is asked, what is made of its answer, and what is never written
over.

The first line a new conversation is named after is tested with the turns, through the
API. Here the job is deferred as a turn defers it, and run as the worker runs it
(`work_through`), or its handler alone when a test acts while the model writes: the
model is a `ScriptedModel`, which a test can hold half-way. Every wait has a time limit,
so that a broken job fails its test instead of hanging the run. A job that is put back,
to ask the model again later, is made due through the database
(`queue_books.wait_is_over`), and run again. Whether a title is on its way is read where
a page reads it, in the list, with the clock moved by the test.
"""

import asyncio
import logging
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import psycopg
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.pool import QueuePool

from actio import worker
from actio.catalog import Catalog, Gate, ModelStatus, OfferedModel
from actio.chat import conversations, titles, turns
from actio.chat.models import Conversation
from actio.chat.parts import TextPart
from actio.chat.turns import STALE_AFTER, Begun, Ending
from actio.core import jobs
from actio.core.ids import new_id
from actio.core.resources import Resources, open_resources
from actio.core.settings import Settings
from actio.identity import sessions
from actio_llm import (
    ChatRequest,
    ConnectionFailedError,
    ContextOverflowError,
    Finish,
    FinishReason,
    LLMError,
    ProviderError,
    ProviderUnavailableError,
    RateLimitedError,
    ReasoningDelta,
    RequestRejectedError,
    StreamEvent,
    TextDelta,
    UnsupportedServerError,
    Usage,
    User,
)

pytestmark = pytest.mark.anyio

NOON = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
ANSWERED = NOON + timedelta(seconds=20)
LATER = NOON + timedelta(minutes=5)
QUESTION = "How do I fix a flat tyre on my bike?"
DONE = Finish(FinishReason.STOP, Usage(input_tokens=80, output_tokens=5))
NEVER = object()
"""In a script: an `asyncio.Event` that nothing sets. Each test makes its own, bound to
its own event loop."""
PATIENCE_S = 5.0
"""How long a test waits for the job to get somewhere. It takes milliseconds; this only
keeps a broken job from hanging the run."""


# --- Helpers --------------------------------------------------------------------------


async def started(
    db: AsyncSession, question: str = QUESTION, *, by: uuid.UUID | None = None
) -> Begun:
    """The first question of a new conversation, with its turn running: a new guest's,
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


async def ended(
    db: AsyncSession,
    begun: Begun,
    status: Any = "completed",
    *,
    at: datetime = ANSWERED,
    ask_for_title: bool = True,
) -> uuid.UUID:
    """Record the turn as ended `status`, with an answer, as a turn is recorded when a
    model is named for titles, and commit. The answer's id."""
    answer_id = new_id()
    await turns.finish(
        db,
        begun.conversation.id,
        begun.turn.id,
        Ending(status, [TextPart(text="Take the wheel off first.")]),
        answer_id=answer_id,
        now=at,
        ask_for_title=ask_for_title,
    )
    await db.commit()
    return answer_id


async def asked_after(
    db: AsyncSession, begun: Begun, after: uuid.UUID | None, question: str
) -> Begun:
    """Another question in the same conversation, after the message `after`, or, after
    none, a new version of its first question: with its turn running."""
    later = await turns.ask(
        db,
        user_id=begun.conversation.user_id,
        conversation_id=begun.conversation.id,
        message_id=new_id(),
        parent_id=after,
        parts=[TextPart(text=question)],
        model="standard",
        thinking=False,
        turn_id=new_id(),
        now=LATER,
    )
    await db.commit()
    return later


async def queued(database_url: str) -> list[tuple[str, dict[str, Any]]]:
    """The jobs in the queue as another connection sees them, oldest first: each one's
    name and arguments. A job that succeeded is deleted; one that failed stays."""
    async with await psycopg.AsyncConnection.connect(database_url) as connection:
        cursor = await connection.execute(
            "SELECT task_name, args FROM procrastinate_jobs ORDER BY id"
        )
        return [(name, args) for name, args in await cursor.fetchall()]


def asked_for(conversation_id: uuid.UUID) -> list[tuple[str, dict[str, Any]]]:
    """The queue, holding the one job that writes this conversation's title."""
    return [(titles.WRITE_TITLE, {"conversation_id": str(conversation_id)})]


async def named(db: AsyncSession, conversation_id: uuid.UUID) -> tuple[Any, ...]:
    """The conversation's title, who named it, when a model was asked for one, and when
    it was last active, as the database has them now."""
    found = await db.execute(
        select(
            Conversation.title,
            Conversation.title_by,
            Conversation.title_asked_at,
            Conversation.last_active_at,
        ).where(Conversation.id == conversation_id)
    )
    row = tuple(found.one())
    await db.commit()  # the next look starts from what the database has then
    return row


async def run_the_job(settings: Settings, catalog: Catalog, clock: Any, work_through: Any) -> None:
    """Run what is queued of the job that writes titles, as a worker with these models
    runs it, until nothing is left."""
    async with open_resources(settings) as resources:
        await work_through(the_title_job(resources, catalog, clock))


def the_title_job(resources: Resources, catalog: Catalog, clock: Any) -> jobs.Job:
    """The job that writes titles, as the worker lists it."""
    [writing] = [
        job
        for job in worker.listed(resources, catalog, clock=lambda: clock.now)
        if job.name == titles.WRITE_TITLE
    ]
    return writing


def shown(request: ChatRequest) -> str:
    """The question as the request's one message quotes it, after the instructions."""
    [message] = request.messages
    assert isinstance(message, User)
    instructions, _, quoted = message.content.partition("\n\n<message>\n")
    assert instructions == titles.INSTRUCTIONS
    assert quoted.endswith("\n</message>")
    return quoted.removesuffix("\n</message>")


def scripted(script: tuple[Any, ...]) -> list[Any]:
    """The script, with an event of this test's own where it says `NEVER`."""
    return [asyncio.Event() if item is NEVER else item for item in script]


async def held(model: Any) -> None:
    """Wait until the model has yielded everything before its hold, and waits there."""
    await asyncio.wait_for(model.waiting.wait(), PATIENCE_S)


async def until(done: Callable[[], bool], *, within: float = PATIENCE_S) -> None:
    """Wait until `done()` holds, looking every fiftieth of a second. The test fails
    after `within` seconds."""
    async with asyncio.timeout(within):
        while not done():  # noqa: ASYNC110 - what is waited for is counted by the gate
            await asyncio.sleep(0.02)


async def until_someone_waits(db: AsyncSession) -> None:
    """Wait until a transaction on this test's database waits for a lock that another
    one holds, as Postgres says. With a time limit: the test fails if nobody ever does."""
    async with asyncio.timeout(PATIENCE_S):
        while True:
            waiting = await db.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity"
                    " WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
            )
            await db.commit()
            if waiting:
                return
            await asyncio.sleep(0.01)


async def until_named_by_a_model(
    db: AsyncSession, conversation_id: uuid.UUID, *, within: float = PATIENCE_S
) -> None:
    """Wait until the worker has written the conversation's title, looking every
    twentieth of a second. The test fails after `within` seconds."""
    async with asyncio.timeout(within):
        while (await named(db, conversation_id))[1] != "model":  # noqa: ASYNC110 - in the database
            await asyncio.sleep(0.05)


def said(caplog: pytest.LogCaptureFixture) -> list[tuple[int, str]]:
    """What the titles' own module said in the log."""
    return [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if record.name == "actio.chat.titles"
    ]


def put_back(run_again: int, in_seconds: float) -> list[tuple[str, str, int, float]]:
    """The queue, as `queue_books.due()` reads it, holding the one job that writes a
    title, put back to ask the model again: run again `run_again` times so far, and due
    in `in_seconds`."""
    return [(titles.WRITE_TITLE, "todo", run_again, in_seconds)]


def status_of(state: Any) -> Callable[..., Awaitable[ModelStatus]]:
    """What stands in for `Catalog.status`: the model is in `state`, whichever is asked."""

    async def status(name: str, *, now: datetime) -> ModelStatus:  # noqa: ARG001
        return ModelStatus(state, thinking=False)

    return status


# --- When a title is asked for ---------------------------------------------------------


async def test_the_first_turn_that_completes_asks_for_the_title_and_no_later_one_does(
    db: AsyncSession, migrated: str
) -> None:
    """One job, with the conversation's id, deferred with the turn's own outcome, and
    the moment noted on the conversation."""
    begun = await started(db)
    answer_id = await ended(db, begun)
    after_the_first = await queued(migrated)
    later = await asked_after(db, begun, answer_id, "And a puncture in a tubeless tyre?")
    await ended(db, later, at=LATER)

    assert after_the_first == asked_for(begun.conversation.id)
    assert await queued(migrated) == after_the_first
    title, by, asked_at, _ = await named(db, begun.conversation.id)
    assert (title, by, asked_at) == (QUESTION, "question", ANSWERED)


@pytest.mark.parametrize("how", ["failed", "stopped", "interrupted", "given up on"])
async def test_a_turn_that_doesnt_complete_asks_for_nothing_and_the_next_that_does_asks(
    db: AsyncSession, migrated: str, how: str
) -> None:
    """A turn that ended any other way leaves the title to the next turn that
    completes: here the same question, answered again."""
    begun = await started(db)
    if how == "given up on":
        await turns.give_up_on_stale(db, begun.conversation.id, now=NOON + STALE_AFTER)
        await db.commit()
    else:
        await ended(db, begun, how)
    after_it = await queued(migrated)
    again = await turns.answer_again(
        db,
        user_id=begun.conversation.user_id,
        conversation_id=begun.conversation.id,
        question_id=begun.turn.question_id,
        model="standard",
        thinking=False,
        turn_id=new_id(),
        now=LATER,
    )
    await db.commit()
    await ended(db, again, at=LATER)

    assert after_it == []
    assert await queued(migrated) == asked_for(begun.conversation.id)
    _, by, asked_at, _ = await named(db, begun.conversation.id)
    assert (by, asked_at) == ("question", LATER)


async def test_a_conversation_renamed_before_its_first_turn_ended_is_never_asked_for_a_title(
    db: AsyncSession, migrated: str
) -> None:
    begun = await started(db)
    conversations.rename(begun.conversation, "Flat tyres")
    await db.commit()

    await ended(db, begun)

    assert await queued(migrated) == []
    title, by, asked_at, _ = await named(db, begun.conversation.id)
    assert (title, by, asked_at) == ("Flat tyres", "person", None)


async def test_no_title_is_asked_for_when_no_model_is_named_for_titles(
    db: AsyncSession, migrated: str
) -> None:
    begun = await started(db)

    await ended(db, begun, ask_for_title=False)

    assert await queued(migrated) == []
    _, by, asked_at, _ = await named(db, begun.conversation.id)
    assert (by, asked_at) == ("question", None)


@pytest.mark.parametrize("for_titles", ["standard", None], ids=["named", "not named"])
async def test_a_turn_through_the_api_asks_for_a_title_when_the_models_file_names_a_model(
    for_titles: str | None, offer: Any, start_app: Any, settings: Settings, migrated: str
) -> None:
    """The route tells the turn, from the catalog it was given."""
    _, catalog = offer(TextDelta("Take the wheel off first."), DONE, titles=for_titles)
    browser = await start_app(settings, catalog=catalog)
    await browser.post("/api/session/guest")
    conversation_id = new_id()
    question = {"id": str(new_id()), "parts": [{"type": "text", "text": QUESTION}]}

    answered = await browser.post(
        f"/api/conversations/{conversation_id}/turns", json={"message": question}
    )

    assert answered.status_code == 200
    assert await queued(migrated) == ([] if for_titles is None else asked_for(conversation_id))


async def test_a_turn_whose_title_the_queue_refuses_is_recorded_and_counted_all_the_same(
    offer: Any,
    start_app: Any,
    settings: Settings,
    migrated: str,
    db: AsyncSession,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The database refuses every job here, as a trigger of the test's own says. The
    answer is stored and paid for, the conversation keeps its first line, with nothing
    noted, and the log says why."""
    async with await psycopg.AsyncConnection.connect(migrated) as connection:
        await connection.execute(
            "CREATE FUNCTION refuse_every_job() RETURNS trigger LANGUAGE plpgsql"
            " AS $$ BEGIN RAISE EXCEPTION 'no jobs today'; END $$"
        )
        await connection.execute(
            "CREATE TRIGGER refuse_every_job BEFORE INSERT ON procrastinate_jobs"
            " FOR EACH ROW EXECUTE FUNCTION refuse_every_job()"
        )
    _, catalog = offer(
        TextDelta("Take the wheel off first."),
        Finish(FinishReason.STOP, Usage(input_tokens=40, output_tokens=3)),
        titles="standard",
    )
    browser = await start_app(settings, catalog=catalog)
    await browser.post("/api/session/guest")
    conversation_id = new_id()
    question = {"id": str(new_id()), "parts": [{"type": "text", "text": QUESTION}]}

    answered = await browser.post(
        f"/api/conversations/{conversation_id}/turns", json={"message": question}
    )
    conversation = (await browser.get(f"/api/conversations/{conversation_id}")).json()
    usage = (await browser.get("/api/usage")).json()["windows"]

    assert answered.status_code == 200
    assert [message["role"] for message in conversation["messages"]] == ["user", "assistant"]
    assert conversation["messages"][1]["metadata"]["turn"]["status"] == "completed"
    assert [window["used"] for window in usage] == [43, 43]
    title, by, asked_at, _ = await named(db, conversation_id)
    assert (title, by, asked_at) == (QUESTION, "question", None)
    assert await queued(migrated) == []
    [(level, line)] = [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if record.name == "actio.chat.turns"
    ]
    assert level == logging.WARNING
    assert line.startswith(
        f"conversation {conversation_id}: its title couldn't be asked for, and it keeps its"
        " first line (the job write-title couldn't be deferred: no jobs today"
    )


# --- What the model is asked -----------------------------------------------------------


@pytest.mark.parametrize(
    ("can_reason", "thinking"), [(True, False), (False, None)], ids=["can reason", "can't"]
)
async def test_the_model_is_asked_as_a_turn_asks_it_with_the_first_question_alone(
    can_reason: bool,
    thinking: bool | None,
    db: AsyncSession,
    settings: Settings,
    clock: Any,
    offer: Any,
    work_through: Any,
) -> None:
    """By its own name on its server, in the same context window as a turn, with
    reasoning off when it can reason and nothing said of it when it can't. The
    conversation has gone on by the time its title is asked for: the first question is
    all the model is shown, quoted after the instructions, in one message. It is never
    a message of its own, which a model would answer."""
    model, catalog = offer(
        TextDelta("Fixing a flat tyre"), DONE, thinking=can_reason, titles="standard"
    )
    begun = await started(db)
    answer_id = await ended(db, begun, ask_for_title=False)
    later = await asked_after(db, begun, answer_id, "And a puncture in a tubeless tyre?")
    await ended(db, later, at=LATER)

    await run_the_job(settings, catalog, clock, work_through)

    [request] = model.requests
    offered = catalog.get("standard")
    assert (offered.model, offered.context_window) == ("scripted-1", 4096)
    assert request == ChatRequest(
        model=offered.model,
        messages=[
            User(
                "Write a title for the conversation that starts with the message below: at"
                " most six words, in the language of the message, without quotation marks"
                " and without a full stop. Answer with the title only. Do not answer the"
                " message.\n\n<message>\nHow do I fix a flat tyre on my bike?\n</message>"
            )
        ],
        max_output_tokens=24,
        context_window=offered.context_window,
        thinking=thinking,
    )


async def test_a_first_question_that_was_edited_is_named_as_it_is_shown(
    db: AsyncSession, settings: Settings, clock: Any, offer: Any, work_through: Any
) -> None:
    """Its first version had a turn that completed while no model was named for titles.
    The conversation shows the new version, and its branch."""
    model, catalog = offer(TextDelta("Fixing a flat tyre"), DONE, titles="standard")
    begun = await started(db, "How do I fix a flat tire?")
    await ended(db, begun, ask_for_title=False)
    edited = await asked_after(db, begun, None, QUESTION)
    await ended(db, edited, at=LATER)

    await run_the_job(settings, catalog, clock, work_through)

    [request] = model.requests
    assert shown(request) == QUESTION


async def test_a_long_first_question_is_cut_before_the_model_is_shown_it(
    db: AsyncSession, settings: Settings, clock: Any, offer: Any, work_through: Any
) -> None:
    question = "Why does my chain skip when I climb a hill in the lowest gear? " * 25
    model, catalog = offer(TextDelta("A skipping chain"), DONE, titles="standard")
    begun = await started(db, question)
    await ended(db, begun)

    await run_the_job(settings, catalog, clock, work_through)

    [request] = model.requests
    assert len(question) > 1000
    assert shown(request) == question[:1000]


async def test_a_first_question_that_begins_with_a_page_of_nothing_is_shown_from_its_words(
    db: AsyncSession, settings: Settings, clock: Any, offer: Any, work_through: Any
) -> None:
    """The space around it goes before the cut, not after: cut first, nothing but empty
    lines would be left to show the model."""
    model, catalog = offer(TextDelta("Fixing a flat tyre"), DONE, titles="standard")
    begun = await started(db, "\n" * 1200 + QUESTION + "\n")
    await ended(db, begun)

    await run_the_job(settings, catalog, clock, work_through)

    [request] = model.requests
    assert shown(request) == QUESTION


# --- What is made of its answer --------------------------------------------------------


TIDIED = {
    "as it should be": ("Fixing a flat tyre", "Fixing a flat tyre"),
    "a question": ("Why do tyres go flat?", "Why do tyres go flat?"),
    "in quotation marks": ('"Fixing a flat tyre"', "Fixing a flat tyre"),
    "in single ones": ("'Fixing a flat tyre'", "Fixing a flat tyre"),
    "in curly ones": ("“Fixing a flat tyre”", "Fixing a flat tyre"),
    "in German ones": ("„Einen Platten flicken“", "Einen Platten flicken"),
    "in guillemets": ("« Réparer une crevaison »", "Réparer une crevaison"),
    "in corner brackets": ("「パンク修理」", "パンク修理"),
    "with a full stop": ("Fixing a flat tyre.", "Fixing a flat tyre"),
    "with a full stop in its quotation marks": ('"Fixing a flat tyre."', "Fixing a flat tyre"),
    "with a full stop after them": ('"Fixing a flat tyre".', "Fixing a flat tyre"),
    "with a Japanese full stop": ("パンク修理。", "パンク修理"),
    "as a heading": ("# Fixing a flat tyre", "Fixing a flat tyre"),
    "as a smaller heading": ("### Fixing a flat tyre", "Fixing a flat tyre"),
    "in bold": ("**Fixing a flat tyre**", "Fixing a flat tyre"),
    "in italics": ("_Fixing a flat tyre_", "Fixing a flat tyre"),
    "as code": ("`Fixing a flat tyre`", "Fixing a flat tyre"),
    "as an item of a list": ("* Fixing a flat tyre", "Fixing a flat tyre"),
    "as an item with a dash": ("- Fixing a flat tyre", "Fixing a flat tyre"),
    "as an item with a plus": ("+ Fixing a flat tyre", "Fixing a flat tyre"),
    "as a numbered item": ("1. Fixing a flat tyre", "Fixing a flat tyre"),
    "as a heading in bold": ("## **Fixing a flat tyre**", "Fixing a flat tyre"),
    "after Title:": ("Title: Fixing a flat tyre", "Fixing a flat tyre"),
    "after title: in quotation marks": ('title: "Fixing a flat tyre".', "Fixing a flat tyre"),
    "after a bold Title:": ("**Title:** Fixing a flat tyre", "Fixing a flat tyre"),
    "after a bold Title, the colon outside": (
        "**Title**: Fixing a flat tyre",
        "Fixing a flat tyre",
    ),
    "starting with a number": ("26 inch wheels compared", "26 inch wheels compared"),
    "starting with a number below zero": ("-5 degrees and tyre grip", "-5 degrees and tyre grip"),
    "two things in quotation marks": ('"Tubes" and "tubeless"', '"Tubes" and "tubeless"'),
    "two things in curly ones": ("“Tubes” and “tubeless”", "“Tubes” and “tubeless”"),
    "an apostrophe in single ones": ("'What's a flat tyre?'", "What's a flat tyre?"),
    "two things in bold": ("**Tubes** and **tubeless**", "**Tubes** and **tubeless**"),
    "with characters no text holds": ("Fixing a\x00 flat\x07 tyre", "Fixing a flat tyre"),
    "with half a character at its end": ("Fixing a flat tyre \ud83d", "Fixing a flat tyre"),
    "with spaces to spare": ("  Fixing   a\tflat  tyre  ", "Fixing a flat tyre"),
    "after empty lines": ("\n\nFixing a flat tyre", "Fixing a flat tyre"),
    "before what the model went on to say": (
        "Fixing a flat tyre\n\nThis title sums up the question.",
        "Fixing a flat tyre",
    ),
    "in a block of code": ("```\nFixing a flat tyre\n```", "Fixing a flat tyre"),
    "in a block of code that names its language": (
        "```text\nFixing a flat tyre\n```",
        "Fixing a flat tyre",
    ),
    "with quotation marks inside": (
        'The "quick release" lever explained',
        'The "quick release" lever explained',
    ),
    "starting with code": ("`__init__` in Python classes", "`__init__` in Python classes"),
    "long": (
        "Fixing a flat tyre on a road bike with tubeless tyres and a quick release skewer",
        "Fixing a flat tyre on a road bike with tubeless tyres and a…",
    ),
}


@pytest.mark.parametrize("case", TIDIED)
def test_what_the_model_wrote_is_tidied_into_a_title(case: str) -> None:
    written, title = TIDIED[case]

    assert titles.tidy(written) == title


@pytest.mark.parametrize(
    "written", ["", "  \n\n ", '""', "“”", "**", "```\n```", ".", "# ", "\x00", "- "], ids=repr
)
def test_what_has_no_words_is_no_title(written: str) -> None:
    assert titles.tidy(written) is None


def test_a_title_is_looked_for_in_the_beginning_of_what_the_model_wrote_alone() -> None:
    """A model given room for 24 tokens that writes on, on a server that lets it,
    doesn't get all of it read: what is read is tidied on the event loop."""
    assert titles.READ_CHARS == 400
    assert titles.tidy(" " * 399 + "Tyres") == "T"
    assert titles.tidy(" " * 400 + "Tyres") is None
    assert titles.tidy("*" * 100_000 + "a" * 100_000) is None


def test_a_long_title_is_cut_as_a_first_line_is() -> None:
    """One rule for the two: the list shows them alike."""
    long = "Fixing a flat tyre on a road bike with tubeless tyres and a quick release skewer"

    assert titles.tidy(long) == titles.title_from([TextPart(text=long)])
    assert titles.TITLE_LENGTH == 60


def test_a_title_that_just_fits_is_left_whole() -> None:
    """One character more, and it is cut, with the mark that says so."""
    fits = "x" * titles.TITLE_LENGTH

    assert titles.cut(fits) == fits
    assert titles.tidy(fits) == fits
    assert titles.cut(fits + "x") == fits + "…"


# --- Writing it ------------------------------------------------------------------------


async def test_the_job_writes_what_the_model_wrote_and_the_list_keeps_its_order(
    db: AsyncSession,
    migrated: str,
    settings: Settings,
    clock: Any,
    offer: Any,
    work_through: Any,
) -> None:
    """Tidied: without the quotation marks around it, or the full stop. The
    conversation's last activity stays the answer's, and so does its place in the list."""
    model, catalog = offer(
        TextDelta('"Fixing a flat '), TextDelta('tyre."'), DONE, titles="standard"
    )
    begun = await started(db)
    await ended(db, begun)

    await run_the_job(settings, catalog, clock, work_through)

    assert len(model.requests) == 1
    assert await named(db, begun.conversation.id) == (
        "Fixing a flat tyre",
        "model",
        ANSWERED,
        ANSWERED,
    )
    assert await queued(migrated) == []  # it succeeded, and was deleted


async def test_a_job_where_no_model_is_named_for_titles_ends_well_and_says_so_once(
    db: AsyncSession,
    migrated: str,
    settings: Settings,
    clock: Any,
    offer: Any,
    work_through: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """As when the models file changed between the turn and the job."""
    model, catalog = offer(TextDelta("Fixing a flat tyre"), DONE)
    begun = await started(db)
    await ended(db, begun)

    await run_the_job(settings, catalog, clock, work_through)

    assert model.requests == []
    title, by, *_ = await named(db, begun.conversation.id)
    assert (title, by) == (QUESTION, "question")
    assert await queued(migrated) == []
    assert said(caplog) == [
        (
            logging.WARNING,
            f"conversation {begun.conversation.id}: no model is named for titles, and it keeps"
            " its first line",
        )
    ]


async def test_a_conversation_renamed_before_the_job_runs_keeps_its_name_and_no_model_is_asked(
    db: AsyncSession,
    migrated: str,
    settings: Settings,
    clock: Any,
    offer: Any,
    work_through: Any,
) -> None:
    model, catalog = offer(TextDelta("Fixing a flat tyre"), DONE, titles="standard")
    begun = await started(db)
    await ended(db, begun)
    conversation = await db.get(Conversation, begun.conversation.id)
    assert conversation is not None
    conversations.rename(conversation, "Flat tyres")
    await db.commit()

    await run_the_job(settings, catalog, clock, work_through)

    assert model.requests == []
    title, by, *_ = await named(db, begun.conversation.id)
    assert (title, by) == ("Flat tyres", "person")
    assert await queued(migrated) == []


async def test_a_conversation_renamed_while_the_model_writes_keeps_its_name(
    db: AsyncSession,
    migrated: str,
    settings: Settings,
    clock: Any,
    offer: Any,
    work_through: Any,
) -> None:
    hold = asyncio.Event()
    model, catalog = offer(TextDelta("Fixing a flat tyre"), hold, DONE, titles="standard")
    begun = await started(db)
    await ended(db, begun)

    running = asyncio.create_task(run_the_job(settings, catalog, clock, work_through))
    try:
        await held(model)
        conversation = await db.get(Conversation, begun.conversation.id)
        assert conversation is not None
        conversations.rename(conversation, "Flat tyres")
        await db.commit()
    finally:
        hold.set()
        await asyncio.wait_for(running, PATIENCE_S * 2)

    title, by, *_ = await named(db, begun.conversation.id)
    assert (title, by) == ("Flat tyres", "person")
    assert await queued(migrated) == []


async def test_a_rename_that_holds_the_conversation_as_the_title_is_written_is_waited_for_and_kept(
    db: AsyncSession, settings: Settings, clock: Any, offer: Any
) -> None:
    """Someone renames the conversation in the moment the model finishes: the rename
    has changed the row, and hasn't committed yet. The job waits for it, then finds the
    conversation named by a person, and leaves it."""
    hold = asyncio.Event()
    model, catalog = offer(TextDelta("Fixing a flat tyre"), hold, DONE, titles="standard")
    begun = await started(db)
    await ended(db, begun)

    async with open_resources(settings) as resources, resources.database() as renaming:
        writing = asyncio.create_task(
            titles.write(resources.database, catalog, begun.conversation.id, now=clock.now)
        )
        try:
            await held(model)
            conversation = await renaming.get(Conversation, begun.conversation.id)
            assert conversation is not None
            conversations.rename(conversation, "Flat tyres")
            await renaming.flush()  # the row is held until the rename commits
            hold.set()
            await until_someone_waits(db)
            await renaming.commit()
        finally:
            hold.set()
            await asyncio.wait_for(writing, PATIENCE_S)

    title, by, *_ = await named(db, begun.conversation.id)
    assert (title, by) == ("Flat tyres", "person")


async def test_a_rename_to_the_title_it_was_read_with_stands_though_a_model_wrote_one_since(
    db: AsyncSession, settings: Settings, clock: Any, offer: Any, work_through: Any
) -> None:
    """A rename's request reads the conversation, the model's title is written, and the
    rename then says the title it read: nothing changed, as its session sees it. The
    title is written all the same, and it is the person's."""
    _, catalog = offer(TextDelta("Fixing a flat tyre"), DONE, titles="standard")
    begun = await started(db)
    await ended(db, begun)
    conversation = await db.get(Conversation, begun.conversation.id)
    assert conversation is not None
    assert conversation.title == QUESTION

    await run_the_job(settings, catalog, clock, work_through)
    conversations.rename(conversation, QUESTION)
    await db.commit()

    title, by, *_ = await named(db, begun.conversation.id)
    assert (title, by) == (QUESTION, "person")


@pytest.mark.parametrize("when", ["before the job runs", "while the model writes"])
async def test_a_conversation_deleted_leaves_a_job_that_ends_well(
    when: str,
    db: AsyncSession,
    migrated: str,
    settings: Settings,
    clock: Any,
    offer: Any,
    work_through: Any,
) -> None:
    """Before the job runs, the model isn't asked at all."""
    hold = asyncio.Event()
    model, catalog = offer(TextDelta("Fixing a flat tyre"), hold, DONE, titles="standard")
    begun = await started(db)
    await ended(db, begun)

    async def delete() -> None:
        conversation = await db.get(Conversation, begun.conversation.id)
        assert conversation is not None
        await conversations.delete(db, conversation)
        await db.commit()

    if when == "before the job runs":
        await delete()
    running = asyncio.create_task(run_the_job(settings, catalog, clock, work_through))
    try:
        if when == "while the model writes":
            await held(model)
            await delete()
    finally:
        hold.set()
        await asyncio.wait_for(running, PATIENCE_S * 2)

    assert len(model.requests) == (0 if when == "before the job runs" else 1)
    assert await db.get(Conversation, begun.conversation.id) is None
    assert await queued(migrated) == []  # it succeeded, and was deleted


async def test_a_job_run_a_second_time_doesnt_ask_the_model_again(
    db: AsyncSession,
    migrated: str,
    settings: Settings,
    clock: Any,
    offer: Any,
    work_through: Any,
) -> None:
    """As after a stop cut it off once it had written, or a worker that took it for
    dead put it back: the conversation is named by a model already."""
    model, catalog = offer(TextDelta("Fixing a flat tyre"), DONE, titles="standard")
    begun = await started(db)
    await ended(db, begun)
    await run_the_job(settings, catalog, clock, work_through)
    await jobs.defer(db, titles.WRITE_TITLE, conversation_id=str(begun.conversation.id))
    await db.commit()

    await run_the_job(settings, catalog, clock, work_through)

    assert len(model.requests) == 1
    title, by, *_ = await named(db, begun.conversation.id)
    assert (title, by) == ("Fixing a flat tyre", "model")
    assert await queued(migrated) == []


class TwoTitles:
    """A model asked twice at the same time, which writes a different title each time,
    and finishes each one only when it is let go. The two are told apart by the order in
    which they came, whichever task asked."""

    def __init__(self) -> None:
        self.titles = ["Flat tyres", "Fixing a puncture"]
        self.asked = [asyncio.Event(), asyncio.Event()]
        self.let_go = [asyncio.Event(), asyncio.Event()]
        self.askers: list[asyncio.Task[Any]] = []

    async def stream(self, request: ChatRequest) -> AsyncGenerator[StreamEvent]:  # noqa: ARG002
        which = len(self.askers)
        task = asyncio.current_task()
        assert task is not None
        self.askers.append(task)
        self.asked[which].set()
        await self.let_go[which].wait()
        yield TextDelta(self.titles[which])
        yield DONE


async def test_two_runs_at_once_leave_one_title_and_no_error(
    db: AsyncSession, settings: Settings, clock: Any
) -> None:
    """Both found the conversation named by its first question, and asked the model.
    The first to finish writes its title. The second finds it there, and leaves it."""
    model = TwoTitles()
    offered = OfferedModel(
        name="standard",
        title="Two titles",
        kind="openai-compatible",
        model="two-titles",
        context_window=4096,
        max_output_tokens=512,
        chat=model,
    )
    catalog = Catalog(
        {"standard": offered}, default="standard", thinking={"standard": False}, titles="standard"
    )
    begun = await started(db)
    await ended(db, begun)

    async with open_resources(settings) as resources:
        runs = [
            asyncio.create_task(
                titles.write(resources.database, catalog, begun.conversation.id, now=clock.now)
            )
            for _ in range(2)
        ]
        try:
            async with asyncio.timeout(PATIENCE_S):
                await model.asked[0].wait()
                await model.asked[1].wait()
                model.let_go[0].set()
                await model.askers[0]  # the run the model heard first
                model.let_go[1].set()
                await model.askers[1]
        finally:
            for event in model.let_go:
                event.set()
            await asyncio.wait(runs, timeout=PATIENCE_S)

    title, by, *_ = await named(db, begun.conversation.id)
    assert (title, by) == ("Flat tyres", "model")


@pytest.mark.parametrize(
    ("script", "says"),
    [
        (
            (ProviderUnavailableError("connection refused"),),
            "no title, its model failed (ProviderUnavailableError)",
        ),
        (
            (RateLimitedError("too many requests", retry_after_s=20),),
            "no title, its model failed (RateLimitedError)",
        ),
        ((TextDelta("Fixing a fl"),), "no title, its model failed (StreamInterruptedError)"),
        ((ProviderError("bad gateway"),), "no title, its model failed (ProviderError)"),
        ((NEVER, DONE), "no title, its model took longer than 0.2 s"),
    ],
    ids=[
        "its server doesn't answer",
        "its provider refuses for now",
        "its answer breaks off",
        "its provider fails",
        "it takes too long",
    ],
)
async def test_a_title_the_model_couldnt_give_for_now_is_asked_for_again_in_thirty_seconds(
    script: tuple[Any, ...],
    says: str,
    db: AsyncSession,
    settings: Settings,
    clock: Any,
    offer: Any,
    work_through: Any,
    queue_books: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The job goes back into the queue, and the conversation keeps its first line, and
    the moment its title was asked for, meanwhile. The log says which conversation, and
    what kind of trouble, once. Too long is a fifth of a second here."""
    monkeypatch.setattr(titles, "WRITTEN_WITHIN_S", 0.2)
    model, catalog = offer(*scripted(script), titles="standard")
    begun = await started(db)
    await ended(db, begun)

    await run_the_job(settings, catalog, clock, work_through)

    assert len(model.requests) == 1
    assert await named(db, begun.conversation.id) == (QUESTION, "question", ANSWERED, ANSWERED)
    assert await queue_books.due() == put_back(1, 30)
    assert said(caplog) == [(logging.WARNING, f"conversation {begun.conversation.id}: {says}")]


@pytest.mark.parametrize(
    ("script", "says"),
    [
        (
            (RequestRejectedError("the request is malformed"),),
            "no title, its model failed (RequestRejectedError)",
        ),
        (
            (ContextOverflowError("the prompt is too long"),),
            "no title, its model failed (ContextOverflowError)",
        ),
        (
            (UnsupportedServerError("the server is older than this client"),),
            "no title, its model failed (UnsupportedServerError)",
        ),
        ((LLMError("something else went wrong"),), "no title, its model failed (LLMError)"),
        ((DONE,), "its model wrote nothing to make a title of, and it keeps its first line"),
        (
            (TextDelta('"'), TextDelta('"'), DONE),
            "its model wrote nothing to make a title of, and it keeps its first line",
        ),
        (
            (ReasoningDelta("Fixing a flat tyre"), DONE),
            "its model wrote nothing to make a title of, and it keeps its first line",
        ),
    ],
    ids=[
        "the provider refuses the request",
        "the question is too long for it",
        "its server is too old",
        "it fails otherwise",
        "it writes nothing",
        "only quotation marks",
        "only its reasoning",
    ],
)
async def test_a_title_the_model_wont_give_leaves_the_first_line_and_the_job_ends_well(
    script: tuple[Any, ...],
    says: str,
    db: AsyncSession,
    migrated: str,
    settings: Settings,
    clock: Any,
    offer: Any,
    work_through: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """It would be the same at the next try, so there is none. The log says which
    conversation, and what kind of trouble."""
    model, catalog = offer(*scripted(script), titles="standard")
    begun = await started(db)
    await ended(db, begun)

    await run_the_job(settings, catalog, clock, work_through)

    assert len(model.requests) == 1
    assert await named(db, begun.conversation.id) == (QUESTION, "question", ANSWERED, ANSWERED)
    assert await queued(migrated) == []  # it succeeded, and was deleted
    assert said(caplog) == [(logging.WARNING, f"conversation {begun.conversation.id}: {says}")]


@pytest.mark.parametrize(
    ("state", "then"),
    [("missing", []), ("unreachable", put_back(1, 30))],
    ids=["missing from its server, for good", "its server doesn't answer, for now"],
)
async def test_a_model_that_isnt_ready_is_not_asked_for_a_title(
    state: Any,
    then: list[tuple[str, str, int, float]],
    db: AsyncSession,
    settings: Settings,
    clock: Any,
    offer: Any,
    work_through: Any,
    queue_books: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """As a turn doesn't ask one. What such a model can do isn't known: one that reasons
    unless told not to would spend the title's few tokens on reasoning. One that is
    missing ends the job well. One whose server doesn't answer puts it back, to ask again
    in thirty seconds."""
    model, catalog = offer(TextDelta("Fixing a flat tyre"), DONE, titles="standard")
    monkeypatch.setattr(catalog, "status", status_of(state))
    begun = await started(db)
    await ended(db, begun)

    await run_the_job(settings, catalog, clock, work_through)

    assert model.requests == []
    assert await named(db, begun.conversation.id) == (QUESTION, "question", ANSWERED, ANSWERED)
    assert await queue_books.due() == then
    assert said(caplog) == [
        (logging.WARNING, f"conversation {begun.conversation.id}: no title, its model is {state}")
    ]


async def test_a_title_that_waits_too_long_for_its_models_server_is_given_up_on_at_the_gate(
    db: AsyncSession,
    settings: Settings,
    clock: Any,
    lineup: Any,
    work_through: Any,
    queue_books: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The time limit is over the wait for the server and the model's writing together:
    here an answer holds the server's one place throughout. The model is never asked,
    the title leaves the gate as it found it, and is asked for again in thirty seconds."""
    monkeypatch.setattr(titles, "WRITTEN_WITHIN_S", 0.2)
    one_at_a_time = Gate(1)
    naming = lineup.add("standard", TextDelta("Fixing a flat tyre"), DONE, gate=one_at_a_time)
    catalog = lineup.catalog(titles="standard")
    begun = await started(db)
    await ended(db, begun)

    async with asyncio.timeout(PATIENCE_S * 2), one_at_a_time:
        await run_the_job(settings, catalog, clock, work_through)
        waiting_afterwards = one_at_a_time.waiting

    assert naming.requests == []
    assert waiting_afterwards == 0
    assert not one_at_a_time.full
    assert await named(db, begun.conversation.id) == (QUESTION, "question", ANSWERED, ANSWERED)
    assert await queue_books.due() == put_back(1, 30)
    assert said(caplog) == [
        (
            logging.WARNING,
            f"conversation {begun.conversation.id}: no title, its model took longer than 0.2 s",
        )
    ]


async def test_a_later_try_that_the_model_answers_writes_the_title(
    db: AsyncSession,
    settings: Settings,
    clock: Any,
    offer: Any,
    work_through: Any,
    queue_books: Any,
) -> None:
    """The same job, run again from its start once its wait is over: its server answers
    by then, and the title is written as at a first try."""
    model, catalog = offer(ProviderUnavailableError("connection refused"), titles="standard")
    begun = await started(db)
    await ended(db, begun)
    await run_the_job(settings, catalog, clock, work_through)
    model.script = [TextDelta("Fixing a flat tyre"), DONE]
    await queue_books.wait_is_over()

    await run_the_job(settings, catalog, clock, work_through)

    assert len(model.requests) == 2
    assert await named(db, begun.conversation.id) == (
        "Fixing a flat tyre",
        "model",
        ANSWERED,
        ANSWERED,
    )
    assert await queue_books.due() == []  # it succeeded, and was deleted


async def test_a_later_try_asks_the_models_server_itself_and_not_what_it_said_the_last_time(
    db: AsyncSession,
    settings: Settings,
    clock: Any,
    offer: Any,
    work_through: Any,
    queue_books: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The catalog keeps what a server said about a model for a while, and the try that
    found the server away had just asked. The first wait is longer than that: at the
    next try the server is asked itself, answers by then, and the title is written. The
    clock is the test's, moved by the first wait."""
    model, catalog = offer(TextDelta("Fixing a flat tyre"), DONE, titles="standard")
    its_server_says = [
        ModelStatus("unreachable", thinking=False),
        ModelStatus("ready", thinking=False),
    ]

    async def ask(offered: OfferedModel) -> ModelStatus:  # noqa: ARG001
        return its_server_says.pop(0)

    monkeypatch.setattr(catalog, "_ask", ask)
    begun = await started(db)
    await ended(db, begun)
    await run_the_job(settings, catalog, clock, work_through)
    clock.advance(seconds=titles.ASKED_AGAIN_AFTER[0].total_seconds())
    await queue_books.wait_is_over()

    await run_the_job(settings, catalog, clock, work_through)

    assert its_server_says == []
    assert len(model.requests) == 1
    title, by, *_ = await named(db, begun.conversation.id)
    assert (title, by) == ("Fixing a flat tyre", "model")


async def test_a_title_the_model_couldnt_give_four_times_is_given_up_on_and_its_job_kept(
    db: AsyncSession,
    settings: Settings,
    clock: Any,
    offer: Any,
    work_through: Any,
    queue_books: Any,
) -> None:
    """Asked again after half a minute, two minutes, then ten. The fourth try fails the
    job: the conversation keeps its first line for good, and the job is kept as any that
    failed, to be looked at. A worker that comes by afterwards asks nothing."""
    model, catalog = offer(ProviderError("bad gateway"), titles="standard")
    begun = await started(db)
    await ended(db, begun)
    queue_after_each: list[list[tuple[str, str, int, float | None]]] = []

    for _ in range(4):
        await run_the_job(settings, catalog, clock, work_through)
        queue_after_each.append(await queue_books.due())
        await queue_books.wait_is_over()
    await run_the_job(settings, catalog, clock, work_through)

    assert queue_after_each[:3] == [put_back(1, 30), put_back(2, 120), put_back(3, 600)]
    assert [(name, status) for name, status, *_ in queue_after_each[3]] == [
        (titles.WRITE_TITLE, "failed")
    ]
    assert len(model.requests) == 4
    assert await named(db, begun.conversation.id) == (QUESTION, "question", ANSWERED, ANSWERED)
    assert [(name, status) for name, status, *_ in await queue_books.due()] == [
        (titles.WRITE_TITLE, "failed")
    ]


@pytest.mark.parametrize(
    ("how", "then"), [("renamed", ("Flat tyres", "person")), ("deleted", None)]
)
async def test_a_conversation_renamed_or_deleted_between_two_tries_is_left_as_it_is(
    how: str,
    then: tuple[str, str] | None,
    db: AsyncSession,
    settings: Settings,
    clock: Any,
    offer: Any,
    work_through: Any,
    queue_books: Any,
) -> None:
    """The next try is the job run again from its start: it finds the conversation named
    by a person, or gone, asks the model nothing, and ends well."""
    model, catalog = offer(ProviderError("bad gateway"), titles="standard")
    begun = await started(db)
    await ended(db, begun)
    await run_the_job(settings, catalog, clock, work_through)
    after_the_first_try = await queue_books.due()
    conversation = await db.get(Conversation, begun.conversation.id)
    assert conversation is not None
    if how == "renamed":
        conversations.rename(conversation, "Flat tyres")
    else:
        await conversations.delete(db, conversation)
    await db.commit()
    await queue_books.wait_is_over()

    await run_the_job(settings, catalog, clock, work_through)

    assert after_the_first_try == put_back(1, 30)  # so there is a second try, and it ran
    assert len(model.requests) == 1
    found = await db.execute(
        select(Conversation.title, Conversation.title_by).where(
            Conversation.id == begun.conversation.id
        )
    )
    row = found.one_or_none()
    assert (None if row is None else tuple(row)) == then
    assert await queue_books.due() == []  # it succeeded, and was deleted


async def test_a_job_cancelled_while_the_model_writes_is_cancelled_and_writes_nothing(
    db: AsyncSession, settings: Settings, clock: Any, offer: Any
) -> None:
    """As a stop cuts a job off at the end of its grace: the cancellation comes out, so
    that the queue runs the job again. It isn't taken for the model's failure."""
    model, catalog = offer(*scripted((TextDelta("Fixing a flat "), NEVER, DONE)), titles="standard")
    begun = await started(db)
    await ended(db, begun)

    async with open_resources(settings) as resources:
        writing = asyncio.create_task(
            titles.write(resources.database, catalog, begun.conversation.id, now=clock.now)
        )
        await held(model)
        writing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(writing, PATIENCE_S)

    assert model.closed.is_set()
    title, by, *_ = await named(db, begun.conversation.id)
    assert (title, by) == (QUESTION, "question")


async def test_no_connection_to_the_database_is_held_while_the_model_writes(
    db: AsyncSession, settings: Settings, clock: Any, offer: Any
) -> None:
    """A title can take its thirty seconds, waiting for a model's server that is busy
    with answers. The pool's connections are the answers' too."""
    hold = asyncio.Event()
    model, catalog = offer(TextDelta("Fixing a flat tyre"), hold, DONE, titles="standard")
    begun = await started(db)
    await ended(db, begun)

    async with open_resources(settings) as resources:
        pool = resources.engine.pool
        assert isinstance(pool, QueuePool)
        writing = asyncio.create_task(
            titles.write(resources.database, catalog, begun.conversation.id, now=clock.now)
        )
        try:
            await held(model)
            held_meanwhile = pool.checkedout()
        finally:
            hold.set()
            await asyncio.wait_for(writing, PATIENCE_S)

    assert held_meanwhile == 0
    title, by, *_ = await named(db, begun.conversation.id)
    assert (title, by) == ("Fixing a flat tyre", "model")


async def test_no_line_of_the_job_carries_the_question_or_the_title(
    db: AsyncSession,
    settings: Settings,
    clock: Any,
    offer: Any,
    once: Any,
    work_through: Any,
    queue_books: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """At any level, the queue's own lines included, each with the trace of its error.
    One job writes its title, at its model's second try, after a first that failed with
    an error that quotes the question. The other's model fails at each of its four tries,
    with such an error: the queue writes the trace of the last one, which fails the
    job."""
    caplog.set_level(logging.DEBUG, logger="actio")
    caplog.set_level(logging.DEBUG, logger="procrastinate")
    question = "Velora Cycles: how tight should the saddle clamp be?"
    title = "Saddle clamp torque"
    refused = ConnectionFailedError(f"can't reach the server for: {question}")
    writes, catalog = offer(once(refused), TextDelta(title), DONE, titles="standard")
    fails, failing = offer(ProviderError(f"the server choked on: {question}"), titles="standard")
    written = await started(db, question)
    await ended(db, written)
    await run_the_job(settings, catalog, clock, work_through)
    not_written = await started(db, question)
    await ended(db, not_written)
    for _ in range(4):
        await run_the_job(settings, failing, clock, work_through)
        await queue_books.wait_is_over()

    everything_said = caplog.text  # every record, with the trace of its error
    assert (len(writes.requests), len(fails.requests)) == (2, 4)
    assert await named(db, written.conversation.id) == (title, "model", ANSWERED, ANSWERED)
    assert [status for _, status, *_ in await queue_books.due()] == ["failed"]
    assert "procrastinate.worker" in everything_said
    assert everything_said.count("(ConnectionFailedError), and is asked once more") == 1
    assert everything_said.count("its model failed (ProviderError)") == 4 + 1  # the trace's
    assert "Traceback" in everything_said
    assert "saddle" not in everything_said.lower()


async def test_a_title_the_database_refuses_fails_its_job_without_a_word_of_it_in_the_log(
    db: AsyncSession,
    migrated: str,
    settings: Settings,
    clock: Any,
    offer: Any,
    work_through: Any,
    queue_books: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The write fails at the database, as when the database is lost in that moment:
    here a trigger of the test's own refuses it. The job fails at once, and isn't run
    again, and the queue says so with the error and its trace, which name the statement
    and none of its values: the title is one of them."""
    caplog.set_level(logging.DEBUG)
    question = "Velora Cycles: how tight should the saddle clamp be?"
    model, catalog = offer(TextDelta("Saddle clamp torque"), DONE, titles="standard")
    begun = await started(db, question)
    await ended(db, begun)
    async with await psycopg.AsyncConnection.connect(migrated) as connection:
        await connection.execute(
            "CREATE FUNCTION refuse_every_title() RETURNS trigger LANGUAGE plpgsql"
            " AS $$ BEGIN RAISE EXCEPTION 'no titles today'; END $$"
        )
        await connection.execute(
            "CREATE TRIGGER refuse_every_title BEFORE UPDATE OF title ON conversations"
            " FOR EACH ROW EXECUTE FUNCTION refuse_every_title()"
        )

    await run_the_job(settings, catalog, clock, work_through)

    assert len(model.requests) == 1
    title, by, *_ = await named(db, begun.conversation.id)
    assert (title, by) == (question, "question")
    assert await queued(migrated) == asked_for(begun.conversation.id)  # it failed, and stays
    assert [status for _, status, *_ in await queue_books.due()] == ["failed"]
    everything_said = caplog.text  # every record, with the trace of its error
    assert "no titles today" in everything_said
    assert "UPDATE conversations SET title=" in everything_said
    assert "saddle" not in everything_said.lower()


# --- At the gate -----------------------------------------------------------------------


class NotingGate(Gate):
    """A server that writes one answer at a time, which notes, each time a place is
    given back, whether the model it watches had closed its stream by then."""

    def __init__(self) -> None:
        super().__init__(1)
        self.watched: Any = None
        self.closed_by_then: list[bool] = []

    async def __aexit__(self, *exc: object) -> None:
        self.closed_by_then.append(self.watched.closed.is_set())
        await super().__aexit__(*exc)


async def test_a_title_gives_its_place_at_the_gate_back_once_the_models_stream_is_closed(
    db: AsyncSession, settings: Settings, clock: Any, lineup: Any
) -> None:
    """Not before: until its stream is closed, the server is still writing for it. The
    model here would go on writing after it said it had finished."""
    gate = NotingGate()
    naming = lineup.add(
        "standard", TextDelta("Fixing a flat tyre"), DONE, TextDelta(" and more"), gate=gate
    )
    gate.watched = naming
    catalog = lineup.catalog(titles="standard")
    begun = await started(db)
    await ended(db, begun)

    async with open_resources(settings) as resources, asyncio.timeout(PATIENCE_S):
        await titles.write(resources.database, catalog, begun.conversation.id, now=clock.now)

    assert gate.closed_by_then == [True]
    assert not gate.full
    title, by, *_ = await named(db, begun.conversation.id)
    assert (title, by) == ("Fixing a flat tyre", "model")


async def test_inside_the_api_a_title_waits_behind_an_answer_at_its_server_and_comes_after_it(
    db: AsyncSession,
    settings: Settings,
    open_app: Any,
    lineup: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In the API's process the worker has the app's own models, and their gates: a
    server that writes one answer at a time is asked for a title only once the answer
    it is writing has ended. The worker lists the title job alone here.

    The title is asked for while the answer is being written, by a job deferred by hand,
    for a conversation that had its first answer before."""
    listing = worker.listed

    def titles_only(resources: Resources, catalog: Catalog, *, clock: Any) -> list[jobs.Job]:
        return [
            job
            for job in listing(resources, catalog, clock=clock)
            if job.name == titles.WRITE_TITLE
        ]

    monkeypatch.setattr(worker, "listed", titles_only)
    one_at_a_time = Gate(1)
    hold = asyncio.Event()
    answering = lineup.add("standard", TextDelta("Take it off."), hold, DONE, gate=one_at_a_time)
    naming = lineup.add("namer", TextDelta("Fixing a flat tyre"), DONE, gate=one_at_a_time)
    earlier = await started(db)
    await ended(db, earlier, ask_for_title=False)
    message = {"id": str(new_id()), "parts": [{"type": "text", "text": "Which tyre levers?"}]}

    async with (
        asyncio.timeout(30),
        open_app(settings, catalog=lineup.catalog(titles="namer"), with_worker=True) as browser,
    ):
        await browser.post("/api/session/guest")
        asking = asyncio.create_task(
            browser.post(f"/api/conversations/{new_id()}/turns", json={"message": message})
        )
        await held(answering)
        await jobs.defer(db, titles.WRITE_TITLE, conversation_id=str(earlier.conversation.id))
        await db.commit()
        # The worker may have started a moment ago: it looks every five seconds besides.
        await until(lambda: one_at_a_time.waiting == 1, within=10)
        asked_meanwhile = naming.started.is_set()
        named_meanwhile = await named(db, earlier.conversation.id)
        hold.set()
        answered = await asking
        await until_named_by_a_model(db, earlier.conversation.id)

    assert not asked_meanwhile
    assert named_meanwhile[:2] == (QUESTION, "question")
    assert answered.status_code == 200
    assert answering.closed.is_set()
    title, by, *_ = await named(db, earlier.conversation.id)
    assert (title, by) == ("Fixing a flat tyre", "model")


# --- A model asked once more ----------------------------------------------------------


@pytest.mark.parametrize(
    ("failure", "after"),
    [
        (ConnectionFailedError(f"can't reach the server for: {QUESTION}"), ""),
        (RateLimitedError(f"429 for: {QUESTION}", retry_after_s=0.01), " after 0.01 s"),
    ],
    ids=["a connection that failed at once", "a server that asks for a short wait"],
)
async def test_a_title_is_written_at_its_models_second_try(
    failure: LLMError,
    after: str,
    db: AsyncSession,
    settings: Settings,
    clock: Any,
    offer: Any,
    once: Any,
    work_through: Any,
    queue_books: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Within the same run of the job, with the same request, as a turn asks it: the job
    isn't put back. The log says so, by the error's kind."""
    model, catalog = offer(once(failure), TextDelta("Fixing a flat tyre"), DONE, titles="standard")
    begun = await started(db)
    await ended(db, begun)

    await run_the_job(settings, catalog, clock, work_through)

    first, second = model.requests
    assert first == second
    title, by, *_ = await named(db, begun.conversation.id)
    assert (title, by) == ("Fixing a flat tyre", "model")
    assert await queue_books.due() == []  # it succeeded, and was deleted
    assert said(caplog) == [
        (
            logging.WARNING,
            f"conversation {begun.conversation.id}: model standard failed"
            f" ({type(failure).__name__}), and is asked once more{after}",
        )
    ]
    assert "flat tyre on my bike" not in caplog.text


@pytest.mark.parametrize(
    ("failure", "after"),
    [
        (ConnectionFailedError("can't reach the server"), ""),
        (RateLimitedError("429", retry_after_s=0.01), " after 0.01 s"),
    ],
    ids=["a connection that failed at once", "a server that asks for a short wait"],
)
async def test_a_title_whose_model_fails_so_twice_is_asked_for_again_in_thirty_seconds(
    failure: LLMError,
    after: str,
    db: AsyncSession,
    settings: Settings,
    clock: Any,
    offer: Any,
    work_through: Any,
    queue_books: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Its second failure is the try's, which another try may not meet again: the job
    is put back, as for any such failure."""
    model, catalog = offer(failure, titles="standard")
    begun = await started(db)
    await ended(db, begun)

    await run_the_job(settings, catalog, clock, work_through)

    kind = type(failure).__name__
    assert len(model.requests) == 2
    assert await named(db, begun.conversation.id) == (QUESTION, "question", ANSWERED, ANSWERED)
    assert await queue_books.due() == put_back(1, 30)
    assert [message for _, message in said(caplog)] == [
        f"conversation {begun.conversation.id}: model standard failed ({kind}), and is asked"
        f" once more{after}",
        f"conversation {begun.conversation.id}: no title, its model failed ({kind})",
    ]


async def test_the_pause_before_a_second_try_is_inside_a_titles_time_limit(
    db: AsyncSession,
    settings: Settings,
    clock: Any,
    offer: Any,
    work_through: Any,
    queue_books: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The limit is over everything a title waits for, the pause too. Here the server
    asks for two seconds, the most that is waited, and a title has a fifth of one: the
    model isn't asked again, and the title is asked for again in thirty seconds. The
    two seconds aren't waited out."""
    monkeypatch.setattr(titles, "WRITTEN_WITHIN_S", 0.2)
    model, catalog = offer(
        RateLimitedError("429", retry_after_s=2), TextDelta("Never"), DONE, titles="standard"
    )
    begun = await started(db)
    await ended(db, begun)

    async with asyncio.timeout(1.5):
        await run_the_job(settings, catalog, clock, work_through)

    assert len(model.requests) == 1
    assert await named(db, begun.conversation.id) == (QUESTION, "question", ANSWERED, ANSWERED)
    assert await queue_books.due() == put_back(1, 30)
    assert [message for _, message in said(caplog)] == [
        f"conversation {begun.conversation.id}: model standard failed (RateLimitedError), and is"
        " asked once more after 2 s",
        f"conversation {begun.conversation.id}: no title, its model took longer than 0.2 s",
    ]


# --- Models that stand in for the one named for titles --------------------------------


async def test_a_model_that_stands_in_writes_the_title_when_the_named_one_cant(
    db: AsyncSession,
    settings: Settings,
    clock: Any,
    lineup: Any,
    work_through: Any,
    queue_books: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Asked as the named one is, with a request of its own: its name on its server, its
    own window, and reasoning off only for a model that can reason. The log says which
    failed, by its error's kind, and which was asked next."""
    named_one = lineup.add(
        "standard",
        ProviderUnavailableError(f"busy with: {QUESTION}"),
        then=["second"],
        thinking=True,
    )
    stand_in = lineup.add(
        "second", TextDelta("Fixing a flat tyre"), DONE, context_window=2048, gate=Gate(1)
    )
    catalog = lineup.catalog(titles="standard")
    begun = await started(db)
    await ended(db, begun)

    await run_the_job(settings, catalog, clock, work_through)

    [asked_first], [asked_then] = named_one.requests, stand_in.requests
    assert (asked_first.model, asked_first.context_window, asked_first.thinking) == (
        "scripted-standard",
        4096,
        False,
    )
    assert asked_then == ChatRequest(
        model="scripted-second",
        messages=asked_first.messages,
        max_output_tokens=titles.TITLE_TOKENS,
        context_window=2048,
        thinking=None,
    )
    title, by, *_ = await named(db, begun.conversation.id)
    assert (title, by) == ("Fixing a flat tyre", "model")
    assert await queue_books.due() == []  # it succeeded, and was deleted
    assert said(caplog) == [
        (
            logging.WARNING,
            f"conversation {begun.conversation.id}: model standard failed"
            " (ProviderUnavailableError), and second is asked next",
        )
    ]
    assert "flat tyre on my bike" not in caplog.text


async def test_a_model_that_isnt_ready_is_passed_over_for_one_that_stands_in(
    db: AsyncSession,
    settings: Settings,
    clock: Any,
    lineup: Any,
    work_through: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    named_one = lineup.add(
        "standard", TextDelta("Never"), DONE, state="unreachable", then=["second"]
    )
    lineup.add("second", TextDelta("Fixing a flat tyre"), DONE)
    begun = await started(db)
    await ended(db, begun)

    await run_the_job(settings, lineup.catalog(titles="standard"), clock, work_through)

    assert named_one.requests == []
    title, by, *_ = await named(db, begun.conversation.id)
    assert (title, by) == ("Fixing a flat tyre", "model")
    assert said(caplog) == [
        (
            logging.WARNING,
            f"conversation {begun.conversation.id}: model standard is unreachable, and is"
            " passed over",
        )
    ]


@pytest.mark.parametrize(
    ("failures", "says", "then"),
    [
        (
            (RequestRejectedError("refused"), ProviderUnavailableError("busy")),
            "ProviderUnavailableError",
            put_back(1, 30),
        ),
        (
            (ProviderError("failed"), ContextOverflowError("too long")),
            "ProviderError",
            put_back(1, 30),
        ),
        (
            (RequestRejectedError("refused"), ContextOverflowError("too long")),
            "ContextOverflowError",
            [],
        ),
    ],
    ids=["refused, then busy", "failed, then too long", "refused, then too long"],
)
async def test_when_every_model_failed_the_title_is_asked_for_again_by_the_rule_of_errors(
    failures: tuple[LLMError, LLMError],
    says: str,
    then: list[tuple[str, str, int, float]],
    db: AsyncSession,
    settings: Settings,
    clock: Any,
    lineup: Any,
    work_through: Any,
    queue_books: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The error that another try may not meet again, when one of them failed so: then
    the job is put back. When none did, the last one's, and the job ends well."""
    first = lineup.add("standard", failures[0], then=["second"])
    second = lineup.add("second", failures[1])
    begun = await started(db)
    await ended(db, begun)

    await run_the_job(settings, lineup.catalog(titles="standard"), clock, work_through)

    assert (len(first.requests), len(second.requests)) == (1, 1)
    assert await named(db, begun.conversation.id) == (QUESTION, "question", ANSWERED, ANSWERED)
    assert await queue_books.due() == then
    assert said(caplog)[-1] == (
        logging.WARNING,
        f"conversation {begun.conversation.id}: no title, its model failed ({says})",
    )


@pytest.mark.parametrize(
    ("states", "then"),
    [(("missing", "unreachable"), put_back(1, 30)), (("missing", "missing"), [])],
    ids=["one of them unreachable, for now", "all missing, for good"],
)
async def test_when_none_of_the_models_is_ready_the_title_waits_only_for_one_that_is_unreachable(
    states: tuple[Any, Any],
    then: list[tuple[str, str, int, float]],
    db: AsyncSession,
    settings: Settings,
    clock: Any,
    lineup: Any,
    work_through: Any,
    queue_books: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A server that doesn't answer may answer at a later try. A model missing from its
    server is missing at the next try too."""
    first = lineup.add("standard", TextDelta("Never"), DONE, state=states[0], then=["second"])
    second = lineup.add("second", TextDelta("Never"), DONE, state=states[1])
    begun = await started(db)
    await ended(db, begun)

    await run_the_job(settings, lineup.catalog(titles="standard"), clock, work_through)

    assert (first.requests, second.requests) == ([], [])
    assert await named(db, begun.conversation.id) == (QUESTION, "question", ANSWERED, ANSWERED)
    assert await queue_books.due() == then
    assert said(caplog) == [
        (
            logging.WARNING,
            f"conversation {begun.conversation.id}: no title, none of its models is ready",
        )
    ]


# --- Whether the list says a title is on its way ---------------------------------------


def pending(listed: httpx.Response) -> list[bool]:
    """What a page of the list says of each conversation: whether its title is on its
    way."""
    return [item["title_pending"] for item in listed.json()["items"]]


async def answered_once(browser: httpx.AsyncClient, conversation_id: uuid.UUID) -> None:
    """Ask the conversation's first question, and read its whole answer."""
    question = {"id": str(new_id()), "parts": [{"type": "text", "text": QUESTION}]}
    answered = await browser.post(
        f"/api/conversations/{conversation_id}/turns", json={"message": question}
    )
    assert answered.status_code == 200


async def test_a_title_is_on_its_way_from_the_first_answer_for_fifteen_seconds(
    offer: Any, start_app: Any, settings: Settings, migrated: str, clock: Any
) -> None:
    """Not while the first answer is being written: no model was asked yet. From the
    moment it completed until fifteen seconds later, and not a moment longer, whatever
    became of the job: here no worker runs it. The conversation says so too."""
    hold = asyncio.Event()
    model, catalog = offer(TextDelta("Take the wheel off first."), hold, DONE, titles="standard")
    browser = await start_app(settings, catalog=catalog)
    await browser.post("/api/session/guest")
    conversation_id = new_id()
    asking = asyncio.create_task(answered_once(browser, conversation_id))
    try:
        await held(model)
        while_written = pending(await browser.get("/api/conversations"))
    finally:
        hold.set()
        await asyncio.wait_for(asking, PATIENCE_S)

    at_once = pending(await browser.get("/api/conversations"))
    opened = (await browser.get(f"/api/conversations/{conversation_id}")).json()
    clock.now += titles.ON_ITS_WAY_FOR - timedelta(microseconds=1)
    just_before = pending(await browser.get("/api/conversations"))
    clock.advance(microseconds=1)
    at_fifteen = pending(await browser.get("/api/conversations"))

    assert timedelta(seconds=15) == titles.ON_ITS_WAY_FOR  # the API's description says 15
    assert while_written == [False]
    assert (at_once, opened["title_pending"]) == ([True], True)
    assert just_before == [True]
    assert at_fifteen == [False]
    assert await queued(migrated) == asked_for(conversation_id)  # still there, never run


@pytest.mark.usefixtures("migrated")
async def test_a_clock_that_was_set_back_doesnt_keep_a_title_on_its_way(
    offer: Any, start_app: Any, settings: Settings, clock: Any
) -> None:
    """The model was asked at a time that is still to come, by the clock as it is now.
    The title is on its way while that time is less than fifteen seconds off, as it is
    the other way round, and not for as long as the clock was set back: a page would
    look again every second for all of that."""
    _, catalog = offer(TextDelta("Take the wheel off first."), DONE, titles="standard")
    browser = await start_app(settings, catalog=catalog)
    await browser.post("/api/session/guest")
    await answered_once(browser, new_id())

    clock.now -= titles.ON_ITS_WAY_FOR - timedelta(microseconds=1)
    just_within = pending(await browser.get("/api/conversations"))
    clock.now -= timedelta(microseconds=1)
    fifteen_back = pending(await browser.get("/api/conversations"))
    clock.now -= timedelta(hours=1)
    an_hour_back = pending(await browser.get("/api/conversations"))

    assert (just_within, fifteen_back, an_hour_back) == ([True], [False], [False])


async def test_no_title_is_on_its_way_where_no_model_is_named_for_titles(
    offer: Any, start_app: Any, settings: Settings, migrated: str
) -> None:
    assert migrated
    _, catalog = offer(TextDelta("Take the wheel off first."), DONE)
    browser = await start_app(settings, catalog=catalog)
    await browser.post("/api/session/guest")

    await answered_once(browser, new_id())

    assert pending(await browser.get("/api/conversations")) == [False]


async def test_no_title_is_on_its_way_once_the_model_wrote_it(
    offer: Any,
    start_app: Any,
    settings: Settings,
    migrated: str,
    clock: Any,
    work_through: Any,
) -> None:
    """At once, well within the fifteen seconds."""
    assert migrated
    _, catalog = offer(TextDelta("Fixing a flat tyre"), DONE, titles="standard")
    browser = await start_app(settings, catalog=catalog)
    await browser.post("/api/session/guest")
    await answered_once(browser, new_id())
    before = pending(await browser.get("/api/conversations"))

    await run_the_job(settings, catalog, clock, work_through)

    listed = (await browser.get("/api/conversations")).json()["items"]
    assert before == [True]
    assert [(item["title"], item["title_pending"]) for item in listed] == [
        ("Fixing a flat tyre", False)
    ]


async def test_no_title_is_on_its_way_once_a_person_named_the_conversation(
    offer: Any, start_app: Any, settings: Settings, migrated: str
) -> None:
    """The rename's own answer says so, and the list after it."""
    assert migrated
    _, catalog = offer(TextDelta("Take the wheel off first."), DONE, titles="standard")
    browser = await start_app(settings, catalog=catalog)
    await browser.post("/api/session/guest")
    conversation_id = new_id()
    await answered_once(browser, conversation_id)
    before = pending(await browser.get("/api/conversations"))

    renamed = await browser.patch(
        f"/api/conversations/{conversation_id}", json={"title": "Flat tyres"}
    )

    assert before == [True]
    assert renamed.status_code == 200
    assert (renamed.json()["title"], renamed.json()["title_pending"]) == ("Flat tyres", False)
    assert pending(await browser.get("/api/conversations")) == [False]
