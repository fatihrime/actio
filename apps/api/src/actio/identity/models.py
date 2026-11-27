"""The tables behind accounts and sessions."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, LargeBinary
from sqlalchemy.orm import Mapped, mapped_column

from actio.core.database import Base


class User(Base):
    """An account: a guest, or a lasting one with an email address and a password.

    A guest is an account nobody can log in to again. It lasts as long as the one
    session it was created with, unless its owner signs up: then the same row becomes a
    lasting account, and keeps everything it owns.
    """

    __tablename__ = "users"
    __table_args__ = (
        # A row is wholly one kind or the other, whatever code writes it.
        CheckConstraint(
            "(is_guest AND email IS NULL AND password_hash IS NULL AND expires_at IS NOT NULL)"
            " OR (NOT is_guest AND email IS NOT NULL AND password_hash IS NOT NULL"
            " AND expires_at IS NULL)",
            name="guest_or_account",
        ),
        # Only a lasting account is put on a plan of its own: a guest has the guests'.
        CheckConstraint("plan IS NULL OR NOT is_guest", name="only_an_account_has_a_plan"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    is_guest: Mapped[bool]
    created_at: Mapped[datetime]
    expires_at: Mapped[datetime | None] = mapped_column(index=True)
    """For a guest: when the account, and everything it owns, may be deleted. Indexed
    for the sweep that deletes them, oldest first."""
    email: Mapped[str | None] = mapped_column(unique=True)
    """In lower case, so that one address is one account however it was typed."""
    password_hash: Mapped[str | None]
    """An Argon2id hash, which names its own parameters and salt."""
    plan: Mapped[str | None]
    """The plan the account is on of its own, by its name in the plans file. Empty: the
    plan the file names for accounts. Whether the file has it is the plans' to say
    (`actio.usage.plans`), when a request is counted: nothing here reads the file."""


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
    expires_at: Mapped[datetime] = mapped_column(index=True)
