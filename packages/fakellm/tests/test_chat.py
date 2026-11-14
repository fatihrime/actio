"""The Chat Completions endpoint, checked against OpenAI's wire format."""

import json
import time
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.anyio

URL = "/v1/chat/completions"
HELLO = {"role": "user", "content": "hello there"}


async def test_reply_echoes_the_last_user_message(client: httpx.AsyncClient, now: int) -> None:
    system = {"role": "system", "content": "Be brief."}
    response = await client.post(URL, json={"model": "fake-chat", "messages": [system, HELLO]})

    assert response.status_code == 200
    assert response.json() == {
        "id": "chatcmpl-fake-000001",
        "object": "chat.completion",
        "created": now,
        "model": "fake-chat",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Echo: hello there"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7},
    }


@pytest.mark.parametrize("limit", ["max_completion_tokens", "max_tokens"])
async def test_output_limit_cuts_the_reply_short(client: httpx.AsyncClient, limit: str) -> None:
    response = await client.post(URL, json={"model": "fake-chat", "messages": [HELLO], limit: 2})

    body = response.json()
    assert body["choices"][0]["message"]["content"] == "Echo: hello "
    assert body["choices"][0]["finish_reason"] == "length"
    assert body["usage"]["completion_tokens"] == 2


async def test_text_parts_of_a_multi_part_message_are_read(client: httpx.AsyncClient) -> None:
    parts = [
        {"type": "text", "text": "hello"},
        {"type": "image_url", "image_url": {"url": "data:,"}},
        {"type": "text", "text": "there"},
    ]
    message = {"role": "user", "content": parts}
    response = await client.post(URL, json={"model": "fake-chat", "messages": [message]})

    assert response.json()["choices"][0]["message"]["content"] == "Echo: hello there"


async def test_each_completion_gets_the_next_id(client: httpx.AsyncClient) -> None:
    request = {"model": "fake-chat", "messages": [HELLO]}
    first = (await client.post(URL, json=request)).json()
    second = (await client.post(URL, json=request)).json()

    assert [first["id"], second["id"]] == ["chatcmpl-fake-000001", "chatcmpl-fake-000002"]
    assert first["choices"] == second["choices"]


async def stream_events(client: httpx.AsyncClient, request: dict[str, Any]) -> list[str]:
    """POST a streaming request and return the `data:` payloads in order."""
    async with client.stream("POST", URL, json=request | {"stream": True}) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        return [line.removeprefix("data: ") async for line in response.aiter_lines() if line]


async def test_stream_sends_role_then_tokens_then_finish(
    client: httpx.AsyncClient, now: int
) -> None:
    events = await stream_events(client, {"model": "fake-chat", "messages": [HELLO]})

    assert events[-1] == "[DONE]"
    chunks = [json.loads(event) for event in events[:-1]]
    assert {(c["id"], c["object"], c["created"], c["model"]) for c in chunks} == {
        ("chatcmpl-fake-000001", "chat.completion.chunk", now, "fake-chat")
    }
    deltas = [c["choices"][0]["delta"] for c in chunks]
    assert deltas[0] == {"role": "assistant", "content": ""}
    assert "".join(delta["content"] or "" for delta in deltas) == "Echo: hello there"
    assert [c["choices"][0]["finish_reason"] for c in chunks] == [None, None, None, None, "stop"]
    assert all(c["usage"] is None for c in chunks)


async def test_stream_ends_with_a_usage_chunk_when_asked(client: httpx.AsyncClient) -> None:
    request = {"model": "fake-chat", "messages": [HELLO], "stream_options": {"include_usage": True}}
    events = await stream_events(client, request)

    usage_chunk = json.loads(events[-2])
    assert usage_chunk["choices"] == []
    assert usage_chunk["usage"] == {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}
    assert events[-1] == "[DONE]"


async def test_unknown_model_is_an_openai_style_404(client: httpx.AsyncClient) -> None:
    response = await client.post(URL, json={"model": "gpt-9", "messages": [HELLO]})

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "message": "The model `gpt-9` does not exist.",
            "type": "invalid_request_error",
            "param": "model",
            "code": "model_not_found",
        }
    }


async def test_invalid_request_is_an_openai_style_400(client: httpx.AsyncClient) -> None:
    response = await client.post(URL, json={"model": "fake-chat", "messages": []})

    assert response.status_code == 400
    error = response.json()["error"]
    assert (error["type"], error["param"], error["code"]) == (
        "invalid_request_error",
        "messages",
        None,
    )


async def test_models_lists_the_served_models(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/models")

    assert response.json() == {
        "object": "list",
        "data": [{"id": "fake-chat", "object": "model", "created": 0, "owned_by": "fakellm"}],
    }


async def test_healthz_reports_ok(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")

    assert response.json() == {"status": "ok"}


# --- Scripted replies: directives in the last user message ----------------------------


def asking(content: str, **fields: Any) -> dict[str, Any]:
    return {"model": "fake-chat", "messages": [{"role": "user", "content": content}], **fields}


async def test_think_puts_reasoning_before_the_answer(client: httpx.AsyncClient) -> None:
    response = await client.post(URL, json=asking("[[think: two plus two]] What is 2+2?"))

    body = response.json()
    assert body["choices"][0]["message"] == {
        "role": "assistant",
        "content": "Echo: What is 2+2?",  # the directive is left out of the echo
        "reasoning": "two plus two",
    }
    assert body["usage"] == {"prompt_tokens": 7, "completion_tokens": 7, "total_tokens": 14}


async def test_think_streams_reasoning_deltas_first(client: httpx.AsyncClient) -> None:
    events = await stream_events(client, asking("[[think: two plus two]] What is 2+2?"))

    deltas = [json.loads(event)["choices"][0]["delta"] for event in events[:-1]]
    assert deltas[0] == {"role": "assistant", "content": ""}  # no reasoning key when unset
    assert [delta.get("reasoning") for delta in deltas[1:4]] == ["two ", "plus ", "two"]
    assert "".join(delta["content"] or "" for delta in deltas) == "Echo: What is 2+2?"


async def test_the_output_limit_cuts_the_reasoning_first(client: httpx.AsyncClient) -> None:
    request = asking("[[think: two plus two]] What is 2+2?", max_completion_tokens=2)
    choice = (await client.post(URL, json=request)).json()["choices"][0]

    assert choice["message"] == {"role": "assistant", "content": "", "reasoning": "two plus "}
    assert choice["finish_reason"] == "length"


async def test_call_returns_a_tool_call_instead_of_an_answer(client: httpx.AsyncClient) -> None:
    response = await client.post(URL, json=asking('[[call: get_weather {"city": "Izmir"}]] Go'))

    body = response.json()
    assert body["choices"][0] == {
        "index": 0,
        "message": {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_fake_000001",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city": "Izmir"}'},
                }
            ],
        },
        "finish_reason": "tool_calls",
    }
    assert body["usage"]["completion_tokens"] == 3  # the name, and two words of arguments


async def test_call_streams_its_arguments_in_fragments(client: httpx.AsyncClient) -> None:
    events = await stream_events(client, asking('[[call: get_weather {"city": "Izmir"}]] Go'))

    chunks = [json.loads(event) for event in events[:-1]]
    parts = [part for c in chunks for part in c["choices"][0]["delta"].get("tool_calls", [])]
    assert parts == [
        {
            "index": 0,
            "id": "call_fake_000001",
            "type": "function",
            "function": {"name": "get_weather", "arguments": ""},
        },
        {"index": 0, "function": {"arguments": '{"city": "Iz'}},
        {"index": 0, "function": {"arguments": 'mir"}'}},
    ]
    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"
    assert all(not c["choices"][0]["delta"]["content"] for c in chunks)


async def test_two_calls_get_their_own_index_and_id(client: httpx.AsyncClient) -> None:
    response = await client.post(URL, json=asking("[[call: first]] [[call: second {}]] Go"))

    calls = response.json()["choices"][0]["message"]["tool_calls"]
    assert [(call["id"], call["function"]) for call in calls] == [
        ("call_fake_000001", {"name": "first", "arguments": "{}"}),
        ("call_fake_000002", {"name": "second", "arguments": "{}"}),
    ]


async def test_after_a_tool_result_the_reply_echoes_it(client: httpx.AsyncClient) -> None:
    """So a scripted tool call isn't repeated for ever: the next step gets a plain answer."""
    messages = [
        {"role": "user", "content": "[[call: get_weather]] Weather?"},
        {"role": "assistant", "content": None, "tool_calls": []},
        {"role": "tool", "tool_call_id": "call_fake_000001", "content": "[[fail: 500]] 18 degrees"},
    ]
    response = await client.post(URL, json={"model": "fake-chat", "messages": messages})

    # A directive inside a tool's result is just text: only the user scripts the fake.
    assert response.json()["choices"][0]["message"]["content"] == "Echo: [[fail: 500]] 18 degrees"


async def test_directives_in_earlier_messages_are_ignored(client: httpx.AsyncClient) -> None:
    messages = [
        {"role": "user", "content": "[[fail: 503]] first"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "second"},
    ]
    response = await client.post(URL, json={"model": "fake-chat", "messages": messages})

    assert response.json()["choices"][0]["message"]["content"] == "Echo: second"


@pytest.mark.parametrize(
    ("directive", "status", "kind", "code"),
    [
        ("[[fail: 503]]", 503, "server_error", None),
        (
            "[[fail: 400 context_length_exceeded]]",
            400,
            "invalid_request_error",
            "context_length_exceeded",
        ),
    ],
)
async def test_fail_answers_with_an_openai_style_error(
    client: httpx.AsyncClient, directive: str, status: int, kind: str, code: str | None
) -> None:
    response = await client.post(URL, json=asking(f"{directive} hello", stream=True))

    assert response.status_code == status
    assert response.json() == {
        "error": {
            "message": f"fakellm: scripted failure ({status})",
            "type": kind,
            "param": None,
            "code": code,
        }
    }
    assert "retry-after" not in response.headers


async def test_a_scripted_429_says_when_to_retry(client: httpx.AsyncClient) -> None:
    response = await client.post(URL, json=asking("[[fail: 429]] hello"))

    assert response.status_code == 429
    assert response.headers["retry-after"] == "1"


async def test_drop_ends_the_stream_without_finishing_it(client: httpx.AsyncClient) -> None:
    events = await stream_events(client, asking("[[drop: 2]] hello there"))

    chunks = [json.loads(event) for event in events]  # no "[DONE]" among them
    assert [c["choices"][0]["delta"]["content"] for c in chunks] == ["", "Echo: ", "hello "]
    assert all(c["choices"][0]["finish_reason"] is None for c in chunks)


@pytest.mark.parametrize("stream", [False, True])
async def test_delay_waits_before_answering(client: httpx.AsyncClient, stream: bool) -> None:
    started = time.monotonic()
    response = await client.post(URL, json=asking("[[delay: 0.2]] hello", stream=stream))

    assert response.status_code == 200
    assert time.monotonic() - started >= 0.2


async def test_pace_waits_between_the_pieces_of_a_stream(client: httpx.AsyncClient) -> None:
    """A real model takes its time over an answer. A test that stops one half-way, or
    watches a page follow it, needs the fake to take some too."""
    started = time.monotonic()
    events = await stream_events(client, asking("[[pace: 0.05]] one two three"))

    # The role, four tokens and the finish: five waits between six pieces.
    contents = [json.loads(event)["choices"][0]["delta"].get("content") for event in events[:-1]]
    assert contents == ["", "Echo: ", "one ", "two ", "three", None]
    assert time.monotonic() - started >= 5 * 0.05


async def test_pace_changes_nothing_in_a_reply_that_is_not_streamed(
    client: httpx.AsyncClient,
) -> None:
    started = time.monotonic()
    response = await client.post(URL, json=asking("[[pace: 1]] one two three", stream=False))

    assert response.json()["choices"][0]["message"]["content"] == "Echo: one two three"
    assert time.monotonic() - started < 1


@pytest.mark.parametrize(
    "directive",
    [
        "[[dance: now]]",
        "[[fail: 200]]",
        "[[fail: soon]]",
        "[[call: f not-json]]",
        "[[call: f [1, 2]]]",
        "[[drop: -1]]",
        "[[delay: 3600]]",
        "[[pace: -0.1]]",
        "[[pace: 60]]",
        "[[pace: slowly]]",
    ],
)
async def test_a_directive_that_cannot_be_read_is_a_400(
    client: httpx.AsyncClient, directive: str
) -> None:
    """A typo in a test's script should fail that test, not be echoed back as text."""
    response = await client.post(URL, json=asking(f"{directive} hello"))

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["message"].startswith("fakellm: ")
    assert error["param"] == "messages"
