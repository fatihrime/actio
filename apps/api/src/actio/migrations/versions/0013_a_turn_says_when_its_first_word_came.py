"""A turn says when its first word came.

`turns` gains `first_word_at`: when the first piece of the model's answer came, its
reasoning included, by the clock of the process that wrote the turn. It is written with
the turn's outcome. A turn whose model wrote nothing has none, and neither has one that
was given up on: its process said nothing of it.

The turns that exist get none: nothing had noted the moment.

Revision: 0013
Follows: 0012
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("turns", sa.Column("first_word_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("turns", "first_word_at")
