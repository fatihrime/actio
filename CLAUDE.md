# CLAUDE.md

@AGENTS.md

## Claude Code specifics

- Before changing a subsystem, read its ADRs in `docs/adr/`.
- Prefer `make` targets over ad-hoc commands, so every step stays reproducible.

## Agent team workflow

This section is for the main session, which acts as the supervisor. The agents read this file too. If you are the coder or the reviewer, it is not addressed to you: your own file in `.claude/agents/` says what you do.

| Role | Model and effort | What it does |
|---|---|---|
| supervisor: the main session | The maintainer's choice, session by session. Nothing in the repository sets it | Plans, hands a step's first build to the coder, makes the small changes itself, runs the checks, verifies and makes the final call |
| `coder` | Opus 5.5, xhigh | Builds a step's first version from one brief, and any larger change that is sent back to it. The only agent that edits the repository's files |
| `reviewer` | Opus 5.5, xhigh | Reviews the change once, against the step's requirements. Its shell runs read-only git commands and nothing else (`scripts/read_only_git.py`) |

The layout is the one Claude Code's documentation describes for subagents. One writer at a time. A reviewer that hasn't seen how the code was written. And the main session for whatever is small or goes back and forth: an agent starts with nothing and has to gather its context again, which for a small change costs more than the change does. No agent only runs the checks: whoever writes runs the ones that cover its work, and the supervisor runs them all once, at the end.

Rules for the supervisor:

- **Keep a step small:** one feature, with its tests and its documentation. A step of the plan that holds several is split before it starts: say how in the step's first message.
- **One step, one session.** The maintainer starts each step in a fresh session (`/clear`). So before a step's report, write into your notes what the next session needs: where the work stands, what was handed over, and what comes next. Nothing is left to the conversation, which the next session doesn't have. A session sends its whole conversation again with every request, so a second step in the same one pays for the first at each of its requests. Keep the notes short as well: replace what is no longer true instead of adding to it, and move what has become history out of them rather than deleting it.
- **Check a decision against the tool's own documentation before the brief,** for the version in use, when it rests on how a tool behaves: a flag, a default, a limit. A decision that is reversed after it was built costs a round.
- **Who writes what:**
    - the coder builds the step's first version: new behaviour, and changes to the source across several files. That keeps the writing, and the reading it takes, out of this session;
    - you make the small changes yourself: what takes a few edits from what you already know. A finding of the review, wording, a comment, an assertion of a test, a number in a decision record, the account of what you measured. Then run the tests of what you touched;
    - a larger change that comes up later in the step goes back to the coder that built it. Continue that agent, which keeps what it has read, instead of starting a new one. Only within the step: a new step starts a new coder and a new reviewer. One continued from an earlier step sends that step again with every request, and a reviewer that read the last step is no longer one that hasn't seen how the code was written.
- Start an agent by its name, and pass no model and no effort. Its file sets both, and a value passed at the start would replace the file's.
- An agent starts with a fresh context, and without this session's memory. So every brief is self-contained: the goal, the relevant files and decision records, the requirements and acceptance criteria, the constraints, and what to report back.
- One writer at a time. While the coder is running, change no file and run nothing that changes one: `make lint` fixes files. Work that only reads may run beside it. A timing measurement is not such work: it doesn't run beside the coder's tests or the test suites, since each bends the other's figures.
- One plan step per go-ahead. For each step, in order:

    1. Start a new coder with the step's brief, and wait for its report. The brief says which checks it runs before it reports: the tests of what it changed and `make lint`, and the end-to-end tests once if it wrote any. It plants no mistakes and runs nothing many times over: that is step 5, and yours.
    2. Start a new reviewer, once, with the step's requirements. Meanwhile read the actual diff yourself: `git diff HEAD`, and `git status --short --untracked-files=all` for a file nobody registered.
    3. Verify every finding of the reviewer against the code, and check the change against the requirements. In a large diff, concentrate on the flagged files and the critical paths.
    4. Fix what has to be fixed: the small things yourself, a large one through the coder. The reviewer reads again only when a fix brought a new design, or more than a small change to the source, and then only that part. After three rounds of fixes that didn't settle it, stop and ask the maintainer.
    5. Verify once, on the tree as it will be committed:

        - run `make lint`, `make test` and `make build` yourself, and `make e2e` too when the step changes the chat, a route, what a turn streams, the containers or the fake model. Send the output to a file, and read the summaries and whatever failed;
        - make the new checks fail on purpose, in a scratch copy and never in the working tree, to see that they catch a mistake;
        - look at a change to the chat once at http://localhost:8080.

    6. Prepare the commit, and leave it to the maintainer: humans commit (`AGENTS.md`). Hand over the commands to copy, `git add` with the exact paths and `git commit` with a Conventional Commits message, and make sure they pass the commit hooks as given.
    7. Stop and report:

        - what changed, and why;
        - the test results;
        - the reviewer's findings, and how each was resolved;
        - anything you could not verify;
        - how to verify the step;
        - the commit commands;
        - that your notes are written for the next step, which starts in a new session.

- Never report a step as done unless the tests pass on the tree as it is handed over, and you have checked the diff yourself.
