"""The ledger's tables: the lasting record of what was used, and which of it the
counters haven't been told of yet."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from actio.core.database import Base


class UsageEntry(Base):
    """What one request used, written once, when the request has ended.

    The counters in Valkey decide whether a request may run, and they can be lost. This
    table is what they are rebuilt from, so a row is never changed and never deleted:
    the database refuses both (see migration `0007`).

    `user_id` is not a foreign key. A guest's account is deleted a day after it was
    created, and its rows here stay: they then name an id that belongs to nobody.
    """

    __tablename__ = "usage_ledger"
    __table_args__ = (
        CheckConstraint("credits >= 0", name="credits_are_not_negative"),
        # For rebuilding an account's counters: its rows since its windows opened.
        Index(None, "user_id", "occurred_at"),
    )

    reservation_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    """The reservation that was settled: for a turn, the turn's own id. Being the key,
    it also keeps a request from being written twice."""
    user_id: Mapped[uuid.UUID]
    model: Mapped[str]
    """The model's name in the catalog, such as `standard`."""
    credits: Mapped[int] = mapped_column(BigInteger)
    """What the request cost, at the prices of the moment."""
    input_tokens: Mapped[int]
    cached_input_tokens: Mapped[int]
    output_tokens: Mapped[int]
    estimated: Mapped[bool]
    """Whether the token counts are Actio's own guess, because the provider reported
    none: a stopped answer never gets its count."""
    windows: Mapped[dict[str, Any]] = mapped_column(JSONB)
    """The windows the request was admitted into, each with the moment it opened, in
    milliseconds: `{"session": 1794312000000, "week": …}`. Usage counts toward the
    window that admitted it, so a rebuild needs these, not the time of writing."""
    occurred_at: Mapped[datetime]


class Uncounted(Base):
    """A row of the ledger that hasn't reached the counters in Valkey, as far as the
    database knows.

    Written with the ledger's row, in the same transaction, and deleted once the
    counters have answered a settle for it. What is left, because Valkey didn't answer
    or the process died in between, the worker settles again (`actio.usage.missed`).

    Who used what is in the ledger's row alone: this one only points at it, so the two
    can't disagree. Unlike the ledger's, these rows are deleted.
    """

    __tablename__ = "usage_uncounted"

    reservation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(UsageEntry.reservation_id), primary_key=True
    )
    entry: Mapped[UsageEntry] = relationship(lazy="raise")
    """The ledger's row, never loaded. It is here so that the ledger's row is written
    first when the two are added together: a foreign key alone doesn't order them, and
    SQLAlchemy would write this one first, pointing at a row that isn't there yet."""
