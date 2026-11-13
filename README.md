# Actio

[![CI](https://github.com/fatihrime/actio/actions/workflows/ci.yml/badge.svg)](https://github.com/fatihrime/actio/actions/workflows/ci.yml)

> A self-hostable, local-first AI platform: a ChatGPT/Claude-style assistant and an OpenAI- and Anthropic-compatible developer gateway, both governed by Claude-inspired usage limits whose correctness is tested.

**Status:** early development, Phase 1 of 7. The foundation, accounts with rate-limited logins, the usage-limits engine and the client for local models are in place. The API now holds conversations and streams a local model's answers; the web chat on top of it comes next. This README grows with each phase, and nothing below counts as done until it ships with tests.

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
  - atomic reservations in Valkey, with a Postgres ledger as the source of truth.
- **Developer gateway:** OpenAI- and Anthropic-compatible endpoints with personal API keys. Coding agents can run on local models, metered by the same limits.
- **Knowledge:** document Q&A and web search, with citations.
- **Channels:**
  - a Telegram assistant;
  - a WhatsApp support bot for a fictional bike shop;
  - a race-free message pipeline with human takeover.
- **Video generation:** durable background jobs on serverless GPUs.

## Engineering highlights

- **Usage limits that are tested, not just plausible.**
  - The engine runs as Lua scripts inside Valkey, so two requests at the same moment can never both take the last credits.
  - A plain-Python reference model is its specification. Random sequences of requests, crashes, clock jumps and data loss run against both, and their results and stored state must match after every step.
  - [The design (ADR 0006)](docs/adr/0006-usage-limits-engine.md) · [the rules](https://fatihrime.github.io/actio/reference/quota/) · [the tests](packages/quota/tests)

- **A local-model client that never answers from half your prompt.**
  - Measured: Ollama has two engines. By default, one cuts a prompt that doesn't fit the model's context window, system prompt included, and still answers as if nothing happened. The other ignores the window and lets an answer run on: a small model thought for three minutes to say hello.
  - Actio's adapter makes the first refuse such a prompt with a clear error, and gives every answer a limit on both.
  - Its tests replay exchanges recorded from a real server, and live tests pin what each engine really does. With the strict settings removed, they fail.
  - [The design (ADR 0007)](docs/adr/0007-local-models-through-ollamas-native-api.md) · [what it guarantees](https://fatihrime.github.io/actio/reference/llm/) · [the tests](packages/llm/tests)

_More are added as each piece ships, with links to the design records (ADRs), tests and measurements._

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
| 0 | Foundation: repo, tooling, CI, docs | done |
| 1 | Local chat + usage limits | in progress: the limits engine, the model client and the chat's API are done; the web chat is next |
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
