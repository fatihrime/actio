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

## Revisions

### 2026-11-19: the worker's container

`make up` runs the worker too, as a container of the stack, held to the stack's rules ([ADR 0005](0005-container-images.md)). Two items of "Not done yet" are done with it: "the worker's container", with a `stop_grace_period` longer than `SHUTDOWN_GRACE_S`, and "a worker started on a database that was never migrated ends with a trace". `--role all`, which the decision said "comes with the worker's container", didn't: it comes with a worker in `make dev`, in the next step.

**The container** (`worker` in `compose.yaml`):

- **The API's image,** run as `actio --role worker`, locked down like every other container: a non-root user, a read-only root with `/tmp` in memory, no Linux capabilities, 256 MB at most, and a health check (below).
- **On the internal network alone,** with no port. Nothing reaches it, and it reaches only the data services.
- **Told only where its data is,** because the settings require both addresses, and where its file is (`ACTIO_ALIVE_FILE`). No models file, no plans file, no Ollama address, no rate limits: no job needs them.
- **Started after the migration succeeded,** as the API is. Not after Valkey: no job uses it yet.

**Started only on a migrated database.** Compose orders it after `migrate`, and the worker checks for itself as well:

- **Each time it has connected,** the first time and after each start again, and before it takes a job, it reads the database's revision and compares it with the one its code expects, as `/api/readyz` does. When they differ it takes no job, and the process ends with one line and status 1: `actio: the database is at migration 0008, and this code needs 0009: run "actio --role migrate" first`, or, for a database that was never migrated, `the database was never migrated, and …`. That was a trace of 78 lines.
- **A database that newer code migrated** is at a migration this code doesn't have, as after going back to an older commit. Migrating with this code couldn't help, so the line says something else: `the database is at migration 0010, and this code needs 0009: the database is ahead of this code`.
- **When it can't ask,** because the database went away again in the moment after the worker had connected, the process ends with one line too, in the driver's words, and status 1.
- **It reads the revision as Alembic does,** and Alembic would say in the log how it runs migrations: the worker's log leaves Alembic's info lines out, since it runs none.
- **The check runs inside `jobs.work`** (its `check`), not before it: `work` listens for the stop signals from before it connects, and a check made before it would run with nobody listening.
- **A database that can't be reached at the first start** is as before: the pool waits its 30 seconds, then one line and status 1.

**Healthy while it touches a file.** The worker has no port for Docker to probe.

- **Working means that Procrastinate's worker runs in this process:** from the moment a run of it has connected, and passed the check, until that run ends. While it does, the worker touches a file every 5 seconds. While it connects, while it waits to start again after it lost its database, and once it has stopped, nothing touches the file.
- **The health check is `python -m actio.core.alive`:** healthy while the file was touched within the last 20 seconds, four beats, as a turn is given up on after four of its own ([ADR 0022](0022-turns-that-run-apart-from-their-request.md)). Otherwise it exits 1 with one line that says why: no variable, no file, a file it can't look at, or the file last touched so many seconds ago. Docker keeps that line in the container's health log.
- **A file touched later than now isn't fresh either,** beyond one beat: its time is from a clock that was set back since, and it would say that the process works for as long as the clock takes to catch up.
- **Docker says "unhealthy" later than the check does.** The check fails 20 seconds after the last beat; Docker reports the container unhealthy after ten failed checks in a row, five seconds apart, as for the other containers: about 70 seconds after the last beat.
- **`actio.core.alive` imports only the standard library,** and reads `ACTIO_ALIVE_FILE` from its own environment, without the settings: Docker starts it every five seconds. The worker reads the same variable as its `alive_file` setting, and a test holds the two names together. Unset, as when the worker is run by hand, nothing is written.
- **`actio.core.jobs` knows nothing of files.** `jobs.work` takes a plain callable, `on_beat`, and calls it once a run has connected and passed its check, then every `alive.BEAT_S` until that run ends. `actio.worker` passes the function that touches the file. A beat that fails, as on a full disk, is said once in the log, as a warning, and the worker goes on working; its health check then says that it isn't.
- **What it proves:** that the worker's event loop turns, inside a run of Procrastinate's worker. **What it doesn't:** that a job succeeds, or that the database answers at this moment. A worker whose database has gone away goes on touching its file until Procrastinate's worker gives up, which can take 30 seconds.
- **Not Procrastinate's own heartbeat,** which it keeps in the database for each worker. Asking for it would put the name of one of Procrastinate's tables into `compose.yaml`, outside the one module that may know it; it would take a connection at each probe; and it says that *a* worker is alive, not *this* one.

**Started again when it failed, and only then: `restart: on-failure`.**

- **The worker is built to end when it can't go on:** 30 seconds after it lost its database, with one line or with Procrastinate's error, and with whatever else Procrastinate's worker raises. Something has to start it again, and in this stack that is Docker. A stop that Procrastinate's worker made by itself is still started again inside the process, a second later (`STARTS_AGAIN_AFTER_S`): Docker sees only a process that ended.
- **Measured** on Docker Engine 29.9.3 with Compose 5.7.0, in a scratch project:
    - a container that exits with status 1 after one second is started again: 4 times in the first 5 seconds, 7 by 15 seconds, 8 by 30, 9 by 45. The pause between tries grows;
    - one that exits with 1 after 12 seconds is started again each time, at once;
    - one that exits with 0 is not started again;
    - one stopped by hand (`docker compose stop`) stays stopped, whatever its status was;
    - `docker compose config --format json` gives the value back as `"on-failure"`.
- **Not `always` or `unless-stopped`.** Those two bring a container back whenever Docker itself starts again, as after a reboot. Nothing else of the stack comes back then, so the worker must not either, alone, trying to reach a Postgres that isn't there.
- **With `on-failure`, the worker stays down after Docker was restarted because it ends well, not because of the policy.** Docker's documentation ("Start containers automatically") says of `on-failure`: "It doesn't restart the container if the daemon restarts". Measured, that isn't so: when Docker starts again it starts a container whose last end was a failure. The worker ends with status 0 when Docker stops it, whether it is working or waiting for its database, so it stays down like the rest (both measured below).
- **A worker that Docker couldn't stop comes back alone.** When Docker or the machine goes down without stopping its containers, as when the power is lost, the worker's end counts as a failure, and Docker starts it again when it starts itself. It finds no Postgres, ends after 30 seconds with its one line, and is started again, every half minute, until `make up` brings the stack back or `make down` takes it away. That is harmless, and it is not what a stack should do: the answer is a restart policy for the whole stack, so that it comes back together, which is a decision of its own (below).
- **The worker is the only container with a restart policy.** The API is built the other way: it stays up without its database and says so at `/api/readyz`. `migrate` keeps `restart: "no"`. A test in `deploy/tests/` holds this.
- **An unhealthy worker is not started again:** Docker only says that it is unhealthy, to whoever looks. `make up` waits until every container is healthy, the worker too, and fails when one isn't. Both are measured below.

**Measured on the stack,** `compose.yaml` as it is, in a project of its own beside the one people run, on the same Docker:

- **`docker compose up --wait`** brought the seven containers up, the six that stay all healthy, in 13 seconds, with the images built. The worker swept an expired guest that was waiting for it as it started;
- **`docker compose stop worker`** took 0.3 seconds, and the worker ended with status 0 and stayed stopped. Started again by hand, it was healthy 5.3 seconds later;
- **its process killed inside the container:** Docker started it again, and it was healthy 5.7 seconds later. Killed through Docker (`docker kill`) it was left alone: Docker takes that for a stop by hand;
- **Postgres restarted:** the worker started again by itself, as before it had a container, and Docker did not restart it;
- **a worker that hangs** (its process halted with SIGSTOP): its health check first failed 24 seconds later, with `not working: /tmp/actio-worker-alive was last touched 24 seconds ago: …`. Docker called it unhealthy 71 seconds after it hung, and left it running. Let go on, it was healthy 0.6 seconds later;
- **Postgres stopped:** the worker ended 30 seconds later, and Docker started it again. While it waited for its database its health check said `/tmp/actio-worker-alive doesn't exist`: a container that is started again gets an empty `/tmp`. With Postgres back after 62 seconds, it was healthy 16 seconds later;
- **told to stop two seconds after Postgres was stopped:** Docker killed it after 25.2 seconds;
- **a worker that never touches its file,** a mistake planted in a copy of the code: `docker compose up --wait` ended with status 1 after 67 seconds, `container … worker-1 is unhealthy`. So `make up` and `make e2e` fail on a worker that doesn't work.

**Measured with Docker itself restarted** (`docker desktop restart`, twice, with the stack of `make up` running and control containers beside it):

- **the stack stayed down,** all of it, the worker too: it had ended with status 0. The second time it was waiting for a database that had been stopped, and it ended with status 0 again;
- **controls with `always` and `unless-stopped` came back;** one with no policy didn't;
- **controls with `on-failure`:** one that ends with status 1 when it is told to stop came back, and so did one that doesn't listen and has to be killed. One that ends with status 0 stayed down;
- **the worker's container started alone,** as Docker would start it, with Postgres stopped: it ended after 30 seconds with `actio: couldn't connect to the database: pool initialization incomplete after 30.0 sec`, and Docker started it again: three times in 110 seconds;
- **`make up` afterwards** brought the stack back in 16 seconds.

**Measured with `make dev`,** a session of it on the stack that `make up` had started: it stopped the worker's container with the API's and the web app's, and the container stayed stopped; the native API and page answered through the proxy 12 seconds later, and `make smoke` passed. `./scripts/with-dev-env.sh uv run actio --role worker` ran a worker beside it, which ended with status 0 at Ctrl-C.

**Stopping:**

- **`stop_grace_period: 25s`,** longer than `jobs.SHUTDOWN_GRACE_S`, 15 seconds, with room to take the worker off Procrastinate's list and close its pools. A test in `deploy/tests/` holds the two together, with five seconds of room at least.
- **Told to stop just after it lost its database,** before Procrastinate's worker has noticed and stopped by itself, the worker would wait 30 seconds more for a connection (measured in the decision's "On the built worker, as it is now": status 1, 29.0 seconds after the signal). Docker cuts that short at 25 seconds. That is accepted: such a worker has nothing in hand that could be finished. One told to stop while it connects, or while it waits to start again, ends at once.
- **`init: true`.** The worker listens for SIGTERM from before it connects. But as process 1 of a container a program gets no signal it has no handler for, and for the half second in which Python starts it has none. With an init process in front, a stop in that moment ends it at once instead of after 25 seconds. The web app's container has the same line, for Node.js.

**Around it:**

- **`make dev` stops the `worker` container too,** as it stops the API's and the web app's, in case `make up` left them running: a worker in a container must not go on working on the development database beside the native processes. `make dev` itself doesn't run a worker yet.
- **`make e2e` runs the worker of `compose.yaml`,** untouched by its overlay, and shows its log lines too when the tests failed.

**Still not done:**

- **`--role all`, and a worker in `make dev`:** the next step;
- **a stack that comes back after a reboot.** No container but the worker has a restart policy, so after Docker was restarted `make up` has to be run again; and a worker that Docker couldn't stop comes back alone until then. A policy for the whole stack would answer both;
- the rest of the list above: retries; a queueing lock; jobs left `doing` by a worker that died; jobs cut off at a stop that stay `aborted`; cleaning out failed and aborted jobs; more than one job at a time.
