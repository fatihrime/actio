"""Following a model's answer as it streams, and making sure the turn gets recorded.

`answer` asks a model and yields what it writes, as `Step`s: a part of the answer
opens, grows, closes. Its last item is `Ended`, after the turn was written to the
database. Whatever happens on the way, the turn doesn't stay `running`:

- the model finishes: `completed`, with the whole answer;
- the model fails: `failed`, with a short code for what went wrong, and whatever it
  had written until then;
- whoever was listening leaves: `stopped`, with what had arrived. Closing the stream
  also closes the connection to the model, which stops generating.

Nothing here knows about HTTP, or about the format a browser reads: `actio.api.turns`
turns the steps into that.
"""

import logging
import uuid
from collections.abc import AsyncGenerator, Callable
from contextlib import aclosing
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import anyio
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from actio.catalog import OfferedModel
from actio.chat import turns
from actio.chat.parts import Part, ReasoningPart, TextPart
from actio.chat.turns import Ending
from actio_llm import (
    ChatRequest,
    ContextOverflowError,
    Finish,
    LLMError,
    ModelNotFoundError,
    ProviderError,
    ProviderUnavailableError,
    ReasoningDelta,
    RequestRejectedError,
    StreamEvent,
    StreamInterruptedError,
    TextDelta,
)

logger = logging.getLogger(__name__)

type Kind = Literal["reasoning", "text"]

EXPLANATIONS = {
    "model_unavailable": "The model's server can't be reached, or is too busy. Try again in a moment.",
    "model_missing": "The model isn't on its server.",
    "context_overflow": (
        "This conversation has become too long for the model. "
        "Start a new one, or ask again in fewer words."
    ),
    "model_refused": "The model's server refused the request.",
    "answer_interrupted": "The answer broke off: the connection to the model was lost.",
    "model_error": "The model's server failed.",
    "internal_error": "Something went wrong while the answer was being written.",
}
"""What went wrong in a failed turn: the code that is stored, and what to tell a person."""


def code_for(error: LLMError) -> str:
    """The code a model call's failure is stored under."""
    match error:
        case ContextOverflowError():
            return "context_overflow"
        case ModelNotFoundError():
            return "model_missing"
        case RequestRejectedError():
            return "model_refused"
        case ProviderUnavailableError():
            return "model_unavailable"
        case StreamInterruptedError():
            return "answer_interrupted"
        case _:
            return "model_error"


# --- What an answer does, step by step ------------------------------------------------


@dataclass(frozen=True, slots=True)
class Opened:
    """A new part of the answer begins: reasoning, or text."""

    kind: Kind
    number: int
    """The part's place in the answer, from 0."""


@dataclass(frozen=True, slots=True)
class Wrote:
    """The next piece of the part that is open."""

    kind: Kind
    number: int
    text: str


@dataclass(frozen=True, slots=True)
class Closed:
    """The open part is complete."""

    kind: Kind
    number: int


@dataclass(frozen=True, slots=True)
class Ended:
    """The turn is over and recorded. Always the last item, when there is one: someone
    who stopped listening gets none."""

    ending: Ending
    at: datetime
    """When it was recorded: the turn's `finished_at`, and its answer's `created_at`."""


type Step = Opened | Wrote | Closed


class Answer:
    """Collects a model's answer while it streams, and says what each event adds.

    Pieces of the same kind that follow each other are one part. When the kind changes,
    the open part closes and a new one opens: reasoning first, then the text, as most
    models write; or back and forth, as some do.
    """

    def __init__(self) -> None:
        self._parts: list[tuple[Kind, list[str]]] = []
        self._open = False
        self.finish: Finish | None = None
        """The model's last event, once it has come."""

    def take(self, event: StreamEvent) -> list[Step]:
        """Add one event from the model, and return the steps it amounts to."""
        match event:
            case TextDelta(text=text):
                return self._write("text", text)
            case ReasoningDelta(text=text):
                return self._write("reasoning", text)
            case Finish():
                self.finish = event
                return self.close()
            case _:
                raise ProviderError("the model asked for a tool, and none was offered to it")

    def close(self) -> list[Step]:
        """Close the open part, if there is one."""
        if not self._open:
            return []
        self._open = False
        return [Closed(self._parts[-1][0], len(self._parts) - 1)]

    @property
    def parts(self) -> list[Part]:
        """What has been written so far, as the parts of a message."""
        made: list[Part] = []
        for kind, pieces in self._parts:
            text = "".join(pieces)
            made.append(TextPart(text=text) if kind == "text" else ReasoningPart(text=text))
        return made

    def _write(self, kind: Kind, text: str) -> list[Step]:
        if not text:
            return []
        steps: list[Step] = []
        if not self._open or self._parts[-1][0] != kind:
            steps += self.close()
            self._parts.append((kind, []))
            self._open = True
            steps.append(Opened(kind, len(self._parts) - 1))
        self._parts[-1][1].append(text)
        steps.append(Wrote(kind, len(self._parts) - 1, text))
        return steps


# --- Running a turn -------------------------------------------------------------------


async def answer(
    offered: OfferedModel,
    request: ChatRequest,
    *,
    conversation_id: uuid.UUID,
    turn_id: uuid.UUID,
    answer_id: uuid.UUID,
    open_session: async_sessionmaker[AsyncSession],
    clock: Callable[[], datetime],
) -> AsyncGenerator[Step | Ended]:
    """Ask the model, yield its answer step by step, and record the turn at the end.

    The turn was started before (`turns.ask`). This writes its outcome in a transaction
    of its own, from `open_session`, and yields `Ended` once that is done: whoever
    reads that far can rely on the answer being stored.
    """
    collected = Answer()
    ending: Ending | None = None
    try:
        async with aclosing(offered.chat.stream(request)) as events:
            async for event in events:
                for step in collected.take(event):
                    yield step
        if collected.finish is None:
            raise StreamInterruptedError("the model's answer ended without saying why")
        ending = Ending(
            "completed",
            collected.parts,
            finish_reason=collected.finish.reason.value,
            usage=collected.finish.usage,
            timings=collected.finish.timings,
        )
    except LLMError as error:
        logger.warning("turn %s: the model failed (%s: %s)", turn_id, type(error).__name__, error)
        for step in collected.close():
            yield step
        ending = Ending("failed", collected.parts, error=code_for(error))
    except Exception:
        ending = Ending("failed", collected.parts, error="internal_error")
        raise
    except BaseException:
        # Cancelled, or closed from outside: whoever was listening has left.
        ending = Ending("stopped", collected.parts)
        raise
    finally:
        if ending is not None:
            # Shielded: when the listener left, this task is being cancelled, and an
            # unshielded write would be cancelled with it, leaving the turn `running`.
            finished = clock()
            with anyio.CancelScope(shield=True):
                await _record(open_session, conversation_id, turn_id, ending, answer_id, finished)
    yield Ended(ending, finished)


async def _record(
    open_session: async_sessionmaker[AsyncSession],
    conversation_id: uuid.UUID,
    turn_id: uuid.UUID,
    ending: Ending,
    answer_id: uuid.UUID,
    now: datetime,
) -> None:
    try:
        async with open_session() as db:
            await turns.finish(db, conversation_id, turn_id, ending, answer_id=answer_id, now=now)
            await db.commit()
    except (SQLAlchemyError, OSError) as error:
        # The turn stays `running` and is given up on later, as `interrupted`.
        logger.error(
            "turn %s: its outcome couldn't be recorded (%s)", turn_id, type(error).__name__
        )
        raise
