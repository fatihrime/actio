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

**The libraries are fetched when an answer needs them.** The first answer fetches the renderer, a block of code its language, a formula its fonts, a diagram Mermaid. Until the renderer is there, and if it can't be fetched, an answer shows as it was written.

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
