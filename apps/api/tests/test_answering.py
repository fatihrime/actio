"""The parts of a turn that need no server: collecting an answer as it streams, naming
what went wrong, choosing what a model is shown, and how much of its window that takes."""

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from actio.chat import answering, context
from actio.chat.answering import Answer, Closed, Opened, Wrote, code_for
from actio.chat.models import Message
from actio.chat.parts import Part, ReasoningPart, TextPart
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


# --- How much of the window a conversation takes after a turn -------------------------
#
# A turn's model read 30 tokens and wrote 12, in every case but those about the counts.

SADDLE = TextPart(text="The saddle.")  # 11 characters of words


@pytest.mark.parametrize(
    ("answer", "figure"),
    [
        pytest.param([SADDLE], 30 + 12, id="words alone: all that was written"),
        # 11 characters of words of 44 in all: a quarter of the 12 tokens.
        pytest.param(
            [ReasoningPart(text="x" * 33), SADDLE], 30 + 3, id="a thought, then the words"
        ),
        # The same share, back and forth: whatever the order, characters are counted.
        pytest.param(
            [
                ReasoningPart(text="x" * 11),
                TextPart(text="The "),
                ReasoningPart(text="x" * 22),
                TextPart(text="saddle."),
            ],
            30 + 3,
            id="thoughts between the words",
        ),
        pytest.param([ReasoningPart(text="Something to sit on.")], 30, id="a thought alone"),
        pytest.param([TextPart(text=" \n\t")], 30, id="nothing but blank space"),
        pytest.param(
            [ReasoningPart(text="Something to sit on."), TextPart(text="\n")],
            30,
            id="a thought, then blank space",
        ),
        pytest.param([], 30, id="nothing written"),
        # An empty thought is no thought.
        pytest.param([ReasoningPart(text=""), SADDLE], 30 + 12, id="an empty thought"),
    ],
)
def test_a_conversation_takes_what_its_model_read_and_the_words_it_answered_with(
    answer: list[Any], figure: int
) -> None:
    assert context.tokens_after(answer, input_tokens=30, output_tokens=12) == figure


@pytest.mark.parametrize(
    ("words", "thought", "share"),
    [
        (2, 1, 7),  # two thirds of 10 tokens is 6.7
        (1, 2, 3),  # one third is 3.3
        (26, 25, 5),  # the recorded thought of `reasoning.sse`: 5.1
    ],
)
def test_the_words_share_is_rounded_to_a_whole_token(words: int, thought: int, share: int) -> None:
    answer: list[Part] = [ReasoningPart(text="x" * thought), TextPart(text="y" * words)]

    assert context.tokens_after(answer, input_tokens=30, output_tokens=10) == 30 + share


@pytest.mark.parametrize(("input_tokens", "output_tokens"), [(None, 12), (30, None), (None, None)])
def test_a_turn_without_its_counts_says_nothing_of_the_window(
    input_tokens: int | None, output_tokens: int | None
) -> None:
    """As a stopped turn: the stream that was closed never said what was read and written."""
    for answer in ([SADDLE], []):
        assert (
            context.tokens_after(answer, input_tokens=input_tokens, output_tokens=output_tokens)
            is None
        )


@given(
    words=st.text(max_size=200),
    thought=st.text(max_size=2_000),
    input_tokens=st.integers(min_value=0, max_value=1_000_000),
    output_tokens=st.integers(min_value=0, max_value=1_000_000),
)
def test_a_conversation_takes_at_least_what_was_read_and_at_most_all_that_was_written_too(
    words: str, thought: str, input_tokens: int, output_tokens: int
) -> None:
    answer: list[Part] = [ReasoningPart(text=thought), TextPart(text=words)]

    figure = context.tokens_after(answer, input_tokens=input_tokens, output_tokens=output_tokens)

    assert figure is not None
    assert input_tokens <= figure <= input_tokens + output_tokens


def test_what_goes_back_to_a_model_is_what_the_conversation_takes() -> None:
    """The figure and what is sent follow one rule. An answer of which nothing is sent
    back adds nothing to the figure; one whose text is sent back whole, with no thought,
    adds all that was written."""
    question = message("user", TextPart(text="Name a bike part."))
    cases: list[tuple[list[Part], list[Any], int]] = [
        ([ReasoningPart(text="Something to sit on.")], [], 0),
        ([TextPart(text="  ")], [], 0),
        ([TextPart(text="The "), TextPart(text="saddle.")], [Assistant("The saddle.")], 12),
    ]
    for answer, sent, added in cases:
        history = [question, message("assistant", *answer)]

        shown = context.for_model(history, now=NOON)
        figure = context.tokens_after(answer, input_tokens=30, output_tokens=12)

        assert shown[2:] == sent
        assert figure == 30 + added
