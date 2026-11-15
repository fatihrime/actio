"""Fails when a page of the documentation site holds a list the site would show wrongly.

The site is built with Python-Markdown, which reads lists more strictly than GitHub
does. Written in either of these ways, a list looks right on GitHub and wrong on the
site:

- right under a line of text, it is no list at all: its items run on as text, markers
  and all;
- indented by two or three spaces, a sub-item is not nested: it joins the list above
  it. The same goes for a table, a quote or a paragraph that belongs to an item.

So in every page under docs/, and in every file a page includes, outside code blocks:
a list item comes after an empty line or another item, and indentation is in fours.

Usage: check_docs_lists.py, from anywhere. pre-commit runs it when Markdown changes.
"""

import re
import sys
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# How a page takes in another file: a line of the snippets extension, in zensical.toml.
INCLUDE = re.compile(r'^--8<-- "(?P<path>[^"]+)"', re.MULTILINE)
ITEM = re.compile(r" *(?:[-*+]|\d+\.) ")
FENCE = re.compile(r" *(?P<marks>`{3,}|~{3,})")

NO_GAP = "a list item needs an empty line, or another item, above it"
INDENT = "indent by four spaces a level"
WHY = (
    "The documentation site reads lists more strictly than GitHub does: "
    'see "This site" in docs/development.md.'
)


def pages() -> list[Path]:
    """Every page of the site, then every file one of them includes."""
    own = sorted((ROOT / "docs").rglob("*.md"))
    included = {
        ROOT / match["path"] for page in own for match in INCLUDE.finditer(page.read_text())
    }
    return [*own, *sorted(included.difference(own))]


def faults(text: str) -> Iterator[tuple[int, str]]:
    """The lines the site would read otherwise than GitHub does, by number, and why."""
    opened = ""  # the marks that opened the code block this line is in
    above = ""
    for number, line in enumerate(text.splitlines(), start=1):
        fence = FENCE.match(line)
        closes = bool(
            opened
            and fence
            and fence["marks"].startswith(opened)
            and line.strip() == fence["marks"]
        )
        if not opened or closes:
            if ITEM.match(line) and above.strip() and not ITEM.match(above):
                yield number, NO_GAP
            if line.strip() and (len(line) - len(line.lstrip(" "))) % 4:
                yield number, INDENT
        if closes:
            opened = ""
        elif fence and not opened:
            opened = fence["marks"]
        above = line


def main() -> None:
    found = [
        f"{page.relative_to(ROOT)}:{number}: {fault}"
        for page in pages()
        for number, fault in faults(page.read_text())
    ]
    if found:
        sys.exit("\n".join([*found, WHY]))


if __name__ == "__main__":
    main()
