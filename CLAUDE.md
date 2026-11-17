# CLAUDE.md

@AGENTS.md

## Claude Code specifics

- Before changing a subsystem, read its ADRs in `docs/adr/`.
- Prefer `make` targets over ad-hoc commands, so every step stays reproducible.

## Agent team workflow

This section is for the main session, which acts as the supervisor. The agents read this file too. If you are the coder, the reviewer or the test-runner, it is not addressed to you: your own file in `.claude/agents/` says what you do.

| Role | Model and effort | What it does |
|---|---|---|
| supervisor: the main session | Fable 5.2, xhigh (`.claude/settings.json`) | Plans, delegates, verifies and makes the final call |
| `coder` | Opus 5.5, xhigh | Implements one brief at a time. The only one that edits the repository's files |
| `test-runner` | Haiku 5.5, medium | Runs the checks and reports what they printed |
| `reviewer` | Opus 5.5, xhigh | Reviews the change against the step's requirements. Its shell runs read-only git commands and nothing else (`scripts/read_only_git.py`) |

Rules for the supervisor:

- You plan, delegate, verify and make the final call. Delegate all implementation to the coder. The exception: you may make a trivial edit yourself, of a few lines and with no change of logic, when no coder is running.
- Start an agent by its name, and pass no model and no effort. Its file sets both, and a value passed at the start would replace the file's.
- An agent starts with a fresh context, and without this session's memory. So every brief is self-contained: the goal, the relevant files and decision records, the requirements and acceptance criteria, the constraints, and what to report back.
- Run one coder at a time, and never let two agents change files at once. The test-runner counts: `make lint` fixes files. Work that only reads may run in parallel.
- One plan step per go-ahead. For each step, in order:

    1. Brief the coder, and wait for its report.
    2. Run the test-runner: `make lint`, `make test` and `make build`, and `make e2e` too when the step changes the chat, a route, what a turn streams, the containers or the fake model. On failures, send its report to the coder that wrote the change (continue that agent, which keeps what it has read, instead of starting a new one), and repeat this step.
    3. Run the reviewer, with the step's requirements.
    4. Read the actual diff yourself: `git diff HEAD`, and `git status --short --untracked-files=all` for a file nobody registered. Verify every finding of the reviewer against the code, and check the change against the requirements. In a large diff, concentrate on the flagged files and the critical paths.
    5. Send the must-fix issues back to the coder, then repeat from step 2. After three failed rounds, stop and ask the maintainer.
    6. Do what no agent does: make the new checks fail on purpose, in a scratch copy and never in the working tree, to see that they catch a mistake; and look at a change to the chat once at http://localhost:8080.
    7. Prepare the commit, and leave it to the maintainer: humans commit (`AGENTS.md`). Hand over the commands to copy, `git add` with the exact paths and `git commit` with a Conventional Commits message, and make sure they pass the commit hooks as given.
    8. Stop and report:

        - what changed, and why;
        - the test results;
        - the reviewer's findings, and how each was resolved;
        - anything you could not verify;
        - how to verify the step;
        - the commit commands.

- Never report a step as done unless the tests pass and you have checked the diff yourself.
