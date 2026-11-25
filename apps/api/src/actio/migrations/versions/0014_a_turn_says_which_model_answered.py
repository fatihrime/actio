"""A turn says which model answered.

`turns` gains `answered_by`: the name in the models file of the model that wrote the
turn's answer, the one asked or one asked in its place (ADR 0028). It is written with
the turn's outcome. A turn whose models handed nothing on has none, and neither has one
that was given up on: its process said nothing of it.

The turns that exist get none. Until ADR 0028 the model asked was the only one that
could answer, and since then the record didn't say which one did: only the log did.

Revision: 0014
Follows: 0013
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("turns", sa.Column("answered_by", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("turns", "answered_by")
