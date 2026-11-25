"""Talking HTTP to a provider under deadlines. Shared by the adapters.

The part worth sharing is `stream_lines`: reading a streamed answer so that a silent
provider is noticed, a slow caller is not mistaken for one, an answer is given only so
long as a whole, and closing the stream closes the connection. The time for the whole
is the one deadline that counts the caller's own time too: it is a bound, which a server
that sends faster than it is read would otherwise stretch for ever.
"""

import asyncio
import contextlib
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from typing import Any

import httpx2

from actio_llm.errors import (
    ConnectionFailedError,
    LLMError,
    ProviderError,
    ProviderUnavailableError,
    StreamInterruptedError,
    TookTooLongError,
)


@dataclass(frozen=True, slots=True)
class Timeouts:
    """How long to wait, in seconds, at each stage of a call."""

    connect_s: float = 3.0
    first_event_s: float = 90.0
    """From sending a chat request to the first line of its answer. Generous: for a
    local model it covers a cold load, and waiting in the queue behind another request."""
    idle_s: float = 20.0
    """Between two lines of an answer."""
    answer_s: float = 300.0
    """From sending a chat request to the end of its answer, by the clock: the caller's
    own time between two lines counts too. A full answer of 2,048 tokens in 300 seconds
    is 6.8 tokens a second. The first line has to come within it as well."""
    request_s: float = 10.0
    """For the small calls, such as listing models."""


type Rejection = Callable[[httpx2.Response, str], LLMError]
"""Builds the error for a response that isn't 200, from the response and its body."""


def _unpacked_badly(provider: str, error: httpx2.DecodingError) -> ProviderError:
    """A body that says it is compressed, and can't be unpacked. The HTTP client's error
    for it is none of its errors of the connection (`TransportError`), so it is caught
    by its own name wherever a body is read."""
    return ProviderError(f"{provider} sent an answer that can't be unpacked: {error!r}")


async def stream_lines(
    client: httpx2.AsyncClient,
    path: str,
    body: dict[str, Any],
    *,
    provider: str,
    timeouts: Timeouts,
    rejection: Rejection,
) -> AsyncGenerator[str]:
    """POST `body` as JSON and yield the answer's non-empty lines as they arrive.

    - No connection, known at once: `ConnectionFailedError`. Nothing was sent.
    - Nothing within `first_event_s`, or within `answer_s` when that is shorter, or the
      connection failed otherwise before the answer began: `ProviderUnavailableError`.
      Nothing was generated.
    - A gap longer than `idle_s`, or a dropped connection, after the first line:
      `StreamInterruptedError`.
    - The answer not over `answer_s` after the request was sent, after the first line:
      `TookTooLongError`, whatever the server is doing, and however much of its answer
      has arrived already. No line is handed on once that time is over. A silence that
      both times would end is the one that is over first.
    - A status other than 200: whatever `rejection` returns.

    Closing the generator closes the connection, and so does each of these errors.
    """
    loop = asyncio.get_running_loop()
    sent = loop.time()
    # Before the first line, whichever of the two times is over first ends the wait.
    begins_within_s = min(timeouts.first_event_s, timeouts.answer_s)
    first_deadline = sent + begins_within_s
    answer_deadline = sent + timeouts.answer_s
    too_slow = ProviderUnavailableError(
        f"{provider} didn't start answering within {begins_within_s:g} s"
    )
    too_long = TookTooLongError(
        f"{provider} was still answering after {timeouts.answer_s:g} s, the most an answer is given"
    )
    # No read timeout from the HTTP client: a timeout that spans a `yield` would also
    # count the consumer's time. Each read gets its own deadline below instead.
    http_timeout = httpx2.Timeout(
        connect=timeouts.connect_s, read=None, write=timeouts.request_s, pool=timeouts.connect_s
    )
    async with contextlib.AsyncExitStack() as stack:
        try:
            async with asyncio.timeout_at(first_deadline):
                response = await stack.enter_async_context(
                    client.stream("POST", path, json=body, timeout=http_timeout)
                )
                if response.status_code != 200:
                    text = (await response.aread()).decode(errors="replace")
                    raise rejection(response, text)
        except TimeoutError as error:
            raise too_slow from error
        except httpx2.ConnectError as error:
            raise ConnectionFailedError(f"can't reach {provider}: {error!r}") from error
        except httpx2.TransportError as error:
            raise ProviderUnavailableError(f"can't reach {provider}: {error!r}") from error
        except httpx2.DecodingError as error:
            raise _unpacked_badly(provider, error) from error

        lines = response.aiter_lines()
        started = False
        while True:
            idle_deadline = loop.time() + timeouts.idle_s
            deadline = min(idle_deadline, answer_deadline) if started else first_deadline
            try:
                async with asyncio.timeout_at(deadline):
                    line = await anext(lines, None)
            except TimeoutError as error:
                if not started:
                    raise too_slow from error
                if answer_deadline <= idle_deadline:
                    raise too_long from error
                raise StreamInterruptedError(
                    f"{provider} sent nothing for {timeouts.idle_s:g} s in the middle of an answer"
                ) from error
            except httpx2.TransportError as error:
                raise StreamInterruptedError(
                    f"the connection to {provider} broke: {error!r}"
                ) from error
            except httpx2.DecodingError as error:
                if not started:
                    raise _unpacked_badly(provider, error) from error
                raise StreamInterruptedError(
                    f"{provider}'s answer couldn't be unpacked half-way: {error!r}"
                ) from error
            if line is None:
                return
            # A line received already is read without waiting, however late it is read:
            # a deadline around the read alone would hand on every line a slow caller has
            # fallen behind by. So the clock is asked before anything is handed on.
            if loop.time() >= answer_deadline:
                raise too_long if started else too_slow
            if line.strip():
                started = True
                yield line


async def request_json(
    client: httpx2.AsyncClient,
    method: str,
    path: str,
    *,
    provider: str,
    timeouts: Timeouts,
    rejection: Rejection,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One of the small calls: send a request and return the JSON object it answers with.
    No connection, known at once, is a `ConnectionFailedError`, as for `stream_lines`."""
    try:
        response = await client.request(
            method,
            path,
            json=body,
            timeout=httpx2.Timeout(timeouts.request_s, connect=timeouts.connect_s),
        )
    except httpx2.ConnectError as error:
        raise ConnectionFailedError(f"can't reach {provider}: {error!r}") from error
    except httpx2.TransportError as error:
        raise ProviderUnavailableError(f"can't reach {provider}: {error!r}") from error
    except httpx2.DecodingError as error:
        raise ProviderError(
            f"{provider} answered {path} with something that can't be unpacked: {error!r}"
        ) from error
    if response.status_code != 200:
        raise rejection(response, response.text)
    try:
        data = response.json()
    except (ValueError, RecursionError) as error:
        raise ProviderError(f"{provider} answered {path} with something that isn't JSON") from error
    if not isinstance(data, dict):
        raise ProviderError(f"{provider} answered {path} with JSON that isn't an object")
    return data
