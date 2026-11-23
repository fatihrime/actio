"""What every request's body is: `Body`.

A body is JSON that anyone can send, and JSON can spell two kinds of character that
can't be stored: U+0000, and half of a surrogate pair (`actio.core.text`). Left in,
either fails later and somewhere else: in the database, in the hashing of a password,
or in the very response that refuses the request and names what it refuses.

So a body is looked through before anything reads it, and each such character becomes
U+FFFD: in every string, at any member, the members' names included. A route's body is
a `Body`, or a test fails.
"""

from typing import Any

from pydantic import model_validator

from actio.api.described import Described
from actio.core.text import storable

LOOKED_THROUGH = 16
"""How deep a body is looked through, in objects and lists inside each other. No request
of this API goes deeper than four. Below this nothing is looked at, and no route reads
anything there: a body nested a thousand deep isn't walked to its bottom. What is down
there is refused for its form, or passed over where a body may hold more than it needs.
The refusal of a body says where it is wrong and never repeats what was sent
(`actio.api.problems`), so what wasn't looked at isn't written into a response."""


class Body(Described):
    """The body of a request. Every string in it can be stored."""

    @model_validator(mode="before")
    @classmethod
    def _made_storable(cls, sent: Any) -> Any:
        return _looked_through(sent, LOOKED_THROUGH)


def _looked_through(sent: Any, deeper: int) -> Any:
    """`sent`, with every string in it made storable, down `deeper` more levels."""
    if isinstance(sent, str):
        return storable(sent)
    if deeper == 0:
        return sent
    if isinstance(sent, dict):
        return {
            (storable(name) if isinstance(name, str) else name): _looked_through(value, deeper - 1)
            for name, value in sent.items()
        }
    if isinstance(sent, list):
        return [_looked_through(value, deeper - 1) for value in sent]
    return sent
