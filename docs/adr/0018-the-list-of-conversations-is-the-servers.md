# 0018. The list of conversations is the server's: read again, never rearranged

- **Status:** accepted
- **Date:** 2026-11-14

## Context

The chat page shows one conversation, and reaches an earlier one only by its address ([ADR 0015](0015-the-web-chat-calls-the-api-from-the-browser.md)). This step puts the list of a person's conversations beside the chat: open one, rename it, delete it.

- **The API already has all of it** ([ADR 0012](0012-conversations-as-a-tree-of-messages.md)): the list, most recently active first, a page at a time by where the last page ended; a rename; a delete with no way back. A new conversation is named after its first question ([ADR 0014](0014-turns-streamed-in-the-ai-sdks-format.md)).
- **The list changes without anyone touching it.** A first question adds a conversation. Going on with an old one moves it to the top. Another window does the same.
- **The page has a rule for a conversation already:** the server is the truth, and the page reads it again instead of working out what happened ([ADR 0015](0015-the-web-chat-calls-the-api-from-the-browser.md)).
- **An answer lasts as long as someone reads it.** Closing the page stops it ([ADR 0014](0014-turns-streamed-in-the-ai-sdks-format.md)). With a list, leaving a conversation half-way through an answer becomes an everyday thing.
- **The components are written here** ([ADR 0015](0015-the-web-chat-calls-the-api-from-the-browser.md)). No menu, dialog or drawer comes ready-made.
- **A layout for phones is planned for a later phase.** Until then a list beside the chat must not make a narrow window useless.

## Options considered

**How the list stays in step with what the page does:**

1. **The page rearranges it:** puts a conversation on top when a question is sent, and shows a rename or a delete as done at once.
   - Pro: the list reacts before the server has answered.
   - Con: the page repeats the server's rule for the order, and has to take its guess back when a request fails.
   - Con: the list is held in pages. One row moved to the top moves the end of every page.
2. **The page reads the list again** whenever something happened that changes it.
   - Pro: one rule, the server's, and nothing to take back.
   - Con: one more request for each change, and the change shows a moment later.

**How a long list is read:**

1. **All of it at once.** It grows for as long as the installation is used.
2. **More when the reader reaches the end.** The page has to notice the end coming into view, which someone using a keyboard never causes by scrolling.
3. **A page at a time, by a button** at the end.

**Where renaming and deleting happen:**

1. **A menu on each row, and a dialog that asks before deleting.**
   - Pro: what most chat applications do.
   - Con: a menu and a dialog are the two parts hardest to get right for a keyboard and a screen reader, and both would have to be written here.
2. **In place.** The row turns into a box to type in, or into the question with its two answers.

**What becomes of an answer that is being written, when its reader opens another conversation:**

1. **It goes on, unseen.**
   - Con: nothing on the page could stop it any more.
   - Con: a local model writes one answer at a time. The next question would wait behind an answer nobody is reading.
2. **It stops,** as when the page is closed. What was written is kept.
3. **It goes on at the server, and can be joined again.** That is what the plan calls detached turns, a later step: it changes how a turn runs, not how a list looks.

**What a narrow window does with the list:**

1. **Shows it beside the chat anyway.** At a phone's width that leaves the chat about a hundred pixels.
2. **Shows none,** until there is a layout for phones.
3. **Lays it over the chat while it is wanted.**

## Decision

**The list on the page is what the server said last. The page reads it again after every turn, and changes what it holds only once the server has done what was asked. Renaming and deleting happen in place. Leaving a conversation stops its answer, for now.**

**Reading the list** (`lib/chat/list.ts`):

- **A page at a time:** 30 conversations, and a button for older ones.
- **Pages follow from each other.** Each is held under where the page before it ended. When the first page changes, the ones after it are read from its new end, so no conversation is skipped or shown twice.
- **Read again at three moments:**
  - when the server has taken a question, which the first piece of its answer says;
  - when a turn has ended, however it ended;
  - when the window is returned to. That brings in what another window did.
- **Held under the account's id,** so a list never outlasts its owner's session.
- **A failure is shown,** with a button to try again. The chat doesn't depend on the list: one that can't be read leaves everything else working.

**Changing it** (`components/sidebar/`):

- **A rename** is typed in place: Enter saves, Escape or leaving the box doesn't. The new title shows once the server has answered with it. If it couldn't be saved, the box keeps what was typed.
- **A delete asks first,** in place, and the keyboard lands on Cancel. There is no way back ([ADR 0012](0012-conversations-as-a-tree-of-messages.md)), so the question says so.
  - The row goes when the server has deleted the conversation. One that was already gone counts as deleted.
  - Deleting the conversation on the page starts a new one in its place. Its address leaves the browser's history: Back doesn't return to it.
- **Neither is shown before it is true.** Both take a few milliseconds here. Over a slow connection the row waits, visibly, and never shows something that then has to be taken back.

**Leaving a conversation stops the answer that is being written in it.** It ends like one stopped with the button: what had arrived is kept, and the question can be asked again.

- Chosen over letting it go on unseen, for the two reasons above.
- It is the least good part of this step, and temporary. With detached turns an answer goes on at the server, and the page joins it again.

**Where the list is:**

- **In a wide window, beside the chat.** A button puts it away and brings it back, and the browser remembers which.
- **In a narrow one** (under 48 rem), **over the chat** while it is wanted. It goes away when a conversation is chosen, with Escape, or with a click beside it.
- **Nowhere without a session,** like its button and New chat: there is nothing to list, and nobody to list it for.

**Every action is a link or a button,** and the keyboard is left somewhere sensible after each: on the box when renaming starts, back on the button afterwards, on the neighbouring row when a row was deleted. A click on a row with a modifier key is left to the browser, which opens the conversation in a new tab.

**Colours.** The components older than the colour names of [ADR 0016](0016-answers-shown-as-markdown.md) now use them too. No component names a colour with its dark twin any more.

## Measured

On a MacBook Pro (M1 Pro, 16 GB), in a headless Chromium against the stack of `make e2e`, 2026-11-14:

| | |
|---|---|
| Scripts a first visit downloads | 233 kB, 4 kB more than before the list |
| The first page of 200 conversations | 6.2 kB; 6 ms at the median of 20 requests |
| From a reload until the list shows | 72 ms |
| Each further page, from the click | about 60 ms |
| One turn, with all seven pages read | seven requests: every page's end had moved by one row |

- **Leaving a conversation frees the model.** Tried once with the real model (`qwen3.5:4b-q4_K_M`, through `make up`): an essay of 600 words was left after 2.5 seconds, and a question asked at once in another conversation had its whole answer 0.9 seconds later. The essay's first 495 characters were kept, with the notice that it was stopped.
- **Seven mistakes were planted,** to see the tests fail: the list not read again after a turn, a delete that leaves the deleted conversation on the page, one conversation in two pages shown twice, a missing conversation treated as a failed delete, a title box that stays when it is left, the defect below put back, and the list beside the chat in a narrow window. Each failed the tests meant for it, and no other.
- **Every test in a real browser six times over, two at a time: 114 of 114 passed.**

## Consequences

- **Easier:**
  - the order of the list is decided in one place, the API's query;
  - nothing on the page can show a change that didn't happen;
  - another window's changes arrive by the same path as the page's own.
- **Harder:**
  - **every change costs a request,** and a turn costs up to two readings. Someone who has read seven pages pays seven small requests for a turn, see above;
  - **switching away from an answer loses the rest of it.** The notice says so when the conversation is opened again, and Try again asks once more;
  - **the keyboard's path through the list is this project's to keep right.** The component tests check where the keyboard is after each action; a screen reader has not been tried yet.
- **Not done yet:**
  - no search, and no grouping by day;
  - a title is the first question's first line until someone renames it. Titles written by a model come with the worker process;
  - an answer doesn't go on without its reader. Detached turns;
  - on a narrow window the list works, but nothing else was designed for a phone: sizes for fingers, the keyboard covering the box. The layout for phones is a later phase;
  - a guest's list is gone with the guest, after a day ([ADR 0009](0009-sessions-in-the-database.md)). The pages for signing up, which keep it, are the next step.
- **How to reverse it:**
  - **showing a change at once** is a change inside `lib/chat/list.ts`: its `rename` and `remove` would update what is held before asking the server, and put it back on a failure. Nothing outside that file would notice;
  - **a menu and a dialog** would replace `components/sidebar/row.tsx` alone.

## Revisions

### 2026-11-14: the list is held under the session, not under the account

The decision said that the list is "held under the account's id, so a list never outlasts its owner's session". With logging in and out ([ADR 0019](0019-accounts-in-the-one-screen.md)) that was no longer true.

- **What was wrong.** An account logged out and, in the same page, back in. Its id is the same both times, so the page showed the list it had read during the first session. A conversation that a guest had brought along meanwhile was missing from it.
- **Why it surfaced only now.** When the list was built, a page had one session for its whole life: everyone was a guest, and the only change was "start a new session", which makes a new guest with a new id. The account's id stood for the session without being it. The first test in a real browser that logs out and in again failed on the difference.
- **The fix.** The list is held under the session it was read in: whose it is, and which of the page's sessions it is. What was held for a session is dropped when it ends.

Two smaller things:

- **A guest's list being gone after a day,** under "Not done yet": the list says so underneath, with a link to sign up, which keeps it.
- **"The defect below",** among the mistakes that were planted, points at nothing in this record. It is the one in [ADR 0015](0015-the-web-chat-calls-the-api-from-the-browser.md)'s revision of the same day: a conversation that began on the page, shown empty when it was returned to.
