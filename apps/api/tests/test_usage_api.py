"""Usage limits over HTTP: what a turn costs, when a question is refused, and
`GET /api/usage`.

A credit is a token here, read or written (`plans_for_tests` in conftest.py), so a turn
costs its tokens added up. Most tests use a `ScriptedModel`, whose last event says what
the turn used: the cost is then a number the test chose. Where the fake model serves,
it counts words.

How the limits themselves behave, below the API, is in `test_usage_limits.py`.
"""

import asyncio
import json
import logging
from datetime import timedelta
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from valkey.asyncio import Valkey
from valkey.exceptions import ConnectionError as ValkeyConnectionError

from actio.api.rate_limits import in_words
from actio.chat import turns
from actio.chat.models import Conversation, Message, Turn
from actio.core.ids import new_id
from actio.core.settings import Settings
from actio.identity import accounts
from actio.usage.limits import UsageLimits, held_for_prompt
from actio.usage.models import Uncounted, UsageEntry
from actio.usage.plans import Plans
from actio_llm import (
    Finish,
    FinishReason,
    ReasoningDelta,
    TextDelta,
    Timings,
    Usage,
    prompt_tokens,
    tokens_in,
)
from actio_quota.engine import ValkeyQuota

pytestmark = pytest.mark.anyio

PROBLEM = "application/problem+json"
PATIENCE_S = 5.0
ROOM = 512
"""The longest answer the scripted model's catalog allows: see `catalog_of`."""
DENIZ = {"email": "deniz@example.com", "password": "a long password"}


def used(read: int, written: int) -> Finish:
    """A model's last event: the answer is complete, and this is what it took."""
    return Finish(FinishReason.STOP, Usage(input_tokens=read, output_tokens=written))


@pytest.fixture
def tight(plans_with: Any) -> Plans:
    """A guest may use 1,000 credits a session, and 10,000 a week."""
    plans: Plans = plans_with(guest=(1_000, 10_000))
    return plans


# --- Helpers --------------------------------------------------------------------------


def chunks(response: httpx.Response) -> list[Any]:
    events = [event for event in response.text.split("\n\n") if event]
    payloads = [event.removeprefix("data: ") for event in events]
    return [payload if payload == "[DONE]" else json.loads(payload) for payload in payloads]


def turn_of(response: httpx.Response) -> dict[str, Any]:
    """The turn as the stream's last word on it: in `finish`, or before an `error`."""
    described = [c for c in chunks(response)[:-1] if c["type"] in ("finish", "message-metadata")]
    turn: dict[str, Any] = described[-1]["messageMetadata"]["turn"]
    return turn


def warnings_in(response: httpx.Response) -> list[tuple[str, int]]:
    return [
        (c["data"]["window"], c["data"]["percent"])
        for c in chunks(response)[:-1]
        if c["type"] == "data-usage-warning"
    ]


class Chat:
    """One conversation, as a browser would hold it."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.path = f"/api/conversations/{new_id()}"
        self.last_answer: str | None = None

    async def turn(self, body: dict[str, Any]) -> httpx.Response:
        response = await self.client.post(f"{self.path}/turns", json=body)
        if response.status_code == 200:
            [start] = [c for c in chunks(response)[:-1] if c["type"] == "start"]
            self.last_answer = start["messageId"]
        return response

    async def ask(self, text: str = "Name a bike part.") -> httpx.Response:
        """Ask `text`, after the last answer of this conversation if it has one."""
        message = {"id": str(new_id()), "parts": [{"type": "text", "text": text}]}
        return await self.turn({"message": message, "parent_id": self.last_answer})


async def usage(client: httpx.AsyncClient) -> dict[str, Any]:
    response = await client.get("/api/usage")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def windows(client: httpx.AsyncClient) -> dict[str, tuple[int, int, str | None]]:
    """Each window as (used, reserved, resets_at)."""
    return {
        w["name"]: (w["used"], w["reserved"], w["resets_at"])
        for w in (await usage(client))["windows"]
    }


async def count(db: AsyncSession, table: type) -> int:
    return await db.scalar(select(func.count()).select_from(table)) or 0


async def ledger(db: AsyncSession) -> list[UsageEntry]:
    return list((await db.scalars(select(UsageEntry).order_by(UsageEntry.occurred_at))).all())


# --- GET /api/usage -------------------------------------------------------------------


async def test_usage_needs_a_session(api: httpx.AsyncClient) -> None:
    response = await api.get("/api/usage")

    assert response.status_code == 401
    assert response.headers["content-type"] == PROBLEM


async def test_a_new_guest_has_a_plan_and_has_used_nothing(api: httpx.AsyncClient) -> None:
    await api.post("/api/session/guest")

    response = await api.get("/api/usage")

    assert response.status_code == 200
    assert response.json() == {
        "plan": {"id": "guest", "title": "Guest"},
        "windows": [
            # Not open yet: the first question that costs something opens it.
            {"name": "session", "limit": 1_000_000, "used": 0, "reserved": 0, "resets_at": None},
            {
                "name": "week",
                "limit": 10_000_000,
                "used": 0,
                "reserved": 0,
                "resets_at": "2026-11-16T00:00:00Z",  # the Monday after the test clock's day
            },
        ],
    }


async def test_what_a_turn_cost_is_on_the_turn_in_the_usage_and_in_the_ledger(
    scripted: Any, db: AsyncSession
) -> None:
    _, browser = await scripted(TextDelta("A derailleur."), used(read=40, written=3))
    chat = Chat(browser)

    response = await chat.ask()

    turn = turn_of(response)
    assert (turn["credits"], turn["capped_by_usage"]) == (43, False)
    assert await windows(browser) == {
        "session": (43, 0, "2026-11-10T17:00:00Z"),  # five hours after it opened
        "week": (43, 0, "2026-11-16T00:00:00Z"),
    }
    stored = (await browser.get(chat.path)).json()["messages"][1]["metadata"]["turn"]
    assert stored == turn  # read back, it says the same
    [entry] = await ledger(db)
    assert str(entry.reservation_id) == turn["id"]  # a turn is paid for under its own id
    assert (entry.model, entry.credits, entry.estimated) == ("standard", 43, False)
    assert (entry.input_tokens, entry.cached_input_tokens, entry.output_tokens) == (40, 0, 3)
    assert set(entry.windows) == {"session", "week"}


async def test_the_fake_model_counts_words_and_a_turn_costs_them(api: httpx.AsyncClient) -> None:
    """The same through the model the end-to-end stack runs, which reports its counts
    the way a real server does."""
    await api.post("/api/session/guest")

    response = await Chat(api).ask("hello there")

    turn = turn_of(response)
    assert turn["output_tokens"] == 3  # "Echo: hello there"
    assert turn["credits"] == turn["input_tokens"] + 3
    assert (await windows(api))["session"][0] == turn["credits"]


async def test_credits_are_held_while_an_answer_is_being_written(scripted: Any) -> None:
    hold = asyncio.Event()
    model, browser = await scripted(
        TextDelta("Half"), hold, TextDelta(" an answer."), used(read=40, written=4)
    )

    asked = asyncio.create_task(Chat(browser).ask())
    await asyncio.wait_for(model.started.wait(), PATIENCE_S)
    during = await windows(browser)
    hold.set()
    await asyncio.wait_for(asked, PATIENCE_S)  # an ending that hung would wait for ever

    held = held_for_prompt(prompt_tokens(model.requests[0].messages)) + ROOM
    assert during["session"][:2] == (0, held)  # the prompt, by guess, and the longest answer
    assert during["week"][:2] == (0, held)
    assert (await windows(browser))["session"][:2] == (44, 0)


async def test_signing_up_changes_the_plan_and_keeps_what_was_used(
    scripted: Any, plans_with: Any
) -> None:
    plans = plans_with(guest=(1_000, 5_000), account=(20_000, 90_000))
    _, browser = await scripted(TextDelta("A derailleur."), used(read=40, written=3), plans=plans)
    await Chat(browser).ask()
    as_a_guest = await usage(browser)

    signed_up = await browser.post("/api/account", json=DENIZ)

    assert signed_up.status_code == 201
    as_an_account = await usage(browser)
    assert as_a_guest["plan"] == {"id": "guest", "title": "Guest"}
    assert as_an_account["plan"] == {"id": "member", "title": "Member"}
    assert [(w["limit"], w["used"]) for w in as_a_guest["windows"]] == [(1_000, 43), (5_000, 43)]
    assert [(w["limit"], w["used"]) for w in as_an_account["windows"]] == [
        (20_000, 43),
        (90_000, 43),
    ]


async def test_a_plan_without_limits_has_no_windows_and_refuses_nothing(
    scripted: Any, plans_with: Any
) -> None:
    _, browser = await scripted(
        TextDelta("A long answer."), used(read=900_000, written=ROOM), plans=plans_with(guest=None)
    )
    chat = Chat(browser)

    answers = [await chat.ask(), await chat.ask("And then?")]

    assert [answer.status_code for answer in answers] == [200, 200]
    assert turn_of(answers[1])["credits"] == 900_000 + ROOM  # counted all the same
    assert await usage(browser) == {"plan": {"id": "guest", "title": "Guest"}, "windows": []}


async def test_without_valkey_usage_cannot_be_read(
    start_app: Any, settings: Settings, migrated: str
) -> None:
    assert migrated
    browser = await start_app(settings)
    await browser.post("/api/session/guest")
    cut_off = settings.model_copy(update={"valkey_url": SecretStr("valkey://127.0.0.1:1/0")})
    stranded = await start_app(cut_off)
    stranded.cookies.set("actio_session", browser.cookies["actio_session"])

    response = await stranded.get("/api/usage")

    assert response.status_code == 503
    assert response.headers["content-type"] == PROBLEM
    assert response.json()["detail"] == "Usage can't be counted right now. Try again in a moment."


# --- A question that doesn't fit ------------------------------------------------------


async def test_a_question_that_no_longer_fits_is_refused_and_leaves_nothing_behind(
    scripted: Any, tight: Plans, db: AsyncSession
) -> None:
    model, browser = await scripted(
        TextDelta("A long answer."), used(read=600, written=390), plans=tight
    )
    await Chat(browser).ask()  # 990 of the session's 1,000
    before = await windows(browser)
    late = Chat(browser)

    refused = await late.ask("And one more thing?")

    assert refused.status_code == 429
    assert refused.headers["content-type"] == PROBLEM
    assert refused.headers["retry-after"] == str(5 * 3600)
    assert refused.json() == {
        "type": "about:blank",
        "title": "Too Many Requests",
        "status": 429,
        "detail": "You've reached your usage limit for this session. It resets in 5 hours.",
        "retry_after": 5 * 3600,
        "window": "session",
        "resets_at": "2026-11-10T17:00:00Z",
        "alternatives": [],
    }
    # The question was never asked: no conversation, no message, no turn, nothing held.
    assert (await browser.get(late.path)).status_code == 404
    assert (await count(db, Conversation), await count(db, Message), await count(db, Turn)) == (
        1,
        2,
        1,
    )
    assert len(model.requests) == 1
    assert await windows(browser) == before
    assert len(await ledger(db)) == 1


async def test_a_refused_question_can_be_sent_again_once_the_session_has_started_over(
    scripted: Any, tight: Plans, clock: Any
) -> None:
    _, browser = await scripted(
        TextDelta("A long answer."), used(read=600, written=390), plans=tight
    )
    await Chat(browser).ask()
    late = Chat(browser)
    question = {"message": {"id": str(new_id()), "parts": [{"type": "text", "text": "Again?"}]}}

    too_early = await late.turn(question)
    clock.advance(hours=5)
    in_time = await late.turn(question)  # the very same message: it was never stored

    assert (too_early.status_code, in_time.status_code) == (429, 200)
    assert (await windows(browser))["session"] == (990, 0, "2026-11-10T22:00:00Z")  # a new one
    assert (await windows(browser))["week"][0] == 1_980


async def test_a_refused_follow_up_leaves_its_conversation_as_it_was(
    scripted: Any, tight: Plans
) -> None:
    _, browser = await scripted(
        TextDelta("A long answer."), used(read=600, written=390), plans=tight
    )
    chat = Chat(browser)
    await chat.ask()
    before = (await browser.get(chat.path)).json()

    refused = await chat.ask("And then?")
    again = await chat.turn({"answer_to": before["messages"][0]["id"]})

    assert (refused.status_code, again.status_code) == (429, 429)  # asking again costs too
    assert (await browser.get(chat.path)).json() == before


async def test_near_the_limit_the_model_is_given_less_room_and_the_turn_says_so(
    scripted: Any, tight: Plans
) -> None:
    model, browser = await scripted(
        TextDelta("An answer."), used(read=300, written=200), plans=tight
    )
    chat = Chat(browser)

    roomy = await chat.ask()
    cramped = await chat.ask("And then?")

    first, second = model.requests
    left = 500 - held_for_prompt(prompt_tokens(second.messages))  # after what the prompt may cost
    assert 16 < left < ROOM
    assert (first.max_output_tokens, turn_of(roomy)["capped_by_usage"]) == (ROOM, False)
    assert (second.max_output_tokens, turn_of(cramped)["capped_by_usage"]) == (left, True)
    [start] = [c for c in chunks(cramped)[:-1] if c["type"] == "start"]
    assert start["messageMetadata"]["turn"]["capped_by_usage"] is True  # known from the start


async def test_a_message_larger_than_the_whole_allowance_is_refused_for_good(
    scripted: Any, plans_with: Any
) -> None:
    _, browser = await scripted(
        TextDelta("An answer."), used(1, 1), plans=plans_with(guest=(50, 50))
    )

    refused = await Chat(browser).ask()

    assert refused.status_code == 429
    assert "retry-after" not in refused.headers  # waiting won't help
    assert refused.json() == {
        "type": "about:blank",
        "title": "Too Many Requests",
        "status": 429,
        "detail": "This message alone needs more than your plan allows in a session.",
        "window": "session",
        "resets_at": None,
        "alternatives": [],
    }


async def test_the_week_is_a_limit_of_its_own(scripted: Any, plans_with: Any, clock: Any) -> None:
    plans = plans_with(guest=(1_000, 1_000))
    _, browser = await scripted(
        TextDelta("A long answer."), used(read=600, written=390), plans=plans
    )
    await Chat(browser).ask()
    clock.advance(hours=5)  # a new session, in the same week

    refused = await Chat(browser).ask()

    assert refused.status_code == 429
    body = refused.json()
    assert body["detail"] == "You've reached your usage limit for this week. It resets in 6 days."
    assert (body["window"], body["resets_at"]) == ("week", "2026-11-16T00:00:00Z")
    assert body["retry_after"] == int(refused.headers["retry-after"]) == 5 * 86_400 + 7 * 3_600


async def test_an_account_is_not_held_to_a_guests_limits(scripted: Any, plans_with: Any) -> None:
    plans = plans_with(guest=(1_000, 10_000), account=(5_000, 50_000))
    _, browser = await scripted(
        TextDelta("A long answer."), used(read=600, written=390), plans=plans
    )
    chat = Chat(browser)
    await chat.ask()

    as_a_guest = await chat.ask("And then?")
    await browser.post("/api/account", json=DENIZ)
    as_an_account = await chat.ask("And then?")

    assert (as_a_guest.status_code, as_an_account.status_code) == (429, 200)


async def test_a_model_that_costs_nothing_is_never_refused(
    scripted: Any, plans_with: Any, db: AsyncSession
) -> None:
    plans = plans_with(guest=(0, 0), free=True)
    _, browser = await scripted(TextDelta("An answer."), used(read=600, written=390), plans=plans)
    chat = Chat(browser)

    answers = [await chat.ask(), await chat.ask("And then?")]

    assert [answer.status_code for answer in answers] == [200, 200]
    assert [turn_of(answer)["credits"] for answer in answers] == [0, 0]
    assert [(entry.credits, entry.output_tokens) for entry in await ledger(db)] == [(0, 390)] * 2
    assert (await windows(browser))["week"][:2] == (0, 0)


async def test_a_refusal_names_the_model_that_costs_nothing_and_that_model_answers(
    scripted: Any, plans_with: Any
) -> None:
    plans = plans_with(guest=(1_000, 10_000), free_models=["local"])
    _, browser = await scripted(
        TextDelta("A long answer."), used(read=600, written=390), plans=plans, also=["local"]
    )
    chat = Chat(browser)
    await chat.ask()  # 990 of the session's 1,000
    again = {"id": str(new_id()), "parts": [{"type": "text", "text": "And then?"}]}
    question = {"message": again, "parent_id": chat.last_answer}

    refused = await chat.turn(question)
    answered = await chat.turn(question | {"model": refused.json()["alternatives"][0]})

    assert (refused.status_code, refused.json()["alternatives"]) == (429, ["local"])
    assert (answered.status_code, turn_of(answered)["model"]) == (200, "local")
    assert turn_of(answered)["credits"] == 0
    assert (await windows(browser))["session"][:2] == (990, 0)  # as before


def test_a_long_wait_is_said_in_days() -> None:
    hour, day = 3_600, 86_400

    assert [in_words(seconds) for seconds in (5 * hour, 47 * hour, 48 * hour, 6 * day + 1)] == [
        "5 hours",
        "47 hours",
        "2 days",
        "7 days",  # rounded up: nobody should come back too early
    ]


# --- An account's own plan ------------------------------------------------------------


@pytest.fixture
def with_dev(plans_with: Any) -> Plans:
    """An account may use 1,000 credits a session and 10,000 a week, and one on the plan
    `dev` of its own 5,000 and 50,000."""
    plans: Plans = plans_with(
        account=(1_000, 10_000),
        plans={"dev": {"title": "Developer", "session_credits": 5_000, "week_credits": 50_000}},
    )
    return plans


async def put_on(db: AsyncSession, plan: str | None) -> None:
    """Put the test's account on `plan` of its own, as `actio plan` does, or with `None`
    take its own plan away."""
    await accounts.put_on_plan(db, email=DENIZ["email"], plan=plan)
    await db.commit()


async def test_an_account_put_on_a_larger_plan_has_its_next_question_admitted_with_what_it_used(
    scripted: Any, with_dev: Plans, db: AsyncSession
) -> None:
    """The session's limit, reached on the accounts' plan, is no longer reached on the
    account's own: from its next request, with what it used in the session that is open."""
    _, browser = await scripted(
        TextDelta("A long answer."), used(read=600, written=390), plans=with_dev
    )
    await browser.post("/api/account", json=DENIZ)
    chat = Chat(browser)
    await chat.ask()  # 990 of the session's 1,000
    refused = await chat.ask("And then?")

    await put_on(db, "dev")
    on_its_own = await usage(browser)
    admitted = await chat.ask("And then?")

    assert refused.status_code == 429
    assert on_its_own == {
        "plan": {"id": "dev", "title": "Developer"},
        "windows": [
            {
                "name": "session",
                "limit": 5_000,
                "used": 990,
                "reserved": 0,
                "resets_at": "2026-11-10T17:00:00Z",  # the session that opened at noon
            },
            {
                "name": "week",
                "limit": 50_000,
                "used": 990,
                "reserved": 0,
                "resets_at": "2026-11-16T00:00:00Z",
            },
        ],
    }
    assert admitted.status_code == 200
    assert (await windows(browser))["session"] == (1_980, 0, "2026-11-10T17:00:00Z")


async def test_an_account_put_back_on_a_smaller_plan_is_refused_by_the_window_it_has_open(
    scripted: Any, with_dev: Plans, db: AsyncSession, clock: Any
) -> None:
    """The other way: it used more on its own plan, in the session that is open, than
    the accounts' plan allows. Its next question is refused until that session starts
    over, not one that would open now."""
    _, browser = await scripted(
        TextDelta("A long answer."), used(read=600, written=390), plans=with_dev
    )
    await browser.post("/api/account", json=DENIZ)
    await put_on(db, "dev")
    chat = Chat(browser)
    answered = [await chat.ask(), await chat.ask("And then?")]  # 1,980 of 5,000
    clock.advance(hours=1)

    await put_on(db, None)
    back = await usage(browser)
    refused = await chat.ask("And then?")

    assert [answer.status_code for answer in answered] == [200, 200]
    assert back["plan"] == {"id": "member", "title": "Member"}
    assert [(w["limit"], w["used"], w["resets_at"]) for w in back["windows"]] == [
        (1_000, 1_980, "2026-11-10T17:00:00Z"),
        (10_000, 1_980, "2026-11-16T00:00:00Z"),
    ]
    assert refused.status_code == 429
    assert refused.headers["retry-after"] == str(4 * 3600)
    body = refused.json()
    assert (
        body["detail"] == "You've reached your usage limit for this session. It resets in 4 hours."
    )
    assert (body["window"], body["resets_at"]) == ("session", "2026-11-10T17:00:00Z")


async def test_an_account_on_a_plan_the_file_does_not_have_is_held_to_the_accounts_plan(
    scripted: Any, with_dev: Plans, db: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    """The file was changed, and the account's row was not: its answers go by the
    accounts' plan, as if it had none of its own, and the log says so once."""
    _, browser = await scripted(
        TextDelta("A long answer."), used(read=600, written=390), plans=with_dev
    )
    await browser.post("/api/account", json=DENIZ)
    await put_on(db, "gold")  # nothing below the plans knows which plans the file has
    chat = Chat(browser)

    read = await usage(browser)
    answers = [await chat.ask(), await chat.ask("And then?")]

    assert read["plan"] == {"id": "member", "title": "Member"}
    assert [answer.status_code for answer in answers] == [200, 429]  # 990 of 1,000, then 990
    said = [(r.levelno, r.getMessage()) for r in caplog.records if r.name == "actio.usage.plans"]
    assert said == [
        (
            logging.WARNING,
            'an account is on the plan "gold", which the plans file doesn\'t have: it has'
            ' "member" instead, the plan the file names for accounts, and so does any other'
            ' account on "gold"',
        )
    ]


async def test_a_guest_who_signs_up_has_no_plan_of_its_own_and_has_the_accounts_plan(
    scripted: Any, with_dev: Plans, db: AsyncSession
) -> None:
    """The guest's row becomes the account, and a guest has no plan of its own: the
    database refuses one (`test_accounts.py`). So the account has none either, beside a
    plan in the file that no account has by itself, until it is put on one."""
    _, browser = await scripted(
        TextDelta("A derailleur."), used(read=40, written=3), plans=with_dev
    )

    signed_up = await browser.post("/api/account", json=DENIZ)

    assert signed_up.status_code == 201
    assert await accounts.own_plan(db, email=DENIZ["email"]) is None
    assert (await usage(browser))["plan"] == {"id": "member", "title": "Member"}


# --- What each way of ending costs ----------------------------------------------------


@pytest.mark.parametrize(
    ("question", "kept"),
    [("[[fail: 503]] Hello?", False), ("[[drop: 2]] one two three four", True)],
    ids=["before a word", "in the middle"],
)
async def test_a_failed_turn_costs_nothing(
    start_app: Any,
    settings: Settings,
    migrated: str,
    tight: Plans,
    db: AsyncSession,
    question: str,
    kept: bool,
) -> None:
    """The fault was the model's. What it had written is kept, and not charged."""
    assert migrated
    browser = await start_app(settings, plans=tight)
    await browser.post("/api/session/guest")
    chat = Chat(browser)

    response = await chat.ask(question)

    turn = turn_of(response)
    assert (turn["status"], turn["credits"]) == ("failed", None)
    stored = (await browser.get(chat.path)).json()
    assert (len(stored["messages"]) == 2) is kept
    assert await ledger(db) == []
    assert (await windows(browser))["session"][:2] == (0, 0)  # what was held is back


async def test_a_turn_whose_model_reports_no_counts_is_counted_by_guess(
    scripted: Any, db: AsyncSession
) -> None:
    model, browser = await scripted(
        ReasoningDelta("Three words will do."),
        TextDelta("Three little words"),
        Finish(FinishReason.STOP, None),
    )

    response = await Chat(browser).ask()

    written = tokens_in("Three words will do.") + tokens_in("Three little words")
    # The guess at the prompt itself: the margin is for holding, not for charging.
    guessed = prompt_tokens(model.requests[0].messages) + written
    turn = turn_of(response)
    assert (turn["status"], turn["credits"]) == ("completed", guessed)
    assert turn["input_tokens"] is None  # the turn keeps only what a provider counted
    [entry] = await ledger(db)
    assert (entry.credits, entry.estimated, entry.output_tokens) == (guessed, True, written)


@pytest.mark.parametrize(
    "reported",
    [
        Usage(input_tokens=-1, output_tokens=3),
        Usage(input_tokens=40, output_tokens=-3),
        Usage(input_tokens=40, output_tokens=3, cached_input_tokens=-1),
        Usage(input_tokens=2**31, output_tokens=3),
        Usage(input_tokens=40, output_tokens=2**31),
        Usage(input_tokens=40, output_tokens=0),
    ],
    ids=[
        "read less than nothing",
        "wrote less than nothing",
        "less than nothing from its cache",
        "read more than a row holds",
        "wrote more than a row holds",
        "wrote nothing, beside an answer with words",
    ],
)
async def test_a_count_that_cannot_be_right_is_no_count_and_the_turn_is_counted_by_guess(
    scripted: Any, db: AsyncSession, caplog: pytest.LogCaptureFixture, reported: Usage
) -> None:
    """A model's server's counts are someone else's numbers. One that can't be right
    must not keep the answer from being recorded, and is not written down: the turn is
    counted as one whose server counted nothing."""
    model, browser = await scripted(
        TextDelta("Three little words"), Finish(FinishReason.STOP, reported)
    )
    chat = Chat(browser)

    response = await chat.ask()

    written = tokens_in("Three little words")
    guessed = prompt_tokens(model.requests[0].messages) + written
    turn = turn_of(response)
    assert (response.status_code, turn["status"], turn["credits"]) == (200, "completed", guessed)
    assert (turn["input_tokens"], turn["output_tokens"]) == (None, None)
    stored = (await browser.get(chat.path)).json()
    assert [message["role"] for message in stored["messages"]] == ["user", "assistant"]
    kept = stored["messages"][1]["metadata"]["turn"]  # the turn's own record keeps none
    counts = (kept["input_tokens"], kept["cached_input_tokens"], kept["output_tokens"])
    assert counts == (None, None, None)
    [entry] = await ledger(db)
    assert (entry.credits, entry.estimated, entry.output_tokens) == (guessed, True, written)
    assert (await windows(browser))["session"][:2] == (guessed, 0)
    said = [(r.levelno, r.getMessage()) for r in caplog.records if r.name == "actio.chat.answering"]
    assert said == [
        (
            logging.WARNING,
            f"turn {turn['id']}: its model's server reported counts that can't be right "
            f"(read {reported.input_tokens}, {reported.cached_input_tokens} of that from its "
            f"cache, wrote {reported.output_tokens}); it is counted by guess",
        )
    ]


async def test_a_count_that_costs_more_than_the_counters_count_is_no_count_either(
    scripted: Any, plans_with: Any, db: AsyncSession
) -> None:
    """Each of its numbers fits in a row. At five credits a token their cost is more
    than the engine counts exactly, and the settle of such a row would be refused."""
    model, browser = await scripted(
        TextDelta("Three little words"),
        used(read=40, written=1_900_000_000),
        plans=plans_with(credits_a_token=5),
    )

    response = await Chat(browser).ask()

    guessed = 5 * (prompt_tokens(model.requests[0].messages) + tokens_in("Three little words"))
    turn = turn_of(response)
    assert (response.status_code, turn["status"], turn["credits"]) == (200, "completed", guessed)
    assert turn["output_tokens"] is None
    [entry] = await ledger(db)
    assert (entry.credits, entry.estimated) == (guessed, True)
    assert (await windows(browser))["session"][:2] == (guessed, 0)
    assert await count(db, Uncounted) == 0


async def test_a_model_that_says_it_wrote_nothing_and_wrote_nothing_is_believed(
    scripted: Any, db: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    """Nothing written is a count like any other, as long as the answer has no words."""
    _, browser = await scripted(used(read=40, written=0))

    response = await Chat(browser).ask()

    turn = turn_of(response)
    assert (turn["status"], turn["credits"], turn["input_tokens"]) == ("completed", 40, 40)
    [entry] = await ledger(db)
    assert (entry.credits, entry.estimated) == (40, False)
    assert "can't be right" not in caplog.text


async def test_a_count_of_nothing_written_is_believed_beside_an_answer_without_words(
    scripted: Any, db: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    """Two line breaks are an answer, and no words: Actio's own guess counts no token in
    them. Counting such a turn by guess would make it cost nothing, where its server
    counted what was read."""
    _, browser = await scripted(TextDelta("\n\n"), used(read=40, written=0))

    response = await Chat(browser).ask()

    turn = turn_of(response)
    assert (turn["status"], turn["credits"], turn["input_tokens"]) == ("completed", 40, 40)
    [entry] = await ledger(db)
    assert (entry.credits, entry.estimated) == (40, False)
    assert "can't be right" not in caplog.text


@pytest.mark.parametrize(
    "reported",
    [
        Timings(load_ms=2**31, prompt_ms=120, generation_ms=75, total_ms=2**31 + 195),
        Timings(load_ms=1800, prompt_ms=-1, generation_ms=75, total_ms=1874),
        Timings(load_ms=1800, prompt_ms=120, generation_ms=75, total_ms=2**31),
    ],
    ids=["longer to load than a row holds", "less than no time to read", "longer in all"],
)
async def test_times_that_cannot_be_right_are_not_kept_and_the_turn_ends_as_ever(
    scripted: Any, db: AsyncSession, caplog: pytest.LogCaptureFixture, reported: Timings
) -> None:
    """Where the time went is a model's server's own account too. One that a turn's
    record can't hold is left out, and nothing else about the turn changes: its count
    is believed, and it costs what it used."""
    _, browser = await scripted(
        TextDelta("A derailleur."),
        Finish(FinishReason.STOP, Usage(input_tokens=40, output_tokens=3), reported),
    )
    chat = Chat(browser)

    response = await chat.ask()

    turn = turn_of(response)
    assert (response.status_code, turn["status"], turn["credits"]) == (200, "completed", 43)
    assert (turn["load_ms"], turn["prompt_ms"], turn["generation_ms"]) == (None, None, None)
    stored = (await browser.get(chat.path)).json()["messages"][1]["metadata"]["turn"]
    assert stored == turn
    [entry] = await ledger(db)
    assert (entry.credits, entry.estimated) == (43, False)
    said = [(r.levelno, r.getMessage()) for r in caplog.records if r.name == "actio.chat.answering"]
    assert said == [
        (
            logging.WARNING,
            f"turn {turn['id']}: its model's server reported times that can't be right "
            f"({reported.load_ms} ms to load, {reported.prompt_ms} to read, "
            f"{reported.generation_ms} to write, {reported.total_ms} in all); none is kept",
        )
    ]


async def test_times_a_models_server_reports_are_kept_on_the_turn(scripted: Any) -> None:
    told = Timings(load_ms=1800, prompt_ms=120, generation_ms=75, total_ms=2000)
    _, browser = await scripted(
        TextDelta("A derailleur."),
        Finish(FinishReason.STOP, Usage(input_tokens=40, output_tokens=3), told),
    )

    turn = turn_of(await Chat(browser).ask())

    assert (turn["load_ms"], turn["prompt_ms"], turn["generation_ms"]) == (1800, 120, 75)


async def test_passing_a_share_of_a_limit_is_announced_once(scripted: Any, plans_with: Any) -> None:
    plans = plans_with(guest=(1_000, 100_000))
    model, browser = await scripted(
        TextDelta("An answer."), used(read=300, written=250), plans=plans
    )
    chat = Chat(browser)

    over_half = await chat.ask()  # 550 of 1,000
    model.script = [TextDelta("An answer."), used(read=100, written=50)]
    still_over_half = await chat.ask("And then?")  # 700
    model.script = [TextDelta("An answer."), used(read=150, written=100)]
    nearly_all = await chat.ask("And then?")  # 950

    assert warnings_in(over_half) == [("session", 50)]
    assert warnings_in(still_over_half) == []
    assert warnings_in(nearly_all) == [("session", 75), ("session", 90)]
    # It comes when the answer is complete and counted, just before the stream finishes.
    kinds = [c["type"] for c in chunks(over_half)[:-1]]
    assert kinds[-4:] == ["text-end", "data-usage-warning", "finish-step", "finish"]
    [warning] = [c for c in chunks(over_half)[:-1] if c["type"] == "data-usage-warning"]
    assert warning == {
        "type": "data-usage-warning",
        "data": {"window": "session", "percent": 50},
        "transient": True,
    }


# --- Answers that are stopped, and answers that take long -----------------------------


async def stop(chat: Chat) -> dict[str, Any]:
    """Stop the answer that is being written in a conversation. Returns its turn, as it
    was recorded."""
    writing = (await chat.client.get(chat.path)).json()["unanswered"]
    stopped = await chat.client.post(f"{chat.path}/turns/{writing['id']}/cancel")
    assert stopped.status_code == 200, stopped.text
    turn: dict[str, Any] = stopped.json()
    return turn


async def test_a_stopped_answer_is_counted_by_guess_for_what_had_arrived(
    scripted: Any, db: AsyncSession
) -> None:
    """Stopping an answer at its last word doesn't make it free. Nor does the model get
    to say what it used: a stream that is closed never sends its count."""
    never = asyncio.Event()
    model, browser = await scripted(
        ReasoningDelta("Let me think."),
        TextDelta("Half an answer"),
        never,
        TextDelta(" that nobody will read."),
        used(read=40, written=9),
    )
    chat = Chat(browser)
    asked = asyncio.create_task(chat.ask("Tell me something."))
    await asyncio.wait_for(model.waiting.wait(), PATIENCE_S)

    stopped = await stop(chat)
    after = await windows(browser)  # asked right after: the stop is counted already
    await asyncio.wait_for(asked, PATIENCE_S)  # a stream that didn't end would wait for ever

    guessed = (
        prompt_tokens(model.requests[0].messages)
        + tokens_in("Let me think.")
        + tokens_in("Half an answer")
    )
    assert (stopped["status"], stopped["credits"]) == ("stopped", guessed)
    assert (after["session"][:2], after["week"][:2]) == ((guessed, 0), (guessed, 0))
    [entry] = await ledger(db)
    assert (entry.credits, entry.estimated) == (guessed, True)
    assert turn_of(asked.result()) == stopped  # whoever read along was told the same


async def test_an_answer_stopped_before_its_first_word_costs_nothing(
    scripted: Any, db: AsyncSession
) -> None:
    never = asyncio.Event()
    model, browser = await scripted(never, TextDelta("Too late."), used(read=40, written=2))
    chat = Chat(browser)
    asked = asyncio.create_task(chat.ask("Tell me something."))
    await asyncio.wait_for(model.waiting.wait(), PATIENCE_S)

    stopped = await stop(chat)
    after = await windows(browser)
    await asyncio.wait_for(asked, PATIENCE_S)

    assert (stopped["status"], stopped["credits"]) == ("stopped", None)
    assert (after["session"][:2], after["week"][:2]) == ((0, 0), (0, 0))
    assert await ledger(db) == []


async def test_deleting_a_conversation_stops_its_answer_which_is_counted_like_any_stopped_one(
    scripted: Any, db: AsyncSession
) -> None:
    """What was used was used, whatever became of the conversation. And an answer
    nobody can read any more isn't written to its end at its account's cost."""
    never = asyncio.Event()
    model, browser = await scripted(
        TextDelta("Half an "), never, TextDelta("answer."), used(read=40, written=4)
    )
    chat = Chat(browser)
    asked = asyncio.create_task(chat.ask("Tell me something."))
    await asyncio.wait_for(model.waiting.wait(), PATIENCE_S)

    deleted = await browser.delete(chat.path)
    after = await windows(browser)
    await asyncio.wait_for(asked, PATIENCE_S)

    guessed = prompt_tokens(model.requests[0].messages) + tokens_in("Half an ")
    assert deleted.status_code == 204
    assert after["session"][:2] == (guessed, 0)
    assert [(entry.credits, entry.estimated) for entry in await ledger(db)] == [(guessed, True)]
    assert await count(db, Turn) == 0


async def test_the_credits_held_for_a_long_answer_stay_held_until_it_ends(
    scripted: Any, db: AsyncSession, clock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hold lasts a minute, so that a process that dies gives it back soon. An answer
    that takes longer renews it with every beat."""
    monkeypatch.setattr(turns, "BEAT", timedelta(milliseconds=10))
    hold = asyncio.Event()
    model, browser = await scripted(
        TextDelta("A long "), hold, TextDelta("answer."), used(read=40, written=4)
    )
    asked = asyncio.create_task(Chat(browser).ask())
    await asyncio.wait_for(model.waiting.wait(), PATIENCE_S)
    held = held_for_prompt(prompt_tokens(model.requests[0].messages)) + ROOM

    during = []
    for _ in range(8):  # six minutes, in steps shorter than a hold lasts
        clock.advance(seconds=45)
        async with asyncio.timeout(PATIENCE_S):
            while True:
                [turn] = (await db.scalars(select(Turn))).all()
                await db.refresh(turn)
                if turn.alive_at == clock.now:  # this moment's beat has been
                    break
                await asyncio.sleep(0.005)
        during.append((await windows(browser))["session"][:2])
    hold.set()
    await asyncio.wait_for(asked, PATIENCE_S)  # an ending that hung would wait for ever

    assert during == [(0, held)] * 8
    assert (await windows(browser))["session"][:2] == (44, 0)


async def test_what_was_held_for_a_turn_that_fell_silent_comes_back_by_itself(
    scripted: Any, tight: Plans, clock: Any
) -> None:
    """The other side of it: without a beat, as when the process that writes the answer
    is gone, the hold runs out a minute after it was last renewed, and the next question
    has the room again. Here the process is only held up, and what its answer cost is
    counted when it ends after all."""
    hold = asyncio.Event()
    model, browser = await scripted(
        TextDelta("Half"), hold, TextDelta(" an answer."), used(read=40, written=4), plans=tight
    )
    asked = asyncio.create_task(Chat(browser).ask())
    await asyncio.wait_for(model.waiting.wait(), PATIENCE_S)
    model.script = [TextDelta("A short answer."), used(read=40, written=4)]  # for the next ones

    clock.advance(seconds=59)
    while_held = await Chat(browser).ask()  # more than half of the session is held
    clock.advance(seconds=1)  # no beat came meanwhile: under test they are 15 real seconds apart
    afterwards = await Chat(browser).ask()
    hold.set()
    await asyncio.wait_for(asked, PATIENCE_S)  # an ending that hung would wait for ever

    assert turn_of(while_held)["capped_by_usage"] is True
    assert turn_of(afterwards)["capped_by_usage"] is False
    assert (await windows(browser))["session"][:2] == (3 * 44, 0)


async def test_what_was_held_for_a_turn_that_fell_silent_is_shown_as_free_without_a_question(
    scripted: Any, clock: Any
) -> None:
    """The same, as the meter sees it: reading the usage gives back a hold that ran out.
    Nothing else is asked in between."""
    hold = asyncio.Event()
    model, browser = await scripted(
        TextDelta("Half"), hold, TextDelta(" an answer."), used(read=40, written=4)
    )
    asked = asyncio.create_task(Chat(browser).ask())
    await asyncio.wait_for(model.waiting.wait(), PATIENCE_S)
    held = held_for_prompt(prompt_tokens(model.requests[0].messages)) + ROOM

    clock.advance(seconds=59)
    while_held = await windows(browser)
    clock.advance(seconds=1)  # no beat came meanwhile: under test they are 15 real seconds apart
    ran_out = await windows(browser)
    hold.set()
    await asyncio.wait_for(asked, PATIENCE_S)  # an ending that hung would wait for ever

    assert while_held["session"][:2] == while_held["week"][:2] == (0, held)
    assert ran_out["session"][:2] == ran_out["week"][:2] == (0, 0)
    assert (await windows(browser))["session"][:2] == (44, 0)  # counted when it ends after all


# --- When the counters are lost -------------------------------------------------------


async def test_usage_survives_the_loss_of_what_valkey_held(
    scripted: Any, tight: Plans, settings: Settings
) -> None:
    """Valkey keeps nothing on disk: a restart empties it. The ledger doesn't forget."""
    _, browser = await scripted(TextDelta("An answer."), used(read=300, written=200), plans=tight)
    chat = Chat(browser)
    await chat.ask()
    before = await windows(browser)

    async with Valkey.from_url(settings.valkey_url.get_secret_value()) as valkey:
        await valkey.delete(*await valkey.keys(f"{settings.valkey_prefix}:q:*"))
        rebuilt = await windows(browser)
        await valkey.delete(*await valkey.keys(f"{settings.valkey_prefix}:q:*"))
        answered = await chat.ask("And then?")  # asking rebuilds them as well
        again = await chat.ask("And then?")

    assert (
        rebuilt
        == before
        == {
            "session": (500, 0, "2026-11-10T17:00:00Z"),
            "week": (500, 0, "2026-11-16T00:00:00Z"),
        }
    )
    assert (answered.status_code, turn_of(answered)["capped_by_usage"]) == (200, True)
    assert again.status_code == 429  # the two turns filled the session, lost counters or not
    assert (await windows(browser))["session"][0] == 1_000


# --- When the counters aren't told ----------------------------------------------------


async def test_a_turn_that_was_counted_leaves_nothing_noted_as_not_counted(
    scripted: Any, db: AsyncSession
) -> None:
    _, browser = await scripted(TextDelta("A derailleur."), used(read=40, written=3))

    response = await Chat(browser).ask()

    [entry] = await ledger(db)
    assert str(entry.reservation_id) == turn_of(response)["id"]
    assert await count(db, Uncounted) == 0


async def test_a_turn_whose_settle_valkey_missed_is_delivered_and_left_noted_for_the_worker(
    scripted: Any,
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Valkey went away between the question and the end of its answer."""
    _, browser = await scripted(TextDelta("A derailleur."), used(read=40, written=3))
    chat = Chat(browser)

    async def away(*_args: Any, **_kwargs: Any) -> None:
        raise ValkeyConnectionError("Connection refused")

    monkeypatch.setattr(ValkeyQuota, "settle", away)

    response = await chat.ask()

    turn = turn_of(response)
    assert (response.status_code, turn["status"], turn["credits"]) == (200, "completed", 43)
    stored = (await browser.get(chat.path)).json()
    assert [message["role"] for message in stored["messages"]] == ["user", "assistant"]
    [entry] = await ledger(db)
    assert (str(entry.reservation_id), entry.credits) == (turn["id"], 43)
    noted = await db.scalars(select(Uncounted.reservation_id))
    assert [str(reservation_id) for reservation_id in noted] == [turn["id"]]
    assert "couldn't be settled in Valkey" in caplog.text


async def test_a_turn_whose_count_cannot_be_noted_is_recorded_and_counted_all_the_same(
    scripted: Any,
    plans_with: Any,
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The database went away after the turn was recorded and counted, before the note
    that it was counted could be taken away. The turn ends as ever, with the share of
    its window that it passed. The note stays: the worker settles the turn again, is
    told that it was settled already, and takes the note away."""
    plans = plans_with(guest=(1_000, 100_000))
    _, browser = await scripted(TextDelta("An answer."), used(read=300, written=250), plans=plans)
    chat = Chat(browser)

    async def cannot_write(*_args: Any, **_kwargs: Any) -> None:
        raise OperationalError("DELETE FROM usage_uncounted", None, OSError("connection lost"))

    monkeypatch.setattr(UsageLimits, "counted", cannot_write)

    response = await chat.ask()

    turn = turn_of(response)
    assert (response.status_code, turn["status"], turn["credits"]) == (200, "completed", 550)
    assert warnings_in(response) == [("session", 50)]
    stored = (await browser.get(chat.path)).json()
    assert [message["role"] for message in stored["messages"]] == ["user", "assistant"]
    assert (await windows(browser))["session"][:2] == (550, 0)
    assert await count(db, Uncounted) == 1
    said = [(r.levelno, r.getMessage()) for r in caplog.records if r.name == "actio.chat.answering"]
    assert said == [
        (
            logging.WARNING,
            f"turn {turn['id']}: couldn't note that its usage was counted (OperationalError)",
        )
    ]
