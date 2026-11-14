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

- <http://localhost:8080>: the chat. It needs a model to answer: see [Models](#models);
- <http://localhost:8080/api/docs>: the API's interactive documentation;
- <http://localhost:8080/api/healthz>: the API's health check;
- <http://localhost:8080/api/readyz>: whether the API can do its work. It says `ready` when the database is reachable and migrated and Valkey answers, and otherwise names what is missing.

`make smoke` checks all three from the command line. `make down` stops everything.

## Models

Actio runs its models on [Ollama](https://ollama.com), version 0.40 or newer, installed on your machine itself: not in a container, so it can use the graphics processor. Install it, start it, and download the model Actio offers by default (3.3 GB):

```sh
ollama pull qwen3.5:4b-q4_K_M
```

Then open <http://localhost:8080> and ask something. No account is needed: a first visit starts a guest session, which lasts a day.

- **The answer appears as it is written.** The first one takes a few seconds longer, while the model loads. **Stop** ends an answer early and keeps what was written.
- **Answers are formatted:** headings, tables, code in colour with a button to copy it, formulas, and diagrams. Try "derive the quadratic formula", or "draw a mermaid flowchart of making tea".
    - A link in an answer shows its address before it opens.
    - A picture an answer points at is not fetched, and HTML in an answer is shown as text. A model's words can come from anywhere, so they can't make your browser do things ([ADR 0016](adr/0016-answers-shown-as-markdown.md)).
- **The page stays at the newest words** while an answer is written. Scroll up to read, and it leaves you there; the arrow at the bottom takes you back.
- **Think before answering** has the model reason first, and shows its reasoning above the answer, folded away.
- **A conversation has an address,** `/c/…`, once its first question is sent: reload the page, or come back to it with the browser's Back button. **New chat** starts another.
- **Your conversations are listed beside the chat,** the one you continued last on top, each under its first question.
    - The pencil renames one, and the bin deletes it, after asking: there is no way back.
    - Opening another conversation while an answer is being written stops that answer, like **Stop** ([ADR 0018](adr/0018-the-list-of-conversations-is-the-servers.md)).
    - The button at the top left puts the list away. In a narrow window it brings the list up over the chat.
    - A guest's conversations last as long as the guest: a day.
- **Sign up to keep them.** The account takes over what you did as a guest, and you are back where you were ([ADR 0019](adr/0019-accounts-in-the-one-screen.md)).
    - An account is an email address and a password of at least 10 characters. No email is sent, so the address isn't checked, and a forgotten password can't be reset by its owner.
    - **Log in** from another browser, or after logging out. What you started there as a guest comes along.
    - Your address in the header leads to your account: change the password, which logs every other browser out, or log out of this one or of all.
- **While the model is missing or Ollama isn't running, the page says so,** with the command above, and notices by itself when that changes.

`make smoke` notes a model whose server doesn't answer too. The API says it in more detail: with a session, `GET /api/models` lists each model as `ready`, `missing` from its server, or its server as `unreachable`.

The page is built on the API, which you can also use directly. To ask the model something from the command line, and watch the answer arrive piece by piece:

```sh
jar=$(mktemp)
curl --silent --cookie-jar "$jar" --request POST http://localhost:8080/api/session/guest
curl --no-buffer --cookie "$jar" --header 'Content-Type: application/json' \
  --data "{\"message\": {\"id\": \"$(uuidgen)\", \"parts\": [{\"type\": \"text\", \"text\": \"What is a derailleur?\"}]}}" \
  "http://localhost:8080/api/conversations/$(uuidgen)/turns"
```

The first command starts a guest session. The second starts a conversation, under an id made on the spot, and streams the answer as events. See [ADR 0014](adr/0014-turns-streamed-in-the-ai-sdks-format.md) for what they mean.

Which models are offered is in `config/models.toml`: the file explains itself. To offer another model, or one on LM Studio or any other server that speaks OpenAI's API, edit it and run `make up` or `make dev` again. A mistake in the file stops the API from starting, with a line that says what is wrong. See [ADR 0013](adr/0013-the-models-on-offer-are-a-file.md).

!!! note "On Linux"
    Ollama listens only on `127.0.0.1` by default, which containers can't reach, so with `make up` the model shows as `unreachable`. Ollama's `OLLAMA_HOST` setting changes where it listens: Docker's bridge address, usually `172.17.0.1`, is reachable from containers and not from your network. This follows Ollama's and Docker's documentation; this project's own Linux run, in CI, has no Ollama to try it with.

## Develop it

```sh
make bootstrap   # checks your tools, installs everything, creates .env
make dev         # starts the stack; Ctrl-C stops the API and the web app
```

`make doctor` lists any missing tools, with install hints. You don't need Node.js itself: pnpm installs the version the project pins.

In this mode, saving a Python file restarts the API, and the browser picks up changes to the web app by itself. `make dev` first stops any containers that `make up` started, so you can switch between the two modes freely. Postgres and Valkey keep their data in Docker volumes in both modes, so it survives restarts.

## Talk to a local model from the terminal

The client the chat reaches models with also works on its own. With the development setup above and [Ollama](https://ollama.com/download) 0.40 or newer running:

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
| `make check` | Run what CI's main job runs: static checks, tests and builds |
| `make e2e` | Run the end-to-end tests: a stack of its own with a fake model, driven in a real browser |
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
:   A rate limit was reached. The page shows the answer, which says how long to wait; its `Retry-After` header says it in seconds. On your own machine, raise the limit in `.env` (see [Configuration](#configuration)) and run `make up` or `make dev` again.

**The chat says a conversation "doesn't exist, or belongs to another session"**
:   A guest's conversations go with the guest's session, which lasts a day and lives in one browser. The address of a guest's conversation opens nowhere else, and not after that day.

**A model is `missing` or `unreachable`**
:   The chat says so above the message box, `GET /api/models` says so, and `make smoke` notes it. `missing`: Ollama runs but hasn't got the model; `ollama pull` it, by the name in `config/models.toml`. `unreachable`: Ollama isn't running, or isn't where the API looks. If it runs on another machine, set `ACTIO_OLLAMA_URL` in `.env`. It takes up to 15 seconds for a change to show.

**The API doesn't start, and says `actio: … models.toml: …`**
:   `config/models.toml` has a mistake, and the line says which. Fix it and start again.

**Logging in answers 503, "This can't be done right now"**
:   The API can't reach Valkey, so it can't count attempts, and refuses them instead of letting them through uncounted. `/api/readyz` and `docker compose ps` show what is down. People who are already logged in are not affected.

**Postgres rejects its password after you recreated `.env`**
:   Postgres keeps the password it was first started with. Delete the local data with `docker compose down --volumes`, then start again.

**`make dev` on Linux**
:   `make dev` relies on Docker Desktop to route containers to services on your machine's 127.0.0.1, which plain Docker Engine doesn't do. Use `make up` instead.
