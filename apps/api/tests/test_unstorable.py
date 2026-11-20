"""The characters that can't be stored, wherever text comes in: from people, in a
request's body, and from a model, in its answer.

There are two kinds. U+0000, which neither Postgres's text nor its JSON holds. And half
of a surrogate pair, half of a character that takes two, as an emoji cut in two leaves
it: no UTF-8 holds that, so it can't be stored, hashed or even sent back in an error.
Each becomes U+FFFD, the mark for a character that can't be shown, as a browser makes it
when it sends text. Nothing is refused for it, and nothing fails.
"""

import json
import logging
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.routing import APIRoute

from actio.api.body import Body
from actio.app import create_app
from actio.chat.answering import Answer, Closed, Opened, Wrote
from actio.core.ids import new_id
from actio.core.settings import Settings
from actio.core.text import storable
from actio_llm import Finish, FinishReason, ReasoningDelta, TextDelta, Usage

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("migrated")]

DONE = Finish(FinishReason.STOP, Usage(input_tokens=80, output_tokens=5))
NUL = "\x00"
HALF = chr(0xD83D)
"""The first half of a face that smiles, U+1F600, without its second."""
OTHER_HALF = chr(0xDE00)
"""Its second half, without the first."""
MARK = chr(0xFFFD)
UNSTORABLE = pytest.mark.parametrize(
    "bad", [NUL, HALF, OTHER_HALF], ids=["nul", "half", "other half"]
)


def asking(text: str, **more: Any) -> dict[str, Any]:
    return {"message": {"id": str(new_id()), "parts": [{"type": "text", "text": text}]}} | more


def spelled(body: Any) -> dict[str, Any]:
    """A request's body as a browser sends it: JSON whose strings are ASCII, with such a
    character spelled as an escape, `\\u0000` or `\\ud83d`. Given to the test client as
    bytes: it can't encode half a pair itself."""
    return {"content": json.dumps(body).encode(), "headers": {"content-type": "application/json"}}


# --- The rule -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        ("Fixing a flat tyre", "Fixing a flat tyre"),
        ("a" + NUL + "b", "a" + MARK + "b"),
        (NUL, MARK),
        ("a" + HALF + "b", "a" + MARK + "b"),
        ("a" + OTHER_HALF + "b", "a" + MARK + "b"),
        (HALF + HALF, MARK + MARK),
        ("a " + chr(0x1F600) + " b", "a " + chr(0x1F600) + " b"),
        ("パンク修理 ı ğ ü", "パンク修理 ı ğ ü"),
        ("a\tb\nc\x1f\x7f", "a\tb\nc\x1f\x7f"),
        ("it has one already: " + MARK, "it has one already: " + MARK),
        ("", ""),
    ],
    ids=[
        "plain",
        "a nul",
        "nothing but a nul",
        "a first half",
        "a second half",
        "two first halves",
        "a whole character that takes two",
        "other scripts",
        "other control characters",
        "the mark itself",
        "nothing",
    ],
)
def test_what_cant_be_stored_becomes_the_mark_and_all_else_is_left(text: str, kept: str) -> None:
    assert storable(text) == kept
    assert storable(kept) == kept
    kept.encode("utf-8")  # it can be sent, and stored


# --- From people ----------------------------------------------------------------------


@UNSTORABLE
@pytest.mark.parametrize("line", ["first", "second"])
async def test_a_question_with_such_a_character_is_asked_with_the_mark_in_its_place(
    bad: str, line: str, offer: Any, start_app: Any, settings: Settings
) -> None:
    """It is stored, its conversation is named after it, and the model is shown it, each
    with the mark. In its first line the conversation's title holds it too, as text; in
    a later line only the message does, as JSON: two columns, both of which refused it."""
    model, catalog = offer(TextDelta("With a torque wrench."), DONE)
    browser = await start_app(settings, catalog=catalog)
    await browser.post("/api/session/guest")
    conversation_id = new_id()
    typed = f"How tight{bad} is the saddle clamp?"
    sent = typed if line == "first" else f"A question\n{typed}"
    shown = sent.replace(bad, MARK)

    answered = await browser.post(
        f"/api/conversations/{conversation_id}/turns", **spelled(asking(sent))
    )
    conversation = (await browser.get(f"/api/conversations/{conversation_id}")).json()

    assert answered.status_code == 200
    question, answer = conversation["messages"]
    assert question["parts"] == [{"type": "text", "text": shown}]
    assert answer["parts"] == [{"type": "text", "text": "With a torque wrench."}]
    assert conversation["title"] == shown.split("\n")[0]
    [request] = model.requests
    assert request.messages[-1].content == shown


@UNSTORABLE
async def test_a_rename_with_such_a_character_names_the_conversation_with_the_mark(
    bad: str, offer: Any, start_app: Any, settings: Settings
) -> None:
    _, catalog = offer(TextDelta("With a torque wrench."), DONE)
    browser = await start_app(settings, catalog=catalog)
    await browser.post("/api/session/guest")
    conversation_id = new_id()
    await browser.post(f"/api/conversations/{conversation_id}/turns", json=asking("How tight?"))

    renamed = await browser.patch(
        f"/api/conversations/{conversation_id}", **spelled({"title": f"Saddle{bad} clamp"})
    )

    assert renamed.status_code == 200
    assert renamed.json()["title"] == f"Saddle{MARK} clamp"


@UNSTORABLE
async def test_an_account_whose_password_holds_such_a_character_logs_in_with_it(
    bad: str, start_app: Any, settings: Settings
) -> None:
    """A password is taken the same way when it is set and when it is tried, so it is
    the same password both times. One with half a character couldn't be hashed at all."""
    browser = await start_app(settings)
    await browser.post("/api/session/guest")
    password = f"a long pass{bad}word of mine"

    account = {"email": "rider@example.com", "password": password}

    signed_up = await browser.post("/api/account", **spelled(account))
    await browser.delete("/api/session")
    logged_in = await browser.post("/api/session", **spelled(account))
    await browser.delete("/api/session")
    with_the_mark = await browser.post(
        "/api/session", json=account | {"password": password.replace(bad, MARK)}
    )
    without_it = await browser.post(
        "/api/session", **spelled(account | {"password": password.replace(bad, "")})
    )

    assert signed_up.status_code == 201
    assert logged_in.status_code == 200
    assert with_the_mark.status_code == 200  # it is the mark that was hashed, both times
    assert without_it.status_code == 401


@UNSTORABLE
async def test_a_login_under_an_address_with_such_a_character_is_a_wrong_address(
    bad: str, start_app: Any, settings: Settings
) -> None:
    """Not an error of Actio's: the address is looked up with the mark in it, and
    nobody has it."""
    browser = await start_app(settings)

    tried = await browser.post(
        "/api/session",
        **spelled({"email": f"rider{bad}@example.com", "password": "a long password"}),
    )

    assert tried.status_code == 401
    assert tried.json()["detail"] == "Wrong email address or password."


@pytest.mark.parametrize(
    "bad", [NUL, HALF, OTHER_HALF, MARK], ids=["nul", "half", "other half", "the mark itself"]
)
async def test_nobody_signs_up_under_an_address_with_such_a_character(
    bad: str, start_app: Any, settings: Settings
) -> None:
    """An address with the mark in it would pass for one: the rules for an address take
    the mark for a letter of some script. It is nobody's mailbox, so it is refused, as
    an address with such a character was before the mark stood in its place."""
    browser = await start_app(settings)
    await browser.post("/api/session/guest")
    account = {"email": f"rider{bad}@example.com", "password": "a long password"}

    signed_up = await browser.post("/api/account", **spelled(account))
    me = (await browser.get("/api/me")).json()

    assert signed_up.status_code == 422
    assert [error["where"] for error in signed_up.json()["errors"]] == ["body.email"]
    assert me["is_guest"] is True


async def test_half_a_pair_sent_as_bytes_not_spelled_is_taken_the_same_way(
    offer: Any, start_app: Any, settings: Settings
) -> None:
    """Not what a browser sends: the three bytes that would be half a pair in UTF-8, if
    UTF-8 had it. The API reads a body leniently, and takes them for half a pair too."""
    _, catalog = offer(TextDelta("With a torque wrench."), DONE)
    browser = await start_app(settings, catalog=catalog)
    await browser.post("/api/session/guest")
    conversation_id = new_id()
    await browser.post(f"/api/conversations/{conversation_id}/turns", json=asking("How tight?"))

    renamed = await browser.patch(
        f"/api/conversations/{conversation_id}",
        content=b'{"title": "Saddle\xed\xa0\xbd clamp"}',
        headers={"content-type": "application/json"},
    )

    assert renamed.status_code == 200
    assert renamed.json()["title"] == f"Saddle{MARK} clamp"


@UNSTORABLE
async def test_what_a_refusal_says_of_such_a_request_can_be_sent(
    bad: str, offer: Any, start_app: Any, settings: Settings
) -> None:
    """A refusal names what it refuses: a model nobody offers, a member nobody expects.
    With half a character in a member's name, the refusal itself couldn't be written,
    and with a nul in a model's name it spelled the nul out."""
    _, catalog = offer(TextDelta("With a torque wrench."), DONE)
    browser = await start_app(settings, catalog=catalog)
    await browser.post("/api/session/guest")

    no_such_model = await browser.post(
        f"/api/conversations/{new_id()}/turns",
        **spelled(asking("How tight?", model=f"fast{bad}")),
    )
    no_such_member = await browser.post(
        f"/api/conversations/{new_id()}/turns",
        **spelled(asking("How tight?") | {f"mood{bad}": "fine"}),
    )

    assert no_such_model.status_code == 422
    assert f"fast{MARK}" in no_such_model.json()["detail"]
    assert no_such_member.status_code == 422
    assert no_such_member.json()["errors"][0]["where"] == f"body.mood{MARK}"


async def test_a_cursor_with_nuls_in_it_is_read_as_the_cursor_without_them(
    offer: Any, start_app: Any, settings: Settings
) -> None:
    """What comes in a request's address isn't a body, and isn't looked through. The one
    string there is the list's cursor. It is read as base64, which passes over what
    isn't base64's, and only a time and an id come out of it: never text."""
    _, catalog = offer(TextDelta("With a torque wrench."), DONE)
    browser = await start_app(settings, catalog=catalog)
    await browser.post("/api/session/guest")
    for question in ("How tight?", "How loose?"):
        await browser.post(f"/api/conversations/{new_id()}/turns", json=asking(question))
    first = (await browser.get("/api/conversations", params={"limit": 1})).json()
    cursor = first["next_cursor"]

    as_given = await browser.get("/api/conversations", params={"limit": 1, "cursor": cursor})
    with_nuls = await browser.get(
        "/api/conversations", params={"limit": 1, "cursor": cursor + NUL * 4}
    )

    assert cursor is not None
    assert as_given.status_code == 200
    assert with_nuls.status_code == 200
    assert with_nuls.json() == as_given.json()
    assert [item["title"] for item in with_nuls.json()["items"]] == ["How tight?"]


class Anything(Body):
    """A body that takes whatever it is sent, to see what the look through it did."""

    held: Any


def nested(text: str, *, levels: int) -> Any:
    """`text` at the bottom of that many lists and objects, in turn."""
    value: Any = text
    for level in range(levels):
        value = [value] if level % 2 else {"in": value}
    return value


def test_a_body_is_looked_through_as_deep_as_a_request_goes_and_no_deeper() -> None:
    """Every string of a body is looked at, its members' names too, down to a depth far
    below any request of this API. Deeper nothing is looked at: no member a route reads
    is there, and a body nested a thousand deep can't make the look go that deep."""
    shallow = Anything.model_validate({"held": nested(f"a{NUL}b", levels=8)})
    named = Anything.model_validate({"held": {f"a{HALF}b": 1}})
    deep = Anything.model_validate({"held": nested(f"a{NUL}b", levels=40)})
    very_deep = Anything.model_validate({"held": nested(f"a{NUL}b", levels=400)})

    assert shallow.held == nested(f"a{MARK}b", levels=8)
    assert named.held == {f"a{MARK}b": 1}
    assert deep.held == nested(f"a{NUL}b", levels=40)
    assert very_deep.held == nested(f"a{NUL}b", levels=400)


def routes_of(holder: Any) -> Iterator[APIRoute]:
    """Every route of an app, those of the routers it includes too."""
    for route in holder.routes:
        if isinstance(route, APIRoute):
            yield route
        elif (included := getattr(route, "original_router", None)) is not None:
            yield from routes_of(included)


async def test_every_request_with_a_body_takes_one_that_is_looked_through(
    settings: Settings, client: httpx.AsyncClient
) -> None:
    """A route whose body isn't a `Body` would take such characters as they come. The
    class each route takes is asked of the routes themselves, and the API's description
    says whether that found every operation that takes a body: how an app holds the
    routers it includes is the framework's own business, and may change."""
    bodies: dict[str, type] = {}
    for route in routes_of(create_app(settings)):
        if route.body_field is None:
            continue
        taken = route.body_field.field_info.annotation
        assert isinstance(taken, type), route.path
        for method in route.methods or ():
            bodies[f"{method} {route.path}"] = taken
    description = (await client.get("/api/openapi.json")).json()
    described = {
        f"{method.upper()} {path}"
        for path, methods in description["paths"].items()
        for method, operation in methods.items()
        if isinstance(operation, dict) and "requestBody" in operation
    }

    assert len(described) >= 6
    assert set(bodies) == described
    assert {where: body for where, body in bodies.items() if not issubclass(body, Body)} == {}


# --- From a model ---------------------------------------------------------------------


@UNSTORABLE
def test_a_piece_of_an_answer_with_such_a_character_is_written_with_the_mark(bad: str) -> None:
    """Where the answer is collected, before a reader is shown it: what is streamed is
    what is stored."""
    answer = Answer()

    steps = [
        *answer.take(ReasoningDelta(f"Clamp{bad}s.")),
        *answer.take(TextDelta(f"Six{bad} Nm")),
        *answer.take(TextDelta(bad)),
        *answer.take(DONE),
    ]

    assert steps == [
        Opened("reasoning", 0),
        Wrote("reasoning", 0, f"Clamp{MARK}s."),
        Closed("reasoning", 0),
        Opened("text", 1),
        Wrote("text", 1, f"Six{MARK} Nm"),
        Wrote("text", 1, MARK),
        Closed("text", 1),
    ]
    assert [part.text for part in answer.parts] == [f"Clamp{MARK}s.", f"Six{MARK} Nm{MARK}"]


@UNSTORABLE
@pytest.mark.parametrize("where", ["text", "reasoning"])
async def test_an_answer_with_such_a_character_is_stored_and_its_turn_ends(
    bad: str,
    where: str,
    offer: Any,
    start_app: Any,
    settings: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """It used to be an answer that couldn't be stored: its turn stayed running until
    it was given up on, and the database's refusal, which quotes the answer up to that
    character, went into the log."""
    caplog.set_level(logging.DEBUG)
    said = f"The saddle{bad} clamp takes 6 Nm."
    script = (
        (TextDelta(said), DONE)
        if where == "text"
        else (ReasoningDelta(said), TextDelta("Six Nm."), DONE)
    )
    _, catalog = offer(*script)
    browser = await start_app(settings, catalog=catalog)
    await browser.post("/api/session/guest")
    conversation_id = new_id()

    answered = await browser.post(
        f"/api/conversations/{conversation_id}/turns", json=asking("How tight?", thinking=True)
    )
    conversation = (await browser.get(f"/api/conversations/{conversation_id}")).json()

    kept = said.replace(bad, MARK)
    chunks = [
        json.loads(line.removeprefix("data: "))
        for line in answered.text.split("\n\n")
        if line.startswith("data: {")
    ]
    assert answered.status_code == 200
    assert [chunk for chunk in chunks if chunk["type"] == "error"] == []
    assert [chunk["delta"] for chunk in chunks if chunk["type"] == f"{where}-delta"] == [kept]
    _, answer = conversation["messages"]
    assert answer["metadata"]["turn"]["status"] == "completed"
    assert {"type": where, "text": kept} in answer["parts"]
    assert conversation["unanswered"] is None
    assert "saddle" not in caplog.text.lower()
