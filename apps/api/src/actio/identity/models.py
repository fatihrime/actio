"""The tables behind accounts and sessions."""

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, LargeBinary
from sqlalchemy.orm import Mapped, mapped_column

from actio.core.database import Base


class User(Base):
    """An account. A guest is an account nobody can log in to again: it lasts as long
    as the one session it was created with."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    is_guest: Mapped[bool]
    created_at: Mapped[datetime]
    expires_at: Mapped[datetime | None]
    """For a guest: when the account, and everything it owns, may be deleted."""


class BrowserSession(Base):
    """One browser, logged in as one account, until it expires or is ended."""

    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[bytes] = mapped_column(LargeBinary(32), unique=True)
    """SHA-256 of the session token. The token itself is only in the browser's cookie,
    so a copy of this table can't be used to log in."""
    created_at: Mapped[datetime]
    last_seen_at: Mapped[datetime]
    expires_at: Mapped[datetime]
