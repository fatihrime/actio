"""The Chat Completions endpoint, checked against OpenAI's wire format."""

import json
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
