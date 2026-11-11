"""Lasting accounts: an email address and a password hash.

- `users.email`, unique, and `users.password_hash`: both empty for a guest.
- A check constraint keeps each row wholly a guest (it expires, and has neither) or
  wholly an account (it has both, and doesn't expire).

Every existing row is a guest with an expiry time, so the constraint holds for them.

Revision: 0003
Follows: 0002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

GUEST_OR_ACCOUNT = (
    "(is_guest AND email IS NULL AND password_hash IS NULL AND expires_at IS NOT NULL)"
    " OR (NOT is_guest AND email IS NOT NULL AND password_hash IS NOT NULL"
    " AND expires_at IS NULL)"
)


def upgrade() -> None:
    op.add_column("users", sa.Column("email", sa.String(), nullable=True))
    op.add_column("users", sa.Column("password_hash", sa.String(), nullable=True))
    op.create_unique_constraint(op.f("uq_users_email"), "users", ["email"])
    op.create_check_constraint(op.f("ck_users_guest_or_account"), "users", GUEST_OR_ACCOUNT)


def downgrade() -> None:
    # Accounts can't exist without these columns: only guests survive going back.
    op.execute("DELETE FROM users WHERE NOT is_guest")
    op.drop_constraint(op.f("ck_users_guest_or_account"), "users", type_="check")
    op.drop_constraint(op.f("uq_users_email"), "users", type_="unique")
    op.drop_column("users", "password_hash")
    op.drop_column("users", "email")
