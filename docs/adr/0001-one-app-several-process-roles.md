# 0001. One application, several process roles

- **Status:** accepted
- **Date:** 2026-11-08

## Context

Actio has several faces:
- a web chat;
- a developer API that OpenAI and Anthropic clients can talk to;
- chat-bot webhooks;
- background work: titles, document ingestion, video jobs, periodic sweeps.

They share most of their rules: usage limits, the model router, conversations, accounts and the database. Actio has to run on a 16 GB laptop next to a local model, and one person maintains it.

## Options considered

1. **A service per area** (chat, gateway, channels, media), talking over HTTP or a queue.
   - Pro: each service can be deployed and scaled on its own.
   - Con: every shared rule becomes a network contract.
   - Con: local development needs many containers and much more memory.
   - Con: keeping data consistent across services needs extra machinery such as outboxes and sagas.
2. **Two codebases, one for the API and one for the worker.**
   - Pro: simpler than option 1.
   - Con: models and configuration get duplicated and drift apart.
3. **One Python application started in different roles** (a modular monolith): `python -m actio --role api|worker|all`.
   - Pro: each area is a module with an explicit boundary, inside one codebase.
   - Con: the boundaries are only as firm as the rules that check them.

## Decision

We chose option 3: one codebase and one container image.
- The `api` role serves HTTP.
- The `worker` role, arriving in Phase 1, runs background jobs.
- The `all` role runs both in one process, for the smallest deployments.

With the job queue in Postgres (planned for Phase 1), a change and the job that follows it can commit in one database transaction.

Automated checks enforce the module boundaries instead of the network. From Phase 1, import-linter contracts forbid, for example, domain code from importing FastAPI, and shared packages from importing the app.

## Consequences

- **Easier:**
  - one process to debug;
  - refactoring across modules;
  - consistent data without extra machinery;
  - fewer containers on a 16 GB machine.
- **Harder:**
  - the boundaries hold only while the import rules are kept;
  - a crash in one module takes down its whole process, so production runs `api` and `worker` separately;
  - scaling happens per role, not per module.
- **How to reverse it:** a module with a clean boundary, such as media jobs, can move into its own service. It gets its own entry point, and its in-process calls are replaced by an API.
