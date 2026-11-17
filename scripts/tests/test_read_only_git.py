"""The reviewing agent's shell runs git commands that only read, and nothing else.

`scripts/read_only_git.py` holds it to that: Claude Code runs it before each shell
command of the reviewer's. Here every command goes to it the way Claude Code sends
one, as JSON on standard input, and its exit status is the answer: 0 lets the command
run, 2 refuses it. Any other status means the script failed, which the agent's file
turns into a refusal (`|| exit 2`), so no case may end in one either.

The agent's file is read too, for the hook that runs the script: without it the
reviewer's shell would run anything, and the cases here would cover nothing.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "read_only_git.py"
REVIEWER = Path(__file__).resolve().parents[2] / ".claude" / "agents" / "reviewer.md"

# How the reviewer's file has Claude Code run the script before each shell command.
HOOKED = """\
hooks:
  PreToolUse:
    - matcher: "Bash"
      hooks:
        - type: command
          command: 'python3 "${CLAUDE_PROJECT_DIR}/scripts/read_only_git.py" || exit 2'
"""

MAY_RUN = [
    "git diff HEAD",
    "git diff HEAD --stat -- apps/api scripts",
    "git --no-pager log --oneline -8",
    "git log -3 --format='%h %s (%an)'",
    "git show HEAD:AGENTS.md",
    "git status --short",
    "git status --short --untracked-files=all",
    "git ls-files --others --exclude-standard",
    "git blame -L 10,20 Makefile",
    # What the shell leaves as it is in a revision.
    "git diff HEAD~2",
    "git log -1 HEAD^",
    # What would join two commands is plain text between quotes, and after a backslash.
    "git grep -n -E 'stop|halt' -- apps/web",
    'git grep -n "resync({ stop: true })"',
    "git grep -n 'a; b && c > d'",
    "git grep -n -F '$(' -- scripts",
    "git grep -n stop\\|halt",
    # What the shell would expand is plain text between single quotes.
    "git grep -n -E 'stop$' -- apps/web",
    "git diff HEAD -- '*.py'",
    "git show 'HEAD@{1}'",
    # Options that only look like the ones that write, and words that name no option.
    "git diff --output-indicator-new=+ HEAD",
    "git log -p --no-ext-diff -1",
    "git grep -n -F '=='",
    "git grep -n -e ''",
    # A search for an option that writes, written so that it isn't one.
    "git grep -n -e '[-]-output'",
]

REFUSED = [
    # Not git, or git asked to change something.
    "",
    "ls",
    "cat AGENTS.md",
    "make test",
    "rm -rf apps",
    "git",
    "git commit -m review",
    "git add -A",
    "git checkout -- .",
    "git restore AGENTS.md",
    "git stash",
    "git reset --hard",
    "git push",
    "git apply fix.patch",
    "git config core.pager sh",
    # git told to behave otherwise before the command is named.
    "git -c core.pager=sh log",
    "git -C /tmp status",
    "GIT_EXTERNAL_DIFF=sh git diff",
    "env git diff",
    "git --config-env=diff.external=SHELL diff",
    "git -p log",
    "git --paginate log",
    "git --exec-path=/tmp diff",
    "git --git-dir=/tmp/x status",
    # A second command, or output sent somewhere.
    "git status; touch x",
    "git status && touch x",
    "git status || touch x",
    "git log &",
    "git diff | tee out.patch",
    "git diff > out.patch",
    "git diff >> out.patch",
    "git status\ntouch x",
    "cd apps && git status",
    "(git status)",
    # Control characters, a tab too: shlex reads some of them otherwise than the shell.
    "git diff HEAD\r--stat",
    "git diff HEAD\x00--stat",
    "git diff HEAD\t--stat",
    "git diff HEAD\x7f--stat",
    # Each of those characters alone, apart from the words around it.
    "git status ; touch x",
    "git log --stdin < revs.txt",
    "git grep -n -F (",
    "git grep -n -F )",
    # A command inside the command.
    "git log $(touch x)",
    'git log "$(touch x)"',
    "git log `touch x`",
    'git log "`touch x`"',
    "git diff <(touch x)",
    # What the shell expands before git sees the words, into an option nobody checked.
    "git diff ${u:---output=AGENTS.md}",
    "git diff $'--output=AGENTS.md'",
    "git diff --{output=AGENTS.md,stat}",
    "git grep -l $'-Osh' -e x",
    # In $'...', \' doesn't end the quote: the rest is a second command.
    "git log $'\\'' ; touch x ; echo \\'",
    # A $ wherever the shell expands it, and each brace and wildcard alone.
    "git diff $u",
    'git grep -n -E "stop$"',
    "git grep -n -F {",
    "git grep -n -F }",
    "git diff HEAD -- *.py",
    "git diff HEAD -- ?.py",
    "git diff HEAD -- [ab].py",
    # Options that write a file or start a program, whole or cut short.
    "git diff --output=out.patch",
    "git diff --output out.patch",
    "git log -p --outp=out.patch",
    "git diff --ext-diff",
    "git grep --open-files-in-pager=sh stop",
    "git grep -O sh stop",
    "git grep -nOsh stop",
    # The same after a --, which an option before it may have taken for its value.
    "git grep -l -e -- --open-files-in-pager='echo RAN:'",
    "git grep -l -e -- -Osh",
    "git diff --no-index -S -- --output=out.txt a b",
    "git blame -L -- --output=out.txt Makefile",
    "git grep -n -F -- --output",
    # Quotes that don't close.
    "git log 'unclosed",
]


def ask(stdin: str) -> subprocess.CompletedProcess[str]:
    """What the script answers to this standard input."""
    # This interpreter, running the script under test.
    return subprocess.run(  # noqa: S603
        [sys.executable, str(HOOK)],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def call(command: object) -> str:
    """A shell call as Claude Code describes it to a hook."""
    return json.dumps(
        {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command}}
    )


@pytest.mark.parametrize("command", MAY_RUN)
def test_a_git_command_that_only_reads_may_run(command: str) -> None:
    answer = ask(call(command))

    assert (answer.returncode, answer.stderr) == (0, "")


@pytest.mark.parametrize("command", REFUSED)
def test_anything_else_is_refused_and_told_what_may_run(command: str) -> None:
    answer = ask(call(command))

    assert answer.returncode == 2
    assert "git grep" in answer.stderr


@pytest.mark.parametrize("command", ["git grep -n -F -- --output", "git grep -n -e -Osh"])
def test_a_search_for_an_option_is_told_how_to_write_it(command: str) -> None:
    answer = ask(call(command))

    assert answer.returncode == 2
    assert "'[-]-output'" in answer.stderr


def test_the_reviewer_runs_each_shell_command_past_the_script_first() -> None:
    """And when the script can't start or crashes, `|| exit 2` refuses the command."""
    text = REVIEWER.read_text(encoding="utf-8")
    frontmatter = text.split("---\n")[1]

    # The frontmatter opens the file and the block begins a line: below other text, or
    # commented out, it would still be there and do nothing.
    assert text.startswith("---\n")
    assert "\n" + HOOKED in "\n" + frontmatter


@pytest.mark.parametrize(
    "stdin",
    [
        "",
        "not json",
        "[]",
        '"git status"',
        "{}",
        '{"tool_input": {}}',
        call(7),
        call(None),
        call(["git", "status"]),
    ],
)
def test_a_call_that_cannot_be_read_is_refused(stdin: str) -> None:
    assert ask(stdin).returncode == 2
