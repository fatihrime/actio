"""What a turn's start waits for of its models' statuses (ADR 0028, revision "a status
that is known isn't waited for"): a status that is known is used without waiting, while
its server is asked again behind the request, and nothing is refused by an old one. The
list of models and a warm-up ask and wait, as before, so a question that follows one
finds its statuses fresh.

The models are scripted (`lineup`), and what each one's server says of it can change, or
be held, while the test runs (`Lineup.becomes`, `Lineup.hold`). The test's clock makes a
status old. The app runs in process, so a response comes back whole once its stream has
ended. Every request has a time limit, which fails a turn that waits for an answer held.
"""

import asyncio
import json
from typing import Any

import httpx
import pytest

from actio.core.ids import new_id
from actio.core.settings import Settings
from actio_llm import ConnectionFailedError, Finish, FinishReason, TextDelta, Usage

pytestmark = pytest.mark.anyio

DONE = Finish(FinishReason.STOP, Usage(input_tokens=5, output_tokens=4))
PATIENCE_S = 5.0
"""How long a test waits for the app to get somewhere. It takes milliseconds; this only
keeps an app that waits for an answer held for ever from hanging the run."""


def chunks(response: httpx.Response) -> list[Any]:
    """The chunks of a stream, in order, without the lines that are no events. The last
    one is the text `[DONE]`."""
    events = [event for event in response.text.split("\n\n") if event.startswith("data: ")]
    payloads = [event.removeprefix("data: ") for event in events]
    return [payload if payload == "[DONE]" else json.loads(payload) for payload in payloads]


def said(response: httpx.Response) -> str:
    """The text of an answer's stream."""
    return "".join(c["delta"] for c in chunks(response)[:-1] if c["type"] == "text-delta")


def turn_of(response: httpx.Response) -> dict[str, Any]:
    """The turn as the stream's last metadata says it was recorded."""
    ending = [c for c in chunks(response)[:-1] if c["type"] in ("finish", "message-metadata")]
    return ending[-1]["messageMetadata"]["turn"]  # type: ignore[no-any-return]


async def guest_on(start_app: Any, settings: Settings, lineup: Any) -> httpx.AsyncClient:
    """A guest's browser on an app that offers the models of `lineup`."""
    browser: httpx.AsyncClient = await start_app(settings, catalog=lineup.catalog())
    await browser.post("/api/session/guest")
    return browser


def asking(browser: httpx.AsyncClient) -> asyncio.Task[httpx.Response]:
    """Ask in a conversation of its own, and go on without waiting."""
    message = {"id": str(new_id()), "parts": [{"type": "text", "text": "Hello?"}]}
    return asyncio.create_task(
        browser.post(f"/api/conversations/{new_id()}/turns", json={"message": message})
    )


async def asked(browser: httpx.AsyncClient, at_once: int = 1) -> list[httpx.Response]:
    """`at_once` questions asked at the same moment, each in a conversation of its own,
    and their whole responses, within the time limit."""
    async with asyncio.timeout(PATIENCE_S):
        return list(await asyncio.gather(*(asking(browser) for _ in range(at_once))))


@pytest.mark.parametrize("look", ["the list of models", "a warm-up"])
async def test_a_question_after_a_look_finds_its_statuses_fresh_and_has_nobody_asked(
    look: str, start_app: Any, settings: Settings, migrated: str, lineup: Any, clock: Any
) -> None:
    """The statuses are 20 s old. The page reads the list of models when it loads, and
    asks for its model to be loaded when something is written in the box: either asks
    the servers again, and waits. A question 10 s after that has every status fresh,
    and has nobody asked."""
    assert migrated
    lineup.add("standard", TextDelta("Hi"), DONE, then=["second"])
    lineup.add("second", TextDelta("Never"), DONE)
    browser = await guest_on(start_app, settings, lineup)
    [first] = await asked(browser)
    clock.advance(seconds=20)

    async with asyncio.timeout(PATIENCE_S):
        if look == "the list of models":
            looked = await browser.get("/api/models")
        else:
            looked = await browser.post("/api/models/standard/warm-up")
    asked_by_the_look = list(lineup.asked)
    clock.advance(seconds=10)
    [later] = await asked(browser)

    assert first.status_code == 200
    assert looked.status_code in (200, 204), looked.text
    assert asked_by_the_look == ["standard", "second", "standard", "second"]
    assert later.status_code == 200, later.text
    assert said(later) == "Hi"
    assert lineup.asked == asked_by_the_look


async def test_a_question_doesnt_wait_for_a_stand_ins_server_while_its_model_is_known_ready(
    start_app: Any, settings: Settings, migrated: str, lineup: Any, clock: Any
) -> None:
    """The statuses are 20 s old, and the stand-in's server holds its answer, as one
    that never answers the connection would: two questions at once are answered by the
    model asked, and its server and the stand-in's are asked once behind them."""
    assert migrated
    lineup.add("standard", TextDelta("Hi"), DONE, then=["second"])
    lineup.add("second", TextDelta("Never"), DONE)
    browser = await guest_on(start_app, settings, lineup)
    [first] = await asked(browser)
    clock.advance(seconds=20)
    held = lineup.hold("second")

    later = await asked(browser, at_once=2)
    await asyncio.wait_for(held.asked.wait(), PATIENCE_S)

    for response in (first, *later):
        assert response.status_code == 200, response.text
        assert said(response) == "Hi"
        assert turn_of(response)["answered_by"] == "standard"
    assert lineup.asked == ["standard", "second", "standard", "second"]


async def test_a_chains_only_model_last_seen_unreachable_and_back_now_answers(
    start_app: Any, settings: Settings, migrated: str, lineup: Any, clock: Any
) -> None:
    """It isn't refused by its old status: two questions at once, and its server is
    asked again once, and waited for, and both are answered."""
    assert migrated
    lineup.add("standard", TextDelta("Hi"), DONE, state="unreachable")
    browser = await guest_on(start_app, settings, lineup)
    [refused] = await asked(browser)
    lineup.becomes("standard", "ready")
    clock.advance(seconds=20)

    later = await asked(browser, at_once=2)

    assert refused.status_code == 503
    assert refused.json()["model_status"] == "unreachable"
    assert [response.status_code for response in later] == [200, 200]
    assert [said(response) for response in later] == ["Hi", "Hi"]
    assert lineup.asked == ["standard", "standard"]


async def test_a_chains_only_model_last_seen_ready_whose_server_went_away_fails_its_turn(
    start_app: Any, settings: Settings, migrated: str, lineup: Any, clock: Any, asks_ended: Any
) -> None:
    """Its old status is used, and two questions start their turns while its server's
    answer is held: its server is away, so each fails as a turn does whose model's
    server can't be reached, asked once more first, and costs nothing. Its server was
    asked again behind them, once, and the next question is refused by that answer."""
    assert migrated
    model = lineup.add("standard", TextDelta("Hi"), DONE)
    browser = await guest_on(start_app, settings, lineup)
    [answered] = await asked(browser)
    model.script = [ConnectionFailedError("can't reach the model's server")]
    lineup.becomes("standard", "unreachable")
    clock.advance(seconds=20)
    held = lineup.hold("standard")

    went = await asked(browser, at_once=2)
    held.answers.set()
    await asks_ended()
    [refused] = await asked(browser)

    assert answered.status_code == 200
    for response in went:
        assert response.status_code == 200, response.text
        turn = turn_of(response)
        assert (turn["status"], turn["error"]) == ("failed", "model_unavailable")
        assert (turn["answered_by"], turn["credits"]) == (None, None)
    assert len(model.requests) == 1 + 2 * 2  # an answer, then two tries for each turn
    assert refused.status_code == 503
    assert (refused.json()["model"], refused.json()["model_status"]) == ("standard", "unreachable")
    assert lineup.asked == ["standard", "standard"]


async def test_none_of_a_chain_ready_is_refused_by_what_its_servers_say_now(
    start_app: Any, settings: Settings, migrated: str, lineup: Any, clock: Any
) -> None:
    """Both statuses are old and say that neither model is ready: two questions at once,
    and both servers are asked again once, and the 503 has the asked model's state as
    its server says it now, not as it was."""
    assert migrated
    lineup.add("standard", state="unreachable", then=["second"])
    lineup.add("second", state="unreachable")
    browser = await guest_on(start_app, settings, lineup)
    [before] = await asked(browser)
    lineup.becomes("standard", "missing")
    clock.advance(seconds=20)

    later = await asked(browser, at_once=2)

    assert before.status_code == 503
    assert before.json()["model_status"] == "unreachable"
    for response in later:
        assert response.status_code == 503
        assert (response.json()["model"], response.json()["model_status"]) == (
            "standard",
            "missing",
        )
    assert lineup.asked == ["standard", "second", "standard", "second"]
