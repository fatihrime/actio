"""The neutral chat format's own rules."""

from collections.abc import Callable

import pytest

from actio_llm import ChatRequest, Finish, FinishReason, Timings, ToolSpec, Usage, User

HELLO = [User("hello")]
TOOL = ToolSpec("lookup", "Look something up.", {"type": "object"})


@pytest.mark.parametrize(
    ("make", "error"),
    [
        (lambda: ChatRequest(model="", messages=HELLO), "needs a model"),
        (lambda: ChatRequest(model="m", messages=[]), "at least one message"),
        (lambda: ChatRequest(model="m", messages=HELLO, max_output_tokens=0), "max_output_tokens"),
        (lambda: ChatRequest(model="m", messages=HELLO, context_window=-1), "context_window"),
        (lambda: ChatRequest(model="m", messages=HELLO, tools=[TOOL, TOOL]), "unique"),
    ],
)
def test_rejects_requests_that_make_no_sense(make: Callable[[], ChatRequest], error: str) -> None:
    with pytest.raises(ValueError, match=error):
        make()


def test_speed_is_output_tokens_over_generation_time() -> None:
    usage = Usage(input_tokens=100, output_tokens=50)
    timed = Timings(load_ms=0, prompt_ms=20, generation_ms=2_000, total_ms=2_020)

    assert Finish(FinishReason.STOP, usage, timed).tokens_per_second == 25.0


def test_speed_is_unknown_without_a_generation_time() -> None:
    usage = Usage(input_tokens=100, output_tokens=50)
    instant = Timings(load_ms=0, prompt_ms=0, generation_ms=0, total_ms=0)

    assert Finish(FinishReason.STOP, usage).tokens_per_second is None  # a hosted provider
    assert Finish(FinishReason.STOP, usage, instant).tokens_per_second is None
