# 0012. Conversations as a tree of messages

- **Status:** accepted
- **Date:** 2026-11-11

## Context

A conversation is what a person and a model said to each other. Storing it has to allow for how people actually use a chat:

- **People write a question again,** in other words, and ask for **another answer** to the same question. The earlier version should not be destroyed: people compare answers, and go back.
- **An answer is more than text.** A model can show its reasoning before answering. Later come tool calls, their results and sources.
- **The web app will render messages with the AI SDK,** which has its own shape for a message: an id, a role, and a list of typed `parts`. Checked on 2026-11-11 against version 7.0.169 of the `ai` package.
- **Conversations are private.** Each belongs to one account. A guest has them too, for the day the guest lasts ([ADR 0009](0009-sessions-in-the-database.md)).

## Options considered

**How to keep a message that was written again:**

1. **A flat list of messages; an edit overwrites.**
   - Pro: the simplest table and the simplest queries.
   - Con: the old version, and everything said after it, is gone.
2. **A flat list, with a version number on each message.**
   - Pro: old versions survive.
   - Con: a version of a message has its own follow-ups, which have versions too. Numbers alone can't say which follow-up belongs to which version.
3. **A tree.** Each message points at the message it follows. Messages that follow the same one are versions of each other.
   - Pro: one rule covers every case, to any depth. Nothing is ever overwritten.
   - Con: a conversation is no longer "its messages in order". Reading means choosing a branch, and which branch is shown has to be stored.

**What a message's content is stored as:**

1. **One column of text.** Reasoning, tool calls and sources don't fit.
2. **The AI SDK's message, as it is.**
   - Pro: nothing to convert on the way to the browser.
   - Con: the format of the database would belong to a JavaScript library, which has changed its message format between major versions before.
3. **Actio's own list of typed parts,** with names that follow the AI SDK's.
   - Pro: converting is copying, today. If the library changes, the conversion changes, and the stored data doesn't.
   - Con: one more definition to keep.

**What deleting means:**

1. **Mark the row as deleted,** and remove it later. Allows undo. Every query must then remember to skip marked rows, and something must do the removing.
2. **Delete the row.** Simple, and it is what was asked for. No undo.

**How to list many conversations:**

1. **By position:** "skip 30, take 30". If a conversation moves to the top between two pages, one is shown twice or skipped.
2. **By where the last page ended:** "the 30 after this one".

## Decision

**A tree of messages, with content as Actio's own typed parts.** Deleting deletes. Lists are read by where the last page ended.

**The tree** (`actio.chat.tree`):

- **A message points at the one it follows,** `parent_id`. A first message has none.
- **The conversation points at the last message of the branch that is shown,** `current_leaf_id`. That branch is read by walking up from there, one parent at a time, in one recursive query.
- **Versions of a message are the messages with the same parent.** The API lists their ids with each message, oldest first, so an interface can offer "2 of 3".
- **Questions and answers take turns.** A person's message is first or follows an answer; an answer follows a person's message.
- **Adding a message shows its branch.** Adding after a message that already has a follower starts a new branch there, and nothing is overwritten.
- **Any message can be chosen to show its branch.** Below it the tree may fork again; at each fork the newest version is followed.
- **The database keeps a tree inside its conversation.** Both pointers to a message, from the message that follows it and from the conversation that shows it, name the conversation as well. So neither can reach into another conversation, whatever code writes the row.
- **The queries are tested against a plain tree.** Random sequences of questions, answers and branch changes go to the database and to a tree kept in Python. Both must show the same branch, with the same versions, after every step.

**The content** (`actio.chat.parts`):

- **A message is a list of parts,** stored as JSON: `text`, and `reasoning` for what a model thought before answering. Each kind is a class that checks it on the way in and on the way out.
- **A part the code doesn't know is an error,** not something to pass along unread.
- **The API answers in the shape the web app's library reads:** `id`, `role`, `parts`, and everything of Actio's own under `metadata` (`parent_id`, `created_at`, `versions`).

**Whose it is:**

- **Every lookup takes the account's id** and finds only that account's conversations. Someone else's conversation answers 404, exactly like one that doesn't exist.
- **A guest's conversations go where the guest goes:**
  - a guest who signs up keeps them: the account is the same row ([ADR 0010](0010-accounts-with-a-password.md));
  - a guest who logs in to an account they already had takes them along: they move to that account before the guest is deleted;
  - a guest who logs out, or whose day is over, is deleted, and the database deletes their conversations with them.
- **Deleting a conversation deletes its messages,** on every branch, at once.

**The list** is ordered by `last_active_at`, the time a message was last added. Renaming doesn't change it, so a renamed conversation stays in its place. A page is fetched with a cursor that says where the previous one ended.

**Where it sits.** `actio.chat` is a domain module beside `actio.identity`, and above it: a conversation's table points at the accounts' table.

- That pointer names the accounts' class, not the text `"users.id"`. Found by running the code on its own on 2026-11-11: a script that loaded only `actio.chat` could not write a conversation, because nothing had loaded the table it points at. The API loads every module, which had hidden this; a background job would not have.
- A test now loads each table module alone, in a new interpreter, and fails if it can't stand by itself.

## Consequences

- **Easier:**
  - writing a question again and asking for another answer are both "add a message after an earlier one". The next step needs no new storage for them;
  - nothing a person or a model said is lost by going on;
  - a new kind of part, such as a tool call, is a new class. No table changes.
- **Harder:**
  - nobody can ask for "the messages of a conversation" any more. Every reader names a branch: the model's context, a search result, an export;
  - a deleted conversation can't be brought back;
  - what is kept about usage must not point at messages in a way that would block deleting them. The ledger will name what it needs by value.
- **Not done yet:**
  - **nothing adds messages over HTTP.** Turns do that, in the next step: they also create the conversation;
  - titles are empty until someone renames a conversation. Automatic titles come with the worker process;
  - no search, and no limit yet on how long a message or a conversation can be. Turns set the limits on what comes in.
- **How to reverse it:** a tree holds every flat list. Going back to one means keeping each conversation's shown branch and dropping the others, which loses data. The stored parts are Actio's own format, so changing it is a migration that nothing outside has to follow.
