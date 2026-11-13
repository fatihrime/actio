"""The format a browser's chat library reads: the AI SDK's "UI message stream", version 1.

It is a stream of server-sent events. Each event is one JSON object, a "chunk", on a
`data:` line, and the stream ends with `data: [DONE]`. For an answer that reasons first:

    data: {"type":"start","messageId":"…","messageMetadata":{…}}
    data: {"type":"start-step"}
    data: {"type":"reasoning-start","id":"reasoning-0"}
    data: {"type":"reasoning-delta","id":"reasoning-0","delta":"…"}
    data: {"type":"reasoning-end","id":"reasoning-0"}
    data: {"type":"text-start","id":"text-1"}
    data: {"type":"text-delta","id":"text-1","delta":"…"}
    data: {"type":"text-end","id":"text-1"}
    data: {"type":"finish-step"}
    data: {"type":"finish","finishReason":"stop","messageMetadata":{…}}
    data: [DONE]

The names and fields were read from version 7.0.169 of the `ai` package. A test in
`apps/web` parses recorded streams with that package itself, so a chunk it wouldn't
accept fails there.
"""

import json
from collections.abc import Mapping
from typing import Any

from actio.chat.answering import Closed, Opened, Step

MEDIA_TYPE = "text/event-stream"

HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",  # asks a proxy in between to pass each event on at once
    "x-vercel-ai-ui-message-stream": "v1",
}

DONE = b"data: [DONE]\n\n"

FINISH_REASONS = {
    "stop": "stop",
    "length": "length",
    "tool_calls": "tool-calls",
    "content_filter": "content-filter",
}
"""Why a model stopped, in the model clients' words and in the stream's."""


def event(chunk: Mapping[str, Any]) -> bytes:
    """One chunk as a server-sent event. A line break inside a text is escaped by JSON,
    so a chunk is always one line."""
    return (
        b"data: " + json.dumps(chunk, separators=(",", ":"), ensure_ascii=False).encode() + b"\n\n"
    )


def for_step(step: Step) -> dict[str, str]:
    """The chunk for one step of an answer: a part opens, grows, or closes."""
    part = f"{step.kind}-{step.number}"
    if isinstance(step, Opened):
        return {"type": f"{step.kind}-start", "id": part}
    if isinstance(step, Closed):
        return {"type": f"{step.kind}-end", "id": part}
    return {"type": f"{step.kind}-delta", "id": part, "delta": step.text}
