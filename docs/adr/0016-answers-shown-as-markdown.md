# 0016. Answers are shown as markdown, which can't act on the page or its reader

- **Status:** accepted
- **Date:** 2026-11-14

## Context

Models answer in markdown: headings, lists, tables, code, formulas in LaTeX, and now and then a diagram. Until this step the chat showed an answer as it was written, asterisks and all ([ADR 0015](0015-the-web-chat-calls-the-api-from-the-browser.md) left this for the next step).

- **An answer arrives in pieces.** At any moment its last piece can be half of something: `**bo`, a link without its address, a block of code that isn't closed yet.
- **A model's words are not to be trusted.** Today a model sees only what the person typed. Later it will read documents and web pages, and text in those can tell it what to write. What it writes is then shown in the browser of someone who is logged in. The known ways to use that are:
    - a script, or HTML that covers the page;
    - a picture whose address carries the conversation to whoever owns the address. The browser fetches a picture without being asked;
    - a link whose words name one site while its address leads to another.
- **Models don't write the markdown that parsers expect.** The default model (`qwen3.5:4b-q4_K_M`) was asked eight questions on 2026-11-14, and wrote:
    - a formula between single dollar signs, `$E = mc^2$`, and a formula of its own as one line, `$$x = \frac{-b}{2a}$$`;
    - money inside a formula with a backslash, `$4 \times 2 = \$8$`, and outside one without, `**$14**`;
    - `<br>` in the cells of a table, on every row;
    - a diagram with a mistake in its syntax.
- **The libraries that render all this are large,** and the page should open as fast as it did.

## Options considered

**What turns markdown into a page:**

1. **react-markdown,** with plugins chosen here.
    - Pro: the usual choice, and small.
    - Con: it knows nothing of answers that arrive in pieces. Half-written markdown shows as its marks until it is closed, and every new piece renders the whole answer again.
2. **Streamdown** (Apache-2.0), a replacement for it made for streamed answers.
    - Pro: it closes what is half-written, and renders again only the block that changed.
    - Pro: code colouring (Shiki), formulas (KaTeX) and diagrams (Mermaid) come as plugins, each with a button to copy what it shows.
    - Con: a young library with a look and words of its own, and it expects shadcn/ui's colour names.
    - Con: as it comes, it trusts more than this project does: it obeys HTML after cleaning it, and fetches every picture.

**HTML in an answer:**

1. **Obey it after cleaning it,** by the rules GitHub uses for comments. The library's default.
    - Pro: `<details>`, `<sub>` and `<br>` work.
    - Con: every tag and attribute that is let through is something to keep safe. The plugin that checks addresses looks at links and at pictures, and at no other tag that can fetch.
    - Con: a model that explains HTML and forgets the backticks loses its tags: "the <b> tag" becomes "the tag", and the rest of the sentence is bold.
2. **Show it as text.**
    - Pro: what markdown itself can turn into is a short list, with two things in it that carry an address.
    - Con: the few useful tags don't work, and models do write `<br>` in tables.

**Pictures an answer points at:**

1. **Fetch them.** The library's default.
2. **Fetch them from listed addresses only.**
3. **Fetch none.** Nothing in Actio makes or stores pictures yet, so there is no address to list.

**Links:**

1. **Ordinary links.**
2. **A dialog that shows the address before opening it.** The library's default.

**A formula between single dollar signs:**

1. **Not read as one.** The library's default, because "$5 and $10" would be a formula. But then the default model's formulas show as raw LaTeX.
2. **Read as the parser reads it:** anything between two dollar signs.
3. **Read by pandoc's rule:** a formula has no space just inside its dollar signs, and no digit right after the closing one.

**When the libraries are fetched:**

1. **With the page.**
2. **When an answer first needs them.**

## Decision

**Answers are shown with Streamdown, set stricter than it comes.** One component, `components/chat/markdown.tsx`, shows a model's words: its answers, and its reasoning. A question is shown as it was typed.

**What an answer can't do:**

- **No HTML.** The plugin that would obey it is left out, so a tag is shown as text. The library's cleaning of what is left stays in, as a second check.
- **No picture is fetched.** One that an answer points at shows as "[Image blocked: …]" with its description.
- **A link opens only after the reader has seen its address,** in a dialog, and then in a new tab that can't reach back into this one. A link may lead to the web or to an email address, and to the two kinds of chat address the checking plugin always lets through. Anything else, such as `javascript:`, `file:` or a program's own scheme, is not a link.
- **A formula can't hold a link or a picture.** KaTeX is told nothing can be trusted, which is how it comes.
- **A diagram is drawn in Mermaid's strict mode:** no scripts, no clicks, no HTML in its labels.

**Read as models write.** Each of these is done on the parsed markdown (`lib/chat/markdown.ts`), so none touches what is inside code:

- **A formula between single dollar signs is a formula, by pandoc's rule.** "$5 and $10" stays money.
- **A dollar sign with a backslash before it never ends a formula.** The parser knows no escapes inside one. Before parsing, each escaped dollar sign is swapped for a character nobody types, and it is put back afterwards.
- **A formula between double dollar signs stands on its own line,** also when it was written in the middle of one, or begun on the line of its opening dollar signs. The parser takes the first for a formula between words, and drops the start of the second.
- **A formula that was cut off is shown as far as it got.** Without the rule above it showed as nothing: found by looking at an answer that had run out of room.
- **`<br>` in a table's cell is a line break,** and on a line of its own it is space. It is the one tag that is obeyed, in the two places where it can mean nothing else. Between words it stays text.
- **HTML that stands on its own lines is shown as a block of code,** so a page that a model wrote without fencing it keeps its lines.
- **A diagram that can't be drawn is shown as it was written,** under a line that says so.

**While an answer arrives:**

- what is half-written is shown as what it is becoming: `**bo` is bold;
- a link that is half-written shows as its words, and becomes a link when its address is whole;
- a diagram is drawn once it is whole;
- an answer that was stopped is stored as far as it got ([ADR 0014](0014-turns-streamed-in-the-ai-sdks-format.md)), and is shown the same way.

**The libraries are fetched when an answer needs them.** The first answer fetches the renderer, a block of code its language, a formula its fonts, a diagram Mermaid. Until the renderer is there, and if it can't be fetched, an answer shows as it was written. (The renderer is fetched ahead now, and a stored answer waits for it: see the revision of 2026-11-21.)

**Colours have names by role** (`app/globals.css`): `muted`, `border`, `primary` and so on, each a light and a dark colour that the browser picks between. Streamdown expects these names, and the app's own components can use them.

**The page follows the end of an answer by watching its size** (`components/chat/follow.ts`), not its messages: code is coloured a moment after it is shown, fonts load, a diagram is drawn. A reader who scrolls up, however little, is left there, and a button leads back to the newest message.

## Measured

In a headless Chromium against `make up`, on 2026-11-14. Sizes are what was sent, compressed:

| The browser fetches | Scripts | Styles and fonts |
|---|---|---|
| An empty chat | 229 kB | 7 kB |
| The first answer, of plain words | 294 kB more | 4 kB more |
| An answer with Python code | 15 kB more | |
| An answer with a formula | | 42 kB more |
| An answer with a diagram | 195 kB more | |

- **The empty chat's scripts are what they were before this step.** Its styles grew from 15 kB to 33 kB, uncompressed.
- **The web image grew from 430 MB to 473 MB.** It holds a script for each of the languages Shiki can colour, and each is fetched only when a block of code names it.
- **With the default model,** asked to derive the quadratic formula: 39 formulas, 15 of them on their own line, none that KaTeX couldn't set, and no dollar sign left in the text. While the answer streamed, the page was never off its end. After a scroll of 6 pixels up it stayed where it was.
- **With every request to another site refused and recorded,** an answer with a link and a picture asked for nothing. After "Open link", the new tab asked for the link's address.

## Consequences

- **Easier:**
    - what an answer may do is decided in one file, and a test fails for each rule that is loosened. Tried on 2026-11-14 with eleven planted mistakes: eight were caught at once, and the other three after their tests were made sharper;
    - a new kind of content, such as pictures Actio makes itself, is one more allowed address.
- **Harder:**
    - **about 250 more packages in the lock file,** most of them Mermaid's. All are under permissive licenses (`THIRD_PARTY_NOTICES.md`);
    - **the library's look and words are its own.** The dialog and the buttons on code, tables and diagrams are in English, in its wording;
    - **the rules for dollar signs are guesses.** "I have $5, you have 10$" becomes a formula, and `$ x $`, with spaces inside, stays text;
    - **only the default model's writing was looked at.** Other models have other habits.
- **Not done yet:**
    - **`\(…\)` and `\[…\]` are not read as formulas.** Some models write them. The parser doesn't know them, and swapping them in the text beforehand would also change code;
    - **no Content-Security-Policy.** It would be a second lock on the same door: a browser told not to fetch from elsewhere, whatever the page says;
    - **a footnote's number opens the dialog,** like any link;
    - **the dialog doesn't keep the keyboard inside it,** and a copied block of code ends with a new line. Both are the library's;
    - no setting for text written right to left;
    - **scrolling is tested with made-up sizes,** because the simulated browser lays nothing out. It was tried in a real browser by hand, and belongs in the end-to-end tests.
- **How to reverse it:** only `components/chat/markdown.tsx` knows the library. Another renderer goes there, and the rules in `lib/chat/markdown.ts` are plugins of the kind most of them take. Loosening a rule is a line in that component, and a test to change on purpose.

## Revisions

### 2026-11-14: tested in a real browser

The last item under "Not done yet" is done ([ADR 0017](0017-end-to-end-tests-on-the-real-stack.md)). Scrolling is tested in a real browser, and so is what an answer can't do: an end-to-end test shows an answer with HTML, a picture and a link in it. Every end-to-end test, whatever it is about, fails if the page asks anything of another site.

### 2026-11-21: the renderer is fetched ahead, and a stored answer waits for it

#### Context

`apps/web/components/chat/message.tsx` loaded `./markdown` (Streamdown and its plugins, 295 kB over the wire) with `React.lazy`, the first time an answer was rendered, and showed the answer "as written" (`AsWritten`) inside a `Suspense` fallback until it was there. Two visible defects followed, measured on 2026-11-20, on a production build, in real browsers:

1. **Opening a stored conversation by its address** (a reload, a bookmark, a link) shows every answer as raw markdown first (` ```python `, `## heading`, `**bold**`), then swaps to the rendered form about 0.1 to 0.3 s later, and the whole page jumps. Layout-shift score of that swap: 0.42 to 0.46 at 1440×900 and 0.75 at 390×844 (Chromium 158); over 0.25 counts as poor. On a slow connection (1.6 Mbit/s, 150 ms latency), with nothing in the browser's cache, the raw text stays for about 1.8 s.
2. **The first answer of a visit** starts as raw text and swaps mid-answer, because the fetch only begins when its first words are rendered: 0.2 s on a fast machine, 2.4 s on the slow connection.

A throwaway prototype of the remedy took the swap's shift to 0 to 0.016, and showed the first answer as markdown after 0.3 s instead of 2.4 s on the slow connection. It also showed that fetching the renderer the moment the page mounts cost about 0.1 s of largest paint, and 85 ms of blocking on a slow phone's profile: it competed with the request for the session.

#### Options considered

1. **Fetch it with the page,** as a part of what the page needs to start.
    - Pro: an answer is never shown as written, and no conversation waits for the renderer.
    - Con: the page's first load, about 242 kB of script, grows by 295 kB, and its first paint and the request for the session wait behind it.
2. **Fetch it ahead, once the page knows whose session it has.**
    - Pro: the first paint and the session's request come first. By the time an answer arrives, the renderer is almost always there.
    - Con: a visitor who never asks fetches it all the same. A stored conversation opened by its address still has to wait for it.
3. **Keep fetching it at the first answer, and only hold a stored conversation until it is there.**
    - Pro: nothing is fetched for a visitor who never asks.
    - Con: the first answer of a visit still starts as written, and swaps mid-answer: the second defect stays.

#### Decision

The second, with a stored conversation held until the renderer has come, and not for long.

- **Still a piece apart from the page.** `./markdown` stays a dynamic `import()`, so it is no part of what the page needs to start. Nothing but `markdown.tsx` imports Streamdown or its plugins.
- **Fetched ahead, once the session is known.** `ChatApp` asks for the renderer as soon as the page knows whose session it has, and not before: the first paint and the session's request are never behind it. It is asked for once a page, by whatever asks first (`fetchRenderer` in `message.tsx`). An answer rendered before that, which can't happen today, asks too, as it did.
- **An answer is markdown from its first render whenever the renderer is there.** Not one render as written, then swapped: `React.lazy` suspends on its first render even when its module has been loaded, and a `Suspense` fallback that is committed is the flash. So there is no `lazy` and no `Suspense` here any more. A small state of the fetch, kept by `message.tsx` for as long as the page is open (on its way, here, or not to be had), is read by each answer through `useSyncExternalStore`, and is nothing on the server.
- **A stored conversation is shown once the renderer has come.** `StoredChat` keeps saying "Loading the conversation…" until the API has answered and the fetch is over, well or not. The two run side by side, and neither waits for the other to start. When the renderer can't be had, as when the installation was upgraded under an open page and the import fails, the conversation is shown as written at once.
- **But not for long.** When the API has answered and the renderer is still on its way three seconds later (`RENDERER_WAIT_MS`), the conversation is shown as written, and becomes markdown when the renderer arrives: what the page did before, as the last resort. A download that stalls must not hide a conversation the page already has. On the slow connection the renderer took 1.7 to 2.4 s, so the wait is no shorter. It goes by the clock, which a test can move.
- **An answer that arrives while the renderer is on its way** (asked in the first moments, on a slow connection) is shown as written, and becomes markdown when the renderer is here. Words are worth more than a blank. This is what the page did before, kept.
- **A renderer that can't be fetched** leaves every answer and every thought as written, for good, and the page says nothing of it, as before.

**Why:** the first option more than doubles the page's first load, which every visitor pays. The third leaves the second defect as it was. The second costs a download nobody waits for, after the page has started. Fetching at the moment the page mounts would have been simpler, and was measured to slow the first paint.

**Tests:**

- in the page's component tests (`components/chat/renderer.test.tsx`), each opened from modules loaded anew, so that the renderer isn't there until the test lets it arrive: it is fetched once the session is known and not before; a stored conversation waits for it and is never shown as written first; past the wait it is shown as written, then as markdown; it is shown as written at once when the renderer can't be had; and an answer is markdown from its first render, recorded at every change of the page;
- end to end, in a real browser (`e2e/renderer.spec.ts`), every change of the page recorded from before its first script runs: a stored answer after a reload, and the first answer of a visit, are never shown with their marks. Both tests failed on the code before this revision.

**What it costs:**

- **295 kB fetched by a visitor who never asks;**
- **a stored conversation waits for the renderer,** at most three seconds after the API has answered;
- **a component test that opens a stored conversation before its file's renderer has been loaded waits for it:** about half a second in the full run of the page's tests, and 0.8 to 1.0 s with every core of the machine busy. The tests' longest wait for something to appear is three seconds for that reason, where the library's own is one (`tests/setup.ts`).

**What no longer holds:** under "Decision", "The first answer fetches the renderer" and, for a stored answer, "Until the renderer is there … an answer shows as it was written". A block of code still fetches its language, a formula its fonts, and a diagram Mermaid, when an answer needs them. The table under "Measured" is of 2026-11-14: an empty chat now fetches the renderer too, once its session is known.

**Measured after:** on 2026-11-21, on a production build with the fake model, in Playwright's builds of three browsers (Chromium 158, Firefox 159, WebKit 27.2), every change of the page recorded from before its first script.

| | Before | After |
|---|---|---|
| A stored conversation of twelve exchanges, opened by its address three times in each browser | its marks on the page each time, in all three; in Chromium from 0.1 s to 0.25 s after the address was opened | never, in any of the three |
| The first answer of a visit, in each browser | begins as written, in all three | never as written |
| Layout shift once the conversation is on the page (100 and 300 exchanges, Chromium) | 0.42 to 0.46 | 0 |
| The first answer shown as markdown, after it was asked | 0.21 s; 2.36 s on the slow connection | 0.09 s; 0.31 s |
| A first visit on the slow connection, at a quarter of the CPU's speed: the largest paint | 2.03 s | 2.03 s |
| The same visit: time the page's scripts kept it busy beyond 50 ms at a stretch | 29 ms | 112 ms |
| Scripts an empty chat fetches, compressed | 242 kB | 537 kB, the renderer's 295 kB after the session is known |

- **The page's first paint is where it was,** which fetching at the moment the page mounts had not managed. The renderer is run when it arrives, so the page is busy with it once, right after its start, and no longer in the middle of the first answer, whose longest stretch on that visit went from 126 ms to 83 ms.
- **On the slow connection, with nothing in the browser's cache,** a stored conversation opened by its address is on the page 4.1 s after the address was opened, as markdown. Before, its raw text was there after 2.25 s, and was swapped at 4.07 s. So there the reader waits about 1.8 s longer for something to read, and sees no swap. Asking for the renderer at once when the address names a conversation was tried too: 3.9 s, not worth a second rule. A browser that has the renderer in its cache doesn't wait.
- **A long conversation opened by its address is now drawn in one piece,** as one opened from the list always was: 300 exchanges keep the page busy for 0.33 s at a stretch, where showing them as written first had kept it under 0.1 s (1.4 s and 0.3 s at a quarter of the CPU's speed). What remains of the layout shift when the page loads, 0.19, is the header and the list arriving with the session. Neither is this revision's subject: both are steps of their own.

### 2026-11-21: code is coloured in a worker

#### Context

`components/chat/markdown.tsx` gave Streamdown the code plugin of `@streamdown/code` 2.0.0. That plugin runs Shiki on the page's own thread, with Shiki's regular-expression engine written in JavaScript (`createJavaScriptRegexEngine({ forgiving: true })`), and has no option to do otherwise. The first block of a language after a page load makes the page stand still, while that grammar's patterns are turned into the browser's regular expressions and first used.

Measured on 2026-11-21, on production builds of the code before this revision, in Playwright's builds of Chromium 158, Firefox 159 and WebKit 27.2:

- **A first block of TypeScript, alone on a page** (30 lines): the page stood still 1.48 s in WebKit, 0.12 s in Chromium and 0.20 s in Firefox. Of WebKit's 1.46 s, 0.6 s was making 208 patterns, and the rest was matching with them. JavaScript, JSX and TSX are the same in WebKit (1.5 s), CSS takes 0.46 s and C++ 0.41 s. The fourteen other languages tried stay under 0.14 s.
- **In the app, an answer streamed at 150 words a second:** the first block of TypeScript gave WebKit frames of 786 and 631 ms, and its colours came 1.4 s after its code. The first block of C++ gave one frame of 433 ms in Chromium, frames of 576 and 283 ms in Firefox, and one of 329 ms in WebKit. CSS: 289 ms in WebKit.
- **A stored conversation with code in eight languages, opened by its address:** a longest frame of 633 to 700 ms in Chromium, 921 ms in Firefox, and 2,196 to 2,650 ms in WebKit. All of it was coloured after 1.5 s, 1.9 s and 3.25 s.

#### Options considered

Each was measured on a page of its own, then in a throwaway build of the app, in the three browsers:

1. **Shiki's WebAssembly engine (Oniguruma), on the page's thread.**
    - Pro: a first block of TypeScript stops the page 0.15 s in WebKit, ten times less than before, 0.12 s in Chromium and 0.16 s in Firefox.
    - Con: it still stops the page, and about four times as long on a slow phone.
2. **Today's engine, written in JavaScript, in a Web Worker.**
    - Pro: the page never stops.
    - Con: in WebKit the colours come 1.5 s after the code.
3. **The WebAssembly engine in a Web Worker.**
    - Pro: the page never stops, in any of the three. The colours come 0.18 to 0.22 s after the code for the first block of a page, and within a frame after that.
    - Con: a worker to start, and a plugin of the project's own to ask it.
4. **Grammars compiled beforehand, with Shiki's raw engine.** Not built.
    - Pro: none of the 0.6 s of making patterns.
    - Con: that is all it saves: WebKit's 0.85 s of matching stays. And it is a new dependency.

#### Decision

The third.

- **Dependencies.** `@streamdown/code` is gone. `shiki` is a direct dependency, pinned to exactly 4.6.0, the version the lock file already had (MIT). No package's version changed in the lock file.
- **One module runs Shiki, and only in the worker:** `lib/chat/colouring.ts`. It has one highlighter for the worker's life, with the WebAssembly engine (`createOnigurumaEngine(import("shiki/wasm"))`), the two themes Streamdown asks for, and each language loaded the first time it is asked for. A load that fails, the engine's or a language's, is forgotten, so the next block that needs it asks again, three times in all: what the network dropped once may be there the next time, and a file that is gone, as one of an older version of the app under a page that stayed open, is not asked for at every piece of code that arrives. A language is found by its name or one of its aliases. One that Shiki doesn't know is coloured as plain text, never an error.
    - **Code that is arriving is coloured line by line,** not from its start each time. For the last four pieces of code in each language, the module keeps the rows of their whole lines and the grammar's state after them, and goes on from there when the next piece begins with that text.
    - **Only the rows the page doesn't have are sent.** The question says how much of the code, from its start, the page has rows for. The answer says how many rows to keep, those of the whole lines in that much, and carries the rows after them. Sending every row each time cost 19% more of the page thread's script time for a block of 300 lines (5.83 s against 4.89 s), and more for a longer one.
- **The worker** (`lib/chat/colouring.worker.ts`) takes a question with an id, and answers with that id and the rows, or with that id and an error.
- **The plugin the page gives Streamdown** (`lib/chat/code-colours.ts`) imports nothing of Shiki, only Streamdown's types. It starts the worker at its first call, so a page that shows no code starts none. It does so with `new Worker(new URL("./colouring.worker.ts", import.meta.url), { type: "module" })`, the form that Next.js 16.4.5's Turbopack bundles, in the production image and under `next dev`. A complete block's result is remembered, 2,000 rows in all, the one used longest ago going first, and given back at once when the same code is asked about again in the same language and themes.
- **`components/chat/markdown.tsx` gives Streamdown that plugin,** in place of `@streamdown/code`'s.
- **A static check holds the split** (`apps/web/eslint.config.mjs`): ESLint's `no-restricted-imports` forbids importing `shiki` anywhere in the web app but in `lib/chat/colouring.ts` and its test, and `no-restricted-syntax` forbids `import("shiki")` there too, which the first rule doesn't look at. One import of Shiki from a file of the page would bring the stall back, and no test would fail.

**How Streamdown takes an answer that comes later** (read in its source, version 2.7.1): a block of code calls the plugin's `highlight` at each change of its code. It shows what the call gives back at once. When that is nothing, what it showed stays, until the call's callback is called, and a callback does nothing once the block's code has changed again. So giving back nothing hands the block's text to an answer, which may come late, or never. Two consequences, both measured:

- **A plugin that answers later lets the text wait.** While a block has no answer, it shows its code as it was when the block appeared: with a worker that was still loading, the code stood still for 0.16 to 0.22 s at a page's first block.
- **Code that changes faster than the answers come is never coloured,** since each answer belongs to a call that is no longer the newest. Seen in WebKit: a whole answer stayed plain for a second.

What handles both is one rule. **A call gives back nothing only for code that is still arriving, when the page has colours for it and the worker has answered everything it was asked in that language and themes.** A complete block's call never gives back nothing: a block is asked about once more when it is complete and never again, so an answer that didn't come would leave its last text away. "Has colours" is a result of the worker, for code that this code begins with, in its language and themes, and not one for no code at all, as when a block has just opened. Then the answer comes in a moment, and what is shown stays until then, where plain code in its place would flicker at each piece that arrives. **Otherwise the call gives its code back at once, with the colours the page has:** the rows of that result's whole lines; then the line that was arriving, as that result's row followed by a plain piece for what the line has gained since; then each further line plain. With no such result, all of it is plain, in the shape Streamdown uses for its own. So the whole text is always there, and at most its newest characters have no colours yet. A block whose answers come late is coloured by its next call, from the answers that came, and its last call's own answer is always taken.

**An answer goes to its own call alone.** It updates the result the page holds for its language and themes, and a complete block's is remembered. Handing an answer about code that was arriving to the newest call as well was built first: on a page's first block, the code on the page then lost text for a frame when the first colours came, 7 characters in WebKit, 26 in Chromium and 94 in Firefox, since the answer was about less code than the block had been given plain. And a call that had given back nothing kept its block's older text for good if the worker stopped answering without an error.

**Without a worker, code stays readable.** In a browser without workers, such as the simulated one of the component tests, when the worker can't be started, when it reports an error, and when it has left 10,000 questions unanswered (`QUESTIONS_OUT_AT_MOST`), every call from then on gets its code back plain, and every call that waited is given its code plain, so that no block is left showing older text. There is no clock: a page asks about each block of code it shows before the worker can answer any, and a long conversation can show a thousand, while code that arrives asks a few dozen times ahead at the most. An answer that is an error does the same for its own call. Nothing reaches Streamdown as an exception, and the console is told once, not once a block. The plugin's two other methods, which Streamdown never calls, say yes to every language and list none: answering them truly would bring Shiki's list of languages to the page.

**Tests:**

- the worker's side, with the real Shiki and its WebAssembly engine (`lib/chat/colouring.test.ts`, which runs in Node: the simulated page's `Blob` can't unpack the engine):
    - TypeScript is coloured for both themes; a language by its alias as by its name, and one Shiki doesn't know as plain text;
    - code asked about piece by piece, cut at every line break, in the middle of every line, and with lines that end in `\r\n`, is coloured exactly as the whole is; an answer leaves out exactly the rows the page has;
    - with Shiki wrapped: the highlighter's engine is the WebAssembly one; code that arrives is coloured from where it had got, each whole line once; only the last four pieces of a language are kept; a language, or the highlighter, that couldn't be loaded once is loaded for the next block; and one that keeps failing is asked for three times in all;
- the worker's entry, with a stand-in for its scope (`lib/chat/colouring.worker.test.ts`): each question is answered under its own id, with the colours or with what went wrong;
- the page's side, with a stand-in for the worker that each test answers by hand (`lib/chat/code-colours.test.ts`): each rule above, with the plain rows' shape asserted as an object. Among them, a page's first block asked about seven times while the answers come late and then in a burst: whatever a call is given, at once or later, holds the whole of its code. And two thousand blocks asked about in one go, as a long conversation does, leave the worker at work;
- through the page, in the simulated browser (`components/chat/markdown.test.tsx`): code that arrives in pieces shows each piece as it comes;
- end to end, in a real browser (`e2e/code.spec.ts`): no worker until the first block of code, then one, also after a block in a second language; and a stored conversation with code in TypeScript, CSS, C++ and Python is coloured after a reload, by one worker.

No test asserts how long anything takes. What holds "the page's thread doesn't run Shiki" is the worker in the end-to-end tests, the ESLint rule, and the tests of the page's side.

#### Measured

After, on a production build of this change, in the same three browsers:

- **No frame over 35 ms** while a first block of Python, TypeScript, CSS, C++ or JSX arrives, in any of them.
- **The code on the page grows with every frame,** also while the worker is still loading for a page's first block, and never loses text again. Its colours come 0.12 to 0.18 s after it for the first block of a page, 0.02 to 0.08 s for the first block of a further language, and within a frame after that.
- **The stored conversation in eight languages** opens with a longest frame of 33 ms in Chromium, 33 to 60 ms in Firefox and 63 to 67 ms in WebKit, which is the page drawing the conversation, not colouring it. All of it is coloured after 1.0 to 1.2 s.
- **A block of 300 lines streams at the cost it had:** 4.51 s of script time on the page's thread, against 4.89 s, and no frame over 35 ms.
- **Blocks of code look as they did:** pictures of three blocks, in the light theme and the dark, are the same before and after, pixel for pixel.
- **It works under `next dev` too.**
- **When the worker's script can't be fetched,** code is shown whole and plain, the console is warned once, and the page reports no error.
- **When a language's file can't be fetched:** Turbopack's own loader asks for a file twice, so one refusal is never noticed. Refused twice, that block is plain, with one warning, and the next block in the language asks again and is coloured. Refused every time, the requests end with the third try. Seen in Chromium and WebKit.

#### Consequences

- **What is fetched,** over the wire, in Chromium, on a production build: the page, with the markdown code fetched ahead, 478 kB of script where it was 537 kB, since Shiki has left it (the markdown code alone is 241 kB where it was 300 kB); the first block of code 236 kB where it was 23 kB, of which the WebAssembly engine is 156 kB, once per browser cache.
- **The first block of a page gets its colours 0.12 to 0.18 s after its code.** Where the engine written in JavaScript didn't stall, that was 0.02 to 0.13 s. The code is plain and readable meanwhile, and nothing moves when the colours come.
- **A worker that stops answering without an error leaves one call's text waiting, until the block's next call:** the page can't tell that the question it gave back nothing for will never be answered. The next call shows the whole code, with the colours there are, and a block's last call, the one of the complete block, never gives back nothing. So no text stays away; the colours do, until the worker is done without, at 10,000 questions out.
- **The page holds one result for each language.** When a block of a language is coloured while another block of that language is still arriving, the arriving one can be plain for a moment. It never lacks text.
- **A Content-Security-Policy, once there is one** (see "Not done yet"), has to allow the worker (`worker-src 'self'`) and WebAssembly (`'wasm-unsafe-eval'` in `script-src`).
- **`shiki` is pinned to an exact version, as `ai` is:** its rows are what Streamdown draws, so an upgrade is a change to check with the tests of the worker's side, not to drift into.
- **An upgrade of Streamdown is a check of its own.** With `@streamdown/code` went the one place where Streamdown's own code plugin would have followed its upgrades. The shape of the plain rows, and the two calls the plugin answers, are Streamdown's, read from its source. What would notice a change:
    - the test through the page (code that arrives in pieces), if Streamdown stopped asking at each change of the code, or showing at once what it is given;
    - the end-to-end tests, `e2e/code.spec.ts` and the coloured code of `e2e/answers.spec.ts`, if it stopped asking the plugin, or drawing the rows it is given.

    The page's side's test holds the plain rows' shape as it was copied, not as Streamdown has it: whether the two still agree is read in Streamdown's source at an upgrade.

**What no longer holds:** under "Consequences", "only `components/chat/markdown.tsx` knows the library": the plugin in `lib/chat/code-colours.ts` knows Streamdown's types, and how it shows code. A block of code still fetches its language, into the worker now, and the first one fetches the worker and its engine too, which the table under "Measured" of 2026-11-14 doesn't count.

#### Why now, and not at the start

At the start, on 2026-11-14, the answers were measured in a headless Chromium alone, with a block of Python, and the library's own plugin was the one to take. In Chromium the stall is short: 0.12 s for a first block of TypeScript. It is WebKit's, ten times as long there, and longest at the languages with the largest grammars. The performance runs of 2026-11-20 were the first in all three browsers, and found it.

### 2026-11-21: an answer that can't be drawn is shown as written

#### Context

An error thrown while an answer was drawn as markdown took the whole page down: the conversation, the list and the box to write in were replaced by Next.js's error page ("This page couldn't load"), until a reload. Seen on 2026-11-21, on a production build of `9e7dfbf`, in Chromium 158, Firefox 159 and WebKit 27.2 alike: with the page open and the renderer already fetched, every script under `/_next/static/` was refused, and a question was asked whose answer holds a block of code.

- **The library fetches parts of its own while it draws.** Streamdown (2.7.1) fetches the part that draws a block of code when the first block is drawn, and the part that draws a diagram with the first diagram: each is a `React.lazy` inside a `Suspense`. `React.lazy` throws an import that failed while it renders, and React keeps a failed `lazy` failed. Nothing in the app caught an error of rendering below the page's root. Turbopack's own loader asks for a file twice before it gives up, so one refusal isn't enough to see it.
- **A file that can't be fetched is one cause,** as when the installation was upgraded under a page that stayed open. Any error of the library or of one of its plugins, on some text a model wrote, would do the same, and a model's words are not to be trusted.
- **The app already did the right thing for its own piece:** `components/chat/message.tsx` fetches the renderer itself (`fetchRenderer`), and shows an answer as it was written (`AsWritten`) until it is here, and for good when it can't be fetched.

#### Options considered

1. **An error boundary around each piece of an answer's text,** where `message.tsx` gives it to the renderer: the answer's words, and its reasoning.
    - Pro: a piece that fails takes nothing else along, and is shown as the reader saw it while the renderer was on its way.
    - Con: the whole piece is shown as written, also what of it could have been drawn.
2. **A boundary around the whole conversation.**
    - Con: every answer would be lost to one that fails.
3. **A boundary around each block inside an answer,** through Streamdown's `components`.
    - Pro: the rest of an answer keeps its formatting.
    - Con: it catches only what goes wrong in the blocks that are wrapped, and ties the app to more of Streamdown's inside.
4. **Leave it to Next.js's error page,** as it was.
    - Con: one answer takes the whole page down, until a reload.

#### Decision

The first. **An answer whose markdown can't be drawn is shown as it was written, and nothing else on the page is touched by it.**

- **`AsWrittenOnError` in `message.tsx`,** a class component, since React has no other form for an error boundary, stands around the renderer's component where `Words` renders it. When drawing a piece of text throws, it shows `AsWritten` in its place, with the same text and class. There is one for each piece of text an answer shows, so the reasoning and the words fail apart.
- **While an answer arrives, its text keeps coming:** each piece is shown as written. Drawing it is not tried again at each piece: a `lazy` that failed fails again each time, and React writes each error it catches to the console.
- **It is tried once more when the answer is complete,** when its `writing` goes from true to false: an error that came from half of something may be gone with the whole. If it throws again, the answer stays as written. A stored answer, which never arrives, is tried once.
- **The reader is told nothing, and the app logs nothing of its own.** React reports an error a boundary caught to the console by itself; in a production build Next.js does, with the error alone. The text as written is what the reader saw before, while the renderer was on its way.
- **Nothing else changed:** `fetchRenderer`, `useRenderer`, `RENDERER_WAIT_MS`, `markdown.tsx`, the code plugin and its worker. A model's words still reach the page through `markdown.tsx` or as plain text, never as HTML.

**Tests:**

- in the page's component tests (`components/chat/as-written.test.tsx`), with a stand-in for the renderer that throws for the text each test chooses, and what React writes to the console kept and asserted:
    - an answer whose markdown throws is shown with exactly its text, as written; the box takes the next question, whose answer is drawn;
    - while such an answer arrives, each piece of its text is on the page as it comes, and none after the first that threw is given to the renderer;
    - once it is complete it is tried once more: drawn when only its half-arrived text threw, and left as written, with one report more, when the whole throws too;
    - a stored answer whose markdown throws is shown as written beside the rest, and tried once;
    - an answer's reasoning that throws leaves its words drawn, and the other way round;
- end to end, in a real browser (`e2e/as-written.spec.ts`): after a first answer drawn as markdown, every further script under `/_next/static/` is refused, and an answer with a block of code is shown as written, its fence marks included, while the first answer stays drawn and the box stays; with the scripts let through again, a next question is answered as markdown. No error is left uncaught by the page's scripts meanwhile, which every end-to-end test checks.

#### Measured

With every script refused once the renderer was here, and an answer with a block of code asked for, in the three browsers:

- **before:** the page was gone in each of them, Next.js's error page in its place, and the page's scripts had left an error uncaught ("Failed to load chunk");
- **after:** in each of them the answer is on the page as it was written, the answer before it is still drawn as markdown, the box to write in is there, and the page's scripts left no error uncaught.

#### Consequences

- **An answer with a block of code whose part of the library couldn't be fetched is shown whole as written,** and so is every later one with code until the page is reloaded, since React keeps a failed `lazy` failed. The same goes for a diagram.
- **The reader is told nothing.** The answer reads as it did while the renderer was on its way, and a reload brings its formatting back, once the file can be had.
- **What a boundary can't catch stays as it was:** an error in an event handler or in a timer, which doesn't happen while rendering.

#### Why now, and not at the start

At the start, the one fetch that was thought of was the renderer's own, and its failure was caught where it was asked for: on the import, and since the revision "the renderer is fetched ahead" in `fetchRenderer`. That the library fetches parts of its own while it draws, where nothing the app catches on an import can reach, was found on 2026-11-21, when a production build was tried with its scripts refused.

### 2026-11-22: an arriving answer is drawn at most every 50 ms

#### Context

While a model's answer arrived, `Words` in `components/chat/message.tsx` gave the renderer the whole text of the piece that was arriving, at every piece that came in: the chat library publishes its messages at each event of the stream. So the page drew the arriving answer at every frame it painted: 57 to 60 times a second in Chromium and WebKit. In Firefox, whose frames were 8.3 ms there, that was 110 times a second for the 1,500 words of the table below, and at each of the 29 frames a second it still painted for the block of code. Each of those draws the piece's whole markdown again, and most of them no reader can tell apart.

Measured on production builds of `9d6539a` in a scratch stack, in a window of 1440×900, in Chromium 158, Firefox 159 and WebKit 27.2, with an answer's words let through one at a time by a pacing proxy. "A quarter of the speed" is Chromium with its CPU slowed four times.

| | Chromium | Chromium at a quarter of the speed | Firefox | WebKit |
|---|---|---|---|---|
| One block of code of 1,500 lines (71,530 characters) at 300 words a second, 36.8 s: frames a second | 57 | 26 | 29 | 59 |
| The same: frames longer than 50 ms | 0 | 234 | 189 | 1 |
| The same: share of the time the page's main thread was busy | 95 % | 99 % | not measurable | not measurable |
| The same: time in the page's scripts | 29.5 s | 27.1 s | | |
| 1,500 words with headings, lists, code and tables at 150 words a second, in a conversation of 600 messages, 10.7 s: main thread busy | 37 % | 99 % | (118 frames a second) | (60 frames a second) |
| 12,000 such words as fast as the stack delivers, 4.2 to 4.4 s: frames a second, and frames over 50 ms | not run | 18, 32 | 36, 1 | not run |
| One table of 200 rows at 300 words a second, 13.6 s: frames a second, and frames over 50 ms | not run | 29, 71 | not run | not run |

Typing into the box while the block of code arrived, at a quarter of the speed, a character was on the page after 20 ms (the median), and 65 ms at the most.

#### Options considered

The first five were built in throwaways, and measured as above:

1. **The chat library's own `throttle: 50`, as it is** (`useChat({ throttle: 50 })`). `@ai-sdk/react` 4.0.170 wraps the callback that publishes the messages in `throttleit` 2.1.0. The status and the error are not held back, and a change of status publishes the messages as they are.
    - Con: four component tests fail, and each shows the page for up to 50 ms with messages older than what the page knows beside them, because the page's own `setMessages`, after a conversation is read again (`resync`), goes through the same callback that is held back. In `chat-app.test.tsx`, "explains a model's failure, and tries again when asked": the failure's notice beside the empty answer it replaced, two articles where one is expected; "goes back into the box, with the reason": a refused question in the box and still on the page. In `newest-first.test.tsx`, "holds nothing back once it is gone when it is read again": a conversation that is gone still on the page. In `usage.test.tsx`, "is offered at the limit, and answers from then on".
    - Con: `throttleit` reads `Date.now()` with no care for a clock that goes backwards. After a clock set back, every change of the messages that comes without a change of status waits until the clock has caught up.
2. **The library's throttle only while an answer streams** (`throttle: streaming ? 50 : undefined`, with a state that follows the status).
    - Pro: all 393 component tests pass. The block of code kept Chromium's main thread busy 31 % of the time; at a quarter of the speed 43 frames a second, and 122 frames over 50 ms; Firefox drew 109 frames a second, 1 of them over 50 ms. Over the 1,500 words in 600 messages the page's scripts took 0.94 s, where they took 2.0 s before, and 1.9 s at a quarter of the speed, where they took 4.1 s, with the main thread busy 47 % of the time there.
    - Con: it rests on how that version's hook happens to subscribe again, and read the messages anew, when the option changes, which nothing documents. And a clock set back while an answer streams stops the answer's text until it ends.
3. **A pacing of our own on the text handed to the renderer, the wait counted from the moment a piece was handed on.**
    - Pro: the block of code kept Chromium busy 35 % of the time; Firefox drew 108 frames a second, 2 of them over 50 ms.
    - Con: once a draw takes 50 ms itself, the page draws back to back. At a quarter of the speed, in three runs: 46 to 48 frames a second for the block of code, with 43, 57 and 85 frames over 50 ms; and 39 to 41 frames a second for the table, with 50 to 59 over 50 ms and the main thread busy 83 to 86 % of the time.
4. **The same, the wait counted from when the piece was on the page.**
    - Pro: as the third at full speed and in Firefox: in a throwaway the block of code kept Chromium busy 32 % of the time, and Firefox drew 111 frames a second, 2 of them over 50 ms. At a quarter of the speed, on the final build (under "Measured" below): 45 to 52 frames a second for the block of code, with 5 to 44 frames over 50 ms in four runs; and 51 to 52 frames a second for the table, with 8 to 16 over 50 ms and the main thread busy 60 to 66 % of the time.
    - Con: the list of messages is still rendered at each piece, which the library's throttle saves.
5. **React's `useDeferredValue` on the text, alone.**
    - Pro: at a quarter of the speed its frames are short, since React draws a deferred value in slices: 51 frames a second, 34 over 50 ms; the 12,000 words 52 frames a second.
    - Con: it saves no work. The block of code kept Chromium's main thread 95 % busy, as before. At a quarter of the speed the 12,000 words were drawn only 7.6 times a second. And Firefox stayed at 39 frames a second, with 69 frames over 50 ms, where the fourth reaches 111.
6. **Keeping the objects of the messages that didn't change when a conversation is read again,** where `resync` makes every message a new object.
    - Con: not needed. Measured on the code before this revision, in a conversation of 600 messages: at Stop no frame over 16.8 ms in Chromium, one of 67 ms at a quarter of the speed, and 20 to 25 ms in WebKit; at each look for an answer that is written elsewhere, every second and a half, no frame over 16.8 ms in Chromium at either speed, and 20 to 23 ms in WebKit. The library's component is memoized on its text, so a message made anew with the same text draws no markdown again.

#### Decision

The fourth. **While an answer arrives, the text handed to the renderer changes at most every 50 ms, counted from when the text before it was on the page, and it is whole in the very render in which the answer ends. Nothing else on the page is slowed.**

- **One hook, `usePaced(text, arriving)`,** in `components/chat/paced.ts`, with `DRAWN_EVERY_MS = 50`:
    - text that isn't arriving is handed on as it is, in the same render, whatever was handed on before. It is then what was last handed on: should it arrive again, it goes on from there;
    - while the text arrives, the hook hands on what it handed on last. The text the component starts with counts as the first. A newer text is handed on once 50 ms have passed since what was last handed on was on the page: the moment is noted in an effect that runs when what was handed on changed, after React committed it, and not when it was handed on. So a draw that takes long doesn't use up the wait, and a slow device is never drawing back to back;
    - the newest text is handed on then, never those that arrived in between;
    - when the text stops arriving, it is handed on whole in that same render: not after a timer, and not an effect later;
    - only an earlier state of the same text is ever handed on in its place. When what was last handed on isn't the beginning of the text now, the text is handed on as it is, at once;
    - the time is read with `Date.now()`, as `untilStill` in `components/chat/follow.ts` reads it, so a test sets it (`tests/clock.ts`). A clock that went backwards holds nothing back;
    - while the wait isn't over, a timer looks again when it would be. It is cleared when a newer text comes, when the text stops arriving and when the component leaves the page, and none runs while nothing waits. When the wait is already over, the text is handed on from the effect itself, not by a timer of 0 ms: one set anew at each piece could be put off for as long as pieces keep coming.
- **`Words` in `message.tsx` uses it,** and what it gives goes to the renderer, to `AsWrittenOnError` and to `AsWritten` alike. So an answer's reasoning, which `Reasoning` draws through `Words`, is paced too. A question doesn't go through `Words`, and isn't.
- **Nothing else changes.** The chat library's messages, which message is being written, the status, the notices and the list of conversations are what they were, at every piece. No `throttle` on `useChat`, and no `useDeferredValue`. `Message` stays memoized.

**Why:** the library's throttle as it is holds back the page's own changes of its messages, and shows the page with older messages beside newer ones. Switched on only while an answer streams, it rests on behaviour that nothing documents, and a clock set back stops the answer. Of the two pacings, the one counted from the hand-on draws back to back once a draw takes 50 ms, which is what a slow device does: at a quarter of the speed it left 43 to 85 frames over 50 ms for the block of code where the one counted from the page left 5 to 44, and 50 to 59 for the table where the other left 8 to 16. `useDeferredValue` makes the frames short in Chromium and saves no work, and Firefox stays at a third of the frames. The fourth touches nothing but the text the renderer is given, was the best of them on a slow device, and as good as any in Firefox.

**Tests:**

- in the hook's tests (`components/chat/paced.test.tsx`), with a small page of the test's own that notes each text the hook handed on once React had put it on the page, with whether it was arriving, and the test's clock:
    - text that isn't arriving is handed on as it is, at once, also after it changed;
    - the text the page starts with is handed on at once; while the clock stands still, nothing newer, also after more than 50 ms of real time; once it has moved on, the newest, and none of those in between; 50 ms by the clock, and not 49;
    - the wait counts from when the text before was on the page: a page that moves the clock 30 ms while it puts a text on the page puts the next one off by as much. That one fails when the moment is noted when a text is handed on;
    - a clock that went backwards holds nothing back, before a newer text comes and while one waits;
    - a text that stops arriving is whole in that very render, never on the page with the older text once it no longer arrives, and goes on from there should it arrive again;
    - a text that doesn't begin with what was last handed on is handed on as it is, at once;
    - with the timers counted, none runs while nothing waits, one alone after a newer text came, the one before it cleared, and none once the text stops arriving or the page is left;
    - those in which effects matter, under React's StrictMode too;
- in the page's component tests (the same file), with a stand-in for the code that draws markdown that notes each text it drew once React had put it on the page, with its `writing` and whether the box could send, and a recorded turn sent piece by piece at the test's command (`sentByHand` in `tests/fake-api.ts`):
    - while the clock stands still, an answer is drawn from its first piece and from none of the later ones; once the clock has moved on, from its newest; and whole, with `writing` false, in the render in which the box can send again;
    - its reasoning is drawn at the same pace, and whole once the answer's words begin;
    - stopped while a piece of it is held back, it ends with what the server kept, as before;
- in the tests of an answer that is shown as written (`components/chat/as-written.test.tsx`), with the clock standing still: an answer whose markdown can't be drawn, and one that arrives while the code that draws markdown can't be had, show their first words, nothing newer however long, the newest once the clock has moved on, and the whole answer at its end. Without them `Words` could hand those two the text as it arrives, and no test would say so;
- end to end, in a real browser (`e2e/paced.spec.ts`): an answer of 300 words at 200 words a second, the length of its text noted from inside the page at each frame. Between its first change and its last, it changed at most `Math.ceil(elapsed / 50) + 3` times: each drawing at least 50 ms after the one before, the frame that shows one a frame late at the most, and one more for the whole answer at its end. And at least five times, the control that it came in pieces and was not drawn only at its end. In the end the whole answer is there. No assertion says how long a frame took.

No test that was there changed what it asserts. `as-written.test.tsx` sends its turns with the shared `sentByHand` now, in place of one of its own.

#### Measured

On a production build of this change, in the same stack, window and browsers, with the same answers. Two runs each, and four at a quarter of the speed for the block of code and the table, which vary most from run to run:

| | Chromium | Chromium at a quarter of the speed | Firefox | WebKit |
|---|---|---|---|---|
| The block of code: frames a second | 60 | 45 to 52 | 108 to 109 | 60 |
| The same: frames longer than 50 ms | 0 | 5, 12, 15 and 44 | 2 and 5 | 0 and 1 |
| The same: share of the time the page's main thread was busy | 32 to 36 % | 85 to 89 % | not measurable | not measurable |
| The same: time in the page's scripts | 8.2 to 8.8 s | 22.2 to 22.6 s | | |
| The same: times a second the answer's text changed on the page | 18 | 13 to 14 | 17 | 18 |
| The 1,500 words in a conversation of 600 messages: main thread busy | 24 to 25 % | 66 to 68 % | (118 frames a second) | (60 frames a second) |
| The 12,000 words: frames a second, and frames over 50 ms | not run | 28 to 34, 15 to 23 | 114, 0 | not run |
| The table of 200 rows: frames a second, and frames over 50 ms | not run | 51 to 52, 8 to 16 | not run | not run |

Each answer was as long on the page in the end as before, and ended when its stream did.

- **The two pacings again, back to back,** at a quarter of the speed, two runs each, since that case varies: counted from the hand-on, 43 and 57 frames over 50 ms for the block of code, and 58 and 59 for the table; counted from the page, as built, 5 and 12, and 16 and 11.
- **Typing meanwhile,** at a quarter of the speed, while the block of code arrived: a character was on the page after 11 ms (the median), and 55 ms at the most, where it took 20 and 65. While the 1,500 words arrived, 9 to 12 ms at either speed, as before.
- **Stop,** in a conversation of 600 messages: the box could send again 34 ms after the click in Chromium, 67 ms at a quarter of the speed, and 9 ms in Firefox and in WebKit, where it took 27, 60, 6 and 14 ms. The frames after it were as before: none over 17 ms in Chromium, one of 67 ms at a quarter of the speed, none over 27 ms in WebKit.
- **A reader's own scroll.** A wheel turned up at a random moment, while 600 words arrived at 100 words a second, was kept 8 of 8 times in each of the three browsers: the reader stayed where the wheel took them, and was offered "Go to the newest message". Before, 6 of 6 in Firefox and in WebKit.
- **With the real model,** on the maintainer's own stack (Qwen 3.5 4B through Ollama), a function with an explanation of it, asked in each of the three browsers: the answer's text changed 18 to 19 times a second while it arrived, it ended whole with its code coloured, and the page reported no error.

#### Consequences

- **The list of messages is still rendered at each piece.** That is cheap, since `Message` is memoized, but not nothing: with 600 messages, the page's scripts took 1.4 to 1.5 s over the 1,500 words, where they took 2.0 s before and the library's throttle left 0.94 s.
- **A word is on the page later than it was:** up to 50 ms after the text before it was drawn, and then for as long as its own drawing takes. An answer grows about twenty times a second at the most, instead of at every frame, and less often where a drawing takes long.
- **A component test that sets the clock, and reads an answer while it arrives, sees the text its words began with, and more only once it moves the clock on.** That is the answer's first piece when the events before it and that piece reach the page together, as they do when a test sends them at once; sent apart, the words begin with nothing. Timers run as ever there (`tests/clock.ts`), and the clock stands still.
- **Not this revision's, and not the page's work:** in Firefox, with a conversation of 600 messages on the page, one frame of 122 to 151 ms comes about four to six seconds after the conversation is whole on the page, in every run, also when the page does nothing at all, as in a run with the conversation open and nothing going on. The page does no work at that moment. What it is inside Firefox was not established. And in WebKit the frame in which the answer with the block of code ended took 187 to 243 ms in five of seven runs, before this change and with it; what in it takes the time was not looked for.

#### Why now, and not at the start

The page was first measured with long answers in the performance runs of 2026-11-20. This is the second half of what they showed about long conversations and long answers, the first being [ADR 0015](0015-the-web-chat-calls-the-api-from-the-browser.md)'s revision "a long conversation opens newest first".

### 2026-11-22: the renderer is fetched once the list of conversations is on the page

#### Context

Since the revision "the renderer is fetched ahead, and a stored answer waits for it", `ChatApp` (`components/chat/chat-app.tsx`) asked for the renderer as soon as the page knew whose session it has. The list of conversations is asked for at the same moment. On a fast connection the renderer's code arrived about ten milliseconds after the list's answer, and its evaluation, one task of 111 to 122 ms at a quarter of the CPU's speed, ran before the frame with the list's rows was painted.

Measured on 2026-11-22 in Chromium, in a wide window, on localhost, for a guest with 40 conversations, with the CPU at a quarter of its speed. Seven visits each, the builds swapped back to back, and each build first checked to wait for the list, or not, with the list's answer held back at a route; medians, the range in brackets.

A first visit, with nothing in the browser's cache:

| When the renderer is asked for | The list's first row is on the page after |
|---|---|
| At the first answer, before it was fetched ahead (`b63af23`) | 399 ms (390 to 442) |
| Once the page knows whose session it has (`4e77c01`) | 542 ms (502 to 746) |
| In the effect that runs once the list was read, with no frame awaited (a throwaway) | 422 ms (414 to 598) |
| Once the list was read and the frame that shows it has been painted (as built), two rounds | 417 ms (406 to 435) and 421 ms (412 to 455) |

A returning visitor, with the app's files in the browser's cache:

| When the renderer is asked for | The list's first row is on the page after |
|---|---|
| Once the page knows whose session it has (`4e77c01`) | 246 ms (231 to 251) |
| In the effect that runs once the list was read, with no frame awaited (a throwaway) | 196 ms (184 to 248): four visits at 184 to 196 ms, three at 215 to 248 ms |
| Once the list was read and the frame that shows it has been painted (as built) | 185 ms (169 to 204) |

On a first visit the code takes some 40 ms to arrive, and the rows were painted by then with or without the frame awaited. From the cache it is there two milliseconds after it was asked for: as built it was asked for 10 to 17 ms after the row was on the page, in each of the seven visits; with no frame awaited, which of the two came first differed from visit to visit.

At the CPU's full speed, on a first visit: 109, 119, 113 and 116 ms for the four, and no long task.

An earlier round of the same day, in which the first two rows read 391 and 532 ms, tried `requestIdleCallback` in place of a rule: 422 ms (398 to 428).

#### Options considered

1. **Once the session is known, as it was.**
    - Con: the list's first row 143 ms later on a first visit than before the renderer was fetched ahead, and 61 ms later for a returning visitor than as built.
2. **In the effect that runs once the list was read.**
    - Pro: on a first visit as good as the third.
    - Con: the order is left to how long the code takes to arrive. From the browser's cache that is two milliseconds, and in three of seven visits the rows came 20 to 60 ms late.
3. **Once the list was read and the frame that shows it has been painted.** A frame's callback (`requestAnimationFrame`) runs before that frame is painted, and a task begun from it runs after.
    - Pro: the order holds by the rule itself, whatever the connection and the cache: the code is asked for after the rows are on the page.
    - Con: the renderer is here later: on a first visit at a quarter of the CPU's speed its code was in at 453 ms, where it was in at 390 ms with no frame awaited. And a page that paints no frames doesn't ask ahead (below).
4. **When the browser is idle, with `requestIdleCallback`.**
    - Con: no better, and WebKit 27.2 doesn't have it: `typeof requestIdleCallback` is "undefined" there, and "function" in Chromium 158 and Firefox 159. One rule for every browser was wanted.

#### Decision

The third. **The page asks for the renderer once it knows whose session it has, the list of conversations has been read for the first time, or could not be, and the frame that shows that has been painted.**

- **In `ChatApp`:** an effect waits until the page knows whose session it has, and the list has items or an error (`list.items !== null || list.error !== null`, from `useConversations` in `lib/chat/list.ts`). It then calls `fetchRenderer` after the next paint, and calls that off when it is undone.
- **After the next paint** is a function of its own, `afterPaint` in `lib/after-paint.ts`: `requestAnimationFrame`, and in its callback a `setTimeout(…, 0)` that calls what it was given. It hands back a function that cancels both.
- **A list that can't be read** doesn't leave the page without the renderer.
- **Unchanged: whatever needs the renderer before that asks for it at once,** through `useRenderer` in `message.tsx`: an answer on the page, and a stored conversation that waits for it. That path waits for no frame. So a stored conversation opened by its address asks for the renderer as it did, side by side with the conversation, and still waits `RENDERER_WAIT_MS` for it at the most.
- **New: the first letter of a question asks for it too** (the box's change, in `components/chat/chat.tsx`). Nothing keeps a question from being written and asked while the list is still read, and a list's request can take long or never answer. An answer's words ask for the renderer only once they are on the page, so without this the first words of such an answer were shown as they were written until the code had come: what the revision "the renderer is fetched ahead" was made to end. The review of this change found it by reading, and a probe then showed it in Chromium, in six visits of six. Asking when the question is sent was tried first and is too late for a model that begins at once: Firefox still showed the first words as written (below).

**Tests:**

- in the page's component tests (`components/chat/renderer.test.tsx`), each from modules loaded anew: the renderer is not asked for before the session is known; nor while the list's first reading is on its way, although the session is known and the box is there; nor after the list has arrived until a frame has come, with the frames in the test's hands (`holdFrames` in `tests/frames.ts`); it is asked for when the list can't be read; it is asked for at the first letter of a question written while the list is still on its way, before anything is sent, and that question's answer is then never seen as written; and an answer on the page before any of that asks for it at once, with no frame coming. The test "is fetched once the page knows whose session it has, and not before" was changed on purpose: it is "is not fetched before the page knows whose session it has" now, and what comes after the session is held by the tests that follow it;
- in the tests of `afterPaint` (`lib/after-paint.test.ts`): nothing is called before the next frame, nor in the frame's callback; it is called once, in the task after it; and nothing is called once it is called off, before the frame or between the frame and that task;
- end to end, in a real browser (`e2e/renderer.spec.ts`): on a first visit, with the list held back at a route, no script that the document doesn't name is asked for in the half second after the box is there; once the list has come, one is. It failed on the code before this revision.

#### Measured

On the code as built, in Playwright's Chromium 158, Firefox 159 and WebKit 27.2, with every change of an answer watched from inside the page:

- **A stored conversation opened by its address,** three visits a browser: never shown as written. Its answers were on the page after 220 to 309 ms in Chromium, 230 to 252 in Firefox and 245 to 254 in WebKit, where the code before this revision took 216 to 231, 228 to 241 and 241 to 253 ms: that path was not changed. On the slow phone of [ADR 0015](0015-the-web-chat-calls-the-api-from-the-browser.md)'s revision of 2026-11-22, in Chromium, after 3.66 s, where it took 3.85 s.
- **The first answer of a visit,** once a browser: never shown as written.
- **A question written while the list never answers,** its first letters typed over a third of a second, four visits a browser, and three in Chromium at a quarter of the CPU's speed: never shown as written, in 15 visits of 15. Before the first letter asked for the renderer, the same probe saw the answer as written in Chromium in six visits of six.
- **The same question pasted and sent in the same instant,** with the test model, which begins its answer within some tens of milliseconds: never shown as written in Chromium and WebKit, four visits each; in Firefox the answer's first words were seen as written once, in three visits of four. The code came later than the words there.

#### Consequences

- **The renderer's download begins later,** by the list's reading and a frame. An answer that arrives before the renderer is here is shown as written until it is, as before.
- **One case is worse than it was:** while the list of conversations has not answered, a question that is pasted and sent in one instant, to a model that begins at once, can show its first words as written for a frame, as measured in Firefox. Before this revision the renderer was asked for when the session was known, and was here by then. Someone who writes a question, or takes a moment between pasting and sending, doesn't see it. What would end it for good is an arriving answer that waits for the renderer as a stored one does, a step of its own.
- **A page that isn't shown, as in a tab in the background, paints no frames,** and asks for the renderer only once it is shown, unless something on it needs the renderer before.
- **A component test that holds the frames** decides when the renderer is asked for ahead; one that doesn't gets the simulated browser's own frames, and the renderer a frame after the list.

**What no longer holds:** in the revision "the renderer is fetched ahead, and a stored answer waits for it", that `ChatApp` asks for the renderer as soon as the page knows whose session it has. The rest of it stands.

#### Why now, and not at the start

The revision that fetched the renderer ahead measured the largest paint, the time the page's scripts kept it busy and the first answer, not when the list's rows were on the page. A performance run after the steps of that week compared the page's first visit with the build before them, back to back, and found the rows later than before.
