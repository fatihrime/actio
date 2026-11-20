"""A conversation knows who named it.

`conversations` gains two columns, for the titles a model writes:

- `title_by`: who named the conversation. `question` while its title is its first
  question's first line, or empty, which a model may replace; `model` once a model has
  written it; `person` once someone has renamed it, and nothing writes over it then. A
  check constraint allows these three alone.
- `title_asked_at`: when a model was asked for the title. Empty: never.

The conversations that exist get `person`. Nothing recorded whether they were renamed,
so each is taken as named by a person: no model ever writes over a title someone may
have chosen, and none of them is asked for one. The default that fills the column for
them is dropped again: the code always says who.

Revision: 0012
Follows: 0011
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("title_by", sa.String(), nullable=False, server_default="person"),
    )
    op.alter_column("conversations", "title_by", server_default=None)
    op.add_column(
        "conversations", sa.Column("title_asked_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_check_constraint(
        op.f("ck_conversations_title_by_is_known"),
        "conversations",
        "title_by IN ('question', 'model', 'person')",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_conversations_title_by_is_known"), "conversations", type_="check")
    op.drop_column("conversations", "title_asked_at")
    op.drop_column("conversations", "title_by")
