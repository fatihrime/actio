"""A running turn says that it is alive.

`turns` gains `alive_at`: when the process that writes the answer last said it was
still at it. A turn runs apart from the request that started it, so nothing else shows
whether a turn marked `running` is being written or was left behind by a process that
died. One that has been silent for a minute is given up on.

Until now a turn was given up on ten minutes after it was created, however alive it
was. The turns that exist get the time they were created as the last time they were
heard from: for a finished turn it means nothing, and one that is still marked
`running` during the upgrade was left by a process that is gone.

Revision: 0008
Follows: 0007
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("turns", sa.Column("alive_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE turns SET alive_at = created_at")
    op.alter_column("turns", "alive_at", nullable=False)


def downgrade() -> None:
    op.drop_column("turns", "alive_at")
