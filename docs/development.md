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

`make check` runs what CI's main job runs: every static check on every file, then the tests, then the builds. The end-to-end tests are apart, because they build and start the whole stack: `make e2e`. `make install` sets up the same static checks to run on the files of each commit:

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
- inside the application, imports only point down the layers: `cli`, `app`, `api`, `tables`, `chat`, then `catalog` and `identity` side by side, `core`. `chat` is above `identity` because a conversation belongs to an account; `catalog` and `identity` don't import each other;
- only `api`, the HTTP interface, imports the web framework. `core` and the domain modules never do, because processes that serve no HTTP use them too;
- every module directly under `actio` must be given a layer. A new module fails the check until someone decides where it belongs.

A broken rule names the file, the line and the import, and says what to do instead.

## Tests

| Suite | Runner | What it covers |
|---|---|---|
| `apps/api/tests` | pytest, Hypothesis, Postgres, Valkey | Settings, the CLI, database migrations (also run by several processes at once), the health and readiness probes, sessions and accounts, the rate limiter's Lua script against a plain token bucket, the limits over HTTP, conversations and the queries on their message tree against a plain tree, the models file and what each kind of model server says about a model, turns against fakellm and against a model a test can hold half-way, and over a real connection for readers who leave before the end |
| `packages/fakellm/tests` | pytest | The wire format, checked with OpenAI's official Python SDK too; the directives that script reasoning, tool calls and failures |
| `packages/llm/tests` | pytest | The Ollama adapter, against exchanges recorded from a real server; the OpenAI-compatible adapter, against fakellm. Streaming, tool calls, refused prompts, broken connections, timeouts |
| `packages/quota/tests` | pytest, Hypothesis, Valkey | Credit math, reset windows across DST, the usage-limits rules under random event sequences, the Lua scripts against the Python reference model, and concurrency |
| `deploy/tests` | pytest | The containers' security rules |
| `apps/web/e2e` | Playwright, the whole stack in containers | What only a real browser and the pieces together show: an answer read while it is written, through the proxy; a reload; Stop; a failed and a broken-off answer; markdown, formulas and a drawn diagram; that an answer fetches nothing and a link asks first; the page following a long answer. Run by `make e2e`, not by `make test` |
| `apps/web` | Vitest, Testing Library, jsdom | The chat, driven as a person would in a simulated browser: asking, stopping, a failed or refused answer, reasoning, a stored conversation, Back and Forward. The streams the API was recorded to produce, read by the real `ai` package. The generated API types, against the API's recorded description |

`make test` runs them all. Tests never read `.env`: they build their own settings.

- **Coverage gate:** `make test` fails if the quota engine's test coverage drops below 90%.
- **Docker:** the quota engine's tests need Valkey, and the API's tests need Postgres and Valkey. They start throwaway containers, from the images pinned in `compose.yaml`, once per run. Each API test gets its own empty database in that Postgres, and its own key prefix in that Valkey, so what one test counts against a rate limit the next doesn't inherit.
  - Without Docker, `uv run pytest` skips those tests; `make test`, and so CI, sets `ACTIO_REQUIRE_DOCKER=1`, so there they fail with the reason instead.
  - Set `ACTIO_TEST_VALKEY_URL` or `ACTIO_TEST_POSTGRES_URL` to use a server you already run.
- **One format, tested from both sides:** the API streams answers in the format of a JavaScript library, the AI SDK. The API's streams are recorded in `apps/api/tests/fixtures/turns/`: a Python test fails when the API stops producing exactly those files, and a test in `apps/web` reads the same files with the library itself. After changing the stream on purpose, record them again with `ACTIO_RECORD_TURNS=1 uv run pytest apps/api/tests/test_turn_fixtures.py`, review the difference, and run the web app's tests. See [ADR 0014](adr/0014-turns-streamed-in-the-ai-sdks-format.md).
- **One description of the API, checked from both sides:** the API's description of itself (OpenAPI) is recorded in `apps/api/openapi.json`, and the web app's types in `apps/web/lib/api/schema.d.ts` are generated from it. A Python test fails when the API no longer matches the file, and a test in `apps/web` fails when the types no longer match it. After changing a route or a schema on purpose, run `make openapi`, which writes both again, and review the difference. The TypeScript compiler then points at every call in the web app that no longer fits. See [ADR 0015](adr/0015-the-web-chat-calls-the-api-from-the-browser.md).
- **The chat's tests never reach a network.** They render the real components and replace `fetch` with a fake API (`apps/web/tests/fake-api.ts`). It answers with the recorded streams above, and with bodies typed by the generated types, so a test can't run on an answer the API wouldn't give. A request that a test set up no answer for fails that test.
- **End to end, in a real browser:** `make e2e` builds the images and starts a stack of its own, as `make up` would, with [fakellm](reference/fakellm.md) as one more container in Ollama's place. Playwright then drives it in a headless Chromium (`apps/web/e2e`), and the stack is removed again. See [ADR 0017](adr/0017-end-to-end-tests-on-the-real-stack.md).
    - It runs beside your own stack, on port 8090 with its own data, and reads no `.env`.
    - A test scripts the model by typing: `[[pace: 0.1]]` makes an answer take its time, `[[fail: 503]]` makes it fail.
    - Every test also fails if the page asks anything of another site, or if a script on it breaks.
    - It needs Docker and about a minute, so it isn't part of `make check`. The first run downloads the browser, about 90 MB.
    - To work on a test: `ACTIO_E2E_KEEP=1 make e2e` leaves the stack running, and `pnpm --dir apps/web exec playwright test` runs the tests against it again. `make e2e args="--headed --workers 1"` shows the browser, one test at a time; the first such run downloads the whole of Chromium, about 200 MB more. A failed test leaves a trace in `apps/web/test-results/`, to open with `pnpm --dir apps/web exec playwright show-trace`.
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

Every push and pull request runs four jobs:

- **Lint, types, tests and build:** `make check`.
- **Quickstart on Linux:** `make up` and `make smoke` on a clean runner with nothing installed beyond Docker.
- **End-to-end tests in a browser:** `make e2e`. A failed run keeps its report, with a trace of each failed test, for a week.
- **Secret scan:** gitleaks over the full history.

Each push to `main` also publishes this site to GitHub Pages.

## Conventions

- **Commits** follow [Conventional Commits](https://www.conventionalcommits.org/): `feat:`, `fix:`, `docs:` and so on.
- **Decisions** that would be hard to reverse get an [ADR](adr/index.md).
- **AI coding agents** follow [`AGENTS.md`](https://github.com/fatihrime/actio/blob/main/AGENTS.md). Among other rules, they never commit or push. See [How AI is used](ai-workflow.md).

## This site

`make docs` serves it at <http://localhost:8001> and reloads pages as you edit them. Pages are Markdown files in `docs/`, and the navigation lives in `zensical.toml`. The build is strict: a broken link or a missing page fails `make build`, and so CI.
