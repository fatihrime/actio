"""Following a model's answer as it streams, and making sure the turn gets recorded.

`answer` asks a model and hands on what it writes, as `Step`s: a part of the answer
opens, grows, closes. It returns `Ended`, after the turn was written to the database.
Whatever happens on the way, the turn doesn't stay `running`:

- the model finishes: `completed`, with the whole answer;
- the model fails: `failed`, with a short code for what went wrong, and whatever it
  had written until then;
- the turn is halted (`Halt`): `stopped`, when whoever asked stopped it, or
  `interrupted`, when the process is shutting down. Either way with what had arrived.
  Halting closes the connection to the model, which stops generating.

Nobody has to be listening. A turn runs apart from the request that started it
(`actio.chat.running`), and a reader who leaves ends nothing. So while a turn runs, it
says every few seconds that it is alive, saves what it has written so far, and renews
the hold on its credits: see `_beat`.

A model's server that writes only so many answers at once has a gate in front of it
(`actio.catalog.Gate`). A turn waits there, running like any other, and the model is
asked only once the turn has passed: so the client's patience for the first word starts
when the server is free. The turn gives its place back as soon as the model's stream is
closed, however it ended.

Recording a turn also settles what it cost. Credits were held for it before it started
(`actio.usage.limits`). What it used goes into the usage ledger in the transaction that
records its outcome, and only then are the counters told: see `_record`.

Nothing here knows about HTTP, or about the format a browser reads: `actio.api.turns`
turns the steps into that.
"""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import anyio
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from actio.catalog import Gate, OfferedModel
from actio.chat import turns
from actio.chat.parts import Part, ReasoningPart, TextPart
from actio.chat.turns import Ending
from actio.usage.limits import Reserved, UsageLimits
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
    tokens_in,
)
from actio_quota import UsageWarning

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
    """The turn is over and recorded."""

    ending: Ending
    at: datetime
    """When it was recorded: the turn's `finished_at`, and its answer's `created_at`."""
    credits: int | None = None
    """What the turn cost its account. `None`: nothing, and nothing was written to the
    usage ledger."""
    warnings: tuple[UsageWarning, ...] = ()
    """The shares of a usage window, such as 75 percent of the session, that this turn
    was the first to pass."""


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


class Halt:
    """The way to end one turn before its model has finished.

    Two things end a turn early: whoever asked stops it, or the process that writes it
    is shutting down. Either calls the turn's `Halt`, from any task. The model's stream
    is closed at whatever it was waiting for, and the turn is recorded with what had
    arrived: as `stopped`, or as `interrupted`.
    """

    def __init__(self) -> None:
        self.scope = anyio.CancelScope()
        """What a halt cancels: `answer` waits for the model inside it."""
        self.reason: Literal["stopped", "interrupted"] | None = None
        """Why the turn was halted, once it was."""

    def __call__(self, reason: Literal["stopped", "interrupted"]) -> None:
        """Halt the turn. The first reason given is the one recorded: a turn that was
        stopped stays stopped when the process shuts down a moment later."""
        if self.reason is None:
            self.reason = reason
            self.scope.cancel()


async def answer(
    offered: OfferedModel,
    request: ChatRequest,
    *,
    conversation_id: uuid.UUID,
    turn_id: uuid.UUID,
    answer_id: uuid.UUID,
    open_session: async_sessionmaker[AsyncSession],
    clock: Callable[[], datetime],
    usage: UsageLimits,
    reserved: Reserved,
    emit: Callable[[Step], Awaitable[None]],
    halt: Halt,
    on_beat: Callable[[], Awaitable[None]] | None = None,
) -> Ended:
    """Ask the model, hand its answer to `emit` step by step, and record the turn at
    the end.

    The turn was started before (`turns.ask`), and `reserved` holds the credits it may
    cost. This writes its outcome, and what it used, in a transaction of its own, from
    `open_session`. It returns once that is done: whoever is told that the turn has
    ended can rely on the answer being stored, and counted.

    When the model's server is busy with other answers, the turn first waits at its
    gate, saying all the while that it is alive.

    `halt` ends it early. `on_beat` is called with every beat, after the turn has said
    that it is alive: for whatever else has to be kept while the turn runs. Neither it
    nor `emit` may raise.
    """
    collected = Answer()
    ending: Ending | None = None
    beats = asyncio.create_task(
        _beat(open_session, turn_id, answer_id, collected, clock, usage, reserved, on_beat)
    )
    try:
        # Only waiting, for the gate or for the model, can be halted. A step that has
        # come is handed on whole, so what readers were shown is what gets stored.
        with halt.scope:
            # The model is asked only once the turn has passed the gate: the second
            # expression is evaluated after the first was entered. The place at the gate
            # is given back as soon as the model's stream is closed.
            async with (
                _through(offered.gate, turn_id, clock),
                aclosing(offered.chat.stream(request)) as events,
            ):
                async for event in events:
                    for step in collected.take(event):
                        with anyio.CancelScope(shield=True):
                            await emit(step)
        if collected.finish is not None:
            ending = Ending(
                "completed",
                collected.parts,
                finish_reason=collected.finish.reason.value,
                usage=collected.finish.usage,
                timings=collected.finish.timings,
            )
        elif halt.reason is not None:
            ending = Ending(halt.reason, collected.parts)
            for step in collected.close():
                await emit(step)
        else:
            raise StreamInterruptedError("the model's answer ended without saying why")
    except LLMError as error:
        logger.warning("turn %s: the model failed (%s: %s)", turn_id, type(error).__name__, error)
        ending = Ending("failed", collected.parts, error=code_for(error))
        for step in collected.close():
            await emit(step)
    except Exception:
        ending = Ending("failed", collected.parts, error="internal_error")
        raise
    except BaseException:
        # Cancelled from outside, without a `Halt`: the process is going down faster
        # than it could say so.
        ending = Ending("interrupted", collected.parts)
        raise
    finally:
        beats.cancel()
        if ending is not None:
            # Shielded: if this task is being cancelled, an unshielded write would be
            # cancelled with it, leaving the turn `running`.
            finished = clock()
            with anyio.CancelScope(shield=True):
                await asyncio.wait({beats})  # so that no beat is written after the outcome
                credits, warnings = await _record(
                    open_session,
                    conversation_id,
                    turn_id,
                    ending,
                    answer_id,
                    finished,
                    usage,
                    reserved,
                )
    return Ended(ending, finished, credits, warnings)


@asynccontextmanager
async def _through(
    gate: Gate | None, turn_id: uuid.UUID, clock: Callable[[], datetime]
) -> AsyncIterator[None]:
    """Wait until the model's server has room for the answer, and keep its place there
    until the end of the block. Without a gate, the server is asked at once.

    A turn that had to wait says so in the log, with how long: whether it got its turn,
    or was stopped, or ended by a shutdown, while it waited."""
    if gate is None:
        yield
        return
    came = clock() if gate.full else None
    passed = False
    try:
        async with gate:
            passed = True
            if came is not None:
                _waited(turn_id, came, clock)
            yield
    finally:
        if came is not None and not passed:
            _waited(turn_id, came, clock, ", and didn't get its turn")


def _waited(
    turn_id: uuid.UUID, came: datetime, clock: Callable[[], datetime], how: str = ""
) -> None:
    # A warning, as the other lines of this module are: only warnings and errors reach
    # the log as the web server sets it up. Answers that wait are worth seeing there.
    waited = (clock() - came).total_seconds()
    logger.warning("turn %s: waited %.1f s for the model's server%s", turn_id, waited, how)


async def _beat(
    open_session: async_sessionmaker[AsyncSession],
    turn_id: uuid.UUID,
    answer_id: uuid.UUID,
    collected: Answer,
    clock: Callable[[], datetime],
    usage: UsageLimits,
    reserved: Reserved,
    on_beat: Callable[[], Awaitable[None]] | None,
) -> None:
    """Say every `turns.BEAT` that the turn is alive, for as long as it runs.

    Three things would otherwise run out under a long answer, each on purpose, because
    each must not outlast a process that died:

    - the turn itself, which is given up on when it has been silent (`turns.STALE_AFTER`);
    - the hold on its credits (`UsageLimits.extend`);
    - whatever `on_beat` keeps: what the turn streams.

    With the same statement that says it is alive, the turn saves what `collected` holds
    by then, as its draft, under `answer_id`: what a process that dies leaves as its
    answer. Nothing while the model has written nothing.

    A beat that fails is tried again at the next: one or two may be missed.
    """
    while True:
        await asyncio.sleep(turns.BEAT.total_seconds())
        now = clock()
        written = collected.parts
        draft = turns.Draft(answer_id=answer_id, parts=written) if written else None
        try:
            async with open_session() as db:
                await turns.still_alive(db, turn_id, now=now, draft=draft)
                await db.commit()
        except (SQLAlchemyError, OSError) as error:
            logger.warning(
                "turn %s: couldn't say that it is alive (%s)", turn_id, type(error).__name__
            )
        await usage.extend(reserved, hold_for=turns.STALE_AFTER, now=now)
        if on_beat is not None:
            await on_beat()


def _costs_nothing(ending: Ending) -> bool:
    """Whether a turn that ended this way is free whatever it wrote: the fault was the
    model's, or Actio's own."""
    return ending.status in ("failed", "interrupted")


@dataclass(frozen=True, slots=True)
class _Used:
    """The tokens to count for a turn."""

    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    estimated: bool
    """Whether they are Actio's guess, because the provider reported none."""


def _used(ending: Ending, reserved: Reserved) -> _Used | None:
    """What to count for a turn that ended this way. `None`: nothing.

    - **A failed turn costs nothing,** and neither does one that a shutdown
      interrupted. The fault was the model's, or Actio's own.
    - **A provider's count is taken as it is.**
    - **Without one, what was written is counted by guess,** with the guess at the
      prompt that was made before the turn started. That is every stopped turn: a
      stream that is closed never sends its count. So stopping an answer at its last
      word doesn't make it free.
    - **A turn that wrote nothing, and has no count, costs nothing.** Nobody can say
      whether the model had even begun to read.
    """
    if _costs_nothing(ending):
        return None
    if ending.usage is not None:
        counted = ending.usage
        return _Used(
            counted.input_tokens, counted.cached_input_tokens, counted.output_tokens, False
        )
    written = sum(tokens_in(part.text) for part in ending.parts)
    if written == 0:
        return None
    return _Used(reserved.prompt_tokens, 0, written, estimated=True)


async def _record(
    open_session: async_sessionmaker[AsyncSession],
    conversation_id: uuid.UUID,
    turn_id: uuid.UUID,
    ending: Ending,
    answer_id: uuid.UUID,
    now: datetime,
    usage: UsageLimits,
    reserved: Reserved,
) -> tuple[int | None, tuple[UsageWarning, ...]]:
    """Write how the turn ended, and what it used, in one transaction. Then tell the
    counters, and once they were told, note that they were, in a transaction of its
    own. Returns what it cost, and the warnings the counters gave.

    In that order, because the counters can be lost and are then rebuilt from the
    ledger: what they have counted must be in the ledger already. What they weren't
    told, the worker tells them later: the ledger's row is still noted as not counted.
    """
    used = _used(ending, reserved)
    credits = None
    try:
        async with open_session() as db:
            if used is not None:
                credits = usage.record(
                    db,
                    reserved,
                    input_tokens=used.input_tokens,
                    cached_input_tokens=used.cached_input_tokens,
                    output_tokens=used.output_tokens,
                    estimated=used.estimated,
                    now=now,
                )
            recorded = await turns.finish(
                db, conversation_id, turn_id, ending, answer_id=answer_id, now=now, credits=credits
            )
            if recorded == "left":
                # The turn was given up on while it was still being written, and its
                # answer isn't kept. An answer nobody gets isn't paid for either.
                logger.warning("turn %s: it ended after it had been given up on", turn_id)
                await db.rollback()
                credits = None
            else:
                await db.commit()
    except (SQLAlchemyError, OSError) as error:
        # The turn stays `running` and is given up on later, as `interrupted`. Nothing
        # of it was written, so nothing is counted for it either.
        logger.error(
            "turn %s: its outcome couldn't be recorded (%s)", turn_id, type(error).__name__
        )
        await usage.release(reserved, now=now)
        raise
    if credits is None:
        await usage.release(reserved, now=now)
        return None, ()
    warnings = await usage.settle(reserved, credits, now=now)
    if warnings is None:
        return credits, ()  # the counters weren't told: `settle` said so in the log
    await _counted(open_session, turn_id, usage, reserved)
    return credits, warnings


async def _counted(
    open_session: async_sessionmaker[AsyncSession],
    turn_id: uuid.UUID,
    usage: UsageLimits,
    reserved: Reserved,
) -> None:
    """Note that the counters were told of what the turn used.

    A note that can't be written fails nothing: the turn is recorded, and counted. The
    worker finds its ledger row still noted as not counted, settles it again, is told
    that it was settled already, and takes the note away."""
    try:
        async with open_session() as db:
            await usage.counted(db, reserved)
            await db.commit()
    except (SQLAlchemyError, OSError) as error:
        logger.warning(
            "turn %s: couldn't note that its usage was counted (%s)",
            turn_id,
            type(error).__name__,
        )
