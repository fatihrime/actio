# AGENTS.md

Guidance for AI coding agents, and for humans, working in this repository.

## Project

Actio is a self-hostable, local-first AI platform. It combines a chat assistant, an OpenAI/Anthropic-compatible developer gateway, and Claude-inspired usage limits. Design decisions live in [`docs/adr/`](docs/adr/).

## Ground rules

- **Small steps.**
  - Make one focused, tested change at a time, ideally under ~400 changed lines.
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
| `docs/adr/` | Architecture decision records |
| `scripts/` | Developer scripts |

## Commands

| Command | What it does |
|---|---|
| `make help` | List available targets |
| `make doctor` | Check that required tools are installed. Prints install hints; installs nothing |
| `make install` | Install the Python and JavaScript workspaces with their dev tools, plus the git hooks |
| `make check` | Run everything CI runs: every static check on all files, the tests, then the web build |
| `make fmt` | Format code and apply safe lint fixes |

`make lint`, `make test` and `make build` run the parts of `make check` on their own. The static checks are defined once, in `.pre-commit-config.yaml`, and run at every commit, in `make check` and in CI. Start the web app alone with `pnpm --dir apps/web dev`, and the fake LLM server with `uv run fakellm`. More targets (`bootstrap`, `dev`, `e2e`) arrive during Phase 0.

## Conventions

- **Python:**
  - 3.13, managed with `uv`;
  - add dependencies with `uv add`, never `pip`; run tools through `uv run` or `make`;
  - workspace packages are named `actio-<name>` and imported as `actio_<name>`, such as `actio-fakellm`;
  - formatted and linted with `ruff`;
  - `mypy --strict` on core packages;
  - tests with `pytest`.
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
