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
