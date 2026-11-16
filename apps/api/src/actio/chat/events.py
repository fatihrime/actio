"""What a turn streams, kept so that it can be read more than once, and from the start.

A turn runs apart from the request that started it (`actio.chat.running`). What it has
to tell a reader, it appends here: to a Valkey stream of its own. Every reader follows
that stream from its beginning, the one whose request started the turn like one who
lost the connection and comes back. Nobody reads from the turn itself, so a reader who
leaves takes nothing away from it.

An event is bytes. This module doesn't know what is in them: `actio.api.ui_stream`
writes them, in the format a browser reads.

The stream is not a record. Valkey keeps nothing on disk, and a stream is deleted soon
after its turn has ended. What a turn produced is in the database
(`actio.chat.turns.finish`); the stream is for reading along while it is produced.
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import timedelta

from valkey.asyncio import Valkey
from valkey.exceptions import ValkeyError

from actio.chat.turns import STALE_AFTER

KEPT_AFTER_THE_END = timedelta(seconds=60)
"""How long a stream stays once its turn has ended. Whoever is following it gets to
read to the end; whoever comes later reads the conversation, where the answer is."""

TICK = timedelta(seconds=1)
"""How long a reader waits for the next event before it is told that there is none
yet. Shorter than the Valkey client's own patience (`VALKEY_TIMEOUT_S`), which would
otherwise take the wait for a server that is gone."""

_BATCH = 256
"""How many events one read takes at most. Someone who joins late reads what there is
in a few of these."""

_EVENT = b"e"
_LAST = b"last"
_MILLISECOND = timedelta(milliseconds=1)


class EventsUnavailableError(Exception):
    """Valkey didn't answer, so nothing could be written, or read."""


class EventsLostError(Exception):
    """The stream is gone while its turn was still being followed: Valkey was
    restarted, or the turn stopped saying it is alive."""


class TurnEvents:
    """The streams of every running turn, in one Valkey database."""

    def __init__(self, client: Valkey, *, prefix: str = "turn") -> None:
        self._client = client
        self._prefix = prefix
        self.tick = TICK
        """How long `follow` waits for an event before it says that there is none."""

    def key(self, turn_id: uuid.UUID) -> str:
        return f"{self._prefix}:{turn_id}"

    async def open(self, turn_id: uuid.UUID, first: bytes) -> None:
        """Start a turn's stream with its first event.

        The stream stays for `STALE_AFTER` unless `keep` is called: a turn whose
        process died leaves nothing behind for long. Raises `EventsUnavailableError`.
        """
        key = self.key(turn_id)
        try:
            async with self._client.pipeline(transaction=True) as pipeline:
                pipeline.delete(key)  # ids are never used twice; a stream never has two starts
                pipeline.xadd(key, {_EVENT: first})
                pipeline.pexpire(key, STALE_AFTER // _MILLISECOND)
                await pipeline.execute()
        except (ValkeyError, OSError) as error:
            raise EventsUnavailableError from error

    async def append(self, turn_id: uuid.UUID, event: bytes) -> bool:
        """Add an event to a turn's stream.

        Returns whether it was added. It isn't when the stream is gone: one that was
        lost doesn't start again in the middle, where no reader could make sense of
        it. Raises `EventsUnavailableError`.
        """
        try:
            added = await self._client.xadd(self.key(turn_id), {_EVENT: event}, nomkstream=True)
        except (ValkeyError, OSError) as error:
            raise EventsUnavailableError from error
        return added is not None

    async def close(self, turn_id: uuid.UUID, last: bytes) -> bool:
        """Add a turn's last event. Readers stop after it, and the stream is deleted
        `KEPT_AFTER_THE_END` later. Returns and raises as `append` does."""
        key = self.key(turn_id)
        try:
            async with self._client.pipeline(transaction=True) as pipeline:
                pipeline.xadd(key, {_EVENT: last, _LAST: b"1"}, nomkstream=True)
                pipeline.pexpire(key, KEPT_AFTER_THE_END // _MILLISECOND)
                added, _ = await pipeline.execute()
        except (ValkeyError, OSError) as error:
            raise EventsUnavailableError from error
        return added is not None

    async def keep(self, turn_id: uuid.UUID) -> None:
        """Keep a running turn's stream for another `STALE_AFTER`. Raises
        `EventsUnavailableError`."""
        try:
            await self._client.pexpire(self.key(turn_id), STALE_AFTER // _MILLISECOND)
        except (ValkeyError, OSError) as error:
            raise EventsUnavailableError from error

    async def discard(self, turn_id: uuid.UUID) -> None:
        """Delete the stream of a turn that never started. Doesn't raise: a stream
        that can't be deleted goes by itself."""
        try:
            await self._client.delete(self.key(turn_id))
        except (ValkeyError, OSError):
            return

    async def follow(self, turn_id: uuid.UUID) -> AsyncGenerator[bytes | None]:
        """A turn's events from the first one, as they come, until its last.

        Yields `None` each time a `tick` has passed with nothing new: the turn is still
        being written, and has nothing to say right now.

        Raises `EventsLostError` when the stream is gone before its last event, and
        `EventsUnavailableError` when Valkey doesn't answer.
        """
        key = self.key(turn_id)
        seen = "0-0"
        # A stream has an event from the moment it is opened. So the first read needn't
        # wait: if it finds nothing, there is no stream.
        wait: int | None = None
        while True:
            try:
                found = await self._client.xread({key: seen}, count=_BATCH, block=wait)
                # Waiting for more of a stream that doesn't exist would never end.
                gone = not found and not await self._client.exists(key)
            except (ValkeyError, OSError) as error:
                raise EventsUnavailableError from error
            if gone:
                raise EventsLostError(str(turn_id))
            wait = self.tick // _MILLISECOND
            if not found:
                yield None
                continue
            [(_, entries)] = found
            for entry_id, fields in entries:
                seen = entry_id.decode()
                yield fields[_EVENT]
                if _LAST in fields:
                    return
