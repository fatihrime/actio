"""Baseline: the schema starts empty.

Tables arrive with the features that own them. This first revision exists so that a
database can be told apart from one that was never migrated: the readiness probe
compares the revision a database is at with the one the code expects.

Revision: 0001
Follows: nothing
"""

from collections.abc import Sequence

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
