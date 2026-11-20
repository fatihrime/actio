"""Usage the counters haven't been told of yet.

`usage_uncounted`: a row for each row of the ledger that hasn't reached the counters in
Valkey, as far as the database knows. It is written with the ledger's row, in the same
transaction, and deleted once the counters have answered the settle that follows. What
is left, because Valkey didn't answer or the process died between the commit and the
settle, the worker settles again. Until now nothing did: such usage reached the counters
only when they were next rebuilt from the ledger, which for counters that survive may
be never.

It holds only the ledger row's id: who used what stays in the ledger alone, so the two
can't disagree. Unlike the ledger's, its rows are deleted.

The rows already in the ledger get none. Whatever they missed, nothing can tell now;
they are counted at the next rebuild, as before.

Revision: 0011
Follows: 0010
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "usage_uncounted",
        sa.Column("reservation_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["reservation_id"],
            ["usage_ledger.reservation_id"],
            name=op.f("fk_usage_uncounted_reservation_id_usage_ledger"),
        ),
        sa.PrimaryKeyConstraint("reservation_id", name=op.f("pk_usage_uncounted")),
    )


def downgrade() -> None:
    op.drop_table("usage_uncounted")
