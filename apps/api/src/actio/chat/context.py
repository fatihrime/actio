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

INSTRUCTIONS = (
    "You are a helpful assistant.\n\n"
    "Today's date is {day}. Use it when a question depends on the date. "
    "Otherwise, don't mention it."
)
"""What a model is told before the conversation.

A model doesn't know what day it is, so it is told. But told nothing else, a small
model takes the date for its subject: asked for some Python, it answered "Hello! Today
is November 14, 2026", and then wrote code that printed the date. So the date comes
after who the model is, and with when to use it. See ADR 0014's revision of 2026-11-14
for what was measured.
"""


def instructions(now: datetime) -> str:
    """The instructions for a turn that starts at `now`.

    - They give the day, not the time: the same instructions all day let the model's
      server reuse what it has already read.
    - They give the day of the week too. Left to work it out, a small model gets it
      wrong, and then bends the date to fit: told "14 November 2026", it answered
      "Friday, November 13".
    """
    return INSTRUCTIONS.format(day=f"{now:%A}, {now.day} {now:%B %Y}")


def for_model(history: list[Message], *, now: datetime) -> list[ModelMessage]:
    """The messages to send: the instructions, then the branch, oldest first.

    Only text goes back to the model. Its earlier reasoning is left out: it was never
    part of what was said, and would only fill the context.
    """
    said: list[ModelMessage] = [System(instructions(now))]
    for message in history:
        text = "".join(
            part.text for part in from_stored(message.parts) if isinstance(part, TextPart)
        )
        if not text.strip():
            continue  # an answer that was cut off before any text has nothing to send back
        said.append(User(text) if message.role == "user" else Assistant(text))
    return said
