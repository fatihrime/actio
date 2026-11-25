# Decisions

Each architecture decision record (ADR) captures one decision that would be hard to reverse:

- the context;
- the options weighed;
- what was chosen and why;
- how to undo it.

Facts that change over time, such as which versions are current, carry the date they were checked.

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-one-app-several-process-roles.md) | One application, several process roles | Accepted |
| [0002](0002-python-workspace-with-uv.md) | A Python workspace managed by uv, on uv's own CPython | Accepted |
| [0003](0003-web-app-on-nextjs.md) | A Next.js web app in a pnpm workspace, on a pinned Node.js | Accepted |
| [0004](0004-local-development-stack.md) | A hybrid local stack: services in Docker, the apps native, Caddy in front | Accepted |
| [0005](0005-container-images.md) | Container images and the production-shaped stack | Accepted |
| [0006](0006-usage-limits-engine.md) | Usage limits: reservations in Valkey, a ledger in Postgres, a Python reference model | Accepted |
| [0007](0007-local-models-through-ollamas-native-api.md) | Local models through Ollama's native API, with silent truncation turned off | Accepted |
| [0008](0008-postgres-through-sqlalchemy-and-psycopg.md) | Postgres through SQLAlchemy and psycopg 3, with migrations as a job | Accepted |
| [0009](0009-sessions-in-the-database.md) | Sessions in the database, in a cookie bound to the host | Accepted |
| [0010](0010-accounts-with-a-password.md) | Lasting accounts with an email address and a password | Accepted |
| [0011](0011-rate-limits-as-token-buckets-in-valkey.md) | Rate limits on the open routes, as token buckets in Valkey | Accepted |
| [0012](0012-conversations-as-a-tree-of-messages.md) | Conversations as a tree of messages | Accepted |
| [0013](0013-the-models-on-offer-are-a-file.md) | The models on offer are a file | Accepted |
| [0014](0014-turns-streamed-in-the-ai-sdks-format.md) | Turns: a question, an answer streamed in the AI SDK's format, and a record of how it went | Accepted |
| [0015](0015-the-web-chat-calls-the-api-from-the-browser.md) | The web chat: the browser calls the API, and the server stays the truth | Accepted |
| [0016](0016-answers-shown-as-markdown.md) | Answers are shown as markdown, which can't act on the page or its reader | Accepted |
| [0017](0017-end-to-end-tests-on-the-real-stack.md) | End-to-end tests drive the real stack in a real browser, with a fake model | Accepted |
| [0018](0018-the-list-of-conversations-is-the-servers.md) | The list of conversations is the server's: read again, never rearranged | Accepted |
| [0019](0019-accounts-in-the-one-screen.md) | Signing up and logging in happen in the one screen, and the page forgets a session that has ended | Accepted |
| [0020](0020-usage-limits-on-turns.md) | Usage limits on turns: credits held before an answer, and the ledger written before the counters | Accepted |
| [0021](0021-usage-limits-in-the-page.md) | Usage limits in the page: the server counts, and the page learns of a limit by being refused | Accepted |
| [0022](0022-turns-that-run-apart-from-their-request.md) | Turns that run apart from their request: a stream to read along with, and a way to stop | Accepted |
| [0023](0023-answers-that-wait-their-turn.md) | Answers that wait their turn: a gate in front of a model's server, and limits on what one account runs at once | Accepted |
| [0024](0024-background-jobs-in-a-queue-in-postgres.md) | Background jobs in a queue in Postgres, through Procrastinate | Accepted |
| [0025](0025-titles-written-by-a-model.md) | Titles written by a model: once per conversation, by the worker, never over a person's | Accepted |
| [0026](0026-models-loaded-ahead-of-the-first-question.md) | A model is loaded ahead of the first question: the page asks, the API answers at once, and the load is a chat's request without messages | Accepted |
| [0027](0027-cloud-models-through-their-openai-compatible-layers.md) | Groq and the Gemini API through their OpenAI-compatible layers, by the one client, which reads what differs leniently; what a model is told about reasoning is in the models file | Accepted |
| [0028](0028-a-model-names-those-that-stand-in-for-it.md) | A model names the models that stand in for it, asked in its place before the first word or thought of an answer; the turn stays the asked model's, at its price | Accepted |
| [0029](0029-a-model-is-passed-over-for-a-request-too-large.md) | A model is passed over when a request is larger than its room, its window or what its server takes in a minute, while another model has room; a guess never refuses a turn | Accepted |
| [0030](0030-the-providers-budgets-are-counted.md) | The providers' budgets are counted in Valkey, and a model with nothing left for a request is passed over while another has budget; a count never refuses a turn | Accepted |

To record a new decision, copy the [template](0000-template.md) to the next free number.
