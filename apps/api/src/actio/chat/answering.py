"""Following a model's answer as it streams, and making sure the turn gets recorded.

`answer` asks a model and hands on what it writes, as `Step`s: a part of the answer
opens, grows, closes. It returns `Ended`, after the turn was written to the database,
or was found given up on or deleted meanwhile (`Ended.recorded`). Whatever happens on
the way, the turn doesn't stay `running`:

- the model finishes: `completed`, with the whole answer;
- the model is still writing when its server's time for a whole answer is over
  (`actio_llm.TookTooLongError`): `completed` too, with what it had written, and the
  reason `time_limit`, counted by guess. With nothing written, the models that stand in
  were asked already, and the turn is `failed`, as `model_too_slow`;
- the model fails: `failed`, with a short code for what went wrong, and whatever it
  had written until then;
- the turn is halted (`Halt`): `stopped`, when whoever asked stopped it, or
  `interrupted`, when the process is shutting down. Either way with what had arrived.
  Halting closes the connection to the model, which stops generating.

However it ends, a turn that got some of the model's answer says when the first piece
came, by the clock it is given: what the person waited for the first word, from the
moment they asked, the wait at the gate below included (`Ending.first_word_at`). And it
says which model wrote that answer (`Ending.answered_by`).

Nobody has to be listening. A turn runs apart from the request that started it
(`actio.chat.running`), and a reader who leaves ends nothing. So while a turn runs, it
says every few seconds that it is alive, saves what it has written so far, and renews
the hold on its credits: see `_beat`.

A model's server that writes only so many answers at once has a gate in front of it
(`actio.catalog.Gate`). A turn waits there, running like any other, and the model is
asked only once the turn has passed: so the client's patience for the first word starts
when the server is free. The turn gives its place back as soon as the model's stream is
closed, however it ended.

A turn asks the model it names, and when that one can't answer, the models that stand in
for it, one after the other, each behind its own gate (`actio.catalog.Catalog.chain`):
but only before the first word, or thought, was handed on (`actio_llm.Walk`, ADR 0028).
A model whose failure may be gone in a moment, a connection that failed at once or a
server that asked to wait two seconds at most, is asked once more first, keeping its
place at its gate (ADR 0028, revision of 2026-11-25). A model that failed gives its
place at its gate back before the next one's is waited at. Whoever answers, the turn is
the asked model's: its name, and the credits held at its price. Its record names the
model that answered all the same, whichever ending it is recorded with (ADR 0028,
revision "a turn says which model answered, and the page says so"). The log says which
model was asked once more, which failed and was passed over, and which answered in
another's place, never what an error said.

Recording a turn also settles what it cost. Credits were held for it before it started
(`actio.usage.limits`). What it used goes into the usage ledger in the transaction that
records its outcome, and only then are the counters told: see `_record`.

Nothing here knows about HTTP, or about the format a browser reads: `actio.api.turns`
turns the steps into that.
"""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from functools import partial
from typing import Literal

import anyio
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from actio.catalog import Gate, OfferedModel
from actio.chat import turns
from actio.chat.parts import Part, ReasoningPart, TextPart
from actio.chat.turns import Ending
from actio.core.text import storable
from actio.usage.limits import Reserved, UsageLimits, can_be_recorded
from actio_llm import (
    ChatRequest,
    ContextOverflowError,
    Finish,
    FinishReason,
    Link,
    LLMError,
    ModelNotFoundError,
    ProviderError,
    ProviderUnavailableError,
    ReasoningDelta,
    RequestRejectedError,
    StreamEvent,
    StreamInterruptedError,
    TextDelta,
    Timings,
    TookTooLongError,
    Usage,
    Walk,
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
    "model_too_slow": "The model took too long, and wrote no answer.",
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
        case TookTooLongError():
            return "model_too_slow"
        case StreamInterruptedError():
            return "answer_interrupted"
        case _:
            return "model_error"


def reason_for(finish: Finish, usage: Usage | None, *, context_window: int | None) -> str:
    """The reason a completed turn is recorded with, from the model's last event, the
    counts the turn keeps (`usage`) and the window its request went out with.

    A model's server says `length` for two limits: the one on a single answer's size,
    and the end of the context window. Only the counts tell them apart. At its own
    limit an answer has written what it was allowed, far from the window's end; at the
    window's end what was read and written together is the window. So `length` with
    the two counts together at the window, or past it as a server that counts oddly
    may say, is `context_window`. Otherwise the model's own reason, `length` too when
    the server counted nothing. See ADR 0014, revision "a turn says which limit its
    answer reached"."""
    reason = finish.reason
    if (
        reason is FinishReason.LENGTH
        and usage is not None
        and context_window is not None
        and usage.input_tokens + usage.output_tokens >= context_window
    ):
        return "context_window"
    return reason.value


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
    """The turn is over, and its outcome was written, or found to have no place."""

    ending: Ending
    """How the turn ended, as its process saw it."""
    at: datetime
    """When it was recorded: the turn's `finished_at`, and its answer's `created_at`."""
    recorded: turns.Recorded
    """What became of the outcome (`turns.finish`). `recorded`: `ending` is the turn's
    record. `left`: the turn had been given up on meanwhile, and its record is what
    giving up wrote, not `ending`. `gone`: its conversation was deleted meanwhile, and
    nothing of it is left to record anything in."""
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
        # Before a reader is shown it: what is streamed is what is stored. A character
        # that can't be stored would keep the whole answer from being recorded.
        text = storable(text)
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


@dataclass(frozen=True, slots=True)
class Asking:
    """A model a turn asks, and the request it is asked with: with the model's own name
    on its server, window and limits."""

    offered: OfferedModel
    request: ChatRequest


async def answer(
    chain: Sequence[Asking],
    *,
    model: str,
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
    ask_for_title: bool = False,
) -> Ended:
    """Ask the models of `chain`, in order, until one answers, hand its answer to `emit`
    step by step, and record the turn at the end.

    `chain` holds the models that can be asked now: the one the turn names, `model`,
    when it is ready, and those that stand in for it. A later one is asked only when the
    one before it failed before anything of its answer was handed on, and was asked once
    more first when its failure could be gone in a moment. When every one of them
    failed, the turn records the error `actio_llm.Walk` raises.

    The turn was started before (`turns.ask`), and `reserved` holds the credits it may
    cost. This writes its outcome, and what it used, in a transaction of its own, from
    `open_session`. It returns once that is done: whoever is told that the turn has
    ended can rely on the answer being stored, and counted. `ask_for_title`, that a
    model is named for titles, goes to `turns.finish` with it.

    When a model's server is busy with other answers, the turn first waits at its gate,
    saying all the while that it is alive.

    `halt` ends it early. `on_beat` is called with every beat, after the turn has said
    that it is alive: for whatever else has to be kept while the turn runs. Neither it
    nor `emit` may raise.
    """
    collected = Answer()
    ending: Ending | None = None
    # When the first piece of the answer was taken: noted once, before it is handed on.
    first_word: datetime | None = None
    # Each model is asked only once the turn has passed its gate, and gives its place
    # back as soon as its last try's stream is closed: before the next one's gate is
    # waited at.
    walk = Walk(
        [
            Link(
                asking.offered.name,
                asking.offered.chat,
                asking.request,
                _through(asking.offered.gate, turn_id, clock),
            )
            for asking in chain
        ],
        on_failed=partial(_passed_over, turn_id, several=len(chain) > 1),
        on_again=partial(_asked_again, turn_id),
    )
    beats = asyncio.create_task(
        _beat(open_session, turn_id, answer_id, collected, clock, usage, reserved, on_beat)
    )
    try:
        # Only waiting, for a gate or for a model, can be halted. A step that has come
        # is handed on whole, so what readers were shown is what gets stored.
        with halt.scope:
            async with aclosing(walk.stream()) as events:
                async for event in events:
                    steps = collected.take(event)
                    if first_word is None and any(isinstance(step, Wrote) for step in steps):
                        first_word = clock()
                    for step in steps:
                        with anyio.CancelScope(shield=True):
                            await emit(step)
        if collected.finish is not None:
            kept = _believed(collected.finish.usage, collected.parts, reserved, turn_id)
            # The window of the request that was answered: a stand-in's is its own.
            answered = walk.answered
            ending = Ending(
                "completed",
                collected.parts,
                finish_reason=reason_for(
                    collected.finish,
                    kept,
                    context_window=None if answered is None else answered.request.context_window,
                ),
                usage=kept,
                timings=_kept(collected.finish.timings, turn_id),
                first_word_at=first_word,
            )
        elif halt.reason is not None:
            ending = Ending(halt.reason, collected.parts, first_word_at=first_word)
            for step in collected.close():
                await emit(step)
        else:
            raise StreamInterruptedError("the model's answer ended without saying why")
    except LLMError as error:
        if isinstance(error, TookTooLongError) and collected.parts:
            # Its model was still writing when its server's time for a whole answer was
            # over: an answer that reached a limit, as one at its longest does, and is
            # paid for, by guess, since a stream that is closed sends no count. Never a
            # failure, which costs nothing: anyone can ask for a long answer on purpose.
            logger.warning(
                "turn %s: model %s was still writing when its server's time for an answer "
                "was over, and the answer stops here",
                turn_id,
                model if walk.answered is None else walk.answered.name,
            )
            ending = Ending(
                "completed", collected.parts, finish_reason="time_limit", first_word_at=first_word
            )
        else:
            # Its kind, never its text: that can quote what the model had written, or
            # what its server said of the request (ADR 0008, revision of 2026-11-23).
            failed_as = code_for(error)
            logger.warning(
                "turn %s: the model failed (%s), recorded as %s",
                turn_id,
                type(error).__name__,
                failed_as,
            )
            ending = Ending("failed", collected.parts, error=failed_as, first_word_at=first_word)
        for step in collected.close():
            await emit(step)
    except Exception:
        ending = Ending("failed", collected.parts, error="internal_error", first_word_at=first_word)
        raise
    except BaseException:
        # Cancelled from outside, without a `Halt`: the process is going down faster
        # than it could say so.
        ending = Ending("interrupted", collected.parts, first_word_at=first_word)
        raise
    finally:
        beats.cancel()
        if walk.answered is not None and walk.answered.name != model:
            logger.warning(
                "turn %s: model %s answered in place of %s", turn_id, walk.answered.name, model
            )
        if ending is not None:
            # Whatever ended it, its record names the model whose answer it holds: the
            # one that handed something on, if one did.
            ending = replace(
                ending, answered_by=None if walk.answered is None else walk.answered.name
            )
            # Shielded: if this task is being cancelled, an unshielded write would be
            # cancelled with it, leaving the turn `running`.
            finished = clock()
            with anyio.CancelScope(shield=True):
                await asyncio.wait({beats})  # so that no beat is written after the outcome
                recorded, credits, warnings = await _record(
                    open_session,
                    conversation_id,
                    turn_id,
                    ending,
                    answer_id,
                    finished,
                    usage,
                    reserved,
                    ask_for_title=ask_for_title,
                )
    return Ended(ending, finished, recorded, credits, warnings)


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


def _passed_over(
    turn_id: uuid.UUID, failed: Link, error: LLMError, later: Link | None, *, several: bool
) -> None:
    """Say in the log that a model failed before the first word, and which is asked
    next. Its error's kind, never its text: that can quote the question, or name the
    account at its provider (ADR 0027). A turn of one model says nothing here: the line
    that records its failure says it."""
    kind = type(error).__name__
    if later is not None:
        logger.warning(
            "turn %s: model %s failed (%s), and %s is asked next",
            turn_id,
            failed.name,
            kind,
            later.name,
        )
    elif several:
        logger.warning(
            "turn %s: model %s failed (%s), and no other is left to ask", turn_id, failed.name, kind
        )


def _asked_again(turn_id: uuid.UUID, failed: Link, error: LLMError, pause: float) -> None:
    """Say in the log that a model failed before the first word, and is asked once more,
    after how long. Its error's kind, never its text, as for `_passed_over`. Said for a
    turn of one model too: no other line says it."""
    kind = type(error).__name__
    if pause:
        logger.warning(
            "turn %s: model %s failed (%s), and is asked once more after %g s",
            turn_id,
            failed.name,
            kind,
            pause,
        )
    else:
        logger.warning(
            "turn %s: model %s failed (%s), and is asked once more", turn_id, failed.name, kind
        )


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


def _believed(
    reported: Usage | None, written: list[Part], reserved: Reserved, turn_id: uuid.UUID
) -> Usage | None:
    """What the model's server counted for a turn, unless that can't be right. Then
    nothing, as if the server had counted nothing: the turn keeps no count, and is
    counted by guess (`_used`). The log says what the server reported.

    A count can't be right when it can't be recorded at all, as a negative one can't,
    or one beyond what a row holds (`can_be_recorded`); or when it says that nothing was
    written, beside an answer that has words: one that Actio's own guess counts tokens
    in. Such counts are someone else's mistake, and must not keep an answer that was
    written from being recorded. Whether a count that passes is true, nothing here can
    tell: it is taken as it is.
    """
    if reported is None:
        return None
    possible = can_be_recorded(
        reserved.price,
        input_tokens=reported.input_tokens,
        output_tokens=reported.output_tokens,
        cached_input_tokens=reported.cached_input_tokens,
    )
    # By the guess that would count the turn instead: an answer of nothing but line
    # breaks has no tokens by it, and would then cost nothing at all.
    has_words = any(tokens_in(part.text) for part in written)
    if possible and (reported.output_tokens > 0 or not has_words):
        return reported
    logger.warning(
        "turn %s: its model's server reported counts that can't be right (read %d, %d of "
        "that from its cache, wrote %d); it is counted by guess",
        turn_id,
        reported.input_tokens,
        reported.cached_input_tokens,
        reported.output_tokens,
    )
    return None


_LONGEST_MS = 2**31 - 1
"""The longest time that a turn's record holds, in milliseconds: its columns are 32-bit
integers. Some 24 days."""


def _kept(reported: Timings | None, turn_id: uuid.UUID) -> Timings | None:
    """What the model's server said of where the time went, unless a turn's record can't
    hold it: a negative time, or one longer than `_LONGEST_MS`. Then nothing, with a
    line in the log. Like a count that can't be right, it must not keep the answer from
    being recorded."""
    if reported is None:
        return None
    times = (reported.load_ms, reported.prompt_ms, reported.generation_ms, reported.total_ms)
    if all(0 <= time <= _LONGEST_MS for time in times):
        return reported
    logger.warning(
        "turn %s: its model's server reported times that can't be right (%d ms to load, %d to "
        "read, %d to write, %d in all); none is kept",
        turn_id,
        *times,
    )
    return None


def _used(ending: Ending, reserved: Reserved) -> _Used | None:
    """What to count for a turn that ended this way. `None`: nothing.

    - **A failed turn costs nothing,** and neither does one that a shutdown
      interrupted. The fault was the model's, or Actio's own.
    - **A provider's count is taken as it is.** One that couldn't be right was dropped
      when the turn ended (`_believed`), and never gets here.
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
    *,
    ask_for_title: bool = False,
) -> tuple[turns.Recorded, int | None, tuple[UsageWarning, ...]]:
    """Write how the turn ended, and what it used, in one transaction, with the job that
    asks for the conversation's title when it is due (`turns.finish`). Then tell the
    counters, and once they were told, note that they were, in a transaction of its
    own. Returns what became of the outcome, as `turns.finish` says it, what the turn
    cost, and the warnings the counters gave.

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
                db,
                conversation_id,
                turn_id,
                ending,
                answer_id=answer_id,
                now=now,
                credits=credits,
                ask_for_title=ask_for_title,
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
        return recorded, None, ()
    warnings = await usage.settle(reserved, credits, now=now)
    if warnings is None:
        return recorded, credits, ()  # the counters weren't told: `settle` said so in the log
    await _counted(open_session, turn_id, usage, reserved)
    return recorded, credits, warnings


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
