# How AI is used

Actio is built with the help of an AI coding assistant, [Claude Code](https://claude.com/claude-code). This page explains how the work is shared, and how the assistant's work is checked before it reaches the repository.

## Who does what

- **I decide** what to build, in what order, and when a step is done.
- **The assistant** checks the current state of tools and libraries, proposes designs, and writes the code, tests and documentation for one step at a time. It also runs the checks.
- **I review** every step, run it myself, and make every commit. The assistant never commits, pushes, or creates anything outside my machine.

The rules the assistant works under live in the repository: [`AGENTS.md`](https://github.com/fatihrime/actio/blob/main/AGENTS.md) and [`CLAUDE.md`](https://github.com/fatihrime/actio/blob/main/CLAUDE.md).

## How a step goes

1. **Scope.** We agree on the next step, small enough to review, complete enough to run.
2. **Facts first.** Versions, release notes and licenses come from the package registries and the official documentation, not from memory. The decision records note the date each fact was checked.
3. **Build it with tests.** A step isn't done until it works end to end.
4. **Make the checks fail on purpose.** We break the code deliberately to confirm that the test or linter meant to catch the mistake really catches it.
5. **Run what CI runs.** `make check` runs every static check and test; changes to the stack also get started and smoke-tested.
6. **Review and commit.** I read a summary of what changed and why, try it, and commit it.

## What keeps it honest

- **Strict tools:**
  - mypy in strict mode;
  - typescript-eslint's strict type-checked rules;
  - TypeScript's strict mode, with checked index access;
  - warnings that fail the tests.
- **Independent checks.** The official OpenAI SDK, not just tests written alongside the code, verifies that fakellm speaks the right wire format.
- **Rules as tests.** The containers' security rules are tests that fail when someone weakens them.
- **Written decisions.** Each decision record lists the options weighed and the evidence behind the choice.
- **Secrets and private names.** gitleaks and a private denylist run on every commit, and CI scans the full history.

## Examples from Phase 0

- **A health check that couldn't fail.** `valkey-cli ping` exits successfully even when it can't log in, so the first version of Valkey's health check would have reported a healthy server with the wrong password. Testing the check itself exposed this. It now looks for the actual reply.
- **Leftover processes.** The first process runner for `make dev` crashed while stopping and left its child processes running. A shutdown test caught it, and the runner was replaced.
- **A configuration typo.** An unquoted value made a container's health check invalid YAML. The `docker compose config` check rejected it before anything ran.
- **An outage, worked around.** CI once failed because GitHub couldn't serve a tool's download. That tool now installs from the lockfile instead.
