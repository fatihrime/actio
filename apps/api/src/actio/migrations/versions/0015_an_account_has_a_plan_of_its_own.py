"""An account has a plan of its own.

`users` gains `plan`: the name of the plan in the plans file that the account is on of
its own. Empty, the account has the plan the file names for accounts, as every account
had until now (ADR 0020, revision "an account has a plan of its own").

A check constraint keeps it empty for a guest: only a lasting account is put on a plan,
and a guest has the guests'. Alembic doesn't compare check constraints, so this one is
written here by hand, as 0003's is.

Every existing row gets none, so the constraint holds for them. Going back drops the
column, and with it which plan each account was on of its own: they all have the
accounts' plan again.

Revision: 0015
Follows: 0014
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ONLY_AN_ACCOUNT_HAS_A_PLAN = "plan IS NULL OR NOT is_guest"


def upgrade() -> None:
    op.add_column("users", sa.Column("plan", sa.String(), nullable=True))
    op.create_check_constraint(
        op.f("ck_users_only_an_account_has_a_plan"), "users", ONLY_AN_ACCOUNT_HAS_A_PLAN
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_users_only_an_account_has_a_plan"), "users", type_="check")
    op.drop_column("users", "plan")
