"""A guess at how many tokens a text is, without the model's own tokenizer.

Two callers need one before, or without, a provider's count:

- **before a request,** to know whether its prompt fits a budget;
- **after a stream that ended early.** A closed stream never sends its last event, and
  some servers report no counts at all, so nobody counted what was read and written.

The guess is three rules, each from what a tokenizer does:

- a digit is a token: numbers are split digit by digit, or nearly;
- a punctuation mark is a token;
- everything else is four bytes to a token, and white space is free, because a space
  travels with the word after it.

Measured against two models' own counts on Ollama, 2026-11-14, for prompts in English,
Turkish, Python, Japanese and JSON full of numbers: the guess came to between 0.90 and
1.26 of the count for `qwen3.5:4b`, and between 0.66 and 1.47 for `granite4:micro`,
whose tokenizer splits Turkish and Japanese finer and keeps digits together. The table
is in this package's README. Counting bytes alone, at three to a token, gave 0.46 for
that JSON: under half.

So the guess can be a third out either way, because tokenizers differ. A caller that
must not come out short adds its own margin, and corrects itself with the provider's
count when one arrives.
"""

import json
import math
import string
from collections.abc import Sequence

from actio_llm.chat import Assistant, Message, ToolSpec

BYTES_PER_TOKEN = 4
"""For text that is neither digits nor punctuation."""

PER_MESSAGE = 6
"""What a chat template writes around each message: its role, and where it starts and
ends. Measured: about five tokens."""

PER_PROMPT = 10
"""What a chat template adds once: the opening of the answer it asks for. Measured with
two messages: 21 tokens around them in all."""

_ONE_EACH = frozenset(string.digits + string.punctuation)
"""The characters that are a token each. All of them are one byte."""


def tokens_in(text: str) -> int:
    """About how many tokens `text` is."""
    one_each = 0
    spaces = 0  # in bytes: a space outside ASCII takes more than one
    for character in text:
        if character in _ONE_EACH:
            one_each += 1
        elif character.isspace():
            spaces += len(character.encode())
    other = len(text.encode()) - one_each - spaces
    return one_each + math.ceil(other / BYTES_PER_TOKEN)


def prompt_tokens(messages: Sequence[Message], tools: Sequence[ToolSpec] = ()) -> int:
    """About how many tokens a prompt is: its messages, the tools offered with it, and
    what a chat template adds around them."""
    total = PER_PROMPT
    for message in messages:
        total += PER_MESSAGE + tokens_in(message.content)
        if isinstance(message, Assistant):
            total += tokens_in(message.reasoning)
            for call in message.tool_calls:
                total += tokens_in(call.name) + tokens_in(json.dumps(call.arguments))
    for tool in tools:
        described = {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        }
        total += tokens_in(json.dumps(described))
    return total
