"""Recorded turns: what the API streams, kept as files, for the web app's tests to read.

Each scenario runs a turn with known ids and a fixed clock, and compares the stream,
byte for byte, with a file in `fixtures/turns/`. Two of the files are not streams: a
conversation read back, and the refusal of a turn at a usage limit. The web app's test
suite (`apps/web/tests/contract/`) parses the same files with the real `ai` package,
the library the browser will read them with, and its component tests answer with them.
So the two sides can't drift apart silently:

- change what the API streams, and the comparison here fails;
- record the files again, and the web app's test fails if the library can't make the
  expected message of them.

To record them again, after a change to the stream that is meant:

    ACTIO_RECORD_TURNS=1 uv run pytest apps/api/tests/test_turn_fixtures.py
    pnpm --dir apps/web run test

and review the difference in the files.
"""

import asyncio
import json
import os
import re
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import httpx2
import pytest
from valkey.asyncio import Valkey

from actio.api import turns as turn_routes
from actio.api import ui_stream
from actio.chat import events
from actio.core.settings import Settings
from actio_llm import Finish, FinishReason, ReasoningDelta, TextDelta, Timings, Usage

pytestmark = pytest.mark.anyio

FIXTURES = Path(__file__).parent / "fixtures" / "turns"
RECORD = os.environ.get("ACTIO_RECORD_TURNS") == "1"
PATIENCE_S = 5.0
DONE = Finish(FinishReason.STOP, Usage(input_tokens=10, output_tokens=5))

# What a browser would have chosen. The turn's and the answer's ids come from the
# test's `server_ids`: 01900000-0000-7000-8000-000000000001 and …002.
CONVERSATION = uuid.UUID("01900000-0000-7000-8000-0000000000c1")
QUESTION = uuid.UUID("01900000-0000-7000-8000-0000000000a1")
TURN = uuid.UUID("01900000-0000-7000-8000-000000000001")


def as_recorded(name: str, produced: str) -> None:
    """Compare `produced` with the file `name`, or write the file when recording."""
    path = FIXTURES / name
    if RECORD:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(produced)
        return
    assert path.exists(), f"{path} is missing. Record it: see this file's first lines."
    assert produced == path.read_text(), (
        f"What the API streams no longer matches {path.name}. If the change is meant, "
        "record the files again: see this file's first lines."
    )


async def turn(browser: httpx.AsyncClient, text: str, **more: Any) -> httpx.Response:
    await browser.post("/api/session/guest")
    message = {"id": str(QUESTION), "parts": [{"type": "text", "text": text}]}
    response = await browser.post(
        f"/api/conversations/{CONVERSATION}/turns", json={"message": message, **more}
    )
    assert response.status_code == 200, response.text
    return response


@pytest.mark.parametrize(
    ("name", "question", "more"),
    [
        ("answer", "Why is the sky blue?", {}),
        (
            "reasoning",
            "[[think: Blue light scatters most.]] Why is the sky blue?",
            {"thinking": True},
        ),
        ("model_unavailable", "[[fail: 503]] Hello?", {}),
        ("broke_off", "[[drop: 3]] one two three four five", {}),
        # The API passes markdown on as it is. This one is for the web app, which shows
        # it: here a piece ends in the middle of what is bold.
        (
            "markdown",
            "[[think: A *bike* has parts.]] Name **two bike** parts.",
            {"thinking": True},
        ),
    ],
)
async def test_a_turn_streams_what_was_recorded(
    api: httpx.AsyncClient, name: str, question: str, more: dict[str, Any]
) -> None:
    response = await turn(api, question, **more)

    as_recorded(f"{name}.sse", response.text)


async def test_an_answer_cut_off_at_the_limit_streams_what_was_recorded(scripted: Any) -> None:
    _, browser = await scripted(
        TextDelta("One, two, "),
        TextDelta("three"),
        Finish(FinishReason.LENGTH, Usage(input_tokens=10, output_tokens=5)),
    )

    response = await turn(browser, "Count to a hundred.")

    as_recorded("cut_off.sse", response.text)


async def test_a_thought_that_used_up_the_answer_streams_what_was_recorded(
    scripted: Any,
) -> None:
    """A model asked to think, that was still thinking when it reached the longest a
    single answer may be, far from the window's end: it wrote no answer. For the web
    app, which says so, and offers to ask again."""
    _, browser = await scripted(
        ReasoningDelta("Let me count them all, "),
        ReasoningDelta("one by one."),
        Finish(FinishReason.LENGTH, Usage(input_tokens=30, output_tokens=512)),
    )

    response = await turn(browser, "How many words are there?", thinking=True)

    as_recorded("thought_too_long.sse", response.text)


async def test_an_answer_that_says_what_it_took_streams_what_was_recorded(
    scripted: Any, clock: Any
) -> None:
    """Every figure the page shows under an answer: a model that says how long it took
    to write, as Ollama does, and a clock that moves between the question, the first
    word and the end. The numbers are worded at a glance:

    - 34 tokens written in the model's own 4 seconds: "8.5 tokens a second". By the
      clock, 2 seconds from the first word to the end, it would be 17: the page takes
      the model's time first;
    - the first word 1.2 seconds after the question: "first word after 1.2 s";
    - 1,200 tokens read and 34 written, at a credit a token: "1,234 credits".
    """
    _, browser = await scripted(
        lambda: clock.advance(seconds=1.2),
        TextDelta("Thirty-four tokens, "),
        TextDelta("in four seconds."),
        lambda: clock.advance(seconds=2),
        Finish(
            FinishReason.STOP,
            Usage(input_tokens=1_200, output_tokens=34),
            Timings(load_ms=10, prompt_ms=300, generation_ms=4_000, total_ms=4_310),
        ),
    )

    response = await turn(browser, "How fast was that?")

    as_recorded("figures.sse", response.text)


async def test_an_answer_of_no_words_that_was_counted_streams_what_was_recorded(
    scripted: Any,
) -> None:
    """A model that answered with nothing, and said what it had read: the turn completes,
    and costs what was read. For the web app, which says what an answer took under its
    words: here there are none to say it under."""
    _, browser = await scripted(Finish(FinishReason.STOP, Usage(input_tokens=30, output_tokens=0)))

    response = await turn(browser, "Say nothing.")

    as_recorded("no_words.sse", response.text)


async def test_an_answer_that_passes_half_of_a_usage_limit_streams_what_was_recorded(
    scripted: Any, plans_with: Any
) -> None:
    """The one chunk of Actio's own in the stream: a data part, which the library has to
    accept and hand to the page without making it part of the message."""
    _, browser = await scripted(
        TextDelta("That was "),
        TextDelta("half of it."),
        Finish(FinishReason.STOP, Usage(input_tokens=300, output_tokens=250)),
        plans=plans_with(guest=(1_000, 10_000)),
    )

    response = await turn(browser, "How much may I still ask?")

    as_recorded("usage_warning.sse", response.text)


async def test_an_answer_that_was_stopped_streams_what_was_recorded(scripted: Any) -> None:
    """What whoever reads along is sent when the answer is stopped: it ends as a whole
    answer does, and the turn in its last chunk says that it was stopped."""
    never = asyncio.Event()
    model, browser = await scripted(TextDelta("One, two, "), TextDelta("three"), never)
    asked = asyncio.create_task(turn(browser, "Count to a hundred."))
    await asyncio.wait_for(model.waiting.wait(), PATIENCE_S)

    stopped = await browser.post(f"/api/conversations/{CONVERSATION}/turns/{TURN}/cancel")
    response = await asyncio.wait_for(asked, PATIENCE_S)

    assert stopped.status_code == 200, stopped.text
    as_recorded("stopped.sse", response.text)


async def test_an_answer_that_ends_after_it_was_given_up_on_streams_what_was_recorded(
    scripted: Any, clock: Any
) -> None:
    """Its process was only slow: someone read the conversation a minute after the turn
    last said it was alive, and gave up on it. The answer that ends after that is not
    the turn's record, so its reader isn't told that it completed: the stream ends in an
    error, in the API's words, and the page reads the conversation."""
    hold = asyncio.Event()
    model, browser = await scripted(TextDelta("Sorry, "), hold, TextDelta("I'm late."), DONE)
    asked = asyncio.create_task(turn(browser, "Are you there?"))
    await asyncio.wait_for(model.waiting.wait(), PATIENCE_S)

    clock.advance(seconds=60)  # no beat in the meantime: under test they are 15 real seconds apart
    given_up_on = (await browser.get(f"/api/conversations/{CONVERSATION}")).json()
    hold.set()
    response = await asyncio.wait_for(asked, PATIENCE_S)

    assert given_up_on["unanswered"]["status"] == "interrupted"
    as_recorded("given_up.sse", response.text)


async def test_reading_along_with_a_turn_whose_stream_is_gone_streams_what_was_recorded(
    scripted: Any, settings: Settings, valkey_url: str
) -> None:
    """What whoever joins is sent when the turn's stream is gone while the turn runs on,
    as after a restart of Valkey: an error in the API's words, and the end. The page
    then reads the conversation instead."""
    hold = asyncio.Event()
    model, browser = await scripted(TextDelta("Half an "), hold, TextDelta("answer."), DONE)
    asked = asyncio.create_task(turn(browser, "Say half an answer."))
    await asyncio.wait_for(model.waiting.wait(), PATIENCE_S)

    async with Valkey.from_url(valkey_url) as valkey:
        await valkey.delete(f"{settings.valkey_prefix}:turn:{TURN}")
    joined = await asyncio.wait_for(
        browser.get(f"/api/conversations/{CONVERSATION}/turns/{TURN}/stream"), PATIENCE_S
    )
    # The turn is written to its end all the same.
    hold.set()
    await asyncio.wait_for(asked, PATIENCE_S)
    await asyncio.wait_for(model.closed.wait(), PATIENCE_S)

    assert joined.status_code == 200, joined.text
    as_recorded("lost.sse", joined.text)


async def test_an_answer_that_keeps_its_reader_waiting_streams_what_was_recorded(
    served: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While the model is silent, the reader is sent a line that is no event, so that
    nothing on the way closes the connection. The library has to skip it.

    This one takes a real connection, to see the line arrive before the model goes on.
    However many of these lines a silence brings, the file keeps one."""
    monkeypatch.setattr(events, "TICK", timedelta(milliseconds=10))
    monkeypatch.setattr(turn_routes, "KEEP_ALIVE_AFTER", timedelta(milliseconds=30))
    hold = asyncio.Event()
    _, address = await served(hold, TextDelta("Sorry to keep you "), TextDelta("waiting."), DONE)
    message = {"id": str(QUESTION), "parts": [{"type": "text", "text": "Are you there?"}]}
    keep_alive = ui_stream.KEEP_ALIVE.decode()

    async with httpx2.AsyncClient(timeout=PATIENCE_S) as browser:
        await browser.post(f"{address}/api/session/guest")
        asking = browser.stream(
            "POST", f"{address}/api/conversations/{CONVERSATION}/turns", json={"message": message}
        )
        async with asking as response, asyncio.timeout(PATIENCE_S):
            arrived = ""
            async for piece in response.aiter_text():
                arrived += piece
                if keep_alive in arrived:
                    hold.set()

    assert arrived.count(keep_alive) >= 1
    silence = re.compile(f"({re.escape(keep_alive)})+")
    as_recorded("keep_alive.sse", silence.sub(keep_alive, arrived))


async def test_a_question_refused_at_a_usage_limit_answers_what_was_recorded(
    scripted: Any, plans_with: Any
) -> None:
    """Not a stream: the refusal a page reads when a limit is reached. It names the
    window that is full, the moment it starts over, and the model that costs nothing:
    here `local`, offered beside the scripted one."""
    _, browser = await scripted(
        TextDelta("A long answer."),
        Finish(FinishReason.STOP, Usage(input_tokens=600, output_tokens=390)),
        plans=plans_with(guest=(1_000, 10_000), free_models=["local"]),
        also=["local"],
    )
    first = await turn(browser, "Name a bike part.")  # 990 of the session's 1,000
    [answer] = [line for line in first.text.splitlines() if '"type":"start"' in line]
    follow_up = {
        "id": "01900000-0000-7000-8000-0000000000a2",
        "parts": [{"type": "text", "text": "And one more?"}],
    }

    refused = await browser.post(
        f"/api/conversations/{CONVERSATION}/turns",
        json={
            "message": follow_up,
            "parent_id": json.loads(answer.removeprefix("data: "))["messageId"],
        },
    )

    assert refused.status_code == 429
    as_recorded("limit_reached.json", json.dumps(refused.json(), indent=2) + "\n")


async def test_a_conversation_reads_back_as_it_was_recorded(api: httpx.AsyncClient) -> None:
    """The messages a browser gets when it opens a conversation again. The web app's
    test checks that the `ai` package accepts them as its own kind of message."""
    await turn(api, "[[think: Blue light scatters most.]] Why is the sky blue?", thinking=True)

    conversation = (await api.get(f"/api/conversations/{CONVERSATION}")).json()

    as_recorded("conversation.json", json.dumps(conversation, indent=2) + "\n")
