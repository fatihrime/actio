"""Checks that need a real Ollama server, because a cassette can't show them.

They are skipped unless ACTIO_TEST_OLLAMA_URL is set (`make test-live`). The reasoning
test also needs ACTIO_TEST_OLLAMA_THINKING_MODEL, a model on that server that can
reason, such as qwen3.5:4b.
"""

import os
import time
from contextlib import aclosing

import httpx2
import pytest

from actio_llm import ChatRequest, Finish, Ollama, ReasoningDelta, TextDelta, User

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
