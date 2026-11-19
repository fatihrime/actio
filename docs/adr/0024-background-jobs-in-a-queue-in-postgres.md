# 0024. Background jobs in a queue in Postgres, through Procrastinate

- **Status:** accepted
- **Date:** 2026-11-18

## Context

Actio is one application that runs in several roles ([ADR 0001](0001-one-app-several-process-roles.md)). The `api` role serves HTTP and `migrate` applies the migrations. The `worker` role, for background jobs, was left for a later step, and so was its queue: in Postgres, so that a change and the job that follows it commit in one transaction. psycopg 3 was chosen as the driver partly for that reason ([ADR 0008](0008-postgres-through-sqlalchemy-and-psycopg.md)).

Work is waiting for it:

- **Expired guests and sessions** are deleted only when a new guest arrives ([ADR 0011](0011-rate-limits-as-token-buckets-in-valkey.md)). On an installation where none does, they stay.
- **Titles written by a model** ([ADR 0018](0018-the-list-of-conversations-is-the-servers.md)), and **turns left behind by a process that died** ([ADR 0020](0020-usage-limits-on-turns.md)), and later documents to read and videos to make.

What a queue has to do here:

- **A job exists if and only if the change it follows was committed.** A job for a change that was rolled back acts on nothing; a change committed without its job is never followed up.
- **The code that defers sits below the code that runs a job.** A domain module defers; the worker, a layer above, knows the handlers.
- **Valkey keeps nothing on disk** ([ADR 0020](0020-usage-limits-on-turns.md)). What is in it is gone after a restart.
- **One person maintains it, on a 16 GB laptop.** Another service to run costs memory and attention.

## Options considered

1. **Procrastinate,** a queue in Postgres for Python, on psycopg 3.
    - Pro: a job can be deferred through a connection it is given, inside that connection's transaction.
    - Pro: schedules, a worker that is woken when a job is committed, and one that stops when it is told to, letting the job in hand finish.
    - Pro: MIT.
    - Con: its tables, functions and types live in Actio's database, and change with its versions. An upgrade is a migration of Actio's.
    - Con: it brings six more packages into the API's image, one of them under the LGPL.
2. **A table of our own,** read with `FOR UPDATE SKIP LOCKED`.
    - Pro: nothing to depend on.
    - Con: schedules, wake-ups, and the worker's bookkeeping, who holds which job, stopping and failures, would all be ours to write and test.
3. **A queue in Valkey or in a broker,** as arq, Celery, Dramatiq, SAQ and Taskiq can keep one.
    - Con: a job can't commit with the change it follows. Each needs a table of jobs to send in Postgres, written in the change's transaction, and something that empties it into the queue.
    - Con: Valkey keeps nothing on disk here, so a restart would lose what is queued. A broker is one more service.

## Decision

**Procrastinate 3.11.0, pinned exactly, behind one module, `actio.core.jobs`. A job is deferred by its name, in the transaction of the change it follows, and run by a worker process, `actio --role worker`.** A table of our own stays the way back.

**One module knows it** (`actio.core.jobs`):

- **Only `actio.core.jobs` imports Procrastinate.** An import rule holds this. If Procrastinate has to go, that one module is rewritten over a table of our own.
- **A job is a name, an async function, and optionally a schedule** (`jobs.Job`). No type of Procrastinate's leaves the module. A scheduled run is passed the time it is for; the module drops it, since no job needs it. What a handler needs, such as the database or a clock, is bound in by whoever lists the job.
- **Pinned exactly,** as the web app pins the `ai` package: its schema lives in Actio's migrations, so an upgrade is a change to make on purpose.

**Deferring** (`await jobs.defer(db, "name", **args)`):

- **Through the connection of the session's own transaction.** Procrastinate takes a connection for a deferral and runs its insert there; the session's is reached through SQLAlchemy. `defer` never commits, like everything below `actio.api`.
- **So the job goes with the change:** there once the transaction commits, and never if it is rolled back, by the code or by an exception, or if a savepoint it was deferred in is rolled back. Until the commit no worker sees it.
- **By its name,** not by importing its handler, which lives above. A name no worker has a handler for ends as `failed`.
- **The deferring side opens nothing.** Its Procrastinate app has no connection settings and is never opened, so a process that only defers, as the API will, holds no pool and no connection for the queue.
- **A job's arguments are JSON,** and Procrastinate writes them in its log with the job: ids and such, never what people wrote.
- **A job the database refuses raises `jobs.DeferError`,** which is one of SQLAlchemy's errors: what any statement of the session raises when it fails, so the code that handles those handles this. Procrastinate wraps the driver's error in one of its own, and that one stays inside the module.

**The worker** (`actio.worker`, `actio --role worker`):

- **Lists every job a worker runs,** with what each needs bound in. It is a top layer, like `actio.api`, beside it: neither imports the other. It reads the clock, and the functions it calls take `now`, as ever.
- **A pool of its own,** of one to two connections, and one more that listens for new jobs. Its handlers use the process's database pool, as the API's routes do (`open_resources`).
- **One job at a time.**
- **Woken, not polling:** Postgres tells the worker when a job is committed (`LISTEN`/`NOTIFY`). It also looks every five seconds, should a notice be missed. One can be in the instant a worker starts: it looks for jobs first and listens a moment later, and a job committed in between waits for the next look (seen in 7 of 26 runs of a test that starts a worker and defers a job at once).
- **A job that succeeded is deleted at once,** with its events. Otherwise a job every ten minutes would leave a row for ever.
- **A job that failed stays, as `failed`,** and is not tried again. Procrastinate logs it as an error, with the job's name and the exception. The worker goes on with the next job.
- **Told to stop,** by SIGTERM or SIGINT, it takes no new job, gives the one in hand 15 seconds (`jobs.SHUTDOWN_GRACE_S`), and then cuts it off: that job stays as `aborted`. Then the process ends, with status 0: whenever it was told, as long as its database is there. Whatever stops the process has to wait longer than those 15 seconds.
    - **The worker listens for the signals itself,** on the event loop, from before it connects (`jobs.work`). The first one cancels the task that runs the worker: the way Procrastinate documents to stop a worker that runs inside a larger program. Procrastinate's worker then does the rest, and `jobs.work` takes the cancellation it asked for as done, and returns. Procrastinate's own handling of the signals is off: it listens only once its worker runs, and for the first signal only (see "Measured").
    - **Told while it connects, or while it waits to start again,** it ends at once. The pool it was opening is closed.
    - **Later signals change nothing.** Each is noted in the log, and the job in hand keeps its 15 seconds. Docker sends one SIGTERM, and SIGKILL after its own wait; someone at a terminal waits 15 seconds at most.
    - **Told to stop while its database is away, it ends badly.** Procrastinate then waits 30 seconds for a connection, to take its worker off the list it keeps in the database, and the process ends with Procrastinate's error, a trace and status 1: up to 30 seconds after the 15. Whatever stops the process decides whether it waits for that.
    - **SIGINT goes the way SIGTERM does.** `asyncio.run` has a handler of its own for it, which cancels the whole program and ends it with `KeyboardInterrupt`. While the worker listens, the event loop takes the signal and that handler isn't called. Afterwards the worker gives each signal back to the handler it found.
- **A worker that stops without being told to starts again.** Procrastinate stops its worker when one of the tasks it runs beside the jobs fails, and returns as from a stop it was asked for. There are three: the one that listens for new jobs, which fails when its connection is lost and can't be made again at once, as when the database restarts; the one that says every ten seconds that the worker is alive; and the one that defers the scheduled jobs. `jobs.work` then says so in the log, as a warning, waits a second (`jobs.STARTS_AGAIN_AFTER_S`), and starts again: a new pool, which waits for the database as at the first start.
    - A database that was away for a moment costs a pause. One that stays away ends the process with status 1: with the one line below, once the new pool has waited its 30 seconds; or, when it went away while the worker ran, with Procrastinate's own error and its trace, after Procrastinate has waited 30 seconds to take its worker off the list it keeps in the database.
    - Only a stop Procrastinate's worker made by itself starts it again. What its worker raises still ends the process.
    - Procrastinate watches the listening connection only while something is scheduled: its watch ends with the first of its worker's tasks on the side that ends, and the one that defers scheduled jobs ends at once when there is none. The worker always has the sweep.
- **A database it can't reach when it starts:** the pool waits 30 seconds for its first connection, as psycopg-pool does. Then the process ends with one line, and status 1: `actio: couldn't connect to the database: pool initialization incomplete after 30.0 sec`. The pool logs each try before it.
- **It sets up its own log,** having no web server to do it: every record on stderr, from the level `ACTIO_LOG_LEVEL` sets, one line each with the time, the level, the logger and the message. How the API logs didn't change.

**Schedules** (`Job.every`):

- **A cron line, in UTC:** five fields, or six with the seconds last, as croniter reads them. `*/10 * * * *` is every ten minutes.
- **Each scheduled time runs once,** however many workers there are: Procrastinate records each time it has deferred in the database.
- **A worker that starts runs the time that has just passed,** if it was less than ten minutes ago and no worker has run it yet. So a job every ten minutes also runs when the first worker starts.

**The schema is migration 0009:**

- **It applies Procrastinate's own migration files, up to 3.11.0, in the order of their names,** read from the installed package by `jobs.schema_files("3.11.0")`. The files for each version come in two kinds, `pre` and `post`; both are applied.
- **Not the package's `schema.sql`,** the whole schema in one file. That is whatever version is installed: after an upgrade the same migration would build a different schema, and the upgrade's own migration would then be applied to a schema that is new already. Up to a named version, migration 0009 builds the same schema whatever is installed.
- **Never copied into the repository.** The SQL is Procrastinate's, read where the package keeps it.
- **Sent through the driver's connection,** inside the migration's transaction, as the files have it. Through SQLAlchemy's, parameters are passed along, and the driver reads every `%` in the files as a placeholder.
- **Undone by dropping everything named `procrastinate_…`** in the schema: tables, functions and procedures, types. Procrastinate ships nothing that undoes its schema. The jobs in the queue go with it.
- **Alembic leaves the queue's tables alone.** No model describes them, so compared with the models they would read as tables to drop. `migrations/env.py` keeps them out of the comparison, for the check that the models and the migrations agree and for `make migration`.
- **A test holds the queue our migrations build to the installed package's `schema.sql`,** applied to a second empty database, and compared in the catalog: the columns, the functions with their definitions, the enums' labels, the composite types, the indexes, the constraints and the triggers. It fails after an upgrade of Procrastinate that came without its migration.

**The first job: the sweep** (`sweep-expired`):

- **Every ten minutes,** the worker deletes a batch of expired guests and sessions, as its own unit of work. One batch a run, as `sessions.delete_expired` takes them. A worker that starts sweeps within a second, unless another has since the last ten-minute mark.
- **It says what it deleted,** in the log at the info level, and nothing when it deleted nothing.
- **The API still sweeps after each new guest.** The two cover different installations: a busy one is kept clear by its newcomers, a quiet one by the schedule. The sweep skips rows another transaction holds, so the two never wait for each other.

**The command line:**

- **`--role worker`** reads the settings, opens the process's resources, and works until it is told to stop. It doesn't read the models file or the plans file: no job needs them yet.
- **`--reload` is the API's.** With another role it is refused, in one line.
- **`all`,** both roles in one process, comes with the worker's container.

**What it brings,** each license read from its own package's metadata: Procrastinate (MIT), asgiref (BSD-3-Clause), attrs (MIT), croniter (MIT), python-dateutil (BSD-3-Clause), packaging (Apache-2.0 or BSD-2-Clause) and **psycopg-pool (LGPL-3.0-only)**. psycopg-pool is an LGPL library used unmodified, as psycopg is ([ADR 0008](0008-postgres-through-sqlalchemy-and-psycopg.md)), and is listed in `THIRD_PARTY_NOTICES.md`.

## Measured

In a spike before the build: a throwaway project with Procrastinate 3.11.0, SQLAlchemy 2.1.8, psycopg 3.3.8 and Python 3.13, against Postgres 17, the image pinned in `compose.yaml`.

**Deferring through the session's connection:**

- after the commit, the row written beside the job and the job were both there. Before it, another connection saw neither;
- after a rollback, or a session left by an exception: neither;
- a savepoint (`db.begin_nested()`) that was rolled back took only the job deferred inside it;
- the session worked as ever after a deferral, before its commit;
- the app that deferred had no pool and was never opened, and deferred all the same;
- 200 deferrals in an open transaction took 111 ms: 0.56 ms each.

**The worker:**

- **woken:** a handler started 1.2 to 5.2 ms after the commit that deferred its job, in five runs (5.2, 3.6, 1.5, 3.9 and 1.2 ms);
- **connections:** two idle workers sharing one app held 4 connections, one of each a listening connection outside the pool; 0 once the app was closed;
- **jobs that succeeded** left no row; one that failed stayed, as `failed`. A job whose name had no handler ended as `failed`, and the worker went on;
- **stopping:** with a job of 1 s in hand and no limit, the worker stopped after 0.72 s, and the job had finished. With a job of 5 s and a limit of 0.5 s, it stopped after 0.53 s; the job got `CancelledError`, and stayed as `aborted`;
- **working through what is queued, and returning** (as tests run a worker): 3 jobs in 0.04 s;
- **a database that can't be reached** when the worker starts: the pool gave up after 30.0 s, with `psycopg_pool.PoolTimeout`.

**Schedules:**

- with two workers at once, each scheduled second ran exactly once;
- a job on `*/10 * * * *` whose last time was less than ten minutes before the worker started ran 0.03 s after the start, for that time. A second worker started within the same ten minutes didn't run it again.

**The schema:**

- 39 files, applied in 0.05 s in one transaction. The result was identical to the package's `schema.sql` in every catalog compared: 23 columns, 18 functions, 2 enums, 7 attributes of composite types, 14 indexes, 7 triggers, 9 constraints and 4 sequences;
- a failure half-way left nothing behind: Postgres changes its schema in transactions;
- through `exec_driver_sql`, the files fail: `psycopg.ProgrammingError: only '%s', '%b', '%t' are allowed as placeholders, got '%)'`, first at `00.17.00_03_add_checks_to_finish_job.sql`;
- the downgrade left 0 relations, 0 functions and 0 types in the schema, and the files applied again built the identical schema;
- compared with an empty set of models, a database with the queue made Alembic want 13 changes, removing its tables and indexes; with the queue's tables left out of the comparison, none.

**For later:**

- a deferral with a queueing lock that is already taken raises `AlreadyEnqueued`, and **the caller's whole transaction is then aborted** ("current transaction is aborted, commands ignored until end of transaction block"), unless the deferral ran inside a savepoint;
- `RetryStrategy(max_attempts=3)` ran a failing job **four** times: `job.attempts` was 0, 1, 2 and 3. It counts retries, not tries.

**On the built worker, before it was changed:** the first build left the signals to Procrastinate, and ended when Procrastinate's worker returned. Run as `python -m actio --role worker` against a scratch Postgres, it did this, which is why the worker now listens for the signals itself and starts again:

- **Postgres restarted under a running worker** (`docker restart`, away for 0.8 s): the worker ended by itself, with status 0, 1.3 s after the restart began. Twice out of twice. The log: `ERROR procrastinate.worker.worker: Side task listener failed with exception`, `Stop requested`, traces, `Stopped worker on all queues`. Procrastinate's listener reconnects once, at once, and when that is refused its side task fails and Procrastinate stops the worker; `run_worker_async` then returns normally. A status 0 reads as a clean stop, so nothing would start it again, while the API lives through the same restart;
- **Postgres stopped for good under a running worker:** status 1 after 30.2 s, with traces. `unregister_worker` waits 30 s for a pool connection, then `ConnectorException` comes out of `run_worker_async`. That one is truthful, and stays as it is;
- **SIGTERM while the worker waits for an unreachable database:** the process died by the signal's default, status -15, because Procrastinate installs its handlers only inside its loop;
- **SIGTERM to the idle worker:** status 0 after 0.14 s. SIGINT: 0.

**On the built worker, as it is now,** run the same way:

- **started on a migrated database that held one expired guest, with nothing deferred by hand:** the guest was gone 0.47 s after the process was started, by the schedule alone. A second worker started within the same ten minutes didn't sweep again;
- **idle, it held 4 connections to Postgres:** the queue's pool, the one that listens, and one of the process's own pool;
- **told to stop:** SIGTERM to the idle worker, status 0 after 0.13 s; SIGINT, status 0 after 0.14 s; SIGTERM while it waited for an unreachable database, status 0, 0.13 s after the signal;
- **Postgres restarted under it** (away for 0.8 s): it said once that it had stopped without being told to, and started again. Eight seconds later it was running, and a SIGTERM then ended it with status 0 after 0.19 s;
- **told to stop one second after its database was stopped,** with no job in hand: status 1, 29.0 s after the signal, with Procrastinate's error and its traces;
- **its database stopped for good, and nobody told it:** status 1 after 30.2 s, as before.

## Consequences

- **Easier:**
    - a change and the job that follows it are committed together, or not at all;
    - a new job is an async function and a line in `actio.worker`; a schedule is a cron line;
    - a quiet installation is cleaned up too, every ten minutes;
    - the API holds nothing more for the queue: deferring is one insert in a transaction it has open anyway.
- **Harder:**
    - **an upgrade of Procrastinate is a migration:** a new one that applies the package's files after the version the last one names, up to the new one, and a new pin. The test of the schema fails until both are there. Procrastinate means its `pre` files to be applied before new code runs, and its `post` files after: read its notes for the versions in between;
    - **one more process to run,** with a pool of its own for the queue: up to three connections to Postgres, beside those of the process's own pool;
    - **at the info level, Procrastinate logs every job,** when it starts and how it ended, with its arguments;
    - **one more LGPL library in the API's image,** and the notices file to keep true.
- **Not done yet:**
    - **the worker's container,** and `--role all` for the smallest installations. They come next, and with them what stops the worker: `stop_grace_period` in `compose.yaml` has to be longer than `SHUTDOWN_GRACE_S`;
    - **a worker started on a database that was never migrated ends with a trace** (status 1 after 0.5 s, measured). The step that gives the worker its container will order it after the migration;
    - **retries.** A job that fails stays failed. Whoever adds them: Procrastinate's `max_attempts` counts retries, so 3 means four tries;
    - **a queueing lock,** which keeps a second job of a kind out while one waits. A deferral it refuses aborts the caller's whole transaction, unless it was made in a savepoint;
    - **jobs left `doing` by a worker that died.** Procrastinate keeps a heartbeat for each worker, and leaves running such jobs again to the application;
    - **jobs cut off at a stop stay `aborted`,** and nothing runs them again. For the sweep that is harmless: the next one comes ten minutes later;
    - **cleaning out failed and aborted jobs,** which stay in the table;
    - **more than one job at a time.**
- **How to reverse it:** `defer`, `work` and `Job` are all the rest of the code sees. Moving to a table of our own, read with `FOR UPDATE SKIP LOCKED`, rewrites `actio.core.jobs` with the same three, and a migration drops the queue's objects, as 0009's downgrade does, and creates the table.
