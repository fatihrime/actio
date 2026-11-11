"""What a message is made of: a list of parts, in the order they were produced.

A person's message is text. A model's answer can start with its reasoning, then the
text of the answer. Tool calls, their results and sources will be further kinds of part.

The parts are stored as JSON, each with a `type`. The names follow the AI SDK's message
parts, which the web app renders, so turning a stored message into one the web app
understands is a matter of copying. But the stored form is Actio's own, checked by the
classes below: a new version of that library can change what travels to the browser
without touching what is in the database.
"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class TextPart(BaseModel):
    """Text for the reader: a person's message, or a model's answer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["text"] = "text"
    text: str


class ReasoningPart(BaseModel):
    """What a model thought before answering. Shown apart from the answer, and never
    sent back to a model as if it had been said."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["reasoning"] = "reasoning"
    text: str


type Part = Annotated[TextPart | ReasoningPart, Field(discriminator="type")]

_PARTS: TypeAdapter[list[Part]] = TypeAdapter(list[Part])


def to_stored(parts: list[Part]) -> list[dict[str, Any]]:
    """Parts as the plain JSON that goes into the database."""
    stored: list[dict[str, Any]] = _PARTS.dump_python(parts, mode="json")
    return stored


def from_stored(stored: list[dict[str, Any]]) -> list[Part]:
    """Parts read back from the database. Raises if a row holds something unknown."""
    return _PARTS.validate_python(stored)
