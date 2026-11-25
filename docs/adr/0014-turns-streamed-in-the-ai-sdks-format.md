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

### 2026-11-14: a turn is paid for

"What an answer costs in credits" was left for later above, and the usage limits were said to need an estimate for turns that end without token counts. Both are decided in [ADR 0020](0020-usage-limits-on-turns.md). Nothing above is taken back; three things are added to it:

- **Before a model is asked,** the transaction that writes the question and the turn also holds the credits the turn may cost. A turn that doesn't fit is answered 429, like one that comes too fast, with the usage window that is full and when it starts over. Nothing of it is stored.
- **The stream has one chunk more,** `data-usage-warning`, sent just before `finish` by a turn that takes its account past 50, 75 or 90 percent of a limit. It is one of the library's data parts, marked `transient`: the library hands it to the page and keeps it out of the message. A stream with it is recorded, and read by the `ai` package in the web app's tests, like the others.
- **The record of a turn gained two things:** `credits`, what it cost, and `capped_by_usage`, whether a limit left its answer less room than the model allows.

A stopped turn still has no token counts of its own. What it is charged is guessed from what it wrote, and the ledger marks the guess as one.

### 2026-11-16: a turn no longer lives inside its request

The decision said "Leaving stops the model", and "Not done yet" began with what follows from it: closing the page stops the answer, and a reader who reconnects can't pick the stream up again. [ADR 0022](0022-turns-that-run-apart-from-their-request.md) takes that decision back. The stream's format, the request, the record of a turn and the rules for a failed one stay as they are above.

- **Why now.** It was always meant to be a step of its own. The decision above chose the simple arrangement first, to have a chat that works before a chat that survives a reload, and said so. With the list of conversations, leaving became something people do in the middle of an answer ([ADR 0018](0018-the-list-of-conversations-is-the-servers.md)).
- **What changed in what is written above:**
    - **"When the reader leaves, the model is stopped"** no longer holds. A turn runs in a task of its own, and a reader who leaves ends nothing. `stopped` now means that whoever asked stopped it, with a request for that.
    - **The stream is read from Valkey,** by the request that started the turn like by anyone who joins later. `GET …/turns/{turn_id}/stream` answers with it from its first event.
    - **"A turn that has been `running` for ten minutes is given up on"** is now a turn that hasn't said it is alive for one minute. A turn that is being written says so every 15 seconds, however long it takes.
    - **`interrupted`** was only what a turn was called that had been given up on. A turn that a shutdown ended is recorded so too, by the process itself, and keeps what it had written.
    - **"Nothing is sent while a model loads"** is done: a reader who hears nothing for 15 seconds is sent a line that is no event, and the library skips it.
- **What is still not done,** of the list above: an answer is still stored only when it ends, so what a killed process had written is lost; a long conversation still fails where it should be shortened; nothing limits how many answers one account has written at once, or the size of a request.
- **Two recordings were added** to the files that both sides test against: a stream that ends because its turn was stopped, and one with the line that keeps a silent connection open. The older recordings didn't change by a byte: the events are what they were, and only the way they travel is new.

### 2026-11-18: how many answers at once, and how large a request

Two items of "Not done yet" are done, in [ADR 0023](0023-answers-that-wait-their-turn.md): "no limit on how many conversations one account has answered at once, and none on the size of a request before it is read".

- **A plan may limit how many answers one account has being written at once,** in all its conversations. One more is refused before anything is stored, like a question that comes too fast: a 429, with `at_once`. "One answer at a time per conversation" holds as before, and is checked first.
- **A turn may also wait** before its model is asked, at a gate in front of a server that writes only so many answers at once. It is `running` meanwhile, and its stream has begun: `start` and `start-step`, then the line that keeps a silent connection open. The stream's format didn't change, and no recording did.
- **The proxy refuses a body over 2 MB** on the API's routes: 413, as a problem document. A body that says its length is refused before any of it is passed on, and one sent in chunks once the limit is reached. The largest question the API takes fits, however its characters are written in JSON.

### 2026-11-19: what a killed process had written is kept

"An answer is stored when it ends. If the API's process dies half-way, what had been written is lost with it", under "Not done yet", and again in the revision of 2026-11-16, is answered by [ADR 0022](0022-turns-that-run-apart-from-their-request.md), revision "what a dead process had written is kept".

- **A running turn saves what its answer holds every 15 seconds,** with the beat that says it is alive, on the turn's row.
- **A turn whose process died is given up on with that as its answer,** `interrupted`, under the id its stream had named, and dated at its last beat.
- **What it wrote after its last beat is still lost:** about 15 seconds, and more when its last beats didn't reach the database. The answer is otherwise still stored when it ends, as decided above.

### 2026-11-23: a turn says when its first word came

The decision above records what a turn cost: its tokens, "and the time spent loading the model, reading the prompt and writing, where the provider reports them". The page now shows under an answer how long its first word took ([ADR 0015](0015-the-web-chat-calls-the-api-from-the-browser.md), revision "an answer says what it took"), and nothing in the record said that. A turn now notes it itself.

- **What is recorded:** `turns.first_word_at` (migration 0013), the moment the first piece of the model's answer was taken, its reasoning included, before it is handed on to the readers. It is read from the clock `actio.chat.answering.answer` is given, as every moment of a turn is. With `started_at`, the request's own moment, it says how long the first word took.
- **When:** with the turn's outcome, where every ending is written (`_end` in `actio.chat.turns`), for every turn that got a piece of its answer: one that completed, one stopped by whoever asked, one whose model failed after some words, and one a shutdown interrupted.
- **What has none:**
    - **a turn whose model wrote nothing:** it failed before a word, or was stopped while it waited at the gate in front of its model's server;
    - **a turn that was given up on.** Its process died, and what it had noted died with it, as its token counts do. The moment is not saved with the beat: it would be the one thing the beat saves that the answer doesn't need;
    - **the turns there were before:** nothing had noted the moment.
- **Why Actio's clock, and not the provider's times.** The record already has the time a model's server says it took to load the model and read the prompt (`load_ms`, `prompt_ms`). Only Ollama reports them: an OpenAI-compatible server and the fake model report none. And they leave out what came before the model was asked: the wait at the gate ([ADR 0023](0023-answers-that-wait-their-turn.md)), which can be minutes, and what Actio does first. What a person waited for is the time from their question to the first word. Read from the turns of the maintainer's own stack on 2026-11-22 (Qwen 3.5 4B through Ollama): the last eleven answers had `load_ms` 1 to 21 and `prompt_ms` 140 to 244, and one answer before them, to a model that had to be loaded first, `load_ms` 5038. And where both are known they agree: by Actio's clock the time from the first word to the end was within 13 ms of Ollama's own `generation_ms` in four answers of two to eight seconds (ADR 0015, revision "an answer says what it took").
- **Where it is said:** `TurnOut.first_word_at`, empty while the turn runs: in the stream's `start` chunk (empty), its `finish` chunk and the conversation read. Every recorded stream's turn has the member now, and nothing else of them changed. One recording was added: a turn with every figure the page shows, from a model that says how long it took to write and a clock that moves between the question, the first word and the end (`figures.sse`). And one of a model that answered with nothing and said what it had read: its turn completes, is paid for, and has no first word (`no_words.sse`).

### 2026-11-23: a turn says how much of the window its conversation takes

The page now says, under the box where a question is written, how full the model's context window is in the conversation ([ADR 0015](0015-the-web-chat-calls-the-api-from-the-browser.md), revision "the page says how full the model's window is"). What a conversation takes of the window follows from what the server sends back to a model, and the decision above sends back the text of each answer and not its reasoning. A turn's record had the tokens its model read and wrote, and nothing that says what is left of them for the next question. It says that now.

- **What is said:** `TurnOut.context_tokens`, about how many tokens of the model's window the conversation takes after the turn: what the model was shown, and of what it wrote, what goes back to a model with the next question. Every place that says a turn says it, worked out the same way: the stream's `finish` chunk, and the `message-metadata` chunk before an error; `GET /api/conversations/{id}`, for each answer's turn and for `unanswered`; and the answer of `POST …/turns/{turn_id}/cancel`. Empty while the turn runs, and when the model's server counted nothing: a stopped turn, since a stream that is closed never sends the counts, and a failed one.
- **The rule,** `tokens_after` in `actio.chat.context`, from the turn's two counts and its answer's parts:
    - either count missing: none;
    - nothing of the answer goes back to a model, because there is no answer or no text in it that isn't blank, which is exactly when `for_model` sends it no more: `input_tokens`;
    - words, and no reasoning: `input_tokens + output_tokens`;
    - words and reasoning: `input_tokens`, and the words' share of `output_tokens` by characters, words over words and reasoning, rounded to a whole token. Never more than the two counts together.
- **The server says the figure, and the page never adds a turn's counts.** What a conversation takes of the window follows from how the server builds the prompt: what goes back to a model and what doesn't. The page doesn't know that rule, and must not copy it: a copy would go wrong without a word the day the rule changes, and a summary of old messages is planned for a later phase.
- **The figure is what the conversation holds after the turn, not the turn's peak.** `input_tokens + output_tokens` is what the window held when the answer ended, its thought included. Shown as the meter, it would fall from 17 % to 2 % after the next plain answer in the measurement below.
- **No column and no migration: the figure is worked out where a turn's record is built,** from what is stored, the two counts and the answer's parts, by one function beside `for_model`. So the turns there were before have it too. And `for_model` and `tokens_after` take what goes back to a model from one helper (`_goes_back`): when that changes, the figure changes with it, in the same module. The conversation read parses each message's parts once, for the message and for its turn; the cancel route reads the turn's answer for it.
- **Where a thought and words share `output_tokens`, the split is a guess, by characters.** No provider Actio talks to says how many of the tokens it wrote were reasoning: Ollama's `eval_count` is one number. So the member's description says "about".

**Measured before it was built,** on the maintainer's stack, with the real model (`qwen3.5:4b-q4_K_M` through Ollama 0.40.16, a window of 8,192), one conversation of five questions through Actio's API, on 2026-11-23:

| Turn | Thinking | `input_tokens` | `output_tokens` | Sum | Next prompt minus this sum |
|---|---|---|---|---|---|
| 1 | no | 64 | 8 | 72 | +16 (a question of 27 characters) |
| 2 | no | 88 | 8 | 96 | +19 (52 characters) |
| 3 | yes | 115 | 1,299 | 1,414 | −1,264 (46 characters) |
| 4 | no | 150 | 21 | 171 | +17 (35 characters) |
| 5 | no | 188 | 6 | 194 | |

Turn 3's answer was 61 characters of words after 5,284 characters of reasoning. So after an answer that didn't think, the turn's two counts together are what the next question finds in the window: the difference is the new question, and the chat template around it. After one that thought, they overstate it nearly tenfold, 1,414 against the 150 the next prompt held, because Actio never sends a thought back (`for_model`). The rule gives 115 + 15 = 130 for turn 3; the next prompt of 150, less a question of that size (16 to 19 tokens above), leaves 131 to 134.

Also checked in the code: `GET /api/models` already gives each model's `context_window`; a stopped turn's record has no counts, nor has a failed one; with Ollama an answer that fills the window ends with the `finish_reason` `length`, and a prompt that doesn't fit fails the turn with `context_overflow` ([ADR 0007](0007-local-models-through-ollamas-native-api.md)); nothing refuses a turn beforehand for its length.

**Options considered, and not built:**

1. **The page adds the two counts,** with no change to the API.
    - Con: overstates the window nearly tenfold after an answer that thought, as measured. And the page would need the server's rule for what goes back to tell the cases apart.
2. **A column,** the figure written with the turn's outcome.
    - Con: a migration, and the turns there were before would have none. And the figure would stay what it was written as when the rule for what goes back changes, as it will with a summary of old messages.
3. **Worked out where the record is built,** from what is stored. Chosen.

**What it leaves:**

- **The split by characters is a guess.** A provider that said how many of the tokens it wrote were reasoning would make it exact, and the rule would take that count where there is one.
- **A stopped answer isn't counted.** Its model's server never said what it read and wrote, so the figure the page shows stays the one from before it, a little under what the conversation takes.
- **A turn that ended after it was given up on said more in its stream than its record keeps,** its status and counts as well as this figure, until the same day, when [ADR 0022](0022-turns-that-run-apart-from-their-request.md), revision "a turn that ends after it was given up on says so", ended that.

**Measured on the built code,** the same day, with the same model on the same stack: four more conversations of the same shape, each with one answer that thought, the last three asked through the page in Chromium, Firefox and WebKit.

| the answer that thought: read | wrote | the two counts together | the figure | the next prompt held |
|---|---|---|---|---|
| 115 | 730 (73 characters of words after 3,066 of reasoning) | 845 | 132 | 152 |
| 116 | 506 | 622 | 134 | 153 |
| 115 | 1,146 | 1,261 | 130 | 150 |
| 115 | 2,048, the most an answer may have, all of it thought | 2,163 | 115 | 133 |

The next question was the same each time, of 46 characters. After the answer that never got to its words, where nothing goes back and the figure is what was read, the next prompt held 18 tokens more than the figure; after the three whose words' share was guessed, 19 or 20 more. So the guess was a token or two under. After the plain answers of those conversations the figure was the two counts together, and the next prompt held 16 to 19 tokens more. The figure itself takes 0.24 ms for 300 answers of 2,000 characters of words after 4,000 of reasoning, beside the 0.33 ms it takes to read their stored parts, which the conversation's read does once for both.

**Tests:** the rule, case by case, with the rounding, the counts missing, and a property test that the figure lies between what was read and the two counts together; and what holds it to `for_model`: an answer of which nothing is sent back adds nothing, and one whose text goes back whole adds all that was written (`test_answering.py`). The stream's last chunk and the conversation read right after it say the same figure, for words alone, a thought and words, a thought alone and a model that wrote nothing; a failed turn has none (`test_turns_api.py`); a stopped one has none, in the stream, the stop's answer and the conversation; a stop that comes after the end answers with the figure the stream said (`test_turns_running_api.py`). Every recorded stream's turn has the member now, and nothing else of them changed.

### 2026-11-23: a turn says which limit its answer reached

A model's server says `length` for two limits: the one on a single answer's size, `max_output_tokens`, sent to Ollama as `num_predict`, and the end of the context window, `num_ctx` ([ADR 0007](0007-local-models-through-ollamas-native-api.md)). The record kept the word as the server said it, and the page had one sentence for both, "The answer reached the model's length limit, and stops here.", which reads as if the window were full ([ADR 0015](0015-the-web-chat-calls-the-api-from-the-browser.md), revision "the page says which limit an answer reached"). Only the server can tell them apart: it knows the window and the limit a request went out with.

- **The value:** a completed turn's `finish_reason` is `context_window` when the model stopped for `length`, and what it read and wrote together, the counts the turn keeps, reached the request's `context_window`, or went past it, as a server that counts oddly may say. Otherwise it stays `length`, also when the server counted nothing. `stop` stays `stop` whatever the counts say.
- **Where it is made:** where the `Ending` of a completed turn is made, in `actio.chat.answering` (`reason_for`), from the model's last event, the counts that `_believed` keeps, and the window of the request. Every place that says a turn reads the record's member, so the stream, the conversation read and a stop's answer say the same.
- **No column and no migration:** `finish_reason` is a string. The turns there were before keep `length` for both limits.
- **In the stream** the AI SDK's own `finishReason` for it is `length` (`ui_stream.FINISH_REASONS`): the library has no finer word. The turn's record in the metadata says `context_window`.
- **The member's description says so,** now that it is published: `stop`; `length`, when the model ran into the limit on a single answer's size; `context_window`, when the conversation and the answer together filled the model's context window.

**Measured** with Ollama 0.40.16 and `qwen3.5:4b-q4_K_M`, asked directly with what Actio's adapter sends (`truncate: false`, `shift: false`), on 2026-11-23:

| What ended the answer | `done_reason` | Read | Wrote | Together | The limits sent |
|---|---|---|---|---|---|
| the answer's own limit | `length` | 21 | 40 | 61 | `num_predict` 40, a window of 2,048 |
| the window's end, thinking | `length` | 390 | 1,658 | 2,048 | `num_predict` 2,000, a window of 2,048 in effect |
| the model, by itself | `stop` | 18 | 4 | 22 | `num_predict` 200 |

So at its own limit an answer has written exactly `num_predict`, and is far from the window's end. At the window's end what was read and written together is the window exactly, with `num_predict` not reached. What the maintainer saw that made this a step: an answer that thought, read 129 tokens, wrote 2,048, exactly the model's `max_output_tokens`, reasoning included, and ended with `length`, after 7,910 characters of thought and no words.

**Options considered, and not built:**

1. **A flag beside `capped_by_usage`,** such as `reached_window`, in a column of its own.
    - Con: a migration, for what the reason the record already holds can say. And a second member for the page to read beside the first, which two limits, never at once, don't need.
2. **The page works it out,** from the turn's counts and the models' limits in `GET /api/models`.
    - Con: the page would have to know the window the request went out with, which the models file can change between the question and the reading, and the server's rule for which counts are kept. As for the figure of the window, the server says it, and the page doesn't copy the rule.
3. **The server says it, in the reason it records.** Chosen.

**What it leaves:**

- **A server that counts nothing** can't be told apart: its turn keeps `length`, and the page says the answer reached the longest a single answer may be, which may be the window's end.
- **The second run asked for `num_ctx` 768 and the server held 2,048:** Ollama seems to keep a floor there. Seen once, not looked into, and not this fix's.

**Tests:** the rule, case by case, at the window, past it, one token short of it, with no counts, with no window, and a model that stopped by itself; and that a count the turn doesn't keep names no limit (`test_answering.py`). Over HTTP, the stream's last chunk and the conversation read say the same reason, with the library's `finishReason` `length` for both limits, and an answer cut at what a usage limit left it, far from the window's end, keeps `length` and `capped_by_usage` (`test_turns_api.py`). A recording was added: a model asked to think, that thought until it reached the longest a single answer may be and wrote no answer (`thought_too_long.sse`). Nothing else changed in the recorded files.

### 2026-11-25: a turn says which model answered

Since [ADR 0028](0028-a-model-names-those-that-stand-in-for-it.md) a model asked in the place of the one a turn asked may write its answer. The turn's record has one more member: `TurnOut.answered_by`, the model that answered, from a column of its own (`turns.answered_by`, migration 0014), written with the outcome as the moment of the first word is. A model that ended its answer without a word has answered too (`no_words.sse` names it). Empty in the stream's `start` chunk and while the turn runs, when no model answered, each having failed before its first word or the turn having been stopped while it waited, for a turn that was given up on, and for the turns recorded before. `model` stays the model that was asked, and `context_tokens` is of the window of the model that answered. Every recorded stream's turn has the member now, and one recording was added (`stood_in.sse`). The reasons, the options and the tests are in ADR 0028's revision "a turn says which model answered, and the page says so".
