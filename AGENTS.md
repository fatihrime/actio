# AGENTS.md

Guidance for AI coding agents, and for humans, working in this repository.

## Project

Actio is a self-hostable, local-first AI platform. It combines a chat assistant, an OpenAI/Anthropic-compatible developer gateway, and Claude-inspired usage limits. Design decisions live in [`docs/adr/`](docs/adr/).

## Ground rules

- **Small steps.**
  - Make one focused, tested change at a time. There is no size limit: a step is done when it works end to end, with its tests.
  - Explain what changed, why, and how to verify it.
- **Humans commit.** Agents never commit, push, or create remote resources. The maintainer reviews and commits every change.
- **No secrets in the repo.**
  - Never commit `.env` files, keys or tokens.
  - Tests must never read `.env`; they use fakes and generated test config.
  - Commit hooks scan for secrets (gitleaks) and for private names listed in `~/.config/actio/denylist.txt`, which stays outside the repo.
- **Never bypass the checks.** Don't use `git commit --no-verify`; fix what the hook reports.
- **Written from scratch.**
  - Don't paste code from other projects.
  - Third-party code enters only as declared dependencies with compatible licenses: MIT, BSD, Apache-2.0 and similar permissive ones, or the LGPL for a library used unmodified ([ADR 0008](docs/adr/0008-postgres-through-sqlalchemy-and-psycopg.md)). No GPL or AGPL code.
  - Check a new dependency's license from its own metadata before adding it. If it reaches a container image under anything but a permissive license, list it in `THIRD_PARTY_NOTICES.md`.
- **Fictional data only.** Demo content, such as the "Velora Cycles" knowledge base, is fictional.

## Layout (planned)

| Path | Purpose |
|---|---|
| `apps/api` | FastAPI modular monolith (`python -m actio --role api\|migrate`; `worker` and `all` come with the job queue). `api/` is the HTTP interface; `chat/` holds conversations, their tree of messages and the turns in which a model answers; `catalog.py` holds the models on offer; `identity/` holds accounts and sessions; `core/` holds settings, ids, the database and the rate limiter; `migrations/` holds the schema's history |
| `apps/web` | Next.js web app: the chat. `app/` holds the pages, which only place components; `components/` the interface: the conversation on the page (`components/chat`), the list of conversations beside it (`components/sidebar`) and the forms for signing up, logging in and the account (`components/account`); `lib/` what it is built on: the calls to the API (`lib/api`, with types generated from the API's description), how a turn is requested, how an answer's markdown is read and how the list of conversations is kept (`lib/chat`), whose session the page has (`lib/session.ts`), ids and the address bar; `tests/` the fake API and the recorded answers the component tests run on; `e2e/` the end-to-end tests, which Playwright runs against the stack of `make e2e` |
| `packages/quota` | Usage-limits engine: Lua scripts plus a Python reference model |
| `packages/llm` | Model clients: the neutral chat format, the Ollama adapter and the OpenAI-compatible adapter (`actio-llm`); the router comes later |
| `packages/fakellm` | Deterministic fake LLM server for tests, scripted by directives in the prompt (`[[think: ...]]`, `[[call: ...]]`, `[[fail: 503]]`, `[[pace: 0.1]]`). Its `Dockerfile` builds the image the end-to-end stack runs |
| `config/` | What an installation changes without touching code. Today `models.toml`, the models on offer ([ADR 0013](docs/adr/0013-the-models-on-offer-are-a-file.md)); plans, assistant profiles and the demo knowledge base come later. TOML, which Python reads without a dependency |
| `compose.yaml` | The production-shaped stack behind `make up`: Caddy, web, API, Postgres, Valkey, and the `migrate` job that runs before the API |
| `compose.dev.yaml` | The overlay behind `make dev`: Caddy routes to the native processes; data ports open on 127.0.0.1 |
| `compose.e2e.yaml` | The overlay behind `make e2e`: the same stack with the fake model as one more container, and the models file in `deploy/e2e/config/` that offers it ([ADR 0017](docs/adr/0017-end-to-end-tests-on-the-real-stack.md)) |
| `deploy/caddy/` | Caddy config: one entry point routing `/api/*` and `/v1/*` to the API, the rest to the web app |
| `deploy/tests/` | Policy tests for the containers: published ports, pinned images, networks, hardening |
| `docs/` | The documentation site's pages (built by Zensical from `zensical.toml`), including the ADRs in `docs/adr/` |
| `scripts/` | Developer scripts |
| `conftest.py` | Test fixtures shared by every suite: a throwaway Postgres and Valkey, from the images pinned in `compose.yaml` |

## Commands

| Command | What it does |
|---|---|
| `make help` | List available targets |
| `make doctor` | Check that required tools are installed. Prints install hints; installs nothing |
| `make bootstrap` | Set up a fresh clone: `doctor`, `install`, a `.env` with random passwords, then pull the images |
| `make install` | Install the Python and JavaScript workspaces with their dev tools, plus the git hooks |
| `make up` | Build and run the whole stack in containers, shaped like production, at http://localhost:8080 |
| `make dev` | Run the stack at http://localhost:8080 with the API and web app native, with hot reload |
| `make smoke` | Check a running stack through Caddy: the web page, and that the API is healthy, ready (database migrated, Valkey answering), serves its docs, starts a guest session and lists its models |
| `make migration name="..."` | Write a new database migration from the table models, as a numbered draft to read and edit |
| `make openapi` | After a change to a route or a schema: record the API's description again (`apps/api/openapi.json`), and generate the web app's types from it (`apps/web/lib/api/schema.d.ts`). Review the difference |
| `make docs` | Preview the documentation site at http://localhost:8001, reloading on edits |
| `make down` | Stop all containers. Their data stays in Docker volumes |
| `make check` | Run what CI's main job runs: every static check on all files, the tests, then the builds (web app and docs site). The end-to-end tests are apart: `make e2e` |
| `make test-live` | Run the model-client tests against a real Ollama server (0.40 or newer, with `granite4:micro` pulled). Not part of `make check` |
| `make e2e` | Run the end-to-end tests: build and start a stack of its own (port 8090, its own data, a fake model), drive it in a real browser with Playwright, and remove it. Needs Docker; about a minute. Not part of `make check`: CI runs it as its own job. `make e2e args="--headed --workers 1"` passes arguments to Playwright, here to show the browser, one test at a time (the first such run downloads the whole of Chromium, about 200 MB). `ACTIO_E2E_KEEP=1` leaves the stack running, and `make e2e args=down` stops it |
| `make fmt` | Format code and apply safe lint fixes |

`make lint`, `make test` and `make build` run the parts of `make check` on their own. The static checks are defined once, in `.pre-commit-config.yaml`, and run at every commit, in `make check` and in CI. Processes can also run alone: `uv run actio --role api --reload`, `pnpm --dir apps/web dev`, or the fake LLM server with `uv run fakellm`. `uv run actio-llm chat <model> "<prompt>"` talks to a local model through the Ollama adapter. The web app is tested at two levels: its components in a simulated browser, against a fake API (`make test`), and the whole stack in a real browser, with a fake model (`make e2e`). A change to the chat should pass both, and be looked at once at http://localhost:8080.

`make up` needs only Docker; it builds the images from `apps/api/Dockerfile` and `apps/web/Dockerfile`. `make dev` needs Docker Desktop: its containers reach the natively running API and web app through `host.docker.internal`. Changes to the containers must keep `deploy/tests/` passing; if a rule must change, change the test on purpose and record why in an ADR. `.env` holds the local passwords. It is created once from `.env.example`, git-ignored, and never read by tests. Postgres keeps the password it first started with, so if you recreate `.env`, also wipe the local data with `docker compose down --volumes`.

## Conventions

- **Python:**
  - 3.13, managed with `uv`;
  - add dependencies with `uv add`, never `pip`; run tools through `uv run` or `make`;
  - workspace packages are named `actio-<name>` and imported as `actio_<name>`, such as `actio-fakellm`;
  - formatted and linted with `ruff`;
  - `mypy --strict` on core packages;
  - tests with `pytest`; property and state-machine tests with Hypothesis (`HYPOTHESIS_PROFILE=dev|ci|nightly`);
  - `make test` fails below 90% coverage of `packages/quota`, and needs Docker running: the quota and API tests start throwaway Valkey and Postgres containers;
  - a change to a table model needs a migration in the same commit (`make migration name="..."`): a test fails when the models and the migrations disagree. Never edit a migration that is already on `main`; write the next one;
  - `apps/api/src/actio/core` imports nothing else from `actio`;
  - change the Lua in `packages/quota` only together with the Python reference model (`oracle.py`): the differential test requires both to behave identically;
  - the same holds for the rate limiter's script, `apps/api/src/actio/core/rate_limit.lua`, and the plain token buckets in `apps/api/tests/test_rate_limit.py`;
  - new HTTP client code uses `httpx2`, the maintained continuation of httpx ([ADR 0007](docs/adr/0007-local-models-through-ollamas-native-api.md)). The older test fixtures that call an app in-process still use httpx;
  - the Ollama adapter's tests replay recorded exchanges (`packages/llm/tests/fixtures/`), and fail if the request it sends changes. After changing what it sends, run `ACTIO_RECORD_OLLAMA=1 make test-live` to record them again from a real server, and review the diff;
  - what a turn streams is recorded too, in `apps/api/tests/fixtures/turns/`, and the web app's tests read those files with the real `ai` package ([ADR 0014](docs/adr/0014-turns-streamed-in-the-ai-sdks-format.md)). After changing the stream on purpose, run `ACTIO_RECORD_TURNS=1 uv run pytest apps/api/tests/test_turn_fixtures.py`, review the diff, then `pnpm --dir apps/web run test`.
- **TypeScript:**
  - `pnpm` 12; pnpm also installs the Node.js version pinned in the root `package.json` (26 LTS);
  - add dependencies with `pnpm --dir apps/web add`, never `npm`. pnpm refuses a version published less than a day ago: take the newest one that is older;
  - the `ai` package is pinned to an exact version: the API writes the stream it reads ([ADR 0014](docs/adr/0014-turns-streamed-in-the-ai-sdks-format.md)), so an upgrade is a change to check, not to drift into. `@ai-sdk/react` is pinned with it: each of its versions depends on exactly one version of `ai`, so the two move together;
  - only the browser calls the API, through `lib/api/client.ts`. No server component, route handler or server action does: the session is the browser's cookie, and the API counts requests by the client's address ([ADR 0015](docs/adr/0015-the-web-chat-calls-the-api-from-the-browser.md));
  - `lib/api/schema.d.ts` is generated from `apps/api/openapi.json`. Never edit it: run `make openapi`. A test fails when it is out of date;
  - the server is the truth about a conversation. The chat library's state is what is shown while an answer streams; when a turn ends any other way than well, the page reads the conversation again instead of working out what happened (`resync` in `components/chat/chat.tsx`);
  - the page moves between conversations through `lib/location.ts` (the browser's history), not with `next/link` or the router: a navigation would end an answer that is streaming. The same goes for every other place in the app, such as `/login`: a link to one is an `InAppLink` (`components/in-app-link.tsx`), and a new place is a case in `placeIn` plus a page file that renders `ChatApp`;
  - whose session the page has comes from `lib/session.ts`, and changes only with what the API answered ([ADR 0019](docs/adr/0019-accounts-in-the-one-screen.md)). Whatever is read from the API for whoever has the session is held under `session.holder`, as the list of conversations is: never under a fixed name or the account's id. Then it is dropped when the session ends, and the same account logging in again reads it anew;
  - a form says what is wrong under the box it is about (`Field` in `components/account/form.tsx`), in the API's words where the API said it, and puts the keyboard on that box. The browser's own validation is off (`noValidate`). A limit a form holds people to is the API's, kept in `lib/account.ts` and checked against the API's description by `tests/contract/limits.test.ts`;
  - the list of conversations is the server's too ([ADR 0018](docs/adr/0018-the-list-of-conversations-is-the-servers.md)). Everything reads and changes it through `lib/chat/list.ts`. After something that changes the list, read it again (`refresh`): never insert, move or sort rows on the page. A rename or a delete shows once the server has done it, not before;
  - what the interface does is a link or a button with a name. After an action the keyboard's focus is put somewhere on purpose, and the action's test says where;
  - components are written here. Third-party interface code comes in as a declared dependency, never as source copied in, which rules out shadcn/ui-style generators ([ADR 0015](docs/adr/0015-the-web-chat-calls-the-api-from-the-browser.md));
  - a model's words reach the page through one component, `components/chat/markdown.tsx`, and no other way: never through `dangerouslySetInnerHTML`. That file decides what an answer may do: no HTML, no fetched pictures, a link only after its address was shown ([ADR 0016](docs/adr/0016-answers-shown-as-markdown.md)). A rule loosened there fails a test in `markdown.test.tsx`; change one only on purpose, with the ADR;
  - nothing else imports `streamdown` or its plugins. `message.tsx` fetches that component when the first answer is shown, which keeps about 300 kB out of the page's first load;
  - where models write markdown that the parser reads otherwise (formulas, money, `<br>` in tables), it is put right in `lib/chat/markdown.ts`, on the parsed markdown. Never with a pattern over the text: that changes what is inside code too;
  - colours have names by role in `app/globals.css`: `bg-muted`, `text-muted-foreground`, `border-border`, `border-input`, `outline-ring`, `bg-warning`, `bg-destructive` and so on, each light and dark at once. Every component uses them: none names a colour with its `dark:` twin. A colour that is missing gets a name there first. A new Streamdown plugin needs its `@source` line there, or its classes are missing from the built styles;
  - component tests drive the page as a person would, against `tests/fake-api.ts`. It answers with the API's recorded streams, and with bodies typed by the generated types. A request the test set up no answer for fails the test. A test opens the whole app with `open()` from `tests/open.tsx`, not a component by itself;
  - strict mode, plus `noUncheckedIndexedAccess`;
  - formatted with Prettier; linted with ESLint and typescript-eslint's strict type-checked rules;
  - stay on TypeScript 6.0 and ESLint 9 until typescript-eslint and eslint-config-next support the next majors ([ADR 0003](docs/adr/0003-web-app-on-nextjs.md));
  - Next.js 16 changed many APIs: read the version-matched docs in `apps/web/node_modules/next/dist/docs/` before writing Next.js code;
  - tests with Vitest, and Playwright for end-to-end tests;
  - end-to-end tests live in `apps/web/e2e` and drive the real stack ([ADR 0017](docs/adr/0017-end-to-end-tests-on-the-real-stack.md)):
    - import `test` and `expect` from `./chat`, never from `@playwright/test`. Then every test also fails when the page asks anything of another site, or a script on it breaks;
    - the model is the fake one: a test says what it does by typing directives into the chat box, such as `[[pace: 0.1]]`, `[[think: ...]]` or `[[fail: 503]]`. A new behaviour is a new directive in `packages/fakellm`, never state in the fake;
    - wait with Playwright's own assertions, which retry. No test is run again when it fails: one that fails now and then has found something, in itself or in the page;
    - cover here what only the whole stack or a real browser shows. The variants of a flow belong in the component tests, which run in a second;
    - a test that needs an account signs up under an address made up for it, at `example.com`. The overlay raises the limits per network address, and leaves the one per email address as people get it.
- **Commits:** [Conventional Commits](https://www.conventionalcommits.org/): `feat:`, `fix:`, `docs:`, `test:`, `chore:` and so on.
- **Architecture boundaries,** enforced by import-linter at every commit and in CI. The rules are in `pyproject.toml`, under `[tool.importlinter]`:
  - `packages/*` never import the application, or each other;
  - inside `actio`, imports only point down the layers: `cli`, `app`, `api`, `tables`, `chat`, then `catalog` and `identity` side by side, `core`. So `core` imports nothing else from `actio`, and `catalog` and `identity` don't import each other;
  - only `actio.api` imports the web framework. `core` and the domain modules, `chat`, `catalog` and `identity`, never do;
  - a new module directly under `actio` must be given a layer in that list, or the check fails. A new domain module goes among the domain modules, above the ones whose tables it points at, and joins the rule against importing the web framework;
  - a table that points at another module's table names its class, such as `ForeignKey(User.id)`, never the text `"users.id"`. Then loading one table module loads what it needs, and a process that loads only that one still works. A test loads each alone.
- **API conventions:**
  - routes live in `actio.api`; rules and tables live in a domain module below it, which never commits: the route decides when the unit of work is done;
  - errors are RFC 9457 problem documents: raise `Problem(status, detail)`. `detail` is shown to people as it is, so write it for them;
  - a route lists the errors it answers in its `responses`, with a description each. The `Problem` schema is filled in for them. A route that takes `CurrentPrincipal` passes `NO_SESSION`, or its router does: a test fails otherwise;
  - the API's description is recorded in `apps/api/openapi.json`, and the web app's types are generated from it. After changing a route or a schema, run `make openapi` and review the difference: tests on both sides fail until then;
  - reading a conversation whole takes several queries. Call `conversations.hold_still` first, so a turn that ends meanwhile isn't seen half ([ADR 0014](docs/adr/0014-turns-streamed-in-the-ai-sdks-format.md), revision of 2026-11-13);
  - nothing below `actio.api` reads the clock. Time comes in as `now`, from the `Now` dependency, so tests can move it. A route that lasts, such as a streamed answer, takes `Clock` and reads it again at the end. Ids the server makes in a route come from `NewId`, so a test knows them beforehand;
  - the format a browser reads is written in one place, `actio.api.ui_stream`. What a model's answer does is described without it, as steps, in `actio.chat.answering`, which also makes sure a turn never stays `running`: whatever ends a stream, its outcome is recorded;
  - a route that needs to know who is asking takes `CurrentPrincipal`;
  - what belongs to an account is looked up with the account's id, in the query itself. Someone else's row answers 404, like one that doesn't exist: see `OwnConversation` in `actio.api.conversations`;
  - a route that anyone can call, or that checks a password, counts the attempt before doing its work: it takes `Limits` and calls one, such as `await limits.log_in(email)` ([ADR 0011](docs/adr/0011-rate-limits-as-token-buckets-in-valkey.md)). A new limit is a method in `actio.api.rate_limits` and a `limit_*` setting, listed in `compose.yaml` and `.env.example` too, or a test fails;
  - a module that defines tables is named `models` and listed in `actio/tables.py`, or a test fails. So nothing else is named `models`: the models people chat with are the `catalog`;
  - a model is asked for by its name in `config/models.toml`, through the `Models` dependency: `catalog.get("standard")`. Nothing outside `actio.catalog` names a provider's own model, or builds a client for one.
- **Decisions:** anything hard to reverse gets an ADR. Copy [`docs/adr/0000-template.md`](docs/adr/0000-template.md).
