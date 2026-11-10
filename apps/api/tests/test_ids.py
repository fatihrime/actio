"""Row ids: UUID version 7, as RFC 9562 lays it out."""

import time
import uuid

from actio.core.ids import made_at_ms, new_id


def test_an_id_is_a_version_7_uuid() -> None:
    value = new_id()

    assert value.version == 7
    assert value.variant == uuid.RFC_4122


def test_an_id_starts_with_the_time_it_was_made() -> None:
    before = time.time_ns() // 1_000_000
    value = new_id()
    after = time.time_ns() // 1_000_000

    assert before <= made_at_ms(value) <= after
    assert made_at_ms(new_id(at_ms=1_800_000_000_000)) == 1_800_000_000_000


def test_ids_sort_by_the_time_they_were_made() -> None:
    """That is the point of version 7: new rows land together in an index."""
    ids = [new_id(at_ms=moment) for moment in (1, 2, 1_000, 1_800_000_000_000, 2**47)]

    assert ids == sorted(ids)
    assert [str(value) for value in ids] == sorted(str(value) for value in ids)


def test_ids_made_in_the_same_millisecond_differ() -> None:
    assert len({new_id(at_ms=1_800_000_000_000) for _ in range(10_000)}) == 10_000
