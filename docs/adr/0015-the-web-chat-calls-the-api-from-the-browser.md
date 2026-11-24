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

### 2026-11-23: an answer says what it took

Under each answer that has ended, one quiet line says what it took, in up to three figures: how fast the model wrote, how long the first word took, and what the answer cost, such as "8.5 tokens a second · first word after 1.2 s · 1,234 credits". Each comes from the turn's record, which the page already had ([ADR 0014](0014-turns-streamed-in-the-ai-sdks-format.md)), and which says since the same day when the first word came (ADR 0014, revision "a turn says when its first word came").

- **The server counts, and the page divides.** The tokens, the credits and the moments are the server's, as what answers cost is ([ADR 0021](0021-usage-limits-in-the-page.md)). The page adds up nothing: it divides the tokens by a time, takes one moment from another, and words the numbers (`lib/chat/figures.ts`, which knows nothing of React).
- **How fast:** the tokens written, over the time the model's server says it took to write them (`generation_ms`), which for Ollama is the rate `ollama run --verbose` prints. A server that says nothing of it, as an OpenAI-compatible one, is timed by the server's clock, from the first word to the end. Either way a rate is said only when the writing took a quarter of a second at the least (`SHORTEST_WRITING_MS`). By the clock, a server that sends a whole answer in one piece would otherwise be said to write thousands of tokens a second. And a server's own time is no better over a few tokens: Ollama times what it wrote from the first token to the last, so that two tokens are timed over the one gap between them (measured below). To a tenth, and whole from 10 on: "7.4 tokens a second", "37 tokens a second". No rate without tokens, and none under 0.05 a second, which would say "0.0" of a model that wrote.
- **How soon:** from the question to the first word, reasoning included, by the server's clock: "first word after 240 ms", "1.2 s", "42 s", "1 min 5 s", said through `lib/time.ts`, and never "0 ms". A first word the record places before the question or after the end can't be right, and is left out, with the rate that would be timed from it.
- **What it cost:** the credits the server counted, as English groups them: "0 credits", "1 credit", "1,234 credits".
- **A figure that can't be right is left out,** and a figure the record doesn't have: the page never shows `NaN`, `Infinity` or a negative number. An answer that was stopped says when its first word came and what it cost, and has no rate: its model never said how many tokens it wrote. One that was given up on has none of the three, and then there is no line at all.
- **Always there, not on hover,** as first planned: a phone has no hover, and neither has a keyboard, and what the interface offers is for everyone. It is quiet instead: small, in the muted colour of `app/globals.css`, and it wraps on a narrow screen.
- **A list a screen reader names,** "What this answer took", with an item a figure. The dots between them are drawn, with nothing in them to read. A question gets nothing of the sort. The log is a live region, which says what is added to it, and the line is no news: it says `aria-live="off"`, to be there for whoever looks for it, and not read out after every answer.
- **After the answer's `article`, as its next sibling,** 8 px under it, so that it reads as that answer's. Not inside it: the article is the model's words and nothing else, which is what many tests compare its whole text with, and what the page says about an answer stands after it, as the notice of how its turn ended already did.
- **Only once the answer is over:** not while it arrives, nor while its words wait for the code that draws markdown ([ADR 0016](0016-answers-shown-as-markdown.md)), nor for a turn the server says is still running. And only under words: a model that answered with nothing, and was counted for what it read, gets no line under nothing. The line comes with the record of the stream's `finish` chunk, and reads the same, word for word, once the conversation is read again.
- **Not shown yet:** the model's name; a way to turn the line off.

**Tests:**

- the rules and their words, boundaries included, in `lib/chat/figures.test.ts` and `lib/time.test.ts`; and the recorded stream with every figure, read by the real `ai` package, with them in its metadata (`tests/contract/turn-stream.test.ts`);
- in the page's component tests (`components/chat/figures.test.tsx`), on a recorded stream whose turn has every figure: the line's three items, found by the list's role and name, after the answer's article and not in it; nothing while the answer arrives, also once its `finish` chunk is in and its stream hasn't ended; the same words for the same record stored; a stopped answer's figures; no list for a record without a figure, nor for an answer of no words whose turn was counted (a recorded stream of its own, `no_words.sse`); nothing for an answer read along with until it has ended. And in `components/chat/renderer.test.tsx`, nothing while the answer's words wait for the code that draws markdown;
- end to end (`e2e/figures.spec.ts`), with the fake model at a pace of about a second: the three figures in their forms, the line in view for a reader who followed the answer, and the same words after a reload.

**Measured** on 2026-11-23, in Playwright 1.65 with Chromium 158, Firefox 159 and WebKit 27.2:

- **With the real model,** on the maintainer's own stack (Qwen 3.5 4B through Ollama), a new guest in each engine: "39 tokens a second · first word after 290 ms · 78 credits" in Firefox, and the like in the other two. No line while the answer arrived, the same words after a reload, and no error of the page. The first answer after the model had been unloaded said "first word after 2.0 s": 1.7 s of that was Ollama loading the model. An answer with reasoning first said "first word after 480 ms", which was its first thought's. One stopped after three seconds said "first word after 300 ms · 183 credits", and no rate.
- **The server's clock against the model server's own time,** in those answers, from the first word to the end: 2,073 ms against Ollama's 2,074, 7,712 against 7,708, 1,951 against 1,938, and 2,119 against 2,117. A rate by the clock is as good as one by the model's server.
- **What Ollama says of very short answers,** asked directly, the same model: one token in 0 ms; two tokens in 26 to 27 ms, which is 75 to 77 a second; five in 102 ms (49 a second); eight in 177 ms (45); 16 in 421 ms (38); 64 in 1,659 ms and 142 in 3,640 ms (39). It times its tokens from the first to the last, so the fewer they are, the more the rate says. Hence the quarter of a second for its time too: under it, this model has written fewer than ten tokens.
- **The page when the line appears,** under an answer taller than the screen, in the three engines: a reader at the conversation's end is at its end at every frame after, and the page has gone up by 22 px, as for one more line of the answer. A reader who turned the wheel up while the answer arrived is where they were. The line stands 8 px under its answer, in 12 px type, with dots of 3 px. In a window 320 px wide the three figures take two rows, and the page is no wider than the window.
- **What it costs the page:** nothing that showed. The web app as built against the committed one (`f2b5a1d`), on one stack, the builds swapped back to back:
    - **an answer of 1,500 words arriving at 150 words a second at the end of more than 300 exchanges,** two rounds: no frame over 34 ms in Chromium or WebKit with either build. In Firefox one frame of 134 to 208 ms in every run of both, which is Firefox's own with a page this large ([ADR 0016](0016-answers-shown-as-markdown.md), revision "an arriving answer is drawn at most every 50 ms");
    - **the same at a quarter of the CPU's speed in Chromium,** 14 runs each: a frame over 50 ms in four runs as built (50 to 100 ms) and in one before (67 and 167 ms), each in the middle of the answer and none at its end, where the line appears. The time in scripts (3.9 to 4.5 s) and in layout (151 to 187 ms) was the same;
    - **a conversation of 100 and of 300 exchanges opened from the list,** its longest frame, five rounds each in Firefox and WebKit: Firefox 43 to 75 ms before and 43 to 74 ms as built, apart from the first run after the stack had started (132 and 133 ms, not seen again). WebKit with 100 exchanges 68 to 73 ms before and 68 to 79 ms as built, with 300 exchanges 24 to 56 ms before and 28 to 67 ms as built. Chromium, two rounds: 17 to 50 ms before and 17 to 67 ms as built;
    - **Stop** sends again within 9 to 35 ms with either.
- **Not heard with a screen reader.** `aria-live="off"` is the standard's way to say that an addition to a live region is no news. Chromium's accessibility tree, read through its DevTools protocol, has the list with its name and its three items, and says nothing of how an addition is passed on. Whether a given screen reader stays quiet at it was not tried.
- **Planted,** one at a time in a scratch copy of the tree: 60 mistakes, 16 in what the API records and 44 in the page's arithmetic, its wording and the line. Each fails a test. Three failed none at first, and each got one: a whole rate under ten, which keeps its decimal ("7.0 tokens a second"); an answer of no words whose turn was counted, which gets no line; and a turn given up on before it had written anything, which says nothing of a first word. Two more were no mistakes: guards against a rate and against credits that aren't finite, which no record can hold, and which were taken out.

### 2026-11-23: a turn that left no answer says so

The page says how a turn ended under the conversation's last message: stopped, failed and why, interrupted, cut at a length limit. One ending had no words: a turn that completed, and left no answer. Its model wrote nothing, and ended as a finished answer does. The page then showed the question, an empty answer, and nothing else: no word of what happened, and no way to ask again but to write the question anew. After a reload it showed the question alone.

- **When:** a turn completed, and the conversation has no answer of it that says anything. Either its model wrote nothing: the server stores no empty answer ([ADR 0014](0014-turns-streamed-in-the-ai-sdks-format.md)), so the question stands open. Or it thought, and then wrote nothing: the answer holds its reasoning and no words.
- **What the page says:** "The model gave no answer.", where it says how a turn ended, with "Try again", which asks for another answer to the same question, as after a failure. A model that reached its length limit before a word keeps the sentence of that limit, and can be tried again now too.
- **The page tells, from the conversation.** A turn's record says `completed` either way, and has no member that says whether an answer was left. `outcomeOf` and `canRetry` (`lib/chat/outcomes.ts`) are told whether the conversation's last message is an answer that says anything (`hasWords` in `lib/chat/messages.ts`: text that isn't blank). A thought is no answer.
- **An answer that is over and has nothing in it to read isn't drawn** (`Answer` in `components/chat/message.tsx`, `isBlank` in `lib/chat/messages.ts`). An answer's stream begins with a message for it, and when nothing followed, the page held an empty `article` until it next read the conversation. The message itself stays where it was, as before: a question asked next takes the open question's place. An answer of nothing but blank space, which a model can write and the server stores, is treated the same: not drawn, and no answer.
- **What an answer took still stands under an answer that only thought:** it has something in it to read. Under an answer that isn't drawn, nothing does.
- **What such a turn costs is as it was:** a model that read the question and wrote nothing is counted for what it read, when its server says so ([ADR 0020](0020-usage-limits-on-turns.md)).

**Options considered:**

1. **The API says it:** a member of the turn's record for "left an answer", or a status of its own.
    - Con: the record changes, and every recorded stream with it, for something the conversation already says. And the turn did complete: the model's server made no mistake that Actio could name.
2. **The page reads the conversation again after an answer of nothing,** and shows what is stored.
    - Con: a request more, for a state the page can tell from what it has. It would still need the words for it.
3. **The page tells from what it has.** Chosen.

**Why now, and not at the start:** nothing had produced this ending. The fake model echoes its question, and a real model nearly always says something. The step of the same day, which shows what an answer took, needed a test of an answer without words, and with it a recording of a model that answers with nothing (`no_words.sse`). Run through the page, that recording showed the empty answer.

**Tests:**

- in the page's component tests (`components/chat/chat-app.test.tsx`): the recorded stream of a model that answered with nothing, which says so, draws no answer, and is asked again; a stored question whose turn completed without an answer; a stored answer that holds only a thought, which is asked again too; one of nothing but blank space, which isn't drawn; and a turn that reached the length limit before a word, which keeps its own sentence and can be tried again. An answer with words says nothing of the sort and offers no other try, once it is over;
- one older test rested on the empty `article` this revision no longer draws (`components/chat/newest-first.test.tsx`, a conversation read again after a model failed): its wait for the page to show "what the server has" was a wait for that article to go. It now tells what was read again from what the stream left by the turn's own sentence, which its fake conversation words differently from its stream;
- end to end (`e2e/no-answer.spec.ts`), with the stack's quiet model, which the question's request is given on its way out: the sentence under the question, the same after a reload, and an answer after "Try again".

**Measured** on 2026-11-23:

- **Before:** the four component tests written first failed on the code as it was (`15ddeeb`): no sentence in three of them, and no "Try again" in the fourth.
- **On a real stack, in Chromium 158, Firefox 159 and WebKit 27.2:** the end-to-end stack, its quiet model asked by giving the first request its name. The sentence and "Try again" stood under the question when the answer's stream ended, and again after a reload, at the same place, with no answer drawn. "Try again" got an answer from the default model, and the sentence went. No error of the page.
- **A model that thought and then said nothing,** the quiet model with a `[[think: ...]]` directive, in Chromium: its thought, the line of what the answer took, then the sentence and "Try again", when the stream ended and after a reload.
- **Planted,** one at a time in a scratch copy: 18 mistakes. 17 fail a test. One is no mistake: a running turn that "can be tried again" shows nowhere, since nothing is said of a turn while it runs. One failed nothing at first, blank text taken for words, and got a test. The end-to-end test fails against a build with the sentence taken out.
- **Not seen with a real model:** nothing here makes one answer with nothing on demand.

### 2026-11-23: the page says how full the model's window is

A model reads the whole conversation with each question, inside a window of so many tokens: 8,192 for the default one. A conversation that no longer fits fails its turn, with "This conversation has become too long for the model", and an answer that fills the window is cut ([ADR 0007](0007-local-models-through-ollamas-native-api.md)). Nothing on the page said how close a conversation was to either. Under the box where a question is written, one line says it now:

```text
Context  ▮▮▮▯▯▯▯  39% full · 3,214 of 8,192 tokens
```

- **The server says the figure, and the page divides.** A turn's record says how many tokens of the window its conversation takes after it, `context_tokens` ([ADR 0014](0014-turns-streamed-in-the-ai-sdks-format.md), revision "a turn says how much of the window its conversation takes"), and `GET /api/models` says each model's `context_window`. The page divides the one by the other, and adds up nothing: what a conversation takes follows from what the server sends back to a model, which the page doesn't know (`lib/chat/context.ts`, `contextFill`).
- **Which figure: the newest there is.** The turns, newest first: the one that left the conversation's last question open, as the server reports it, then each answer's turn from the newest message back. The first that has a figure is said. A turn that runs has none, so while an answer arrives the line says the figure from before it, and the answer's once its turn has ended.
- **After Stop, or a turn that failed, the line keeps the newest figure there is.** Nobody counted the stopped answer: its model's server never said what it read and wrote. So the figure is a little under what the conversation takes, by what the stopped answer added.
- **Which window: the model's that answers now,** the one the page sends a question to: the installation's default, or the model that costs nothing, chosen at a usage limit. When the newest figure is another model's, the line says nothing: those are tokens of another window.
- **What it says:** the word "Context", a bar in the usage meter's look ([ADR 0021](0021-usage-limits-in-the-page.md)), and the share in its words: whole percent, rounded down, "under 1%" of a little, and never more than "100% full", also of a figure over the window. Where the line has 384 px of room, the tokens too, grouped as English groups them. From 90 % (`NEARLY_FULL`), the bar and the words take the caution colour, and where the line has 576 px it ends "start a new chat soon". The words take a name of their own in `app/globals.css`, `caution-foreground`: the bar's caution colour is too light for small type on a light page.
- **When it says nothing:** before any turn of the conversation has a figure, while the models haven't been read, and when the newest figure is another model's. Then there is no meter on the page, and its place stays, empty.
- **Under the box, in a place that is there from the box's first render,** one line high, with a figure or without: when the figure comes, changes or goes, nothing else on the page moves, neither the box nor the conversation. Nothing in the line wraps. How much it says goes by the room the line itself has, not by the window's width (a container query): the list of conversations stands beside the chat in some windows and not in others, so that a window 768 px wide leaves the line 448 px, less than a phone held sideways does. Should the words still not fit, the bar gives way first, down to 16 px, and then the words end in "…": they never reach outside the line. Not in the box's own row: in a narrow window "Think before answering" and Send leave no room there. Not a notice: a figure that changes is no news. Not in the log: its children are the messages and what follows them.
- **For a screen reader,** a meter named by the word "Context", from 0 to 100, its value the whole percent, and its text the rest, as in "39% full: 3,214 of 8,192 tokens", and from 90 % "93% full: 7,640 of 8,192 tokens. Start a new chat soon." The words beside it say the same, and are hidden from it, so that it is told once. It is in no live region, and nothing tells a screen reader when the figure changes.
- **Drawn again only when what it says changes,** on plain values (`memo`): not at each piece of an answer that arrives. No test holds that: drawn again, the line says the same, and nothing on the page can tell.

**Options considered, and not built:**

1. **In the box's own row,** beside "Think before answering" and Send.
    - Con: in a narrow window there is no room left in it.
2. **A notice at 90 %,** where the page says what needs attention.
    - Con: a share of a window that grows a little with each answer is no news, and a notice would come again and again. The line says it in the caution colour, where the figure always is.
3. **The turn's peak,** what the model read and wrote together.
    - Con: that is what the window held when the answer ended, its thought included. After an answer that thought, the line would say 17 % and then fall to 2 % after the next plain answer (ADR 0014, revision above).
4. **What the line says decided by the window's width,** the tokens and the words about a new chat from the `sm` width up. It was the step's first build.
    - Con: with the list beside the chat, a window 768 px wide left the line 448 px, and its longest words, with the bar at its shortest, took 440 px in Chromium on macOS. Another font, or a model with a larger window and so longer numbers, would have pushed them outside the line.

**Measured on the built code,** in Chromium, Firefox and WebKit (Playwright 1.65, on macOS), against the end-to-end stack and against a stack with the real model:

- **Nothing moves when the figure comes.** The box and the conversation, read at every frame from the moment a question is sent, are where and as large as they were before the question once the meter shows, in all three engines. The place is 16 px high and 6 px under the box from the first render. While an answer is written the box stands 2 px higher, because Stop has a border and Send has none: that is older than the meter, and not its doing.
- **The meter says the turn's record,** and the same after a reload, in all three. While a second answer arrived it said the first one's figure until the turn had ended, and after a stopped answer it stayed. With the real model, after answers that thought 506, 1,146 and 2,048 tokens, it said 134, 130 and 115 tokens, and the next prompts held 153, 150 and 133 (ADR 0014, revision above).
- **One line at every width.** In windows 320, 360, 375, 390, 412, 430, 600, 768, 1,024 and 1,280 px wide the line was 16 px high, with nothing cut and nothing outside its place. The tokens show from the window 430 px wide, a line of 398 px, where the words end at 343 px with the bar at its full 96. With a window of 1,048,576 tokens, 95 % full, put in the page's way, they end at 382 px of those 398, and at 514 px with the words about a new chat, which show from the window 1,024 px wide, a line of 704 px. The three engines measured the same to the pixel, with this machine's fonts.
- **The small words can be read:** on the light page 5.03 to 1 in the caution colour and 4.83 to 1 otherwise.
- **Not heard with a real screen reader.** Chromium's accessibility tree has a meter named "Context" with its value, and no other node that says the figure.

**Tests:**

- the rule and its words in `lib/chat/context.test.ts`: the newest figure, the open turn's first, a turn without one passed over, another model's, the models not read yet, the rounding, under 1 %, never more than 100 %, and the step at 90 %; and the recorded streams with the member, read by the real `ai` package (`tests/contract/turn-stream.test.ts`);
- in the page's component tests (`components/chat/context-meter.test.tsx`): the place under the box from the start, empty, and the meter in it once an answer has ended, with its name, its values and the words a screen reader isn't told, outside the log and in no live region; a stored conversation's figure as soon as it is shown; the server's figure of an answer that thought, not the two counts together; the figure from before while an answer arrives, and the answer's once its turn has ended; the figure from before after a stopped answer; the figure of a turn that completed and left its question open, stored and as it arrives; the caution colour and the added words from 90 %, and none below; 100 % at the most; no meter for another model's figure once the model that costs nothing answers, nor before the models are read. One older test asked for no meter at all, and now names the usage meters it meant (`components/usage/usage.test.tsx`);
- end to end (`e2e/context.spec.ts`): after a guest's first answer, the figure the API recorded for it, of the stack's 8,192, in the meter's words and the same after a reload, with the box and the conversation where they were before the figure came; after an answer that thought, the record's figure, which is less than what its model read and wrote; and for a conversation that nearly fills the window, one line in a window 320 px wide, with the share alone, in one 768 px wide, with the list beside the chat, with the tokens too, and in one 1280 px wide with all its words, each as high as the others, with nothing in it overflowing or cut.

### 2026-11-23: the box is as large with Stop in it as with Send

Measuring the line above showed something older: while an answer is written, the box where a question is written was 2 px higher on the page, and 2 px taller, than before and after. Stop has a border, and Send had none, so the row the button stands in grew by the border's two widths whenever Stop took Send's place: 94 px against 92 in Chromium and WebKit, 90 against 88 in Firefox. The conversation above lost those 2 px, and got them back when the answer ended.

Send has a border now too, as wide as Stop's. It is transparent, so the button's own colour shows through it: Send's face is 2 px taller and wider than it was, and as large as Stop. The box is 2 px taller than it was at rest, and the same with either button in it.

**Why only now:** the page's frame and the answers were each checked for moving (the revision "the page's frame is there before the session" and what followed), and the box was not: no test compared it across an answer's beginning and end. The meter's own check did, which read the box at every frame.

**Tests:** end to end, in a real browser (`e2e/turns.spec.ts`): the box with Send in it, with Stop in it while an answer is written, and with Send again once the answer was stopped, is where and as large as it was each time. A simulated browser lays nothing out, so no component test can hold it.

### 2026-11-23: the page says which limit an answer reached

**What the maintainer saw:** with "Think before answering" on, the question "huh". Under the thought and nothing else, the page said "The answer reached the model's length limit, and stops here.", beside the meter of the revision "the page says how full the model's window is", which said "1% full · 129 of 8,192 tokens". The turn's record: read 129, wrote 2,048, exactly the model's `max_output_tokens`, reasoning included, `finish_reason` `length`, 7,910 characters of thought and no words. Both statements were true, and the sentence read as if the window were full. Two limits can end an answer, and the page had one phrase for both.

- **The server says which limit** ([ADR 0014](0014-turns-streamed-in-the-ai-sdks-format.md), revision "a turn says which limit its answer reached"): a completed turn's `finish_reason` is `context_window` when the conversation and the answer together filled the model's window, and `length` when the answer reached the longest a single answer may be. The page never works it out from the counts.
- **The page's words,** all in `lib/chat/outcomes.ts`, by the turn's record and by whether the conversation has an answer of it with words (`answered`), as before:

    | The turn completed with | Words? | The page says | "Try again" |
    |---|---|---|---|
    | `context_window` | yes | The conversation has filled the model's context window, and the answer stops here. Start a new chat to go on. | no |
    | `context_window` | no | The conversation has filled the model's context window, and the model wrote no answer. Start a new chat to go on. | no: another try meets the same wall |
    | `length`, `capped_by_usage` | either | as before: This answer stops here: your usage limit left no room for more of it. | as before: only without words |
    | `length` | yes | The answer reached the longest a single answer may be, and stops here. | no, as before |
    | `length`, the turn was asked to think (`turn.thinking`) | no | The model was still thinking when it reached the longest a single answer may be, and wrote no answer. Try again, or turn off “Think before answering” first. | yes |
    | `length`, not asked to think | no | The model reached the longest a single answer may be, and wrote no answer. | yes |

- **No "Try again" at the window's end,** with words or without, and also when a usage limit had left the answer less room (`capped_by_usage`): where what was read and written together reached the window, the window's end came first, or at the same moment, and it is what the page names. Another answer to the same question is asked with the same conversation before it, which already fills the window: it would meet the same wall. A new chat is what goes on, and the sentence says so. At the longest an answer may be, another try can go otherwise, and without thinking it has the whole of that room for its words: the sentence says that too, and "Try again" asks with what the switch says then.
- **The sentence for an interrupted turn changed too,** from "The server was restarted while this answer was being written." to "This answer broke off: the server that was writing it was restarted, or stopped answering." The old one was untrue of a turn given up on while its process was only slow ([ADR 0022](0022-turns-that-run-apart-from-their-request.md), revision "a turn that ends after it was given up on says so"). The new one is true of a shutdown, of a process that died, and of that.

**Why it surfaced only now:** the meter put a second limit on the page, the window's, beside a sentence that named neither. Before it, "the model's length limit" could be read as either, and nothing on the page said otherwise.

**Tests:**

- in the page's component tests (`components/chat/chat-app.test.tsx`): each row of the table, from a stored conversation, with "Try again" there or not; the thought that used up the answer as it arrives too, from a recorded stream (`thought_too_long.sse`), and "Try again" after it with the switch turned off first, which asks with `thinking: false`; the recorded answer that reached its length (`cut_off.sse`), in its new sentence; and the sentence of an interrupted turn wherever it is said;
- end to end (`e2e/no-answer.spec.ts`), with "Think before answering" on and a thought longer than an answer may be on the stack: the thought, the sentence and "Try again", and the same after a reload.

### 2026-11-23: the reader chooses light or dark

#### Context

The page followed the operating system's light or dark setting, and nothing else: `color-scheme: light dark` on its root, every colour a `light-dark()` pair or a system colour (`app/globals.css`), and a diagram, which a script draws, asked the system through the media query (`useDarkScheme` in `lib/scheme.ts`). A reader who wanted a dark page under a light system, or the other way round, had no say.

Measured on 2026-11-23, with a throwaway that held only the stylesheet's rules and the script's line of this revision, built into a scratch stack, in Playwright 1.65 with Chromium 158, Firefox 159 and WebKit 27.2:

- **Streamdown colours code through Tailwind's `dark:` variant.** A token is a `span` with the classes `text-[var(--sdm-c,inherit)]` and `dark:text-[var(--shiki-dark,var(--sdm-c,inherit))]`, and the same pair for backgrounds, in `node_modules/streamdown/dist/chunk-*.js`. They are its only uses of `dark:`. The plugins for formulas and diagrams have none, and the app's own components none (`AGENTS.md` rules it out). Tailwind's own `dark` variant is `@media (prefers-color-scheme: dark)`: it follows the system, not a choice. With `color-scheme` alone, someone who chose Light under a dark system would get the dark theme's code colours on a light page.
- **Tailwind 4.3.5 compiles a variant of the app's own** (checked with its `compile()`), and with it code follows the mark:

    ```css
    @custom-variant dark {
      @media (prefers-color-scheme: dark) {
        &:where(:root:not([data-scheme="light"]) *) {
          @slot;
        }
      }
      &:where(:root[data-scheme="dark"] *) {
        @slot;
      }
    }
    ```

    `:where()` keeps a `dark:` class as weighty as the plain class beside it, as Tailwind's own variant does, so the order in the stylesheet goes on deciding.

- **`color-scheme` by a mark on the root is enough for everything CSS decides.** With `:root { color-scheme: light dark }`, `:root[data-scheme="light"] { color-scheme: light }`, `:root[data-scheme="dark"] { color-scheme: dark }` and the variant above: six visitors an engine (the system light or dark; the choice remembered as nothing, "light" or "dark"), each opening a stored conversation with a block of TypeScript and a diagram. In all 18 cases the page's ground (a pixel of a screenshot), its words (the `h1`'s colour: black, or white, in Firefox `rgb(251, 251, 254)`), the list's place (`lab(98.26 0 0)` light, `lab(8.31 0.62 -2.17)` dark) and the keyword `const` (`rgb(215, 58, 73)` light, `rgb(249, 117, 131)` dark) were the chosen scheme's from the first frame the page's own script could see (in Chromium at 50 ms, before the browser's `first-paint` at 72 ms), and never changed while the page loaded. A mark set while the page was open changed all of them at once, with no reload. Without a mark, the page followed the system when it turned dark; marked light under a dark system, it stayed light.
- **The diagram did not follow in the throwaway,** as expected: `useDarkScheme` asked the media query. Its node's fill stayed the system's (`rgb(236, 236, 255)` in the light theme, `rgb(31, 32, 32)` in the dark one), whatever the mark said. And when the system turned dark with the page open, the diagram was drawn anew in the dark theme: Streamdown draws again when the `plugins` it is given change. So once `useDarkScheme` follows the choice, diagrams do.
- **No frame the page paints is in the other scheme.** Chromium's screencast, every frame it painted across reloads, for a reader who chose the scheme their system doesn't have: with the stylesheet arriving as the stack sends it, 0 of 20 loads showed a frame in the system's scheme, and none of 6 more at a sixth of the CPU's speed. The control, the mark set 60 ms after the page was read instead of by the script, showed 3 to 13 such frames in a run of three loads, so the probe sees a flash when there is one.
- **Before the page's first paint, the browser may show a blank of its own,** with nothing of the page in it. In Chromium 158, its screencast, every frame across reloads, for a reader who chose the scheme their system doesn't have, half of the loads with a light choice under a dark system and half the other way round:
    - **the stylesheet arriving as the stack sends it,** not held back: no load began with a blank frame, and no frame was in the system's scheme (0 of 20);
    - **the stylesheet held back 400 ms at a route,** the throwaway otherwise unchanged, its mark set by the script at the start of the body: 23 of 24 loads began with a blank frame, and in 9 of 24 that blank was in the system's scheme, not the chosen one. Held back 100 ms and 200 ms, a light choice under a dark system alone: 3 of 12 each. Held back 50 ms, the same way: every load began blank, and none in the system's scheme (0 of 12);
    - **the stylesheet held back 400 ms, and a script before it:** the document's HTML rewritten on its way to the browser, so that the head began with a script, before the stylesheet's link, which sets the root element's own `color-scheme` from the remembered choice: 0 of 24 in the system's scheme. 22 of 24 still began blank, in the chosen scheme. The same with the root's background set as well: 0 of 24. The same with a `<meta name="color-scheme">` added for the choice: 0 of 24.

    Every frame in the system's scheme was such a blank: none had anything of the page in it. Not measured in Firefox and WebKit: the probe reads Chromium's own screencast.

#### Options considered

**Where the choice is kept:**

1. **Nowhere: the page follows the system only,** as it did.
    - Con: a reader who wants another scheme for this page than for the rest of their device has no way to get it.
2. **On the account.**
    - Con: the page has to know it before its first paint, before it knows whose session it has. A guest has no account. And it is a property of the device as much as of the person.
3. **In the browser's storage,** like the switch that puts the list away.

**How the page shows it:**

1. **A class or a mark that a script always resolves,** the system's side included: the script asks the media query, and marks the page light or dark.
    - Con: the page then needs a script to follow the system, and its mark is wrong until that script has run: when the system changes while the page is open, until a listener has marked it again.
2. **A mark for a choice only, and CSS for the system:** no mark while the choice is the system's, and the browser follows the system by itself, through `color-scheme` and the media query.

**Where the control stands:**

1. **In the header, behind a menu.**
    - Con: the header is there from the first paint, before the session, and a control that showed the choice would differ from the HTML the server sent (the revision "the page's frame is there before the session"). And three named choices need no menu to open.
2. **On a page of settings.**
    - Con: there is none. The account's page is for accounts, and a guest chooses too.
3. **Under the list of conversations,** which arrives with the session.

#### Decision

The last of each: **the reader chooses System, the default, Light or Dark, under the list of conversations. The choice is this browser's, marked on the page's root before the first paint, and CSS shows it. What a script draws asks one hook.**

- **Three choices, the browser's own.** Kept in `localStorage` under `actio.scheme`: `"light"` or `"dark"`. System is no entry: choosing it removes the entry. Anything else stored there reads as System.
- **A mark on the page's root element,** `data-scheme="light"` or `"dark"`, and none for System. The frame's script sets it before the first paint, as it sets the list's mark (`lib/frame.ts`): still one script, the body's first child, a constant, the app's one use of `dangerouslySetInnerHTML`, now named `FRAME_SCRIPT` for what it does. The storage's key and the mark's name are there beside the list's (`SCHEME_KEY`, `SCHEME_MARK`), and `markScheme` beside `markListAway`. The script never throws: storage off limits leaves no mark, and a scheme entry that is none of the two sets none, and doesn't keep the list's mark from being set. Its names are its block's own, so it leaves nothing in the page's scope.
- **The script stays the body's first child,** and none is added before the stylesheet, though one there would colour the browser's blank before the first paint (Context). The app has no place there: React writes the stylesheet's link ahead of everything the app renders into the head (the revision of 2026-11-22, option 2), and a script in the head after the stylesheet is what that revision moved to the body, for what it cost.
- **CSS decides everything it can,** in `app/globals.css`: `color-scheme: light dark` on the root, `light` or `dark` by the mark, and the variant `dark` above, for Streamdown's code colours. The role colours stay as they were. No component of the app's has a `dark:` class.
- **`lib/scheme.ts` is the one place that knows the choice.** `useScheme` gives the choice and a way to make it, the same answer in every component, kept current. `useDarkScheme` says whether the page is shown dark now: the choice, or the system's setting while the choice is System, which it follows as the system changes. `markdown.tsx` asks it as before, and needed no change. No other code of the page's asks `prefers-color-scheme`, and none but that module and the frame's script reads the key. The styles ask the media query themselves, in the variant above, and so does the icon, which stands in the browser's own frame and follows the system.
- **The mark is the page's truth, and the storage what the browser remembers of it.** The hooks read the mark (`useSyncExternalStore`), so what the components say and what CSS shows can't differ.
- **The mark changes only where the choice changes:** in the script at the page's start, when the reader chooses, when a choice made elsewhere arrives, and once when the page starts to follow those (below). Never from an effect that mirrors a hook: while React takes the page over, a hook built on `useSyncExternalStore` answers with the server's System, and an effect that marked the root with that would take away, for a moment, the mark the script set. The list's mark is kept by such an effect in `ChatApp`, but its switch is read at the browser's first render.
- **A choice made elsewhere reaches the page, whatever is on it:** made in another window of this browser (the `storage` event for the key, and for a storage that was cleared, whose event has no key), or while the browser kept this page in its back/forward cache, where it was told of nothing (`pageshow` with `persisted`). The choice is then read again from the storage, and where it isn't what the page shows, it is marked and the components are told. `ChatApp` listens for as long as the app is on the page (`followChoicesMadeElsewhere`), so this holds with the list put away and no answer on the page.
- **The choice is read once more when the page starts to follow, before the browser paints,** for the two ways the mark and the storage can have come apart by then, of which no event tells the page:
    - **another window chose** after the script at the page's start had read the storage and before the page listened;
    - **React could not take the page over as the server sent it,** as when an extension of the browser's changed the page first. It then draws the page anew, and takes every attribute off the root element as it does (`acquireSingletonInstance` in React 19.3), the script's mark with them. Seen on 2026-11-23, in the three browsers, on a build of this change with one word that differs between the server's page and the browser's: the script wrote the mark, React took it off, with its error 418, and without this reading the page showed the system's scheme for the rest of the visit, with the choice still in the storage and the control saying System. Read in an effect that runs after the paint, the mark was back 1 to 22 ms after React took it off (Chromium 1 to 3 ms, Firefox 2 to 3 ms, WebKit 4 to 22 ms), and in 12 of 18 such loads a frame had found the page without it meanwhile: without the list's mark too, which its own effect put back as late. So both are put back in effects that run before the paint (`useLayoutEffect` in `ChatApp`), the list's as well, which was that way since the revision "the page's frame is there before the session". Then no frame found the page without its marks, in 27 of 27 such loads, nine a browser, and the control says what was chosen.

    Where the mark says what is remembered, which is nearly always, nothing is written, so that the mark still changes at no moment while React takes the page over.

- **Storage off limits,** as in some private windows: the choice takes effect for the page that is open, and isn't remembered. A page shown again from the back/forward cache then keeps what it shows. Nothing throws. A storage that can be read and not written, as a full one, keeps its older entry: the page shows the new choice until it next reads the storage, and the older one from then.
- **The control stands under the list of conversations,** the last thing in the list's column, after the usage meter and the guest's note (`components/sidebar/theme.tsx`): a `fieldset` whose `legend` is "Theme", with three of the browser's own radio buttons, "System", "Light" and "Dark", drawn as one row of three segments in the manner of the sections above it, in role colours only, the chosen one set apart, and an outline around the one the keyboard is on. The browser's own radio buttons give what a choice of one in three needs, without code: one stop for Tab, the arrow keys to move and choose, and for a screen reader a group with a name, and which one is chosen. It is there for guests and accounts, at every place of the app, and in a narrow window in the list that lies over the chat.
- **The keyboard is where the browser puts it.** Chosen with an arrow key, it is on the radio button that was chosen, in the three browsers. Chosen with the mouse, it is there in Chromium and Firefox, and not in WebKit, where a click on the choice's words left the keyboard on nothing. And Playwright's WebKit, as Safari does until it is set otherwise, passed by the group at Tab alone, as by the link before it: Option with Tab reached it, once, and went on from it.
- **The outline goes by the browser's word that the keyboard is in use, and by the group's own.** WebKit 27.2 doesn't say so (`:focus-visible`) of a radio button that an arrow key moved to: the outline was there after Tab and gone after the first arrow. So the group notes that an arrow key was pressed in it, until the mouse or a finger is used in it, and the outline is drawn around the radio button the keyboard is on while it says so, as well as where `:focus-visible` holds. No other key is noted: one held for something else, as for a shortcut, moves nothing in the group. After a click, no outline is drawn, in Chromium and Firefox as before.
- **With forced colours the chosen one is underlined.** Where a system's own few colours take the place of the page's (`forced-colors`, as Windows' contrast themes), the ground that sets the chosen one apart is gone, and its heavier letters alone told it from the others. Seen in Chromium with forced colours emulated: the three had one ground and one colour of words. There the current conversation's row in the list is likewise told by its heavier letters alone, and the usage meter's bar isn't seen at all, its share being said in words beside it. Both are as they were: not part of this revision.

**Tests:**

- the script, run as an element of the page (`lib/frame.test.ts`): the names people's browsers hold; "light" and "dark" stored mark the root with that value; nothing stored, "system", "blue", "" and "DARK" mark nothing; storage off limits throws nothing and marks nothing; both marks together; a scheme entry that is none leaves the list's mark set; and `markScheme`, which takes the mark away for System. `app/layout.test.tsx` holds as before: one script in the page, the body's first element, none in the head, its text the frame's script;
- the choice (`lib/scheme.test.ts`): what the page started with; what the page shows, though the storage says otherwise since; a choice remembered, marked, and told to two readers at once, and System, which takes both away; storage off limits; whether the page is shown dark, for each choice under each setting of the system, the system followed as it changes while the choice is System and not while it is Light or Dark; a `storage` event for the key, one for a cleared storage, one whose entry is no scheme, a `pageshow` that was persisted and one that wasn't, an event for another key, which changes nothing, and nothing followed, of either kind, once the following has ended; the choice read once when the following starts, where another window chose meanwhile and where the mark was taken off the root, and nothing written on the root where the mark says what is remembered;
- the control, through the whole app against the fake API (`components/sidebar/theme.test.tsx`): the group "Theme" under the list and last in its column, System chosen; Dark chosen with the mouse, by its words, marked, remembered, chosen, and the keyboard on it, then System, which takes mark and entry away; with the keyboard, one stop for Tab, and an arrow key that chooses the next; the group's note that an arrow key was used in it, and no other key, until the mouse is; Dark chosen on an earlier visit, the page started by its script; the same with the script's mark lost, which the page puts back; a choice made in another window while the list is put away and nothing on the page asks, also in React's StrictMode, and none followed once the app has left the page; the group in the list that lies over the chat in a narrow window. A diagram, through the whole app too, in `components/chat/markdown.test.tsx`, where the stand-in for the diagram's library is at home: drawn in the light theme, anew in the dark one once Dark is chosen, and light again;
- the frame as the browser takes it over (`components/chat/frame.test.tsx`): a fourth visitor, dark chosen on an earlier visit, hydrates without a mismatch, and for every visitor the scheme's mark is what the script left, and changed at no moment, through the hydration and the session's arrival. A `MutationObserver` watches it, and a control shows that it sees a mark taken away and put back at once;
- end to end (`e2e/scheme.spec.ts`), with colours read from the page and compared with what the same page shows in the other scheme, or with the keyword's own two colours, never with a colour written into the test: with nothing chosen, the page follows the system, light and dark, while it is open, and every part read differs between the two; Dark under a light system: the words, the list, the code and the diagram dark, and after a reload the mark, the dark words and the dark list while every one of the app's scripts is held back at a route, then the control says Dark; Light under a dark system: the words, the list, the diagram and the code's keyword light, which Tailwind's own `dark` variant would fail, then System, dark again; Dark chosen and the list put away on a page that React can't take over as it was sent, the server's page with one word changed on its way: drawn anew, with React's error 418, it is dark still and the list away, and no frame, of all the page's frames from its first, found it without either mark once the script had set them; the outline around a choice after a click, none, and with the group's note put on it by hand, around that choice alone, which is the styles' doing for the note, where this browser's own word would draw it at an arrow key; and with forced colours, the chosen one underlined and no other, and none once the page has its own colours again. The test of the page drawn anew says which error it brings about: the tests' rule that no script of the page's may break (`e2e/chat.ts`) lets a test name such an error, from the moment it names it, and fails the test as well if the error never comes;
- one older component test asked for no group on the page at all once a question before deleting was answered, and now names the group it meant (`components/sidebar/sidebar.test.tsx`).

**Measured on what was built,** on 2026-11-23, on a stack of its own built from this change, in Playwright 1.65 with Chromium 158, Firefox 159 and WebKit 27.2:

- **The six visitors an engine again, and the control.** All 18 saw their scheme in ground, words, the list's place, the keyword and now the diagram, with the values of the throwaway. Then, with a second window of the same browser open beside: Dark chosen under a light system, Light, the system turned dark, which changed nothing, System, which went dark with it, and the system turned light again. At each step both windows showed the same scheme, the diagram was drawn anew, and the storage held the choice, or nothing for System.
- **The mark is written once.** Watched from before the page's first script, through React taking the page over and the session's arrival, for a reader who chose the scheme their system doesn't have, with and without the list put away: the script wrote it, and nothing wrote it again, in the three browsers, on the built page and under the development server, where React's StrictMode runs each effect twice. Nothing was said in the console there but one warning of Firefox's own, which the code before this revision gets too.
- **Chromium's screencast, as above:** with the stylesheet as the stack sends it, no load of 24 began blank or showed a frame in the system's scheme; with it held back 400 ms, 23 of 24 began with the browser's blank, and 6 of 24 with that blank in the system's scheme. Two rounds before, on the same code but for what the review changed, gave 10 and 6 of 24. With nothing chosen, none of 12: the blank is the system's, which is then right.
- **The frame doesn't move:** the frame's probe of the revision "the page's frame is there before the session" said "nothing moved" in 18 of 18 cases.
- **The keyboard:** Tab reached the group once and left it again, and each arrow key moved and chose, with the outline around the radio button the keyboard was on at every step, in the three browsers; in WebKit with Option and Tab, and its outline after an arrow key only since the group notes the key.
- **The mistakes planted to see the tests fail,** one at a time in a copy of the change: 49 in the simulated page, of which 48 failed a test that names what broke, and one, a reading of the storage that throws where it can't be read, failed the run with the error itself; and 14 in a stack built from the copy, each of which failed an end-to-end test of this revision. A first round, before the review's findings were settled, had left two of 42 that failed nothing: a following of `pageshow` that never ended, and any text taken for a scheme. Each has its test since.

#### Consequences

- **What is left of the other scheme, before the first paint:** a reader whose choice differs from their system's setting, on a load where the stylesheet is not in the browser's cache and takes long to arrive, such as the first load after a release that changed the styles, on a slow connection, sees the browser's blank in the system's scheme until the stylesheet is in: in 6 to 10 of 24 such loads in Chromium as built, over three rounds, with the stylesheet 400 ms late, and in none where it came within 50 ms, as it did from the stack on the same machine. Never a frame of the page itself. Ending it would take a script before the stylesheet, which the app can't place; or the choice known to the server, which takes a cookie, and pages rendered for each request instead of once; or the styles written into the page itself, which Next.js 16.4 offers as an experiment (`inlineCss`): the styles, about 8 kB compressed today, would then come with every page instead of once. None of these is built.
- **The choice is each browser's.** A choice on the account, which travels between browsers, is not built.
- **A page whose frame script doesn't run shows the system's scheme until the app is on it,** and the remembered one from then: the page reads the choice from the mark, which the script sets at the page's start, and once more when it starts to follow choices made elsewhere. Today the script always runs. A Content-Security-Policy that didn't allow it (the revision "the page's frame is there before the session") would bring that flash to every load of a reader whose choice differs from their system's, beside the list's place at the first paint.
- **The variant `dark` is the app's own.** An upgrade of Streamdown that coloured code by other means than `dark:` would bring the dark theme's code colours back to a page chosen light under a dark system, and the end-to-end test of Light under a dark system would fail.
- **Not built:** nothing for `theme-color`, the colour a browser may give its own frame around the page; a choice on the account.
- **Not heard with a screen reader,** and not seen with a system's own contrast theme, only with Chromium's emulation of forced colours. The tests find the group by its role and name, and the chosen radio button by its state, in the simulated page and through Playwright in Chromium.

**Why now, and not at the start:** the page was dark from its first step for a reader whose system is set so, which was enough to build the chat on. A choice of one's own came with the rest of what the page says and offers of its own accord, in the steps of this week. And that the code's colours hang on Tailwind's `dark` variant only matters once the choice and the system can differ.

### 2026-11-23: with forced colours, what stood apart by its ground has a line

#### Context

Where a system's own few colours take the place of the page's (`forced-colors`, as Windows' contrast themes do), the browser gives every ground on the page the system's one ground, and every word and line one of its few colours. The revision before this one met that at the choice of a theme, underlined the chosen one there, and named two more places, as they were. The page was then looked at: the chat with two conversations, the question before deleting, and the form for logging in. The review of this revision read the classes of what the markdown library draws as well, and found the last two of the list below, which were then looked at too.

Seen on 2026-11-23, on the code of that revision, in Playwright 1.65 with Chromium 158 told that colours are forced (`forcedColors`), under a light and a dark system, from pictures and from the styles as the browser computed them:

- **The current conversation's row in the list** had the ground of every other row, and was told from them by its heavier letters alone.
- **The usage meter's bars and the context meter's bar** were not seen: the bar and what fills it both had the page's ground, and neither a line. What was left were the words beside them.
- **A question** had lost its bubble: its ground was the page's, and it had no line. It was words at the right of the page.
- **Two buttons were words alone:** "Delete" in the question before deleting, beside a "Cancel" that kept its line, and the forms' main button, "Log in", "Sign up" and the account page's. Each is a filled ground without a border. Send, as filled, kept a line: the border it has since the revision "the box is as large with Stop in it as with Send", which nobody sees with the page's own colours, the system colours there.
- **The button that opens a link** was words and an icon alone too, beside a "Copy link" that kept its line: in the dialog that holds a link until the reader has seen its address ([ADR 0016](0016-answers-shown-as-markdown.md)), which is the markdown library's own.
- **The dots between an answer's figures** were not seen in Chromium: a dot is a ground in the colour of the words around it, and got the page's ground. Firefox left them their colour.
- **What had a line kept it:** the notices, the box to write in, every button with a border, a block of code, the question before deleting.

#### Options considered

1. **The system's colours for what is chosen** (`Highlight` and `HighlightText`), with the browser told to leave them (`forced-color-adjust: none`).
    - Con: a ground is what the system takes away there, on purpose, and two more colours by name, for one state, where the page's colours have names by role.
2. **A border that is always there,** of no colour with the page's own colours, as Send has.
    - Con: it makes each of these two pixels larger for everybody, and it can't tell the current row from the others: every row would have it, and the system colours them all.
3. **A line only where colours are forced,** and for what is a ground and nothing else, the colour of the words.

#### Decision

The third, in the styles alone (`forced-colors:` in each component's classes, the rule said once in `app/globals.css`):

- **A line around what stood apart by its ground alone** (`forced-colors:border`, which the system colours): the current conversation's row, a question's bubble, each meter's bar, "Delete" in the question before deleting, and the forms' main button.
- **The button that opens a link gets its line from the app's stylesheet,** by a rule for that dialog where colours are forced (`app/globals.css`): the dialog's classes are the library's, and the page can't write them.
- **What is a ground and nothing else is in the colour of the words:** what fills a meter's bar, and the dot between an answer's figures (`forced-colors:bg-foreground`, the role whose colour is the system's own for text). The system leaves a colour of its own as it is, and gives any other ground the page's: so nothing more is asked of the browser. A class that told it to leave the ground alone (`forced-color-adjust-none`) was built first and taken out: planted as a mistake, its absence failed nothing, and Chromium and Firefox fill the bar in the colour of the words without it.
- **Nothing changes with the page's own colours:** no line is drawn there, and nothing is a pixel larger.
- **Left as they are:**
    - **a level that is close to its limit** has the colour of every other level there. Its share, in figures beside it, is what still says how close;
    - **the rows move by two pixels when another conversation becomes the current one,** there: its line makes the current row that much taller, and a line on every row would tell none apart;
    - **"Delete" is no longer told from "Cancel" by a colour.** Its word says it;
    - **code within a line and the place of a picture that isn't fetched** lose their ground and keep their letters;
    - **the grounds under the pointer** are gone, as every ground is;
    - **the two buttons that are words with the page's own colours too,** "Show older conversations" and a password's "Show", stay words.

**Tests:** end to end (`e2e/forced-colours.spec.ts`), with the browser told that colours are forced. Two conversations, the second on the page: the question, the current row and both meters' bars each have a line, and the other row has none; each bar's filling and the dot between the answer's figures are in the colour of the page's words, which a bar's own ground is not; "Delete" in the question before deleting and "Log in" in its form have a line; and once the browser is told that colours are no longer forced, none of these lines is drawn, "Delete" has none either, and neither a bar nor the dot is in the colour of the words. And an answer with a link: in the dialog that shows its address, the button that opens it has a line, as "Copy link" has, and none once colours are no longer forced, where "Copy link" keeps its own. The simulated page lays nothing out and applies no styles, so there is no component test.

**Measured on what was built,** on 2026-11-23, on a stack of its own built from this change: in Chromium 158 and Firefox 159, under a light and a dark system in Chromium, each had its line in a colour of the system's, and each bar and dot the colour of the words on the page's ground. Playwright's WebKit says that colours are forced when told so, and forces none: Safari has no such mode. The mistakes planted to see the tests fail, one at a time in a stack built from a copy of the change: 15, each of which failed one.

#### Consequences

- **With forced colours, what has a line is two pixels larger** than with the page's own, the line being there only then: the current row, a question and the three buttons. A meter's bar keeps its size, and what fills it is two pixels thinner.
- **A new thing that stands apart by its ground alone needs its line too.** `AGENTS.md` says so; nothing but looking finds one that was forgotten.
- **Not seen with a system's own contrast theme,** only with the browsers' emulation of forced colours.

**Why now, and not at the start:** the page was never looked at with forced colours until the choice of a theme was, on this day. The review of that step asked whether its chosen one could still be told there; it couldn't, and the rest of the page was then looked at the same way.

### 2026-11-23: an answer waits for the model until it has words

**What was wrong.** An answer said "Waiting for the model…" until its first piece began, and then drew that piece. But a piece begins a moment before its first words: the stream says `text-start`, and then the first `text-delta`. When the two reached the page apart, the text was drawn from its beginning, empty, in the wait's place, and its first words were handed to the renderer only when the pace allowed, 50 ms later ([ADR 0016](0016-answers-shown-as-markdown.md), revision "an arriving answer is drawn at most every 50 ms"). Meanwhile the answer was an article with nothing in it.

- **It showed as a flicker:** the wait line went, nothing stood there, then the words came. In a conversation longer than the screen, the conversation got 20 px shorter and then 24 px longer.
- **And it cost a reader their place.** Someone at the end who turned the wheel up a few pixels at that moment was moved up to the new end by the browser when the conversation got shorter. When it got longer again, Chromium put them back where the wheel had left them, by itself, a few frames later. The page took that for the reader scrolling down to the end, and followed the answer again, over the wheel they had just turned.

**How it was found.** The end-to-end test of the page following a long answer failed once in a run of the whole suite. It turns the wheel 6 px up, and waits for the button that leads back to the end. Run alone it then passed 60 times of 60, and the whole suite 17 times of 17, on the code before the step that was being checked and after it. Beside eight busy processes it failed 2 times in 30 on each. A probe then noted every wheel, every scroll, every move the page made itself and every frame around the wheel: the four runs of 32 in which the button never came all showed the conversation 20 px shorter for a few frames right after the wheel, and the reader back at their place when it was longer again. A second probe, frame by frame, found what got shorter: the answer's article, from the wait line's 20 px to nothing, and then to its first line.

- **The test turned its wheel at that moment by a mistake of its own:** it waited for the conversation to be longer than the screen "some seconds into the answer", and the question it asks is as long as the answer, so the wait was over when the question was on the page.

**What changed:**

- **A piece of text that has begun and has no words yet draws nothing** (`Answer` in `components/chat/message.tsx`), and neither does one of blank space alone. Until something of the answer is drawn, the answer goes on saying that it waits for the model. So its first words are the first thing drawn of them, at once, and the pace counts from there. A thought that has begun is drawn as before: it says "Thinking…" by itself.
- **A thought stays open until the text after it has words,** and folds away as they come, in the same draw. It folded when the text began: a moment with the folded thought alone, and nothing yet of the answer. The piece that can still be arriving is the last one that is drawn.
- **A move of the browser's own isn't the reader scrolling down** (`useFollow` in `components/chat/follow.ts`). When what is shown gets shorter and the browser moves the reader up to its new end, the hook keeps where they were. A scroll down that goes no further than that place, to within a pixel, with nothing done by the reader in between, is the browser putting them back, and the end isn't followed again for it. Whatever the reader does that scrolls, a wheel, a key, a finger, a scrollbar taken hold of, makes the next scroll theirs, and so does a scroll past that place.
- **The end-to-end test waits for the answer's own words,** a few hundred of them, before it looks and turns its wheel.

**Measured** on 2026-11-23, on the machine of this record's other measurements, on a stack of `make e2e` built from the code before and after:

| | Before | After |
|---|---|---|
| A frame found the answer empty between the wait line and its first words: Chromium, Firefox, WebKit, 12 runs each with nothing else running | 8, 2 and 5 runs, for up to 50 ms | 0, 0 and 0 |
| The same beside eight busy processes: 16 runs each before, 32 each after | 13, 2 and 6 runs, for up to 50 ms | 0, 0 and 0 |
| An answer that thinks first, 16 runs each beside eight busy processes: a frame found its thought folded with nothing yet of the answer | 10, 4 and 7 runs, for up to 50 ms | 0, 0 and 0 |
| The wheel turned 6 px up as the answer begins, beside eight busy processes, Chromium: runs in which the button to the newest message never came | 4 of 32 | 0 of 192: 64 with the first change alone, 128 with all of them |

- **With a real wheel and real keys, in each engine, the page follows as before:** left where they are after the wheel far up, followed again after the wheel back down to the end, left after 6 px up, followed again after the End key, and after the button.
- **No engine puts a reader back by itself in the ordinary case.** Made on purpose in each, a second after the wheel, and in Chromium also 40 ms after it: the conversation 30 px shorter for a few frames, and as long as before again. The reader stayed at the end they had been moved up to, in Chromium, Firefox and WebKit. What was recorded is a race in Chromium, within a few frames of the wheel, on a busy device.
- **The hook's rule is held by a replay of what was recorded,** with a page whose sizes the test makes up (`components/chat/follow.test.tsx`): the wait line's place is no longer empty, so the end-to-end test meets nothing that gets shorter at that moment.

**What it leaves:**

- **The wait line is 20 px high and a first line 24:** the conversation still gets 4 px longer when the first words come.
- **An answer that thought first still gets shorter as its first words come:** its thought folds away then, once, with the words in its place. A reader near the end is moved up by the browser as before, and the hook's rule holds for them too.
- **A reader the browser moved up to the end, and didn't put back, stays there,** and isn't followed: the button to the newest message comes once the answer has gone on for 48 px.
- **Seen on the way, and older than this:** WebKit reported "ResizeObserver loop completed with undelivered notifications" as an error of the page in 1 run of 144 before these changes and in 1 of 84 after them, with nothing else running. The revision "the page is laid out before its sizes are watched" ended the one at every page load; what brings this rare one about isn't known.

**Why now, and not at the start:** the wait line and the pace were built in different steps, and each was tested with the events that begin an answer arriving together, as they do in the recorded streams the component tests read. The empty answer lasted 50 ms; nobody looked for it. The test that met it had passed in every run until then.

### 2026-11-24: what an answer took comes a frame after its last words

P1's performance run set the page beside the one from before the line of what an answer took (revision "an answer says what it took"), back to back. One of the numbers the project holds itself to was no longer held, in one engine: while an answer of up to 4,000 words arrives at 300 words a second, no frame takes longer than 50 ms. In Safari's engine the frame in which such an answer ends took 37 to 54 ms, where it had taken 26 to 35.

- **What that frame holds.** When an answer ends, the page hands all of its text to the renderer once more, as markdown that is complete ([ADR 0016](0016-answers-shown-as-markdown.md), revision "an arriving answer is drawn at most every 50 ms"). Since the line, the line came onto the page in that same render, after the answer's article. With the line switched off and nothing else changed, the frame was as before; with the line drawn one frame later, too. Why the line's coming costs that engine what it does, at the end of a long answer and not of a short one, isn't known: under the test's driver the engine gives no account of where a frame's time went.
- **The line comes one frame later** (`useSettled` in `components/chat/settled.ts`, asked by `Took` in `components/chat/message.tsx`): for an answer that arrived on this page, once the frame with its last words has been painted (`afterPaint`). A stored answer's line is there from its first render: nothing of it arrived here.
- **The wait is kept where little is drawn.** `Took` draws the line and nothing else, so a frame later the line is drawn and the answer's words are not drawn again.
- **The notice of how the turn ended waits the same frame** (`components/chat/chat.tsx`). It stands under the line. Said with the answer's end as before, it would be there a frame before the line, and move down when the line came in above it.
- **Tests:** with the frames held, an answer that has ended has all of its words on the page and no line, and no notice, until a frame comes; a stored answer has its line with no frame at all (`components/chat/figures.test.tsx`). In a real browser, the first frame that finds the answer over finds no line yet (`e2e/figures.spec.ts`).

**Measured** in Safari's engine (WebKit 27.2), the frame in which an answer of 4,000 words at 300 words a second ends, on the machine of this record's other measurements:

| | Without the line | The line in the same frame | The line a frame later |
|---|---|---|---|
| The night of 2026-11-23, six runs a build, the build before the line against the one with it | 26 to 35 ms | 37 to 54 ms, three of six at 50 or more | |
| 2026-11-24, one build with a switch for the three, eight runs each in turn, the machine busy with other work | 26 to 49 ms, 40 in the middle | 38 to 53 ms, 47 in the middle | 23 to 45 ms, 37 in the middle |
| 2026-11-24, the committed page against this change, twelve runs each in turn, the machine busy with other work | | 43 to 63 ms, 50 in the middle, six over 50 | 31 to 54 ms, 41 in the middle, one over 50 |

In Chromium and Firefox no frame of that answer took longer than 19 ms with this change, in three runs each; the phase's run had found none over 34 ms without it.

**What it leaves:**

- **The frame in which a long answer ends still draws all of its words once more.** That is the 26 to 35 ms of the first row, and on a machine busy with other work it reached 49 ms with no line at all. The wait gives back what the line had added; it doesn't shorten that frame.
- **The line's own frame is the next one,** 16 ms later. What it costs there was not timed apart: no frame after the answer's end was longer than the one that ends it.
- **An answer that is not arriving when it first comes onto the page has its line in the frame that draws its words.** That is a stored answer; it would also be one whose whole stream had been read before the page drew any of it, which was not seen and not looked for. Either way it is the frame a stored conversation is drawn in, and no answer is arriving in it.
- **Tests that look for a notice that must not be there now look a moment after the answer's end,** since a notice would come a frame after it: the end-to-end tests through `chat.answered()`, which waits three frames, and one component test by a short wait.
- **Seen on the way: a click through the test's driver is not a person's click.** The same run found the list of conversations a quarter slower to put away under a long conversation than before the context meter (revision "the page says how full the model's window is"). It was the driver: to click a button it finds by its name, it reads a computed style of some two hundred elements, and with a container query on the page, as the meter's line has, each such read is a layout of the page. Clicked by its place on the screen, the list is put away in one frame of 17 ms under 300 exchanges, 50 at the most the first time, as before the meter. The page's own code reads no computed style, and has to keep it so in anything that runs often.

**Why now, and not at the start:** the step that added the line tested what it says, when, and where it stands. How long the frame takes in which a long answer ends was measured next at the phase's end, against the build before the line: that comparison is what the run is for.

### 2026-11-24: a conversation opens where its reader left it

A stored conversation opened at its end, always (revision "a long conversation opens newest first"). A reader who had gone up to an older message and then left the conversation, for another one, for the account's place, or with Back, came back to its end and had to find the message again. Now they come back to the message, at the same place in the window.

- **The place is a block of a message, and how far into it the window began** (`Spot` in `lib/chat/places.ts`): the id of the message at the top of the window, the way from its `article` down to the block that crossed the window's top edge, a child's index at each of four levels at the most, and how far that edge was below the block's top. Not a position of the element that scrolls, which another window's width or messages that came meanwhile would change. And not an offset from the message's top, which was built first: what is above the window inside a message is not the same height when the conversation is drawn anew. Measured, a message 984 px high when its reader left it 595 px in was 850 px high when they came back, and they were 134 px from where they had been, in all three browsers. That was a block of code drawn as a placeholder while it is off the screen, which is ended at its root ([ADR 0016](0016-answers-shown-as-markdown.md), revision "a block of code has its real height from the first"); a diagram that is drawn later, or a message shown as written until the renderer is there, would do the same.
- **Kept in the page's memory,** for whoever has the session (`session.holder`) and for each conversation: the 100 conversations most recently left, and none of them once the session is another one's. Not in the browser's storage: a reload opens a conversation at its end, as before.
- **A reader at the end has no place.** While the end is followed nothing is kept for the conversation, and what was kept is forgotten: it opens at its end, exactly as it did.
- **Read by the hook that follows the end** (`useFollow`, `remember`): after a scroll of the reader's, at most once a frame, and when the conversation leaves the page, in the cleanup of a layout effect, which React runs while the elements are still on the page. Never for a move of the page's own. The message and the block are found by halves, a few reads of the layout however long the conversation or the answer.
- **Restored before the first paint** (`at`), in the layout effect that otherwise goes to the end: down the place's way as far as it leads to something that is drawn now. Where it leads into something that isn't, as into a thought the reader had opened, which is folded when it is drawn anew, the window's top goes to the deepest element that is drawn, no further than its end.
- **Twenty messages around the place are drawn at once** (`components/chat/chat.tsx`): the 5 before the place's message and 15 from it on, held to the conversation's two ends. Twenty is what the opening's frames were measured with. When those from the place on don't fill the window, the next step below is drawn before the paint, until the place is reached.
- **The rest comes in steps of 20, in both directions, the nearest first, below and above in turn.** Above the place as before: drawn out of sight, shown while the reader is still, the reader kept where they were. Below it a step is shown as soon as it is drawn: nothing above the reader changes by it.
- **While messages are still held back below, the end of what is shown is not the conversation's end.** A reader who goes down to it is not taken to follow the end, the button to the newest message stays, and what stands at a conversation's end, the line that waits for the model and the notices, is not drawn yet. Whatever needs the end draws all that is held back below first, at once: a question that is sent, that button, an answer the page reads along with. The page never shows a message with one missing before it.
- **A message's `article` carries its id** (`data-message-id`), by which a place finds it again. An attribute: the article's text is still the message's words and nothing else.
- **Tests:** the rules of the place, in a page whose sizes the test makes up (`components/chat/follow.test.tsx`, `components/chat/newest-first.test.tsx`, `lib/chat/places.test.ts`); and in a real browser, a loop inside the page that looks at every frame from the first that shows the conversation (`e2e/newest-first.spec.ts`, `e2e/code.spec.ts`): opened from the list, by Back and by its address, the place's message stands where it stood, to within 2 px.

**Measured** on 2026-11-24, on the machine of this record's other measurements, which was busy with other work, on a production build with the fake model, in Playwright's builds of Chromium 158, Firefox 159 and WebKit 27.2. Conversations of exchanges of 110 to 260 words with headings, lists, code and tables were gone up through with the wheel, left for another conversation, and come back to through the list, by a click on the row's place on the screen:

| | Chromium | Firefox | WebKit |
|---|---|---|---|
| The block the reader had at the window's top, at the first frame back and at every frame until they acted: 60 and 100 exchanges left halfway up, 12 returns | where it stood, to the pixel | the same | the same |
| The same with the place inside a block of code, 5 returns | to the pixel | the same | the same |
| The same in 300 exchanges, left halfway up and near the top, 7 returns | to the pixel | the same | the same |
| The longest frame while the rest came in, 100 exchanges | 16.8 ms | 42 to 51 ms | 22 to 57 ms |
| The same, 300 exchanges | 16.8 to 33 ms | 50 to 59 ms, and 107 ms once in seven | 26 to 55 ms, and 98 ms once in seven |
| Until all of it was shown, 100 and 300 exchanges | 183 to 301 ms, 567 to 767 ms | 257 to 416 ms, 833 to 1,276 ms | 297 to 452 ms, 1,263 to 1,431 ms |
| A wheel notch turned at a random moment of the first 400 ms: kept | 28 of 28 | 28 of 28 | 17 of 28 |
| A conversation left at its end, 100 exchanges: the longest frame as it opened at its end again | 16.8 ms | 42 ms | 29 to 36 ms |
| 300 exchanges gone up through with the wheel, 60 notches: the longest frame, before this change and after | 17 to 33 ms, 17 ms | 10 ms, 15 to 18 ms | 103 to 131 ms, 117 to 132 ms |

- **A place inside a thought the reader had opened,** in all three: back at the folded thought, its end at the window's top, with nothing drawn below that the window didn't need.
- **WebKit's wheel:** the page before this change, opened at its end and tried the same way, kept 5 notches of 14. It is the loss the revision "a long conversation opens newest first" left in that engine, no more of it.

**What it leaves:**

- **Going to the newest message right after coming back far up a very long conversation stops the page.** With 558 to 580 of 600 messages still held back below, the button drew them at once: one frame of 400 to 417 ms in Chromium, and 1.7 to 1.9 s at a quarter of the CPU's speed. So does a question sent in that moment. It is the uninterrupted drawing that "newest first" ended for opening, kept here for the one case where the reader asks for the end before it is there: about a second after the return in 300 exchanges, a third of one in 100. Filling in behind and going to the end when it is there would never stop the page, and would get there later.
- **A reload opens at the end,** and so does the same conversation in another window: the places are one page's.
- **In a window of another width the place is the same block,** and not the same line of it: the offset is in pixels.
- **A frame of over 100 ms while 600 messages are scrolled through in WebKit** was there before and is there after, once in 60 notches. Its cause is not known.

**Why now, and not at the start:** it was asked for on 2026-11-24, by the maintainer, as a reader of long conversations. The step also found what the measuring of "newest first" had not looked for: a block of code that changes its height when it comes near the window, which ADR 0016's revision of the same day ends.
