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
