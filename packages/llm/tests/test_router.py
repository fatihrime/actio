"""The walk down a chain of models: who is asked, when, what moves on to the next, and
what is raised when none answers.

The providers and holds are scripted here, and note in one log what happens to them, in
order: no network, no server. Every wait has a time limit, so that a walk that waits for
ever fails its test instead of hanging the run.
"""

import asyncio
import math
from collections.abc import AsyncGenerator, Sequence
from contextlib import aclosing
from typing import Any

import pytest

from actio_llm import (
    FOR_NOW,
    ChatRequest,
    ConnectionFailedError,
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
    TookTooLongError,
    ToolCall,
    UnsupportedFeatureError,
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
    It notes in `log` when it is asked, and when its stream is closed, and the event
    loop's time of each. `then` is the script of every ask after the first: without it,
    each ask plays the same script."""

    def __init__(
        self, name: str, log: list[str], *script: Any, then: Sequence[Any] | None = None
    ) -> None:
        self.name = name
        self.log = log
        self.script = [asyncio.Event() if item is NEVER else item for item in script]
        self.then = (
            self.script
            if then is None
            else [asyncio.Event() if item is NEVER else item for item in then]
        )
        self.requests: list[ChatRequest] = []
        self.asked_at: list[float] = []
        self.closed_at: list[float] = []
        self.waiting = asyncio.Event()
        """Set while it waits for what `NEVER` stands for."""

    async def stream(self, request: ChatRequest) -> AsyncGenerator[StreamEvent]:
        script = self.then if self.requests else self.script
        loop = asyncio.get_running_loop()
        self.requests.append(request)
        self.asked_at.append(loop.time())
        self.log.append(f"{self.name} asked")
        try:
            for item in script:
                if isinstance(item, BaseException):
                    raise item
                if isinstance(item, asyncio.Event):
                    self.waiting.set()
                    await item.wait()
                else:
                    yield item
        finally:
            self.closed_at.append(loop.time())
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


class Again:
    """What `on_again` was told, in order, each noted in the log too."""

    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.told: list[tuple[str, LLMError, float]] = []

    def __call__(self, failed: Link, error: LLMError, pause: float) -> None:
        self.told.append((failed.name, error, pause))
        self.log.append(f"told that {failed.name} is asked again")


class Pauses:
    """Stands in for the walk's pause, `asyncio.sleep`: notes each wait it is given, in
    the log too, and waits none of it. It lets the event loop run once, as the real
    pause does: a walk that asked a link over and over would otherwise never let a
    test's time limit end it."""

    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.waited: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waited.append(seconds)
        self.log.append(f"paused {seconds:g} s")
        await asyncio.sleep(0)


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
        RateLimitedError("429: slow down", retry_after_s=20),  # longer than is waited
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
    "handed_on", [TextDelta("Half an"), ReasoningDelta("Let me think")], ids=["a word", "a thought"]
)
async def test_an_answer_cut_at_its_time_is_raised_as_it_came_and_nobody_else_is_asked(
    handed_on: StreamEvent,
) -> None:
    """The answer is that model's, cut where its time was over: neither asked again nor
    asked of the next."""
    log: list[str] = []
    error = TookTooLongError("first was still answering after 300 s")
    failures, again = Failures(log), Again(log)
    first = Model("first", log, handed_on, error, then=[TextDelta("Never"), DONE])
    second = Model("second", log, TextDelta("Never"), DONE)
    walk = Walk([link(first), link(second)], on_failed=failures, on_again=again)
    seen: list[StreamEvent] = []

    with pytest.raises(TookTooLongError) as failure:
        await walked_into(walk, seen)

    assert failure.value is error
    assert seen == [handed_on]
    assert (len(first.requests), second.requests) == (1, [])
    assert (failures.told, again.told) == ([], [])
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


# --- Asked once more -----------------------------------------------------------------


async def test_a_connection_that_failed_at_once_is_asked_again_at_once() -> None:
    """With the same request, before any later link: nothing was sent, and a second try
    that is refused too costs as little. The second try's answer is the link's."""
    log: list[str] = []
    failures, again, pauses = Failures(log), Again(log), Pauses(log)
    error = ConnectionFailedError("can't reach first: connection refused")
    first = Model("first", log, error, then=[TextDelta("Hello"), DONE])
    second = Model("second", log, TextDelta("Never"), DONE)
    links = [link(first), link(second)]
    walk = Walk(links, on_failed=failures, on_again=again, pause=pauses)

    events = await walked(walk)

    assert events == [TextDelta("Hello"), DONE]
    assert first.requests == [links[0].request, links[0].request]
    assert second.requests == []
    assert walk.answered is links[0]
    assert failures.told == []
    assert again.told == [("first", error, 0)]
    assert again.told[0][1] is error
    assert sum(pauses.waited) == 0  # at once


@pytest.mark.parametrize("wait", [0, 0.25, 2.0], ids=["none", "a fraction", "the most"])
async def test_a_server_that_asks_for_a_short_wait_is_asked_again_after_it(wait: float) -> None:
    log: list[str] = []
    again, pauses = Again(log), Pauses(log)
    error = RateLimitedError("429: try again in a moment", retry_after_s=wait)
    first = Model("first", log, error, then=[TextDelta("Hello"), DONE])
    second = Model("second", log, TextDelta("Never"), DONE)
    walk = Walk([link(first), link(second)], on_again=again, pause=pauses)

    events = await walked(walk)

    assert events == [TextDelta("Hello"), DONE]
    assert (len(first.requests), second.requests) == (2, [])
    assert pauses.waited == [wait]
    assert again.told == [("first", error, wait)]
    assert log == [
        "first asked",
        "first closed",
        "told that first is asked again",
        f"paused {wait:g} s",
        "first asked",
        "first closed",
    ]


@pytest.mark.parametrize(
    "wait",
    [None, 2.001, 20, math.inf, math.nan, -1],
    ids=["none said", "just over the most", "twenty seconds", "for ever", "nan", "less than none"],
)
async def test_a_server_that_names_no_wait_or_a_longer_one_is_asked_once(
    wait: float | None,
) -> None:
    """The next link is asked at once: a model that stands in is there for that."""
    log: list[str] = []
    failures, again, pauses = Failures(log), Again(log), Pauses(log)
    error = RateLimitedError("429: slow down", retry_after_s=wait)
    first = Model("first", log, error, then=[TextDelta("Never"), DONE])
    second = Model("second", log, TextDelta("Answered"), DONE)
    walk = Walk([link(first), link(second)], on_failed=failures, on_again=again, pause=pauses)

    events = await walked(walk)

    assert events == [TextDelta("Answered"), DONE]
    assert (len(first.requests), len(second.requests)) == (1, 1)
    assert (again.told, pauses.waited) == ([], [])
    assert failures.told == [("first", error, "second")]


async def test_the_pause_is_the_wait_the_server_asked_for_and_nothing_is_asked_during_it() -> None:
    """The walk's own pause, by the event loop's clock: a twentieth of a second here."""
    log: list[str] = []
    told = asyncio.Event()

    def again(_failed: Link, _error: LLMError, _pause: float) -> None:
        told.set()

    first = Model(
        "first",
        log,
        RateLimitedError("429", retry_after_s=0.05),
        then=[TextDelta("Hello"), DONE],
    )
    walking = asyncio.create_task(walked(Walk([link(first)], on_again=again)))
    await asyncio.wait_for(told.wait(), PATIENCE_S)
    await asyncio.sleep(0.02)
    asked_meanwhile = len(first.requests)

    events = await asyncio.wait_for(walking, PATIENCE_S)

    assert asked_meanwhile == 1
    assert events == [TextDelta("Hello"), DONE]
    paused = first.asked_at[1] - first.closed_at[0]
    assert 0.049 <= paused < 1.0


@pytest.mark.parametrize(
    "script",
    [
        pytest.param((ProviderUnavailableError("503: too busy"),), id="too busy"),
        pytest.param(
            (ProviderUnavailableError("didn't start answering within 90 s"),),
            id="too late to begin",
        ),
        pytest.param(
            (ProviderUnavailableError("can't reach first: ConnectTimeout()"),),
            id="a connection never answered",
        ),
        pytest.param((StreamInterruptedError("the connection dropped"),), id="broke off"),
        pytest.param(
            (TookTooLongError("first was still answering after 300 s"),),
            id="took too long, with no word",
        ),
        pytest.param((), id="ended without its finish"),
        pytest.param((ProviderError("500"),), id="failed"),
        pytest.param((RequestRejectedError("400: no such option"),), id="refused"),
        pytest.param((ContextOverflowError("413: too large"),), id="too large"),
        pytest.param((ModelNotFoundError("first-model"),), id="missing"),
        pytest.param((UnsupportedFeatureError("can't reason"),), id="can't do it"),
        pytest.param((UnsupportedServerError("Ollama 0.31 is too old"),), id="too old"),
        pytest.param((LLMError("something new"),), id="anything else"),
    ],
)
async def test_any_other_failure_is_not_asked_again(script: tuple[LLMError, ...]) -> None:
    """Nothing says that it is over in a moment, or another try may meet it again: the
    next link is asked at once. The link here would answer a second try."""
    log: list[str] = []
    again = Again(log)
    first = Model("first", log, *script, then=[TextDelta("Never"), DONE])
    second = Model("second", log, TextDelta("Answered"), DONE)
    walk = Walk([link(first), link(second)], on_again=again)

    events = await walked(walk)

    assert events == [TextDelta("Answered"), DONE]
    assert (len(first.requests), len(second.requests)) == (1, 1)
    assert again.told == []


@pytest.mark.parametrize(
    "then",
    [
        ConnectionFailedError("can't reach first: connection refused again"),
        RateLimitedError("429: try again in a moment", retry_after_s=1),
    ],
    ids=["a connection that failed again", "a server that asks for a short wait"],
)
async def test_a_link_is_asked_twice_at_most_and_its_second_error_is_its_failure(
    then: LLMError,
) -> None:
    """Whatever the second try fails with, one that would be worth asking again too:
    `on_failed` is told once, of that error."""
    log: list[str] = []
    failures, again, pauses = Failures(log), Again(log), Pauses(log)
    error = ConnectionFailedError("can't reach first: connection refused")
    first = Model("first", log, error, then=[then])
    second = Model("second", log, TextDelta("Answered"), DONE)
    walk = Walk([link(first), link(second)], on_failed=failures, on_again=again, pause=pauses)

    events = await walked(walk)

    assert events == [TextDelta("Answered"), DONE]
    assert (len(first.requests), len(second.requests)) == (2, 1)
    assert again.told == [("first", error, 0)]
    assert failures.told == [("first", then, "second")]
    assert failures.told[0][1] is then
    assert sum(pauses.waited) == 0  # the second's wait isn't waited


async def test_when_every_link_failed_a_links_second_error_is_the_one_that_counts() -> None:
    """By the rule of `FOR_NOW`, as ever. Here the first link's first error is one that
    another try may not meet, and its second isn't: so the last link's is raised. Alone,
    a link's second error is."""
    log: list[str] = []
    failures = Failures(log)
    too_long, too_long_too = ContextOverflowError("too long"), ContextOverflowError("too long")
    first = Model("first", log, ConnectionFailedError("refused"), then=[too_long])
    second = Model("second", log, too_long_too)
    busy = ProviderUnavailableError("busy")
    alone = Model("alone", log, ConnectionFailedError("refused"), then=[busy])

    with pytest.raises(LLMError) as chain_failed:
        await walked(Walk([link(first), link(second)], on_failed=failures))
    with pytest.raises(LLMError) as alone_failed:
        await walked(Walk([link(alone)]))

    assert chain_failed.value is too_long_too
    assert [error for _, error, _ in failures.told] == [too_long, too_long_too]
    assert alone_failed.value is busy
    assert (len(first.requests), len(second.requests), len(alone.requests)) == (2, 1, 2)


async def test_each_link_of_a_chain_has_a_second_try_of_its_own() -> None:
    """Not one for the whole walk."""
    log: list[str] = []
    again = Again(log)
    first = Model(
        "first", log, ConnectionFailedError("refused"), then=[ProviderUnavailableError("busy")]
    )
    second = Model(
        "second", log, ConnectionFailedError("refused too"), then=[TextDelta("Answered"), DONE]
    )
    walk = Walk([link(first), link(second)], on_again=again)

    events = await walked(walk)

    assert events == [TextDelta("Answered"), DONE]
    assert log == [
        "first asked",
        "first closed",
        "told that first is asked again",
        "first asked",
        "first closed",
        "second asked",
        "second closed",
        "told that second is asked again",
        "second asked",
        "second closed",
    ]
    assert walk.answered is not None
    assert walk.answered.name == "second"


async def test_a_link_asked_again_keeps_its_hold_through_both_tries() -> None:
    """Entered once, and given back once the second try's stream is closed: a turn that
    waited at a gate isn't sent to the back of the line to be asked again. The first
    try's stream is closed before the link is said to be asked again."""
    log: list[str] = []
    walk = Walk(
        [
            link(
                Model(
                    "first",
                    log,
                    ConnectionFailedError("refused"),
                    then=[TextDelta("Answered"), DONE],
                ),
                Hold("gate 1", log),
            )
        ],
        on_again=Again(log),
    )

    await walked(walk)

    assert log == [
        "gate 1 waited for",
        "gate 1 held",
        "first asked",
        "first closed",
        "told that first is asked again",
        "first asked",
        "first closed",
        "gate 1 given back",
    ]


async def test_a_link_asked_again_gives_its_hold_back_before_the_next_ones_is_waited_for() -> None:
    log: list[str] = []
    walk = Walk(
        [
            link(
                Model(
                    "first",
                    log,
                    ConnectionFailedError("refused"),
                    then=[ProviderUnavailableError("busy")],
                ),
                Hold("gate 1", log),
            ),
            link(Model("second", log, TextDelta("Answered"), DONE), Hold("gate 2", log)),
        ],
        on_failed=Failures(log),
        on_again=Again(log),
    )

    await walked(walk)

    assert log == [
        "gate 1 waited for",
        "gate 1 held",
        "first asked",
        "first closed",
        "told that first is asked again",
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


async def test_a_cancellation_during_the_pause_passes_through() -> None:
    """As a stop does: the hold is given back, the link isn't asked again, and no later
    link is asked. The pause is the walk's own, of two seconds, and isn't waited out."""
    log: list[str] = []
    told = asyncio.Event()

    def again(_failed: Link, _error: LLMError, _pause: float) -> None:
        told.set()

    first = Model(
        "first",
        log,
        RateLimitedError("429", retry_after_s=2.0),
        then=[TextDelta("Never"), DONE],
    )
    second = Model("second", log, TextDelta("Never"), DONE)
    walking = asyncio.create_task(
        walked(Walk([link(first, Hold("gate 1", log)), link(second)], on_again=again))
    )
    await asyncio.wait_for(told.wait(), PATIENCE_S)

    walking.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(walking, 0.5)

    assert len(first.requests) == 1
    assert second.requests == []
    assert log[-2:] == ["first closed", "gate 1 given back"]


async def test_another_exception_from_the_second_try_passes_through() -> None:
    log: list[str] = []
    mistake = RuntimeError("a mistake")
    first = Model("first", log, ConnectionFailedError("refused"), then=[mistake])
    second = Model("second", log, TextDelta("Never"), DONE)

    with pytest.raises(RuntimeError) as failure:
        await walked(Walk([link(first), link(second)]))

    assert failure.value is mistake
    assert (len(first.requests), second.requests) == (2, [])


@pytest.mark.parametrize(
    "error",
    [
        ConnectionFailedError("can't reach first: connection refused"),
        RateLimitedError("429: try again in a moment", retry_after_s=0),
    ],
    ids=["a connection that failed", "a server that asks for no wait"],
)
async def test_after_something_was_handed_on_nothing_is_asked_again(error: LLMError) -> None:
    """The error is raised as it came, though it would be worth another try before."""
    log: list[str] = []
    again = Again(log)
    first = Model("first", log, TextDelta("Half an"), error, then=[TextDelta("Never"), DONE])
    second = Model("second", log, TextDelta("Never"), DONE)
    walk = Walk([link(first), link(second)], on_again=again)
    seen: list[StreamEvent] = []

    with pytest.raises(LLMError) as failure:
        await walked_into(walk, seen)

    assert failure.value is error
    assert seen == [TextDelta("Half an")]
    assert (len(first.requests), second.requests) == (1, [])
    assert again.told == []


@pytest.mark.parametrize(
    "error",
    [
        StreamInterruptedError("the connection dropped"),
        ConnectionFailedError("can't reach first: connection refused"),
    ],
    ids=["broke off", "a failure worth asking again before"],
)
async def test_what_a_second_try_handed_on_is_the_links_answer_as_a_first_trys_is(
    error: LLMError,
) -> None:
    """A second try is a try like the first: once it has handed something on, its error
    is raised as it came, the link isn't asked a third time, and no later link is asked,
    whose answer would follow the half that a reader was shown."""
    log: list[str] = []
    failures = Failures(log)
    first = Model(
        "first", log, ConnectionFailedError("refused"), then=[TextDelta("Half an"), error]
    )
    second = Model("second", log, TextDelta("Never"), DONE)
    links = [link(first), link(second)]
    walk = Walk(links, on_failed=failures)
    seen: list[StreamEvent] = []

    with pytest.raises(LLMError) as failure:
        await walked_into(walk, seen)

    assert failure.value is error
    assert seen == [TextDelta("Half an")]
    assert (len(first.requests), second.requests) == (2, [])
    assert walk.answered is links[0]
    assert failures.told == []


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
        (TookTooLongError("still answering"), True),
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
