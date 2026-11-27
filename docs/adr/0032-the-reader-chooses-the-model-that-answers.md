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
