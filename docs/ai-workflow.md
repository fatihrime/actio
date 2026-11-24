# How AI is used

Actio is built with the help of an AI coding assistant, [Claude Code](https://claude.com/claude-code). This page explains how the work is shared, and how the assistant's work is checked before it reaches the repository.

## Who does what

- **I decide** what to build, in what order, and when a step is done.
- **The assistant** checks the current state of tools and libraries, proposes designs, and writes the code, tests and documentation for one step at a time. It also runs the checks.
- **I review** every step, run it myself, and make every commit. The assistant never commits, pushes, or creates anything outside my machine.

Since the middle of Phase 1 the assistant's part is shared between sessions of it, three today. One plans the step, runs every check and verifies the result. A second writes the step's first version from a written brief. A third reviews that change without having seen how it was written, and can do nothing but read.

The rules the assistant works under live in the repository: [`AGENTS.md`](https://github.com/fatihrime/actio/blob/main/AGENTS.md) and [`CLAUDE.md`](https://github.com/fatihrime/actio/blob/main/CLAUDE.md), which also says how the three work together.

## How a step goes

1. **Scope.** We agree on the next step, small enough to review, complete enough to run.
2. **Facts first.** Versions, release notes and licenses come from the package registries and the official documentation, not from memory. The decision records note the date each fact was checked.
3. **Build it with tests.** A step isn't done until it works end to end.
4. **Have it read by a session that didn't write it.** The reviewer reads the change against the step's requirements. Each of its findings is checked against the code before anything is changed for it.
5. **Make the checks fail on purpose.** We break the code deliberately, in a copy of the tree, to confirm that the test or linter meant to catch the mistake really catches it.
6. **Run what CI runs.** `make check` runs every static check and test; changes to the stack also get started and smoke-tested. A change to the chat also runs the end-to-end tests, in a real browser, and is looked at once in the running app.
7. **Review and commit.** I read a summary of what changed and why, try it, and commit it.

## What keeps it honest

- **Strict tools:**
    - mypy in strict mode;
    - typescript-eslint's strict type-checked rules;
    - TypeScript's strict mode, with checked index access;
    - warnings that fail the tests.
- **Independent checks.** The official OpenAI SDK, not just tests written alongside the code, verifies that fakellm speaks the right wire format.
- **Rules as tests.** The containers' security rules are tests that fail when someone weakens them.
- **Written decisions.** Each decision record lists the options weighed and the evidence behind the choice.
- **Measured speed.** A phase ends with a [performance run](performance.md) in three browser engines, against numbers the project holds itself to. Phase 1 was the first that did. That page says what a run found, what was fixed, and what is still open.
- **Secrets and private names.** gitleaks and a private denylist run on every commit, and CI scans the full history.

## Examples from Phase 0

- **A health check that couldn't fail.** `valkey-cli ping` exits successfully even when it can't log in, so the first version of Valkey's health check would have reported a healthy server with the wrong password. Testing the check itself exposed this. It now looks for the actual reply.
- **Leftover processes.** The first process runner for `make dev` crashed while stopping and left its child processes running. A shutdown test caught it, and the runner was replaced.
- **A configuration typo.** An unquoted value made a container's health check invalid YAML. The `docker compose config` check rejected it before anything ran.
- **An outage, worked around.** CI once failed because GitHub couldn't serve a tool's download. That tool now installs from the lockfile instead.

## Examples from Phase 1

- **A prompt cut without a word.** Measuring Ollama before building on it showed that one of its two engines cuts a prompt that doesn't fit the model's context window, system prompt included, and still answers. The adapter makes it refuse such a prompt. With its two strict settings taken out, the tests against a real server fail ([ADR 0007](adr/0007-local-models-through-ollamas-native-api.md)).
- **A guess that came out at under half.** Guessing a prompt's tokens from its bytes was measured against the model server's own count: for a page of numbers it came out at under half, because the model reads every digit as a token. So the guess counts digits and punctuation one by one ([ADR 0020](adr/0020-usage-limits-on-turns.md)).
- **Tests that passed with the mistake in.** Eleven mistakes were planted in the rules for what an answer may do on the page. Eight failed a test at once. The other three failed none until their tests were made sharper ([ADR 0016](adr/0016-answers-shown-as-markdown.md)).
- **A freeze ten times as long in one browser engine.** The answers had first been measured in Chromium alone. The performance runs were the first in three engines: in Safari's, the first block of TypeScript in an arriving answer gave frames of 0.8 and 0.6 s. Colouring such a block alone on a page stopped that page for 1.5 s there, and for 0.12 s in Chromium. Code is now coloured in a worker ([what the runs have found](performance.md#what-the-runs-have-found)).
- **A finding that was the measurement's own.** A run reported the list of conversations as a quarter slower to put away than before. It was the test's own driver, not the page, and the page on performance [says so](performance.md#what-a-run-got-wrong).
