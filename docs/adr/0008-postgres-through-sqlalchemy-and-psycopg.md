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

## Revisions

### 2026-11-18: the job queue shares the transaction

The job queue arrived, with Procrastinate ([ADR 0024](0024-background-jobs-in-a-queue-in-postgres.md)):

- **A job is deferred through the connection of the session's own transaction.** SQLAlchemy hands Procrastinate the psycopg connection under it, and the job's row is written there. So the job commits with the change it follows, or not at all, and the API opens no second pool for the queue.
- **The queue's tables are built by migration 0009,** from Procrastinate's own SQL, and no table model describes them. Alembic leaves them out when it compares the models with the database.
- **The worker has a pool of its own,** from psycopg-pool, under LGPL-3.0-only like psycopg. It is used unmodified, and listed in the notices file beside psycopg.

### 2026-11-20: a statement that fails is said without its values

The engine is made with SQLAlchemy's `hide_parameters=True` (`actio.core.database`).

- **Why:** SQLAlchemy ends a failed statement's error with the values it was sent. Here those are what people wrote: a question, an answer's parts, a title. An error's text reaches the log: a job that fails is logged by the queue with its trace, and so is a turn whose outcome couldn't be recorded.
- **Why only now:** the review of the titles a model writes ([ADR 0025](0025-titles-written-by-a-model.md)) found it for a title whose write fails, and the same held already for an answer. Nothing had failed that way yet.
- **What an error still says:** the statement, with its placeholders, and the database's own message. So does the log.
- **What it doesn't cover:** the database's own message, which can quote a value. Measured the same day, with the one character that neither Postgres's text nor its JSON can hold, U+0000:
    - **in a model's answer:** the answer couldn't be stored, so its turn stayed `running` until it was given up on, and the database's message quoted the answer up to that character (`CONTEXT:  JSON data, line 1: …`), which reached the log with the error's trace;
    - **in a question's first line:** the request failed with an error of the driver's that quotes nothing, and was answered 500;
    - **both are older than this revision,** and hiding the values doesn't mend them. Keeping that character out of what is stored is a step of its own, not done yet.
- **The cost:** an error no longer shows the values that made a statement fail. Whoever needs them reads the row, or asks the database's own log.

### 2026-11-20: a character that can't be stored becomes U+FFFD

The revision above left one thing not done: the character Postgres can't hold. Measured before this one, it was two kinds of character, and more places than the two named there.

**The two kinds:**

- **U+0000.** Postgres holds it neither in its text nor in its JSON.
- **Half of a surrogate pair:** half of a character that takes two, as an emoji cut in two leaves it. JSON can spell it (`\ud83d`), and no UTF-8 can hold it: so it can't be stored, and Postgres's JSON refuses the spelling too.

**Measured on the code before,** each sent as a browser sends it, spelled as an escape in a JSON body, or written by a model the tests script:

- **U+0000 from a person:** in a question, in its first line or a later one, in a rename and in the address of a login, the request was answered 500. A password with one was taken, since a password is hashed and not stored as text. A model's name and a new account's address were refused, 422.
- **Half a pair from a person:** in a question, 500. Everywhere else the request was refused, 422: Pydantic reads a string that has a rule, such as a longest length, as Unicode to check the rule, and half a pair isn't Unicode ("unable to parse raw data as a unicode string"). A question's text has no rule of its own, and a plain string is taken as it comes.
- **Either from a model,** in an answer's text or its reasoning: the answer couldn't be stored. Its turn stayed `running` until it was given up on, a minute later, with the question unanswered, and the database's message, which quotes the answer up to that character, went into the log.

**Options:**

1. **Refuse a request that holds one,** 422.
    - Con: a person can't see such a character in what they pasted, and can't find it.
    - Con: nothing can be refused of a model: its answer is there, or lost.
2. **Leave it out.**
    - Con: a password would change in silence, and so would a question, with nothing to show where.
3. **Put U+FFFD in its place,** the character that stands for one that can't be shown.
    - Pro: it is what a browser makes of half a pair when it sends text as text, and what it shows for one. From a browser, only JSON's escapes carry half a pair past that.
    - Pro: one rule for people and for models, and nothing fails later and somewhere else.

**Decision: each such character becomes U+FFFD where the text comes in** (`actio.core.text.storable`).

- **In a request's body, before anything reads it** (`actio.api.body`): every request's body is a `Body`, which looks through what was sent, every string and every member's name, sixteen levels deep, where no request goes deeper than four. What is deeper is not looked at, and no route reads it. A test fails for a route whose body isn't one: it asks each route for the class it takes, and the API's description whether that was every route with a body. The same goes for half a pair that comes as three bytes of the body and not as an escape, which no browser sends: the body is read leniently, and they become half a pair too.
- **In a model's answer, where it is collected** (`Answer` in `actio.chat.answering`), before a reader is shown it: what is streamed is what is stored.
- **A model's title had it already** ([ADR 0025](0025-titles-written-by-a-model.md)): there such a character becomes a space, as other control characters do.
- **Other control characters are left:** Postgres holds them.

**What follows:**

- **A password is taken with the mark,** when it is set and when it is tried: the same password both times. One with U+0000 used to be taken as it was, so an account whose password holds one can no longer log in with it. No installation is known to have one.
- **A request that was refused for half a pair is taken now,** with the mark: a rename, the address of a login, a model's name. A login's address is looked up with the mark, and nobody has it. A refusal that names what it refuses names it with the mark.
- **Nobody signs up under an address with the mark in it:** 422, as before for an address with either character. Without that rule it would be taken: the rules for an address take U+FFFD for a letter of some script, and the account would stand under an address that is nobody's mailbox. Found by the review of this revision.
- **A character that a model sends in two pieces,** one half in each, becomes two marks: each piece is looked at as it comes, since it is streamed as it comes.
- **Not looked through:** what comes in a request's address. The one string there is the list's cursor. It is read as base64, which passes over a character that isn't base64's, so a cursor with U+0000 added to it is the cursor without: a test holds that. Only a time and an id come out of a cursor, never text.
- **A refusal of a body says where it is wrong, and never repeats what was sent.** So a string below the sixteen levels, which was not looked at, isn't written into a response either.

### 2026-11-23: the line a failed turn leaves names its model's error by its kind

A turn whose model fails leaves one warning in the log. It now reads `turn …: the model failed (ProviderError), recorded as model_error`: the error's kind, and the code the turn is recorded with (`actio.chat.answering`).

- **What it was:** the kind and the error's text. The text of a model client's error can quote what was received: up to 200 characters of a line of the answer that couldn't be read, a tool call's arguments, or whatever the model's server wrote of a request it refused. That is what a model wrote, or what a server says of what people wrote, and it stood in the log.
- **The rule is the one of 2026-11-20 above,** and of the titles a model writes ([ADR 0025](0025-titles-written-by-a-model.md)): what people wrote stays out of the log, so a line about a model's error names its kind. The line of a failed turn is older than that rule, and was written the other way on purpose: "the server's own words stay in its log", as a test's comment said. The titles' job and the load of a model ahead ([ADR 0026](0026-models-loaded-ahead-of-the-first-question.md)) follow the rule; the turn's line didn't.
- **Why only now:** the review of the load ahead read a sentence that said every such line names the kind alone, and found the one that didn't.
- **The cost:** the log no longer says what a model's server answered: the status of a refusal, or the sizes in "the prompt is longer than the window". The turn's record has the code, and the reader is told in words. Whoever needs the server's own words reads the server's own log. For a model on Ollama, the model client's command line asks it again and prints the error: `uv run actio-llm chat <the model's name on the server> "<prompt>" --context <its context window>`.
- **Held by a test** whose model fails with an error that quotes a line: the log has the kind and the code, and not the line.
