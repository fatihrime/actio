# Development

## Toolchains

- **Python 3.13 with uv.** One workspace (`apps/api`, `packages/fakellm`, `packages/llm` and `packages/quota`), one lockfile, and uv's own CPython build. See [ADR 0002](adr/0002-python-workspace-with-uv.md).
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
| Python | Ruff (lint and format), mypy in strict mode, import rules (import-linter) |
| Web app | Prettier, ESLint with typescript-eslint's strict type-checked rules, `tsc` |
| Shell scripts | shellcheck |
| Docker Compose | `docker compose config` |
| GitHub Actions | actionlint, zizmor |
| Secrets | gitleaks, plus a private denylist of names that must never appear |

`make fmt` applies formatting and safe fixes. Don't bypass a failing check with `git commit --no-verify`; fix what it reports.

### Import rules

Actio is one application split into modules, not into services ([ADR 0001](adr/0001-one-app-several-process-roles.md)), so a tool keeps the modules apart. The rules live in `pyproject.toml`, under `[tool.importlinter]`, and `uv run lint-imports` checks them:

- the packages under `packages/` never import the application, or each other;
- inside the application, imports only point down the layers: `cli`, `app`, `api`, `tables`, `chat`, `identity`, `core`. `chat` is above `identity` because a conversation belongs to an account;
- only `api`, the HTTP interface, imports the web framework. `core` and the domain modules never do, because processes that serve no HTTP use them too;
- every module directly under `actio` must be given a layer. A new module fails the check until someone decides where it belongs.

A broken rule names the file, the line and the import, and says what to do instead.

## Tests

| Suite | Runner | What it covers |
|---|---|---|
| `apps/api/tests` | pytest, Hypothesis, Postgres, Valkey | Settings, the CLI, database migrations (also run by several processes at once), the health and readiness probes, sessions and accounts, the rate limiter's Lua script against a plain token bucket, the limits over HTTP, conversations and the queries on their message tree against a plain tree |
| `packages/fakellm/tests` | pytest | The wire format, checked with OpenAI's official Python SDK too; the directives that script reasoning, tool calls and failures |
| `packages/llm/tests` | pytest | The Ollama adapter, against exchanges recorded from a real server; the OpenAI-compatible adapter, against fakellm. Streaming, tool calls, refused prompts, broken connections, timeouts |
| `packages/quota/tests` | pytest, Hypothesis, Valkey | Credit math, reset windows across DST, the usage-limits rules under random event sequences, the Lua scripts against the Python reference model, and concurrency |
| `deploy/tests` | pytest | The containers' security rules |
| `apps/web` | Vitest | The page renders |

`make test` runs them all. Tests never read `.env`: they build their own settings.

- **Coverage gate:** `make test` fails if the quota engine's test coverage drops below 90%.
- **Docker:** the quota engine's tests need Valkey, and the API's tests need Postgres and Valkey. They start throwaway containers, from the images pinned in `compose.yaml`, once per run. Each API test gets its own empty database in that Postgres, and its own key prefix in that Valkey, so what one test counts against a rate limit the next doesn't inherit.
  - Without Docker, `uv run pytest` skips those tests; `make test`, and so CI, sets `ACTIO_REQUIRE_DOCKER=1`, so there they fail with the reason instead.
  - Set `ACTIO_TEST_VALKEY_URL` or `ACTIO_TEST_POSTGRES_URL` to use a server you already run.
- **Real models:** `make test-live` runs the model-client tests against a real Ollama server instead of the recordings. It needs Ollama 0.40 or newer with `granite4:micro` pulled, so it isn't part of `make check`. See the [model clients reference](reference/llm.md#tests).
- **Nightly:** a scheduled workflow runs the quota tests with 5,000 random examples each.
- **Random tests:** Hypothesis tries 100 random examples per property on a laptop and 200 in CI. Set `HYPOTHESIS_PROFILE=nightly` for 5,000. When a test fails, Hypothesis shrinks it to a minimal sequence of steps and prints it.

## Database migrations

The schema's history is in `apps/api/src/actio/migrations/versions/`, one numbered file per change ([ADR 0008](adr/0008-postgres-through-sqlalchemy-and-psycopg.md)).

1. Change or add a table model.
2. Run `make migration name="what it does"`. It compares the models with a migrated database and writes the difference as the next numbered file.
3. Read that file: it is a draft. Check both `upgrade` and `downgrade`.
4. Commit the model and the migration together.

- `make dev` and `make up` apply pending migrations before the API starts. `uv run actio --role migrate` does it by hand.
- A test fails when the models and the migrations disagree, and another undoes every migration.
- Never edit a migration that is already on `main`: databases have applied it. Write the next one.

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
