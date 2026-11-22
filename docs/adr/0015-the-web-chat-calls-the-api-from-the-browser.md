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

## Revisions

### 2026-11-14: answers are shown as markdown

The first item under "Not done yet" is done. How answers are shown, and what they are kept from doing, is its own decision: [ADR 0016](0016-answers-shown-as-markdown.md).

- **The page no longer scrolls on each new message.** It now watches the size of what is shown, because formatted answers grow after their words have arrived.
- **Why now, and not planned at the start:** following by message was enough for plain text, and what formatted answers need wasn't known until they were on the page.

### 2026-11-14: a question sent before the page has read the conversation again

A question typed right after an answer had failed could be refused with "This conversation has no such message", and went back into the box.

- **The cause.** A failed answer's stream begins like any other, so the page holds an answer for it. The server stores no answer of which nothing arrived ([ADR 0014](0014-turns-streamed-in-the-ai-sdks-format.md)). The page finds that out when it reads the conversation again, as decided above, and until then it sent the next question as following the answer that doesn't exist.
- **Why it surfaced only now.** The gap is as long as one request takes: a few milliseconds on one machine, more over a slow connection. The component tests always waited for what the server kept before they went on, because that is what they assert on, and nobody types that fast. The first tests in a real browser, against the real stack, do ([ADR 0017](0017-end-to-end-tests-on-the-real-stack.md)): one of them failed in three runs of four.
- **The fix.** The page works out what a new question follows from what it holds, without waiting: an answer with nothing in it is left out, and then a question without an answer, which the new one replaces. A component test sends the second question while the server's reply about the first is still held back.

The tests in a real browser, listed under "Not done yet", exist since the same day.

### 2026-11-14: a conversation that began on this page is read again once the page has left it

Someone asks a first question, opens another conversation, and comes back to the first. The page showed it empty, under its own address. A question asked there would have become a second first question, beside the real one.

- **The cause.** A conversation that began on this page isn't loaded from the server: its messages are already there. The page told "began here" by the id alone. But what the page holds of a conversation is gone the moment it shows another one, and the id stayed "began here" for as long as no new chat was started.
- **Why it surfaced only now.** Until there was a list, the only way back to a conversation was the browser's Back and Forward. The tests went Back to a conversation that had been loaded, and to one from before a New chat, which gives the next conversation a new id and so hid the defect. Forward into one that began here was never tried. The list of conversations ([ADR 0018](0018-the-list-of-conversations-is-the-servers.md)) makes leaving and returning one click each. The defect was found while building it, by reading how the page decides what to load, and then shown by a test written for it, which failed.
- **The fix.** The page gives the id up as soon as the address names another conversation. From then on that conversation is read from the server like any other. A component test goes Back and then Forward, and a test in a real browser leaves and returns through the list.

Two more things changed with the list:

- **The list of conversations,** under "Not done yet", exists: [ADR 0018](0018-the-list-of-conversations-is-the-servers.md).
- **The notice for an answer that is being written elsewhere** no longer says "in another window". It can also be this page's own answer, for the moment between leaving a conversation and the server recording that this stopped it.

### 2026-11-14: the one screen shows more than the chat

"No pages for signing up and logging in", under "Not done yet", is done: [ADR 0019](0019-accounts-in-the-one-screen.md). They are not pages but places in the same component, for the reason given above for conversations: nothing loads between them.

- **`ChatApp` now reads from the address what to show:** a conversation, or one of `/login`, `/signup` and `/account`.
- **A conversation that began on the page is read from the server again after any of these was shown,** not only after another conversation. The rule of the revision above is the same; what can take a conversation's place on the page is more.
- **"A first visit starts a guest session" still holds,** for every address. What is new is that the session can change during a page's life, and that the page then forgets what it had read.

### 2026-11-16: Stop is a request, and leaving is not a stop

An answer is now written at the server in a task of its own, whether or not a page reads it ([ADR 0022](0022-turns-that-run-apart-from-their-request.md)). Two sentences above no longer hold.

- **"Right after a stop the server may not have recorded it yet, so the page asks again shortly until it has."** Closing the connection was the stop, and the page then waited for the server to notice: up to ten looks, 300 ms apart. Now the page stops reading, reads the conversation, and asks the API to stop the turn that it says is running. The API answers once that is recorded, so the next look at the conversation is the last. The waiting loop is gone.
- **Why the page reads the conversation before it stops anything.** It doesn't always know the turn: an answer of which nothing has arrived yet has told the page no id. The server knows what is running. So Stop goes one way, whatever has arrived.
- **The chat library ends its reading whenever its component leaves the page,** and reports that the same way as a Stop. The page tells the two apart by remembering that Stop was pressed, and forgets it once the library is done: an answer that ended by itself at that moment must not leave a stop waiting for the next one.
- **"An answer that is being written elsewhere"** is now also, and mostly, the page's own answer after a reload or a look at another conversation. The page still looks every second and a half until it is done, and then reads the list and the usage counts, which changed without it. Reading along again is the next step.
- **The rule itself stands:** after anything but a clean end, the page shows what the server has. A stream that ends cleanly and says "stopped" is a clean end, and needs no second look.

### 2026-11-17: the page reads along again

"The page still looks every second and a half until it is done… Reading along again is the next step", in the revision above, has become this ([ADR 0022](0022-turns-that-run-apart-from-their-request.md), revision of the same day):

- **An answer that is being written, and that the page isn't reading, is read along with.** The page asks the library to join the turn's stream, by the turn's id, and the words arrive as they are written, with Stop, as for an answer the page asked for.
- **The look every second and a half is the fallback,** for a turn whose stream can't be read: gone, refused, or lost on the way. The page tries once for each turn, and doesn't try that turn again while its conversation is on the page.
- **The rule above still holds:** a joined stream that ends in an error is no clean end, and the page reads the conversation.
- **One notice no longer stays until it is dismissed:** the one that said the server can't be reached when the page's own answer broke off. The page then reads along with that answer again, and the notice goes once the page reads along with an answer: that one, or a later one. Every other notice stays as before.

### 2026-11-21: the page's frame is there before the session

#### Context

The server renders the frame of the page, as decided above, and the browser paints that HTML before any of the app's scripts have run. So the server's guess is what people see first. It knows neither the window's width nor whether this browser put the list of conversations away, which the browser's own storage holds.

The page first painted a header with "Actio", and "Starting…" where the chat would be. When the session arrived (`me` known, in `components/chat/chat-app.tsx`), three things moved what was already on the page:

- **the list's button appeared before the title,** which moved from x=12 to x=62;
- **the buttons are taller than the title:** the header grew from 45 to 51 px, and everything below it moved 6 px;
- **in a wide window the list appeared,** 288 px wide, and pushed the chat from x=0 to x=288.

Measured on 2026-11-21 on a production build of `34db4ad`, in Chromium 158, Firefox 159 and WebKit 27.2. In a narrow window the title and the header moved the same way in all three. Chromium's layout-shift score, where over 0.1 is no longer good and over 0.25 is poor:

| Window | Layout shift |
|---|---|
| 1440×900, at `/` and at `/c/<id>` | 0.1904 |
| 1280×720, the end-to-end tests' window | 0.2115 |
| 1440×900, the list put away: the header alone | 0.033 |
| 390×844 | 0.0565 |

And `/favicon.ico` answered 404 at every load: the app had no icon.

#### Options considered

**For someone who put the list away,** which only the browser knows:

1. **A script in the page's head marks `<html>` before the first paint,** and CSS decides whether the list has a place.
    - Pro: right from the first paint, for everyone.
    - Con: a script written into the page, the one the app writes there itself, which a Content-Security-Policy will have to allow.
2. **Read the switch once the page's scripts run,** and accept one moment in which the list's place shows and goes.
    - Con: a shift as large as the one being removed, for exactly those people.
3. **Keep no place for the list,** and put only the header right.
    - Con: leaves most of the shift in a wide window: 0.19, less the header's 0.033.
4. **Keep the switch in a cookie that the server reads.**
    - Con: every page would be rendered for each request, and the Next.js server would have a part in the session, against the first decision above.

**For the header:**

1. **Placeholders of the buttons' size.**
    - Con: they drift from the buttons when those change.
2. **The buttons themselves,** there before the session, and hidden until it comes.

#### Decision

**What arrives with the session fills places that were already there. Nothing that is on the page before the session moves when it arrives.**

- **The header's two buttons are always in the page,** "Conversations" and "New chat": the same elements before the session and after, so their size can't differ. Until the session comes they are disabled, `aria-hidden`, and invisible (`visibility: hidden`, which keeps their box). Until then the list's button says nothing of whether the list is shown: no `aria-expanded`, no `title`.
- **The list has a place before the session:** while nobody has a session, an empty `div` of the list's width, edge and colour stands where the list will be, `aria-hidden`. Not an `aside`: a landmark with nothing in it would be announced. CSS alone shows it, in a wide window only (`hidden md:block`), since the page's own reading of the width says "wide" on the server. It stays for as long as nobody has a session, also when none could be started. Its width, edge and colour and the list's come from one constant.
- **Someone who put the list away has no place for it, from the first paint.** One script in the page's head (`app/layout.tsx`) runs while the HTML is read, and sets `data-list-away` on `<html>` when the browser's storage says the list was put away. It never throws: storage can be off limits. `<html>` has `suppressHydrationWarning`, since React doesn't know that attribute: without it, React's development build says at every load that the page it takes over doesn't match, for whoever put the list away. A variant of the styles, `list-away:` (`html[data-list-away] &`), hides the place, and outweighs `md:block` by its selector, whatever the order of the built stylesheet.
- **The page keeps the mark in step with the switch** from then on, at its start and at every change. After a log-out the place is shown again with no page loaded in between, and must say what the switch says now. Next.js's guide also warns that React's StrictMode, in development, clears an attribute of `<html>` that React doesn't manage. Under `next dev` of this version it didn't: the mark stayed with this upkeep taken out. The log-out is the reason.
- **What the server sent is what the browser's first render makes.** The remembered switch is read in that first render, and React doesn't repair an attribute that differs from the HTML it takes over. The window's width is read otherwise, with the server's answer first and the browser's right after: it can't differ, but what hangs on it would change in front of the reader, a moment after the first paint. So nothing rendered before the session depends on the switch, the window's width, or whether the list lies over the chat: CSS and the mark decide.
- **One module holds what the script and the page must agree on,** `lib/frame.ts`: the name the switch is stored under, the attribute, the script's text built from both, and the function the page sets the mark with. The layout, which only the server renders, imports it.
- **This is the one script the app itself writes into a page,** and its one use of `dangerouslySetInnerHTML`. Next.js writes scripts of its own into every page. The rule against that (`AGENTS.md`, [ADR 0016](0016-answers-shown-as-markdown.md)) is about a model's words reaching the page; this is a constant written here.
- **The app has an icon:** `app/icon.svg`, and `app/favicon.ico` with the same drawing at 16, 32 and 48 px, for whatever asks for the default address. Next.js's convention for those files turns them into two links in the head.

**Tests:**

- in the page's component tests: the HTML the server sends holds both buttons, out of reach, and the list's place (`app/page.test.tsx`); the layout sends the script, and tells React to let its mark be (`app/layout.test.tsx`); the very text it sends, run in the simulated page, marks `<html>` only when the switch is on, and throws nothing when storage is off limits (`lib/frame.test.ts`); the server's HTML is hydrated as three visitors, with nothing remembered, with the list put away and in a narrow window, and none may cause a recoverable error or an error in the console, which a control shows the test would see (`components/chat/frame.test.tsx`); the buttons are there and out of reach until the session comes (`components/chat/chat-app.test.tsx`); the mark follows the switch, also in StrictMode (`components/sidebar/sidebar.test.tsx`);
- end to end, in a real browser (`e2e/frame.spec.ts`), with the session's answer held back until the test lets it go: the header, the title and the chat have the same boxes before the session and after, and the layout shifts that Chromium measures from before the page's first script add up to less than 0.01: in a wide window and in a narrow one, at `/` and at `/c/<id>`, and in a wide one with the list put away, at `/`. Before the session the buttons can't be seen or used. The icon's addresses answer, the default one and those of the two links (`e2e/icon.spec.ts`). The narrow window is only narrow: with a phone's profile, touch emulated, Chromium marks each shift as one that followed a person's input, and leaves it out of the sum. The boxes are compared for that reason too.

#### Measured

After, on 2026-11-21, on a production build of this change, in the same three browsers: at `/` and at `/c/<id>`, in a wide window, a wide one with the list put away and a narrow one, the title, the header and the chat keep their places. Chromium's layout shift is 0.0007 in a wide window and 0 in a narrow one: what is left is the group of links at the header's right, which grows leftwards when "Sign up" and "Log in" arrive. No hydration error in any of them, and under the development server (`next dev`) none of the three says anything about the page it takes over; with `suppressHydrationWarning` taken off `<html>`, each of them does, for whoever put the list away.

#### Consequences

- **A Content-Security-Policy, when there is one, has this script to allow beside Next.js's own.** Next.js writes inline scripts of its own into every page, so the policy needs an answer for those anyway. Its guide gives two: nonces, which are impossible for a page that is rendered before anyone asks for it, or a policy that allows inline scripts. This one falls under whichever is chosen, and being a constant, it could also be allowed by its hash.
- **The list's place and the list share their width, edge and colour through one constant,** so a change to one is a change to both.
- **The header's buttons exist before the session.** Anything that looks for them has to look as a person does, by their role, which skips them while they are hidden.
- **The icon is a placeholder drawn for this project,** which the maintainer may replace.
- **Not done:** "Sign up", "Log in" and the link to an account have no places before the session. They appear at the header's right, beside "New chat", and move nothing that was visible.

**Why now, and not at the start:** the page began as a header and the chat, and the parts that depend on the session came in later steps: the list ([ADR 0018](0018-the-list-of-conversations-is-the-servers.md)) and what the header says of the session ([ADR 0019](0019-accounts-in-the-one-screen.md)). The shift they cause was first measured in the performance runs of 2026-11-20.

### 2026-11-21: the page is laid out before its sizes are watched

#### Context

The page keeps the end of a growing answer in view by watching the size of what is shown (`components/chat/follow.ts`; the revision of 2026-11-14 above): a `ResizeObserver` on the conversation and on the element that scrolls it.

In WebKit, Safari's engine, every page load reported "ResizeObserver loop completed with undelivered notifications" as an error of the page. Measured on 2026-11-21, on a production build of `9e7dfbf`, in WebKit 27.2: at 15 of 15 visits when the page loads at `/`, and at 6 of 6 under `next dev`. Never at a new chat, a conversation opened from the list, or a reload at a conversation, and never in Chromium 158 or Firefox 159.

Nothing on the page goes wrong by it: the observer is told again at the next frame. But it is an error in the console of every visit in Safari, and it would fail any test that takes an error of the page's scripts for a failure, as the end-to-end tests do.

**The cause,** read from what the observer was told, in a build that logs it: WebKit's first word to the new observer was 0×0 for both elements, their sizes from before they were laid out, since they had come onto the page in that same frame, when the session arrived. It then laid the page out, found both changed, told the observer of the conversation again, and could not tell it of the scroller, the outer of the two, in the same pass: which is what the error says. Chromium's first word already has the sizes as laid out.

#### Options considered

The first three were built, and each loaded 15 times in WebKit:

1. **Lay the page out before the sizes are watched:** one read of the scroller's box, before `observe`. The error at 0 of 15 loads.
2. **See to what is on the page at once, and start watching with the next frame.** 0 of 15.
    - Con: a frame in which nothing is watched, and the first move to the end becomes a call of its own.
3. **Start watching in a layout effect** instead of an effect. 14 of 15: no help.
4. **Go to the end a frame after a change of size,** which was first taken for the remedy. Not counted over 15 loads.
    - Con: the error has nothing to do with scrolling, as what the observer was told showed, and the end would fall a frame behind what is written.

#### Decision

The first.

#### Measured

After, on a production build of this change: the error at 0 of 15 page loads in WebKit, and at 0 of 6 under `next dev`; none in Chromium and Firefox, as before. The end of a growing answer stays in view as it did: below the screen in none, one or two of an answer's painted frames, 211 to 759 of them, in the three browsers, before and after, for prose, for 120 lines of code, and for both in one answer.

#### Consequences

- **One layout is forced when a conversation appears on the page,** which the browser does in that frame anyway.
- **The end-to-end tests can't see this error:** they run Chromium, which never had it ([ADR 0017](0017-end-to-end-tests-on-the-real-stack.md)). A component test holds the order of the two calls (`components/chat/follow.test.tsx`); what WebKit makes of it was measured by hand, and would have to be again after a change to how the hook starts.

**Why now, and not at the start:** the page was first run in WebKit in the performance runs of 2026-11-20.

### 2026-11-21: a long conversation opens newest first

#### Context

A stored conversation came onto the page in one render: every message, and the markdown of every answer, before the browser could draw another frame. Measured on a production build of `c406a1d`, in a window of 1440×900, with conversations of 100 and of about 300 exchanges whose answers are 110 to 260 words with headings, lists, code and tables, in Chromium 158, Firefox 159 and WebKit 27.2:

| Opened from the list | Chromium | Firefox | WebKit | Chromium at a quarter of the CPU's speed |
|---|---|---|---|---|
| 100 exchanges: longest time between two frames | 183 ms | 251 to 258 ms | 238 to 260 ms | 750 ms |
| 300 exchanges | 350 to 367 ms | 475 to 567 ms | 513 to 523 ms | 1,467 ms |

Opened by its address, with a reload or a link, the same: 133 to 242 ms at 100 exchanges, 383 to 650 ms at 300, and 583 and 1,500 ms at a quarter of the speed. The first message was on the page 245 to 359 ms after the click at 100 exchanges, and 444 to 751 ms after it at 300.

And at every opening, in all three browsers, one frame was painted with the conversation at its top, its end 126,385 px below the screen at 100 exchanges and 417,483 px at 310. The next frame was at the end. The page went to the end when the `ResizeObserver` of `components/chat/follow.ts` first reported, and that observer is made in an effect, which React runs after the browser has painted what was committed.

#### Options considered

1. **One render, as it was.**
2. **One transition around the whole conversation.** Measured in the performance runs of 2026-11-20: the longest stop 83 ms, 326 ms at a quarter of the speed.
    - Con: the opening took 659 ms instead of 526 ms. The answers of the list, of the usage and of the models, arriving meanwhile, each start the interrupted render again from its beginning, and nothing of the conversation is on the page until all of it is done.
3. **Newest first, the older messages in steps behind them, each step a transition drawn straight onto the page.** Built first, and measured: its frames were as short as the fifth's, under "Measured" below.
    - Con: in Firefox and WebKit a reader's own scroll is lost while the older messages come in. Measured on a production build, with a conversation of about 300 exchanges: one wheel notch at a random moment of the filling in was kept 3 of 16 times in Firefox and 6 of 16 in WebKit; otherwise the reader was at the end afterwards, as if the wheel had not been turned. A swipe of thirty small wheel events, 600 px in all, ended 0, 0 and 600 px from the end in three tries in Firefox, and 460 to 600 px in WebKit. A Page Up in WebKit moved the reader 633 or 0 of 701 px. Chromium lost nothing, and on a conversation that was whole all three kept everything.
    - The cause, as far as it was measured: a browser scrolls on a thread of its own, and the page hears of it a frame later. Each step changes the page above the reader and moves the scroller, and in Firefox and WebKit that overrides a scroll that is under way. It is not only the page's own writing of `scrollTop`: a build that wrote it only where the browser had not kept the place itself wrote it once in a whole opening, the first move to the end, since the browsers' own anchoring kept the place at all 30 steps, in all three, and it still lost wheel turns.
4. **The same in plain steps, with a timer between them.** Measured in Chromium at a quarter of the CPU's speed, at 300 exchanges: steps of 20 left 24 frames over 50 ms, the longest 100 ms; steps of 10 left 4, of 67 ms at the longest; steps of 40 left 15 over 100 ms. Transitions left none over 50 ms: React draws a transition in slices, and only its commit is one piece. At full speed the two don't differ.
    - Pro: a step lands in a bounded time, whatever else goes on.
5. **Newest first, each step drawn out of sight as a transition, and shown a step at a time only while the reader is still.**
6. **The third, writing `scrollTop` only where the browser did not keep the place.** Built and measured in the three browsers.
    - Con: no help. And in WebKit a Page Up then left the reader 12,000 to 48,000 px away: WebKit's own anchoring doesn't hold while it animates a scroll of the keyboard's.
7. **The third, taking no step while the reader scrolls,** the steps still drawn straight onto the page. Built and measured.
    - Pro: swipes kept whole in all three.
    - Con: a lone notch was still lost 1 of 3 times in Firefox and 2 of 3 in WebKit, by the step that was already under way.
8. **The fifth, showing what is drawn less often and in larger pieces,** at most every 150 ms. Built and measured.
    - Con: no better than a step at a time: in WebKit 12 of 16 notches kept, against 11 of 16.
9. **Draw only what is near the screen,** a windowed list. Not built.
    - Con: the browser's own search finds only what is drawn, the scrollbar is a guess for messages whose height is unknown, and keeping the end in view and a reader's place become that code's.
10. **Read the conversation from the server in pages,** the older ones when the reader scrolls up. Not built.
    - Con: the same loss for the browser's search, and a change to the API. And reading isn't what takes the time: drawing is.
11. **`content-visibility: auto` on each message.** Not built.
    - Con: it skips the layout and painting of what is off the screen, not React's rendering and the markdown, where the time goes.

#### Decision

The fifth. **The newest messages are on the page at once, at the conversation's end from the first painted frame. The older ones are drawn behind, out of sight and without holding the page up, and shown while the reader is still. What the reader looks at never moves by it, and a scroll of theirs is never taken from them.**

- **Newest first** (`components/chat/chat.tsx`). A `Chat` that starts from a stored conversation of more than 20 messages draws its newest 20 (`DRAWN_FIRST`), and holds the older ones back. They are drawn in steps of 20 (`DRAWN_EACH_STEP`), the newest of them first.
- **What is held back is a count of the conversation's oldest messages,** and only what it had when it came onto the page. A question, an answer that arrives, and whatever the conversation holds beyond that count when it is read again from the server (`resync`), are drawn and shown as they come, as before. Only the drawing and the showing start later. What a new question follows, whether the conversation has an answer yet, how its last turn ended, whether it is empty and which message is being written still read every message; which message is being written is counted in the whole list. A conversation that is gone when it is read again, an empty list, throws nothing, and takes both counts to nothing with it: a question asked next is the first message of a new conversation, at the place in the list where a step was held back, and is shown as it comes.
- **Drawn out of sight, each step a transition** (`startTransition`), begun from an effect once the step before it is committed. What is out of sight changes nothing the reader sees or scrolls, so drawing doesn't wait for them. The next count is worked out from the one on the page, not by a function of the state before it: React's StrictMode, as under `make dev`, runs the effect twice, and the second run takes no second step.
- **Each step's messages are in an element of their own,** which stays their parent for good: a message that moved to another parent would be drawn anew. It is keyed by where its step begins, and has the `hidden` attribute until it is shown. The attribute, not a class: it is what the browser and the queries by role go by, and the simulated page of the component tests has no stylesheet. It lays its messages out as the log lays out its own children (`flex flex-col gap-6`), so the page looks as it did. Not `display: contents`: keeping the reader's place needs the position of the first thing shown, and an element without a box has none. The newest 20 are in such an element too, shown from the start; a step that has no messages left has none.
- **Shown while the reader is still,** a step at a time, the newest first, each in a task of its own, begun by a timer. A second count says which message is the first that is shown. While something is drawn and not shown, an effect sets a timer whose task asks `useFollow` how long until the reader is still (`untilStill`). If that isn't 0 it waits that long and asks again; otherwise it reads where the reader stands (`standing`), shows the step with `flushSync`, and tells `useFollow` right after (`addedAbove`). Asking, showing and putting the reader back are one task, so nothing of the reader's comes between them. The timer is cleared when the effect is undone, and the next count is worked out from the current one, as for drawing.
- **20 and 20.** Steps of 10, each a transition, made no frame shorter than steps of 20 beyond the noise (59 against 75 ms in Firefox at a page's first opening, 69 against 68 in WebKit), and took as long in all. And 20 fills a screen even with messages of one line, which 10 may not.
- **`Message` is memoized** (`components/chat/message.tsx`): a step drawn or shown, a key pressed in the box and an answer arriving draw no message again whose message and `writing` didn't change. Measured in the performance runs of 2026-11-20: the time the page's scripts took while an answer arrived under 300 exchanges went from 626 to 243 ms with it.
- **A screen reader says nothing of the older messages.** The log is a live region, whose additions are said. While anything is still to be shown it has `aria-live="off"`; once nothing is, it has no `aria-live`, as before. A conversation that fits in the first 20 never has it.
- **At the end before the first paint.** `useFollow` goes to the end in a layout effect when it starts. The observer stays in the effect, with the read of the scroller's box before `observe`: starting to observe in a layout effect did not cure WebKit's error (the revision before this one).
- **The reader's place holds when messages are shown above, measured, not inferred.** Browsers keep a reader's place by themselves (`overflow-anchor`, which all three support), but none does at `scrollTop` 0, the top of what is shown, where a reader who wants the older messages goes. `standing` gives the scroller's `scrollTop`, and the first thing of the conversation that is shown, the log's first child that isn't `hidden`, with its `offsetTop`. `addedAbove` takes how far that thing moved down, sets `scrollTop` to what it was plus that, and moves the hook's note of where the reader last was (`top`) by as much. Where the browser has already moved the scroller, that sets the value it has. Not the growth of `scrollHeight`: the change that shows older messages can let an arriving answer grow at its end at once. Seen in the throwaway that showed with `flushSync` and took the growth: a reader who had scrolled up slipped by 24 px, one line, once an opening, in all three browsers.
- **Why `top` moves along.** The hook tells that the reader went up by comparing `scrollTop` with `top`. When messages are added above between a reader's scroll and the browser's `scroll` event, `scrollTop` has grown by what was added, so it is larger than `top` although the reader went up: the hook took that for down, kept following, and pulled the reader back to the end at the next change of size. In the first throwaway, before `top` moved along, a reader who had scrolled up 700 px was back at the end, and the message they looked at had moved 834 px.
- **Whether the reader is still.** The hook notes when the reader last did something that scrolls: a `wheel` event, a `touchmove` or a `pointerdown` on the scroller, which is a scrollbar taken hold of too; a key that scrolls (`PageUp`, `PageDown`, `Home`, `End`, `ArrowUp`, `ArrowDown`, space), pressed anywhere, since those keys scroll the conversation wherever the keyboard is, heard on the window while it is captured; and a `scroll` event that finds the reader a pixel or more from where the hook last noted them. The page's own moves leave the note where they put the scroller, so they don't count. The reader is still once 150 ms have passed since (`STILL_AFTER_MS`): longer than the time between two events of one gesture, of a key held down, or of the frames of a scroll the browser animates itself, and shorter than anyone notices. The time is read with `Date.now()`, so a test sets it (`tests/clock.ts`), and a clock that went backwards counts as still. The hook hands out one function for it: how long until the reader is still, 0 when they are.
- **A wheel turned up leaves the end at once.** A `wheel` event with `deltaY < 0`, where there is something above (`scrollTop > 0`), ends the following then, not when the browser says that the reader scrolled: until then a change of size would take the reader back to the end, over the scroll under way. Not with Ctrl held: that makes the page larger and scrolls nothing, and two fingers moved apart on a trackpad are reported the same way.

**Tests:**

- in the page's component tests (`components/chat/newest-first.test.tsx`), with a stand-in for the code that draws markdown, which notes each text it drew once React has put it on the page, in order, with the `writing` it was given, and can wait for a promise the test keeps (`use`) for one older answer, which keeps that step from being drawn for certain:
    - a conversation of 50 messages is drawn newest 20 first, then the 20 before them, then the first 10, also under StrictMode, and ends with every message on the page once, in its order;
    - while the reader scrolls, by a wheel or a key, and the test's clock stands still, every older message is drawn and none is shown, and none is found by its role; once the clock has moved on, they are shown a step at a time, the newest first, and each was drawn once, showing included;
    - its log has `aria-live="off"` from the moment it is on the page, and none once all are shown; a conversation of 20 messages never has it;
    - each stored answer is drawn once by the time all are shown, and none again for typing, a question and its answer;
    - an answer that arrives while older messages aren't all shown is drawn as arriving;
    - a question asked while older messages are held back follows the conversation's last message, its answer shows, and the older messages still all arrive. The app is opened inside a Suspense boundary there (`open(path, { suspense: true })`): a step that waits and isn't a transition would put the boundary's fallback in the page's place, and each of those tests says that it stayed away. Without a boundary, React 19.3 leaves the page as it was for any update that waits, and throws nothing, so a test without one could not tell;
    - a conversation read again while older messages are held back ends whole, and one that is gone by then holds nothing back, at once, and shows a question asked next as it comes;
- in the hook's tests (`components/chat/follow.test.tsx`), with made-up sizes:
    - the page is at the end when it is first painted, before any size was reported;
    - messages shown above leave a reader at the end at the end, and a reader elsewhere at what they read, with the button to the newest message; what grew at the end in the same change doesn't move the reader, which fails when `addedAbove` takes the growth of `scrollHeight`; and a reader whose place the browser kept is moved no further;
    - a scroll up that the browser tells of only after messages were shown above is a scroll up, after which a change of size leaves the reader where they are. That one fails when `top` doesn't move along;
    - each thing that counts as the reader scrolling does, and the page's own moves, a key that doesn't scroll and a scroll of less than a pixel don't; the reader is still 150 ms after, by the clock, and not before, and with a clock that went backwards;
    - a wheel turned up ends the following at once, so that a change of size before any `scroll` event leaves the reader; one turned up with nothing above, turned down, or turned with Ctrl held doesn't;
- end to end, in a real browser (`e2e/newest-first.spec.ts`), with a conversation of 60 exchanges whose answers each hold a small table, made through the API from the page's own session, and the page watched from inside at the start of each frame, counting the messages that are shown:
    - opened from the list, the page is at its end at every frame from the first that shows a message, and in the end all messages are there and the log has no `aria-live`;
    - opened by its address, its first frame is at the end;
    - a reader who goes to the top of what is shown once the first frame has begun, in a task of its own, sees the message there move a pixel at the most, at every frame until all messages are in, and is offered "Go to the newest message".

    In each, the first frame showed less than the whole conversation, or the reader went up while messages were still to come: the control that the test saw them come in. No test says how long a frame took: on a shared machine that says little. And no end-to-end test turns a wheel: those tests run Chromium, which kept every scroll on the first build too.

#### Measured

On a production build of this change, in the same window and browsers. By then the larger conversation had grown to about 330 exchanges, its newest fifteen each with an answer of 1,500 words, from the runs with an arriving answer below. Two runs each, opened from the list:

| | Chromium | Firefox | WebKit | Chromium at a quarter of the CPU's speed |
|---|---|---|---|---|
| 100 exchanges: longest time between two frames | 33 to 50 ms | 74 to 75 ms | 69 to 76 ms | 267 to 333 ms |
| 330 exchanges | 17 to 33 ms | 50 to 51 ms | 34 to 45 ms | 117 to 150 ms |
| 100 exchanges: first messages shown after | 116 to 124 ms | 121 to 138 ms | 123 to 133 ms | 346 to 425 ms |
| 330 exchanges | 106 to 118 ms | 125 to 133 ms | 107 to 198 ms | 234 to 288 ms |
| 100 exchanges: all shown after | 320 to 328 ms | 496 to 551 ms | 506 to 519 ms | 1,377 to 1,502 ms |
| 330 exchanges | 855 to 921 ms | 1,311 to 1,326 ms | 1,300 to 1,347 ms | 3,309 to 3,465 ms |

The 100-exchange conversation was each run's first opening after a page load, which is where its longer frame comes from. At a quarter of the speed the one render, measured again once the larger conversation had grown, had taken 900 ms at 100 exchanges and 1,717 ms at 314. Opened by its address: 17 to 75 ms at the longest at full speed in the three browsers, and 183 to 200 ms at a quarter of the speed.

**Where the page stands.** A reader who scrolled up 700 px, or to the very top, as soon as the first frame had been shown: the message they looked at moved 0 px until all messages were shown, in all three browsers, in every run. No frame began with the conversation anywhere but at its end, with one exception: in one of Firefox's eight openings a frame began 134 px from the end, before the page watched its size, and the next was at the end. Its cause was not looked for here. WebKit's "ResizeObserver loop" error (the revision before this one) was counted again, since the hook now starts with a move to the end: at 0 of 15 page loads, new chats, conversations opened from the list and reloads.

**A reader's own scroll,** with the conversation of about 330 exchanges, while the older messages came in. Sixteen tries a browser for the lone notch, at random moments, where the first build (the third option) had kept 3 in Firefox and 6 in WebKit:

- a lone wheel notch: kept 16 of 16 in Chromium and in Firefox; in WebKit 9 and 12 of 16 in two rounds, and 7 of 16 while the machine was busy with other work;
- a swipe of thirty small wheel events: kept whole in all three, 9 of 9;
- a Page Up: kept whole in all three, 12 of 12.

**With an answer arriving** in the conversation as it is opened, 1,500 words at 200 words a second: every older message was shown after 1.2 s in Chromium, 1.6 s in Firefox and 1.7 s in WebKit. At a quarter of the CPU's speed in Chromium, 91 of 331 exchanges were shown when the answer ended after 7.4 s, and all of them 2.9 s later. A reader who scrolled up as soon as the page read along: the message they looked at moved 0 px, in five runs, two of them at a quarter of the speed.

#### Consequences

- **Until the older messages are shown, the browser's own search finds only what is shown,** and the scrollbar shortens as they come.
- **The first messages are on the page sooner, and all of them later than with the one render:** at about 300 exchanges, the first after 106 to 198 ms where they took 444 to 751 ms, and all of them after 855 to 1,347 ms, while the reader is still.
- **While the reader scrolls, nothing older is shown,** however long: what is drawn waits until they have been still for 150 ms. A key that scrolls counts wherever it is pressed, a space typed into the box too, and so does any press on the conversation, as a click that begins to select some text.
- **In WebKit a lone wheel notch that falls together with a step being shown is still lost:** 4 to 7 of 16 at random moments of the filling in, and 9 of 16 while the machine was busy, where the first build lost 10. No write of the page's own is needed for it: a build that wrote the scroller's place once in a whole opening lost them too. Whatever follows the notch is kept, since the page then waits for the reader.
- **Scrolling in Firefox and WebKit is held by no test.** The end-to-end tests run Chromium ([ADR 0017](0017-end-to-end-tests-on-the-real-stack.md)), which kept every scroll on the first build too. It was measured by hand, and would have to be again after a change to how a conversation comes onto the page.
- **While an answer arrives in the conversation being opened, on a slow device the older messages come more slowly.** At a quarter of the CPU's speed, with an answer of 1,500 words arriving at 200 words a second, 91 of 331 exchanges were shown when the answer ended, after 7.4 s, and all of them 2.9 s later; at full speed all were shown after 1.2 to 1.7 s in the three browsers. And React finishes a transition at once that has waited five seconds.
- **`Message` is memoized,** so nothing that is new at every render may be passed to it: every message would be drawn again each time.
- **How long a frame takes is held by no test.** It was measured by hand, in three browsers, and would have to be again after a change to how a conversation comes onto the page.

**Why now, and not at the start:** the page was first measured with long conversations in the performance runs of 2026-11-20. The frame at the top was found while measuring this step, and the scrolls lost in Firefox and WebKit while measuring its first build.

### 2026-11-22: the frame's script stands at the start of the body

#### Context

The revision "the page's frame is there before the session" put one script into the page's head (`app/layout.tsx`): it marks `<html>` before the first paint when the list of conversations was put away. In the HTML that Next.js sends, that script comes after the stylesheet's `<link>`, so the browser's parser stops at it until the stylesheet is in. While the parser stood there, Chromium asked for only one of the page's scripts at a time, which Next.js marks `async`.

Seen directly, on 2026-11-22, in Playwright 1.65 with Chromium 158, Firefox 159 and WebKit 27.2, every script of the app's and the stylesheet held back at a route:

- **with the script in the head,** Chromium asked for 1 of the 7 scripts the document names while they were held; Firefox and WebKit for 7 of 7;
- **with the script as the body's first child,** Chromium asked for 7 of 7 too, within 10 ms.

What it costs, in Chromium, on an emulated phone of 390×844 with the CPU at a quarter of its speed and "slow 4G" (150 ms latency, 1.6 Mbit/s). Five visits each, the builds swapped back to back; medians, the range in brackets:

| | Before the script existed (`b63af23`) | The script in the head (`4e77c01`) | The script at the start of the body |
|---|---|---|---|
| The box can be typed into | 1,869 ms (1,855 to 1,926) | 2,017 ms (2,005 to 2,035) | 1,852 ms (1,838 to 1,860) |
| The same, at a sixth of the CPU's speed | 1,940 ms | 2,120 ms | 1,936 ms |
| The same, over HTTPS with HTTP/2 | 1,853 ms | 2,023 ms | 1,855 ms |
| The first paint: "Starting…" and the frame | 584 ms | 468 ms | 572 ms |

The third column was measured on a throwaway that held this change alone. The code as built was timed against `4e77c01` once more, back to back, five visits each: the box can be typed into at 1,861 ms (1,853 to 1,873) against 2,023 ms (2,006 to 2,035), at a sixth of the CPU's speed at 1,928 ms, and the first paint comes at 588 ms against 472 ms.

Over HTTPS, with a proxy and a throwaway certificate in front, the difference stays: it is not a matter of HTTP/1.1. With the script in the head, the first of the document's scripts was in at 678 ms instead of 500 ms, the last at 1,729 instead of 1,585 ms, for the same bytes (247,625 against 246,687). The first paint came sooner with the script in the head: with the scripts held back, the stylesheet had the line to itself.

Which rule inside Chromium does this was not read in its source. What is written here is what was measured.

#### Options considered

1. **The script in the head, as it was.**
    - Con: on the slow phone the box can be typed into 148 ms later than before the script existed.
2. **The script before the stylesheet, in the head.**
    - Con: React and Next.js place the stylesheet first, and the app can't order them.
3. **An external script that blocks rendering,** in place of one written into the page.
    - Con: a request more, before the first paint. And `blocking="render"`, which would make an external script hold the paint without holding the parser, is not in every browser.
4. **The script as the body's first child.**
    - Con: the first paint comes about as late as before the script existed.

#### Decision

The fourth.

- **The script is the first child of `<body>`** (`app/layout.tsx`), before the page's content. The layout renders no `<head>` of its own any more: Next.js writes the head, with the stylesheet, the page's scripts and the icon's links. In the HTML it sends, React puts a hidden `<div>` of its own before the script; the script still comes before the header and everything else of the frame.
- **The parser waits at it until the stylesheet is in,** as it did in the head. By then it has read the whole head, and Chromium asked for all of the page's scripts at once.
- **It still runs before anything of the frame has been read,** so the mark is on `<html>` before the frame's first paint. The frame does not move: the frame's probe said "nothing moved" in 18 of 18 cases, in the three browsers, in a wide window, a wide window with the list put away and a narrow one, at `/` and at a stored conversation's address. And it hydrates without a complaint in production.
- **Firefox and WebKit ask for all of the page's scripts at once either way.**
- Everything else of the revision "the page's frame is there before the session" holds: the one script the app writes into a page, a constant, its one use of `dangerouslySetInnerHTML`, `suppressHydrationWarning` on `<html>`, and what a Content-Security-Policy will have to allow.

**Tests:**

- `app/layout.test.tsx` held the script in the head, and was changed on purpose: the page holds one script, the head none, and it is the body's first element, before the page's content. It fails when the script is put back in the head, and when something is rendered before it in the body.
- End to end, in a real browser (`e2e/first-load.spec.ts`): every script and stylesheet of the app's held back at a route, the scripts that the document names are read from its own source, without the one marked `nomodule`, and each of them has to be asked for while all are still held; then they are let through, and the box can be typed into. On the code before this revision Chromium asked for one of the seven, and the test failed when its wait was over. The end-to-end tests run Chromium alone ([ADR 0017](0017-end-to-end-tests-on-the-real-stack.md)), which is where it showed.

#### Consequences

- **On the slow phone the box can be typed into about 160 ms sooner** than with the script in the head, and no later than before the script existed: 1,861 ms as built, against 2,023 ms, and 1,869 ms before.
- **The first paint is where it was before the script existed:** 588 ms on the slow phone as built, against 472 ms with the script in the head and 584 ms before it. That is what was given up.
- **What the frame's script does is unchanged,** and so is the frame from its first paint.

**Why now, and not at the start:** the script came in with the revision "the page's frame is there before the session", whose measurements were of the frame: whether it moves when the session arrives. A performance run after the steps of that week compared the slow phone's first visit with the build before them, back to back, and found the box later than before.
