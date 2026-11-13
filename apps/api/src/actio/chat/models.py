"""The tables behind conversations: the conversations, their messages, and the turns
in which a model answered."""

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
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from actio.core.database import Base
from actio.identity.models import User

Role = Literal["user", "assistant"]
TurnStatus = Literal["running", "completed", "stopped", "failed", "interrupted"]


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


class Turn(Base):
    """One attempt to answer a question: which model was asked, and how it ended.

    A question can be answered more than once, so it can have several turns. A turn
    that produced something points at the answer it wrote. One that failed before the
    model said anything has no answer: the question stays, to be asked again.
    """

    __tablename__ = "turns"
    __table_args__ = (
        # The question and the answer are messages of the turn's own conversation.
        ForeignKeyConstraint(
            ["conversation_id", "question_id"],
            ["messages.conversation_id", "messages.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["conversation_id", "answer_id"],
            ["messages.conversation_id", "messages.id"],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "status IN ('running', 'completed', 'stopped', 'failed', 'interrupted')",
            name="status_is_known",
        ),
        CheckConstraint("(status = 'running') = (finished_at IS NULL)", name="running_or_finished"),
        # One answer is written at a time in a conversation, whatever code starts a turn.
        Index(
            "uq_turns_conversation_id_running",
            "conversation_id",
            unique=True,
            postgresql_where=text("status = 'running'"),
        ),
        Index(None, "conversation_id", "question_id"),
        Index(None, "conversation_id", "answer_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE")
    )
    question_id: Mapped[uuid.UUID]
    answer_id: Mapped[uuid.UUID | None]
    model: Mapped[str]
    """The model's name in the catalog, such as `standard`, as it was asked for."""
    thinking: Mapped[bool]
    """Whether the model was asked to reason before answering."""
    status: Mapped[TurnStatus] = mapped_column(String)
    """`running` while the answer is written. Then how it ended: `completed`; `stopped`,
    because whoever asked left; `failed`, because the model did; or `interrupted`,
    because the process that was writing it is gone."""
    finish_reason: Mapped[str | None]
    """Why the model stopped writing, for a completed turn: `stop`, `length`, …"""
    error: Mapped[str | None]
    """What went wrong, for a failed turn, as a short code: see `actio.chat.answering`."""
    input_tokens: Mapped[int | None]
    cached_input_tokens: Mapped[int | None]
    output_tokens: Mapped[int | None]
    """Token counts as the provider reported them. Empty when it reported none: a turn
    that was stopped, or failed, usually has none."""
    load_ms: Mapped[int | None]
    prompt_ms: Mapped[int | None]
    generation_ms: Mapped[int | None]
    """Where the time went, for providers that say: loading the model, reading the
    prompt, writing the answer."""
    created_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]
