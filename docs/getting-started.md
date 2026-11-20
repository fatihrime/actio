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
- <http://localhost:8080/api/readyz>: whether the API can do its work. It says `ready` when the database is reachable and migrated and Valkey answers, and otherwise names what is missing. Under `make dev`, where the worker runs inside the API's process, it also says whether that worker works.

`make smoke` checks them from the command line, and that a guest can start a session and is told the models and its usage limits. `make down` stops everything.

## Models

Actio runs its models on [Ollama](https://ollama.com), version 0.40 or newer, installed on your machine itself: not in a container, so it can use the graphics processor. Install it, start it, and download the model Actio offers by default (3.3 GB):

```sh
ollama pull qwen3.5:4b-q4_K_M
```

Then open <http://localhost:8080> and ask something. No account is needed: a first visit starts a guest session, which lasts a day.

- **The answer appears as it is written.** The first one takes a few seconds longer, while the model loads. **Stop** ends an answer early and keeps what was written.
- **Only Stop ends an answer.** Reloading the page, closing it or losing the connection doesn't: the answer is written to its end at the server, and is there when you come back: still arriving, if it isn't done yet, with Stop beside it. A second window that opens the conversation while the answer is being written shows it arriving too ([ADR 0022](adr/0022-turns-that-run-apart-from-their-request.md)). An answer you no longer want still counts against your usage limits unless you stop it.
- **Ollama writes one answer at a time.** A question asked while another answer is being written, in another conversation or by someone else on the same Actio, waits its turn: the page shows the question and the wait for the first words, and they come once the other answer is done. Answers are taken in the order they were asked. **Stop** ends one that waits too; it has written nothing then, and costs nothing ([ADR 0023](adr/0023-answers-that-wait-their-turn.md)).
- **Answers are formatted:** headings, tables, code in colour with a button to copy it, formulas, and diagrams. Try "derive the quadratic formula", or "draw a mermaid flowchart of making tea".
    - A link in an answer shows its address before it opens.
    - A picture an answer points at is not fetched, and HTML in an answer is shown as text. A model's words can come from anywhere, so they can't make your browser do things ([ADR 0016](adr/0016-answers-shown-as-markdown.md)).
- **The page stays at the newest words** while an answer is written. Scroll up to read, and it leaves you there; the arrow at the bottom takes you back.
- **Think before answering** has the model reason first, and shows its reasoning above the answer, folded away.
- **A conversation has an address,** `/c/…`, once its first question is sent: reload the page, or come back to it with the browser's Back button. **New chat** starts another.
- **Your conversations are listed beside the chat,** the one you continued last on top, each under its first question.
    - The pencil renames one, and the bin deletes it, after asking: there is no way back.
    - You can open another conversation while an answer is being written. The answer goes on, and its conversation shows it when you return: as it is written, if it isn't done yet.
    - Deleting a conversation stops the answer that is being written in it.
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

A server that writes several answers at the same time is told so in the same file, under its provider. Ollama started with `OLLAMA_NUM_PARALLEL=4`, for example:

```toml
[providers.ollama]
kind = "ollama"
answers_at_once = 4
```

Then four answers are written at once, and a fifth waits its turn. Left out, it is 1 for Ollama, and there is no limit for a server that speaks OpenAI's API, such as vLLM, which takes every answer as it comes. For LM Studio or llama.cpp's server, give the number of requests it was set to handle in parallel.

!!! note "On Linux"
    Ollama listens only on `127.0.0.1` by default, which containers can't reach, so with `make up` the model shows as `unreachable`. Ollama's `OLLAMA_HOST` setting changes where it listens: Docker's bridge address, usually `172.17.0.1`, is reachable from containers and not from your network. This follows Ollama's and Docker's documentation; this project's own Linux run, in CI, has no Ollama to try it with.

## Usage limits

An answer costs credits, and everyone has only so many: per session, which opens with the first question and lasts five hours, and per week. At the default prices a credit is one token of an answer, and what the model reads costs a quarter of that.

| | A session | A week |
|---|---|---|
| A guest | 25,000 credits | 100,000 |
| An account | 100,000 | 600,000 |

- **Under the list of conversations, a meter shows how much of each limit is used,** and when it resets, by your clock.
- **On the way to a limit you are told once:** when half of it is used, three quarters, and nine tenths.
- **Near a limit, answers get shorter.** An answer is given only as much room as is left. One that had to stop for that says so.
- **At a limit, a question is refused.** The page says which limit was reached, when it resets, and counts down the time until then. Nothing is sent meanwhile. The question isn't stored: it is back in the box, to be sent later.
- **A guest may have two answers being written at once, and an account three,** in all their conversations together. One question more is refused, and says so: "2 answers are already being written for you, as many as your plan allows at once. Wait for one to finish, or stop one." The question is back in the box, and can be sent as soon as one of the answers has ended. An answer that waits for the model's server counts too.
- **A model that costs nothing can carry on.** Where the installation offers one, the page offers it at the limit, and that model answers until the limit resets. The default files offer none: see below.
- **The API says the same.** With the session from the commands above:

    ```sh
    curl --silent --cookie "$jar" http://localhost:8080/api/usage
    ```

    It answers with your plan, and for each limit how much is used and when it starts over. The page shows exactly this, and counts nothing itself ([ADR 0021](adr/0021-usage-limits-in-the-page.md)).

Prices, plans, how many answers each plan may have written at once, the length of a session and the moment the week starts over are in `config/plans.toml`, which explains itself. Edit it and run `make up` or `make dev` again.

- Every model in `config/models.toml` needs a price there, or the API doesn't start, and says which model is missing one.
- A model priced at nothing is free: it uses up no credits and is never refused. Offer a small one beside the default, at `input = 0` and `output = 0`, and the page offers it by its title to whoever reaches a limit: "Use Local (free)".
- A plan that names no limits limits nothing: on a machine that is yours alone, give accounts such a plan.

What was used is kept in the database, in a ledger that only ever grows. See [ADR 0020](adr/0020-usage-limits-on-turns.md).

## Develop it

```sh
make bootstrap   # checks your tools, installs everything, creates .env
make dev         # starts the stack; Ctrl-C stops the API, with its worker, and the web app
```

`make doctor` lists any missing tools, with install hints. You don't need Node.js itself: pnpm installs the version the project pins.

In this mode, saving a Python file restarts the API, and the worker that runs inside its process, the part behind the background jobs, such as the sweep of expired guests every ten minutes. The browser picks up changes to the web app by itself. Ctrl-C stops them as Docker stops the containers: an answer that is being written gets up to 15 seconds to finish, and is recorded as interrupted after that. Ctrl-\ ends them at once. The proxy's configuration, `deploy/caddy/Caddyfile`, is built into its image: after a change to it, run `make dev` (or `make up`) again to apply it. A mistake in it stops the build, with Caddy's own words, and the proxy that runs goes on as it was. `make dev` first stops any containers that `make up` started, so you can switch between the two modes freely. Postgres keeps its data in a Docker volume in both modes, so it survives restarts. Valkey keeps nothing on disk: after a restart the rate limits start full, and the usage limits are counted up again from the database.

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
| `make smoke` | Check a running stack: the web page, the API's health and its docs, a guest session, the models and the usage limits |
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

**`/api/readyz` says `migrations pending`, `unreachable` or `not working`**
:   The API is running but can't do its work yet. `migrations pending`: the database is older than the code; `make up` and `make dev` both apply the missing migrations when you run them again. `unreachable`: Postgres or Valkey isn't running or refuses the password; `docker compose ps` shows their state, and the API's log says more. Under `make dev`, `"worker": "not working"`: the worker inside the API's process, which runs the background jobs, hasn't started yet, or can't work. It says why in the API's log, in lines that say `The worker can't work` or `The worker ended with an error`, and tries again every ten seconds.

**"Too many new guests from your network", or another 429**
:   A rate limit was reached. The page shows the answer, which says how long to wait; its `Retry-After` header says it in seconds. On your own machine, raise the limit in `.env` (see [Configuration](#configuration)) and run `make up` or `make dev` again.

**The chat says a conversation "doesn't exist, or belongs to another session"**
:   A guest's conversations go with the guest's session, which lasts a day and lives in one browser. The address of a guest's conversation opens nowhere else, and not after that day.

**A model is `missing` or `unreachable`**
:   The chat says so above the message box, `GET /api/models` says so, and `make smoke` notes it. `missing`: Ollama runs but hasn't got the model; `ollama pull` it, by the name in `config/models.toml`. `unreachable`: Ollama isn't running, or isn't where the API looks. If it runs on another machine, set `ACTIO_OLLAMA_URL` in `.env`. It takes up to 15 seconds for a change to show.

**The API doesn't start, and says `actio: … models.toml: …` or `actio: … plans.toml: …`**
:   One of the two files in `config/` has a mistake, and the line says which. Fix it and start again. A model that is offered in `models.toml` and has no price in `plans.toml` is such a mistake.

**"You've reached your usage limit"**
:   The session's allowance, or the week's, is used up: see [Usage limits](#usage-limits). The page says when it starts over, and counts down to it. On your own machine, raise the limits in `config/plans.toml`, or leave them out, and run `make up` or `make dev` again.

**"2 answers are already being written for you"**
:   As many answers are on their way, in your other conversations, as your plan allows at once: see [Usage limits](#usage-limits). Wait for one to finish, or open its conversation and stop it, then send the question again: it is still in the box. To allow more, raise `answers_at_once` in `config/plans.toml`, or leave it out, and run `make up` or `make dev` again.

**An answer takes long to begin while another is being written**
:   The model's server writes one answer at a time, so this one waits its turn inside Actio, and begins when the other is done. The API's log says how long it waited: `turn …: waited 12.3 s for the model's server`. If the server can write several at once, say how many in `config/models.toml`: see [Models](#models).

**"This is too much to send at once"**
:   A request to the API was larger than 2 MB, and the proxy in front of it refused it. A question has room for 100,000 characters, which is far less.

**"The server was restarted while this answer was being written"**
:   The API was stopped in the middle of an answer: by `make down`, by `make up` after a change, or by a saved file under `make dev`. It gives the answers being written 15 seconds to finish, then keeps what each had written and marks it interrupted. If the API was killed instead, the answer is marked so once it has been silent for a minute, with what it had written when it last saved it: an answer that is being written is saved every 15 seconds. Such an answer costs nothing. **Try again** asks for it once more.

**`make down` takes a quarter of a minute**
:   An answer was being written: see the entry above. With none under way it takes a second or two.

**"An answer to this is still being written", and it stays**
:   The answer is being written at the server, and the page can't read along with it: the stream of its words was lost, as after a restart of Valkey, or couldn't be asked for. It can't be asked for when six of your windows or tabs already read along with answers, the most one account may. Otherwise the page shows the words as they arrive. It looks again every second and a half, and shows the answer when it is done. If the API was killed while writing it, nothing ever finishes it: the page notices a minute after the answer was last heard from, shows what had been written when it was, and offers to try again.

**A question answers 503, "Usage can't be counted right now"**
:   The API can't reach Valkey, where usage is counted, and refuses instead of answering uncounted. `/api/readyz` and `docker compose ps` show what is down.

**Logging in answers 503, "This can't be done right now"**
:   The API can't reach Valkey, so it can't count attempts, and refuses them instead of letting them through uncounted. `/api/readyz` and `docker compose ps` show what is down. People who are already logged in are not affected.

**Postgres rejects its password after you recreated `.env`**
:   Postgres keeps the password it was first started with. Delete the local data with `docker compose down --volumes`, then start again.

**`make dev` on Linux**
:   `make dev` relies on Docker Desktop to route containers to services on your machine's 127.0.0.1, which plain Docker Engine doesn't do. Use `make up` instead.
