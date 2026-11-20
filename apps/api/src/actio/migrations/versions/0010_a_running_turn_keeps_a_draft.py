"""A running turn keeps a draft of its answer.

`turns` gains `draft`: what the answer holds so far, as message parts, with the id its
message will have. The process that writes the answer saves it with each beat, in the
statement that writes `alive_at`. A turn whose process died is given up on with its
draft as its answer, where until now what it had written was lost.

A check constraint allows a draft on a running turn only: whatever ends a turn empties
it. Every existing turn gets none, so the constraint holds for them. A turn still marked
`running` during the upgrade is given up on as before, without an answer.

Revision: 0010
Follows: 0009
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "turns",
        sa.Column(
            "draft", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True
        ),
    )
    op.create_check_constraint(
        op.f("ck_turns_draft_only_while_running"), "turns", "status = 'running' OR draft IS NULL"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_turns_draft_only_while_running"), "turns", type_="check")
    op.drop_column("turns", "draft")
