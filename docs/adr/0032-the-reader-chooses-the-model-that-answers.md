# 0032. The reader chooses the model that answers, in a menu by the box, and the browser keeps the choice

- **Status:** accepted
- **Date:** 2026-11-27

## Context

The page asked the installation's default model, by naming none, and a model that costs nothing while a usage limit is reached ([ADR 0021](0021-usage-limits-in-the-page.md)). An installation may offer several models: the example of free cloud models offers four beside the local one ([ADR 0027](0027-cloud-models-through-their-openai-compatible-layers.md)), and a model may name others that stand in for it ([ADR 0028](0028-a-model-names-those-that-stand-in-for-it.md)). `GET /api/models` lists them all, in the models file's order, and a turn's request may name one by its `id`, 32 characters at the most. The page had no way to.

What the choice had to fit:

- **The server is the truth** ([ADR 0015](0015-the-web-chat-calls-the-api-from-the-browser.md)): what a model can do, and whether it is ready, is the list's. The page reads the list again every 5 seconds while the model that answers isn't ready.
- **What arrives with the session fills a place that was there before it** (ADR 0015, revision of 2026-11-21). The chat itself is drawn only once the session is known, so nothing of it is in the HTML the server sends.
- **Two of the reader's choices are this browser's already,** kept in its storage: reasoning on or off, and light or dark (ADR 0015, revision "the reader chooses light or dark").
- **At a usage limit, the offer of a model that costs nothing has it answer until the limit starts over, and the usual one again afterwards.** ADR 0021 named a third option there, "the model picker, which comes with the router and with more than two models".
- **Much follows the model that answers,** from one value the page holds: the switch for reasoning, and what a question asks of it; the load ahead ([ADR 0026](0026-models-loaded-ahead-of-the-first-question.md)); the notice of a model that isn't ready, the blocked Send and the reads every 5 seconds; the line under the box (ADR 0015, revision "the page says how full the model's window is"); and the line under an empty conversation.
- **The conversation's component is drawn again at every piece of an answer,** and maps every message.
- **The box is at the page's foot,** in a row of its own with the switch for reasoning and Send, in windows as narrow as 320 px.

## Options considered

**Where the choice is kept:**

1. **With the conversation, on the server.**
    - Con: a column and a migration.
    - Con: a choice made before a conversation's first question has nowhere to live there, so a second place is needed anyway.
    - Con: a conversation opened later asks a model that the reader sees chosen nowhere else.
2. **With the account, on the server.**
    - Con: a guest has an account only for a day.
    - Con: one more route.
    - Con: the switch for reasoning and the scheme are the browser's.
3. **In the browser, one for every conversation.** Chosen.

**What it is:**

1. **The browser's own `<select>`.**
    - Con: a row is one line of plain text, and the next steps say more of each model: what it costs, that it keeps failing.
2. **A button with a menu, written here.** Chosen.
    - Con: its keys, its focus and its closing are the page's to get right, in three engines.

**A model that isn't ready:**

1. **It can't be chosen.**
    - Con: a stored choice can be found not ready at any load, so the page has to handle that state anyway.
    - Con: a row that can't be chosen hides the advice that the notice of a model that isn't ready gives, with the command that downloads it.
2. **It can be chosen, and its row says why it can't answer.** Chosen.

**What a question names:**

1. **Always the model shown.**
    - Con: a default that changed since the list was read is then asked for by its old name.
2. **Only a choice.** Chosen.

**At a usage limit:**

1. **The offer's button sets the picker's choice.**
    - Con: after the limit, the model that costs nothing stays chosen, against ADR 0021's "until the limit starts over".
2. **The model that stands in stays on top of the choice.** Chosen.

**With one model on offer:**

1. **A picker of one.**
    - Con: a button that changes nothing, in a row that has little room.
2. **Nothing.** Chosen.

## Decision

**A button first in the box's row says the title of the model that answers, and opens a menu of the models on offer above it. The choice is this browser's, one for every conversation. The model that answers is the one that stands in at a usage limit, then the reader's choice, each while it is on offer, then the default. A question names the model only when it is one of the first two; the default is asked for by naming none, as before.**

**The choice** (`lib/chat/chosen.ts`):

- **A model's `id`, in this browser's `localStorage` under `actio.model`.** There is no entry until the reader chooses.
- **Nothing kept is believed that isn't a string of 1 to 32 characters,** the longest a question may name (`MODEL_ID_MAX`), which a test holds to the API's description. Storage that is off limits, as in some private windows, means that the choice isn't remembered: it holds while the chat is on the page.
- **It is read when the chat is first drawn,** which is never in the server's HTML (`useChosen`). It is held above the conversation, in `ChatApp`'s `Current`: one choice for every conversation, new or stored, kept over a reload. Another tab reads it when it is loaded. A choice made in another tab is not followed.
- **Which model answers, and what a question names, are one function** (`answering`), of the list, the model that stands in at a limit and the choice:
    - the one that stands in, if on offer, which the question names;
    - else the reader's choice, if on offer, which the question names;
    - else the default, and the question names none.
- **A stored choice that isn't on offer** is left in storage, and not used.
- **Before the list is read,** no model is known to answer, and a question names the stored choice: one sent in that moment is not answered by another model than the one the picker shows a moment later. Once the list is read and the choice isn't on offer, a question names none.
- **Everything that follows the model that answers follows it:**
    - the switch for reasoning, and `thinking` in what is asked;
    - the load ahead, when the model comes to answer and when something is written, through `warmUp` alone, which keeps its minute for each model;
    - the notice of a model that isn't ready, the blocked Send, and the reads of the list every 5 seconds, which go by the model that answers and not by the default;
    - the line under the box, which says nothing of another model's figure;
    - the line under an empty conversation.
- **"Try again" names what a new question would name now.**

**The picker** (`components/chat/model-picker.tsx`):

- **It is shown once the models are read and more than one is on offer.** With one, the page is as before, in what it draws and in its elements: no button, no menu. Before the list is read, nothing.
- **First on the left of the box's row,** with the switch for reasoning to its right: the switch comes and goes with the model, and so never moves the button. The button is as high as Send, so the row is no higher with it, and nothing that was on the page moves when it comes.
- **The row stays one line in any width.** Beside the picker, the switch stands in a place that takes the room the picker leaves, and never less than "Think" needs. Its words give way first: it says "Think" alone while that place is under 12.5rem wide, by a container query on the place, as the line under the box has. Then the title gives way, and ends in "…". The switch's name is "Think before answering" whatever it shows. Without the picker, the switch is as it was.
- **The button** says the title of the model that answers, and is named "Model: " and that title, with `aria-haspopup="menu"`, `aria-expanded` and `aria-controls`.
- **The menu opens above the button,** since the box is at the page's foot: `role="menu"`, named "Model", in the page from when the button is, and `hidden` while it is closed. It is never higher than the room above the button, and never wider than the room from the button's left edge to the window's right one, both measured as it opens: it scrolls inside itself when the list is longer, and a row folds a title that is wider.
- **Each model is a `menuitemradio` button with `aria-checked`,** in the list's order: its title, and under it, for a model whose status isn't `ready`, why in a few words, "Isn't downloaded yet" or "Can't be reached", which is the row's description (`notReadyInShort`, beside `notReady` in `lib/chat/outcomes.ts`).
- **A model that isn't ready can be chosen.** Chosen, it brings the notice of a model that isn't ready, with what to do about it, and the blocked Send, as the default did.
- **The keys are the ARIA Authoring Practices' menu button's:**
    - on the button, Enter, Space and a click open and close the menu; ArrowDown and ArrowUp open it. Opening puts the keyboard on the checked row, once the menu is shown: a browser puts it on nothing inside an element that is hidden;
    - in the menu, ArrowDown and ArrowUp move, round the ends; Home and End go to them; Enter, Space and a click choose and close; Escape closes, choosing nothing. Each of those three puts the keyboard back on the button;
    - Escape in the open menu doesn't reach the handler that puts away the list of conversations lying over a narrow window's chat;
    - Tab closes the menu, and the browser moves the keyboard on from where the row was: to what follows the picker, or with Shift back to its button;
    - a press outside closes it, by the pointer, and moves the keyboard nowhere;
    - a press on the menu's own edge, between its rows, leaves it open with the keyboard on the menu itself, which can take it (`tabIndex={-1}`), and the arrows go on from there;
    - no type-ahead.
- **Every button of the picker is `type="button"`:** it stands in the box's form, where any other would send the question.
- **WebKit puts the keyboard on no button that is clicked.** So nothing rests on the button having it after a click: the page puts the keyboard on a row, and back on the button, and a press outside closes by the pointer, never by the keyboard leaving.
- **It can be used while an answer is being written, and while sending is blocked.** It changes nothing of an answer under way.
- **Opening it reads the list again,** without waiting for it: the rows say what is known, and change when the answer comes. Nothing is read again and again while it is open.
- **Whether it is open, and where the keyboard is in it, are its own state.** `ChatApp` makes it whole, and the conversation places it in the row: opening it doesn't draw the conversation again, and an answer arriving doesn't draw the picker again.
- **How it looks:**
    - the colours by their roles;
    - the checked row has a tick drawn in the colour of the words, and the menu a line around it, so that both show where a system's own few colours take the place of the page's;
    - the outline of the keyboard's place is drawn as elsewhere on the page, and also once a key of the picker's was used: as `ThemeChoice` found of WebKit, an engine may not say that the keyboard is in use on a button the page put it on;
    - the menu lies over the conversation, above the buttons on a block of code. The list of conversations that lies over a narrow window's chat lies above the menu: its shade and the list each went one layer up for it.

**At a usage limit,** as ADR 0021 decided:

- **"Use Local (free)" has that model answer until the limit starts over.** Then the reader's own choice answers again, or the default. While it stands in, the picker's button names it, and the menu checks it.
- **A choice made in the picker ends the standing in:** the reader's newest decision counts (`endStandIn` in `lib/usage.ts`). The limit stays reached.
- **Sending is blocked at a limit unless the model that answers is one of the refusal's models that cost nothing,** whether it came by the offer's button or by the picker. The notice then says "Local is answering, at no cost.", as before. What the notice says didn't change.

**How it is tested:**

- **The component tests** (`components/chat/model-picker.test.tsx`), through the whole page against the fake API, as a person would: one model and no picker, in what is drawn and in the elements, the switch for reasoning in the row itself as before; nothing before the list is read; two models, the default named and checked, in the list's order, and the switch beside the picker with a name of its own; choosing by mouse and by keys, and where the keyboard is after each; up and down among three models, which two can't tell apart; closing by Escape, by the button, by Tab and Shift+Tab, and by a press outside; a press on the menu's own edge, which leaves it open with the keyboard on it, the arrows and Escape working from there; Escape in a narrow window with the list over the chat, which closes the menu alone; Enter that chooses and sends nothing, and every button `type="button"`; a choice during an answer, which goes on and isn't stopped; the choice kept over a new page, in a new conversation and in a stored one; a question sent before the list is read, which names the stored choice; a stored choice not on offer, the default answering and named by none, the choice left in storage; a question with two models on offer and the picker never opened, the same request member for member as before; a model that isn't ready, its row's reason for each state, its notice once chosen, the blocked Send, and the list read every 5 seconds until it is ready; opening, which reads the list, and a row that changes when the answer comes; the switch for reasoning coming and going with the model, and `thinking` in each request; the line under the box empty for another model's figure, and back; at a usage limit, the stand-in named in the button and the menu, a choice in the picker that ends it, and the model that costs nothing chosen in the picker, which unblocks sending; and the reader's earlier choice named again once the limit has started over.
- **The choice's module** (`lib/chat/chosen.test.ts`): what is believed from storage, at 32 characters and 33, an empty string and nothing; a choice kept and read by the next page; storage off limits; and the order of `answering`, a choice of the default included, before the list is read too.
- **The load ahead** (`components/chat/warm-up.test.tsx`, which loads the app's modules anew for each test): a choice from an earlier visit read, and its model alone asked to be loaded, once, with and without React's StrictMode; a model chosen in the picker asked for when it comes to answer, one that isn't ready not, and none twice within the minute.
- **`tests/contract/limits.test.ts`** holds the 32 characters to `TurnRequest.model` in the API's description.
- **End to end** (`e2e/picker.spec.ts`), on the stack's seven models: "Local" chosen with the mouse, the request naming `local`, its answer, which cost 0 credits, and the choice still there after a reload and after "New chat"; the picker reached and used with real keys, the keyboard on the checked row once the menu is shown, moved, a model chosen with Enter that sends nothing of the question waiting in the box, the keyboard back on the button, Escape, and Tab and Shift+Tab from a row, which close the menu and go on to Send and back to the button; in a window 320 px wide, the row in one line inside the box, the switch showing "Think" and named by all of its words, the page no wider than the window and the menu inside it, and then a model chosen whose title has 21 characters, as long as the example's longest, which is cut short beside the switch (the stack's `hurried` has that title for it); and "Away" chosen, whose server isn't there, answered for by the fake under the default name, "answered by Fake model" under the answer. The older specs that give a request a model's name on its way out stay as they were.

## Measured

On a MacBook Pro (M1 Pro, 16 GB), 2026-11-27, in Playwright 1.65's Chromium, Firefox and WebKit, by scripts kept outside the repository: first against the stack of `make e2e`, the fake model under seven names, then against the stack of `make up` with `config/examples/free-cloud` in the place of `config/`.

**On the stack of `make e2e`, the same in the three engines** (a window of 1100 by 800 px unless it says otherwise):

- **The menu of seven rows opened above the button,** from 461 to 711 px down the window, the button at 719.
- **The keyboard was where the decision puts it** after each action: on the checked row once the menu was open, by a click, Enter or an arrow; on the button after a click on a row, in WebKit too, after Enter on a row and after Escape; and off the button after a press outside.
- **Nothing moved when the list of models came,** held back for a second and a half: the box and Send stood where they had, to a tenth of a pixel.
- **The button stayed where it was** when a model with the switch for reasoning was chosen after one without.
- **In a window 320 px wide,** with a title of 21 characters and the switch: one line, the button from 29 to 139 px, cut short, the switch saying "Think", Send from 223 to 291, the box from 16 to 304, the menu from 29 to 221, and the page no wider than the window. Light, dark, and dark with forced colours.
- **In a window 330 px high,** the menu began 8 px under the window's top, was 233 px high, and scrolled: its last row was in view once the keyboard was on it.
- **A box put into the conversation on layer 10,** where the buttons of a block of code and a table's sticky head are, lay under the open menu.

**What the review left to a run,** on the first build and on the tree as committed:

| | First build | As committed |
|---|---|---|
| A press on the menu's own edge | In Chromium and WebKit the menu stayed open with the keyboard on the page's body: the arrows did nothing, and Escape closed nothing | The keyboard is on the menu, ArrowDown goes to a row, Escape closes, in the three engines |
| A title of 80 characters, the longest the models file takes, in a window 768 px wide | The menu from 317 to 853 px, 855 in Firefox, and the page as wide | The menu from 317 to 760 px, the title folded, the page 768 px wide |
| What the switch says beside a title of 10 characters | "Think" in a window of 480 px, with 171 px free beside it, and of 800 px, with 203; at 768 px all of its words in Chromium and WebKit, and "Think" in Firefox | All of its words from 480 px up, "Think" at 320 and 400 px, the same in the three engines |

The first build's press was 2 px inside the menu's corner, which Firefox took for outside, since the corner is rounded: it closed the menu. As committed, the press was at the middle of the menu's top edge.

**What Tab needs.** The first build closed the menu in a timer, after the key, with a comment that said the row would be hidden otherwise before the browser moved on, "and Tab would start from nowhere". On a page of its own (a box, a button with a menu of two rows, a checkbox, a button), a menu closed at once went the same way as one closed in a timer, in each engine: Tab to the checkbox, Shift+Tab to the menu's button. So the menu closes at once, and the timer is gone. WebKit's Tab stops at text fields alone, as Safari's does unless it is set otherwise, and with Option at everything: there it was pressed with Option.

**Mistakes planted** in a scratch copy of the tree, one at a time:

- **61 on the first build, of which the tests caught 58.** The three they missed:
    - the switch without a name of its own beside the picker: the simulated page has no styles and reads the hidden words too. It has an assertion now, and the real browser's test at 320 px;
    - ArrowUp that went down: with two models, both ways end on the same row. A test with three models holds it;
    - the menu closed at once at Tab, instead of in the timer: nothing a test could see, and, as above, nothing a browser does. The timer came out.
- **61 on the tree as committed, all caught:** three of the list were written anew for what the fixes changed.
- **6 that only a real browser shows,** each on a stack built from the copy, with `e2e/picker.spec.ts` alone: the switch without its name, the keyboard put on a row before the menu is shown, a row that sends the box's form, the switch saying all of its words in any room, Tab leaving the menu open, and a title that doesn't give way. Five failed a test each. The sixth failed nothing: every title of the stack was short. The stack's `hurried` has a title of 21 characters since, the test chooses it at 320 px, and the mistake fails it.

The tests of the first build were written before any planting; those named above came after it, and after the review.

**On the stack of `make up`, with the example's five models,** the local one and four of Groq's and the Gemini API's, in Chromium:

- **The menu listed the five by their titles.** The page's first load had asked for the default's load (202).
- **"GPT-OSS 20B (Groq)" chosen:** its load was asked for (204: no Ollama server), the question named `groq-small`, its first word came after 500 ms, and it cost 57 credits. The line under the box said "1% full · 128 of 8,000 tokens".
- **"Qwen 3.5 4B" chosen, in the same conversation:** the line under the box said nothing until it had answered. Its question named `standard`, since it was chosen, and no second load was asked for within the minute. Its first word came after 3.3 s, and the line then said "1% full · 100 of 8,192 tokens".
- **After a reload** the button still said "Qwen 3.5 4B", and the browser kept `standard`.
- **In WebKit and Firefox,** with the local model alone, the same: the choice named in the question, and kept over a reload.
- **One question went to Groq in all, and none to the Gemini API.** The API's and the worker's logs had no warning.

## Consequences

- **Easier:**
    - an installation that offers several models has each of them a press away, in every conversation;
    - what follows the model that answers follows a choice by the same value it followed before;
    - nothing changed for an installation of one model, nor for a reader who never opens the picker: the request is as it was.
- **Harder:**
    - **the picker's keys, focus and closing are the page's own,** to be checked in three engines whenever it changes;
    - **the box's row holds one more thing,** and beside the picker the switch for reasoning says less in a narrow row;
    - **a question that names a model may meet a default that changed:** that is the reader's choice, kept until they choose another.
- **Known, and left** (each worked out, or read in the code, not seen):
    - **no type-ahead** in the menu;
    - **a choice made in another tab isn't followed:** each tab reads it once, when it is loaded;
    - **the choice isn't the account's:** another browser starts at the default;
    - **a long conversation can be continued with a model of a smaller window,** and after a change of model the line under the box says nothing until that model has answered once;
    - **an answer says which model wrote it only when one stood in** for the one asked;
    - **what a model costs, and that one keeps failing, was told to wait or has no budget, isn't said in the menu:** the next steps;
    - **a stored choice that is no longer on offer, named by a question sent before the list is read,** is refused by the API, "No model is called …", as that question's problem, with the question back in the box; the next one names none;
    - **storage that is off limits keeps the choice only while the chat is on the page:** the forms of the session take its place, and coming back reads nothing;
    - **the menu's height and width are measured as it opens:** a window resized while it is open keeps what it had;
    - **the menu can lie over the page's header** in a window too low for its list, since it is as high as the room above the button lets it be;
    - **with forced colours, the row the pointer is over, and the row the keyboard was put on by a press, show by no ground:** the outline is drawn once a key is used;
    - **the five end-to-end tests written before the picker still give a request a model's name on its way out,** which holds what they hold; choosing in the page instead would be a change to each.
- **How to reverse it:** `components/chat/model-picker.tsx` and `lib/chat/chosen.ts`, with `answering` back in `components/chat/chat-app.tsx` with the stand-in alone; `named` and `picker` in `components/chat/chat.tsx`, which names the stand-in again, and blocks sending at a limit while it doesn't answer; the picker's place in `components/chat/composer.tsx`; `endStandIn` in `lib/usage.ts`; `notReadyInShort`; the two icons; and their tests. The layers of the list over a narrow window's chat can stay. The API is unchanged.

## Revisions

### 2026-11-27: an answer says which model wrote it, where there is a choice

The decision left it under "Known, and left": "an answer says which model wrote it only when one stood in for the one asked". The line under an answer, what it took, named a model only then (ADR 0028, revision "a turn says which model answered, and the page says so"), which had weighed an item under every answer and decided against it: nearly every answer was the default model's, so the item would have said the same of each. The picker changed that. Two answers of one conversation can now be two models' own, each the model asked, and nothing on the page said which wrote which: seen in the decision's look on the stack of `make up` (Measured, above), an answer by "GPT-OSS 20B (Groq)" and one by "Qwen 3.5 4B" in one conversation, with nothing under either that told them apart. It was left so that the picker could be built and looked at first.

**When the line names the model that wrote the answer:**

1. **Only when the answer's model isn't the one chosen now.**
    - Con: every line of a conversation would change when the reader chooses in the picker, and what an answer says of itself would depend on a choice made after it.
2. **Only in a conversation whose answers aren't all one model's.**
    - Con: an answer by another model would change the lines of the answers above it.
3. **Under every answer, with one model on offer too.**
    - Con: ADR 0028's reason against it holds there: a line that says the same of every answer, what the empty conversation says already.
4. **Under every answer whose record names the model that wrote it, but the asked model's own answer where it is the one model on offer.** Chosen.
    - Pro: what an answer says of itself rests on its record and on what the installation offers, never on the reader's choice or on the other answers.
    - Pro: on an installation of one model, that model's own answers read as before.
    - Con: on an installation of several models, every answer's line is one item longer, and in a narrow window a row higher.

**What a stand-in's item says:**

1. **The same words as for the model asked,** "answered by" and its title.
    - Con: with the name under every answer, nothing would say that another model answered than the one asked.
2. **"answered by" its title "in place of" the asked model's.** Chosen: "answered by Fake model in place of Away".

**A turn whose record names no model,** given up on, or recorded before 2026-11-25:

1. **Taken for the asked model's,** as its figures and the line under the box take it (`answeredBy`).
    - Con: a claim the record doesn't make. A turn given up on may hold a stand-in's draft as its answer.
2. **Nothing said of it.** Chosen, however many models are on offer.

**A model the installation no longer offers:**

1. **Not named.**
    - Con: the reader would take the answer for a model's that is on offer. ADR 0028 decided the same for a stand-in.
2. **Named by its id in the record.** Chosen, for the model that wrote the answer and for the model asked alike.

**A long title.** With the item under every answer, and two titles in a stand-in's, a long title is in every line. Measured before the build (below): in a window 320 px wide, an item holding a title of 80 characters in one word, the longest the models file takes, stood from 16 to 674 px, and the conversation scrolled sideways. So no item is wider than the line: one wider than the row shrinks to it, and its words fold there, inside a word when one alone is wider than the row (`wrap-anywhere`). `wrap-break-word` would fold the word too, but leaves the item's smallest width as it is, so that a flex item doesn't shrink with it. An item that fits is drawn as before: it goes to the next row whole before any of its words fold. The first build also gave the item a class of its own to shrink by (`min-w-0`). The review read it as doing nothing beside `wrap-anywhere`, a run in the three engines showed so (Measured, below), and it came out.

**What changed** (`apps/web`):

- **`figuresOf`** (`lib/chat/figures.ts`) ends the line, once the models are read and for a turn that isn't running, with `writtenBy`:
    - nothing, when the record names no model that wrote the answer (`answered_by` empty);
    - "answered by" the writer's title "in place of" the asked model's, when they differ;
    - "answered by" the writer's title, when they are the same, unless that model is the one model on offer: the list has exactly one, and it is this one;
    - a model the list doesn't have, by its id in the record.
- **The name doesn't go through `answeredBy`,** which keeps its fallback to the asked model for its other callers, the line under the box and the notices of how a turn ended. Nothing of theirs changed.
- **Each item of the line** (`Figures` in `components/chat/message.tsx`) folds its words anywhere when one is wider than the row, and shrinks to the row with them: `wrap-anywhere`.
- **Unchanged:** the order of the three figures, the item last, so that nothing else in the line moves when the models come; the list's role and name, and `aria-live="off"`; where it stands, the answer's next sibling; when it comes, a frame after an arriving answer's last words and at once for a stored one; no line without a figure or an item. The models reach the line through `ModelsOnOffer`, read by `Took` alone, as before: no answer's words are drawn again when they come or are read again. The API, what a question names, the picker, `contextFill` and the notices.

**Tests:**

- **the rule** (`lib/chat/figures.test.ts`): the asked model's own answer with it alone on offer, with another beside it, and with another alone; a stand-in's with both on offer, and with the asked model alone; an answer asked of a model no longer on offer; the item last after the three figures, and alone when the record has no other figure; nothing for a record that names no model, with one model on offer, two, or one it doesn't name; nothing before the models are read; and nothing while the answer is written. The stand-in's expectations have the new words;
- **on the page** (`components/chat/answered-by.test.tsx`, which loads the app's modules anew for each test): the three tests of a stand-in's answer, with the new words; and with two models on offer, the asked model's own answer that arrives, its item last in the line, which comes a frame after its last words; a stored conversation of two answers by two models, each line naming its own at once when the models were read before it, with no frame to wait for, each the next sibling of its answer; the same conversation when the models come after it, the item added last to each line, whose other items stay the same elements, and neither answer's words drawn again; and an answer kept from a turn that was given up on, whose record names no model, with two models on offer: no line under it. `components/chat/figures.test.tsx`, with one model on offer, passes unchanged;
- **end to end,** on the stack's seven models: a fourth item, "answered by Fake model", under the default model's answer, and the same after a reload (`e2e/figures.spec.ts`); "answered by Fake model in place of Away" (`e2e/stand-in.spec.ts`, which still gives its request the model's name on its way out, and the test of `e2e/picker.spec.ts` that chooses "Away"); and a conversation with an answer by the default model, then "Local" chosen in the picker and a second answer, the two lines ending with "answered by Fake model" and "answered by Local", the same after a reload (`e2e/picker.spec.ts`).

No component test holds that a long title folds: the simulated page has no layout. It is measured in the three engines.

**Known, and left** (worked out from the rules, and not seen, unless it says measured):

- **the name isn't said while the answer arrives,** only once its turn has ended, as the rest of the line;
- **a turn that was given up on, and the turns recorded before 2026-11-25, don't say which model wrote them;**
- **the item comes when the models are read,** which after a page load can be a moment after a stored conversation's answers are drawn; in a window too narrow for the line's one row, each line then takes a row more. Measured below: the reader keeps their place;
- **a model is named by its id once the installation no longer offers it,** and a model whose title changed is named by its new title;
- **an installation that changes the model behind a name it keeps says nothing of it:** the record names the model by its name in the models file;
- **on an installation of one model, nothing is said under that model's own answers,** as before. An answer there names a model only when another one wrote it or was asked: one that stood in, or one the installation no longer offers;
- **the dot before an item that folded over several rows stands at the middle of the item's height,** as it did before for one folded at its spaces. Seen in the look at titles of 80 characters.

#### Measured

Before the build, on 2026-11-27: a MacBook Pro (M1 Pro, 16 GB), Playwright 1.65's Chromium, Firefox and WebKit, by scripts kept outside the repository, against the stack of `make e2e` built from `8290909`, with "Away" chosen in the picker, so that every answer had the item, as every answer on that stack has it after this revision.

- **The item coming late.** Nine answers, a window 320 by 700 px, the list of models held back by the look until 1.5 s after the conversation was drawn again, then let go. The same in the three engines: each line was 16 px high before the list came and 36 px after it, a second row, and the conversation grew by 180 px. A reader at the conversation's end was at its end before and after. A reader who had gone up 1,500 px and reloaded there: the message at the top of the window stood at the same place before and after, to a tenth of a pixel, in each engine.
- **Without the look's hold,** three reloads in each engine: the item came in the same change of the page as its line. The list of models was answered 12 to 19 ms after it was asked (Chromium and WebKit).
- **A long title,** the default model's replaced on its way to the page, in a window 320 px wide, the same in the three engines: 80 characters in several words, the line four rows (72 px) inside the window; 80 characters in one word, the item from 16 to 674 px, and the conversation scrolling sideways; 21 characters, the longest of `config/examples/free-cloud`, two rows (36 px).

On the tree as committed, the same day, machine and engines.

**Against the stack of `make e2e` built from it,** the same in the three engines:

- **One conversation of three answers,** in a window 1100 px wide: the default model's, then "Local" chosen in the picker, then "Away". The lines ended "answered by Fake model", "answered by Local" and "answered by Fake model in place of Away", each one row, 16 px high, 8 px under its answer, and read the same after a reload. The API's records of the three turns: `standard` answered by `standard`, `local` by `local`, `away` by `standard`. In a window 320 px wide, light, dark, and dark with forced colours: the lines 36, 36 and 56 px high, which is two rows, two, and three, and neither the page nor the conversation wider than the window. No script on the page failed.
- **The item coming late,** as before the build, but with no model chosen, each answer the default model's own: the figures of before the build, in each engine. A line 16 px high before the list came and 36 px after it, the conversation 180 px higher, the reader at its end still at its end, and the message at the top of the window of the reader who had gone up at the same place. Without the hold, the item came in the same change of the page as its line.
- **A long title,** the default model's and Away's both replaced, so that the item held two: 80 characters in one word each, the item from 16 to 304 px, the line 152 px high, and the conversation no wider than the window; 80 characters in several words, 104 px; 21 characters, 72 px. The three figures before the item stood where they had stood before the build, to a pixel.
- **The end-to-end tests,** on a stack of their own: 86 passed.

**Which class the fold needs.** On a page of its own, with the line's elements and the styles its classes compile to, 320 px wide, the item holding a title of 80 characters in one word, the same in the three engines: with neither class, and with `min-w-0` alone, the conversation was 667 px wide inside 320; with `wrap-anywhere` alone, and with both, 320. With `min-w-0` alone the item's own box ended at 304 px, and its words ran on past it. With a title in several words, or of 21 characters, the four gave the same places.

**With two local models,** on the stack of `make up` with a models file made for the look: Qwen 3.5 4B as the default, Granite 4 Micro beside it, both on the machine's Ollama, and a third, "Elsewhere", on a server where nothing listens, which the default stands in for. One guest in each engine, three made-up questions: to the default, then to Granite chosen in the picker, then to "Elsewhere". What the page showed was the same in the three: "answered by Qwen 3.5 4B", "answered by Granite 4 Micro" and "answered by Qwen 3.5 4B in place of Elsewhere", last in lines of one row at 1100 px, such as "50 tokens a second · first word after 2.4 s · 88 credits · answered by Granite 4 Micro", and the same after a reload; at 320 px, 36, 36 and 56 px. The API's log for each third question: "model elsewhere is unreachable, and is passed over", then "model standard answered in place of elsewhere". No request went outside the machine.

**With the shipped models file,** one model, on the same stack, in Chromium: no picker, and under an answer "first word after 410 ms · 21 credits", with no model named, the same after a reload.

**Mistakes planted** in a scratch copy of the tree, one at a time:

- **26 plantings on the first build:** 22 mistakes in the rule and in the line, four of them planted a second time with the page's tests alone, the rule's own table left out. 24 failed a test. The two that failed none:
    - a record that names no model taken for the asked model's, with the page's tests alone: the rule's table caught it, and nothing on the page did. A test was written: the answer kept from a turn that was given up on, with two models on offer, has no line under it;
    - the fold's class taken away: no component test can hold it, as said above. The look at a long title holds it.
- **26 on the tree as committed:** 25 failed a test, and the fold's class none.
- **2 against a stack built from the copy of the first build,** with the three end-to-end specs that read the line, which didn't change after it: the asked model's own answer never named, and a stand-in's item in its old words. Each failed two tests.

The tests of the first build were written before any planting; the one named above came after it.

### 2026-11-27: the list says what a model costs, and the menu's rows say it where the models differ

The decision left it under "Known, and left": "what a model costs, and that one keeps failing, was told to wait or has no budget, isn't said in the menu: the next steps". This revision is the first half of that sentence; the second is the next step's. A model has a price, in the plans file ([ADR 0020](0020-usage-limits-on-turns.md)): three whole numbers, in credits per million tokens, for what it reads, what it writes, and what it reads again from its server's cache. Nothing said it before an answer was paid for: `GET /api/models` didn't have it, and a row of the menu said a title and, for a model that can't answer, why. With the picker, a reader chooses among models that may each cost something else, and saw what one cost only under its first answer.

**What a row says of the cost.** The plan named it "cost multipliers".

1. **A multiple of the default model's cost,** such as "4×".
    - Con: a multiple needs a model to be measured against, and the one at hand, the default, may cost nothing: an installation whose local model is free and whose cloud models are counted has no multiple to say.
    - Con: one number can't stand for two prices. What a turn costs is its tokens read at one price and its tokens written at another, and the two don't come in a usual proportion (Measured, below). So a multiple worked out for any fixed mix is wrong for most turns as soon as two models' prices aren't in proportion to each other.
2. **The credits of a usual answer,** such as "about 600 credits a question".
    - Con: a usual answer is a fixed mix of tokens read and written, which the turns measured don't have: the same reason as the multiple's second.
    - Con: it is a forecast, which the page doesn't make: the page adds up no credits ([ADR 0021](0021-usage-limits-in-the-page.md)), and what an answer did cost is said under it.
3. **The price itself, in credits a token.** Chosen.
    - Pro: always there and always true, and in the unit the page already shows under every answer, "73 credits".
    - Pro: the number said first is a multiple all the same, of the shipped price for what a model writes: there a credit is one token of an answer. So "2 credits a token" is twice the shipped price of what is written, and eight times that of what is read.
    - Con: two numbers where what a model reads and what it writes cost apart.

**The decision: the list says each model's price as the installation states it, and a row of the menu says it in words, under the title, wherever the models on offer don't all say the same.**

**The list** (`actio.api.catalog`):

- **Each model has a `price`,** three whole numbers in credits per million tokens:
    - `input`, for what it reads: the conversation so far;
    - `output`, for what it writes: its answer and its reasoning;
    - `cached_input`, for what its server says it read again from its cache. Always a number: the price of `input` where the plans file states no other, as a turn is counted, and 0 where the file states 0.
- **It is the model's own price, by its name in the models file:** never that of a model that stands in for it, since a question to it costs this whichever of them answers ([ADR 0028](0028-a-model-names-those-that-stand-in-for-it.md)). All three are 0 for a model that costs nothing.
- **The route joins it,** from the plans it reaches through the usage limits: `actio.catalog` and `actio.usage` don't import each other. Nothing below `actio.api` changed.

**The words** (`lib/chat/prices.ts`, `priceInWords`), from `input` and `output`, each said as that number divided by a million:

- both 0: "Free", the page's word for it already ("Use Local (free)");
- both alike: one number, "1 credit a token", "0.5 credits a token";
- otherwise what it writes first, then what it reads: "1 credit a token written, 0.25 read", "1 credit a token written, 0 read";
- "credit" where the first number said is exactly 1, "credits" otherwise;
- a number is said exactly, never rounded: a price is a whole number of millionths of a credit a token, so six decimals at the most, and no zeros after the last digit, "0.000001 credits a token";
- what a model reads again from its server's cache is not said in a row. The list has it.

**Said only where the models differ** (`pricesSaid`). Each model of the menu has its words worked out. When every model's words are the same, no row says a cost; otherwise every row says its own, those that are alike too. Five rows that each say "1 credit a token written, 0.25 read" tell a reader nothing to choose by; one "Free" among them does, and then the others' prices are what it is free against. The rule compares the words, not the prices: two models that differ in what they read again from the cache alone say the same words, and say none. So does one model alone.

**Where, and in which order:**

- **In the menu's row alone:** not on the picker's button, not under an answer, not in the offer at a usage limit.
- **Under the title, after the reason a model can't answer:** the title, then "Can't be reached" or "Isn't downloaded yet" if there is one, then the cost. Each is a line of its own, in the small muted style the reason has.
- **The row's name stays its title alone.** Its description is the reason and the cost, the reason first, each only when it is there: "Can't be reached 1 credit a token".

**What changed** (`apps/web`):

- **`ModelPicker`** works out what its rows say from the list it is given, as it was read last, and hands each row its line or nothing. Nothing new is passed to the conversation, its box or its messages: opening the picker, and what its rows say, draws no message again, as before.
- **`Choice`** draws the line under the reason, and names both in its description.
- **Unchanged:** the picker's keys, its focus and its closing; the button; what a question names; the offer at a usage limit; the line under an answer.

**The end-to-end stack** (`deploy/e2e/config/plans.toml`): the model that names conversations, `namer`, has the shipped price now, `input = 250_000`, `output = 1_000_000`, `cached_input = 25_000`: the one model of the stack whose row says two numbers, and whose price has a `cached_input` of its own. It is still not free, so still not offered at a limit, and a title costs its account nothing whatever the price. So the stack's menu shows the three forms: "Fake model", "Hurried, in 3 seconds", "Rationed", "Queued" and "Away" say "1 credit a token", "Local" says "Free", and "Namer" "1 credit a token written, 0.25 read". No row there says a reason: "Away", whose server isn't there, is listed `ready` through the model that stands in for it.

**Tests:**

- **the list** (`apps/api/tests/test_catalog_api.py`): a price of three different numbers listed as those three; one whose file states no `cached_input` listed with `input`'s; one whose file states a `cached_input` of 0 listed with 0; a model that costs nothing with three zeros; a model whose own server can't be reached, ready through the model that stands in for it, listed at its own price and not at the stand-in's; and the member and its three numbers in the API's description, each described. The test of the whole list has the tests' own price in it, a credit a token for all three;
- **the rule** (`lib/chat/prices.test.ts`): the words of the table above, row by row, numbers said exactly, "credit" and "credits", "Free" only when both are 0, nothing of the cache; and which rows say theirs: none with all alike, all with one different, none for two that differ in what is read from the cache alone, and none for one model alone;
- **on the page** (`components/chat/model-picker.test.tsx`): rows that say their costs where the models differ, as their descriptions, and are still named by their titles alone; no row that says a cost where every model costs the same, nor the words in the menu, nor a description of any kind; a row that can't answer where no cost is said, which says the reason and nothing else; a row that can't answer where costs are said, which says the reason first and then the cost, in its description and in what it draws; that a description names the lines that are there, and no other; and the costs of a new read of the list, which opening the menu asks for. The fake API's model has the shipped price, and its model that costs nothing three zeros: so where a test offers both, the rows say their costs, and the five assertions on a row's description, in two tests, changed with that, as did the one that read the rows' text for their titles, which reads their names now;
- **end to end** (`e2e/picker.spec.ts`): the menu of the stack's seven models, with the descriptions of "Fake model", "Local", "Namer" and "Away", each row named by its title alone, and "Namer"'s cost drawn as a line under its title, inside the menu.

**Known, and left** (worked out from the rule, and not seen, unless it says measured):

- **the price of what a model reads again from its server's cache isn't said in a row:** the list has it;
- **a price in credits a token is no forecast of what an answer will cost:** that depends on the conversation behind the question and on the answer's length, and the line under an answer says what it did cost;
- **the meter under the list of conversations says shares of the limits, not credits,** so a row's credits can't be held against it by reading: an answer's credits under it and the meter's move are what connect the two;
- **where every model costs the same, nothing says what that is** before an answer was paid for;
- **a reader whose plan has no limits is told the prices all the same;**
- **whoever has a session, a guest too, can read every model's price in the list;**
- **nothing says that a question to a model costs its price also when another model answers in its place:** the line under such an answer names both models, and not whose price it was;
- **the menu is higher by a line a row where costs are said:** in a low window it scrolls inside itself sooner;
- **a screen reader is given a row's reason and its cost as one description,** the two run together, "Can't be reached 1 credit a token": nobody listened to one.

**How to reverse it:** `lib/chat/prices.ts` and its test; the line and the description in `Choice`, and `pricesSaid` in `ModelPicker` (`components/chat/model-picker.tsx`), with their tests, and the prices of the fake API's models. `price` in `ModelOut`, and `PriceOut` (`actio.api.catalog`), with their tests, and `Price` in `lib/api/client.ts`, then `make openapi`. `namer`'s price in the end-to-end stack can stay.

#### Measured

Before the build, on 2026-11-27, on a MacBook Pro (M1 Pro, 16 GB):

- **How a turn's cost divides between reading and writing.** The usage ledger of the development stack on this machine, 2026-11-14 to 2026-11-27: 516 turns whose tokens their provider counted and that read and wrote something (of 535 rows), most of them short questions asked while building and testing, none of them people's traffic. At the shipped prices (0.25 credits a token read, 0.025 read again from the cache, 1 written), the share of a turn's cost that was its answer: 7 % at the tenth percentile, 49 % at the median, 97 % at the ninetieth; 67 % of all their credits together. Tokens read for each token written: 0.15 at the tenth percentile, 5.7 at the median, 51 at the ninetieth.
- **That a price is said exactly.** `new Intl.NumberFormat("en", { maximumFractionDigits: 6 })` of a price divided by a million, held against the digits worked out as text, for every price from 0 to 2,000,000, 2,000,000 more drawn at random up to 100,000,000, and twelve edges (4,000,013 in all, Node 24.11.1): none differed.

On the tree as committed, the same day, in Playwright 1.65's Chromium, Firefox and WebKit, by scripts kept outside the repository, against the stack of `make e2e`. Each figure was the same in the three engines:

- **The rows,** in a window 1100 by 800 px: "Fake model", "Hurried, in 3 seconds", "Away", "Rationed" and "Queued" said "1 credit a token", "Local" said "Free", and "Namer" "1 credit a token written, 0.25 read". Each row was 48 px high, a title's line of 20 px and a cost's of 16 px, and the menu 240 px wide and 346 px high, inside the window, with nothing to scroll inside it. A row was found by its title alone.
- **With every model's price made the same on its way to the page,** no row said a cost, and each was 32 px high.
- **A window 320 px wide,** light, dark, and dark with forced colours: the menu from 29 to 269 px, the page no wider than the window, no line past its row.
- **A window 360 px high:** the menu from 8 to 271 px, scrolling inside itself, the checked row in view.
- **The longest words a price makes,** "99.999998 credits a token written, 99.999999 read", beside a title of 80 characters and a row that said "Isn't downloaded yet" too, at 320 px wide: the menu from 29 to 312 px, inside the window, each cost on two lines inside its row, the page no wider than the window.
- **That a price is said exactly by each engine's own number formatting:** the comparison above, run inside a page of each (Chromium 158, Firefox 159, WebKit 27.2), 4,000,013 prices: none differed.

**With real models,** on the stack of `make up` built from that tree, with a scratch pair of files in the place of `config/`: Ollama's `qwen3.5:4b-q4_K_M` at the shipped price, its `granite4:micro` at nothing, and a model at an address nothing listens at, at three times the shipped price, which the first stands in for. In Chromium:

- **The rows said** "1 credit a token written, 0.25 read", "Free" and "3 credits a token written, 0.75 read". The third said no reason: it is listed `ready` through its stand-in.
- **One question to each, and each answer cost what its row's price makes of its tokens,** rounded up once: 81 read and 32 written, 53 credits; 135 read and 29 written by the model that costs nothing, 0 credits; and 196 read and 25 written by the first model in the third's place, 222 credits, the third's price.

**Mistakes were planted,** one at a time, in a scratch copy of the tree, never the working tree:

- **On the first build, 39,** against the tests of the list, the rule and the menu: in the route (the price of the model that would answer first, or of the default; reading and writing changed places; what the cache costs where the file states none; a description gone), in the words (when "Free", "credit" or "credits", a number rounded, the order, one number or two, the unit), in which rows say theirs, and in the row (whose cost, the description and its order, the row's name, a cost kept from the first read). 37 failed a test. Of the two that failed nothing, one was a gap: a row with nothing to say whose description named lines that aren't there reads no differently. The tests now hold which lines a description names, and that a row with nothing to say has none.
- **The review found a second gap by reading:** with the fake API's models priced, every row of the tests that said a reason said a cost too, and no test was left of a row that says a reason alone, as each row of five models at one price does when its server is away. Two broken versions of the row would have passed. It has a test now, and both versions fail it, and it alone.
- **On the tree as committed, 44,** those two among them: 43 failed a test. The one left takes the cost's line out of a line of its own, by its class, and is no mistake in what is drawn, below.
- **In a real browser, five,** each in a stack built from the scratch copy, against the end-to-end test: every model listed at the default's price, the stack's `namer` priced like its other models again, the cost as a part of the row's name, and the title and the lines under it side by side, each of which failed it. The fifth was the one that the component tests couldn't see: without its class, the cost was still drawn under the title and in line with it, the title before it being a block of its own, and the test passed.

### 2026-11-27: the list says what a question to a model would meet now, and the menu's rows say it

The decision left it under "Known, and left": "what a model costs, and that one keeps failing, was told to wait or has no budget, isn't said in the menu: the next steps". The revision before this one was the sentence's first half; this is its second. [ADR 0031](0031-a-model-that-keeps-failing-is-asked-last.md) left the same under its own: "`GET /api/models` and the page say nothing of a failing model".

Three things the server knows of a model change what a question to it meets, and nothing said them before the question was asked:

- **it keeps failing:** its circuit breaker is open (ADR 0031), and it is asked after the models of its chain that aren't failing;
- **its provider said to wait,** in a 429 that named a wait longer than 2 s: until then it counts as having no budget left ([ADR 0030](0030-the-providers-budgets-are-counted.md), revision "a provider's own wait is kept, and until it is over its model has no budget");
- **its counted budget is spent** (ADR 0030): it is asked after the models with budget, or the question is refused for now.

A reader chose such a model in the menu and learned it from the question's refusal, or from the words under an answer another model wrote. And a model whose own server isn't there, with a stand-in that is ready, was listed `ready`, and its row said nothing of who would answer.

**What the list says:**

1. **Each model's own state,** its breaker, its provider's wait and its budget, from which the page works out who answers.
    - Con: the page would build the server's rule again from `then` and each model's state, and `status` is the chain's, so it couldn't tell whether a stand-in is ready itself.
    - Con: a model's own health is for whoever runs the installation: the admin page and `/metrics`, a later step's.
2. **A value of `status`,** such as `failing` or `limited`.
    - Con: the page and the warm-up read `status` as "can answer", and a failing model, or one without budget, is still asked.
    - Con: a status is what a model's server says of it, which nothing a breaker says changes (ADR 0031, revision "a status that is known isn't waited for").
3. **The rule's own answer for the shortest question.** Chosen.
    - Pro: the server decides, by the rule a turn is decided by, and the page says it.
    - Pro: it also says which model answers when the model itself isn't ready and a stand-in is, which the list never said.
    - Con: it holds for the shortest question alone (below).
4. **The breakers and the providers' waits alone,** without the counted budgets, which need Valkey.
    - Con: the list would say nothing of a count that is spent, which a free plan's soon is: the Gemini API gave a free account 20 requests a day of `gemini-3.6-flash` on 2026-11-25. And the look costs 0.16 ms (Measured, below).

**Which breaker's word:**

1. **Failing until its model answers,** in the place of what the breaker says at the moment.
    - Con: those words would stay for as long as nobody tries the model, and they are what keeps a reader from trying it.
2. **What the breaker says now** (`Breaker.failing`): failing while its wait runs, and while one request tries the model after it, and nothing once the wait is over and nobody tries it. Chosen: by the order's own rule, the words never outlast what was seen by more than the breaker's wait, five minutes at the most, and the list never says anything else than a question would meet.

**The decision: `GET /api/models` says, for each model, what the shortest question to it would meet right now, by the reads a turn's start makes and the one rule a turn is decided by, with one look at the budgets for the whole list; and a row of the menu says it in one line, before the cost, where the reason a model can't answer was. Nothing else on the page follows it.**

**The list** (`actio.api.catalog`):

- **Three members beside `status`,** whose value and meaning don't change:
    - `asked_first`, a model's `id` or `null`: the model the question would be given to first. Itself, or one of `then`: another while it can't answer itself, has nothing left of what its provider lets this installation send it, or keeps failing while the other doesn't. `null` when nobody would be asked: none of them can answer, which `status` says, or the question would be refused for now;
    - `failing`, whether the model `asked_first` names keeps failing, by its breaker now. It is asked all the same: none that would do better is there to ask before it. False when `asked_first` is `null`;
    - `retry_after`, whole seconds or `null`: for how long the shortest question to it would be refused for now, the very number a turn's 429 names for a request that the same limit refuses (`Asked.refused.seconds`). `null` when it would be asked, and when none of its chain is ready.
- **The shortest question is one token** (`SHORTEST_QUESTION`), by Actio's guess at a prompt: the smallest a request can be. A longer one may meet something else: it may wait longer, be given to another model, or be refused where the shortest isn't; and one longer than every ready model's room is asked all the same, where the shortest may be refused, since a guess never refuses a request ([ADR 0029](0029-a-model-is-passed-over-for-a-request-too-large.md)). Every model has room for the shortest: a models file's smallest window is 256 tokens, and its smallest `tokens_a_minute` 1.
- **The reads and the rule are a turn's own,** in a turn's order: the look at the budgets for one request of that one token, `Chain.with_own_waits`, `Chain.failing`, then `Chain.asked_with`. The rule is called, never written a second time, on the server or on the page.
- **One look for the whole list:** `look_at_chains` in `actio.catalog` asks Valkey once for every chain, each ready model with a budget once, whichever chains it is in, and nothing when no ready model has a budget, as with the shipped models file. It shares the look's body with `Chain.look`, which does what it did.
- **A read of the list writes nothing and counts as no try:** every bucket, breaker and provider's wait is as it was after it. It says nothing in the log.
- **With Valkey away,** the list answers as if no count were spent, and says a provider's wait and the breakers all the same. Nothing goes to the log: the page reads the list every 5 seconds while its model can't answer, and a turn says that the budgets couldn't be read where it matters.
- **`thinking`'s description is reworded:** it said "the model that would be asked first", which beside `asked_first` could be read as that model. Its rule is as it was: the first of the chain that can answer, whichever is then asked first.
- **Unchanged:** `status`, `thinking`, `then` and `price`; a turn's start, the title job, `Chain`'s four methods, the breaker, a provider's wait, what a request takes of a budget and how a try is recorded; the 429 and the warm-up.

**What a row says** (`meetsInShort` in `lib/chat/outcomes.ts`, which takes a model and the models on offer, in the place of `notReadyInShort`): the first of these that applies, or nothing.

| When | The row says |
|---|---|
| `status` is `missing` | "Isn't downloaded yet", as before |
| `status` is `unreachable` | "Can't be reached", as before |
| `retry_after` is under 60 | "At its provider's limit for under a minute" |
| `retry_after` is 60 or more | "At its provider's limit for about " and the wait as `timeLeft` words it: "about 1 minute", "about 12 minutes", "about 4 hours 12 minutes", "about 24 hours" |
| `asked_first` is the model itself, and `failing` | "Keeps failing" |
| `asked_first` is another model's `id` | that model's title and " answers for now", "Local answers for now", or its `id` where the list has no model of it |
| the same, and `failing` | "Local answers for now, and keeps failing" |

- **The wait's words are `lib/time.ts`'s** (`timeLeftRoughly`, beside `timeLeft`): "under a minute" where `timeLeft` would say seconds, "about" and its words from a minute on. A wait is a day at the most, but the words are given for whatever it is.
- **It is not counted down:** no clock is read for it, and it has no `role="timer"`. The row says the wait as the list said it, and the list is read again each time the menu opens. "Under a minute" and "about" are there because the number is the shortest question's, and as old as the read.
- **Who answers, and not why** another model does: what a reader choosing a model acts on is who answers. The cause is in the server's log.
- **A row never says of its own model what is so of another.** `failing` is of the model given the question first: where that is another, as when the model itself can't answer and the one that stands in for it keeps failing, the row names it.

**Where it is said:**

- **In the row alone, in the reason's line:** the same element, under the title and before the cost, the same suffix to its id, and the row's description in the order line, cost. A row is still named by its title alone. `ModelPicker` works the line out for each row from the list as it was read last, as it does the cost, and hands it to `Choice`.
- **Nothing else follows the three members:** the picker's button says the title, as before; no notice says them, and Send is never blocked for them, since the wait is one request's and a refusal counts its own down already ([ADR 0021](0021-usage-limits-in-the-page.md), revision "a refusal for now is counted down"); the reads of the list every 5 seconds go by `status` alone, as before; the load ahead, the switch for reasoning, the line under the box and the words under an answer don't read them. `Chat` is given nothing new. With one model on offer there is no picker, and nothing shows.
- **The load ahead goes by the model's `id` and `status`,** and no longer by the list's object for it (`Current` in `components/chat/chat-app.tsx`; `warmUp` takes the two). A read of the list brings a new object whenever anything in the list changed, and a wait counted in it changes at every read: by the object, each such read asked for the load again once its minute was over, with nothing new of the model that answers.

**The end-to-end stack** offers what it offered (`deploy/e2e/config/`, of which a comment changed): its `away` is answered for by `standard`, so its row now says "Fake model answers for now" before its cost, and its `rationed`, of one request a minute, is now asked by a test of the picker too, beside `e2e/no-budget.spec.ts`, each of which holds whatever the other has taken.

**Tests:**

- **the list** (`apps/api/tests/test_catalog_api.py`, through the route, with a `lineup` where a state is needed): a model alone with nothing in its way, in the test of the whole list, every member as before and the three new ones; a model that can't answer and a chain of stand-ins, the first ready one given the question, and none of a chain ready, nobody given it and nothing refused; a budget spent, filled under the app's keys at the test's clock, the stand-in with budget given it first, and alone refused for a minute, with nothing in the log; the same wait as the 429 of a question asked in the same moment, 43 seconds after 17.25 of a minute's one request; a bucket of tokens with one left, which the shortest question fits and a real question doesn't, refused; a provider's own wait, for models without a budget, the stand-in first, alone refused, and nothing once the clock is past it; breakers opened, the stand-in that isn't failing given it first, alone or with every model of the chain failing the first of the chain and `failing`, and nothing once the wait is over; while one request tries a model, `failing`, and once it answered, not; three reads of the list after a breaker's wait is over, which leave nobody trying the model, the next question asked of it in its place, and a budget's buckets as they were; one look for five chains, at each ready model with a budget once, and none where the one with a budget isn't ready; Valkey away, nothing said spent, a provider's wait and a breaker said, and no line in the log at warning or above; two names of one model on one provider, the same of its breaker and its budget; and the three members in the API's description, each described. The cases hold `status`, `thinking`, `then` and the price as they were, and one holds that whether a question can reason stays the first ready model's to say, whichever is given the question first. Where the log is held, it is held at every level, for what Actio's own code says;
- **the look** (`apps/api/tests/test_catalog.py`): `look_at_chains` over three chains, one question to Valkey at the buckets of the ready models with a budget, each once, and of each chain what its own look says; nothing asked when no ready model has a budget, and Valkey away raised when one has;
- **the words** (`lib/chat/outcomes.test.ts`, `lib/time.test.ts`): each row of the table, the order of the four, 59 and 60 seconds, an hour and a day, a title looked up and an id where the list has none, a model given its own question saying nothing, another model that keeps failing named by its title, and nothing made up of a model that nobody is given the question of; and the wait's words at 1, 59, 60 and 61 seconds, a millisecond past 59 seconds, 59 minutes and a second, an hour, 90 minutes and a day;
- **on the page** (`components/chat/model-picker.test.tsx`): the three new states, each a row's description and its lines, before the cost where costs are said and alone where they aren't, each row named by its title alone; a row that changes when the menu is opened again and the list says otherwise, with no timer in it; and, with the model that answers at its provider's limit and with it failing, Send open, no notice, the button named as before, and the list not read again 6 seconds on. The fake API's models say what the API would of a model with nothing in its way;
- **the load ahead** (`components/chat/warm-up.test.tsx`): a list read anew as the menu opens, more than a minute after the load was asked for, that says another wait of another model, and no second request to load the model that answers; then something written, which asks for it as ever;
- **end to end** (`e2e/picker.spec.ts`): "Away"'s row says "Fake model answers for now" before its cost, in its description and as its two lines are drawn, the other three rows held as before; and "Rationed" chosen in the picker and asked until the list read as the menu opens says that it is at its provider's limit, which its row then says, for "under a minute" or "about 1 minute", on a fresh stack and on one kept running, whatever `e2e/no-budget.spec.ts` left or does beside it. No end-to-end test opens a breaker: that takes failed questions to a model whose breaker the other tests share or read the rows of, and it was looked at in the three engines instead (Measured, below).

**Known, and left** (worked out from the rule, and not seen, unless it says measured):

- **the answer is the shortest question's:** a longer one may wait longer or go to another model, and a bucket of tokens with one token left says nothing;
- **a row is as old as the last read of the list:** a menu left open says what was so when it opened;
- **the breakers and the providers' waits are the API process's own:** a separate worker's, and another API process's, are apart, and a restart forgets them;
- **a model whose breaker opened says "Keeps failing" for 30 seconds to 5 minutes,** then nothing until a question tries it;
- **with one model on offer there is no menu,** and nothing on the page says any of it before a question is asked;
- **a row says who answers, not why another does;**
- **with Valkey away, a read of the list takes 2 seconds longer** where a ready model has a budget (measured);
- **a list in which anything changed is a new one to the page, and a wait counted in it changes at every read:** the conversation and each answer's line of figures are drawn again then, where a read that changed nothing draws nothing. Measured: about 2 ms more for 400 messages in WebKit, and nothing that showed in Chromium;
- **a model's own state, for whoever runs the installation,** is a later step's.

**How to reverse it:** `asked_first`, `failing` and `retry_after` in `ModelOut`, with their part of `list_models` and `SHORTEST_QUESTION` (`actio.api.catalog`), `look_at_chains` (`actio.catalog`), and their tests, then `make openapi`; `meetsInShort` back to `notReadyInShort` of the status alone, `timeLeftRoughly`, `why` handed to `Choice`, and their tests, and the three members of the fake API's models. `Chain.look` can keep the body it shares, and the load ahead its `id` and `status`.

#### Measured

2026-11-27, MacBook Pro M1 Pro.

**Before the build:**

- **What one look costs.** 1,000 looks from the API's container of the development stack on this machine to its Valkey (9.1.3), under a prefix of their own: a look at eight buckets, four limits of two models, took 0.156 ms at the median, 0.217 ms at the 95th percentile and 0.892 ms at the longest; a look at one bucket 0.110 ms, 0.125 ms and 0.184 ms. Nothing was written: no key was left.

**On the tree as committed,** in Playwright 1.65's Chromium, Firefox and WebKit, by scripts kept outside the repository, against the stack of `make e2e`, kept running. Each was the same in the three engines unless it says otherwise:

- **The rows as the stack starts,** in a window 1100 by 800 px: "Away" said "Fake model answers for now" and then "1 credit a token", in a row 64 px high, a title's line of 20 px and two of 16 px. The other six said their cost alone, in rows of 48 px. No row held a timer, and the menu was inside the window.
- **"Rationed", one request a minute,** chosen and asked once: the list said `retry_after` 60, and its row "At its provider's limit for about 1 minute", then its cost. The button was still named "Model: Rationed", Send wasn't blocked once something was written, no status line of the page spoke of a limit, and in 11 seconds with the menu shut the list wasn't read once. A second question was then refused for now: its notice counted "in 49 seconds" while the list said 49. In Firefox's first run the first question was itself refused, the bucket having been spent by the run before it, with 6 seconds left: the row said "At its provider's limit for under a minute".
- **"Hurried", a model alone,** asked questions that the fake fails with a 503: after the fourth the list said `failing`, an answer from before being the fifth call its breaker had kept, and the row said "Keeps failing", then its cost. Send wasn't blocked. The list said so for about 30 seconds, read every 5, then nothing; a question after that was answered, and the list said nothing of the model.
- **The longest words, at 320 px wide,** light, dark, and dark with forced colours, the list changed on its way to the page: "At its provider's limit for about 4 hours 12 minutes", a title of 80 characters with "answers for now" after it, and "Keeps failing", each above a cost. The menu stood from 29 to 312 px, the page no wider than the window, no line past its row; the three rows were 80, 96 and 64 px high.
- **Valkey held still** (`docker pause` of the stack's Valkey: away, with what it held): three reads of the list each answered 200 after 2,019 to 2,023 ms, and said nothing of Rationed's spent budget, where they had taken 5 to 20 ms and said 28 seconds. Back, 7 to 10 ms, and 22 seconds. The API's log of that minute had no line of it.
- **What a list that changed costs a long conversation.** A conversation of 200 questions, 400 messages and 200 lines of figures drawn, and 24 reads of the list measured, each by opening the menu: in turns a list that said another wait of one model each time, and one that said the same. The longest time the page's own thread was held, from the list's arrival on, over 600 ms: in WebKit 3.0 ms at the median for the list that changed (2.0 to 10.0 ms) and 1.0 ms for the one that didn't (1.0 to 2.0 ms), with 3.0 ms (1.0 to 12.0 ms) in the same 600 ms with nothing going on; in Chromium 5.3 ms for each of the three. The row said "about 15 minutes", then "about 14 minutes", as the wait went from 893 to 816 seconds.
- **The picker's and the budget's end-to-end tests, five times over at once,** on the stack kept running, each asking the same model of one request a minute: 40 of 40 passed, in 12 seconds.

**With real models,** on the stack of `make up` built from that tree, with a scratch pair of files in the place of `config/`: Ollama's `qwen3.5:4b-q4_K_M`; its `granite4:micro` with a budget of one request a minute; the repository's fake model, run on this machine, once alone and once with the first model standing in for it, one model on one provider under two names; and a model at an address nothing listens at, which the first stands in for. In Chromium:

- **As the stack started,** the row of the model whose server isn't there said "Qwen 3.5 4B answers for now", and no other row anything but its cost.
- **One question to the model with the budget,** which it answered: the list said 55 seconds, and its row "At its provider's limit for under a minute". A second question was refused for now, its notice counting "in 54 seconds" while the list said 54.
- **The fake told to answer 429 with a wait of 90 seconds:** alone, the list said `retry_after` 90, and its row "At its provider's limit for about 2 minutes"; under its other name, with a stand-in, "Qwen 3.5 4B answers for now". A question to the first was refused for now, "in 2 minutes"; one to the second was answered by the local model, with "answered by Qwen 3.5 4B in place of Fake, with a stand-in" under the answer. Once the wait was over, both rows said their cost alone.
- **The fake failed four more questions,** the 429 having been the first failed call its breaker kept: the list said `failing` of it alone, and its row "Keeps failing"; under its other name, "Qwen 3.5 4B answers for now". Half a minute on, both rows said their cost alone, and the next question to it was answered.
- **The server's log** of those minutes had seven lines about the models, each a turn's or a model's own, as before the step, and none from a read of the list.

**The review,** by a reviewer that had not seen the build: nothing that had to be fixed before the step could go on, and eleven smaller points, of which these changed something:

- **A row said "Keeps failing" of a model that wasn't.** Where a model can't answer itself, or has no budget left, and the one given the question first in its place keeps failing, `failing` is of that one, and the row said it under the first model's title. Now the row names the other: "Local answers for now, and keeps failing".
- **The load ahead followed the list's object.** A read that changes nothing leaves the page its old list, but a wait counted in the list changes at every read: each such read gave the page a new object for the model that answers, and the effect that asks for its load ran again, sending a request once its minute was over, with nothing new of that model. It goes by the model's `id` and `status` now, and a test holds it. That the conversation is drawn again for such a list was measured, above, and left.
- **Words that said more than the code does:** that what the list says "holds for every question at the least", which a question longer than every model's room contradicts, since it is asked where the shortest may be refused; that a breaker "opens on five failed calls at the least", in a test's comment and here, where three of five open it; the route's own description, which named none of the three members; `failing`'s description, which gave one of the breaker's reasons as the only one; and the reason given here for having no end-to-end test of a breaker.
- **The tests held the log at warnings and above.** They hold it at every level now, for what Actio's own code says.

Not taken: a test of the fake API's own models, which are test data; and the 12 seconds that the two waits of real time add to one file of the component tests, which run beside the others.

**Mistakes were planted,** one at a time, in a scratch copy of the tree, never the working tree:

- **On the first build, 49,** beside the review, against the tests of the list, the look, the words, the time and the menu: in the route (a longer question than the shortest, the breakers or the providers' waits left out, `failing` as the model's own or as any model's of the chain, the wait rounded down, the first ready model in the place of the first asked, Valkey away as an error, a look for each chain, a read that counts as a try), in the look for many chains, in the words and their order, in the wait's words, in the row (whose line, its place before the cost, the description, the row's name), and in what else might follow (reads every 5 seconds, a notice and a blocked Send, the button's name). 46 failed a test. Three failed nothing, and each has a test now:
    - whether a question can reason taken from the model given the question first, where it is the first ready one's to say: every model of the tests could reason alike;
    - a model that nobody is given the question of, with nothing said why, saying that "null answers for now": the list says no such thing of a model, and the row is held to saying nothing then;
    - a wait rounded down to the second in the rough words, where `timeLeft` rounds up: no test asked a millisecond past 59 seconds.
- **On the tree as committed, 54,** those three and five for what the review brought: a line in the log at each read, another model's failing said of the row's own model or not said at all, a failing model given its own question saying nothing, the load ahead asked for by the list's object, and asked for once whatever its model's status becomes. All 54 failed a test.

- **In a real browser, five,** each in a stack built from the scratch copy, against the picker's end-to-end tests: the model itself always listed as asked first, a spent count not said, the list's look made at keys where nothing is counted, and the other model named by its `id` each failed one of the two tests. The fifth, the cost drawn above what a question would meet, failed nothing: the test held the row's description, whose order is that of the ids it names, and not where the lines are drawn. It holds that now, for the two lines of "Away", and the mistake fails it.

### 2026-11-27: a conversation's read names the models on offer, and an answer's line names its model from the first draw

The revision "an answer says which model wrote it, where there is a choice" left it under "Known, and left": "the item comes when the models are read, which after a page load can be a moment after a stored conversation's answers are drawn; in a window too narrow for the line's one row, each line then takes a row more". [ADR 0028](0028-a-model-names-those-that-stand-in-for-it.md) left the same under its revision "a turn says which model answered, and the page says so", for the item of a stand-in. Both entries are answered here.

The line under an answer took the title of the model that wrote it from the list of models (`GET /api/models`), which the page reads once when it loads. That list also says what each model's server says now, and waits for the servers when what it knows is older than 15 seconds: with cloud models, for the providers' own lists ([ADR 0028](0028-a-model-names-those-that-stand-in-for-it.md), revision "a status that is known isn't waited for"). So after a page load the item could come after a stored conversation's answers were drawn, and in a narrow window each line then took a row more. The performance run after the picker found what it costs, and it was measured again before this revision (Measured, below): with the example's cloud models, at each load more than 15 seconds after the last ask of the servers, the list was answered 274 to 2,398 ms after the conversation was first drawn, every line went from 16 to 36 px high, and Chromium counted a layout shift of 0.0885 with six answers, where the page holds itself to 0.05 (`docs/performance.md`).

What the line needs is each model's title and how many models are on offer: what the models file says. It needs nothing a model's server says.

**How the line gets the titles before the list of models is read:**

1. **The list answers by what is known, and asks the servers behind the request,** as a turn's start does (ADR 0028, revision "a status that is known isn't waited for").
    - Con: it helps only while a status is younger than five minutes, and a page opened after a break, the usual load, would still wait. What to answer when nothing is known would need a new kind of status.
    - Con: that revision had the list wait on purpose: the page reads it once, and its notice that a model can't be reached, and its reads every 5 seconds, rest on what the servers said within 15 seconds.
2. **A stored conversation waits for the list before its first draw,** with a limit, as it waits for the renderer.
    - Con: it holds the conversation's words back by what the list takes, 0.3 to 2.4 s as measured, for one item of a line under them. With a short limit, most such loads would still move.
3. **Left as it is,** with its figure in this record.
    - Con: the page holds itself to a layout shift under 0.05 when a stored conversation opens.
4. **A second, quick route for the names alone.**
    - Con: one more request at every load, and it races the conversation's draw (the quick list of today is ahead by 56 to 82 ms), unless the draw waits for it, which is the second option again, in small.
5. **The names remembered in the browser from the last read.**
    - Con: a copy that can be out of date, none at a browser's first load, and one more thing kept in storage and believed from it.
6. **The conversation's read says the models on offer, by id and title.** Chosen.
    - Pro: the read the draw waits for anyway brings what the line needs: no race, no wait, no new request, nothing kept in the browser, and the list is as it was.
    - Con: a slice of what `GET /api/models` says is said a second time, in every read of a conversation: a few dozen bytes a model.

**The decision: a conversation's read says the models on offer, each by its id and title, from the models file alone, and the page names the model that wrote an answer by them until the list of models is read.**

**The API** (`actio.api.conversations`):

- **`ConversationOut` has a member `models`:** for each model on offer, its `id` and its `title` (`ModelTitle`), the two that `GET /api/models` says of it, in the same order, the models file's. A model whose server is away, or that nothing was asked about yet, is on offer and named like any other.
- **Both routes that answer a conversation say it,** `GET /api/conversations/{id}` and `PUT /api/conversations/{id}/branch`, through `_with_messages`, which is given the catalog (`Models`). It goes through the catalog's models and nothing else: no model's server is asked for it, and nothing is waited for. The page doesn't show another branch yet; the second route says it because it answers the same shape.
- **Its description** says what the member is for: that the conversation can be shown from this answer alone, with the model that wrote each answer, and that whether a model can answer right now is the list of models' to say.
- **Unchanged:** the routes' paths, errors and other members, `hold_still` and the order of the reads; `GET /api/models`; the catalog; the turn's record and its stream; the tables.

**The page** (`apps/web`):

- **`ModelTitle`** (`lib/api/client.ts`) is the item's type, generated. `figuresOf` and `writtenBy` (`lib/chat/figures.ts`) and the context `ModelsOnOffer` (`components/chat/message.tsx`) take it: an id and a title, and nothing else. A list of `Model`s fits it. The rule and its words are as they were.
- **`Chat` gives the context the list of models once it is read, and until then the stored conversation's own** (`stored.models`, kept once when the conversation comes onto the page): the list is the newer read, so it takes the place of the other as soon as it is there. `null` only when there is neither, as in a new chat before the list is read. The value is one of two objects that are there already, never one made at a render, so `Took` is drawn again only when it changes from the one to the other: once, when the list comes, and with the same words, so nothing on the page changes. `Message` has no new property, and no answer's words are drawn again.
- **Only the names:** everything else that follows the models has the list alone, as before: the line under the box (`contextFill`: a window is the list's to say), the picker, the switch for reasoning, the notices, the reads every 5 seconds and the load ahead. The conversation's names say nothing of a window or a status, and their type keeps them from being taken for more.

**Tests:**

- **the API's** (`apps/api/tests/test_conversations_api.py`): the read of a conversation names the models of a lineup of three, one of whose servers can't be reached and one that hasn't got its model, each by id and title, in the models file's order, which isn't their names' order, and as `GET /api/models` lists them; with nothing known of any model, the read asks no server, and while the list of models waits for a server's answer, which the test holds, a conversation is read all the same, the reads within a time limit; `PUT …/branch` says them; an empty conversation says them; the test of a whole conversation has the member; and the member and its item are described in the API's description. The recorded read of a conversation (`fixtures/turns/conversation.json`, which `test_turn_fixtures.py` compares whole, and which the web app's tests read) was recorded again, with the member;
- **the page's** (`components/chat/answered-by.test.tsx`, the whole app opened with its modules loaded anew, the list of models held where a test needs it late): a stored answer a model stood in for, the list not answered yet, with "answered by … in place of …" in the very change of the page that put its words there, and the same, with the same items and its words drawn once, when the list comes, and when it is read again 5 seconds on; two stored answers by the models asked, two models named by the read, each line naming its model in the change that put the answers there, and the same, with neither answer's words drawn again, when the list comes; a read that names one model, the asked model's own answer named by nothing until the list comes with two; a read that titles a model otherwise than the list, said by the read until the list comes, and by the list after; a list that can't be read, the lines staying as the read named them; and a new chat, its answer named by nothing until the list is read. Where the list was read before the conversation, the list is what names, from the answers' first draw: a read that titles both models otherwise, and one that names one model alone where the list has two. And a line's words are made once for a stored answer, and not again at a key pressed in the box, before the list is read or after: the test counts the calls of `figuresOf`, which a names' object made anew at each render would have called at each key. The two tests that held that the item came with the list hold now that it is there first. The fake API's `conversation()` names the model of its own default list unless a test says other models (`titlesOf`), and the tests of this file that list several models open conversations that name the same, unless the test is about the two differing;
- **end to end** (`e2e/picker.spec.ts`): in a window 320 px wide, a short question to the default model, then a reload with the list of models held back: the line under the answer says "answered by Fake model", in a row of its own, while the page has asked for the list and the picker, which comes with it, isn't on the page; then the list is let through, the picker comes, and the line and the box stand where they stood.

`lib/chat/figures.test.ts` is as it was: its lists of `Model`s fit the type.

**Consequences:**

- **Easier:**
    - **a stored conversation's lines name their models in the render that first draws them,** on every load, whatever the list of models takes;
    - **no request is added, and nothing is kept in the browser.**
- **Harder:**
    - **the page has two sources of the models' titles,** for the line under an answer alone, the newer one winning;
    - **a slice of the list is said twice,** in every read of a conversation.

**Known, and left** (worked out from the rule, and not seen, unless it says measured):

- **a new chat's first answer that ends before the list of models is read names its model only when the list comes:** a chat that was never stored has no read of its own;
- **the names a conversation's read brought are as old as that read,** and say nothing once the list is there. A read of the same conversation again on the page (`resync`) leaves them as they were;
- **the picker's button and the switch for reasoning still come with the list;**
- **a slice of the list is said twice,** in every read of a conversation;
- **an installation that renames a model between the read and the list** has the line say the old title, then the new one: the item changes in place, and its row can fold otherwise.

**How to reverse it:** `models` in `ConversationOut`, `ModelTitle`, and the catalog given to `get_conversation`, `show_branch` and `_with_messages` (`actio.api.conversations`), with their tests, then `make openapi` and the recorded read of a conversation recorded again (`ACTIO_RECORD_TURNS=1 uv run pytest apps/api/tests/test_turn_fixtures.py`); `ModelTitle` in `lib/api/client.ts`, and `Model` back in `figuresOf`, `writtenBy` and `ModelsOnOffer`; `storedTitles` in `Chat` (`components/chat/chat.tsx`), the context given `models` again; `titlesOf` and `conversation()`'s fourth parameter in the fake API; the tests of `answered-by.test.tsx` back to the item coming with the list; and the end-to-end test.

#### Measured

**Before,** on 2026-11-27, on a MacBook Pro (M1 Pro, 16 GB), on a stack built from the tree with the example's cloud models (`config/examples/free-cloud/`), in Chromium, 390 px wide, a stored conversation of six answers:

- **At each of 8 loads that came more than 15 s after the last ask of the models' servers,** the list of models was answered 274 to 2,398 ms after the conversation's first draw, the list itself taking 358 to 2,484 ms. Every line went from 16 to 36 px high, and Chromium counted a layout shift of 0.0885 with six answers. The performance run after the picker, on a stack of its own with the list held back by hand, counted 0.7053 with a hundred exchanges.
- **At each of 4 loads right after another,** the list was answered after 16 to 32 ms, 56 to 82 ms before the draw, and nothing moved: a shift of 0.0017.

**After,** the same day, on the same stack built from the tree as it was committed, the same conversation, the same script:

- **In Chromium, at each of 8 loads that came more than 15 s after the last ask,** the list was answered 243 to 498 ms after the conversation's first draw, the list itself taking 330 to 589 ms: the providers' own pace that hour. Every line named its model in the frame that first drew it, and was 36 px high from then on; nothing moved when the list came, and Chromium counted a layout shift of 0.0017 all in all, where it had counted 0.0902. At each of 4 loads right after another, as before: the list 56 to 63 ms ahead of the draw, 0.0017.
- **In Firefox and in Safari's engine,** 4 such loads each and 2 right after another: the same, the name in the frame of the first draw and every line 36 px from the first, with the list 151 to 315 ms after the draw in Firefox, and 251 to 882 ms after it at three of the four loads in Safari's engine. At the fourth, the engine's first page, the conversation was drawn after 1.7 s, when the list was there already.
- **With the list held back by hand,** in Chromium and in Safari's engine: every line said "answered by Qwen 3.5 4B" and was 36 px high while the page had asked for the list and had no picker, and the same once the list was let through.
- **Mistakes planted on purpose,** one at a time in a scratch copy of the tree, with the tests that should notice each: 14, eight in the route and six in the page, and for the end-to-end test two more on a stack built from the copy. On the first build 13 failed a test: a names' object made anew at every render failed none, and has the test that counts how often a line is worded since. On the tree as committed all 14 failed a test, and the end-to-end test failed at both of its two, the page naming by the list alone and the API naming no model.
- **The review found one thing the checks hadn't run:** the recorded read of a conversation, which a test compares whole, lacked the member. It was recorded again; nothing else of the recording changed.
