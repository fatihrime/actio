"""The walk down a chain of models: who is asked, when, what moves on to the next, and
what is raised when none answers.

The providers and holds are scripted here, and note in one log what happens to them, in
order: no network, no server. Every wait has a time limit, so that a walk that waits for
ever fails its test instead of hanging the run.
"""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import aclosing
from typing import Any

import pytest

from actio_llm import (
    FOR_NOW,
    ChatRequest,
    ContextOverflowError,
    Finish,
    FinishReason,
    Link,
    LLMError,
    ModelNotFoundError,
    ProviderError,
    ProviderUnavailableError,
    RateLimitedError,
    ReasoningDelta,
    RequestRejectedError,
    StreamEvent,
    StreamInterruptedError,
    TextDelta,
    ToolCall,
    UnsupportedServerError,
    Usage,
    User,
    Walk,
)

pytestmark = pytest.mark.anyio

PATIENCE_S = 5.0
"""How long a test waits for the walk to get somewhere. It takes microseconds; this only
keeps a broken walk from hanging the run."""
DONE = Finish(FinishReason.STOP, Usage(input_tokens=5, output_tokens=3))
NEVER = object()
"""In a script: wait for an `asyncio.Event` that nothing sets. Each test makes its own,
bound to its own event loop."""


class Model:
    """A provider that follows a script: events to yield, errors to raise, and `NEVER`.
    It notes in `log` when it is asked, and when its stream is closed."""

    def __init__(self, name: str, log: list[str], *script: Any) -> None:
        self.name = name
        self.log = log
        self.script = [asyncio.Event() if item is NEVER else item for item in script]
        self.requests: list[ChatRequest] = []
        self.waiting = asyncio.Event()
        """Set while it waits for what `NEVER` stands for."""

    async def stream(self, request: ChatRequest) -> AsyncGenerator[StreamEvent]:
        self.requests.append(request)
        self.log.append(f"{self.name} asked")
        try:
            for item in self.script:
                if isinstance(item, BaseException):
                    raise item
                if isinstance(item, asyncio.Event):
                    self.waiting.set()
                    await item.wait()
                else:
                    yield item
        finally:
            self.log.append(f"{self.name} closed")


class Hold:
    """What a link holds while it is asked, such as the gate in front of its server. It
    notes in `log` when it is waited for, held and given back. With `full`, it is never
    had: as a server that is busy for ever."""

    def __init__(self, name: str, log: list[str], *, full: bool = False) -> None:
        self.name = name
        self.log = log
        self.full = full
        self.waiting = asyncio.Event()

    async def __aenter__(self) -> None:
        self.log.append(f"{self.name} waited for")
        if self.full:
            self.waiting.set()
            await asyncio.Event().wait()
        self.log.append(f"{self.name} held")

    async def __aexit__(self, *_: object) -> None:
        self.log.append(f"{self.name} given back")


def link(model: Model, hold: Hold | None = None) -> Link:
    """A link to `model`, asked for a model of its own name."""
    return Link(model.name, model, ChatRequest(f"{model.name}-model", [User("Hi")]), hold)


class Failures:
    """What `on_failed` was told, in order, each with the log as it was then."""

    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.told: list[tuple[str, LLMError, str | None]] = []

    def __call__(self, failed: Link, error: LLMError, later: Link | None) -> None:
        self.told.append((failed.name, error, None if later is None else later.name))
        self.log.append(f"told that {failed.name} failed")


async def walked(walk: Walk) -> list[StreamEvent]:
    """Every event the walk yields, to its end."""
    async with asyncio.timeout(PATIENCE_S), aclosing(walk.stream()) as events:
        return [event async for event in events]


async def walked_into(walk: Walk, seen: list[StreamEvent]) -> None:
    """Every event the walk yields, put in `seen` as it comes: those before an error
    too, which the error then follows."""
    async with asyncio.timeout(PATIENCE_S), aclosing(walk.stream()) as events:
        async for event in events:
            seen.append(event)  # noqa: PERF401 - one at a time, to keep those before an error


# --- Who is asked ---------------------------------------------------------------------


async def test_the_first_link_that_answers_is_the_only_one_asked() -> None:
    log: list[str] = []
    first = Model("first", log, TextDelta("Hello"), DONE)
    second = Model("second", log, TextDelta("Never"), DONE)
    walk = Walk([link(first, Hold("gate 1", log)), link(second, Hold("gate 2", log))])

    events = await walked(walk)

    assert events == [TextDelta("Hello"), DONE]
    assert log == [
        "gate 1 waited for",
        "gate 1 held",
        "first asked",
        "first closed",
        "gate 1 given back",
    ]
    assert second.requests == []
    assert walk.answered is not None
    assert walk.answered.name == "first"


async def test_a_later_link_is_asked_only_after_the_one_before_it_failed() -> None:
    """And with its own request."""
    log: list[str] = []
    first = Model("first", log, ProviderUnavailableError("busy"))
    second = Model("second", log, TextDelta("From the second"), DONE)
    links = [link(first), link(second)]
    walk = Walk(links)

    events = await walked(walk)

    assert events == [TextDelta("From the second"), DONE]
    assert log == ["first asked", "first closed", "second asked", "second closed"]
    assert first.requests == [links[0].request]
    assert second.requests == [links[1].request]
    assert walk.answered is links[1]


@pytest.mark.parametrize(
    "error",
    [
        ProviderUnavailableError("connection refused"),
        RateLimitedError("429: slow down", retry_after_s=2),
        ContextOverflowError("413: larger than the account takes in a minute"),
        RequestRejectedError("400: no such option"),
        ModelNotFoundError("first-model"),
        ProviderError("500"),
        UnsupportedServerError("Ollama 0.31 is too old"),
        StreamInterruptedError("the connection dropped"),
        LLMError("something new"),
    ],
    ids=lambda error: type(error).__name__,
)
async def test_any_failure_of_a_model_before_anything_was_handed_on_moves_on(
    error: LLMError,
) -> None:
    log: list[str] = []
    failures = Failures(log)
    second = Model("second", log, TextDelta("Answered"), DONE)
    walk = Walk([link(Model("first", log, error)), link(second)], on_failed=failures)

    events = await walked(walk)

    assert events == [TextDelta("Answered"), DONE]
    assert len(second.requests) == 1
    assert failures.told == [("first", error, "second")]
    assert failures.told[0][1] is error


async def test_a_stream_that_ends_without_its_finish_before_anything_moves_on() -> None:
    """It broke off: a stream always ends with a `Finish` or an error."""
    log: list[str] = []
    failures = Failures(log)
    walk = Walk(
        [link(Model("first", log)), link(Model("second", log, TextDelta("Answered"), DONE))],
        on_failed=failures,
    )

    events = await walked(walk)

    assert events == [TextDelta("Answered"), DONE]
    [(name, error, later)] = failures.told
    assert (name, type(error), later) == ("first", StreamInterruptedError, "second")


@pytest.mark.parametrize(
    "handed_on",
    [
        TextDelta("Half an"),
        TextDelta(" "),
        ReasoningDelta("Let me think"),
        ToolCall("call-1", "weather", {"city": "Izmir"}),
    ],
    ids=["a word", "blank space", "a thought", "a tool call"],
)
async def test_after_something_was_handed_on_the_error_is_raised_and_nobody_else_is_asked(
    handed_on: StreamEvent,
) -> None:
    """As it came. A thought counts: a page that has shown it can't take it back."""
    log: list[str] = []
    error = StreamInterruptedError("the connection dropped")
    failures = Failures(log)
    second = Model("second", log, TextDelta("Never"), DONE)
    walk = Walk([link(Model("first", log, handed_on, error)), link(second)], on_failed=failures)
    seen: list[StreamEvent] = []

    with pytest.raises(StreamInterruptedError) as failure:
        await walked_into(walk, seen)

    assert failure.value is error
    assert seen == [handed_on]
    assert second.requests == []
    assert failures.told == []
    assert walk.answered is not None
    assert walk.answered.name == "first"


@pytest.mark.parametrize(
    "empty", [TextDelta(""), ReasoningDelta("")], ids=["a piece of text", "a thought"]
)
async def test_a_piece_with_no_text_in_it_is_not_a_word(empty: StreamEvent) -> None:
    log: list[str] = []
    second = Model("second", log, TextDelta("Answered"), DONE)
    first = Model("first", log, empty, ProviderUnavailableError("busy"))
    walk = Walk([link(first), link(second)])

    events = await walked(walk)

    assert events == [empty, TextDelta("Answered"), DONE]
    assert len(second.requests) == 1
    assert walk.answered is not None
    assert walk.answered.name == "second"


async def test_a_model_that_finishes_without_a_word_has_answered() -> None:
    log: list[str] = []
    second = Model("second", log, TextDelta("Never"), DONE)
    walk = Walk([link(Model("first", log, DONE)), link(second)])

    events = await walked(walk)

    assert events == [DONE]
    assert second.requests == []
    assert walk.answered is not None
    assert walk.answered.name == "first"


# --- What passes through ---------------------------------------------------------------


async def test_another_exception_passes_through_and_nobody_else_is_asked() -> None:
    """Only a model's failures are sorted: anything else is a mistake to see."""
    log: list[str] = []
    mistake = RuntimeError("a mistake")
    second = Model("second", log, TextDelta("Never"), DONE)
    walk = Walk([link(Model("first", log, mistake)), link(second)])

    with pytest.raises(RuntimeError) as failure:
        await walked(walk)

    assert failure.value is mistake
    assert second.requests == []


@pytest.mark.parametrize("where", ["at its hold", "in its stream"])
async def test_a_cancellation_passes_through_from_wherever_the_walk_waits(where: str) -> None:
    """Cancelled before anything was handed on, as a stop or a time limit does: no later
    link is asked, and what was held is given back."""
    log: list[str] = []
    if where == "at its hold":
        hold = Hold("gate 1", log, full=True)
        first = Model("first", log, TextDelta("Never"), DONE)
        waiting = hold.waiting
    else:
        hold = Hold("gate 1", log)
        first = Model("first", log, NEVER, DONE)
        waiting = first.waiting
    second = Model("second", log, TextDelta("Never"), DONE)
    walking = asyncio.create_task(walked(Walk([link(first, hold), link(second)])))
    await asyncio.wait_for(waiting.wait(), PATIENCE_S)

    walking.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(walking, PATIENCE_S)

    assert second.requests == []
    if where == "in its stream":
        assert log[-2:] == ["first closed", "gate 1 given back"]
    else:
        assert first.requests == []


# --- Holds ----------------------------------------------------------------------------


async def test_a_link_that_failed_gives_its_hold_back_before_the_next_ones_is_waited_for() -> None:
    """Two walks whose chains cross would otherwise each keep one gate while they wait
    at the other's."""
    log: list[str] = []
    failures = Failures(log)
    walk = Walk(
        [
            link(Model("first", log, ProviderUnavailableError("busy")), Hold("gate 1", log)),
            link(Model("second", log, TextDelta("Answered"), DONE), Hold("gate 2", log)),
        ],
        on_failed=failures,
    )

    await walked(walk)

    assert log == [
        "gate 1 waited for",
        "gate 1 held",
        "first asked",
        "first closed",
        "gate 1 given back",
        "told that first failed",
        "gate 2 waited for",
        "gate 2 held",
        "second asked",
        "second closed",
        "gate 2 given back",
    ]


async def test_closing_the_walk_early_closes_the_open_stream_and_gives_its_hold_back() -> None:
    log: list[str] = []
    second = Model("second", log, TextDelta("Never"), DONE)
    walk = Walk(
        [
            link(Model("first", log, TextDelta("Half an"), NEVER, DONE), Hold("gate 1", log)),
            link(second, Hold("gate 2", log)),
        ]
    )

    async with asyncio.timeout(PATIENCE_S), aclosing(walk.stream()) as events:
        first_event = await anext(events)

    assert first_event == TextDelta("Half an")
    assert log[-2:] == ["first closed", "gate 1 given back"]
    assert second.requests == []
    assert "gate 2 waited for" not in log


# --- When none answers ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("errors", "raised"),
    [
        pytest.param(
            [ProviderUnavailableError("busy"), ContextOverflowError("too long")],
            0,
            id="busy, then too long: the first",
        ),
        pytest.param(
            [ContextOverflowError("too long"), RateLimitedError("slow down")],
            1,
            id="too long, then at a limit: the second",
        ),
        pytest.param(
            [ProviderError("failed"), StreamInterruptedError("broke off")],
            0,
            id="failed, then broke off: the first",
        ),
        pytest.param(
            [ContextOverflowError("too long"), ContextOverflowError("too long too")],
            1,
            id="too long twice: the last",
        ),
        pytest.param(
            [ModelNotFoundError("first-model"), UnsupportedServerError("too old")],
            1,
            id="missing, then too old: the last",
        ),
    ],
)
async def test_when_every_link_failed_the_first_error_another_try_may_not_meet_is_raised(
    errors: list[LLMError], raised: int
) -> None:
    """And without one, the last link's: each the object itself, as it came."""
    log: list[str] = []
    failures = Failures(log)
    walk = Walk(
        [link(Model(f"model {number}", log, error)) for number, error in enumerate(errors)],
        on_failed=failures,
    )

    with pytest.raises(LLMError) as failure:
        await walked(walk)

    assert failure.value is errors[raised]
    assert [(name, later) for name, _, later in failures.told] == [
        ("model 0", "model 1"),
        ("model 1", None),
    ]
    assert [error for _, error, _ in failures.told] == errors
    assert walk.answered is None


@pytest.mark.parametrize(
    ("error", "for_now"),
    [
        (ProviderUnavailableError("away"), True),
        (RateLimitedError("slow down"), True),
        (StreamInterruptedError("broke off"), True),
        (ProviderError("failed"), True),
        (RequestRejectedError("refused"), False),
        (ContextOverflowError("too long"), False),
        (ModelNotFoundError("m"), False),
        (UnsupportedServerError("too old"), False),
        (LLMError("something new"), False),
    ],
    ids=lambda value: type(value).__name__ if isinstance(value, LLMError) else str(value),
)
def test_which_errors_another_try_may_not_meet_again(error: LLMError, for_now: bool) -> None:
    assert isinstance(error, FOR_NOW) is for_now


# --- One link -------------------------------------------------------------------------


async def test_with_one_link_the_walk_is_its_provider() -> None:
    log: list[str] = []
    events = [ReasoningDelta("Hm"), TextDelta("Hello"), DONE]
    error = ProviderUnavailableError("busy")
    answers, fails = Model("one", log, *events), Model("fails", log, error)
    failures = Failures(log)

    answered = await walked(Walk([link(answers)]))
    with pytest.raises(ProviderUnavailableError) as failure:
        await walked(Walk([link(fails)], on_failed=failures))

    assert answered == events
    assert failure.value is error
    assert failures.told == [("fails", error, None)]


def test_a_walk_needs_a_link() -> None:
    with pytest.raises(ValueError, match="at least one link"):
        Walk([])
