# Actio

[![CI](https://github.com/fatihrime/actio/actions/workflows/ci.yml/badge.svg)](https://github.com/fatihrime/actio/actions/workflows/ci.yml)

> A self-hostable, local-first AI platform: a ChatGPT/Claude-style assistant and an OpenAI- and Anthropic-compatible developer gateway, both governed by Claude-inspired usage limits whose correctness is tested.

**Status:** early development, Phase 0 (foundation). This README grows with each phase, and nothing below counts as done until it ships with tests.

**Documentation:** <https://fatihrime.github.io/actio/>: getting started, architecture, development and the decision records.

## Why Actio?

Running an LLM on your own machine is easy. Running it well for several people is harder:

- one machine has to be shared fairly;
- long jobs have to survive restarts;
- traffic has to route around a provider that's down;
- usage limits have to hold under load, provably.

Actio tackles these problems end to end on a 16 GB laptop and free tiers.

## What v1 will do

- **Local chat** on Ollama: streaming, a reasoning display and branching conversations.
- **Usage limits inspired by Claude:**
  - a rolling 5-hour session plus a weekly limit;
  - integer credits;
  - atomic reservations in Redis, with a Postgres ledger as the source of truth.
- **Developer gateway:** OpenAI- and Anthropic-compatible endpoints with personal API keys. Coding agents can run on local models, metered by the same limits.
- **Knowledge:** document Q&A and web search, with citations.
- **Channels:**
  - a Telegram assistant;
  - a WhatsApp support bot for a fictional bike shop;
  - a race-free message pipeline with human takeover.
- **Video generation:** durable background jobs on serverless GPUs.

## Engineering highlights

_Filled in as each piece ships, with links to the design records (ADRs), tests and measurements._

## Quickstart

**Just run it,** on macOS or Linux. You need only Docker, `make` and `openssl`:

```sh
git clone https://github.com/fatihrime/actio.git && cd actio
make up   # builds and starts every service in containers; open http://localhost:8080
```

**Develop it,** on macOS with [Docker Desktop](https://docs.docker.com/desktop/). The API and the web app run natively, with hot reload:

```sh
make bootstrap   # checks your tools, installs everything, creates .env with random passwords
make dev         # starts the stack; open http://localhost:8080
```

`make smoke` checks a running stack, and `make down` stops it. CI runs the `make up` quickstart on Linux for every change.

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| 0 | Foundation: repo, tooling, CI, docs | in progress |
| 1 | Local chat + usage limits | planned |
| 2 | Model router + developer gateway | planned |
| 3 | Core chat UX | planned |
| 4 | Knowledge + web search | planned |
| 5 | Telegram + WhatsApp | planned |
| 6 | Video pipeline | planned |
| 7 | Hardening + v1.0 | planned |

**After v1:**
- memory and projects;
- artifacts and code analysis;
- MCP connectors and deep research;
- image generation;
- voice;
- file creation.

## How this project is built

Actio is built from scratch with AI assistance (Claude Code).
- Every change is small and tested, and I review and commit each one myself.
- Design decisions are recorded in [`docs/adr/`](docs/adr/).
- [How AI is used](https://fatihrime.github.io/actio/ai-workflow/) explains the workflow, and how the assistant's work is checked.

## License

[MIT](LICENSE)
