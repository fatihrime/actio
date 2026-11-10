"""Talking HTTP to a provider under deadlines. Shared by the adapters.

The part worth sharing is `stream_lines`: reading a streamed answer so that a silent
provider is noticed, a slow caller is not mistaken for one, and closing the stream
closes the connection.
"""

import asyncio
import contextlib
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from typing import Any

import httpx2

from actio_llm.errors import (
    LLMError,
    ProviderError,
    ProviderUnavailableError,
    StreamInterruptedError,
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
    request_s: float = 10.0
    """For the small calls, such as listing models."""


type Rejection = Callable[[httpx2.Response, str], LLMError]
"""Builds the error for a response that isn't 200, from the response and its body."""


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

    - Nothing within `first_event_s`: `ProviderUnavailableError`. Nothing was generated.
    - A gap longer than `idle_s`, or a dropped connection, after the first line:
      `StreamInterruptedError`.
    - A status other than 200: whatever `rejection` returns.

    Closing the generator closes the connection.
    """
    first_deadline = asyncio.get_running_loop().time() + timeouts.first_event_s
    too_slow = ProviderUnavailableError(
        f"{provider} didn't start answering within {timeouts.first_event_s:g} s"
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
        except httpx2.TransportError as error:
            raise ProviderUnavailableError(f"can't reach {provider}: {error!r}") from error

        lines = response.aiter_lines()
        started = False
        while True:
            watchdog = (
                asyncio.timeout(timeouts.idle_s) if started else asyncio.timeout_at(first_deadline)
            )
            try:
                async with watchdog:
                    line = await anext(lines, None)
            except TimeoutError as error:
                if not started:
                    raise too_slow from error
                raise StreamInterruptedError(
                    f"{provider} sent nothing for {timeouts.idle_s:g} s in the middle of an answer"
                ) from error
            except httpx2.TransportError as error:
                raise StreamInterruptedError(
                    f"the connection to {provider} broke: {error!r}"
                ) from error
            if line is None:
                return
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
    """One of the small calls: send a request and return the JSON object it answers with."""
    try:
        response = await client.request(
            method,
            path,
            json=body,
            timeout=httpx2.Timeout(timeouts.request_s, connect=timeouts.connect_s),
        )
    except httpx2.TransportError as error:
        raise ProviderUnavailableError(f"can't reach {provider}: {error!r}") from error
    if response.status_code != 200:
        raise rejection(response, response.text)
    try:
        data = response.json()
    except ValueError as error:
        raise ProviderError(f"{provider} answered {path} with something that isn't JSON") from error
    if not isinstance(data, dict):
        raise ProviderError(f"{provider} answered {path} with JSON that isn't an object")
    return data
