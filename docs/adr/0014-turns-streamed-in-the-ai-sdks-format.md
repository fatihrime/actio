# 0014. Turns: a question, an answer streamed in the AI SDK's format, and a record of how it went

- **Status:** accepted
- **Date:** 2026-11-11

## Context

A turn is one question put to a model, and its answer. The answer takes seconds to minutes, and people expect to read it while it is written, and to be able to stop it.

- **The web app will be built on the AI SDK's `useChat`** ([ADR 0003](0003-web-app-on-nextjs.md)). That library has its own format for a streamed answer, and it makes the ids of a chat and of its messages in the browser, before the server has heard of them. Checked against version 7 of the `ai` package.
- **Things go wrong half-way.** The model's server is down, or the answer breaks off, or the reader closes the page, or the API's own process dies.
- **Requests get repeated.** A browser that didn't hear back sends again.
- **Conversations are trees** ([ADR 0012](0012-conversations-as-a-tree-of-messages.md)): a question can be reworded, and answered more than once.

## Options considered

**The stream's format:**

1. **A format of Actio's own,** over server-sent events.
   - Pro: belongs to nobody else.
   - Con: the web app needs a reader for it, and loses everything `useChat` already does: growing a message piece by piece, keeping reasoning apart, reporting errors.
2. **The AI SDK's "UI message stream", version 1,** written by the Python API.
   - Pro: the library reads it as it is.
   - Con: the format belongs to a JavaScript library. Nothing ties a Python server to it but care.

**Who chooses the ids of a new conversation and a new question:**

1. **The server.** The browser asks for a conversation first, then sends the question, and replaces the ids it had made up with the server's.
2. **The browser.** One request, and the ids on both sides are the same from the start.

**A request that is sent twice:**

1. **An `Idempotency-Key` header,** remembered by the server for a while.
2. **The message's own id.** A message that is already there was already asked.

**Where a model's failure is reported:**

1. **As the response's status.** Then nothing can be answered until the model has said its first word, which after a cold start takes seconds.
2. **Inside the stream,** after a `200`.

**When the reader leaves:**

1. **Go on generating,** and keep the answer for when they come back. Needs turns that run apart from the request.
2. **Stop the model.**

**What a turn that failed leaves behind:**

1. **An empty answer,** marked as failed. Then every later reader has to step around it, and so does every later prompt.
2. **No answer.** The question stays, to be answered again.

## Decision

**One route, `POST /api/conversations/{id}/turns`, streams the answer as the AI SDK's UI message stream.** The browser chooses the ids. A model's failures come in the stream. Leaving stops the model. A turn that produced nothing writes no answer. Every turn is recorded.

**The request:**

- **`message`** adds a question and answers it. `parent_id` is the answer it follows; a first question has none. Rewording an earlier question is the same request with an earlier `parent_id`.
- **`answer_to`** answers a question that is already there once more.
- **The first question creates the conversation,** under the id in the path, and names it after its first line.
- **The question's id makes a repeat harmless.** The same message sent again answers 409, with the id and the state of the turn it already has. Nothing is asked twice.
- **The browser sends one message, never the history.** The server builds what the model is shown from what it stored: the branch that ends at the question, as text, after one line that gives the date. A client can't put words into a conversation that were never said.
  - Earlier reasoning is not sent back. It was never part of what was said.
  - Nothing is cut to make it fit. A conversation too long for the model fails the turn, and says so.

**Before a model is asked,** one transaction writes the conversation if it is new, the question, and the turn, marked `running`. A question never exists without the turn that is answering it. What is wrong at this point is an ordinary error: 404, 409, 422, 429, or 503 when the model isn't ready.

**The stream** (`actio.api.ui_stream`):

- **Chunks:** `start`, `start-step`, then for each part of the answer `reasoning-start`, `-delta`, `-end` or `text-start`, `-delta`, `-end`, then `finish-step`, `finish`, and `[DONE]`.
- **The message's metadata has the same shape as in `GET /api/conversations/{id}`,** so the web app handles one shape. `start` carries the turn as `running`; `finish` carries it as it was recorded, with token counts and timings.
- **A failure sends the turn's outcome, then an `error` chunk** with words for a person. The library stops reading at `error`, so the details go first.
- **`finish` is sent after the answer is stored.** Whoever reads that far can fetch the conversation and find it there.

**The record** (`turns`):

- **How it ended:** `completed`; `stopped`, because the reader left; `failed`, because the model did; `interrupted`, because the server writing it is gone.
- **A failed turn has a code:** `model_unavailable`, `model_missing`, `context_overflow`, `model_refused`, `answer_interrupted`, `model_error`, `internal_error`. The model server's own words stay in the log.
- **What arrived is kept.** An answer that broke off, or was stopped, is stored as far as it got. A turn that got nothing writes no answer: the branch ends at the question, which can be answered again or reworded.
- **What it cost:** tokens in, cached and out, and the time spent loading the model, reading the prompt and writing, where the provider reports them.

**When the reader leaves, the model is stopped,** and the turn is recorded as `stopped`.

- The server cancels the request at whatever it is waiting for. Writing the outcome is shielded from that cancellation. Found by test: without the shield, three tests leave the turn `running` for ever.
- Measured through the proxy with a local model, 2026-11-11: 0.03 s after the reader left, the turn was `stopped` with the 33 words that had arrived. A new question right after was answered in 0.26 s, so the model had stopped.

**One answer at a time per conversation.** A second turn answers 409. A unique index in the database enforces it, whatever code starts a turn.

**A turn that has been `running` for ten minutes is given up on,** as `interrupted`, the next time someone reads the conversation or asks in it. Nothing else would ever end a turn whose process was killed, and its conversation could never be answered again.

**Reasoning** is asked for only from a model whose server says it can ([ADR 0013](0013-the-models-on-offer-are-a-file.md)). A model that reasons by default is told not to, unless the request asks for it.

**Messages are limited per account,** 120 an hour by default ([ADR 0011](0011-rate-limits-as-token-buckets-in-valkey.md)).

**The format is tested from both sides:**

- The API's streams are recorded as files. A Python test fails when the API stops producing exactly them.
- The web app's tests read the same files with the real `ai` package (7.0.166), through the transport and the reader that `useChat` uses, and check the message the library makes of them.
- Tried on 2026-11-11: with a field of a chunk misnamed, the first test failed; after recording again, the second did, with the library's own "Type validation failed".

**Measured with `qwen3.5:4b-q4_K_M` through the proxy,** 2026-11-11:

| | First event | Whole answer | Notes |
|---|---|---|---|
| First question, model not loaded | 0.02 s | 5.1 s | 3.9 s of that loading the model; 36 pieces between 4.2 s and 5.1 s |
| Follow-up that needs the history | 0.01 s | 0.36 s | 46 of 105 prompt tokens came from the model server's cache |
| A question in Turkish, with reasoning | 0.02 s | 14.8 s | 463 pieces, the first after 0.19 s |

## Consequences

- **Easier:**
  - the web chat can use `useChat` with a thin layer that shapes the request;
  - asking, rewording and asking for another answer are one request;
  - a failure is a stored fact with a code, not a line in a log.
- **Harder:**
  - **the stream's format belongs to someone else's library.** Its version is pinned, and the recorded streams must be recorded again when the stream changes on purpose;
  - **ids chosen by a browser** are valid, and unused, and nothing more. Nothing may read meaning into them, such as the time inside a version 7 id;
  - **a stopped or failed turn has no token counts,** because the model never reported them. The usage limits will need an estimate.
- **Not done yet:**
  - **a turn lives inside its request.** Closing the page stops the answer, and a reader who reconnects can't pick the stream up again. Turns that run apart from the request, and can be rejoined, come in a later step;
  - **an answer is stored when it ends.** If the API's process dies half-way, what had been written is lost with it;
  - a conversation too long for the model fails, where it should be shortened or summarized;
  - no limit on how many conversations one account has answered at once, and none on the size of a request before it is read;
  - nothing is sent while a model loads, which can take a minute. A proxy that closes silent connections would cut in;
  - tools, and what an answer costs in credits.
- **How to reverse it:** following a model's answer (`actio.chat.answering`) produces steps that know nothing about the format. Another format is another small module beside `actio.api.ui_stream`, and the record of turns stays as it is.

## Revisions

### 2026-11-13: a conversation is read whole

`GET /api/conversations/{id}` could answer with half of two states: the branch from before a turn ended, and the turn's outcome from after. A stopped answer then showed as a question with "stopped" under it and no text, until the page was loaded again.

- **The cause.** Reading a conversation takes several questions to the database: its row, its branch, the turns behind it. A turn that was recorded between two of them was seen by the later ones only.
- **Why it surfaced only now.** It takes a reader that asks at the very moment a turn is recorded. The API's tests read after a turn, or while one was held still; none read while one ended. The web chat does exactly that: after Stop it asks at once what the server kept ([ADR 0015](0015-the-web-chat-calls-the-api-from-the-browser.md)). Driving the page in a browser showed it on the second try.
- **The fix.** A turn already locks its conversation's row before it writes. A reader now holds that row shared until it has put its answer together (`conversations.hold_still`): turns wait for it, and other readers don't. A test plays the interleaving: the turn ends after the reader has found the conversation and before it reads the messages.

### 2026-11-14: the date is no longer all a model is told

The decision above says the model is shown the conversation "after one line that gives the date". With that line as its only instructions, the default model (`qwen3.5:4b-q4_K_M`) took the date for its subject. Asked for some Python, it answered "Hello! Today is November 14, 2026", and wrote code that printed the date.

- **Why it surfaced only now.** The tests of turns run against a fake model, which echoes the question. They pin what is sent, and can't say what a real model makes of it. The first person to chat with the real model for a while noticed within minutes.
- **The fix.** The instructions now say who the model is first, and say when the date is for:

  > You are a helpful assistant.
  >
  > Today's date is Saturday, 14 November 2026. Use it when a question depends on the date. Otherwise, don't mention it.

- **The day of the week is given too.** Left to work it out, the model got it wrong, and once bent the date to fit: told "14 November 2026", it answered "Friday, November 13".
- **Measured,** against the real model, with seven questions that have nothing to do with the date ("Hello", "give me a basic code on python", "What can you do?" and four more), each asked several times:

  | Instructions | Answers that brought up the date |
  |---|---|
  | The date alone (before) | 6 of 35; with reasoning on, 3 of 14 |
  | Who the model is, then the date and when to use it, without the weekday | 0 of 35 |
  | The same with the weekday (now) | 2 of 21 |
  | No date at all | 1 of 35, and it can't say what day it is |

  On the three questions that tempt it most, the wordings with and without the weekday were close: 1 to 3 answers in 18. The counts are small, so they show the size of the change, not which wording is best.
- **What is left.**
  - **It still happens now and then.** A model of this size can't be held to an instruction every time. The lasting fix is to give the date through a tool the model calls when it needs it, which comes with tools.
  - **A conversation in which the date already came up keeps bringing it up,** because the model sees its own earlier answers. A new conversation starts clean.
  - **The day is the server's, in UTC.** Around midnight it can differ from the reader's.
  - **How good the instructions are is measured by hand.** It belongs in an evaluation that runs against real models, which this project doesn't have yet.
