"""What a model is shown when it answers: the conversation so far, in its own format.

The history is one branch of the conversation's tree, from its first message down to
the question. The server builds it from what it stored: a client sends only its new
message, so it can't put words into the conversation that were never said.

How much of the model's context window a conversation takes after a turn follows from
the same rule, what goes back to a model and what doesn't, and is worked out here
beside it (`tokens_after`). Change what goes back in `_goes_back`, and both change.
"""

from collections.abc import Sequence
from datetime import datetime

from actio.chat.models import Message
from actio.chat.parts import Part, ReasoningPart, TextPart, from_stored
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
        text = _goes_back(from_stored(message.parts))
        if text is None:
            continue  # an answer that was cut off before any text has nothing to send back
        said.append(User(text) if message.role == "user" else Assistant(text))
    return said


def tokens_after(
    answer: Sequence[Part], *, input_tokens: int | None, output_tokens: int | None
) -> int | None:
    """About how many tokens of its model's context window a conversation takes after a
    turn: what the model was shown, and of what it wrote, what goes back to a model with
    the next question. `answer` is what the turn wrote, as the parts of its answer, and
    empty when it wrote nothing; the two counts are its model server's.

    - Either count missing: nothing. A stopped or failed turn has none.
    - Nothing of the answer goes back (`for_model` sends it no more): what was shown.
    - Its words go back, and it thought nothing: what was shown, and all it wrote.
    - It thought, and its words go back: what was shown, and the words' share of what
      it wrote, by characters, rounded to a whole token. That share is a guess: no
      provider Actio talks to says how many of the tokens it wrote were reasoning.

    So the figure is never less than what was shown, nor more than the two counts
    together, which is what the window held when the answer ended, its thought
    included. See ADR 0014, revision "a turn says how much of the window its
    conversation takes".
    """
    if input_tokens is None or output_tokens is None:
        return None
    said = _goes_back(answer)
    if said is None:
        return input_tokens
    thought = sum(len(part.text) for part in answer if isinstance(part, ReasoningPart))
    if thought == 0:
        return input_tokens + output_tokens
    # A share below one, rounded: never more than all that was written.
    return input_tokens + round(output_tokens * len(said) / (len(said) + thought))


def _goes_back(parts: Sequence[Part]) -> str | None:
    """What of a message goes back to a model: its text, without what it thought.
    `None` when that is nothing, or nothing but blank space: then nothing of it does."""
    text = "".join(part.text for part in parts if isinstance(part, TextPart))
    return text if text.strip() else None
