"""Recorded turns: what the API streams, kept as files, for the web app's tests to read.

Each scenario runs a turn with known ids and a fixed clock, and compares the stream,
byte for byte, with a file in `fixtures/turns/`. The web app's test suite
(`apps/web/tests/contract/`) parses the same files with the real `ai` package, the
library the browser will read them with. So the two sides can't drift apart silently:

- change what the API streams, and the comparison here fails;
- record the files again, and the web app's test fails if the library can't make the
  expected message of them.

To record them again, after a change to the stream that is meant:

    ACTIO_RECORD_TURNS=1 uv run pytest apps/api/tests/test_turn_fixtures.py
    pnpm --dir apps/web run test

and review the difference in the files.
"""

import json
import os
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from actio_llm import Finish, FinishReason, TextDelta, Usage

pytestmark = pytest.mark.anyio

FIXTURES = Path(__file__).parent / "fixtures" / "turns"
RECORD = os.environ.get("ACTIO_RECORD_TURNS") == "1"

# What a browser would have chosen. The turn's and the answer's ids come from the
# test's `server_ids`: 01900000-0000-7000-8000-000000000001 and …002.
CONVERSATION = uuid.UUID("01900000-0000-7000-8000-0000000000c1")
QUESTION = uuid.UUID("01900000-0000-7000-8000-0000000000a1")


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


async def test_a_conversation_reads_back_as_it_was_recorded(api: httpx.AsyncClient) -> None:
    """The messages a browser gets when it opens a conversation again. The web app's
    test checks that the `ai` package accepts them as its own kind of message."""
    await turn(api, "[[think: Blue light scatters most.]] Why is the sky blue?", thinking=True)

    conversation = (await api.get(f"/api/conversations/{CONVERSATION}")).json()

    as_recorded("conversation.json", json.dumps(conversation, indent=2) + "\n")
