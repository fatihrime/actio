"""The route that puts a question to a model and streams the answer back.

`POST /api/conversations/{id}/turns` does one of two things:

- with `message`: adds a new question to the conversation, and answers it. The first
  question creates the conversation, under the id in the path. Rewording an earlier
  question is the same request, with the `parent_id` of the message it should follow;
- with `answer_to`: answers a question that is already there once more.

What goes wrong before a model is asked is an ordinary error: 404, 409, 422, 429, 503.
Once the turn has started the answer is `200`, and everything else, the model's own
failures included, comes in the stream (`actio.api.ui_stream`).

The conversation's and the question's ids are chosen by whoever sends them, as a
browser does before it has heard from the server. The question's id also makes a
repeated request harmless: the same message is never asked twice.
"""

import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from datetime import datetime
from typing import Any, Self

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from actio.api import ui_stream
from actio.api.conversations import MessageMetadata, TurnOut
from actio.api.dependencies import (
    NO_SESSION,
    AppResources,
    Clock,
    CurrentPrincipal,
    Database,
    Models,
    NewId,
    Now,
)
from actio.api.problems import Problem
from actio.api.rate_limits import Limits
from actio.catalog import OfferedModel, UnknownModelError
from actio.chat import answering, context, tree, turns
from actio.chat.answering import Ended
from actio.chat.parts import TextPart
from actio.chat.turns import Begun, Ending
from actio_llm import ChatRequest

router = APIRouter(tags=["conversations"], responses=NO_SESSION)

MAX_QUESTION_CHARS = 100_000
"""The longest question accepted, in characters. Far more than a small model can read;
it is a bound on what gets stored, not a promise that a model will take it."""

NOT_READY = {
    "missing": "This model isn't on its server yet.",
    "unreachable": "This model's server can't be reached.",
}


class NewMessage(BaseModel):
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


class TurnRequest(BaseModel):
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
        429: {"description": "Too many messages. `Retry-After` says how long to wait."},
        503: {
            "description": "The model can't answer right now: `model_status` says why. "
            "Or messages can't be counted right now."
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
    resources: AppResources,
    clock: Clock,
    now: Now,
    new_id: NewId,
) -> StreamingResponse:
    """Ask a model something, and stream its answer."""
    try:
        offered = catalog.get(body.model)
    except UnknownModelError:
        raise Problem(422, f"No model is called {body.model!r}. See GET /api/models.") from None
    await limits.turn(principal.user_id)
    status = await catalog.status(offered.name, now=now)
    if status.state != "ready":
        raise Problem(503, NOT_READY[status.state], model=offered.name, model_status=status.state)
    if body.thinking and not status.thinking:
        raise Problem(422, "This model can't reason before it answers.", model=offered.name)

    begun = await _begin(db, body, conversation_id, principal.user_id, offered, new_id(), now)
    await db.commit()

    request = ChatRequest(
        model=offered.model,
        messages=context.for_model(begun.history, now=now),
        max_output_tokens=offered.max_output_tokens,
        context_window=offered.context_window,
        # A model that reasons by default must be told not to. One that can't reason
        # must not be told anything: its server refuses the setting.
        thinking=body.thinking if status.thinking else None,
    )
    chunks = _stream(begun, offered, request, new_id(), resources.database, clock)
    return StreamingResponse(chunks, media_type=ui_stream.MEDIA_TYPE, headers=ui_stream.HEADERS)


async def _begin(
    db: AsyncSession,
    body: TurnRequest,
    conversation_id: uuid.UUID,
    user_id: uuid.UUID,
    offered: OfferedModel,
    turn_id: uuid.UUID,
    now: datetime,
) -> Begun:
    """Start the turn, or say as a problem why it can't start."""
    shared: dict[str, Any] = {
        "user_id": user_id,
        "conversation_id": conversation_id,
        "model": offered.name,
        "thinking": body.thinking,
        "turn_id": turn_id,
        "now": now,
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


async def _stream(
    begun: Begun,
    offered: OfferedModel,
    request: ChatRequest,
    answer_id: uuid.UUID,
    open_session: async_sessionmaker[AsyncSession],
    clock: Callable[[], datetime],
) -> AsyncIterator[bytes]:
    """The turn as server-sent events, from `start` to `[DONE]`."""
    turn = begun.turn

    def metadata(about: TurnOut, created_at: datetime, *, written: bool = True) -> dict[str, Any]:
        # The same shape a message has in GET /api/conversations/{id}. A turn that wrote
        # nothing adds no version to the question's answers.
        versions = [*begun.earlier_answers, answer_id] if written else begun.earlier_answers
        return MessageMetadata(
            parent_id=turn.question_id, created_at=created_at, versions=versions, turn=about
        ).model_dump(mode="json")

    running = TurnOut.of(turn)
    yield ui_stream.event(
        {
            "type": "start",
            "messageId": str(answer_id),
            "messageMetadata": metadata(running, turn.created_at),
        }
    )
    yield ui_stream.event({"type": "start-step"})
    steps = answering.answer(
        offered,
        request,
        conversation_id=turn.conversation_id,
        turn_id=turn.id,
        answer_id=answer_id,
        open_session=open_session,
        clock=clock,
    )
    async with aclosing(steps):
        async for step in steps:
            if not isinstance(step, Ended):
                yield ui_stream.event(ui_stream.for_step(step))
                continue
            ended = metadata(
                _ended(running, step.ending, step.at), step.at, written=bool(step.ending.parts)
            )
            if step.ending.status == "failed":
                # The details first: the chunk after them ends the stream for the reader.
                yield ui_stream.event({"type": "message-metadata", "messageMetadata": ended})
                explanation = answering.EXPLANATIONS[step.ending.error or "internal_error"]
                yield ui_stream.event({"type": "error", "errorText": explanation})
            else:
                yield ui_stream.event({"type": "finish-step"})
                reason = ui_stream.FINISH_REASONS.get(step.ending.finish_reason or "", "other")
                yield ui_stream.event(
                    {"type": "finish", "finishReason": reason, "messageMetadata": ended}
                )
    yield ui_stream.DONE


def _ended(running: TurnOut, ending: Ending, at: datetime) -> TurnOut:
    """The turn as it was recorded, without reading it back."""
    usage, timings = ending.usage, ending.timings
    return running.model_copy(
        update={
            "status": ending.status,
            "finish_reason": ending.finish_reason,
            "error": ending.error,
            "input_tokens": usage.input_tokens if usage else None,
            "cached_input_tokens": usage.cached_input_tokens if usage else None,
            "output_tokens": usage.output_tokens if usage else None,
            "load_ms": timings.load_ms if timings else None,
            "prompt_ms": timings.prompt_ms if timings else None,
            "generation_ms": timings.generation_ms if timings else None,
            "finished_at": at,
        }
    )
