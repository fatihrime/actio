"""The format a browser's chat library reads: the AI SDK's "UI message stream", version 1.

It is a stream of server-sent events. Each event is one JSON object, a "chunk", on a
`data:` line, and the stream ends with `data: [DONE]`. For an answer that waited its
turn at its model's gate, and reasons first:

    data: {"type":"start","messageId":"…","messageMetadata":{…}}
    data: {"type":"start-step"}
    data: {"type":"data-waits","id":"waits","data":{"behind":2}}
    data: {"type":"data-waits","id":"waits","data":{"behind":1}}
    data: {"type":"data-waits","id":"waits","data":{"behind":0}}
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

The `data-waits` lines are there only for a turn that waited at a model's gate: how many
answers have to end before it starts, each time that changed, and 0 once its turn has
come. Each has the same `id`, so the library keeps one part of the message for them,
whose `data` the next one replaces. The `data-usage-warning` line is there only when the
answer took its account past a share of a usage limit. The names and fields were read
from version 7.0.166 of the `ai` package. A test in
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

from actio.chat.answering import Closed, Opened, Step, Waits

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
    # The library's `length` is a limit on tokens, and this one is on time: the turn's
    # record in the metadata says which.
    "time_limit": "other",
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


WAITS_ID = "waits"
"""The `id` of every `data-waits` chunk of a turn: one part of the message holds them."""


def waits(behind: int) -> dict[str, Any]:
    """The chunk that says how many answers have to end before the turn's answer starts:
    `behind`, 1 or more while it waits at its model's gate, and 0 once its turn has come.

    A data part of Actio's own, and not `transient`: the library keeps it in the message,
    and with the same `id` each time it replaces the one part's `data`. So whoever reads
    the stream, from its start whenever they began, ends with the newest.
    """
    return {"type": "data-waits", "id": WAITS_ID, "data": {"behind": behind}}


def for_step(step: Step) -> dict[str, Any]:
    """The chunk for one step of an answer: the turn waits at a gate, or a part opens,
    grows, or closes."""
    if isinstance(step, Waits):
        return waits(step.behind)
    part = f"{step.kind}-{step.number}"
    if isinstance(step, Opened):
        return {"type": f"{step.kind}-start", "id": part}
    if isinstance(step, Closed):
        return {"type": f"{step.kind}-end", "id": part}
    return {"type": f"{step.kind}-delta", "id": part, "delta": step.text}
