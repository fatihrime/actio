"""The parts of a turn that need no server: collecting an answer as it streams, naming
what went wrong, and choosing what a model is shown."""

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from actio.chat import answering, context
from actio.chat.answering import Answer, Closed, Opened, Wrote, code_for
from actio.chat.models import Message
from actio.chat.parts import ReasoningPart, TextPart
from actio.core.ids import new_id
from actio_llm import (
    Assistant,
    ContextOverflowError,
    Finish,
    FinishReason,
    LLMError,
    ModelNotFoundError,
    ProviderError,
    ProviderUnavailableError,
    RateLimitedError,
    ReasoningDelta,
    RequestRejectedError,
    StreamInterruptedError,
    System,
    TextDelta,
    ToolCall,
    UnsupportedFeatureError,
    UnsupportedServerError,
    Usage,
    User,
)

NOON = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
STOP = Finish(FinishReason.STOP, Usage(input_tokens=3, output_tokens=2))


def steps_of(answer: Answer, *events: Any) -> list[Any]:
    return [step for event in events for step in answer.take(event)]


# --- Collecting an answer -------------------------------------------------------------


def test_text_that_arrives_in_pieces_is_one_part() -> None:
    answer = Answer()

    steps = steps_of(answer, TextDelta("The "), TextDelta("saddle."), STOP)

    assert steps == [
        Opened("text", 0),
        Wrote("text", 0, "The "),
        Wrote("text", 0, "saddle."),
        Closed("text", 0),
    ]
    assert answer.parts == [TextPart(text="The saddle.")]
    assert answer.finish is STOP


def test_reasoning_closes_when_the_answer_begins() -> None:
    answer = Answer()

    steps = steps_of(
        answer, ReasoningDelta("Something "), ReasoningDelta("to sit on."), TextDelta("The saddle.")
    )

    assert steps == [
        Opened("reasoning", 0),
        Wrote("reasoning", 0, "Something "),
        Wrote("reasoning", 0, "to sit on."),
        Closed("reasoning", 0),
        Opened("text", 1),
        Wrote("text", 1, "The saddle."),
    ]
    assert answer.parts == [
        ReasoningPart(text="Something to sit on."),
        TextPart(text="The saddle."),
    ]


def test_a_model_that_goes_back_to_reasoning_gets_a_new_part_each_time() -> None:
    answer = Answer()

    steps_of(
        answer,
        ReasoningDelta("First thought."),
        TextDelta("A start. "),
        ReasoningDelta("Second thought."),
        TextDelta("The end."),
        STOP,
    )

    assert answer.parts == [
        ReasoningPart(text="First thought."),
        TextPart(text="A start. "),
        ReasoningPart(text="Second thought."),
        TextPart(text="The end."),
    ]


def test_an_empty_piece_opens_nothing() -> None:
    answer = Answer()

    assert steps_of(answer, TextDelta(""), ReasoningDelta(""), STOP) == []
    assert answer.parts == []


def test_an_answer_that_broke_off_can_be_closed_as_far_as_it_got() -> None:
    answer = Answer()
    steps_of(answer, TextDelta("Half an "))

    assert answer.close() == [Closed("text", 0)]
    assert answer.close() == []  # once
    assert answer.parts == [TextPart(text="Half an ")]
    assert answer.finish is None


def test_a_tool_call_nobody_asked_for_is_the_models_error() -> None:
    answer = Answer()

    with pytest.raises(ProviderError, match="none was offered"):
        answer.take(ToolCall("call-1", "get_weather", {"city": "Izmir"}))


# --- Naming what went wrong -----------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (ProviderUnavailableError("connection refused"), "model_unavailable"),
        (RateLimitedError("slow down", retry_after_s=2), "model_unavailable"),
        (ModelNotFoundError("qwen3.5:9b"), "model_missing"),
        (ContextOverflowError("too long"), "context_overflow"),
        (UnsupportedFeatureError("does not support thinking"), "model_refused"),
        (RequestRejectedError("no such option"), "model_refused"),
        (StreamInterruptedError("connection lost"), "answer_interrupted"),
        (ProviderError("HTTP 500"), "model_error"),
        (UnsupportedServerError("Ollama 0.31 is too old"), "model_error"),
        (LLMError("something new"), "model_error"),
    ],
)
def test_every_failure_of_a_model_call_has_a_code_and_an_explanation(
    error: LLMError, code: str
) -> None:
    assert code_for(error) == code
    assert answering.EXPLANATIONS[code].endswith(".")


# --- What a model is shown ------------------------------------------------------------


def message(role: str, *parts: Any) -> Message:
    return Message(
        id=new_id(),
        conversation_id=uuid.uuid4(),
        role=role,
        parts=[part.model_dump(mode="json") for part in parts],
        created_at=NOON,
    )


def test_a_model_is_told_the_day_and_shown_the_text_of_the_branch() -> None:
    history = [
        message("user", TextPart(text="Name a bike part.")),
        message(
            "assistant", ReasoningPart(text="Something to sit on."), TextPart(text="The saddle.")
        ),
        message("user", TextPart(text="Why "), TextPart(text="that one?")),
    ]

    shown = context.for_model(history, now=datetime(2026, 3, 5, 23, 59, tzinfo=UTC))

    assert shown == [
        System(
            "You are a helpful assistant.\n\n"
            "Today's date is Thursday, 5 March 2026. Use it when a question depends on the date. "
            "Otherwise, don't mention it."
        ),
        User("Name a bike part."),
        Assistant("The saddle."),  # without what it thought
        User("Why that one?"),
    ]


def test_the_day_is_not_all_a_model_is_told() -> None:
    """Told nothing but the date, a small model takes the date for its subject, and
    opens its answers with it. So the date comes after who the model is, and with when
    to use it."""
    told = context.instructions(datetime(2026, 3, 5, 9, 0, tzinfo=UTC))

    who, about_the_day = told.split("\n\n")
    assert who == "You are a helpful assistant."
    # With the day of the week: a small model that has to work it out gets it wrong.
    assert about_the_day.startswith("Today's date is Thursday, 5 March 2026.")
    assert "don't mention it" in about_the_day


def test_a_model_is_told_the_same_all_day() -> None:
    """The model's server reuses what it has already read, as long as it starts the same."""
    morning = context.instructions(datetime(2026, 3, 5, 0, 0, tzinfo=UTC))
    night = context.instructions(datetime(2026, 3, 5, 23, 59, tzinfo=UTC))

    assert morning == night
    assert context.instructions(datetime(2026, 3, 6, 0, 0, tzinfo=UTC)) != night


def test_an_answer_with_no_text_is_not_sent_back() -> None:
    """An answer that ran out of room while still reasoning said nothing."""
    history = [
        message("user", TextPart(text="A hard question")),
        message("assistant", ReasoningPart(text="Thinking, and thinking, and")),
        message("user", TextPart(text="Well?")),
    ]

    shown = context.for_model(history, now=NOON)

    assert shown[1:] == [User("A hard question"), User("Well?")]
