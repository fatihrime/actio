"""The tables behind conversations."""

import uuid
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from actio.core.database import Base
from actio.identity.models import User

Role = Literal["user", "assistant"]


class Conversation(Base):
    """One conversation, belonging to one account.

    Its messages form a tree, because a message can be written again: editing a
    question, or asking for another answer, starts a new branch beside the old one.
    `current_leaf_id` says which branch is shown: the one that ends at that message.
    """

    __tablename__ = "conversations"
    __table_args__ = (
        # The shown branch must end in this conversation, not in someone else's.
        ForeignKeyConstraint(
            ["id", "current_leaf_id"],
            ["messages.conversation_id", "messages.id"],
            use_alter=True,  # the two tables point at each other: this one is added last
        ),
        # For the list of an account's conversations, most recently active first.
        Index(None, "user_id", "last_active_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    # The accounts' table is named through its class, not as the text "users.id": so
    # loading this module loads that one, and a process that only handles conversations
    # still knows the table they point at.
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(User.id, ondelete="CASCADE"))
    title: Mapped[str | None]
    """Empty until someone, or something, names the conversation."""
    current_leaf_id: Mapped[uuid.UUID | None]
    """The last message of the branch that is shown. Empty while there are no messages."""
    created_at: Mapped[datetime]
    last_active_at: Mapped[datetime]
    """When a message was last added. Renaming a conversation doesn't change it, so
    the list keeps its order."""


class Message(Base):
    """One message: what a person asked, or what a model answered.

    `parent_id` is the message it follows. A first message has none. Messages with the
    same parent are alternatives to each other: versions of a question, or of an answer.
    """

    __tablename__ = "messages"
    __table_args__ = (
        # What the two rules below point at: a message, named together with its conversation.
        UniqueConstraint("conversation_id", "id"),
        # A message follows a message of the same conversation.
        ForeignKeyConstraint(
            ["conversation_id", "parent_id"],
            ["messages.conversation_id", "messages.id"],
            ondelete="CASCADE",
        ),
        CheckConstraint("role IN ('user', 'assistant')", name="role_is_known"),
        # For finding the messages that follow one.
        Index(None, "conversation_id", "parent_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE")
    )
    parent_id: Mapped[uuid.UUID | None]
    role: Mapped[Role] = mapped_column(String)
    """Who wrote it: a person (`user`) or a model (`assistant`)."""
    parts: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    """What the message is made of, in order: see `actio.chat.parts`."""
    created_at: Mapped[datetime]
