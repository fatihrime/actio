# Development

## Toolchains

- **Python 3.13 with uv.** One workspace (`apps/api` and `packages/fakellm`), one lockfile, and uv's own CPython build. See [ADR 0002](adr/0002-python-workspace-with-uv.md).
- **TypeScript with pnpm 12.**
  - pnpm also installs the pinned Node.js, 26 LTS.
  - TypeScript stays on 6.0 and ESLint on 9 until the linters support the next major versions.
  - See [ADR 0003](adr/0003-web-app-on-nextjs.md).
- **Docker Compose** for the local stack and the images. See [ADR 0004](adr/0004-local-development-stack.md) and [ADR 0005](adr/0005-container-images.md).

Add dependencies with `uv add` or `pnpm --dir apps/web add`, never with pip or npm.

## Checks

`make check` runs everything CI runs: every static check on every file, then the tests, then the builds. `make install` sets up the same static checks to run on the files of each commit:

| Area | Checks |
|---|---|
| Every file | Whitespace, line endings, merge markers, file size, YAML and TOML syntax |
| Python | Ruff (lint and format), mypy in strict mode |
| Web app | Prettier, ESLint with typescript-eslint's strict type-checked rules, `tsc` |
| Shell scripts | shellcheck |
| Docker Compose | `docker compose config` |
| GitHub Actions | actionlint, zizmor |
| Secrets | gitleaks, plus a private denylist of names that must never appear |

`make fmt` applies formatting and safe fixes. Don't bypass a failing check with `git commit --no-verify`; fix what it reports.

## Tests

| Suite | Runner | What it covers |
|---|---|---|
| `apps/api/tests` | pytest | Settings, the CLI, the health check |
| `packages/fakellm/tests` | pytest | The wire format, checked with OpenAI's official Python SDK too |
| `packages/quota/tests` | pytest, Hypothesis, Valkey | Credit math, reset windows across DST, the usage-limits rules under random event sequences, the Lua scripts against the Python reference model, and concurrency |
| `deploy/tests` | pytest | The containers' security rules |
| `apps/web` | Vitest | The page renders |

`make test` runs them all. Tests never read `.env`: they build their own settings.

- **Coverage gate:** `make test` fails if the quota engine's test coverage drops below 90%.
- **Docker:** the quota engine's Valkey tests start a throwaway Valkey container, from the image pinned in `compose.yaml`. Without Docker, `uv run pytest` skips them; `make test`, and so CI, sets `ACTIO_REQUIRE_DOCKER=1`, so there they fail with the reason instead. Set `ACTIO_TEST_VALKEY_URL` to use a server you already run.
- **Nightly:** a scheduled workflow runs the quota tests with 5,000 random examples each.
- **Random tests:** Hypothesis tries 100 random examples per property on a laptop and 200 in CI. Set `HYPOTHESIS_PROFILE=nightly` for 5,000. When a test fails, Hypothesis shrinks it to a minimal sequence of steps and prints it.

## CI

Every push and pull request runs three jobs:

- **Lint, types, tests and build:** `make check`.
- **Quickstart on Linux:** `make up` and `make smoke` on a clean runner with nothing installed beyond Docker.
- **Secret scan:** gitleaks over the full history.

Each push to `main` also publishes this site to GitHub Pages.

## Conventions

- **Commits** follow [Conventional Commits](https://www.conventionalcommits.org/): `feat:`, `fix:`, `docs:` and so on.
- **Decisions** that would be hard to reverse get an [ADR](adr/index.md).
- **AI coding agents** follow [`AGENTS.md`](https://github.com/fatihrime/actio/blob/main/AGENTS.md). Among other rules, they never commit or push. See [How AI is used](ai-workflow.md).

## This site

`make docs` serves it at <http://localhost:8001> and reloads pages as you edit them. Pages are Markdown files in `docs/`, and the navigation lives in `zensical.toml`. The build is strict: a broken link or a missing page fails `make build`, and so CI.
