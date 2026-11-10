"""Row ids: UUID version 7, made by the application.

A UUIDv7 starts with the time it was made, so new rows land next to each other in an
index, unlike random ids, and sort by age. The application makes them, not the database:
an id is known before the row is written, and Postgres 17 has no function for them.

Python gains `uuid.uuid7()` in 3.14. Until this project moves there, these few lines
follow RFC 9562, section 5.7.
"""

import os
import time
import uuid

_VERSION_7 = 0x7 << 76
_VARIANT_RFC = 0b10 << 62


def new_id(*, at_ms: int | None = None) -> uuid.UUID:
    """A new id. `at_ms` fixes its timestamp, for tests."""
    milliseconds = time.time_ns() // 1_000_000 if at_ms is None else at_ms
    random = int.from_bytes(os.urandom(10))
    return uuid.UUID(
        int=(milliseconds & 0xFFFF_FFFF_FFFF) << 80  # 48 bits: Unix time in milliseconds
        | _VERSION_7  # 4 bits: the version
        | (random >> 68) << 64  # 12 bits: random
        | _VARIANT_RFC  # 2 bits: the variant
        | random & 0x3FFF_FFFF_FFFF_FFFF  # 62 bits: random
    )


def made_at_ms(value: uuid.UUID) -> int:
    """The time inside a UUIDv7, in milliseconds since the Unix epoch."""
    return value.int >> 80
