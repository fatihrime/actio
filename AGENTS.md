# AGENTS.md

Guidance for AI coding agents, and for humans, working in this repository.

## Project

Actio is a self-hostable, local-first AI platform. It combines a chat assistant, an OpenAI/Anthropic-compatible developer gateway, and Claude-inspired usage limits. Design decisions live in [`docs/adr/`](docs/adr/).

## Ground rules

- **Small steps.**
  - Make one focused, tested change at a time. There is no size limit: a step is done when it works end to end, with its tests.
  - Explain what changed, why, and how to verify it.
- **Humans commit.** Agents never commit, push, or create remote resources. The maintainer reviews and commits every change.
- **No secrets in the repo.**
  - Never commit `.env` files, keys or tokens.
  - Tests must never read `.env`; they use fakes and generated test config.
  - Commit hooks scan for secrets (gitleaks) and for private names listed in `~/.config/actio/denylist.txt`, which stays outside the repo.
- **Never bypass the checks.** Don't use `git commit --no-verify`; fix what the hook reports.
- **Written from scratch.**
  - Don't paste code from other projects.
  - Third-party code enters only as declared dependencies with compatible licenses: MIT, BSD, Apache-2.0 and similar permissive ones, or the LGPL for a library used unmodified ([ADR 0008](docs/adr/0008-postgres-through-sqlalchemy-and-psycopg.md)). No GPL or AGPL code.
  - Check a new dependency's license from its own metadata before adding it. If it reaches a container image under anything but a permissive license, list it in `THIRD_PARTY_NOTICES.md`.
- **Fictional data only.** Demo content, such as the "Velora Cycles" knowledge base, is fictional.

## Layout (planned)

| Path | Purpose |
|---|---|
| `apps/api` | FastAPI modular monolith (`python -m actio --role api\|migrate`; `worker` and `all` come with the job queue). `api/` is the HTTP interface; `identity/` holds accounts and sessions; `core/` holds settings, ids, the database and the rate limiter; `migrations/` holds the schema's history |
| `apps/web` | Next.js web app |
| `packages/quota` | Usage-limits engine: Lua scripts plus a Python reference model |
| `packages/llm` | Model clients: the neutral chat format, the Ollama adapter and the OpenAI-compatible adapter (`actio-llm`); the router comes later |
| `packages/fakellm` | Deterministic fake LLM server for tests, scripted by directives in the prompt (`[[think: ...]]`, `[[call: ...]]`, `[[fail: 503]]`) |
| `config/` | `models.yaml`, `plans.yaml`, assistant profiles, demo knowledge base |
| `compose.yaml` | The production-shaped stack behind `make up`: Caddy, web, API, Postgres, Valkey, and the `migrate` job that runs before the API |
| `compose.dev.yaml` | The overlay behind `make dev`: Caddy routes to the native processes; data ports open on 127.0.0.1 |
| `deploy/caddy/` | Caddy config: one entry point routing `/api/*` and `/v1/*` to the API, the rest to the web app |
| `deploy/tests/` | Policy tests for the containers: published ports, pinned images, networks, hardening |
| `docs/` | The documentation site's pages (built by Zensical from `zensical.toml`), including the ADRs in `docs/adr/` |
| `scripts/` | Developer scripts |
| `conftest.py` | Test fixtures shared by every suite: a throwaway Postgres and Valkey, from the images pinned in `compose.yaml` |

## Commands

| Command | What it does |
|---|---|
| `make help` | List available targets |
| `make doctor` | Check that required tools are installed. Prints install hints; installs nothing |
| `make bootstrap` | Set up a fresh clone: `doctor`, `install`, a `.env` with random passwords, then pull the images |
| `make install` | Install the Python and JavaScript workspaces with their dev tools, plus the git hooks |
| `make up` | Build and run the whole stack in containers, shaped like production, at http://localhost:8080 |
| `make dev` | Run the stack at http://localhost:8080 with the API and web app native, with hot reload |
| `make smoke` | Check a running stack through Caddy: the web page, and that the API is healthy, ready (database migrated, Valkey answering) and serves its docs |
| `make migration name="..."` | Write a new database migration from the table models, as a numbered draft to read and edit |
| `make docs` | Preview the documentation site at http://localhost:8001, reloading on edits |
| `make down` | Stop all containers. Their data stays in Docker volumes |
| `make check` | Run everything CI runs: every static check on all files, the tests, then the builds (web app and docs site) |
| `make test-live` | Run the model-client tests against a real Ollama server (0.40 or newer, with `granite4:micro` pulled). Not part of `make check` |
| `make fmt` | Format code and apply safe lint fixes |

`make lint`, `make test` and `make build` run the parts of `make check` on their own. The static checks are defined once, in `.pre-commit-config.yaml`, and run at every commit, in `make check` and in CI. Processes can also run alone: `uv run actio --role api --reload`, `pnpm --dir apps/web dev`, or the fake LLM server with `uv run fakellm`. `uv run actio-llm chat <model> "<prompt>"` talks to a local model through the Ollama adapter. End-to-end tests (`make e2e`) arrive with the chat interface.

`make up` needs only Docker; it builds the images from `apps/api/Dockerfile` and `apps/web/Dockerfile`. `make dev` needs Docker Desktop: its containers reach the natively running API and web app through `host.docker.internal`. Changes to the containers must keep `deploy/tests/` passing; if a rule must change, change the test on purpose and record why in an ADR. `.env` holds the local passwords. It is created once from `.env.example`, git-ignored, and never read by tests. Postgres keeps the password it first started with, so if you recreate `.env`, also wipe the local data with `docker compose down --volumes`.

## Conventions

- **Python:**
  - 3.13, managed with `uv`;
  - add dependencies with `uv add`, never `pip`; run tools through `uv run` or `make`;
  - workspace packages are named `actio-<name>` and imported as `actio_<name>`, such as `actio-fakellm`;
  - formatted and linted with `ruff`;
  - `mypy --strict` on core packages;
  - tests with `pytest`; property and state-machine tests with Hypothesis (`HYPOTHESIS_PROFILE=dev|ci|nightly`);
  - `make test` fails below 90% coverage of `packages/quota`, and needs Docker running: the quota and API tests start throwaway Valkey and Postgres containers;
  - a change to a table model needs a migration in the same commit (`make migration name="..."`): a test fails when the models and the migrations disagree. Never edit a migration that is already on `main`; write the next one;
  - `apps/api/src/actio/core` imports nothing else from `actio`;
  - change the Lua in `packages/quota` only together with the Python reference model (`oracle.py`): the differential test requires both to behave identically;
  - the same holds for the rate limiter's script, `apps/api/src/actio/core/rate_limit.lua`, and the plain token buckets in `apps/api/tests/test_rate_limit.py`;
  - new HTTP client code uses `httpx2`, the maintained continuation of httpx ([ADR 0007](docs/adr/0007-local-models-through-ollamas-native-api.md)). The older test fixtures that call an app in-process still use httpx;
  - the Ollama adapter's tests replay recorded exchanges (`packages/llm/tests/fixtures/`), and fail if the request it sends changes. After changing what it sends, run `ACTIO_RECORD_OLLAMA=1 make test-live` to record them again from a real server, and review the diff.
- **TypeScript:**
  - `pnpm` 12; pnpm also installs the Node.js version pinned in the root `package.json` (26 LTS);
  - add dependencies with `pnpm --dir apps/web add`, never `npm`;
  - strict mode, plus `noUncheckedIndexedAccess`;
  - formatted with Prettier; linted with ESLint and typescript-eslint's strict type-checked rules;
  - stay on TypeScript 6.0 and ESLint 9 until typescript-eslint and eslint-config-next support the next majors ([ADR 0003](docs/adr/0003-web-app-on-nextjs.md));
  - Next.js 16 changed many APIs: read the version-matched docs in `apps/web/node_modules/next/dist/docs/` before writing Next.js code;
  - tests with Vitest, and Playwright for end-to-end tests.
- **Commits:** [Conventional Commits](https://www.conventionalcommits.org/): `feat:`, `fix:`, `docs:`, `test:`, `chore:` and so on.
- **Architecture boundaries,** enforced by import-linter at every commit and in CI. The rules are in `pyproject.toml`, under `[tool.importlinter]`:
  - `packages/*` never import the application, or each other;
  - inside `actio`, imports only point down the layers: `cli`, `app`, `api`, `tables`, `identity`, `core`. So `core` imports nothing else from `actio`;
  - only `actio.api` imports the web framework. `core` and the domain modules, such as `identity`, never do;
  - a new module directly under `actio` must be given a layer in that list, or the check fails. A new domain module goes next to `identity`, and joins the rule against importing the web framework.
- **API conventions:**
  - routes live in `actio.api`; rules and tables live in a domain module below it, which never commits: the route decides when the unit of work is done;
  - errors are RFC 9457 problem documents: raise `Problem(status, detail)`;
  - nothing below `actio.api` reads the clock. Time comes in as `now`, from the `Now` dependency, so tests can move it;
  - a route that needs to know who is asking takes `CurrentPrincipal`;
  - a route that anyone can call, or that checks a password, counts the attempt before doing its work: it takes `Limits` and calls one, such as `await limits.log_in(email)` ([ADR 0011](docs/adr/0011-rate-limits-as-token-buckets-in-valkey.md)). A new limit is a method in `actio.api.rate_limits` and a `limit_*` setting, listed in `compose.yaml` and `.env.example` too, or a test fails;
  - a module that defines tables is named `models` and listed in `actio/tables.py`, or a test fails.
- **Decisions:** anything hard to reverse gets an ADR. Copy [`docs/adr/0000-template.md`](docs/adr/0000-template.md).
