"""The usage ledger, and what a turn cost.

- `usage_ledger`: what each request used, written once when the request has ended. The
  counters that enforce the limits live in Valkey and can be lost; they are rebuilt
  from these rows. So a row is never changed and never deleted: a trigger refuses
  UPDATE, DELETE and TRUNCATE, whatever code tries.
- `turns` gains `credits`, what the turn cost, and `capped_by_usage`, whether a usage
  limit left its answer less room than the model allows.

Revision: 0007
Follows: 0006
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "usage_ledger",
        sa.Column("reservation_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("credits", sa.BigInteger(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("cached_input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("estimated", sa.Boolean(), nullable=False),
        sa.Column("windows", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("credits >= 0", name=op.f("ck_usage_ledger_credits_are_not_negative")),
        sa.PrimaryKeyConstraint("reservation_id", name=op.f("pk_usage_ledger")),
    )
    op.create_index(
        op.f("ix_usage_ledger_user_id_occurred_at"),
        "usage_ledger",
        ["user_id", "occurred_at"],
        unique=False,
    )
    # Rows are only ever added. The application has no code that changes or deletes
    # one; these triggers make sure that no later code, and no slip at a prompt, does.
    op.execute(
        """
        CREATE FUNCTION usage_ledger_is_append_only() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'usage_ledger only takes new rows: % is refused', TG_OP
                USING ERRCODE = 'restrict_violation';
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER usage_ledger_rows_stay
        BEFORE UPDATE OR DELETE ON usage_ledger
        FOR EACH ROW EXECUTE FUNCTION usage_ledger_is_append_only()
        """
    )
    op.execute(
        """
        CREATE TRIGGER usage_ledger_is_not_emptied
        BEFORE TRUNCATE ON usage_ledger
        FOR EACH STATEMENT EXECUTE FUNCTION usage_ledger_is_append_only()
        """
    )

    op.add_column("turns", sa.Column("credits", sa.BigInteger(), nullable=True))
    op.add_column(
        "turns",
        sa.Column("capped_by_usage", sa.Boolean(), server_default=sa.false(), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("turns", "capped_by_usage")
    op.drop_column("turns", "credits")
    # Dropping the table takes its triggers with it; the function is its own object.
    op.drop_index(op.f("ix_usage_ledger_user_id_occurred_at"), table_name="usage_ledger")
    op.drop_table("usage_ledger")
    op.execute("DROP FUNCTION usage_ledger_is_append_only()")
