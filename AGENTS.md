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

More targets (`bootstrap`, `dev`, `test`, `e2e`) arrive during Phase 0.

## Conventions

- **Python:**
  - 3.13, managed with `uv`;
  - formatted and linted with `ruff`;
  - `mypy --strict` on core packages;
  - tests with `pytest`.
- **TypeScript:**
  - strict mode, `pnpm`;
  - ESLint + Prettier;
  - tests with Vitest and Playwright.
- **Commits:** [Conventional Commits](https://www.conventionalcommits.org/): `feat:`, `fix:`, `docs:`, `test:`, `chore:` and so on.
- **Architecture boundaries,** to be enforced with import-linter:
  - `core` imports nothing internal;
  - domain modules never import FastAPI;
  - `packages/*` never import from `apps/*`.
- **Decisions:** anything hard to reverse gets an ADR. Copy [`docs/adr/0000-template.md`](docs/adr/0000-template.md).
