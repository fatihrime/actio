# 0015. The web chat: the browser calls the API, and the server stays the truth

- **Status:** accepted
- **Date:** 2026-11-13

## Context

The API holds conversations and streams answers ([ADR 0012](0012-conversations-as-a-tree-of-messages.md), [ADR 0014](0014-turns-streamed-in-the-ai-sdks-format.md)). This step puts a chat page on top of it, built on the AI SDK's `useChat` ([ADR 0003](0003-web-app-on-nextjs.md)).

- **Next.js can do the work in two places.** Its server can fetch data and render it into the page, or the page can fetch from the browser.
- **The session is a cookie that page scripts can't read** ([ADR 0009](0009-sessions-in-the-database.md)), and the API limits some requests by the client's address ([ADR 0011](0011-rate-limits-as-token-buckets-in-valkey.md)).
- **An answer can end in several ways:** complete, stopped by the reader, failed half-way, refused before it began, or cut by a lost connection. The page has to show the right thing after each.
- **The API is Python and the page is TypeScript.** Something has to keep the page's idea of a response equal to what the API sends.
- **The plan named shadcn/ui and AI Elements for the interface.** Both work by copying their components' source into the repository. This project's rule is that third-party code enters only as a declared dependency (`AGENTS.md`).

## Options considered

**Who calls the API:**

1. **The Next.js server,** which renders conversations into the page and passes requests on.
   - Pro: a conversation is in the first HTML the browser gets.
   - Con: a second place that handles the session, and every request it passes on has to carry the client's address, or the rate limits count the web server instead.
   - Con: a streamed answer would pass through one more process.
2. **The browser,** at the address the page came from. The reverse proxy already sends `/api/*` to the API ([ADR 0004](0004-local-development-stack.md)).
   - Pro: one path for the cookie, and the API sees who is really asking.
   - Con: a stored conversation shows a moment later than the page around it.

**How the page moves between conversations (`/` is a new one, `/c/<id>` a stored one):**

1. **The framework's navigation.** Each address is a page of its own.
   - Con: a new conversation gets its address while its first answer is still streaming. A navigation at that moment builds the page anew, which ends the stream.
2. **One component for both addresses,** which changes the address through the browser's history and reads it back from there.

**What the page shows after a turn that didn't end well:**

1. **What it can work out itself.** It knows what it sent and what arrived.
   - Con: often it can't know. Was a question that was stopped at once stored? Did the server keep the same half answer the page shows?
2. **What the server has.** The page asks for the conversation again.

**How the page knows the API's shapes:**

1. **Types written by hand.** Nothing notices when they fall behind.
2. **A client generated from the API's description** (OpenAPI), functions included.
   - Con: a lot of generated code for a dozen routes.
3. **Types generated from the description, and a small typed `fetch`** (`openapi-typescript`, `openapi-fetch`; both MIT).

**Where the interface's components come from:**

1. **shadcn/ui and AI Elements,** copied in and adapted.
   - Pro: a finished look, and accessible parts, quickly.
   - Con: several thousand lines of someone else's code in the repository, to be kept current by hand. It would break the rule above, or need the rule changed.
2. **Components written here,** on libraries that are declared dependencies.
   - Con: more to write, and accessibility is this project's own work.

## Decision

**The browser calls the API itself. The chat library holds the page's state while an answer streams, and the server's copy replaces it whenever a turn ends any other way than well. The API's shapes reach the page as generated types. The components are written here.**

**Calling the API:**

- **Only the browser calls it,** with `fetch`, at the page's own address. The Next.js server renders the frame of the page and nothing of anyone's: it has no session to ask with.
- **A first visit starts a guest session** (`POST /api/session/guest`), which also keeps a session that is already there. When the API refuses because the network has started too many guests, the page says what the API said, with how long to wait and a button to try again. It doesn't retry by itself: each try counts against the same limit.
- **A refusal is an error with the API's own words.** Every error is a problem document, and its `detail` is written for people. The page shows it as it is.

**Moving between conversations:**

- **Both addresses show one component, `ChatApp`.** It reads the conversation's id from the address, and changes the address with `history.pushState` and `replaceState`, which Next.js follows by itself. Back, Forward and reload work; no page is loaded in between.
- **A new conversation's id is made in the browser,** like its questions' ids: version 7 UUIDs, from `crypto.getRandomValues`. The handier `crypto.randomUUID` is missing on pages served over plain HTTP, which a home network may do.
- **The address becomes `/c/<id>` once the server has the conversation,** and not before: a first question that is refused leaves nothing to give an address to.

**The page's state:**

- **`useChat` holds the messages,** and streams an answer into them. Each message's `metadata` is the API's own: the message it follows, its versions, and for an answer the turn that wrote it.
- **The page sends only what is new,** through the library's hook for shaping a request: the question and the id of the message before it, or the id of a question to answer again.
- **After a stop, a failure, a refusal or a lost connection, the page reads the conversation again** and shows that. A refused question's words go back into the box. Right after a stop the server may not have recorded it yet, so the page asks again shortly until it has.
- **How a turn ended is told from what the API stores about it,** a status and a code, in the page's own words. So a failed turn reads the same while it happens and after a reload.
- **A question that got no answer can be tried again, or reworded.** A new question sent in its place follows the same message, so the old one becomes an earlier version ([ADR 0012](0012-conversations-as-a-tree-of-messages.md)).

**The API's shapes:**

- **The API's description is a file, `apps/api/openapi.json`,** and a Python test fails when the API no longer matches it.
- **The page's types are generated from that file** (`apps/web/lib/api/schema.d.ts`), and a test of the web app fails when they no longer match it. `make openapi` writes both again; then the TypeScript compiler points at every call that no longer fits.
- **The description now says what errors look like.** Every error response has the `Problem` schema, where the framework described a body this API never sends, and each route lists the errors it answers. Operations are named after their functions, such as `start_turn`.
- **The stream's shapes come from the `ai` package itself,** and the recorded streams keep the two sides together ([ADR 0014](0014-turns-streamed-in-the-ai-sdks-format.md)). `@ai-sdk/react` is pinned too: each of its versions depends on exactly one version of `ai`.

**The components:**

- **Written in this repository,** with Tailwind CSS, on the browser's own elements where they do the job: a reasoning block is a `<details>`, the message box a `<textarea>`.
- **Libraries come in as dependencies:** SWR for reading from the API (the AI SDK already depends on it), and Streamdown for showing markdown, in the next step.

## Measured

On a MacBook Pro (M1 Pro, 16 GB) with `qwen3.5:4b-q4_K_M`, through `make dev` and Caddy, in a headless Chromium, on 2026-11-13:

| | First words on the page | Whole answer |
|---|---|---|
| First question, model not loaded | 3.7 s | 5.1 s |
| Follow-up | 0.6 s | 1.4 s |
| A question with reasoning | 0.3 s (the reasoning) | 17.2 s |

After Stop, the page showed what the server had kept within 0.1 s.

## Consequences

- **Easier:**
  - nothing but the API handles a session, and the limits count real clients;
  - the page can't drift from the server: whatever went wrong, it ends up showing what is stored;
  - a change to a route or a schema fails two tests, and then the compiler lists what to fix.
- **Harder:**
  - **a stored conversation appears after the page around it,** with a short "Loading" in between;
  - **the interface is this project's to build and to keep accessible.** Keyboard use and labels are checked in the component tests; a screen reader has not been tried yet;
  - **two recorded files to keep current** after a change to the API, though one command writes both.
- **Not done yet:**
  - answers are shown as plain text. Markdown, code and formulas come in the next step;
  - no list of conversations: an earlier one is reached by its address or the browser's history;
  - no pages for signing up and logging in;
  - the tests run in a simulated browser, against a fake API that answers with the recorded streams. Tests in a real browser, against the real stack, come in a later step. For this step the page was driven by hand in a headless Chromium;
  - an answer that another window started is waited for, not followed word by word, and closing the page stops an answer ([ADR 0014](0014-turns-streamed-in-the-ai-sdks-format.md));
  - two windows opened at the same instant by a first-time visitor each start a guest, and the first loses its session;
  - the interface is in English only.
- **How to reverse it:**
  - **server rendering** can be added for one kind of page, such as a shared conversation, without touching the chat: the API doesn't care who calls it;
  - **ready-made components** could replace these one by one, after changing the rule on copied code. Nothing outside `apps/web/components` knows how they are made.
