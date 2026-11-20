"""Text that can be stored.

Two kinds of character can't be, and text from outside can hold either: what people
send, and what a model writes.

- U+0000. Postgres holds it neither in its text nor in its JSON.
- Half of a surrogate pair: half of a character that takes two, as an emoji cut in two
  leaves it. No UTF-8 holds that, so it can't be stored, hashed, or written into a
  response either.

`storable` puts U+FFFD in the place of each: the mark for a character that can't be
shown. It is what a browser makes of half a pair when it sends text as text. So nothing
is refused for such a character, and nothing fails on it, later and somewhere else.
"""

import re

MARK = chr(0xFFFD)
"""U+FFFD, the replacement character: what stands where a character couldn't be kept."""

_CANT_BE_STORED = re.compile(r"[\x00\ud800-\udfff]")


def storable(text: str) -> str:
    """`text` with U+FFFD in the place of each character that can't be stored: U+0000,
    and each half of a surrogate pair. Everything else is left as it is, other control
    characters too."""
    return _CANT_BE_STORED.sub(MARK, text)
