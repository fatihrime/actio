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

### 2026-11-19: `--role all`, and a worker in `make dev`

`actio --role all` runs the API's web server with the worker inside the same process, and `make dev` runs it, with `--reload` ([ADR 0004](0004-local-development-stack.md), revision of the same day). The first item of the revision above's "Still not done" is done with it. Production still runs `api` and `worker` apart ([ADR 0001](0001-one-app-several-process-roles.md), revision of the same day).

**The role** (`actio.cli`):

- **It goes the API's way.** It reads the models file and the plans file first, so a mistake in one is one line and status 2, then runs uvicorn with the API's arguments: the address, the trusted proxies, the 25 seconds the server waits for its requests at a stop. Only the app and the log differ (below).
- **It doesn't migrate.** `actio --role migrate` stays its own step, run before, as for `api`.
- **`--reload` is allowed for `api` and `all`,** watching the same folder. For `migrate` and `worker` it is refused, with the same line as before.
- **The app is `create_app(with_worker=True)`.** uvicorn's reloader takes the app by its name and calls it without arguments, so a second factory stands beside the first: `create_app_with_worker`. `--role api` serves `create_app`, and runs no worker.

**The worker inside the process** (`actio.worker.run_inside`):

- **It works as the worker role does:** `jobs.work` with the same jobs, the same check of the database's migration each time it has connected, and the same file touched while it works when `ACTIO_ALIVE_FILE` names one. It opens no resources of its own: its jobs use the app's, the pool the routes use. The queue's pool and its listening connection are opened by `jobs.work`, as ever.
- **It leaves the process's signals alone** (`on_signals=False`). The API listens for them, and tells it.
- **It never ends the process.** In the worker role a worker that can't go on ends its process, and Docker starts it again. Inside the API nothing would: the API stays up without its database and says so at `/api/readyz`, and under `--reload` a server process that ended by itself isn't started again until the next save. So the worker says in the log why it stopped, at the error level, and starts again after `worker.TRIES_AGAIN_AFTER_S`, 10 seconds, each time:
    - a database it can't reach, or that isn't migrated: one line, without a trace, in the error's own words, which say what to do: `The worker can't work: the database was never migrated, and this code needs 0009: run "actio --role migrate" first. It tries again in 10 s.`;
    - anything else: `The worker ended with an error. It starts again in 10 s.`, with the trace.
- **The pause,** because a database that isn't migrated answers at once: without it the worker would go round in a circle, and fill the log with the same line.
- **Cancelled while it connects or while it waits to try again,** it ends at once. Cancelled while it works, it ends once the job in hand has finished, or has had its grace. Either way the cancellation is passed on.
- **Once it was told to stop, nothing starts it again.** Told to stop while its database is away, Procrastinate's worker waits 30 seconds for a connection, to take itself off its list, and then raises its own error in place of the cancellation. Started again after that, the worker would work on, nobody would tell it to stop a second time, and the API's process would wait for it for ever. So when its task was asked to cancel (`Task.cancelling()`), the worker says the error as it otherwise would, without "tries again", and ends. A test replaces `jobs.work` with one that fails so, and fails if it is called a second time.

**In the API's lifespan** (`actio.app`):

- **The worker starts in a task** once the process's resources are open.
- **Told to stop by a signal, it is told at the same moment as the answers being written** ([ADR 0022](0022-turns-that-run-apart-from-their-request.md)): `told_to_stop`, which runs inside the signal handler, leaves a second note for the event loop. The web server tells the application to shut down only once the requests it is answering have ended, up to 25 seconds later (`cli.WAITS_FOR_REQUESTS_S`). A job in hand that got its 15 seconds only then would outlast the 35 seconds Docker gives the API. Told at the signal, the worker's grace runs inside the server's wait, so a process that runs both takes no longer to stop than the API alone. A test holds `jobs.SHUTDOWN_GRACE_S` shorter than `cli.WAITS_FOR_REQUESTS_S`.
- **It is told by cancelling its task, and only once.** Procrastinate's worker takes the first cancellation as "take no new job, and give the one in hand its grace", and then waits for its own shutdown without shielding it. A second cancellation would cancel that shutdown: the job in hand would lose its grace, and the worker would stay on Procrastinate's list. So a second signal, and the lifespan's exit after a signal, tell it nothing more. A test sends two real signals, SIGTERM then SIGINT, with a job in hand, then shuts the app down while the job is still in hand, and fails if the task is cancelled twice. Another sees that the worker takes no new job from the first signal on, while the app still serves.
- **On the way out,** the lifespan tells the worker to stop if nothing has yet, as in a shutdown without a signal, before the turns are drained, so that the two graces run together. It drains the turns as before, and returns only once the worker's task has ended, while the process's resources are still open: the job in hand uses the database. It waits without a time limit. Told to stop while its database is away, Procrastinate waits up to 30 seconds more for a connection, as in the worker role, and whatever stops the process cuts that short.
- **An app without the worker** does what it did before.
- **A SIGINT that follows another signal is uvicorn's force quit,** and nothing of the application's: uvicorn then skips the application's shutdown, and the process ends at once. The job in hand is cut off and stays `doing`, with its worker still on Procrastinate's list, as an answer being written stays `running` (measured below). At a terminal that is a second Ctrl-C. Under `make dev` it is the first: see "Still not done".

**Measured** before the build, on uvloop, which is the API's event loop, with `jobs.work(..., on_signals=False)` as a task beside other work: one cancellation, with a job of one second in hand, ended the task after 1.02 seconds, cancelled, with the job finished and no row left.

**Measured on the built process,** `python -m actio --role all` against a scratch Postgres 17, the image pinned in `compose.yaml`:

- **started on a migrated database that held one expired guest:** `/api/healthz` answered 0.64 seconds after the process was started, and the guest was gone after 0.73 seconds, by the worker's schedule alone. A second start within the same ten minutes didn't sweep again. Idle, the process held 4 connections to Postgres, as the worker's own process does;
- **told to stop while idle:** at a SIGTERM it ended 0.15 seconds later, by the signal, after Procrastinate's `Stopped worker on all queues` and uvicorn's `Application shutdown complete.`, with no trace. At a SIGINT, with status 0 after 0.36 seconds;
- **started on a database that was never migrated:** `/api/healthz` answered after 0.59 seconds, and `/api/readyz` said `"database":"migrations pending"`. The worker said its one line at once, and again 10 seconds later. Migrated meanwhile, it swept an expired guest 6.5 seconds after the migration had ended, with nothing started again by hand;
- **Postgres restarted under it** (away for 0.9 seconds): the worker said once that it had stopped without being told to, and started again, as the worker role does. The API answered, and a sweep deferred afterwards ran 0.04 seconds later;
- **Postgres stopped for 75 seconds:** the process stayed up, `/api/healthz` answered, and `/api/readyz` said `"database":"unreachable"`. The worker ended 30 seconds after the database had gone, with Procrastinate's error and its traces, as the worker's own process does, and said `The worker ended with an error. It starts again in 10 s.` Its next start waited 30 seconds for a connection, and said `The worker can't work: couldn't connect to the database: pool initialization incomplete after 30.0 sec. It tries again in 10 s.` With Postgres back, a deferred sweep ran 4.4 seconds after the database answered again;
- **told to stop one second after its database was stopped:** the process ended 28.9 seconds after the SIGTERM, by the signal. In its log: Procrastinate's error and its traces, `The worker ended with an error.`, and uvicorn's `Application shutdown complete.` The worker was not started again.

**Measured with a job in hand,** the app with the worker inside it, served by uvicorn, a second into a job of eight seconds:

- **one SIGTERM,** as Docker and uvicorn's reloader send: the process ended 7.0 seconds later, when the job had finished. No row was left, the worker was off Procrastinate's list, and the log had no trace;
- **two SIGINTs a tenth of a second apart,** as Ctrl-C under `make dev` reaches the server: the process ended after 0.37 seconds, with status 0. The job stayed `doing`, the worker stayed on the list, and the log had five traces and no `Application shutdown complete.`

**Measured with `make dev`,** a session of it on the stack that `make up` had started:

- it stopped the containers of the API, the web app and the worker. The native API and page answered through the proxy 11 seconds later, and `make smoke` passed;
- **a sweep deferred as the application defers ran within 0.04 seconds,** by the worker inside the API's process. Procrastinate's lines for the job were in the terminal, under `[api]`;
- **a Python file saved:** the reloader noticed after 0.12 seconds. The server shut down as at a SIGTERM (`Stopped worker on all queues`, `Application shutdown complete.`), and 0.99 seconds after the save the API was ready and its worker had started again. A sweep deferred then ran within 0.05 seconds;
- **Ctrl-C:** `make dev` ended 0.4 seconds later, with nothing left running and no trace. The idle worker had stopped, 3 milliseconds after it was told, but the server skipped the application's shutdown: no `Waiting for application shutdown.` in its log. The same command line with `--role api`, which this step didn't change, did the same;
- `make up` afterwards brought the stack back in 15 seconds.

**The log of `all`:**

- **uvicorn's default sets up only its own loggers.** So Actio's records below a warning never reach the API's log, and its warnings are written bare. The worker's lines, what the sweep deleted and Procrastinate's line for each job, would be lost.
- **So `--role all` gives uvicorn a log config:** a deep copy of uvicorn's own, which keeps its formatters, handlers and loggers under their names (uvicorn writes into its formatters), with one more handler on stderr, in the worker role's format, on the root logger. `actio` and `procrastinate` log from `ACTIO_LOG_LEVEL`, and everything else from warnings up, or from the settings' level when that is higher. Below a warning, `httpx2` writes a line for each request to a model's server, the reloader one for each change it sees, and Alembic two each time the migration is read.
- **Given to uvicorn, not set up by the command line,** because under `--reload` the server runs in a process of its own, started anew, which sets up its log from uvicorn's config alone.
- **`--role api` logs as it did.**

**What else differs from the worker role:**

- **`ACTIO_ALIVE_FILE` is honoured the same way,** though no container runs `all`: unset, nothing is written.
- **The process ends as the API's does.** uvicorn, once it has shut down well, sends itself the signal it was told with again, and a SIGTERM that Python doesn't handle ends a process by the signal: status 143 in a shell, not 0. Nothing restarts an `all` process by its status.

**Still not done:**

- **`/api/readyz` doesn't say whether the worker inside an `all` process works.** It is the next small step;
- **Ctrl-C under `make dev` is a force quit.** The terminal sends the SIGINT to every process `make dev` started, and concurrently, which runs the API and the web app side by side ([ADR 0004](0004-local-development-stack.md)), sends each of them the same signal again. uvicorn takes the second as its force quit. So an answer being written isn't recorded as interrupted, and a job in hand stays `doing`, until something sweeps them. That is older than this step: the API alone was stopped the same way, and the measurement above found it. Putting it right is a step of its own;
- the rest of the list of the revision above.

### 2026-11-19: Ctrl-C under `make dev`

"Ctrl-C under `make dev` is a force quit", in the list above, is done. A server that uvicorn's reloader started now takes the first signal it gets, and later ones change nothing: the application is shut down, and the job in hand gets its grace ([ADR 0022](0022-turns-that-run-apart-from-their-request.md), revision of the same day). Measured there, a second into a job of eight seconds: the processes ended 7.3 seconds after Ctrl-C, with the job finished and its worker off Procrastinate's list, where the job had stayed `doing` before.

The bullet "A SIGINT that follows another signal is uvicorn's force quit", in the revision above, still holds for a server started directly, as a container starts it or someone at a terminal without `--reload`.

### 2026-11-19: readiness says whether the worker inside works

"`/api/readyz` doesn't say whether the worker inside an `all` process works", in the list of the revision "`--role all`, and a worker in `make dev`", is done. Before, a worker inside the API that couldn't work said so in the log every ten seconds, and the probe went on saying `ready`.

**What the probe answers** (`actio.api.health`):

- **A fourth key, `worker`,** in a process that runs the worker inside it: `ok` or `not working`.
- **`not working` makes the answer 503,** with the status `unavailable`, as a database that is away does. Such a process has two kinds of work, the requests and the jobs, and can't do one of them. `/api/healthz` still answers 200: as for a database that is away, starting the process again wouldn't help, and the worker inside tries again by itself.
- **Where no worker runs, the key is left out,** not sent as `null`. So the `api` role answers exactly as it did, and whatever reads its answers whole, a script or a test, reads the same. The recorded description lists `worker` as an optional string, `ok` or `not working`, and never as `null` (pydantic's `SkipJsonSchema[None]`): the web app's generated type is `worker?: "ok" | "not working"`.
- **It says `not working` in the log,** as a warning, as it does for the database and Valkey: `readiness: the worker inside this process isn't working`. Never why: the worker's own lines say that, as before, each time it tries.

**When the worker inside works:**

- **It says that it works as the worker role does,** through `jobs.work`'s `on_beat`: once a run of Procrastinate's worker has connected and passed its check of the migration, then every 5 seconds until that run ends.
- **The probe says `not working`:**
    - before the worker's first beat;
    - from the moment the worker ends with an error, or finds that it can't work, until its next beat;
    - once 20 seconds have passed since its last beat (`alive.STALE_AFTER_S`), whatever it is doing.
- **The 20 seconds are the file check's rule,** the one by which the worker's container is healthy: four beats, so that a worker that is only slow misses one or two. Both ways of asking, the file and the probe, hold the worker to the same delay, and one rule is easier to hold than two that could come apart.
- **A worker that ends is not working at once,** without waiting for its beat to go stale. `jobs.work` beats as soon as a run starts, before Procrastinate's worker has done anything. A run that fails right after that, every time, is started again by `run_inside` every 10 seconds (`worker.TRIES_AGAIN_AFTER_S`), which is sooner than 20: each try would note a fresh beat, and the probe would say `ok` for ever while the log says every ten seconds that the worker ended with an error. In the worker's own container that can't happen: the process ends, and its file goes with the container's `/tmp`. So each time `run_inside` comes back from a run with an error, of either kind, it says so to its caller (`on_stopped`), before its pause, and also when it had been told to stop and ends; and the app forgets the last beat (`LastBeat.forget`). Not when the worker ends by the cancellation that tells it to stop: then the process is shutting down.
- **A stop that Procrastinate's worker made by itself raises nothing:** `jobs.work` starts it again a second later, inside the same call (`jobs.STARTS_AGAIN_AFTER_S`). There the last beat stays as it was, and the 20 seconds hold.
- **The last beat is kept in memory,** not in a file: `alive.LastBeat`, beside the file's rule in `actio.core.alive`, which still imports only the standard library. It is told the time, like everything in `actio.core`. The app notes each beat by the event loop's clock (`loop.time()`), which doesn't jump when the machine's clock is set, so it needs none of the file's guard against a time later than now.
- **`ACTIO_ALIVE_FILE` is honoured beside it, as before.** `actio.worker.run_inside` takes an `on_beat` of its caller's, and each beat calls it and touches the file, each whatever the other does: one that fails doesn't keep the other from being said, and what failed is said once in the log by `jobs.work`, as ever. The worker role is unchanged.

**How the probe asks** (`actio.app`): `_WorkerInside` notes the worker's beats, and forgets the last one when `run_inside` says that the worker stopped. Its lifespan puts on the app's state what the probe calls to ask, `worker_works`, or `None` in an app without a worker. The route only calls what it finds there, as it reads the process's resources: `actio.api` imports neither `actio.app`, a layer above it, nor `actio.worker`, beside it.

**What it changes around it:** `make dev` runs `--role all`, so `make smoke` there passes only once the worker inside works too. It retries for up to a minute while the stack starts, as before. `make up` runs the `api` role, whose answers didn't change.

**What the file doesn't have:** in an `all` process that is told of a file (`ACTIO_ALIVE_FILE`), a run that fails at once still touches it, each time it is tried: the file is not taken back when the worker ends, as the beat in memory is. No container runs `all`. One that does should ask `/api/readyz`, not the file.

**Measured** on the built process, `python -m actio --role all` against a scratch Postgres 17 and a scratch Valkey 9.1, the images pinned in `compose.yaml`:

- **started on a migrated database:** `/api/readyz` answered 0.61 seconds after the process was started, and said `ready`, with `"worker":"ok"`, at 0.63 seconds;
- **Postgres stopped under it:** the probe said `"database":"unreachable"` 0.2 seconds after the stop began, and `"worker":"not working"` 30.1 seconds after it, when Procrastinate's worker gave up and the worker ended with its error. In the first build, in which only a beat gone stale counted, that took 50.0 seconds: those 30, and the 20 of the last beat. With Postgres back, the probe said `ready` again 9.3 seconds after the database answered;
- **started on a database that was never migrated:** `"database":"migrations pending"` and `"worker":"not working"`. Migrated meanwhile, the probe said `ready` 8.4 seconds after the migration had ended: the worker had tried again by itself;
- **the `api` role on the same database:** `{"status":"ready","database":"ok","valkey":"ok"}`, as before;
- **a `make dev` session:** the probe said `{"status":"ready","database":"ok","valkey":"ok","worker":"ok"}`, and `make smoke` passed. After a Python file was saved, it said `ready` again 0.96 seconds later, the worker inside included.

### 2026-11-19: a second scheduled job, which gives up on stale turns

The worker has a second job, `give-up-on-stale-turns` (`worker.GIVE_UP`), one of the two that "Context" names as waiting for it: turns left behind by a process that died. Why and how is in [ADR 0022](0022-turns-that-run-apart-from-their-request.md), revision "the worker gives up on turns nobody looks at".

- **What it is:** every ten minutes, on the same schedule as the sweep, it gives up on a batch of turns that have been `running` and silent for a minute, in every conversation, as its own unit of work. It skips a turn another transaction holds, as the sweep skips its rows. A worker that starts runs it too, for the ten-minute mark that has just passed.
- **What it needs:** the database alone, like the sweep. The worker still reads neither the models file nor the plans file, and no job of its uses Valkey.
- **What the log gets:** Procrastinate's three lines at the info level for each run, that the job was deferred, that it starts, and that it ended: 432 more lines a day. And the job's own line, `gave up on stale turns: 2`, only when it gave up on any.
- **Cut off at a stop,** it stays `aborted`, as "Not done yet" says of any job. As for the sweep, the next run comes ten minutes later.

### 2026-11-19: the second job keeps what a dead turn had written

The job that gives up on stale turns now keeps each turn's draft as its answer ([ADR 0022](0022-turns-that-run-apart-from-their-request.md), revision "what a dead process had written is kept"). Two things in the revision above change with it:

- **It also skips a turn whose conversation another transaction holds,** not only a turn that is held itself, and leaves it for the next run. It still waits for no row.
- **It writes turn by turn,** where it wrote a batch in one statement: each kept answer is a message, written as any answer is. A run over 500 turns took 1.7 seconds, where it took 16 ms.

It still needs the database alone.

### 2026-11-20: a third job, which counts what the usage counters missed

The worker has a third job, `count-missed-usage` (`worker.COUNT_MISSED`). Every ten minutes it settles again, in Valkey, the usage that the API wrote to the ledger and couldn't tell the counters of, because Valkey didn't answer or the process died in between. Why and how is in [ADR 0020](0020-usage-limits-on-turns.md), revision "the counters catch up".

- **The first job that uses Valkey.** Until now the worker's container was told where Valkey is only because the settings require it (revision "the worker's container"), and no job of its used Valkey (revision of the second job). This one does. It reaches Valkey through the process's resources, which build the usage limits' engine once for every role (`Resources.quota`), so that the worker counts under the API's keys.
- **The containers didn't change.** The worker is still not ordered after Valkey in `compose.yaml`: a run that finds Valkey away stops at its first row, and leaves the rest for the next run, without failing. A run that finds nothing to count doesn't ask Valkey at all.
- **What it takes:** a batch of 100 at most, oldest first, by a statement of its own that holds only its own rows, skipping those another transaction holds, as the other two do. It never waits for a row's lock.
- **What it needs:** the database and the engine. It still reads neither the models file nor the plans file.
- **What the log gets:** Procrastinate's three lines for each run, as for the other two: 432 more a day. And the job's own line, only when it counted something, and one warning when Valkey didn't answer.

### 2026-11-20: the queue looks after what its workers leave behind

Three items of "Not done yet" are done: "jobs left `doing` by a worker that died", "jobs cut off at a stop stay `aborted`", and "cleaning out failed and aborted jobs". So is a fourth thing that workers leave behind, measured before this step: a worker that died stays on Procrastinate's list of workers until some worker next starts more than 30 seconds after its last beat.

Until now that did little harm: every job had a schedule, and its next run came ten minutes later. The next step brings the first job that is deferred for one thing only, a conversation's title. Such a job, once lost, is lost.

**A job may run more than once:**

- **After a stop that cut it off, or a worker that died with it in hand, it runs again from its start.** So every handler has to be safe to repeat. The three that were there are, since each takes a batch of what is left to do, and a batch that was done is not found again. The fourth, below, is too. `AGENTS.md` makes it a rule for whoever writes the next one.

**A job cut off at a stop runs again** (`actio.core.jobs`):

- **Through a retry strategy of ours, on every task the worker registers.** Procrastinate asks it of each job that didn't succeed. A job cut off at the end of the grace has had its task cancelled: for a cancellation the strategy answers "again, at once", and the job goes back to `todo`, for the next worker that takes it. For an error it answers nothing.
- **A job that fails stays failed,** as before. Retries after an error are still not done.

**A job left `doing` by a worker that died runs again,** by a scheduled job (below):

- **Through Procrastinate's own `get_stalled_jobs` and `retry_job`,** not SQL of our own: Procrastinate's heartbeats and its rules for them are its to change, and an upgrade brings them along. It finds the jobs `doing` under a worker that hasn't beaten for 30 seconds, and those `doing` under no worker, whose worker was taken off the list.
- **It leaves alone a job whose worker says that it is alive.** That is what the heartbeat is for. A test holds it, with two workers on one database. A worker that is alive and has said nothing for more than 30 seconds, as after the machine slept, is taken for dead like any other: see "With more than one worker", below.
- **It never runs itself again.** The job that looks is `doing` while it looks. If its own worker's heartbeat is old at that moment, as after the machine slept, Procrastinate finds it among the jobs that dead workers left. So the worker that looks first says that it is alive, through Procrastinate's `update_heartbeat`, and the job that looks is also left out by its id: another worker may have taken its worker off the list already, and then it is `doing` under none.
- **The same beat keeps it from taking its own worker off the list,** in the same moment. That was found in the build, not planned before it. A worker whose row is gone can't take a job any more: Procrastinate writes the worker's id into each job it takes, and that column points at the worker's row. Measured afterwards (below): the next job it goes to take fails on that foreign key, and the worker ends with an error, to be started again.
- **A job that changed between the look and the retry is skipped,** not an error: its worker wasn't dead after all, and ended it. Procrastinate's function refuses then, with an error of its own, which is caught for that job alone. The look goes on with the next. One such ending isn't refused: Procrastinate's `retry_job` takes a `failed` job too, so a job that its late worker ended as `failed` in that moment is put back, and runs again once (from reading its SQL, not measured). Keeping it failed would take SQL of our own, for a moment of a few milliseconds.

**How often, how long, and by which clock:**

- **Not for ever: `jobs.RUNS_AGAIN = 3`.** A job that has already been run again three times is not run again: cut off once more, it stays `aborted`; left by a dead worker once more, it ends as `failed`, and the log names it, as a warning. So a job runs four times at most. Procrastinate counts the times in the job's `attempts`. The reason: a job that takes its worker down each time would otherwise go round for ever, since the worker's container starts again when it fails.
- **Failed, aborted and cancelled jobs are deleted after seven days** (`worker.KEPT_FOR`), with their events, through Procrastinate's `delete_old_jobs`: long enough to be looked at after a long weekend. Procrastinate takes the time in whole hours. Jobs that succeeded are deleted at once, as before.
- **A dead worker is taken off the list** by the same job, after the jobs were seen to, with the same 30 seconds (`jobs.WORKER_DEAD_AFTER_S`, Procrastinate's own default), through its `prune_stalled_workers`.
- **One job, every ten minutes,** like the other three: `look-after-the-queue` (`worker.LOOK_AFTER_QUEUE`). A job that a dead worker left is run again within ten minutes and a half: thirty seconds for its worker to count as dead, and up to ten minutes for the next look. A worker that starts runs it too, for the ten-minute mark that has just passed.
- **Time here is the database's.** The handler takes no clock: the one exception to "time comes in as `now`". The queue's books are Procrastinate's, and they go by the database's clock: its heartbeats, and the times at which its jobs ended, are written with `NOW()`. A clock of ours would be compared with theirs.
- **A worker that is stopping stops saying that it is alive at once,** while the job in hand still has its grace: Procrastinate ends its heartbeat first. So one beat and the grace have to fit inside the 30 seconds, or another worker's look could take a stopping worker for dead, and put its job back while it is being finished. They do, 10 and 15 seconds, and a test holds it: `BEATS_EVERY_S + SHUTDOWN_GRACE_S < WORKER_DEAD_AFTER_S`. The worker is told the two numbers of the heartbeat by Actio now, where it took Procrastinate's defaults, which are the same.

**With more than one worker,** which no installation runs today, a worker that is alive can be taken for dead: one that was silent for more than 30 seconds, as after its machine slept. The review of this step listed what then follows, from Procrastinate's SQL:

- **Its job can run twice at once.** The look puts the job back, another worker takes it, and the late worker is still running it. Whichever ends it first decides how it ended, and the other's ending is refused, or lands on the second run. So "safe to repeat" also means safe beside itself. The three jobs that work on rows are: each takes its batch with `SKIP LOCKED`.
- **Two looks at once can put the same job back twice,** the second time after a worker has taken it again.
- **A worker whose row another took off the list** still has its job in hand, `doing` under no worker now, and every look puts that job back while it runs, up to the limit. Its own beat no longer protects it: it writes to a row that isn't there.
- **With one worker none of this can happen.** The look is that worker's only job in hand, and it says first that it is alive.
- **What would close it** is a look that puts a job back only if it still belongs to the worker it was found under, which takes SQL of our own, or a lock that keeps the same job from being taken twice. It goes with "more than one job at a time", below.

**How the job reaches the queue:**

- **Procrastinate's manager takes no connection of ours,** except to defer: the upkeep needs the running worker's own app, and its pool.
- **A job listed with `with_queue=True` is given the queue** (`jobs.Queue`), before the arguments it was deferred with. Procrastinate passes such a task its context, which holds the worker's app and the job that runs; `jobs.Queue` holds the two, and nothing of Procrastinate leaves the module. Its one method, `look_after(kept_for=…)`, does the three steps in order, and returns what it did (`jobs.Upkeep`).
- **The job is listed in `actio.worker.listed`** like the others, so that the list still shows every job a worker runs.
- **Its handler says what was done,** through `actio.worker`'s logger: at the info level, how many jobs were run again and how many dead workers were taken off the list, only when there was either; and a warning that names each job given up on. Deleting old jobs says nothing: Procrastinate doesn't say how many it deleted.
- **A look that the database fails raises an error of the module's own,** `jobs.UpkeepError`, as a deferral that fails raises `DeferError`: Procrastinate's errors stay inside too. The look then ends as a job that `failed`, and the next one comes ten minutes later. Each of its steps is written at once, so what was done stays done.
- **A job left `doing` by a force quit is run again the same way.** The revision "`--role all`, and a worker in `make dev`" says such a job stays `doing`: it does, until the next look, and no longer for ever.

**Measured by the supervisor before the step,** on Procrastinate 3.11.0 and the pinned Postgres, with a worker stopped as Actio stops it, by cancelling `run_worker_async` once:

- **a job cut off at a stop:** after the grace, Procrastinate asked the task's retry strategy with the `CancelledError`, for a job with `attempts` 0. A strategy that answered `RetryDecision()` made the job `todo` again at once, with `attempts` 1 and no worker; the log said "Aborted, to retry", and the next worker ran it to its end. One that answers `None` leaves it `aborted`;
- **a job that raised,** under the same strategy: it was asked with the `RuntimeError`, answered `None`, and the job stayed `failed`;
- **a worker killed with a job in hand** (SIGKILL): the job stayed `doing` under that worker's id. A worker started two seconds later didn't take the dead one off the list: Procrastinate does that only when a worker starts, and only for heartbeats older than 30 seconds, and a worker beats every 10. `get_stalled_jobs()` returned the job 30 seconds after the kill. `retry_job` then made it `todo`, with `attempts` 1, and the live worker ran it at once;
- **`prune_stalled_workers(30)`** took the dead worker off the list;
- **`delete_old_jobs(…, include_failed=True, include_cancelled=True, include_aborted=True)`** deleted the jobs that had been in a final state for longer than the hours it was given, with their events: with 1 hour a fresh failed job stayed, with 0 it went. It returns nothing;
- **all of these go by the database's clock,** `NOW()` in Procrastinate's SQL, as its heartbeats do. One thing doesn't, as the build found: the time a job that is put back is dated for (below);
- from reading the SQL, not measured: `retry_job` raises when the job is no longer `doing` or `failed`, and `finish_job(…, failed)` ends a `todo` or `doing` job as failed. Procrastinate cleans the table of its scheduled runs itself.

**Found in the build,** from reading Procrastinate's code: a job put back by `retry_job` is dated for now by the process's clock, not the database's (`scheduled_at`, from Python's `utcnow()`), and a worker takes it once the database's clock has reached that time. Where the two clocks differ, as between a machine and the virtual machine Docker runs on it, the job can wait for that difference. And a waiting worker isn't told of a job that was put back, as it is of a new one: it finds it at its next look, five seconds later at most. The tests that need a job run again wait for it with a worker that keeps looking, not one that returns once the queue looks empty.

**Measured,** on a MacBook Pro (M1 Pro, 16 GB), 2026-11-20, after the build:

- **With Actio's own worker, in real processes and real time,** on a Postgres from the pinned image. Nothing was set back by hand, and the look came every five seconds instead of every ten minutes:
    - **a worker killed with a job in hand:** the job stayed `doing` under it. A second worker's look put it back 31 seconds after the kill, and took the dead worker off the list in the same look. The second worker ran the job from its start, to its end 39 seconds after the kill: the job takes 8. Told to stop, that worker ended with status 0, and left the list itself;
    - **a job cut off at a stop:** SIGTERM, with a job of 40 seconds in hand. The process ended with status 0 after 15.1 seconds, and the job was `todo`, with `attempts` 1 and no worker;
    - **a live worker whose row was deleted from the list:** the next job it went to take failed on the foreign key from a job to its worker, the process ended with status 1, and the job was still `todo`. So a worker that is taken off the list by mistake doesn't work on under a row that isn't there: it ends, and is started again as any worker that failed.
- **In containers,** the stack of the end-to-end tests, after its 33 tests had passed. The worker's process was killed from inside its container, as a crash is. Docker started the container again within a second, and it was healthy six seconds later: two workers stood on the queue's list, the dead one with a beat 16 seconds old. At the next ten-minute mark, two minutes later, the new worker's log said `jobs left by workers that died, run again: 0, dead workers taken off the list: 1`, and one worker was left on the list. The look itself took 4 ms.
- **Thirty-seven mistakes were planted,** one at a time, in a scratch copy of the tree: in the strategy (again for an error too, without a limit, the limit moved by one, never again, left off either kind of task); in the look (no beat first, its own job run again, the limit left out or moved, a job given up on deleted or ended as `aborted`, a job that ended meanwhile stopping the look, every error of the database taken for such a job, dead workers left on the list, each kind of old job left, days taken for hours, the 30 seconds made longer or shorter, an error of Procrastinate's let out, a beat or a grace that no longer fits); and in the worker (the job not listed, not given the queue, without a schedule, keeping nothing or too long, what it says). Each fails a test. Four failed nothing at first, and got one: the strategy left off the task that looks; any error of the database taken for a job that ended meanwhile; and a look that only ran a job again, or only took a worker off the list, saying nothing.

**What it costs:**

- **Three more lines of Procrastinate's in the log every ten minutes,** 432 a day, as each job has.
- **A job may run more than once,** and every handler has to be written for that.

**Still not done:**

- **retries after an error.** A job that fails stays failed. Whoever adds them: they go into the same retry strategy, and `attempts` counts every time a job was run again, after a stop as after an error;
- **a queueing lock,** which keeps a second job of a kind out while one waits;
- **more than one job at a time.**

### 2026-11-20: a fifth job, the first that a change defers, writes a conversation's title

The worker has a fifth job, `write-title` (`titles.WRITE_TITLE`), and the first without a schedule: the first turn of a conversation that completes defers it, in the transaction that records the turn, once for that conversation, when the models file names a model for titles. "Context" named it among the work waiting for the worker. Why and how is in [ADR 0025](0025-titles-written-by-a-model.md).

- **The first job deferred for one thing only,** as the revision above foresaw: once lost, it is lost. It is safe to repeat and to run beside itself, as that revision requires: a run that finds the conversation named already asks the model nothing, and the write asks again under the conversation's row lock.
- **The first job that asks a model.** The worker has the models on offer now: `actio.worker.listed` takes the catalog, beside the resources.
    - **`--role worker`** reads the models file before it starts, as the API does, so that a mistake in it is one line and status 2, and builds a catalog of its own beside its resources. A key's variable that isn't set, which shows only when the catalog is built, ends it the same way. It still doesn't read the plans file. The decision's "It doesn't read the models file or the plans file: no job needs them yet" no longer holds for the models file.
    - **`--role all`** gives its worker the app's own catalog, so that a title waits at the same gate as the answers of that process. In the worker role the gate is the worker's own, and the API's doesn't know of a title being written ([ADR 0025](0025-titles-written-by-a-model.md) says what that costs).
- **Its container reaches the models' servers.** Measured by the supervisor before the step, from the containers of the stack people run: from the worker's, on `backend` alone, which is internal, `host.docker.internal` didn't even resolve; from the API's, which is on `edge` too, Ollama answered. So, in `compose.yaml`:
    - **a third network, `models`,** not internal, the way out to the models' servers, with the API and the worker on it. The worker stays off `edge` and publishes no port: nothing reaches it still, and `models` is its one way out;
    - **the worker gets what the API has for the models,** through anchors the two share, so that they can't drift apart: the folder `./config`, mounted read-only where the image expects the models file; `ACTIO_OLLAMA_URL`, with the same default; and the line that teaches Linux the name `host.docker.internal`;
    - **in the end-to-end stack `models` stays internal,** as it was: the fake needs no way out, so there the worker reaches the fake and nothing else. That worker reads the same folder as that API, `deploy/e2e/config`, whose models file names no model for titles.
- **The containers' rules that changed, on purpose** (`deploy/tests/test_compose_policy.py`), as [ADR 0005](0005-container-images.md) asks of a rule that changes. The reason for each is the one above: a conversation's title is written by a model, and the model's server is outside the stack.
    - "The worker cannot be reached or reach out" became: it can't be reached, with no port and off `edge`, and its one way out is `models`, which only the API and the worker are on, with `backend` internal as before;
    - new: the worker reads the models file from the same read-only mount as the API, and finds Ollama where the API does, in both stacks;
    - "the fake model is reached by the API alone" became "by the API and the worker alone";
    - "the end-to-end stack's worker is the one of `compose.yaml`" became "the one of `compose.yaml` but for its config folder", as for the API.
- **A mistake in the models file stops the worker too.** In the stack its container then ends with status 2, and Docker starts it again, and again, until the file is fixed (`restart: on-failure`). The API doesn't start at all meanwhile, so `make up` fails either way.
- **What the log gets:** Procrastinate's lines for each title, as for every job, with the conversation's id as its argument, and a warning of the job's own when no title came. Never the question or the title.

"Still not done" is as in the revision above: retries after an error, a queueing lock, more than one job at a time.

### 2026-11-20: a job is run again after an error its listing names

"Retries after an error", in "Still not done", is done, for each job that asks for them. The first to ask is the fifth job: a title that a model's server couldn't give for now, because it was restarting, or busy with a long answer, was lost for good ([ADR 0025](0025-titles-written-by-a-model.md), revision of the same day). Two sentences no longer hold as they were written: the decision's "A job that failed stays, as `failed`, and is not tried again", and, in the revision "the queue looks after what its workers leave behind", "A job that fails stays failed, as before". Both still hold for an error that the job's listing doesn't name.

**A job's listing can say which errors of its handler are worth another try** (`actio.core.jobs`):

- **Per job,** in the line that lists it: `jobs.Job(name, run, again=jobs.Again(errors=(…,), waits=(…,)))`. Only the handler knows which of its errors may be gone at a later try. Another, such as a database that refused a statement, or a mistake in the code, would come back each time.
- **`errors`:** the kinds of error worth another try. An error of a kind derived from one of them counts too.
- **`waits`:** how long the job waits before each time it is run again after such an error: the first before the first time, the second before the second, and so on. The last also serves any time after it.
- **Refused when it is made,** with a `ValueError` or a `TypeError`, so when the worker's list is built: no error, no wait, a wait below zero or one that is no `timedelta`, or an error that is no kind of `Exception`, a cancellation among them, since a cancellation is how a stop cuts a job off, and has its own rule. Tuples only: the strategy's question of an error, `isinstance`, takes a tuple of kinds alone, and a list would fail only when an error came, inside Procrastinate's handling of it.
- **Without a listing, a job is as it was:** an error fails it at once. So is the look after the queue, and every scheduled job.

**One count, one limit, whatever the reason.** A job is run again `jobs.RUNS_AGAIN` (3) times at most in all: after a stop cut it off, after a worker that died left it, or after an error its listing names. So it still runs four times at most.

- **Procrastinate keeps that count,** in the job's `attempts`, which goes up by one each time a job is put back, for any reason. The waits are read by it: a job already run again once after a stop waits the second wait after its first error.
- **One count, not two,** because a job whose error comes back and whose worker dies on it would otherwise run as often as both limits allow together, and a second count would need a column of our own beside Procrastinate's.

**The rule is the one retry strategy's,** which Procrastinate asks of every job that didn't succeed (`_RunAgain`, one for each job a worker lists, with that job's listing), in this order:

1. **run again `RUNS_AGAIN` times already:** no more. Cut off, the job stays `aborted`; failed, it stays `failed`;
2. **cut off at a stop:** again at once, as before;
3. **an error the listing names:** again once the wait for that time is over;
4. **any other error:** no, and the job stays `failed`.

**The time is Procrastinate's to read.** The strategy hands Procrastinate the wait, exactly (`retry_in`), and Procrastinate dates the job by the worker process's clock, as it does a job put back at once after a stop. Nothing in `actio.core` reads the clock, and `Queue.look_after` didn't change. Two things change a wait:

- **the database's clock decides when the job is due,** and a difference between it and the worker's clock makes the wait that much longer, or shorter;
- **a worker that waits for jobs isn't told of a job put back,** as it is of a new one: it finds it at its next look, every five seconds. For a worker with nothing else in hand, a wait of 30 seconds is 30 to 35.

**A job that is run again after an error is no error yet: its line in the log carries no trace.**

- **What Procrastinate writes** for such a job, at the info level: `Job write-title[12](conversation_id='…') ended with status: Error, to retry, lasted 0.004 s`, with the exception's trace, 25 lines at each try. The trace of an error that the listing names says nothing that the handler's own line didn't: the handler says what went wrong before it raises, as the title's job does (`no title, its model failed (ProviderError)`), since Procrastinate's line doesn't name the error.
- **So `actio.core.jobs` takes the trace off that record, and off no other:** by Procrastinate's own name for it, `action` `job_error_retry`, with a filter on the logger Procrastinate writes it through, `procrastinate.worker`. Only this module knows Procrastinate, its records included. The text of the line stays Procrastinate's.
- **On the logger, not on a handler,** so that it holds however the process set its log up: the worker role with its one handler, and `--role all` with uvicorn's and one more. It is put there when a worker starts, once for the process, however many times a worker starts in it.
- **Every other line keeps its trace:** the last try's, which fails the job, an error as for any job that failed; that of a job failed by an error its listing doesn't name; and that of a job cut off at a stop ("Aborted, to retry"), which says where the job was when it was cut off.

**An error worth another try is raised without its cause** (`from None`), and its text holds kinds and ids alone, never what people wrote. The last try's trace is in the log, and a cause's text would be printed there (measured, below): the title's job raises its error from a model's error, which may quote the question.

**For whoever lists the next job:** list an error of the handler's own as worth another try only when it may be gone at a later try; say in one line of the log what happened before raising it; raise it without its cause, its text without people's words; and remember that the job may already have been run again after a stop, and so have fewer tries left. `AGENTS.md` says the same.

**How the tests hold it** (`test_jobs.py`, on the real queue, with jobs and errors of their own): a job put back is dated a minute or more ahead, never due by itself while a test runs. A test reads in how long it is due (`queue_books.due()`), by the clock that dated it, which is the test's own process's, where its worker runs: so the assertion doesn't depend on how far the database's clock is from it. Then it ends the wait through the database (`queue_books.wait_is_over()`), and runs a worker again: none sleeps through a wait, nor waits for a worker's look. The title's tests do the same with the title's own waits, the shortest of them half a minute.

**Measured by the supervisor before the step,** on Procrastinate 3.11.0 and the pinned Postgres, with a retry strategy of a spike's own:

- **a job that raised, under a strategy that answered `RetryDecision(retry_in={"seconds": 3})`:** its row went back to `todo` with `attempts` 1, dated three seconds ahead, by the worker process's clock (`scheduled_at`, from Procrastinate's `utils.utcnow()`). The strategy was asked with the exception, and with `job.attempts` 0, then 1, then 2;
- **a waiting worker isn't told of a job that was put back:** it took it at its next look, which comes every five seconds. Dated three seconds ahead, the job ran again 5.0 seconds after its first try; dated six seconds ahead, 10 seconds after its second;
- **when the strategy answered `None`,** the job ended `failed`, and its `attempts` read one more: Procrastinate counts the last try as it ends the job;
- **the log:** for a job to be run again, `procrastinate.worker` wrote at the info level "… ended with status: Error, to retry, lasted … s", with `action` `job_error_retry` and the exception as `exc_info`: a trace of 25 lines. For the last try, at the error level, "… ended with status: Error", with `action` `job_error` and the trace. `_log_job_outcome`, in Procrastinate's `worker.py`, writes through the module's own logger, `procrastinate.worker`;
- **a trace holds the words of whatever its exception was raised from:** an error raised `from error` printed the cause's text in the log ("RuntimeError: the model said: WORDS-OF-A-PERSON", 32 lines). The same error raised `from None` didn't;
- **a worker that returns once the queue is idle** (`until_idle`, as the tests run one) doesn't wait for a job dated for later: it returned after 0.2 seconds, and left the job `todo`, with `attempts` 1;
- **one count:** a job run again after an error, with `attempts` 1, and then cut off at a stop, went back with `attempts` 2;
- **`RetryDecision(retry_in=…)` takes Procrastinate's `TimeDeltaParams`,** whose members are typed as whole numbers. `retry_at` would need the clock, which nothing in `actio.core` reads. So the strategy passes the wait as one whole number, of microseconds, which is the `timedelta` exactly.

**Measured after the build,** on a MacBook Pro (M1 Pro, 16 GB), 2026-11-20, with the one job that is listed so, the title's, whose waits are 30 seconds, 2 minutes and 10 minutes ([ADR 0025](0025-titles-written-by-a-model.md) has what this meant for the titles):

- **With Actio's own processes,** `actio --role api` and `actio --role worker`, on a Postgres and a Valkey of their own. The worker was told that the model's server is at an address where nothing listens, so each of its tries failed at once, while the API reached the server:
    - **the four tries began 0.0, 30.1, 153.1 and 758.1 seconds after the first,** by the times in the worker's log: 30.1, 123.0 and 605.0 seconds after the try before. Each is the listing's wait, and up to five seconds for the worker's next look;
    - **after the fourth the job was `failed`,** with `attempts` 4, and nothing ran it again;
    - **the log of the four:** four warnings of the title's own, three lines "… ended with status: Error, to retry, lasted 0.025 s" at the info level, each one line, and one "… ended with status: Error" at the error level, with the one trace of the run: 93 lines in all, where a trace at each try would have added 75.
- **In `actio --role all`,** where uvicorn sets the log up: the line of a job put back was one line too, without a trace. The same with `--reload`, as `make dev` runs it, where the server is a process of the reloader's and sets its log up by itself.
- **In the worker's container,** in the stack of `make up`: the same line, and the job taken again 34.4 seconds after it was put back for 30.
- **Fifty mistakes were planted,** one at a time, in a scratch copy of the tree: in the strategy (a fifth run, a limit that holds for one reason only, every error tried again or none, the wrong wait, a wait in the wrong unit, a stop that waits, a strategy left off a task), in the listing's refusals, in the filter (never put on, on every record, on the last try's or the stop's line too, the line left out, one more at each start), in what the title's job takes for worth another try, in its waits and in its listing. Each fails a test. Two failed nothing at first:
    - **a wait that lost its days, or what is under a second:** the strategy handed Procrastinate the wait as three numbers, and no test had a wait that needed the first or the last. It hands over one number now, and the tests' waits of minutes hold it;
    - **a wait that is no `timedelta`:** with that refusal taken out, the next one raised an error of the same kind, for another reason. The test now holds each refusal's own words.

**What it costs:**

- **a job whose error comes back each time runs four times,** spread over its waits, and holds the worker for each try: the worker runs one job at a time;
- **a job that fails at its last try is an error in the log,** with its trace, and a row kept for seven days (`worker.KEPT_FOR`), as any job that failed;
- **the lines of the tries before it name no error:** whoever reads the log finds the kind in the handler's own line beside it.

**Still not done:**

- **a queueing lock,** which keeps a second job of a kind out while one waits;
- **more than one job at a time;**
- **a provider's own "retry after"** (`RateLimitedError.retry_after_s`): a job's waits are the ones its listing gives.

### 2026-11-22: a fifth scheduled job, every hour, which compares the usage counters with the ledger

The worker has a sixth job, `compare-usage` (`worker.COMPARE_USAGE`), and the fifth with a schedule: it compares the usage counters in Valkey with the ledger in Postgres, and says in its log how far apart they are. Why and how is in [ADR 0020](0020-usage-limits-on-turns.md), revision "the counters are compared with the ledger".

- **The first that isn't on the ten-minute marks:** `5 * * * *`, every hour at five past. Not at the moment of the other four, and five minutes after the job that counts what the counters missed.
- **Every hour, not every day,** as [ADR 0006](0006-usage-limits-engine.md) had it ("a nightly job"). A worker that starts runs the scheduled time that has just passed only if it was less than ten minutes ago, as "Schedules" says above: read in Procrastinate 3.11.0's own source, `MAX_DELAY = 60 * 10` in its `periodic.py`, and the docstring of `periodic_defaults` in its `app.py`, a time overdue by more is left for the next one. So a daily job's time, missed while the worker was down, is lost until the next day, and on a machine that is off at night, every day. An hourly one loses an hour.
- **A worker that starts** between five and a quarter past runs it at once, for the time that has just passed, as it runs the other four for their last ten-minute mark.
- **What it needs:** the database and the engine, as the job that counts what was missed. It still reads no plans file: it compares the windows the counters have stored.
- **It writes nothing,** so it is safe to repeat and to run beside itself, as every handler has to be (revision "the queue looks after what its workers leave behind").
- **What the log gets:** Procrastinate's three lines for each run, 72 a day, and the job's own: one line at every run at least, so that a run can be told from none, and warnings when something is apart.
