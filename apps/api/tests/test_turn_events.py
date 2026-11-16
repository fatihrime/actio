"""What a turn streams, kept in Valkey: written once, read by anyone, from the start.

Against a real Valkey server. How the API uses it is in `test_turns_running_api.py`.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import timedelta

import pytest
from valkey.asyncio import Valkey

from actio.chat.events import (
    KEPT_AFTER_THE_END,
    EventsLostError,
    EventsUnavailableError,
    TurnEvents,
)
from actio.chat.turns import STALE_AFTER
from actio.core.ids import new_id

pytestmark = pytest.mark.anyio

PATIENCE_S = 5.0


@pytest.fixture
async def valkey(valkey_url: str) -> AsyncIterator[Valkey]:
    async with Valkey.from_url(valkey_url, socket_timeout=2.0) as client:
        yield client


@pytest.fixture
def events(valkey: Valkey) -> TurnEvents:
    """Streams under a prefix of this test's own, with readers that don't wait long."""
    made = TurnEvents(valkey, prefix=f"test-{uuid.uuid4().hex[:16]}:turn")
    made.tick = timedelta(milliseconds=20)
    return made


async def read(events: TurnEvents, turn_id: uuid.UUID) -> list[bytes]:
    """Everything a reader gets, from the first event to the last."""
    async with asyncio.timeout(PATIENCE_S):
        return [event async for event in events.follow(turn_id) if event is not None]


class Reader:
    """Someone who follows a turn's stream in a task of their own. What has come so far
    can be looked at while they are still reading."""

    def __init__(self, events: TurnEvents, turn_id: uuid.UUID) -> None:
        self.seen: list[bytes | None] = []
        """Each event, and `None` for each time there was nothing new."""
        self.task = asyncio.create_task(self._follow(events, turn_id))

    async def _follow(self, events: TurnEvents, turn_id: uuid.UUID) -> None:
        async for event in events.follow(turn_id):
            self.seen += [event]

    async def has(self, enough: Callable[[list[bytes | None]], bool]) -> None:
        """Wait until what has come so far is `enough`."""
        async with asyncio.timeout(PATIENCE_S):
            while True:
                if enough(self.seen):
                    return
                await asyncio.sleep(0.01)


async def test_what_a_turn_wrote_is_read_in_order_up_to_its_last_event(events: TurnEvents) -> None:
    turn = new_id()
    await events.open(turn, b"start")
    assert await events.append(turn, b"one")
    assert await events.append(turn, b"two")
    assert await events.close(turn, b"done")

    assert await read(events, turn) == [b"start", b"one", b"two", b"done"]


async def test_a_reader_who_comes_late_reads_from_the_beginning(events: TurnEvents) -> None:
    turn = new_id()
    await events.open(turn, b"start")
    for number in range(600):  # more than one read takes at a time
        await events.append(turn, str(number).encode())

    late = asyncio.create_task(read(events, turn))
    await asyncio.sleep(0.05)
    await events.close(turn, b"done")

    assert await late == [b"start", *(str(number).encode() for number in range(600)), b"done"]


async def test_a_reader_waits_for_what_is_still_to_come(events: TurnEvents) -> None:
    turn = new_id()
    await events.open(turn, b"start")

    reader = Reader(events, turn)
    await reader.has(lambda seen: seen.count(None) >= 2)  # nothing new, twice: it waits on
    await events.close(turn, b"done")
    await asyncio.wait_for(reader.task, PATIENCE_S)

    assert reader.seen[0] == b"start"
    assert reader.seen[-1] == b"done"
    assert set(reader.seen[1:-1]) == {None}


async def test_every_reader_reads_all_of_it(events: TurnEvents) -> None:
    turn = new_id()
    await events.open(turn, b"start")
    first = asyncio.create_task(read(events, turn))
    await events.append(turn, b"one")
    second = asyncio.create_task(read(events, turn))
    await events.close(turn, b"done")

    whole = [b"start", b"one", b"done"]
    assert (await first, await second, await read(events, turn)) == (whole, whole, whole)


async def test_two_turns_dont_read_each_other(events: TurnEvents) -> None:
    mine, yours = new_id(), new_id()
    await events.open(mine, b"mine")
    await events.open(yours, b"yours")
    await events.close(mine, b"my end")
    await events.close(yours, b"your end")

    assert await read(events, mine) == [b"mine", b"my end"]
    assert await read(events, yours) == [b"yours", b"your end"]


async def test_a_stream_that_is_gone_does_not_start_again_in_the_middle(
    events: TurnEvents, valkey: Valkey
) -> None:
    """Valkey keeps nothing on disk: after a restart the stream is gone. What a turn
    writes then would be an answer without its beginning."""
    turn = new_id()
    await events.open(turn, b"start")
    await events.append(turn, b"one")
    await valkey.delete(events.key(turn))

    assert not await events.append(turn, b"two")
    assert not await events.close(turn, b"done")
    assert not await valkey.exists(events.key(turn))


async def test_a_reader_is_told_when_the_stream_is_gone(events: TurnEvents, valkey: Valkey) -> None:
    turn = new_id()
    await events.open(turn, b"start")

    reader = Reader(events, turn)
    await reader.has(lambda seen: len(seen) > 0)
    await valkey.delete(events.key(turn))
    with pytest.raises(EventsLostError):
        await asyncio.wait_for(reader.task, PATIENCE_S)

    assert reader.seen[0] == b"start"


async def test_a_stream_that_never_was_is_lost_at_once(events: TurnEvents) -> None:
    events.tick = timedelta(seconds=30)  # it doesn't take a wait to find out

    with pytest.raises(EventsLostError):
        await read(events, new_id())


async def test_a_stream_doesnt_outlast_a_turn_that_stopped_saying_it_is_alive(
    events: TurnEvents, valkey: Valkey
) -> None:
    turn = new_id()
    await events.open(turn, b"start")
    opened = await valkey.pttl(events.key(turn))
    await valkey.pexpire(events.key(turn), 1_000)  # as if most of that time had passed

    await events.keep(turn)
    kept = await valkey.pttl(events.key(turn))

    whole = STALE_AFTER // timedelta(milliseconds=1)
    assert whole - 2_000 < opened <= whole
    assert whole - 2_000 < kept <= whole


async def test_a_stream_goes_soon_after_its_last_event(events: TurnEvents, valkey: Valkey) -> None:
    turn = new_id()
    await events.open(turn, b"start")
    await events.close(turn, b"done")

    left = await valkey.pttl(events.key(turn))

    whole = KEPT_AFTER_THE_END // timedelta(milliseconds=1)
    assert whole - 2_000 < left <= whole
    assert await read(events, turn) == [b"start", b"done"]  # still there for who is reading


async def test_a_stream_has_one_beginning(events: TurnEvents) -> None:
    turn = new_id()
    await events.open(turn, b"an earlier start")
    await events.open(turn, b"start")
    await events.close(turn, b"done")

    assert await read(events, turn) == [b"start", b"done"]


async def test_a_stream_that_never_started_can_be_discarded(
    events: TurnEvents, valkey: Valkey
) -> None:
    turn = new_id()
    await events.open(turn, b"start")

    await events.discard(turn)
    await events.discard(turn)  # and once more does no harm

    assert not await valkey.exists(events.key(turn))


async def test_without_valkey_nothing_is_written_or_read() -> None:
    turn = new_id()
    async with Valkey.from_url("valkey://127.0.0.1:1/0", socket_connect_timeout=0.5) as nowhere:
        away = TurnEvents(nowhere, prefix="away")

        with pytest.raises(EventsUnavailableError):
            await away.open(turn, b"start")
        with pytest.raises(EventsUnavailableError):
            await away.append(turn, b"one")
        with pytest.raises(EventsUnavailableError):
            await away.close(turn, b"done")
        with pytest.raises(EventsUnavailableError):
            await away.keep(turn)
        with pytest.raises(EventsUnavailableError):
            await read(away, turn)
        await away.discard(turn)  # gives up quietly: there is nothing to delete
