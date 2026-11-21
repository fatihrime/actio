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
