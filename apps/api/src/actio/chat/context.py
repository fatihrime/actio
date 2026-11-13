"""What a model is shown when it answers: the conversation so far, in its own format.

The history is one branch of the conversation's tree, from its first message down to
the question. The server builds it from what it stored: a client sends only its new
message, so it can't put words into the conversation that were never said.
"""

from datetime import datetime

from actio.chat.models import Message
from actio.chat.parts import TextPart, from_stored
from actio_llm import Assistant, System, User
from actio_llm import Message as ModelMessage


def for_model(history: list[Message], *, now: datetime) -> list[ModelMessage]:
    """The messages to send: a line of instructions, then the branch, oldest first.

    Only text goes back to the model. Its earlier reasoning is left out: it was never
    part of what was said, and would only fill the context.
    """
    # A model doesn't know what day it is. The day, not the time: the same instructions
    # all day let the model server reuse what it has already read.
    said: list[ModelMessage] = [System(f"Today's date is {now.day} {now:%B %Y}.")]
    for message in history:
        text = "".join(
            part.text for part in from_stored(message.parts) if isinstance(part, TextPart)
        )
        if not text.strip():
            continue  # an answer that was cut off before any text has nothing to send back
        said.append(User(text) if message.role == "user" else Assistant(text))
    return said
