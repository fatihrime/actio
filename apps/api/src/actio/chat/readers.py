"""How many readers each account has open: responses that read along with a turn.

A reader holds its connection for as long as the answer it reads is being written,
and asks Valkey for more every second meanwhile (`actio.chat.events`). Without a limit,
one account could open as many as it liked, and leave them open. So each account may
have `READERS_PER_ACCOUNT`, counted in this process:

- **joining** a turn that is being written is refused when the account has as many
  already (`join`);
- **the reader of a turn that its request has just started** is never refused: the
  turn has started, and that reader is part of it. It counts all the same (`start`).

A reader counts until it `leave`s, which whoever made it sees to however the reading
ended.
"""

import uuid
from collections import Counter
from collections.abc import Callable

READERS_PER_ACCOUNT = 6
"""How many readers one account may have open at once: as many connections as a
browser opens to one address over HTTP/1.1, where every reader takes one. A page reads
one answer at a time, so that is six windows reading along at once."""


class TooManyReadersError(Exception):
    """The account has as many readers open as it may, or more: the reader of a turn
    that was just started counts, and is never refused."""

    def __init__(self, reading: int, at_once: int) -> None:
        super().__init__(f"{reading} of {at_once}")
        self.reading = reading
        """How many readers the account has open."""
        self.at_once = at_once
        """How many it may have."""


class Reader:
    """One reader, counted against its account until it leaves."""

    def __init__(self, leaving: Callable[[], None]) -> None:
        self._leaving = leaving
        self._left = False

    def leave(self) -> None:
        """The reading has ended. Saying so again changes nothing."""
        if not self._left:
            self._left = True
            self._leaving()


class TurnReaders:
    """The readers of every account, in this process."""

    def __init__(self) -> None:
        self._reading: Counter[uuid.UUID] = Counter()

    def join(self, account: uuid.UUID) -> Reader:
        """Count a reader who joins a turn that is being written. Raises
        `TooManyReadersError` when the account has as many as it may."""
        if self._reading[account] >= READERS_PER_ACCOUNT:
            raise TooManyReadersError(self._reading[account], READERS_PER_ACCOUNT)
        return self.start(account)

    def start(self, account: uuid.UUID) -> Reader:
        """Count the reader of a turn that the account has just started. Never
        refused."""
        self._reading[account] += 1
        return Reader(lambda: self._left(account))

    def _left(self, account: uuid.UUID) -> None:
        self._reading[account] -= 1
        if self._reading[account] <= 0:
            del self._reading[account]  # an account that reads nothing takes no room
