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
