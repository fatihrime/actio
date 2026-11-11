"""Conversations, and the tree of their messages.

- `conversations`: one conversation of one account. Deleting the account deletes them.
  `current_leaf_id` is the last message of the branch that is shown.
- `messages`: a message follows its parent, `parent_id`. Messages with the same parent
  are versions of each other, so the messages of a conversation form a tree.

The two tables point at each other, so the pointer from a conversation to its shown
message is added last. Both pointers to a message name the conversation too: a message
can only follow, and a conversation can only show, a message of that same conversation.

Revision: 0005
Follows: 0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("current_leaf_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_active_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_conversations_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversations")),
    )
    op.create_index(
        op.f("ix_conversations_user_id_last_active_at_id"),
        "conversations",
        ["user_id", "last_active_at", "id"],
        unique=False,
    )
    op.create_table(
        "messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("parent_id", sa.Uuid(), nullable=True),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("parts", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("role IN ('user', 'assistant')", name=op.f("ck_messages_role_is_known")),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_messages_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id", "parent_id"],
            ["messages.conversation_id", "messages.id"],
            name=op.f("fk_messages_conversation_id_parent_id_messages"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
        sa.UniqueConstraint("conversation_id", "id", name=op.f("uq_messages_conversation_id_id")),
    )
    op.create_index(
        op.f("ix_messages_conversation_id_parent_id"),
        "messages",
        ["conversation_id", "parent_id"],
        unique=False,
    )
    op.create_foreign_key(
        op.f("fk_conversations_id_current_leaf_id_messages"),
        "conversations",
        "messages",
        ["id", "current_leaf_id"],
        ["conversation_id", "id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_conversations_id_current_leaf_id_messages"), "conversations", type_="foreignkey"
    )
    op.drop_index(op.f("ix_messages_conversation_id_parent_id"), table_name="messages")
    op.drop_table("messages")
    op.drop_index(op.f("ix_conversations_user_id_last_active_at_id"), table_name="conversations")
    op.drop_table("conversations")
