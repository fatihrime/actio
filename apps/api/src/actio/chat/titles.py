"""A conversation's title: its first question's first line, until a model writes one.

A new conversation is named after the first line of its first question (`title_from`).
When the models file names a model for titles (`Catalog.titles`), the first of its turns
that completes asks for a better one: it defers the job `WRITE_TITLE`, in the
transaction that records it (`actio.chat.turns.finish`). The worker runs `write`, which
asks that model for a title of a few words, from the first question alone, and writes it
over the first line, unless a person has renamed the conversation meanwhile.

Who named a conversation is on its row (`Conversation.title_by`): `question`, `model`,
or `person`. Only `write` makes a `question` a `model`, and nothing writes over a
`person`: a title someone chose stays.

For a moment after a model was asked, a title is on its way (`on_its_way`). The list of
conversations says so, and a page that reads it reads it again until it no longer does.

A title costs the account nothing: nothing is held for it, and the usage ledger doesn't
hear of it. It is one small call per conversation, bounded by `QUESTION_CHARS` and
`TITLE_TOKENS`, and it comes only after a turn that passed the limits itself.

What people wrote stays out of the log: neither the question nor the title is said in
it, at any level. Ids only.
"""

import asyncio
import logging
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import aclosing, nullcontext
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from actio.catalog import Catalog, OfferedModel
from actio.chat import tree
from actio.chat.models import Conversation
from actio.chat.parts import Part, TextPart, from_stored
from actio_llm import (
    ChatRequest,
    Finish,
    LLMError,
    StreamEvent,
    StreamInterruptedError,
    TextDelta,
    User,
)

logger = logging.getLogger(__name__)

WRITE_TITLE = "write-title"
"""The job that asks a model for a conversation's title, and writes it: deferred once
for a conversation, by the first of its turns that completes."""

TITLE_LENGTH = 60
"""A title is cut at a word to about this many characters: a first question's first
line, and what a model wrote, alike."""

QUESTION_CHARS = 1000
"""How much of the first question a model is shown, at most, to name its conversation
after."""

TITLE_TOKENS = 24
"""The most a model may write for a title, in tokens. Measured with the instructions
below, a model wrote 2 to 13."""

WRITTEN_WITHIN_S = 30.0
"""How long a title may take: the wait for the model's server, and the model's answer,
together. Past it, the conversation keeps its first line."""

ON_ITS_WAY_FOR = timedelta(seconds=15)
"""How long after a model was asked for a title the title is said to be on its way, while
none has come. A title took about half a second, measured (ADR 0025). One that takes
longer, as behind a long answer at a server that writes one at a time, shows whenever
the list is next read."""

INSTRUCTIONS = (
    "Write a title for the conversation that starts with the message below: at most six "
    "words, in the language of the message, without quotation marks and without a full "
    "stop. Answer with the title only. Do not answer the message."
)
"""What a model is told, in one message of the person's: these instructions, then the
first question, quoted (`_asking`).

The question is quoted, never the model's own message to answer. Measured on a small
local model with reasoning off (ADR 0025): shown the question as a message of its own,
after these instructions, it named easy questions well, and answered those that carry
an instruction of their own, or did what they said: "Answer in three short sentences",
"Reply with the translation only", "say BANANA". With the question quoted after the
instructions it wrote a title for every one of them, in the question's language.
"""

READ_CHARS = 400
"""How much of what a model wrote is looked at for a title, at most. It was asked for six
words, and given room for `TITLE_TOKENS`: a server that lets it write on doesn't get all
of it read."""

_QUOTES = "\"'“”„‟‘’‚‛«»‹›「」『』"  # noqa: RUF001 - they are quotation marks, on purpose
"""The quotation marks a model may put around a title, in the languages it writes."""

_APOSTROPHES = "'’"  # noqa: RUF001 - both are apostrophes, on purpose
"""The quotation marks that also stand inside a word, as in `What's`."""

_UNPRINTABLE = re.compile(r"[\x00-\x1f\x7f-\x9f\ud800-\udfff]")
"""What no title holds: control characters, which a database's text can't all take, and
halves of a character that was cut in two."""

_FENCE = re.compile(r"^(?:```|~~~)\S*$")
"""A line that opens or closes a block of code, which holds no title."""

_BEFORE = re.compile(r"^(?:[#*_`+-]+|\d+[.)])\s+")
"""Markdown before a title: a heading's `#`, or a list's `*`, `-`, `+` or `1.`."""

_AROUND = re.compile(r"^([*_`]+)((?:(?!\1).)+)\1$")
"""Markdown around a title, and nowhere inside it: `**…**`, `_…_`, `` `…` ``."""

_NAMED = re.compile(r"^([*_`]*)title\s*(?::\s*\1|\1\s*:)\s*", re.IGNORECASE)
"""A title that says it is one: `Title: …`, `**Title:** …`, or `**Title**: …`."""


def title_from(parts: list[Part]) -> str | None:
    """A title for a conversation, from its first question: its first line, cut at a
    word when it is long. `None` if the question has no text to take one from."""
    text = "".join(part.text for part in parts if isinstance(part, TextPart)).strip()
    first_line = " ".join(text.split("\n", 1)[0].split())
    if not first_line:
        return None
    return cut(first_line)


def on_its_way(conversation: Conversation, *, now: datetime) -> bool:
    """Whether the conversation's title is on its way: a model was asked for it less
    than `ON_ITS_WAY_FOR` ago, and the conversation is still named after its first
    question. So the list is worth reading again shortly.

    Not before the first answer, nor where no model is named for titles: no model was
    asked. Not once the model's title is written, nor once a person named the
    conversation. And not after `ON_ITS_WAY_FOR`, whatever became of the job: one that
    ended without a title is said to be on its way until then, since nothing records
    that it ended.

    Nor when the model was asked more than that long after `now`, as a clock that was
    set back makes it look: a page would otherwise look again for as long as the clock
    was set back."""
    return (
        conversation.title_by == "question"
        and conversation.title_asked_at is not None
        and abs(now - conversation.title_asked_at) < ON_ITS_WAY_FOR
    )


def cut(title: str) -> str:
    """`title`, cut at a word to about `TITLE_LENGTH` characters when it is longer, with
    `…` to say so. Written here only."""
    if len(title) <= TITLE_LENGTH:
        return title
    kept = title[:TITLE_LENGTH].rsplit(" ", 1)[0] or title[:TITLE_LENGTH]
    return kept.rstrip(" ,;:.") + "…"


def _asking(question: str) -> str:
    """The one message a model is sent for a title: the instructions, then the question
    between two marks, as something to name and not to answer."""
    return f"{INSTRUCTIONS}\n\n<message>\n{question}\n</message>"


def tidy(written: str) -> str | None:
    """A title from what a model wrote for one: its first line that has words, without
    the quotation marks around it, markdown around or before it, a `Title:` before it or
    a full stop after it, with its spaces collapsed, and cut as a first line is (`cut`).
    `None` when nothing is left. Only the first `READ_CHARS` characters are looked at."""
    for line in written[:READ_CHARS].splitlines():
        title = _tidied(line)
        if title is not None:
            return title
    return None


def _tidied(line: str) -> str | None:
    if _FENCE.match(line.strip()):
        return None
    line = _UNPRINTABLE.sub(" ", line)
    before = None
    while line != before:  # until nothing more comes off: they may be nested
        before = line
        line = line.strip()
        line = _BEFORE.sub("", line)
        line = _AROUND.sub(r"\2", line)
        line = _NAMED.sub("", line)
        if len(line) >= 2 and line[0] in _QUOTES and line[-1] in _QUOTES:
            inside = line[1:-1]
            # Marks that open again inside aren't around the title: `"A" and "B"`. An
            # apostrophe inside is no such mark.
            if line[0] in _APOSTROPHES or line[0] not in inside:
                line = inside
        line = line.rstrip(".。")
    title = " ".join(line.split())
    if not any(character.isalnum() for character in title):
        return None
    return cut(title)


async def write(
    open_session: async_sessionmaker[AsyncSession],
    catalog: Catalog,
    conversation_id: uuid.UUID,
    *,
    now: datetime,
) -> None:
    """Ask the model the catalog names for titles to name the conversation, and write
    what it says, unless someone has named the conversation meanwhile. The job
    `WRITE_TITLE` runs this. It reads and writes in transactions of its own, from
    `open_session`, and holds no connection to the database while the model writes.

    It is safe to repeat, and to run beside itself. A conversation that is gone, or no
    longer named by its first question, is left as it is, and the model isn't asked.
    The same is asked again under the lock of the conversation's row before the title is
    written, so that a rename made while the model writes is kept, and of two runs at
    once, the second leaves the first one's title.

    Nothing that goes wrong with the model fails the job: a model that fails, writes
    nothing to make a title of, or takes longer than `WRITTEN_WITHIN_S` leaves the first
    line, and the log says so, with the conversation's id. A cancellation is passed on,
    as ever: it is how a stop cuts the job off, and the queue runs it again.
    """
    if catalog.titles is None:
        logger.warning(
            "conversation %s: no model is named for titles, and it keeps its first line",
            conversation_id,
        )
        return
    question = await _first_question(open_session, conversation_id)
    if question is None:
        return
    written = await _ask(catalog, catalog.get(catalog.titles), question, conversation_id, now=now)
    if written is None:
        return
    title = tidy(written)
    if title is None:
        logger.warning(
            "conversation %s: its model wrote nothing to make a title of, and it keeps its"
            " first line",
            conversation_id,
        )
        return
    async with open_session() as db:
        conversation = await db.scalar(
            select(Conversation)
            .where(Conversation.id == conversation_id)
            .with_for_update()
            .execution_options(populate_existing=True)  # as it is now, not as it was read
        )
        if conversation is None or conversation.title_by != "question":
            return
        # Its last activity stays as it was: the list keeps its order.
        conversation.title = title
        conversation.title_by = "model"
        await db.commit()


async def _first_question(
    open_session: async_sessionmaker[AsyncSession], conversation_id: uuid.UUID
) -> str | None:
    """The text of the conversation's first question, as the branch it shows begins,
    without the space around it, cut to `QUESTION_CHARS`. `None` when there is nothing
    to name: the conversation is gone, someone or something named it already, or its
    first question has no text."""
    async with open_session() as db:
        conversation = await db.get(Conversation, conversation_id)
        if conversation is None or conversation.title_by != "question":
            return None
        branch = await tree.shown_branch(db, conversation)
    if not branch:
        return None
    first = from_stored(branch[0].parts)
    # Before the cut: a question that begins with a page of empty lines has words too.
    text = "".join(part.text for part in first if isinstance(part, TextPart)).strip()
    if not text:
        return None
    return text[:QUESTION_CHARS]


async def _ask(
    catalog: Catalog,
    offered: OfferedModel,
    question: str,
    conversation_id: uuid.UUID,
    *,
    now: datetime,
) -> str | None:
    """What the model wrote for a title: its text, never its reasoning. `None` when it
    isn't ready, failed, or took longer than `WRITTEN_WITHIN_S`, and then the log says
    which.

    The request is shaped as a turn's is, where the two meet: the same context window,
    since a local server reloads its model when that changes, and reasoning off, for a
    model that can reason. A model whose server writes only so many answers at once
    waits at its gate, as an answer does, and gives its place back once its stream is
    closed.

    A model that isn't ready is not asked, as a turn doesn't ask one. What it can do
    isn't known then, and one that reasons unless it is told not to would spend a
    title's few tokens on reasoning."""
    try:
        async with asyncio.timeout(WRITTEN_WITHIN_S):
            status = await catalog.status(offered.name, now=now)
            if status.state != "ready":
                logger.warning(
                    "conversation %s: no title, its model is %s", conversation_id, status.state
                )
                return None
            request = ChatRequest(
                model=offered.model,
                messages=[User(_asking(question))],
                max_output_tokens=TITLE_TOKENS,
                context_window=offered.context_window,
                # As for a turn with thinking off: one that can't reason must not be told.
                thinking=False if status.thinking else None,
            )
            async with (
                offered.gate or nullcontext(),
                aclosing(offered.chat.stream(request)) as events,
            ):
                return await _text_of(events)
    except LLMError as error:
        logger.warning(
            "conversation %s: no title, its model failed (%s)",
            conversation_id,
            type(error).__name__,
        )
    except TimeoutError:
        logger.warning(
            "conversation %s: no title, its model took longer than %g s",
            conversation_id,
            WRITTEN_WITHIN_S,
        )
    return None


async def _text_of(events: AsyncIterator[StreamEvent]) -> str:
    """The text of a model's answer, once it has finished. Its reasoning is no part of
    a title."""
    pieces: list[str] = []
    async for event in events:
        if isinstance(event, TextDelta):
            pieces.append(event.text)
        elif isinstance(event, Finish):
            return "".join(pieces)
    raise StreamInterruptedError("the model's answer ended without saying why")
