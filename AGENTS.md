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
  - Third-party code enters only as declared dependencies with compatible licenses (MIT, BSD, Apache-2.0).
- **Fictional data only.** Demo content, such as the "Velora Cycles" knowledge base, is fictional.

## Layout (planned)

| Path | Purpose |
|---|---|
| `apps/api` | FastAPI modular monolith (`python -m actio --role api\|worker\|all`) |
| `apps/web` | Next.js web app |
| `packages/quota` | Usage-limits engine: Lua scripts plus a Python reference model |
| `packages/llm` | Model router and provider adapters |
| `packages/fakellm` | Deterministic fake LLM server for tests |
| `config/` | `models.yaml`, `plans.yaml`, assistant profiles, demo knowledge base |
| `compose.yaml` | The production-shaped stack behind `make up`: Caddy, web, API, Postgres, Valkey |
| `compose.dev.yaml` | The overlay behind `make dev`: Caddy routes to the native processes; data ports open on 127.0.0.1 |
| `deploy/caddy/` | Caddy config: one entry point routing `/api/*` and `/v1/*` to the API, the rest to the web app |
| `deploy/tests/` | Policy tests for the containers: published ports, pinned images, networks, hardening |
| `docs/` | The documentation site's pages (built by Zensical from `zensical.toml`), including the ADRs in `docs/adr/` |
| `scripts/` | Developer scripts |

## Commands

| Command | What it does |
|---|---|
| `make help` | List available targets |
| `make doctor` | Check that required tools are installed. Prints install hints; installs nothing |
| `make bootstrap` | Set up a fresh clone: `doctor`, `install`, a `.env` with random passwords, then pull the images |
| `make install` | Install the Python and JavaScript workspaces with their dev tools, plus the git hooks |
| `make up` | Build and run the whole stack in containers, shaped like production, at http://localhost:8080 |
| `make dev` | Run the stack at http://localhost:8080 with the API and web app native, with hot reload |
| `make smoke` | Check a running stack through Caddy: the web page, the API's health and docs |
| `make docs` | Preview the documentation site at http://localhost:8001, reloading on edits |
| `make down` | Stop all containers. Their data stays in Docker volumes |
| `make check` | Run everything CI runs: every static check on all files, the tests, then the builds (web app and docs site) |
| `make fmt` | Format code and apply safe lint fixes |

`make lint`, `make test` and `make build` run the parts of `make check` on their own. The static checks are defined once, in `.pre-commit-config.yaml`, and run at every commit, in `make check` and in CI. Processes can also run alone: `uv run actio --role api --reload`, `pnpm --dir apps/web dev`, or the fake LLM server with `uv run fakellm`. End-to-end tests (`make e2e`) arrive with the chat interface.

`make up` needs only Docker; it builds the images from `apps/api/Dockerfile` and `apps/web/Dockerfile`. `make dev` needs Docker Desktop: its containers reach the natively running API and web app through `host.docker.internal`. Changes to the containers must keep `deploy/tests/` passing; if a rule must change, change the test on purpose and record why in an ADR. `.env` holds the local passwords. It is created once from `.env.example`, git-ignored, and never read by tests. Postgres keeps the password it first started with, so if you recreate `.env`, also wipe the local data with `docker compose down --volumes`.

## Conventions

- **Python:**
  - 3.13, managed with `uv`;
  - add dependencies with `uv add`, never `pip`; run tools through `uv run` or `make`;
  - workspace packages are named `actio-<name>` and imported as `actio_<name>`, such as `actio-fakellm`;
  - formatted and linted with `ruff`;
  - `mypy --strict` on core packages;
  - tests with `pytest`; property and state-machine tests with Hypothesis (`HYPOTHESIS_PROFILE=dev|ci|nightly`);
  - `make test` fails below 90% coverage of `packages/quota`, and needs Docker running: the quota tests start a Valkey container;
  - change the Lua in `packages/quota` only together with the Python reference model (`oracle.py`): the differential test requires both to behave identically.
- **TypeScript:**
  - `pnpm` 12; pnpm also installs the Node.js version pinned in the root `package.json` (26 LTS);
  - add dependencies with `pnpm --dir apps/web add`, never `npm`;
  - strict mode, plus `noUncheckedIndexedAccess`;
  - formatted with Prettier; linted with ESLint and typescript-eslint's strict type-checked rules;
  - stay on TypeScript 6.0 and ESLint 9 until typescript-eslint and eslint-config-next support the next majors ([ADR 0003](docs/adr/0003-web-app-on-nextjs.md));
  - Next.js 16 changed many APIs: read the version-matched docs in `apps/web/node_modules/next/dist/docs/` before writing Next.js code;
  - tests with Vitest, and Playwright for end-to-end tests.
- **Commits:** [Conventional Commits](https://www.conventionalcommits.org/): `feat:`, `fix:`, `docs:`, `test:`, `chore:` and so on.
- **Architecture boundaries,** to be enforced with import-linter:
  - `core` imports nothing internal;
  - domain modules never import FastAPI;
  - `packages/*` never import from `apps/*`.
- **Decisions:** anything hard to reverse gets an ADR. Copy [`docs/adr/0000-template.md`](docs/adr/0000-template.md).
