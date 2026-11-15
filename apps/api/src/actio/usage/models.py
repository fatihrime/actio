"""The ledger's table: the lasting record of what was used."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, Index
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

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
