"""Checks that need a real Ollama server, because a cassette can't show them.

They are skipped unless ACTIO_TEST_OLLAMA_URL is set (`make test-live`). Two of them
need a second model on that server, named by an environment variable:

- ACTIO_TEST_OLLAMA_THINKING_MODEL: a model that can reason;
- ACTIO_TEST_OLLAMA_MLX_MODEL: a model that runs on Ollama's MLX engine.

On an Apple Silicon Mac, qwen3.5:4b is both.
"""

import os
import time
from contextlib import aclosing

import httpx2
import pytest

from actio_llm import (
    ChatRequest,
    Finish,
    FinishReason,
    Ollama,
    ReasoningDelta,
    TextDelta,
    User,
)

pytestmark = pytest.mark.anyio

MODEL = "granite4:micro"
CONTEXT = 4096


async def test_closing_the_stream_frees_the_server(live_url: str) -> None:
    """Stop works by hanging up: Ollama must stop generating, not finish in the background.

    The server answers one request at a time, so if the abandoned answer were still
    being written (about a minute of it), the next request would wait behind it.
    """
    async with httpx2.AsyncClient(base_url=live_url) as client:
        ollama = Ollama(client)
        endless = ChatRequest(
            model=MODEL,
            messages=[User("Count from 1 to 3000, one number per line.")],
            context_window=CONTEXT,
        )
        stream = ollama.stream(endless)
        for _ in range(5):
            await anext(stream)
        await stream.aclose()

        started = time.monotonic()
        quick = ChatRequest(
            model=MODEL,
            messages=[User("Reply with: ok")],
            context_window=CONTEXT,
            max_output_tokens=5,
        )
        async with aclosing(ollama.stream(quick)) as events:
            last = [event async for event in events][-1]

    assert isinstance(last, Finish)
    assert time.monotonic() - started < 15


async def test_a_reasoning_model_thinks_only_when_asked(live_url: str) -> None:
    model = os.environ.get("ACTIO_TEST_OLLAMA_THINKING_MODEL")
    if model is None:
        pytest.skip("set ACTIO_TEST_OLLAMA_THINKING_MODEL to a model that can reason")

    async with httpx2.AsyncClient(base_url=live_url) as client:
        ollama = Ollama(client)
        assert (await ollama.describe(model)).can("thinking")

        kinds: dict[bool, list[type]] = {}
        for thinking in (True, False):
            request = ChatRequest(
                model=model,
                messages=[User("What is 17 * 23? Answer with the number.")],
                context_window=CONTEXT,
                max_output_tokens=1024,
                thinking=thinking,
            )
            async with aclosing(ollama.stream(request)) as events:
                kinds[thinking] = [type(event) async for event in events]

    assert ReasoningDelta in kinds[True]
    assert TextDelta in kinds[True]
    # The reasoning comes first: no answer text before the last piece of thinking.
    last_thought = len(kinds[True]) - 1 - kinds[True][::-1].index(ReasoningDelta)
    assert TextDelta not in kinds[True][:last_thought]
    assert ReasoningDelta not in kinds[False]
    assert TextDelta in kinds[False]


async def test_the_mlx_engine_reads_a_long_prompt_in_full_and_the_adapter_bounds_the_answer(
    live_url: str,
) -> None:
    """The MLX engine ignores the context window. This pins what it does instead.

    If a later Ollama starts enforcing the window on this engine, the first half fails
    with `ContextOverflowError`: that would be good news, and the moment to give both
    engines the same guarantee.
    """
    model = os.environ.get("ACTIO_TEST_OLLAMA_MLX_MODEL")
    if model is None:
        pytest.skip("set ACTIO_TEST_OLLAMA_MLX_MODEL to a model that runs on the MLX engine")

    code_word = "ZEBRA-4417"
    filler = "\n".join(
        f"Line {i}: the quick brown fox jumps over the lazy dog." for i in range(100)
    )
    async with httpx2.AsyncClient(base_url=live_url) as client:
        ollama = Ollama(client)
        if (await ollama.describe(model)).enforces_window:
            pytest.skip(f"{model} doesn't run on the MLX engine on this machine")

        # A prompt about three times the window, with what matters at its very start.
        long_prompt = ChatRequest(
            model=model,
            messages=[
                User(
                    f"The code word is {code_word}.\n{filler}\n"
                    "What is the code word? Answer with it only."
                )
            ],
            context_window=512,
            max_output_tokens=40,
            thinking=False,
            temperature=0,
        )
        async with aclosing(ollama.stream(long_prompt)) as events:
            answered = [event async for event in events]

        # An answer that would never end by itself, with only the window to stop it.
        endless = ChatRequest(
            model=model,
            messages=[User("Count from 1 to 3000, one number per line.")],
            context_window=256,
            thinking=False,
        )
        async with aclosing(ollama.stream(endless)) as events:
            counted = [event async for event in events]

    text = "".join(event.text for event in answered if isinstance(event, TextDelta))
    assert code_word in text  # nothing was cut from the start of the prompt
    finish = answered[-1]
    assert isinstance(finish, Finish)
    assert finish.usage is not None
    assert finish.usage.input_tokens > 512  # and the prompt's real size is reported

    last = counted[-1]
    assert isinstance(last, Finish)
    assert last.reason is FinishReason.LENGTH
    assert last.usage is not None
    assert last.usage.output_tokens == 256  # the adapter's limit, since the engine has none
