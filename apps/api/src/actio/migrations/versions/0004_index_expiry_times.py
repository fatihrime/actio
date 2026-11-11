"""Indexes on the expiry times, for the sweep that deletes what has expired.

- `users.expires_at`: expired guests are found oldest first, without reading every
  account.
- `sessions.expires_at`: the same for expired sessions.

Revision: 0004
Follows: 0003
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(op.f("ix_users_expires_at"), "users", ["expires_at"], unique=False)
    op.create_index(op.f("ix_sessions_expires_at"), "sessions", ["expires_at"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_sessions_expires_at"), table_name="sessions")
    op.drop_index(op.f("ix_users_expires_at"), table_name="users")
