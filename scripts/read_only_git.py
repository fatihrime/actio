"""Keeps the reviewing agent's shell to git commands that only read.

The reviewer (`.claude/agents/reviewer.md`) reads a change and reports on it; it
changes nothing. Its tools leave out Write and Edit, but a shell can write too, and an
agent's list of tools can't narrow Bash to some commands. So that agent's file has
Claude Code run this script before each shell command of the reviewer's (a PreToolUse
hook), and whatever is not one read-only git command is refused.

Claude Code passes the call as JSON on standard input, with the command line under
`tool_input.command`. Exit status 0 lets it run. Status 2 refuses it, and what was
written to standard error is shown to the agent. Any other status counts as this
script having failed, and then the command runs: so a call that can't be read is
refused too, with status 2. The agent's file runs this script followed by `|| exit 2`,
so that a script that can't start, or that crashes, refuses the command as well.

The check is on the words git will be given, so the shell must hand them on as
written: whatever it would expand first is refused, and a pattern or a path that needs
such a character goes in single quotes. Every word after the command's name is
checked, after a `--` too: git gives an option that takes a value the next word,
whatever it is, and then reads on for options.

This stops a reviewer that reaches for a tool it shouldn't have. It is not a sandbox:
a git command that reads can still be made to do more by the repository's own
configuration, which is the maintainer's. And it assumes the shell's own defaults:
zsh's EXTENDED_GLOB option would make a bare `^`, `#` and `~` wildcards, and a global
alias (`alias -g`) is expanded anywhere on the line.

It runs on whatever `python3` is on the PATH, from 3.9 on, with the standard library.

Usage: read_only_git.py < call.json
"""

from __future__ import annotations

import json
import shlex
import sys

# What the reviewer may run. Each reads the repository and leaves it as it was.
READS = ("blame", "diff", "grep", "log", "ls-files", "show", "status")
# Options of theirs that write a file or start another program.
WRITES_OR_RUNS = ("--output", "--ext-diff", "--open-files-in-pager")
# Outside quotes, these join commands, send output somewhere or start a subshell.
JOINS = ";|&<>()"
# Outside quotes, the shell expands these before git sees the words: braces into
# several words, and wildcards into file names, which may begin with --output=.
EXPANDS = "{}*?["
# These are expanded between double quotes too, into a variable's value or a command's
# output. And after a $ the shell reads quotes otherwise than shell_text: in $'...',
# \' doesn't end the quote.
EXPANDS_IN_DOUBLE_QUOTES = "$`"
# What to do instead, for a pattern that would be refused as one of those options.
SEARCH_FOR_IT = (
    "To search for such a word, write a pattern that doesn't begin with it, such as"
    " '[-]-output', and leave out -F, which would search for the brackets themselves"
)

RULE = (
    "The reviewer's shell runs one git command that only reads, and nothing else: "
    + ", ".join(f"git {name}" for name in READS)
    + ". Read a file with the Read tool, and search with git grep."
)


def shell_text(command: str) -> tuple[str, str]:
    """What the shell reads outside any quotes, and what it reads between double quotes."""
    bare: list[str] = []
    double: list[str] = []
    quote = ""
    chars = iter(command)
    for char in chars:
        if quote == "'":
            if char == "'":
                quote = ""
        elif char == "\\":
            next(chars, "")  # the character after a backslash stands for itself
        elif quote:
            if char == '"':
                quote = ""
            else:
                double.append(char)
        elif char in ("'", '"'):
            quote = char
        else:
            bare.append(char)
    return "".join(bare), "".join(double)


def refusal(command: str) -> str:
    """Why this command line may not run. Empty when it may."""
    # A newline starts a second command. And shlex splits a word at a carriage return,
    # and keeps a NUL in one, where the shell doesn't: the words checked would not be
    # the words git gets. So no control character at all, a tab included.
    if any(char < " " or char == "\x7f" for char in command):
        return "one command, on one line, with no tab or other control character"
    bare, double = shell_text(command)
    if any(char in EXPANDS_IN_DOUBLE_QUOTES for char in bare + double) or any(
        char in EXPANDS for char in bare
    ):
        return (
            "the shell would expand a $, a backtick, a brace or a wildcard (* ? [) before"
            " git saw it. Put a pattern or a path that needs one in single quotes"
        )
    if any(char in JOINS for char in bare):
        return "no pipe, no redirection and no second command"
    try:
        words = shlex.split(command)
    except ValueError:
        return "its quotes don't close"
    if words[:1] != ["git"]:
        return "it is not a git command"
    args = words[2:] if words[1:2] == ["--no-pager"] else words[1:]
    if not args or args[0] not in READS:
        return "right after `git` comes one of those commands (only --no-pager may stand between)"
    # Every word, after a `--` too: an option that takes a value may have taken the `--`.
    for word in args[1:]:
        name = word.split("=", 1)[0]
        # git takes the beginning of a long option for the whole of it.
        if len(name) > 2 and any(option.startswith(name) for option in WRITES_OR_RUNS):
            return f"{name} writes a file or starts a program. {SEARCH_FOR_IT}"
        if args[0] == "grep" and word.startswith("-") and not word.startswith("--") and "O" in word:
            return f"-O opens the files it finds in a program. {SEARCH_FOR_IT}"
    return ""


def main() -> int:
    try:
        command = json.load(sys.stdin)["tool_input"]["command"]
    except (ValueError, LookupError, TypeError):
        command = None
    reason = refusal(command) if isinstance(command, str) else "the call holds no command line"
    if not reason:
        return 0
    sys.stderr.write(f"Refused: {reason}. {RULE}\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
