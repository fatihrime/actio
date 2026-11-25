"""The routes that put a question to a model, and let its answer be read and stopped.

`POST /api/conversations/{id}/turns` does one of two things:

- with `message`: adds a new question to the conversation, and answers it. The first
  question creates the conversation, under the id in the path. Rewording an earlier
  question is the same request, with the `parent_id` of the message it should follow;
- with `answer_to`: answers a question that is already there once more.

What goes wrong before a model is asked is an ordinary error: 404, 409, 422, 429, 503.
Once the turn has started the answer is `200`, and everything else, the model's own
failures included, comes in the stream (`actio.api.ui_stream`).

The turn runs apart from that request (`actio.chat.running`). The request only reads
along: what the turn writes goes to a stream of events that is kept while it runs
(`actio.chat.events`), and the response follows that stream. Whoever leaves ends
nothing, and can come back:

- `GET /api/conversations/{id}/turns/{turn_id}/stream` reads along again, from the
  answer's first event;
- `POST /api/conversations/{id}/turns/{turn_id}/cancel` stops the turn.

A turn costs credits, and an account has only so many per session and per week
(`actio.usage`). Before a model is asked, the most the turn can cost is held. A turn
that doesn't fit is answered 429, with the moment the limit starts over, and nothing of
it is stored: the question was never asked.

An account's plan may also say how many answers it may have being written at once. A
turn that would be one more is answered 429 as well, before anything is kept or held.
And an account has only so many readers open (`actio.chat.readers`): joining a turn
beyond them is answered 429. The reader of the request that starts a turn is never
refused, and counts.

The conversation's and the question's ids are chosen by whoever sends them, as a
browser does before it has heard from the server. The question's id also makes a
repeated request harmless: the same message is never asked twice.
"""

import logging
import math
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import aclosing, suppress
from datetime import datetime, timedelta
from typing import Any, Self

from fastapi import APIRouter, Response
from fastapi.responses import StreamingResponse
from pydantic import ConfigDict, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.types import Receive, Scope, Send

from actio.api import ui_stream
from actio.api.body import Body
from actio.api.catalog import not_ready
from actio.api.conversations import MessageMetadata, OwnConversation, TurnOut
from actio.api.dependencies import (
    NO_SESSION,
    AppResources,
    Clock,
    CurrentPrincipal,
    Database,
    Events,
    Models,
    NewId,
    Now,
    Readers,
    Runner,
    Usage,
)
from actio.api.described import Described
from actio.api.problems import Problem
from actio.api.rate_limits import Limits, in_words
from actio.api.usage import UNAVAILABLE
from actio.catalog import OfferedModel, UnknownModelError
from actio.chat import answering, context, tree, turns
from actio.chat.answering import Ended, Halt
from actio.chat.events import EventsLostError, EventsUnavailableError, TurnEvents
from actio.chat.parts import TextPart
from actio.chat.readers import Reader, TooManyReadersError
from actio.chat.turns import Begun
from actio.core.budget import Budgets, BudgetsUnavailableError
from actio.identity.sessions import Principal
from actio.usage.limits import (
    Account,
    LimitReachedError,
    Reserved,
    UsageLimits,
    UsageUnavailableError,
)
from actio_llm import ChatRequest, prompt_tokens

logger = logging.getLogger(__name__)
router = APIRouter(tags=["conversations"], responses=NO_SESSION)

MAX_QUESTION_CHARS = 100_000
"""The longest question accepted, in characters. Far more than a small model can read;
it is a bound on what gets stored, not a promise that a model will take it."""

KEEP_ALIVE_AFTER = timedelta(seconds=15)
"""How long a reader hears nothing before it is sent a line that says the connection is
alive. A model that is being loaded can be silent for a minute, and a proxy on the way
may close a connection that is."""

CANT_BE_READ = "Answers can't be written right now. Try again in a moment."
"""For the 503 when Valkey doesn't take what a turn streams: an answer nobody could
read along with isn't started."""

NO_SUCH_TURN: dict[int | str, dict[str, Any]] = {
    404: {"description": "No such conversation, or no such turn in it."}
}


# A part of a turn's request, not a body itself: `TurnRequest` is the `Body` that is
# looked through, and this with it.
class NewMessage(Described):
    """A question, as its sender made it."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    """Chosen by the sender. Sending the same message again doesn't ask twice."""
    parts: list[TextPart] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def _says_something(self) -> Self:
        length = sum(len(part.text) for part in self.parts)
        if not any(part.text.strip() for part in self.parts):
            raise ValueError("a message needs some text")
        if length > MAX_QUESTION_CHARS:
            raise ValueError(f"a message has at most {MAX_QUESTION_CHARS} characters")
        return self


class TurnRequest(Body):
    model_config = ConfigDict(extra="forbid")

    message: NewMessage | None = None
    """A new question to add and answer."""
    parent_id: uuid.UUID | None = None
    """With `message`: the answer it follows. Left out for a first question."""
    answer_to: uuid.UUID | None = None
    """Instead of `message`: a question that is already there, to answer again."""
    model: str | None = Field(default=None, max_length=32)
    """A model's `id` from `GET /api/models`. Left out: the default one."""
    thinking: bool = False
    """Ask the model to reason before it answers, if it can."""

    @model_validator(mode="after")
    def _asks_or_answers_again(self) -> Self:
        if (self.message is None) == (self.answer_to is None):
            raise ValueError(
                "send `message`, to ask something, or `answer_to`, to have a question "
                "answered again: one of the two"
            )
        if self.answer_to is not None and self.parent_id is not None:
            raise ValueError("`parent_id` goes with `message`")
        return self


@router.post(
    "/api/conversations/{conversation_id}/turns",
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "The answer, as the AI SDK's UI message stream, version 1.",
            "content": {ui_stream.MEDIA_TYPE: {}},
        },
        404: {"description": "No such conversation, or no such message in it."},
        409: {"description": "The message was sent before, or an answer is still being written."},
        429: {
            "description": "Too many messages in a short time, or a usage limit is "
            "reached. `Retry-After` says how long to wait. For a usage limit, `window` "
            "names the one that is full, `resets_at` says when it starts over, and "
            "`alternatives` lists the models that cost nothing. Or as many answers are "
            "being written as the plan allows at once, which `at_once` says: nobody "
            "knows when one ends, so there is no `Retry-After`."
        },
        503: {
            "description": "The model can't answer right now, nor can any model asked "
            "in its place: `model_status` says why the model itself can't. Or messages, "
            "or usage, can't be counted right now, or an answer couldn't be read along "
            "with."
        },
    },
)
async def start_turn(
    conversation_id: uuid.UUID,
    body: TurnRequest,
    principal: CurrentPrincipal,
    db: Database,
    catalog: Models,
    limits: Limits,
    usage: Usage,
    events: Events,
    runner: Runner,
    readers: Readers,
    resources: AppResources,
    clock: Clock,
    now: Now,
    new_id: NewId,
) -> StreamingResponse:
    """Ask a model something, and stream its answer.

    When the model can't answer before its first word, a model asked in its place does,
    as `GET /api/models` lists them (`then`). The turn is the asked model's all the
    same, and costs what a question to it costs: its `answered_by` says which model
    wrote the answer.

    The answer is written whether or not this response is read to its end. To stop it,
    cancel the turn: closing the connection doesn't."""
    try:
        offered = catalog.get(body.model)
    except UnknownModelError:
        raise Problem(422, f"No model is called {body.model!r}. See GET /api/models.") from None
    await limits.turn(principal.user_id)
    # The model, and those that stand in for it: one that isn't ready is passed over.
    chain = await catalog.chain_status(offered.name, now=now)
    if not chain.ready:
        raise not_ready(offered.name, chain.status.state)
    # Whether the turn can ask for reasoning is the first ready one's to say: it is asked
    # first, unless the request is larger than its room, or its budget has nothing left
    # for it, while another can take it (below). That a guess or a count passes it over
    # changes nothing of what the turn may ask.
    if body.thinking and not chain.ready[0][1].thinking:
        raise Problem(422, "This model can't reason before it answers.", model=offered.name)

    plan = usage.plans.plan_for(is_guest=principal.is_guest)
    begun = await _begin(
        db, body, conversation_id, principal.user_id, offered, new_id(), now, plan.answers_at_once
    )
    messages = context.for_model(begun.history, now=now)
    # Actio's guess at the prompt: what the credits are held by, what each model's room
    # is held against, and what a request is first counted as in a model's budget.
    tokens = prompt_tokens(messages)
    # What the providers' plans have left for the request, for each ready model that has
    # a budget (ADR 0030). A look that can't be had passes nobody over for a budget: the
    # log says so below, with the turn's id.
    try:
        spent = await chain.look(resources.budgets, tokens, now=now)
        unread = None
    except BudgetsUnavailableError as error:
        spent, unread = {}, type(error.__cause__).__name__
    # Before anything is committed: a turn that is refused here leaves nothing behind.
    # The credits are held for the asked model, at its price, whoever answers.
    reserved = await _reserve(usage, db, principal, begun, offered, tokens, now)
    turn = begun.turn
    turn.capped_by_usage = reserved.capped
    answer_id = new_id()
    # The stream begins before the turn does, with the turn as it starts. So whoever
    # finds a turn running also finds a stream to follow.
    start = {
        "type": "start",
        "messageId": str(answer_id),
        "messageMetadata": _metadata(
            begun, answer_id, TurnOut.of(turn, answer=None), turn.created_at
        ),
    }
    try:
        await events.open(turn.id, ui_stream.event(start))
    except EventsUnavailableError:
        await usage.release(reserved, now=now)
        raise Problem(503, CANT_BE_READ) from None
    try:
        await db.commit()
    except Exception:
        await usage.release(reserved, now=now)  # the turn never started
        await events.discard(turn.id)
        raise

    for passed, status in chain.passed_over:
        logger.warning(
            "turn %s: model %s is %s, and is passed over", turn.id, passed.name, status.state
        )
    # A ready model whose room is smaller than the guess is passed over too, while
    # another has room; and then one whose budget has nothing left for the request,
    # while another has. When none has, every one is asked: neither a guess nor a count
    # refuses a turn.
    asked = chain.asked_with(tokens, spent)
    for passed in asked.too_small:
        # Numbers alone: never a word of the question.
        logger.warning(
            "turn %s: model %s has room for %d tokens, and the request is about %d: it is"
            " passed over",
            turn.id,
            passed.name,
            passed.room,
            tokens,
        )
    if unread is not None:
        logger.warning(
            "turn %s: the models' budgets couldn't be read (%s), and none is passed over for"
            " its budget",
            turn.id,
            unread,
        )
    for passed, short in asked.spent:
        logger.warning(
            "turn %s: model %s has no budget left for the request for about %d s (%s): it is"
            " passed over",
            turn.id,
            passed.name,
            short.seconds,
            short.limit,
        )
    asking = [
        answering.Asking(
            model,
            ChatRequest(
                model=model.model,
                messages=messages,
                # No more than was held for the asked model, whoever answers.
                max_output_tokens=min(model.max_output_tokens, reserved.max_output_tokens),
                context_window=model.context_window,
                # A model that reasons by default must be told not to. One that can't
                # reason must not be told anything: its server refuses the setting.
                thinking=body.thinking if status.thinking else None,
            ),
        )
        for model, status in asked.models
    ]
    runner.start(
        turn.id,
        lambda halt: _write(
            begun,
            asking,
            answer_id,
            resources.database,
            clock,
            usage,
            reserved,
            _Outlet(events, turn.id),
            halt,
            budgets=resources.budgets,
            tokens=tokens,
            ask_for_title=catalog.titles is not None,
        ),
    )
    # The turn has started: its own reader is part of it, however many the account has.
    return _following(events, turn.id, readers.start(principal.user_id))


@router.get(
    # `OwnConversation` takes the conversation's id from the path.
    "/api/conversations/{conversation_id}/turns/{turn_id}/stream",  # noqa: FAST003
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "The answer that is being written, from its beginning, in "
            "the same format as the turn's own response.",
            "content": {ui_stream.MEDIA_TYPE: {}},
        },
        204: {
            "description": "Nothing is being written: the turn has ended. Read the "
            "conversation for what it left."
        },
        **NO_SUCH_TURN,
        429: {
            "description": "The account already has six readers open, or more: "
            "responses that read along with a turn. Those of the requests that started "
            "the turns count, and are never refused."
        },
    },
)
async def follow_turn(
    turn_id: uuid.UUID,
    conversation: OwnConversation,
    principal: CurrentPrincipal,
    db: Database,
    events: Events,
    readers: Readers,
    now: Now,
) -> Response:
    """Read along with a turn that is being written: after a reload, after a lost
    connection, or from another window.

    The stream starts at the answer's first event, whenever it is joined, so that it
    can be read by itself."""
    await turns.give_up_on_stale(db, conversation.id, now=now)
    turn = await turns.get(db, conversation, turn_id)
    # Nothing more is read from the database: the connection isn't kept while the
    # answer streams.
    await db.commit()
    if turn is None:
        raise Problem(404, "This conversation has no such turn.")
    if turn.status != "running":
        return Response(status_code=204)
    try:
        reader = readers.join(principal.user_id)
    except TooManyReadersError as error:
        raise Problem(429, _too_many_readers(error.reading, error.at_once)) from None
    return _following(events, turn.id, reader)


@router.post(
    "/api/conversations/{conversation_id}/turns/{turn_id}/cancel",  # noqa: FAST003 - as above
    responses=NO_SUCH_TURN,
)
async def cancel_turn(
    turn_id: uuid.UUID, conversation: OwnConversation, db: Database, runner: Runner, now: Now
) -> TurnOut:
    """Stop a turn that is being written. What it had written is kept, and counted.

    Answers once the turn is recorded, with the turn as it is then: `stopped`. A turn
    that had ended already is left as it ended, and answered the same way. So is one
    that this server can't stop, because the process that was writing it is gone: it
    says `running` until it is given up on, a minute after it was last heard from."""
    if await turns.get(db, conversation, turn_id) is None:
        raise Problem(404, "This conversation has no such turn.")
    await db.commit()  # the turn records itself, in a transaction of its own
    if not await runner.stop(turn_id):
        await turns.give_up_on_stale(db, conversation.id, now=now)
    turn = await turns.get(db, conversation, turn_id)
    answer = await turns.answer_of(db, turn) if turn is not None else None
    await db.commit()
    if turn is None:  # the conversation was deleted meanwhile
        raise Problem(404, "This conversation has no such turn.")
    return TurnOut.of(turn, answer=answer)


async def _begin(
    db: AsyncSession,
    body: TurnRequest,
    conversation_id: uuid.UUID,
    user_id: uuid.UUID,
    offered: OfferedModel,
    turn_id: uuid.UUID,
    now: datetime,
    answers_at_once: int | None,
) -> Begun:
    """Start the turn, or say as a problem why it can't start."""
    shared: dict[str, Any] = {
        "user_id": user_id,
        "conversation_id": conversation_id,
        "model": offered.name,
        "thinking": body.thinking,
        "turn_id": turn_id,
        "now": now,
        "answers_at_once": answers_at_once,
    }
    try:
        if body.message is not None:
            return await turns.ask(
                db,
                message_id=body.message.id,
                parent_id=body.parent_id,
                parts=list(body.message.parts),
                **shared,
            )
        assert body.answer_to is not None  # noqa: S101 - the request model requires one of the two
        return await turns.answer_again(db, question_id=body.answer_to, **shared)
    except turns.NoSuchConversationError:
        raise Problem(404, "No such conversation.") from None
    except tree.NoSuchMessageError:
        raise Problem(404, "This conversation has no such message.") from None
    except tree.OutOfTurnError:
        raise Problem(
            409,
            "Questions and answers take turns: a question follows an answer, "
            "and only a question can be answered.",
        ) from None
    except turns.StillAnsweringError as error:
        raise Problem(
            409, "This conversation is still being answered.", turn_id=str(error.turn_id)
        ) from None
    except turns.AlreadyAskedError as error:
        if error.turn is None:
            raise Problem(409, "This message was already sent.") from None
        raise Problem(
            409,
            "This message was already sent.",
            turn_id=str(error.turn.id),
            turn_status=error.turn.status,
        ) from None
    except turns.IdTakenError:
        raise Problem(409, "This id is already in use. Choose another.") from None
    except turns.TooManyAnswersError as error:
        raise _too_many_answers(error.running, error.at_once) from None


def _too_many_answers(running: int, at_once: int) -> Problem:
    """The 429 for a turn that would be one more answer than the plan allows at once.
    No `Retry-After`: nobody knows when an answer ends. `at_once` is the plan's limit."""
    if running == 1:
        said = (
            "An answer is already being written for you, and your plan allows one at a "
            "time. Wait for it to finish, or stop it."
        )
    else:
        share = "as many as" if running == at_once else "more than"
        said = (
            f"{running} answers are already being written for you, {share} your plan "
            "allows at once. Wait for one to finish, or stop one."
        )
    return Problem(429, said, at_once=at_once)


def _too_many_readers(reading: int, at_once: int) -> str:
    """Why a join is refused: the account has as many readers open as it may, or more,
    since the reader of a turn just started is never refused."""
    to_close = reading - at_once + 1
    return (
        f"You already have {reading} windows or tabs reading answers as they are "
        f"written, and an account may have {at_once} at once. Close "
        f"{'one' if to_close == 1 else to_close} of them, and try again."
    )


async def _reserve(
    usage: UsageLimits,
    db: AsyncSession,
    principal: Principal,
    begun: Begun,
    offered: OfferedModel,
    prompt: int,
    now: datetime,
) -> Reserved:
    """Hold the credits the turn may cost, or say as a problem why it can't run: for
    `offered`, the model the turn asks, at its price and up to its longest answer, for
    a prompt of about `prompt` tokens.

    The hold lasts as long as a turn may stay `running`: one that outlives it is given
    up on, and gets nothing counted.
    """
    try:
        return await usage.reserve(
            db,
            Account(principal.user_id, is_guest=principal.is_guest),
            begun.turn.id,
            model=offered.name,
            prompt_tokens=prompt,
            max_output_tokens=offered.max_output_tokens,
            hold_for=turns.STALE_AFTER,
            now=now,
        )
    except UsageUnavailableError:
        raise Problem(503, UNAVAILABLE) from None
    except LimitReachedError as reached:
        raise _limit_reached(reached, now) from None


def _limit_reached(reached: LimitReachedError, now: datetime) -> Problem:
    """The 429 for a turn that doesn't fit: which window is full, and when it starts
    over. The words are for a person; the members beside them are for a page that
    wants to count down, or to offer a model that costs nothing."""
    window = reached.window  # `session` or `week`: the word reads as it is
    if reached.resets_at is None:
        return Problem(
            429,
            f"This message alone needs more than your plan allows in a {window}.",
            window=window,
            resets_at=None,
            alternatives=reached.alternatives,
        )
    seconds = max(1, math.ceil((reached.resets_at - now).total_seconds()))
    return Problem(
        429,
        f"You've reached your usage limit for this {window}. It resets in {in_words(seconds)}.",
        headers={"Retry-After": str(seconds)},
        retry_after=seconds,
        window=window,
        # Written as every other moment in this API is: in UTC, ending in `Z`.
        resets_at=reached.resets_at.isoformat().replace("+00:00", "Z"),
        alternatives=reached.alternatives,
    )


def _metadata(
    begun: Begun,
    answer_id: uuid.UUID,
    about: TurnOut,
    created_at: datetime,
    *,
    written: bool = True,
) -> dict[str, Any]:
    """An answer's metadata, in the shape a message has in GET /api/conversations/{id}.
    A turn that wrote nothing adds no version to the question's answers."""
    versions = [*begun.earlier_answers, answer_id] if written else begun.earlier_answers
    return MessageMetadata(
        parent_id=begun.turn.question_id, created_at=created_at, versions=versions, turn=about
    ).model_dump(mode="json")


class _Outlet:
    """Where a turn's chunks go: its stream of events, for as long as there is one.

    A turn doesn't depend on being read. When Valkey doesn't take a chunk, or the
    stream is gone, this stops sending and the turn runs on: its answer is recorded
    all the same. A stream with a hole in it would be worse than none, so what is left
    of it is deleted, and whoever was following is told that it was lost.
    """

    def __init__(self, events: TurnEvents, turn_id: uuid.UUID) -> None:
        self._events = events
        self._turn_id = turn_id
        self._open = True

    async def send(self, chunk: Mapping[str, Any]) -> None:
        if self._open:
            await self._put(self._events.append(self._turn_id, ui_stream.event(chunk)))

    async def end(self) -> None:
        """Send the stream's last line. Its readers stop there."""
        if self._open:
            await self._put(self._events.close(self._turn_id, ui_stream.DONE))

    async def keep(self) -> None:
        """Keep the stream while the turn runs: called with each of its beats."""
        if self._open:
            # If that fails, the next chunk finds out whether the stream is still there.
            with suppress(EventsUnavailableError):
                await self._events.keep(self._turn_id)

    async def _put(self, putting: Awaitable[bool]) -> None:
        try:
            taken = await putting
        except EventsUnavailableError:
            taken = False
        if not taken:
            self._open = False
            logger.warning(
                "turn %s: what it streams can't be kept any more; it runs on unread", self._turn_id
            )
            await self._events.discard(self._turn_id)


async def _write(
    begun: Begun,
    asking: Sequence[answering.Asking],
    answer_id: uuid.UUID,
    open_session: async_sessionmaker[AsyncSession],
    clock: Callable[[], datetime],
    usage: UsageLimits,
    reserved: Reserved,
    out: _Outlet,
    halt: Halt,
    *,
    budgets: Budgets,
    tokens: int,
    ask_for_title: bool,
) -> None:
    """The turn itself: ask the models of `asking`, the one it names and those that
    stand in for it, that are ready, and send what the one that answers writes as
    chunks, from `start-step` to `[DONE]`. `start` was sent when the stream was opened.
    What each request to a model takes of its budget is counted in `budgets`, from
    `tokens`, the guess at the prompt. `ask_for_title`: a model is named for titles, and
    the conversation's first turn that completes asks it for one (`turns.finish`).

    This runs in a task of its own (`actio.chat.running`), whoever reads."""
    turn = begun.turn
    running = TurnOut.of(turn, answer=None)
    await out.send({"type": "start-step"})
    try:
        ended = await answering.answer(
            asking,
            model=turn.model,
            conversation_id=turn.conversation_id,
            turn_id=turn.id,
            answer_id=answer_id,
            open_session=open_session,
            clock=clock,
            usage=usage,
            reserved=reserved,
            budgets=budgets,
            tokens=tokens,
            emit=lambda step: out.send(ui_stream.for_step(step)),
            halt=halt,
            on_beat=out.keep,
            ask_for_title=ask_for_title,
        )
    except Exception:
        # Something of Actio's own failed. Readers are told, and read the conversation.
        await out.send({"type": "error", "errorText": answering.EXPLANATIONS["internal_error"]})
        await out.end()
        raise
    if ended.recorded != "recorded":
        # Its outcome wasn't recorded: the turn had been given up on meanwhile, or is
        # gone with its conversation. The stream says nothing of an ending that isn't
        # the record's. Readers are told, and read the conversation.
        lost = ui_stream.GIVEN_UP if ended.recorded == "left" else ui_stream.GONE
        await out.send({"type": "error", "errorText": lost})
        await out.end()
        return
    about = _metadata(
        begun, answer_id, _ended(running, ended), ended.at, written=bool(ended.ending.parts)
    )
    if ended.ending.status == "failed":
        # The details first: the chunk after them ends the stream for the reader.
        await out.send({"type": "message-metadata", "messageMetadata": about})
        explanation = answering.EXPLANATIONS[ended.ending.error or "internal_error"]
        await out.send({"type": "error", "errorText": explanation})
    else:
        for warning in ended.warnings:
            await out.send(ui_stream.usage_warning(warning.bucket, warning.percent))
        await out.send({"type": "finish-step"})
        reason = ui_stream.FINISH_REASONS.get(ended.ending.finish_reason or "", "other")
        await out.send({"type": "finish", "finishReason": reason, "messageMetadata": about})
    await out.end()


def _following(events: TurnEvents, turn_id: uuid.UUID, reader: Reader) -> StreamingResponse:
    """The response that reads along with a turn, as `reader`."""
    return _Reading(reader, _read(events, turn_id))


class _Reading(StreamingResponse):
    """A response that reads along with a turn. Its reader counts against its account
    until the response is over, however that came about: the stream ended, the client
    left, something failed, or the client was gone before anything could be sent, and
    the stream was never read at all."""

    def __init__(self, reader: Reader, content: AsyncIterator[bytes]) -> None:
        super().__init__(content, media_type=ui_stream.MEDIA_TYPE, headers=ui_stream.HEADERS)
        self._reader = reader

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._reader.leave()


async def _read(events: TurnEvents, turn_id: uuid.UUID) -> AsyncIterator[bytes]:
    """What a turn has streamed, and goes on to stream, as server-sent events: from
    `start` to `[DONE]`, whenever the reading began."""
    silent = timedelta(0)
    try:
        # Closed with this response: a reader who leaves stops asking Valkey at once.
        async with aclosing(events.follow(turn_id)) as following:
            async for event in following:
                if event is not None:
                    silent = timedelta(0)
                    yield event
                    continue
                silent += events.tick
                if silent >= KEEP_ALIVE_AFTER:
                    silent = timedelta(0)
                    yield ui_stream.KEEP_ALIVE
    except (EventsLostError, EventsUnavailableError) as error:
        # The turn may be running on. Its reader can't tell from here, and is told so.
        logger.warning("turn %s: a reader lost its stream (%s)", turn_id, type(error).__name__)
        yield ui_stream.event({"type": "error", "errorText": ui_stream.LOST})
        yield ui_stream.DONE


def _ended(running: TurnOut, ended: Ended) -> TurnOut:
    """The turn as it was recorded, without reading it back. Its answer is what it
    wrote, which is what was stored."""
    ending = ended.ending
    usage, timings = ending.usage, ending.timings
    input_tokens = usage.input_tokens if usage else None
    output_tokens = usage.output_tokens if usage else None
    return running.model_copy(
        update={
            "status": ending.status,
            "finish_reason": ending.finish_reason,
            "error": ending.error,
            "input_tokens": input_tokens,
            "cached_input_tokens": usage.cached_input_tokens if usage else None,
            "output_tokens": output_tokens,
            "context_tokens": context.tokens_after(
                ending.parts, input_tokens=input_tokens, output_tokens=output_tokens
            ),
            "load_ms": timings.load_ms if timings else None,
            "prompt_ms": timings.prompt_ms if timings else None,
            "generation_ms": timings.generation_ms if timings else None,
            "credits": ended.credits,
            "first_word_at": ending.first_word_at,
            "answered_by": ending.answered_by,
            "finished_at": ended.at,
        }
    )
