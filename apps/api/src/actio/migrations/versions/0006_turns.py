"""Turns: each attempt to answer a question.

- `turns`: which model was asked, how it ended, and what it cost in tokens and time.
  A turn points at the question, and at the answer once one was written; both are
  messages of the turn's own conversation, which the foreign keys require.
- A unique index allows one running turn per conversation: one answer is written at a
  time, whatever code starts a turn.

Revision: 0006
Follows: 0005
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "turns",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("question_id", sa.Uuid(), nullable=False),
        sa.Column("answer_id", sa.Uuid(), nullable=True),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("thinking", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("finish_reason", sa.String(), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("cached_input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("load_ms", sa.Integer(), nullable=True),
        sa.Column("prompt_ms", sa.Integer(), nullable=True),
        sa.Column("generation_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(status = 'running') = (finished_at IS NULL)",
            name=op.f("ck_turns_running_or_finished"),
        ),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'stopped', 'failed', 'interrupted')",
            name=op.f("ck_turns_status_is_known"),
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_turns_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id", "answer_id"],
            ["messages.conversation_id", "messages.id"],
            name=op.f("fk_turns_conversation_id_answer_id_messages"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id", "question_id"],
            ["messages.conversation_id", "messages.id"],
            name=op.f("fk_turns_conversation_id_question_id_messages"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_turns")),
    )
    op.create_index(
        op.f("ix_turns_conversation_id_answer_id"),
        "turns",
        ["conversation_id", "answer_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_turns_conversation_id_question_id"),
        "turns",
        ["conversation_id", "question_id"],
        unique=False,
    )
    op.create_index(
        "uq_turns_conversation_id_running",
        "turns",
        ["conversation_id"],
        unique=True,
        postgresql_where=sa.text("status = 'running'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_turns_conversation_id_running",
        table_name="turns",
        postgresql_where=sa.text("status = 'running'"),
    )
    op.drop_index(op.f("ix_turns_conversation_id_question_id"), table_name="turns")
    op.drop_index(op.f("ix_turns_conversation_id_answer_id"), table_name="turns")
    op.drop_table("turns")
