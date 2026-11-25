"""Asking a model in another's place: the walk down a chain of models.

A model may have others that stand in for it. When it can't answer, the next one is
asked, and so on down the chain, but only before anything of an answer was handed on:
once a reader may have been shown a word, or a thought, the answer is that model's, and
its failure is the caller's, as when a provider is asked by itself.

`Walk` is given the chain as `Link`s, in order: each a provider, the request for it, a
name for its caller's log, and what to hold while it is asked, such as the gate in front
of its server. `Walk.stream()` yields the events of the first link that answers. The
rules:

- **One at a time, in order.** A link is asked only after the one before it failed.
- **Before anything was handed on, any `LLMError` moves on to the next link:** a server
  too busy or away, a limit, a request too large, a request refused, a model the server
  hasn't got, a provider that failed, a server too old, an answer that broke off with
  nothing in it yet, and a stream that ended without its `Finish`.
- **But first, a failure that may be gone in a moment has its link asked once more,**
  with the same request: a connection that failed at once, with nothing sent
  (`ConnectionFailedError`), at once; and a server that asked to wait `WAITS_AT_MOST_S`
  or less (`RateLimitedError.retry_after_s`, from 0), after that wait. No other failure
  is asked again: a connection that was never answered has spent its time already, one
  that was made and then closed may have had its request read, and for a server too
  busy, or an answer that broke off, nothing says that it is over in a moment.
- **Once for each link.** A link is asked twice at most, and each link of the chain has
  its own second try. Whatever the second try fails with is the link's failure, and the
  walk moves on.
- **After something was handed on, nothing moves on, and nothing is asked again.** The
  error is raised as it came, and no later link is asked. Something is a piece of text
  or of reasoning with text in it, a tool call, or a `Finish`: a model that wrote
  nothing has answered too. A thought counts: a page that has shown it can't take it
  back. A piece with no text in it is nothing.
- **Only `LLMError` is sorted.** A cancellation, or any other exception, passes through
  as it came, from wherever the walk is waiting, a hold, a link's stream or the pause
  before a second try, and nothing more is asked.
- **One hold at a time, and once for each link.** A link's hold is entered once, before
  its first try, and kept through the pause and its second try: a turn that waits at a
  gate doesn't go to the back of the line to be asked again. Each try's stream is
  closed before the next begins, and the last one's before the hold is given back. A
  link that failed gives its hold back before the next link's hold is waited for: two
  walks whose chains cross never each keep one hold while they wait for the other.
- **When every link failed,** one of their errors is raised, the object itself: the
  first that another try may not meet again (`FOR_NOW`), and without one, the last
  link's. A model too busy, then another that can't hold the conversation, is the first:
  the answer may come at another try, and the reader is told so.

Closing the walk's stream early closes the stream of the link that is open, and gives
its hold back, as closing a provider's stream does. With one link, the walk is that
provider, asked once more by the same rule.

Nothing here knows who asks: a link's name is only for its caller's log.
"""

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, aclosing, nullcontext
from dataclasses import dataclass

from actio_llm.chat import ChatProvider, ChatRequest, ReasoningDelta, StreamEvent, TextDelta
from actio_llm.errors import (
    FOR_NOW,
    ConnectionFailedError,
    LLMError,
    RateLimitedError,
    StreamInterruptedError,
)

WAITS_AT_MOST_S = 2.0
"""The longest wait a server may ask for, in seconds, and have its link asked once more
after it. A wait that is longer, or none, moves on to the next link at once: the model
that stands in is there for that. The 429 recorded from Groq asked for 2 s."""


@dataclass(frozen=True, slots=True)
class Link:
    """One model of a chain: whom to ask, what, and what to hold meanwhile."""

    name: str
    """What its caller calls it, for the log."""
    chat: ChatProvider
    request: ChatRequest
    """The request for this provider: its own model's name, window and limits."""
    hold: AbstractAsyncContextManager[object] | None = None
    """Entered before the link is asked, and left once its last try's stream is closed,
    however it ended: such as the gate in front of its server. Entered once at most, and
    not at all for a link that isn't reached."""


type OnFailed = Callable[[Link, LLMError, Link | None], None]
"""Told of a link that failed: the link, its error, and the link asked next, or `None`
when it was the last."""

type OnAgain = Callable[[Link, LLMError, float], None]
"""Told that a link failed and is asked once more: the link, its error, and the pause
before the second try, in seconds, 0 for at once."""


class Walk:
    """A walk down a chain of links, in order, to the first that answers: see the
    module. It is walked once, through `stream`.

    `on_failed` is told of each link that failed before anything of it was handed on,
    once for a link, after its last try, as it happens: its stream is closed and its
    hold given back by then, and the next link isn't asked yet. The error is the last
    try's. It may not raise.

    `on_again` is told of each link that is asked once more, when its first try has
    failed and that try's stream is closed, before the pause. It may not raise.

    `pause` waits the pause before a second try: `asyncio.sleep`, unless a test waits
    otherwise.
    """

    def __init__(
        self,
        links: Sequence[Link],
        *,
        on_failed: OnFailed | None = None,
        on_again: OnAgain | None = None,
        pause: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ) -> None:
        if not links:
            raise ValueError("a walk needs at least one link")
        self._links = tuple(links)
        self._on_failed = on_failed
        self._on_again = on_again
        self._pause = pause
        self.answered: Link | None = None
        """The link that answered, once something of it was handed on: the one whose
        request the answer was written for. `None` until then, and when every link
        failed."""

    async def stream(self) -> AsyncGenerator[StreamEvent]:
        """The events of the first link that answers, as its provider yields them.
        Raises when every link failed: see the module for which error."""
        failures: list[LLMError] = []
        for number, link in enumerate(self._links):
            later = self._links[number + 1] if number + 1 < len(self._links) else None
            again = False
            # The link is asked only once its hold is entered, and the hold is left once
            # its last try's stream is closed, however the block ends.
            async with link.hold or nullcontext():
                while True:
                    answered = False
                    failed: LLMError | None = None
                    async with aclosing(link.chat.stream(link.request)) as events:
                        while failed is None:
                            try:
                                event = await anext(events)
                            except StopAsyncIteration:
                                if answered:
                                    return
                                failed = StreamInterruptedError(
                                    "the model's answer ended without saying why"
                                )
                            except LLMError as error:
                                if answered:
                                    raise
                                failed = error
                            else:
                                if not answered and _hands_on(event):
                                    answered = True
                                    self.answered = link
                                yield event
                    # The try's stream is closed by now.
                    wait = None if again else _asked_again_after(failed)
                    if wait is None:
                        break
                    again = True
                    if self._on_again is not None:
                        self._on_again(link, failed, wait)
                    await self._pause(wait)
            failures.append(failed)
            if self._on_failed is not None:
                self._on_failed(link, failed, later)
        raise next((error for error in failures if isinstance(error, FOR_NOW)), failures[-1])


def _asked_again_after(error: LLMError) -> float | None:
    """How long to wait, in seconds, before a link that failed with `error` before
    anything was handed on is asked once more. `None`: it isn't."""
    if isinstance(error, ConnectionFailedError):
        return 0.0
    if isinstance(error, RateLimitedError):
        wait = error.retry_after_s
        # `nan` compares false with everything, and `inf` is more than the most.
        if wait is not None and 0 <= wait <= WAITS_AT_MOST_S:
            return float(wait)
    return None


def _hands_on(event: StreamEvent) -> bool:
    """Whether an event gives a reader something: every event but a piece of text or of
    reasoning with no text in it."""
    if isinstance(event, TextDelta | ReasoningDelta):
        return bool(event.text)
    return True
