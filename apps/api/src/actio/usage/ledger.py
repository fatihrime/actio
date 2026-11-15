"""Writing to the ledger, and reading it back.

A row is added in the same transaction that records how a request ended, and before
the counters in Valkey are told: whatever the counters hold, the ledger has it too.

These functions never commit, and take the time as an argument.
"""

import uuid
from collections.abc import Mapping
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from actio.usage.models import UsageEntry
from actio_quota import LedgerEntry, Usage


def add(
    db: AsyncSession,
    *,
    reservation_id: uuid.UUID,
    user_id: uuid.UUID,
    model: str,
    credits: int,
    tokens: Usage,
    estimated: bool,
    windows: Mapping[str, int],
    now: datetime,
) -> UsageEntry:
    """Write what one request used. The caller's transaction decides when it counts."""
    entry = UsageEntry(
        reservation_id=reservation_id,
        user_id=user_id,
        model=model,
        credits=credits,
        input_tokens=tokens.input_tokens,
        cached_input_tokens=tokens.cached_input_tokens,
        output_tokens=tokens.output_tokens,
        estimated=estimated,
        windows=dict(windows),
        occurred_at=now,
    )
    db.add(entry)
    return entry


async def since(db: AsyncSession, user_id: uuid.UUID, moment: datetime) -> list[LedgerEntry]:
    """What an account used from `moment` on, as the engine replays it in a rebuild.

    A request is written after the window that admitted it opened. So the rows since the
    oldest window that is still open are all that a rebuild can need.
    """
    rows = await db.execute(
        select(UsageEntry.reservation_id, UsageEntry.credits, UsageEntry.windows).where(
            UsageEntry.user_id == user_id, UsageEntry.occurred_at >= moment
        )
    )
    return [
        LedgerEntry(
            str(reservation_id), credits, {name: int(start) for name, start in windows.items()}
        )
        for reservation_id, credits, windows in rows
    ]
