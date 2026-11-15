"""The guess at a text's tokens: its three rules, and what it adds for a whole prompt.

How close the guess comes to a real tokenizer can't be shown here: `test_ollama_live.py`
compares it with a server's own count.
"""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from actio_llm import Assistant, Message, System, ToolCall, ToolResult, ToolSpec, User
from actio_llm.estimate import PER_MESSAGE, PER_PROMPT, prompt_tokens, tokens_in


@pytest.mark.parametrize(
    ("text", "tokens"),
    [
        ("", 0),
        ("word", 1),  # four bytes to a token
        ("words", 2),  # rounded up
        ("two words", 2),  # the space is free: it travels with the word after it
        ("   \n\t  ", 0),
        ("2026", 4),  # a digit is a token
        ("1,250.00", 8),  # and so is a punctuation mark
        ('{"id": 42}', 2 + 5 + 1),  # two digits, five marks, and `id`
        ("çay", 1),  # four bytes: `ç` takes two
        ("自転車", 3),  # nine bytes
    ],
)
def test_a_text_is_counted_by_three_rules(text: str, tokens: int) -> None:
    assert tokens_in(text) == tokens


def test_numbers_are_not_counted_like_words() -> None:
    """Counting bytes alone, a page of numbers came out under half of what the model
    made of it. Each digit is a token of its own."""
    prose = "the chain climbs onto the next sprocket " * 10
    numbers = "4821 1250 9995 1499 2026 1100 4200 8090 " * 10

    assert len(prose) == len(numbers)
    assert tokens_in(numbers) == 320  # every digit
    assert tokens_in(prose) < tokens_in(numbers) / 3


@given(st.text(), st.text())
def test_a_longer_text_is_never_fewer_tokens(first: str, second: str) -> None:
    assert tokens_in(first + second) >= tokens_in(first)
    # Rounding up twice can't come to less than rounding up once.
    assert tokens_in(first) + tokens_in(second) >= tokens_in(first + second)


@given(st.text())
def test_a_text_is_at_most_a_token_a_byte(text: str) -> None:
    assert 0 <= tokens_in(text) <= len(text.encode())


def test_a_prompt_adds_what_a_chat_template_writes_around_each_message() -> None:
    messages: list[Message] = [System("word"), User("two words")]

    assert prompt_tokens(messages) == PER_PROMPT + 2 * PER_MESSAGE + 1 + 2


def test_a_prompt_counts_what_a_tool_loop_sends_back() -> None:
    call = ToolCall("call-1", "lookup", {"sku": "VC42"})
    plain: list[Message] = [User("Is it in stock?"), Assistant("Let me look.")]
    looped: list[Message] = [
        User("Is it in stock?"),
        Assistant("Let me look.", tool_calls=(call,), reasoning="I should check."),
        ToolResult("call-1", "lookup", "3 left"),
    ]

    extra = prompt_tokens(looped) - prompt_tokens(plain)

    assert extra == (
        tokens_in("I should check.")
        + tokens_in("lookup")
        + tokens_in('{"sku": "VC42"}')
        + PER_MESSAGE
        + tokens_in("3 left")
    )


def test_a_prompt_counts_the_tools_offered_with_it() -> None:
    messages = [User("Is it in stock?")]
    tool = ToolSpec("lookup", "Look a part up by its number.", {"type": "object"})

    with_tool = prompt_tokens(messages, [tool])

    assert with_tool > prompt_tokens(messages) + tokens_in("Look a part up by its number.")
    assert prompt_tokens(messages, [tool, tool]) - with_tool == with_tool - prompt_tokens(messages)
