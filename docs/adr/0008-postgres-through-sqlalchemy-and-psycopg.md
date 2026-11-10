# 0008. Postgres through SQLAlchemy and psycopg 3, with migrations as a job

- **Status:** accepted
- **Date:** 2026-11-10

## Context

Actio keeps its data in Postgres ([ADR 0004](0004-local-development-stack.md)): accounts, conversations, the usage ledger and, later, the job queue. Before the first table exists, four things have to be settled:

- **How the application talks to Postgres.** The API is asynchronous. A later step adds a job queue whose jobs must commit in the same transaction as the data they follow ([ADR 0001](0001-one-app-several-process-roles.md)).
- **How the schema changes.** Changes must be versioned, reviewable and reversible.
- **When migrations run.** The stack must still come up with one command, `make up`.
- **How the stack says it isn't ready.** A process that is alive is not necessarily able to do its work.

## Options considered

**The driver, under SQLAlchemy 2:**
1. **psycopg 3.**
   - Pro: one driver for both asynchronous and synchronous code, so the application and its migrations share it.
   - Pro: Procrastinate, the job queue planned for a later step, is built on it. Only then can a job be enqueued in the transaction that writes its data.
   - Con: its license is LGPL-3.0. Actio's rule, until now, allowed only MIT, BSD and Apache-2.0 dependencies.
2. **asyncpg.**
   - Pro: Apache-2.0, fast and widely used.
   - Con: asynchronous only, and Procrastinate can't use it. The job queue would have to be written by hand.
3. **No SQLAlchemy:** SQL written by hand on either driver.
   - Pro: nothing between the code and the database.
   - Con: no table models to generate migrations from, or to check them against.

**When migrations run:**
1. **The API migrates the database when it starts.**
   - Pro: nothing extra to run.
   - Con: the API changes the schema it is serving from, and several replicas would do so at once.
   - Con: the API's database user would need the right to change tables, for ever.
2. **A job that runs before the API,** and must succeed first.
   - Pro: a schema change is its own step, with its own log and its own failure.
   - Pro: later, the job can connect as a user with more rights than the API has.
   - Con: one more container to order at start-up.
3. **By hand.**
   - Con: `make up` would no longer be enough.

## Decision

We chose **SQLAlchemy 2 on psycopg 3,** and **migrations as a job.**

- **LGPL libraries are now allowed,** when they are used unmodified, as separate libraries.
  - The maintainer decided this on 2026-11-10, when the driver's license turned up while adding it.
  - The LGPL's duties attach to the library, not to a program that imports it, so Actio stays MIT.
  - It also describes what was already true: the web image has shipped libvips, an LGPL library, since the first version, as a dependency of Next.js's image optimizer.
  - The price is a notices file. [`THIRD_PARTY_NOTICES.md`](https://github.com/fatihrime/actio/blob/main/THIRD_PARTY_NOTICES.md) lists what the images ship and how to replace the two LGPL libraries. GPL and AGPL code stays out.
- **One engine per process,** created at start-up and closed at shutdown. Nothing connects until it is used, so a process starts even while its database is down, and says so.
- **The connection settings are required secrets.** A process without `ACTIO_DATABASE_URL` or `ACTIO_VALKEY_URL` refuses to start, and names what is missing. Their values never appear when settings are printed.
- **Alembic migrations live inside the Python package** (`actio/migrations`), so the container image carries them and finds them without a configuration file. They are numbered: `0001_baseline.py`, `0002_…`.
  - The baseline creates nothing. It exists so that a database which was never migrated can be told apart from one that was.
- **`actio --role migrate` applies them and exits.** In the stack it is the `migrate` container, and the API starts only after that container succeeded.
- **Migrators take turns.** Each takes a Postgres advisory lock before it touches anything. Six started together all succeed; without the lock, the same test fails every time.
- **Two probes.**
  - `/api/healthz` answers whenever the process is alive.
  - `/api/readyz` answers 200 only when the database is reachable and at the migration the code expects, and Valkey answers. Otherwise it answers 503 and names what is missing: `unreachable` or `migrations pending`. The reason goes to the log only, because it can contain host names.

**One container rule changed on purpose.** Every container had to have a health check. A job that runs once and exits has nothing to probe, so jobs are now exempt. To keep the exemption narrow, jobs are listed by name in the policy test, which also requires that a job never restarts and that something waits for it to succeed.

**How it is tested:**
- Against a real Postgres, started from the image pinned in `compose.yaml`. Each test gets its own empty database.
- One test fails when a table model changes and no migration was written for it.
- The readiness probe is tested with the database unreachable, never migrated, and with Valkey down.

## Consequences

- **Easier:**
  - a schema change is a file in a pull request, applied the same way on a laptop, in CI and in production;
  - "the database isn't migrated" is a named state, not a stack trace on the first request;
  - the job queue can share the application's transactions.
- **Harder:**
  - start-up has an order: Postgres, then the migration, then the API;
  - the API's tests need Docker, as the usage-limits tests already did;
  - shipping an LGPL library means keeping the notices file true.
- **How to reverse it:**
  - SQLAlchemy hides the driver: moving to asyncpg is a dependency and one word in a URL, as long as nothing depends on psycopg directly;
  - migrating at start-up instead would be one call to the same function the job uses.
