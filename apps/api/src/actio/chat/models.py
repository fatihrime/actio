"""The tables behind conversations: the conversations, their messages, and the turns
in which a model answered."""

import uuid
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    false,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from actio.core.database import Base
from actio.identity.models import User

Role = Literal["user", "assistant"]
TurnStatus = Literal["running", "completed", "stopped", "failed", "interrupted"]
TitleBy = Literal["question", "model", "person"]


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
        CheckConstraint("title_by IN ('question', 'model', 'person')", name="title_by_is_known"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    # The accounts' table is named through its class, not as the text "users.id": so
    # loading this module loads that one, and a process that only handles conversations
    # still knows the table they point at.
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(User.id, ondelete="CASCADE"))
    title: Mapped[str | None]
    """Empty until someone, or something, names the conversation."""
    title_by: Mapped[TitleBy] = mapped_column(String)
    """Who named the conversation: `question`, its title is still its first question's
    first line, or empty, and a model may replace it; `model`, a model wrote it
    (`actio.chat.titles`); `person`, someone renamed it, and nothing writes over it."""
    title_asked_at: Mapped[datetime | None]
    """When a model was asked for the title. Empty: never."""
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
        CheckConstraint("status = 'running' OR draft IS NULL", name="draft_only_while_running"),
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
    answered_by: Mapped[str | None]
    """The name in the catalog of the model that wrote the turn's answer: the one asked,
    or one asked in its place (ADR 0028). Written with the turn's outcome. Empty while
    the turn runs, when no model handed anything on, for a turn that was given up on,
    whose process said nothing of it, and for the turns recorded before it was kept."""
    thinking: Mapped[bool]
    """Whether the model was asked to reason before answering."""
    status: Mapped[TurnStatus] = mapped_column(String)
    """`running` while the answer is written. Then how it ended: `completed`; `stopped`,
    because whoever asked stopped it; `failed`, because the model did; or `interrupted`,
    because the process that was writing it shut down, or is gone."""
    finish_reason: Mapped[str | None]
    """Why the model stopped writing, for a completed turn: `stop`, `length`, …"""
    error: Mapped[str | None]
    """What went wrong, for a failed turn, as a short code: see `actio.chat.answering`."""
    input_tokens: Mapped[int | None]
    cached_input_tokens: Mapped[int | None]
    output_tokens: Mapped[int | None]
    """Token counts as the provider reported them. Empty when it reported none: a turn
    that was stopped, or failed, usually has none. Empty too when what it reported
    couldn't be right (`actio.chat.answering`)."""
    load_ms: Mapped[int | None]
    prompt_ms: Mapped[int | None]
    generation_ms: Mapped[int | None]
    """Where the time went, for providers that say: loading the model, reading the
    prompt, writing the answer."""
    credits: Mapped[int | None] = mapped_column(BigInteger)
    """What the turn cost its account, as the usage ledger has it: 0 for a model that
    costs nothing. Empty when nothing was counted for the turn: it failed or was
    interrupted, or it wrote nothing and its model's server counted nothing."""
    capped_by_usage: Mapped[bool] = mapped_column(server_default=false())
    """Whether the answer was given less room than the model allows, because a usage
    limit was near. With the finish reason `length`, that is why it was cut."""
    created_at: Mapped[datetime]
    alive_at: Mapped[datetime]
    """When the process that writes the answer last said it was still at it: every few
    seconds while the turn runs. A running turn that has been silent for too long is
    taken for dead: see `actio.chat.turns.STALE_AFTER`."""
    draft: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    """What the answer holds so far, saved with each beat, and the id its message will
    have: see `actio.chat.turns.Draft`. Empty until something is written, and once the
    turn has ended. A turn whose process died is given up on with it as its answer."""
    first_word_at: Mapped[datetime | None]
    """When the first piece of the model's answer came, its reasoning included, by the
    clock of the process that wrote the turn: written with the turn's outcome. Empty
    while the turn runs, when the model wrote nothing, and for a turn that was given up
    on, whose process said nothing of it."""
    finished_at: Mapped[datetime | None]
