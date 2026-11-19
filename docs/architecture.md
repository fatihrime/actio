# Architecture

## Today

`make up` runs six containers, after a seventh, `migrate`, has brought the database schema up to date and exited. Five serve one address, <http://localhost:8080>; the sixth, the worker, runs the background jobs, and nothing reaches it:

```mermaid
flowchart LR
    browser([Browser]) -->|"localhost:8080"| caddy
    subgraph edge ["edge network"]
        caddy["Caddy"]
        web["Web app<br>Next.js"]
        api["API<br>FastAPI"]
    end
    subgraph backend ["backend network: internal, no route out"]
        migrate["migrate<br>runs once, first"]
        worker["Worker<br>background jobs"]
        postgres[("Postgres 17<br>with pgvector")]
        valkey[("Valkey 9")]
    end
    ollama(["Ollama<br>on your machine, not in a container"])
    caddy -->|"/api/* and /v1/*"| api
    caddy -->|"everything else"| web
    migrate -->|"schema changes"| postgres
    api --> postgres
    api --> valkey
    worker --> postgres
    api -->|"host.docker.internal"| ollama
```

- **Caddy** is the only entry point, and the only container with a port on your machine. It sends `/api/*` and `/v1/*` to the API, and every other path to the web app. It passes streamed responses through at once, which the chat's token-by-token replies need. A request to the API with a body over 2 MB it refuses itself, with a 413 in the API's own error format: a body that says its length before any of it is passed on, one sent in chunks once the limit is reached ([ADR 0023](adr/0023-answers-that-wait-their-turn.md)). Its configuration, `deploy/caddy/Caddyfile`, is built into its image, as the API's code is into the API's: after a change to it, `make up` or `make dev` again applies it, and a mistake in it stops the build while the running proxy goes on as it was.
- **The API** is one Python application, `actio`, built on FastAPI. It runs as several processes from the same code, chosen with `--role`: `api` serves HTTP, `migrate` applies database migrations and exits ([ADR 0008](adr/0008-postgres-through-sqlalchemy-and-psycopg.md)), `worker` runs background jobs (below), and `all` serves HTTP with the worker inside the same process, for the smallest installations and for `make dev`. Today the API serves its interactive docs, two probes, guest sessions and accounts:
    - `/api/healthz` answers whenever the process is alive;
    - `/api/readyz` answers 200 only when the database is reachable and migrated and Valkey answers. Otherwise it answers 503 and names what is missing;
    - `POST /api/session/guest` starts a session as a new guest and sets its cookie, `GET /api/me` says who the cookie belongs to, and `DELETE /api/session` ends it ([ADR 0009](adr/0009-sessions-in-the-database.md));
    - `POST /api/account` turns a guest into a lasting account with an email address and a password, `POST /api/session` logs in, `PUT /api/account/password` changes the password, and `DELETE /api/sessions` logs out of every browser ([ADR 0010](adr/0010-accounts-with-a-password.md));
    - new guests, sign-ups and password attempts are rate-limited. Past a limit, the answer is 429 with a `Retry-After` header ([ADR 0011](adr/0011-rate-limits-as-token-buckets-in-valkey.md));
    - `GET /api/conversations` lists your conversations a page at a time, and `GET /api/conversations/{id}` returns one with its messages. `PATCH` renames it, `DELETE` deletes it, and `PUT …/branch` shows another version of a question or an answer ([ADR 0012](adr/0012-conversations-as-a-tree-of-messages.md));
    - `POST /api/conversations/{id}/turns` puts a question to a model and streams the answer back, piece by piece, in the format the AI SDK reads. The first question creates the conversation. The same route rewords an earlier question, or asks for another answer. Each turn is recorded: which model, how it ended, what it cost in tokens and time ([ADR 0014](adr/0014-turns-streamed-in-the-ai-sdks-format.md));
    - a turn runs apart from the request that started it, in a task of its own. The request only reads along, and closing it ends nothing. `GET …/turns/{turn_id}/stream` reads along again from the answer's first event, and `POST …/turns/{turn_id}/cancel` stops the turn. Told to stop, the API gives the answers that are being written a moment, then records what is unfinished as interrupted ([ADR 0022](adr/0022-turns-that-run-apart-from-their-request.md));
    - `GET /api/models` lists the models on offer, from `config/models.toml`, and says whether each can answer right now: `ready`, `missing` from its server, or its server `unreachable` ([ADR 0013](adr/0013-the-models-on-offer-are-a-file.md));
    - a model's server writes only so many answers at once: Ollama one, unless the models file says otherwise. A turn that comes while its server is busy waits its turn inside the API, at a gate in front of the server, running like any other, and its model is asked once there is room ([ADR 0023](adr/0023-answers-that-wait-their-turn.md));
    - a turn costs credits, and an account has only so many per session and per week, from `config/plans.toml`. The most a turn can cost is held before a model is asked; a turn that doesn't fit is answered 429, with the moment the limit starts over. `GET /api/usage` says how much is used ([ADR 0020](adr/0020-usage-limits-on-turns.md));
    - a plan also says how many answers one account may have being written at once, and one more is answered 429, before anything is kept. An account reads along with an answer in six places at once at most: one more join is answered 429 too ([ADR 0023](adr/0023-answers-that-wait-their-turn.md)).
- **The worker** is the same application, run with `--role worker`, in a container of its own from the API's image. `make dev` runs it inside the API's process instead, with `--role all`, and it reloads with the API ([ADR 0024](adr/0024-background-jobs-in-a-queue-in-postgres.md)).
    - It runs background jobs from a queue in Postgres, Procrastinate's, one at a time. A job is deferred in the transaction of the change it follows, so the two are committed together, or not at all.
    - A job can also have a schedule. Today there is one job: every ten minutes, the worker deletes expired guests and sessions, which the API otherwise deletes only after a new guest arrives.
    - It starts after the migration, and checks for itself that the database is at the migration its code expects. When it isn't, the worker takes no job, and ends with one line that says so.
    - Told to stop, by SIGTERM or Ctrl-C, it lets the job in hand finish for up to 15 seconds, and ends well, whenever it is told. A second signal changes nothing. Docker gives it 25 seconds.
    - It lives through a restart of the database: it stops, starts again a second later, and waits for the database as when it first started. When it can't go on, as 30 seconds after it lost its database, it ends, and Docker starts it again.
    - It has no port for Docker to probe. While it works it touches a file every five seconds, and its health check says whether it did so lately.
    - Inside the API's process, it never ends that process: when it can't work, it says why in the log and tries again ten seconds later. It is told to stop at the same moment as the answers being written.
- **Ollama** runs the local models. It runs on your machine itself, where it can use the graphics processor, and the API reaches it from its container.
- **The web app** is a Next.js application: the chat ([ADR 0015](adr/0015-the-web-chat-calls-the-api-from-the-browser.md)).
    - **Its server sends only the frame of the page.** Everything of yours is fetched by the browser, straight from the API, with the session cookie. Nothing but the API handles a session.
    - **A first visit starts a guest session,** so you can ask at once. `/` is a new conversation, and `/c/<id>` a stored one.
    - **The list of your conversations is beside the chat,** to open, rename and delete them. The page reads it from the API again after every turn, and never rearranges it by itself ([ADR 0018](adr/0018-the-list-of-conversations-is-the-servers.md)).
    - **Signing up, logging in and the account are places in the same screen,** at `/signup`, `/login` and `/account`. What the page has read for one session is dropped when that session ends, so the next one starts with nothing of it ([ADR 0019](adr/0019-accounts-in-the-one-screen.md)).
    - **An answer streams into the page** through the AI SDK's `useChat`. You can stop it, try a failed one again, reword a question that got no answer, and have the model show its reasoning.
    - **Only Stop ends an answer:** the page then asks the API to stop the turn. Leaving the conversation, or the page, ends the reading and nothing else.
    - **An answer that is still being written is read along with** wherever the conversation is opened: after a reload, on coming back to it, in a second window. The page joins the turn's stream, from the answer's first word, and Stop works there too. When the stream can't be read, the page says that the answer is on its way, and looks until it is done ([ADR 0022](adr/0022-turns-that-run-apart-from-their-request.md)).
    - **The server is the truth.** When an answer ends any other way than well, the page reads the conversation again and shows what is stored.
    - **The usage limits are the server's to count.** A meter under the list shows what the API said was used, read again after every turn. That a limit is reached, the page learns from a question being refused; it then says how long to wait, sends nothing meanwhile, and offers a model that costs nothing where there is one ([ADR 0021](adr/0021-usage-limits-in-the-page.md)).
    - **An answer is shown as the markdown it is written in:** headings, tables, coloured code, formulas and diagrams. It is a model's text, so it can do nothing else: HTML in it is shown as text, no picture is fetched, and a link opens only after you have seen its address ([ADR 0016](adr/0016-answers-shown-as-markdown.md)).
    - **Its types for the API are generated** from the API's recorded description, `apps/api/openapi.json`, so a change to the API that the page doesn't follow fails to compile.
- **Postgres and Valkey** sit on an internal network. Only the API, the migration job and the worker can reach them, and they can't reach the internet. The API connects to both, the worker to Postgres.
    - Postgres holds six tables: accounts, their sessions, conversations, the messages of each conversation, which form a tree, the turns in which a model answered, and the usage ledger: what each turn used, in rows that are only ever added. Beside them is the queue of background jobs, in Procrastinate's own tables.
    - Valkey holds what is counted fast: the rate limits' counters, one number per client address or email address that has made an attempt lately, under a hashed name; and each account's usage counters, with the credits held for answers being written.
    - Valkey also holds what each running turn streams, in a stream of its own, so that an answer can be read by more than one reader and from its beginning. A stream is deleted a minute after its turn has ended.
    - Valkey keeps nothing on disk. After a restart the rate limits start full, and an account's usage counters are rebuilt from the ledger the next time they are needed. An answer that was being written is finished unread, and stored as ever.

Inside the API, the code is split into layers that only import downward, and a tool enforces it:

| Layer | Holds |
|---|---|
| `actio.api` | The HTTP interface: routes, the session cookie, the check against requests from other sites, which attempts are rate-limited, the error format. The only layer that knows the web framework |
| `actio.worker` | The worker: every background job it runs, with what each needs, and their schedules. Beside `actio.api`: neither imports the other |
| `actio.chat` | Conversations, their tree of messages, and turns: starting one, following a model's answer as it streams, recording how it ended and what it cost. Running turns apart from their requests, the stream of what each writes, and how many answers and readers one account has at once. Tables and rules, with no HTTP |
| `actio.catalog` | The models on offer: reading `config/models.toml`, asking a model's server about it, and the gate where answers wait while that server is busy |
| `actio.identity` | Accounts and sessions: tables and rules, with no HTTP |
| `actio.usage` | Usage limits: reading `config/plans.toml`, holding credits before a request and counting them after it through the [engine](reference/quota.md), and the ledger |
| `actio.core` | Settings, ids, the database connection and its migrations, the queue of background jobs, the file the worker touches while it works and the health check that reads it, the rate limiter, reading a file in `config/`, hearing the signal that stops the process |

| Path | Goes to |
|---|---|
| `/api/*` | The API: Actio's own endpoints, such as `/api/healthz` and `/api/docs` |
| `/v1/*` | The API: the gateway compatible with OpenAI and Anthropic (planned) |
| Everything else | The web app |

## Two ways to run it

- **`make up`** runs everything in containers, shaped like production. See [ADR 0005](adr/0005-container-images.md).
- **`make dev`** runs only Postgres, Valkey and Caddy in containers. The API and the web app run natively with hot reload, and Caddy reaches them through Docker Desktop. The worker runs inside the API's process, and reloads with it. See [ADR 0004](adr/0004-local-development-stack.md).

## Security rules

The containers follow the same rules in both modes. Tests in `deploy/tests/` check them on every change:

- only Caddy publishes a port, and only on 127.0.0.1;
- every image is pinned by digest, including the base images of the images Actio builds;
- the data services sit only on the internal network;
- every container runs as a non-root user, on a read-only filesystem, without Linux capabilities, with a memory limit and a health check;
- only the worker is started again by Docker, and only when it failed.

## Planned

The next phases add:

- **more providers:** cloud models behind a router with fallbacks, and a way to choose between models. In tests, [fakellm](reference/fakellm.md) stands in for all of them. The router will also say when an answer waits for its server, and how many are ahead of it.

## Repository layout

| Path | Contents |
|---|---|
| `apps/api` | The Python application: the API and the worker |
| `apps/web` | The Next.js web app: the chat's pages and components, its calls to the API, and their tests |
| `packages/fakellm` | A fake LLM server for tests, scripted by directives in the prompt |
| `packages/llm` | Model clients: a neutral chat format, adapters for Ollama and for OpenAI-compatible servers, a guess at a text's tokens, and the `actio-llm` command |
| `packages/quota` | The usage-limits engine: Lua scripts for Valkey, and the Python reference model they're tested against |
| `compose.yaml`, `compose.dev.yaml` | The stack for `make up`, and the overlay for `make dev` |
| `config/` | What an installation changes without touching code. Today: `models.toml`, the models on offer, and `plans.toml`, what they cost and how much each kind of account may use |
| `deploy/` | Caddy's configuration and the image it is built into, and the tests for the containers' rules |
| `docs/` | This site, including the decision records |
| `scripts/` | Developer scripts: tool checks, the denylist hook, the smoke test |
