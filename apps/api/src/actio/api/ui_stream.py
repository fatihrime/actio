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
    data: {"type":"data-usage-warning","data":{"window":"session","percent":75},"transient":true}
    data: {"type":"finish-step"}
    data: {"type":"finish","finishReason":"stop","messageMetadata":{…}}
    data: [DONE]

The `data-usage-warning` line is there only when the answer took its account past a
share of a usage limit. The names and fields were read from version 7.0.169 of the `ai`
package. A test in
`apps/web` parses recorded streams with that package itself, so a chunk it wouldn't
accept fails there.

Between two events there can be a line that is no event, `: keep-alive`. A reader gets
one whenever the turn has had nothing to say for a while, which keeps a proxy from
closing a connection it takes for dead. The format calls it a comment, and whoever
reads the stream skips it.
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

KEEP_ALIVE = b": keep-alive\n\n"
"""Sent to a reader while there is nothing to send: a comment, in the words of
server-sent events. Readers skip it."""

LOST = "The connection to this answer was lost. It may still be being written."
"""What a reader is told when what the turn streams can no longer be read, while the
turn itself may be running on. Whoever shows the conversation should read it again."""

GIVEN_UP = (
    "This answer was given up on before it ended. The conversation shows what was kept of it."
)
"""What a reader is told when a turn ended after it had been given up on: its record is
what giving up wrote, not the ending its reader saw arrive. Whoever shows the
conversation should read it again."""

GONE = "This conversation was deleted while its answer was being written."
"""What a reader is told when a turn ended after its conversation was deleted: nothing
of it was left to record its outcome in."""

FINISH_REASONS = {
    "stop": "stop",
    "length": "length",
    # The library has no finer word: the turn's record in the metadata says which limit.
    "context_window": "length",
    "tool_calls": "tool-calls",
    "content_filter": "content-filter",
}
"""Why a model stopped, in the words a turn is recorded with and in the stream's."""


def event(chunk: Mapping[str, Any]) -> bytes:
    """One chunk as a server-sent event. A line break inside a text is escaped by JSON,
    so a chunk is always one line."""
    return (
        b"data: " + json.dumps(chunk, separators=(",", ":"), ensure_ascii=False).encode() + b"\n\n"
    )


def usage_warning(window: str, percent: int) -> dict[str, Any]:
    """The chunk that says a usage window has just passed `percent` of its limit.

    A data part of Actio's own: the library hands it to the page as it arrives. It is
    `transient`, so it doesn't become part of the message. It is sent once per window
    and share, just before the stream finishes.
    """
    return {
        "type": "data-usage-warning",
        "data": {"window": window, "percent": percent},
        "transient": True,
    }


def for_step(step: Step) -> dict[str, str]:
    """The chunk for one step of an answer: a part opens, grows, or closes."""
    part = f"{step.kind}-{step.number}"
    if isinstance(step, Opened):
        return {"type": f"{step.kind}-start", "id": part}
    if isinstance(step, Closed):
        return {"type": f"{step.kind}-end", "id": part}
    return {"type": f"{step.kind}-delta", "id": part, "delta": step.text}
