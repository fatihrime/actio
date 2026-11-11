# Getting started

There are two ways to run Actio on your machine. Both serve it at <http://localhost:8080>.

| | Just run it | Develop it |
|---|---|---|
| Commands | `make up` | `make bootstrap`, then `make dev` |
| In containers | Everything | Postgres, Valkey and Caddy |
| Native, with hot reload | Nothing | The API and the web app |
| Works on | macOS and Linux | macOS with Docker Desktop |
| You need | Docker, `make`, `openssl` | Also git, uv and pnpm 12 |

## Just run it

```sh
git clone https://github.com/fatihrime/actio.git && cd actio
make up
```

The first run builds the images, which takes a few minutes; later runs take seconds. When `make up` returns, every container reports healthy. Then open:

- <http://localhost:8080>: the web app;
- <http://localhost:8080/api/docs>: the API's interactive documentation;
- <http://localhost:8080/api/healthz>: the API's health check;
- <http://localhost:8080/api/readyz>: whether the API can do its work. It says `ready` when the database is reachable and migrated and Valkey answers, and otherwise names what is missing.

`make smoke` checks all three from the command line. `make down` stops everything.

## Develop it

```sh
make bootstrap   # checks your tools, installs everything, creates .env
make dev         # starts the stack; Ctrl-C stops the API and the web app
```

`make doctor` lists any missing tools, with install hints. You don't need Node.js itself: pnpm installs the version the project pins.

In this mode, saving a Python file restarts the API, and the browser picks up changes to the web app by itself. `make dev` first stops any containers that `make up` started, so you can switch between the two modes freely. Postgres and Valkey keep their data in Docker volumes in both modes, so it survives restarts.

## Talk to a local model

The chat interface isn't built yet, but the client it will use is. With the development setup above and [Ollama](https://ollama.com/download) 0.40 or newer running:

```sh
ollama pull granite4:micro     # 2.1 GB
uv run actio-llm chat granite4:micro "Say hello in five words." --context 4096
```

The answer streams to your terminal, followed by its token counts and speed. `uv run actio-llm models` lists the models on your machine and what each can do. See the [model clients reference](reference/llm.md).

## Commands

| Command | What it does |
|---|---|
| `make up` | Build and run the whole stack in containers |
| `make dev` | Run the stack with the API and the web app native |
| `make smoke` | Check a running stack: the web page, the API's health and its docs |
| `make down` | Stop all containers; their data is kept |
| `make check` | Run everything CI runs: static checks, tests and builds |
| `make docs` | Preview this documentation site at <http://localhost:8001> |

`make help` lists every command.

## Configuration

`make` creates a `.env` file with random passwords the first time it needs one. The file is git-ignored and readable only by you. To change a host port, uncomment it in `.env`:

```sh
ACTIO_HTTP_PORT=8080      # the web app and the API, through Caddy
ACTIO_POSTGRES_PORT=5432  # make dev only
ACTIO_VALKEY_PORT=6379    # make dev only
```

If people reach Actio under another address than `http://localhost:8080`, say so in `.env` too:

```sh
ACTIO_PUBLIC_URL=https://chat.example.com
```

The API accepts changes only from pages at that address, and over HTTPS it marks the session cookie `Secure`. See [ADR 0009](adr/0009-sessions-in-the-database.md).

New guests, sign-ups and password attempts are rate-limited. A limit is written as a count per `second`, `minute`, `hour` or `day`: `30/hour` allows 30 at once, then one more every two minutes. These are the defaults:

```sh
ACTIO_LIMIT_GUESTS_PER_ADDRESS=30/hour   # new guests from one address
ACTIO_LIMIT_SIGNUPS_PER_ADDRESS=10/hour  # sign-up attempts from one address
ACTIO_LIMIT_LOGINS_PER_ADDRESS=30/hour   # login attempts from one address
ACTIO_LIMIT_LOGINS_PER_EMAIL=10/hour     # password attempts for one account, from anywhere
```

Everyone behind one router shares an address. With Docker Desktop, every browser on your machine does. Raise the first limit for a classroom; lower it, to `3/day` say, for a public demo. See [ADR 0011](adr/0011-rate-limits-as-token-buckets-in-valkey.md).

## Troubleshooting

**"Address already in use"**
:   Another program holds a port Actio needs. That's 8080 for both modes, or `ACTIO_HTTP_PORT` if you changed it. `make dev` also needs 3000 (the web app) and 8000 (the API). Stop the other program, or change `ACTIO_HTTP_PORT`.

**`/api/readyz` says `migrations pending` or `unreachable`**
:   The API is running but can't do its work yet. `migrations pending`: the database is older than the code; `make up` and `make dev` both apply the missing migrations when you run them again. `unreachable`: Postgres or Valkey isn't running or refuses the password; `docker compose ps` shows their state, and the API's log says more.

**"Too many new guests from your network", or another 429**
:   A rate limit was reached. The answer says how long to wait, and its `Retry-After` header says it in seconds. On your own machine, raise the limit in `.env` (see [Configuration](#configuration)) and run `make up` or `make dev` again.

**Logging in answers 503, "This can't be done right now"**
:   The API can't reach Valkey, so it can't count attempts, and refuses them instead of letting them through uncounted. `/api/readyz` and `docker compose ps` show what is down. People who are already logged in are not affected.

**Postgres rejects its password after you recreated `.env`**
:   Postgres keeps the password it was first started with. Delete the local data with `docker compose down --volumes`, then start again.

**`make dev` on Linux**
:   `make dev` relies on Docker Desktop to route containers to services on your machine's 127.0.0.1, which plain Docker Engine doesn't do. Use `make up` instead.
