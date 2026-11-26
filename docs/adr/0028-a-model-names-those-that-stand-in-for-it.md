# 0028. A model names the models that stand in for it, before the first word

- **Status:** accepted
- **Date:** 2026-11-25

## Context

A model on offer runs on one server ([ADR 0013](0013-the-models-on-offer-are-a-file.md)). When that server can't answer, the turn fails, and the page says why and offers another try. ADR 0013 left that under "Not done yet": "one model per name, with no second choice when its server is away". With the cloud providers' free tiers in the models file ([ADR 0027](0027-cloud-models-through-their-openai-compatible-layers.md)), a server that can't answer is no longer rare:

- **Groq's free plan refuses a request larger than its tokens a minute,** 8,000, with 413, which the client calls a `ContextOverflowError`: no wait makes it fit. And it refuses the requests over its limit of a minute with 429, a `RateLimitedError`.
- **`gemini-3.5-flash-lite` was too busy for 11 of its 17 answers with reasoning,** on 2026-11-25: refused before a stream, or broken off in the middle of one, 8 of them in the middle of its thoughts. None of 12 without reasoning was.
- **The local model is there all the while,** with a smaller window and no limit a minute.

ADR 0027 named this step under "Known, and left": "a chain of providers, falling back from one to another, trying again, and the cloud's own timeouts come in the next steps". This one is the first part of that router: another model is asked in place of one that can't answer. A second try of the same model, the providers' own timeouts, a check of capacity before asking, budgets and a circuit breaker come later.

What the choice had to fit:

- **Everything outside `actio.catalog` asks for a model by its name in the models file** (ADR 0013), and a turn's record names that model.
- **A turn is paid for at its model's price, held before the model is asked** ([ADR 0020](0020-usage-limits-on-turns.md)): the most it may cost, with the model's longest answer.
- **A server that writes only so many answers at once has a gate,** shared by the models on it ([ADR 0023](0023-answers-that-wait-their-turn.md)). A turn waits there, and gives its place back once its model's stream is closed.
- **What a reader is shown is what is stored.** A thought is streamed as it arrives, and the stream's format has no event that takes back what was shown ([ADR 0014](0014-turns-streamed-in-the-ai-sdks-format.md)).
- **A model's error's text never reaches a log.** It can quote the question, and Groq's names the account's organization ([ADR 0008](0008-postgres-through-sqlalchemy-and-psycopg.md), revision of 2026-11-23; ADR 0027).
- **The title job asks a model too,** and asks it again later after an error that another try may not meet ([ADR 0025](0025-titles-written-by-a-model.md), revision "a title its model couldn't give for now is asked for again").
- **A cancellation is how a turn is stopped, and how a job is cut off** ([ADR 0022](0022-turns-that-run-apart-from-their-request.md), [ADR 0024](0024-background-jobs-in-a-queue-in-postgres.md)). It is never a failure.

## Options considered

**Where a chain is written:**

1. **`then`, a list of other models on offer,** in the model's table: `then = ["gemini-lite", "standard"]`.
    - Pro: Groq's, the Gemini API's and a local model are different models, with their own windows, abilities and quality: "another model answers" is what happens, and the file says so.
    - Pro: nothing about a provider's model is written twice. Each stand-in keeps its own server, window, limits, reasoning settings, gate and price.
    - Pro: the file gets one key.
    - Con: every stand-in is on offer itself, and needs a price.
2. **A list of provider entries inside a model,** the plan's first idea: one name, several servers behind it.
    - Pro: the name stays abstract.
    - Con: each entry repeats a window, limits and reasoning settings.
    - Con: the same provider's model in two chains is written twice, and its limits would have to be shared between the copies.
3. **Three levels: servers, their models, and names that list them.**
    - Pro: the cleanest.
    - Con: a second way to write every models file there is.

**What the first word is,** after which no other model is asked:

1. **A word of text, or a thought.**
    - Pro: what a reader was shown stays shown, and is stored.
    - Con: the 8 of `gemini-3.5-flash-lite`'s 17 answers that broke off in the middle of their thoughts are past the point where a stand-in takes over, and still say that the answer broke off.
2. **A word of text only,** with the thoughts held back until it comes.
    - Con: no reader would see a model think, which the page shows as it happens.
3. **A word of text only,** with the thoughts that were shown taken off the page when another model takes over.
    - Con: the stream's format has no event for that. The page would show one model's thoughts above another's answer, or read the conversation again in the middle of an answer.

**Which failures move on to the next model, before the first word:**

1. **Only those that another try may not meet again:** a server away, too busy or at a limit, an answer that broke off, a provider that failed.
    - Con: Groq's 413 is a request too large for that account, which another model's window may hold. A model missing from its server, or a request one server refuses, may be had elsewhere.
2. **Every failure of a model call.**
    - Pro: nothing was shown, and another server may have the room, the model or the time.
    - Con: a request another server refuses too is asked of each.

**Whose failure a turn records, when every model failed:**

1. **The last one's.**
    - Con: Groq's model at its limit of a minute, then a local model whose window the conversation doesn't fit, would record the second: "This conversation has become too long for the model. Start a new one". A minute later the first would have answered it.
2. **The first one's.**
    - Con: a request refused by the first, and the second too busy, would say that another try won't help, when it may.
3. **The first that another try may not meet again, and without one the last one's.**
    - Pro: when an answer may come at another try, the reader is told so: the page offers it. When none may, the last says why.

**Whose price a turn is paid at:**

1. **The model that answered.**
    - Con: the credits are held before anyone knows which model will answer. A dearer stand-in would cost more than was held; a cheaper one would make a turn's cost depend on a server's state.
2. **The model that was asked.**
    - Pro: the price the reader chose is the price paid, and what is held is what is settled.
    - Con: a stand-in that costs more is paid at less, and one that costs nothing at more.

## Decision

**A model in the models file may name other models of the file, `then`, which are asked in its place, in that order, when it can't answer. Only before the first word, or thought, of an answer was handed on: after it, the answer is that model's, and its failure is the turn's. Before it, any failure of a model call moves on. When every model failed, the turn records the first error that another try may not meet again, and without one the last. Whoever answers, the turn is the asked model's: its name, its price, and no answer longer than its own may be.**

**The file** (`config/models.toml`, `actio.catalog`):

- **`then`,** in a `[models.<name>]` table, is a list of names of other models in the file. The model, then those it names, in order, is its chain.
- **A stand-in is a whole model on offer:** its own server, window, limits, reasoning settings, gate and price. Nothing more is written about it.
- **A stand-in's own `then` isn't followed:** only the asked model's list counts. So two models may name each other, and no model is asked twice.
- **Checked when the file is read,** each a mistake in one line that names the model and `then`, as the file's other checks do: a name that isn't one of the models, the model's own name, a name twice.
- **Left out, or empty,** the model has no stand-in, as before.

**The walk** (`actio_llm.router`, which knows nothing of the application): it is given the chain as links, in order, each a provider, the request for it, a name for the log, and what to hold while it is asked, the gate. It yields the events of the first link that answers.

- **One at a time, in order.** A later link is asked only after the one before it failed.
- **Before anything was handed on, any `LLMError` moves on:** too busy, a 429, a 413, a refused request, a model missing from its server, a provider that failed, a server too old, an answer that broke off with nothing in it yet, and a stream that ended without its last event.
- **After something was handed on, nothing moves on.** Something is a piece of text or of reasoning with text in it, a tool call, or the last event of a stream: a model that wrote nothing has answered too. The error is raised as it came, and no later link is asked.
- **A piece with no text in it is no word,** as a turn hands on nothing for it.
- **Only `LLMError` is sorted.** A cancellation, or any other exception, passes through as it came, from wherever the walk is waiting, and asks no later link.
- **One hold at a time.** A link that failed has its stream closed and its gate given back before the next link's gate is waited at. Two turns whose chains cross never each keep one gate while waiting at the other.
- **Closing the walk early** closes the stream of the link that is open, and gives its gate back, as closing a provider's stream does.
- **When every link failed,** one of their errors is raised, the object itself: the first one that another try may not meet again, and without one the last link's. Which errors those are is written once, `actio_llm.FOR_NOW`, which the title job reads too: `ProviderUnavailableError`, and so `RateLimitedError`, `StreamInterruptedError` and `ProviderError`.
- **Its caller is told** of each link that failed, with its error, as it happens, and which link answered.
- **With one link, the walk is that provider.**

**The catalog** (`actio.catalog`): `OfferedModel.then` holds the names. `Catalog.chain` gives a model's chain, and `Catalog.chain_status` says which of it is ready now, in order, each with its status, asked for at the same time. A model that isn't `ready` is passed over, `missing` or `unreachable` alike: it is not asked. `Catalog.status` is what it was, the model's own server's answer, reused for 15 seconds. A load ahead is the model's own, never a stand-in's.

**A turn** (`actio.api.turns`, `actio.chat.answering`):

- **The ready models of the asked model's chain are asked.** None ready: the 503 as before, with the asked model's name and its own state. The asked model not ready and a stand-in ready: the turn starts.
- **Reasoning:** a turn that asks for it, when the first ready model can't reason, is refused with the 422 as before.
- **The credits are held for the asked model,** at its price and up to its longest answer, as before. The turn's `model` is the asked one.
- **Each ready model gets a request of its own,** with the same messages: its own name on its server and its own window; told whether to reason only when it can (`thinking` as the turn asked, or else nothing: a model that can't reason must not be told); and no more output than its own longest answer, nor than was held. So no stand-in writes more than was paid for.
- **Each is asked behind its own gate,** through the walk. A turn that waited at a gate says so in the log, as before, for each gate.
- **Which limit an answer reached** is told by the window of the request that was answered: a stand-in's own.
- **The log,** in warnings, as the module's other lines: each model that failed and was passed over, with the turn's id, its name, its error's kind and the name of the one asked next; the last of a chain of several that failed, with "no other is left to ask"; each model passed over because it wasn't ready, with its state; and a stand-in that answered, in place of which model. Never an error's text: a test fails when one gets in. A turn whose model has no stand-in says what it said before, one line.
- **Everything else is as before:** when the first word came, the beat, the draft, what a stop and a shutdown do, what is recorded and counted.

**The title job** (`actio.chat.titles`): the same chain and the same walk, for the model that `titles` names, each with a request of its own (24 tokens, its own window, reasoning off for a model that can reason, nothing said of it for one that can't), each behind its gate, all inside the 30 seconds of a title. None of the chain ready: when one of them is `unreachable`, the title is asked for again later; when all are `missing`, there is none, as before. The walk's error is sorted by `FOR_NOW`, as before.

**The API** (`actio.api.catalog`):

- **`GET /api/models` lists `then`** for each model, the ids of its stand-ins, in order, empty for most. Its `status` is `ready` when it or one of its stand-ins is ready, and otherwise its own. Its `thinking` is the first ready one's, and its own when none is, as for a model that has no stand-in. `provider`, `model`, `context_window` and `max_output_tokens` stay the model's own.
- **`POST /api/models/{id}/warm-up`** answers 503 when none of the chain is ready, as a turn's route does; 204 when a stand-in is ready and the model itself isn't, since a stand-in is never loaded ahead; and otherwise as before.
- **The page didn't change.**

**The example** (`config/examples/free-cloud/models.toml`): `groq-large` names `gemini-lite`, then `standard`. Groq's free plan refuses a request larger than 8,000 tokens, and limits requests a minute: then flash-lite answers, with its larger window, and the local model when flash-lite is too busy. A question to `groq-large` may go to any of the three servers, as the file says, and its head already says what a free Gemini key means for what is sent. `default` and `titles` are the shipped model, as before. `config/models.toml` describes `then`, and names no chain: it offers one model.

**A key that isn't set still stops the start,** whatever chain names its model. The file names what an installation uses, and a stand-in doesn't make a missing key less of a mistake.

**Tests:** the walk's, with scripted providers and holds of their own, no network, each rule held by a test that fails without it; the file's three mistakes, a stand-in's own `then` not followed, and the example's chain; the chain's statuses; a turn answered by a stand-in, streamed and stored, completed, by the asked name and at its price; the stand-in's request; a failure after a word, or a thought alone, that asks no stand-in; the error when all failed; a stand-in at the end of its own window; a turn that gives its first gate back before it waits at the second, with the second full; a stop while it waits there, and while the stand-in writes; models that aren't ready, passed over, the 503 with the asked model's state, and the 422 by the first ready one; the log, with no error's text; a title written by a stand-in, put back or not by the rule, and none ready; the listing; and the warm-up.

## Consequences

- **Easier:**
    - a cloud model at its limit, or too busy, is answered for by another, with one line in the models file;
    - a request too large for Groq's free plan is answered by a model with more room, before the reader sees anything fail;
    - two turns whose chains cross can't hold each other's servers;
    - which errors are worth another try is written once, for the turn and the title.
- **Harder:**
    - **a question may go to any server of its model's chain.** On the Gemini API's free tier, what is sent may be read by human reviewers: a chain that names a Gemini model sends there whatever its first model couldn't answer;
    - **a turn is paid at the asked model's price,** whoever answered: a stand-in that costs more is paid at less;
    - **each stand-in is a model on offer,** with a price of its own, which anyone can choose by itself;
    - **a request that every server of a chain refuses is asked of each,** before the turn fails.
- **Known, and left:**
    - **a thought counts as the first word.** An answer that broke off in the middle of its thoughts says so, as before, and no stand-in takes over. Not counting thoughts would mean holding them back until the first word of text, or taking shown thoughts off the page, which the stream's format has no event for;
    - **which model answered is in the log, and nowhere else yet.** The turn's record and the page come in a later step. Until then the page takes a stand-in's answer for the asked model's, in two places:
        - **the line under the box** divides the tokens the stand-in counted by the asked model's window ([ADR 0015](0015-the-web-chat-calls-the-api-from-the-browser.md), revision "the page says how full the model's window is"). A conversation too large for Groq's 8,000 tokens, which flash-lite answers, reads as full, with more tokens than the window holds (see Measured);
        - **an answer that reached the end of the stand-in's window** is recorded so, and the page offers no other try for it, though the asked model, with a larger window, may answer one;
    - **no second try of the same model, and one patience for every kind of server.** A server that hangs holds a turn for the client's 90 seconds before the next model is asked. Both come in the next step. Since the revision of 2026-11-25 below, a model whose failure may be gone in a moment is asked once more, and a provider in the models file may say how long its server is given to begin an answer;
    - **a model whose gate is full is waited for, not passed over;**
    - **a server's health is its status,** asked at most every 15 seconds, until a circuit breaker says more. A turn's start, and a warm-up, wait for the status of every model of the chain, asked at the same time: a stand-in whose server doesn't answer the connection holds each of them for the catalog's 2 seconds, once in those 15 (see Measured). Before, only the asked model's own server could;
    - **a stop in the instant a model fails** reaches the turn at the next model's first wait, its connection: nothing is sent to that model, and the turn is recorded as stopped (see Measured);
    - **a stand-in is listed, and can be chosen by itself;**
    - **a load ahead is never a stand-in's:** a question that a stand-in answers waits for its model's load, where its server loads models.
- **How to reverse it:** for an installation, leave `then` out of the models file. In the code: `actio_llm.router`, the chain in `actio.catalog` (`then`, `chain`, `chain_status`, `Chain`), the requests and the walk in `actio.api.turns`, `actio.chat.answering` and `actio.chat.titles`, `then` in `ModelOut` with `make openapi`, and the warm-up's 204 for a stand-in.

## Measured

On 2026-11-25, on the tree as it was committed.

**Mistakes planted on purpose.** After the build and the review, 62 mistakes were planted one at a time in a scratch copy of the tree, and the tests that should notice each were run: 60 failed a test. Two of the 62 were the rules the review had found no test for, whose tests were written before the run: that a turn may not ask for reasoning because a later model could reason, and what a model is listed with when none of its chain is ready. The two that failed nothing:

- **the credits held for the first ready model, not the asked one.** The two differ only when the asked model isn't ready, and the test of that case looked at the answer alone;
- **the asked model's name, for the log's line about who answered in whose place,** taken from the first ready model: the same case.

One test now holds both: a model that isn't ready, passed over for a stand-in that costs nothing, costs the asked model's price, has the asked model's name in the usage ledger, and the log says which model answered in its place. Planted again, each of the two failed it.

**A stand-in whose server doesn't answer.** A chain's statuses were timed, for a model with one stand-in, both on servers that aren't there, asked three times by the clock given to the catalog:

| The stand-in's server | First | 5 s later | 16 s later |
|---|---|---|---|
| Refuses the connection | 0.01 s | 0.00 s | 0.00 s |
| Never answers the connection: an address nothing routes to | 2.00 s | 0.00 s | 2.00 s |

So a server that is down costs nothing, whether it is asked anew or its status is reused, and one that hangs costs a turn's start the catalog's 2 seconds for the connection, once in the 15 seconds a status is reused.

**A stop in the instant a model fails.** In a throwaway, the first of two providers failed, and the stop was given in that very moment; the second stood for an HTTP client, noting that its stream was started, then waiting as a connection does. Its stream was started and closed, the wait never ended, no event was handed on, and the stop was caught where a turn's is.

**On the real page,** the whole stack built from this tree, one guest in a real browser, with models files made for the look:

| What was set up, and asked | What the page showed, and the API's log |
|---|---|
| The default model on a server that isn't there, with the local `qwen3.5:4b-q4_K_M` in its `then`, and the same for titles. A short question | `GET /api/models` listed the model as `ready`, with its stand-in. The local model's answer, the turn over 5.5 s after the question: "first word after 4.5 s", since no stand-in is loaded ahead, 35 credits. A title, "Why do bicycle chains need oil?", written by the stand-in, the job 0.6 s. The log: "model away is unreachable, and is passed over", "model standard answered in place of away" |
| Groq's `openai/gpt-oss-120b` as the default, with `gemini-3.5-flash-lite` and then the local model in its `then`. A question of 57 characters | Groq's own answer, with a thought, folded, the turn over after 0.9 s: "first word after 360 ms", 67 credits, "150 of 8,000 tokens". Nothing in the log |
| The same, a question of 39,893 characters: 11,431 tokens by Actio's guess, more than Groq's free plan takes in a minute | flash-lite's answer, and a right one, the turn over after 1.4 s: "first word after 1.2 s", 3,237 credits. Under the box, "100% full · 12,857 of 8,000 tokens · start a new chat soon": flash-lite's count over Groq's window. The log: "model groq-large failed (ContextOverflowError), and gemini-lite is asked next", "model gemini-lite answered in place of groq-large" |
| The same, two questions of 19,950 and 20,423 characters, 4 s apart, each in a conversation of its own | The first was Groq's, with a thought, the turn over after 0.9 s: 1,399 credits, "5,386 of 8,000 tokens". The second was flash-lite's, without one, after 1.3 s: "first word after 910 ms", 1,664 credits, "6,575 of 8,000 tokens". The log: "model groq-large failed (RateLimitedError), and gemini-lite is asked next", "model gemini-lite answered in place of groq-large" |

No request of the page's was refused, and no script on it failed, in any of the five. They took four requests to Groq's chat, two of them refused, and two to flash-lite's. An answer that breaks off in the middle of its thoughts was not seen that day, and can't be brought about on purpose.

## Revisions

### 2026-11-25: a model is asked once more, and a server says how long it may take to begin

The decision asked each model of a chain once, and moved on at any failure before the first word; and every server had the client's 90 seconds to begin an answer. Under "Known, and left" it named both: "no second try of the same model, and one patience for every kind of server". They were left to this step on purpose, so that the walk could be built and looked at first, with nothing in it but the move to the next model.

**Which failures are asked again:**

1. **Every failure that another try may not meet again** (`FOR_NOW`).
    - Con: a server too busy, an answer that broke off and a connection that was never answered would each cost a second try before the model that stands in is asked, with nothing to say that the second would fare better.
2. **Only what costs little to ask again, and may be gone in a moment.** Chosen.
    - **A connection that failed at once:** nothing listens at the address, the name doesn't resolve, or the server hung up during the TLS handshake. Each was over within a tenth of a second (see Measured), and nothing was sent, so the same request goes again without a model writing an answer twice. A second try that fails so too costs as little. It can help where the second connection goes another way than the first: a name looked up again, another address behind the name, a server that hung up on one handshake. A server that is restarting is not back within that tenth of a second: the model that stands in is asked for it, as before. What the second try is worth was not measured: such a failure can't be brought about on a real server on purpose.
    - **A server that names a wait of 2 s or less** (`RateLimitedError.retry_after_s`): it says itself when it takes requests again, and the model asked is the one the reader chose. Groq names such a wait for a request that passes its minute's tokens by a little, and took the same request after it (see Measured).
    - **Not a connection that was never answered:** its 2 seconds are spent, and a second try would double the wait before a stand-in is asked.
    - **Not a server that hung up on a request:** the request may have been read, and a model may be writing an answer that nobody gets.
    - **Not a 503, a first line that came too late, or an answer that broke off:** nothing says that they are gone in a moment, and the model that stands in is there for them.

**How often:**

1. **Once for a whole walk.**
    - Con: a chain whose first server blinked would have no second try left for the next one's.
2. **Once for each model of a chain.** Chosen: each server's hiccup is its own. The pause it adds is 2 s at the most for each model; the second try itself may take as long as a first one.

**Where the turn waits:**

1. **Inside its place at the gate.** Chosen: a second try doesn't go to the back of the line, and the server is never asked for more answers at once than its gate allows, as before.
2. **Outside it, the place given back and waited for again.**
    - Con: a turn that had waited its turn would wait it again behind those that came while it paused.

**How a server's patience is said:**

1. **By the kind of server:** less for `openai-compatible` than for `ollama`.
    - Con: `openai-compatible` is Groq, and also LM Studio, which loads a model cold as Ollama does.
2. **A key of the provider, `begins_within_seconds`.** Chosen.
    - **Left out, 90 seconds, as before,** so that no installation changes by an upgrade. A short default would cut off a first question to a model that is still loading.
    - **The example's two cloud providers say 30.**

**What changed:**

- **A connection that failed at once has a class of its own,** `actio_llm.ConnectionFailedError`, a `ProviderUnavailableError`. `wire.stream_lines` and `wire.request_json` raise it for `httpx2.ConnectError`, and for nothing else, with the words of before, "can't reach …". Every other failure of the connection before an answer stays a plain `ProviderUnavailableError`, `ConnectTimeout` among them. By its parent it is in `FOR_NOW`, a turn records it as `model_unavailable`, and the catalog takes it for `unreachable`, as before. A log line that named `ProviderUnavailableError` for such a connection names the new class.
- **The walk asks a link once more** (`actio_llm.router`), when its try failed before anything of it was handed on:
    - **with a `ConnectionFailedError`,** at once;
    - **with a `RateLimitedError` whose `retry_after_s` is from 0 to `WAITS_AT_MOST_S`,** 2, after that wait. `None`, more, `inf`, `nan` and a negative wait move on, as before;
    - **with the same request,** through a new stream; **twice at most:** whatever the second try fails with is the link's failure. That is what `on_failed` is told, once for a link and after its last try, and what counts when every link failed;
    - **for each link of a chain,** each with a second try of its own;
    - **inside the link's hold,** entered once: the first try's stream is closed before the pause, and the hold is given back once the second try's stream is closed, before the next link's is waited for;
    - **a cancellation passes through the pause,** as from a hold or a stream, and so does any exception that isn't an `LLMError`, from either try. After something was handed on, nothing changes;
    - **its caller is told,** through a new callback, `on_again`: the link, its error and the pause, before the pause. The pause is `asyncio.sleep`, which a test replaces (`pause`).
- **A turn and the title job say it in the log,** in a warning: `turn …: model standard failed (ConnectionFailedError), and is asked once more`, and after a server's wait `… and is asked once more after 2 s`; the title job the same, with `conversation …:`. The error's kind, never its text, and for a turn of one model too. Nothing else changed for them: what is recorded, the first word's time, the beat, the stop. The title's 30 seconds hold the pause and the second try with everything else, and which errors put its job back is as before.
- **A provider may say how long its server is given to begin an answer:** `begins_within_seconds`, in a `[providers.<name>]` table, for either kind of server. It is the seconds from the moment a request is sent to the first line of its answer, after which the server counts as away and the next model of a chain is asked: the client's `Timeouts.first_event_s` for that provider's models.
    - **A number greater than 0, whole or not.** 0, a negative number, a string, `inf` and `nan` are each a mistake in one line that names `providers.<name>.begins_within_seconds`.
    - **`catalog.build` gives each provider's clients the catalog's `TIMEOUTS` with that one value replaced** (`ProviderEntry.timeouts`), for an Ollama and for a server that speaks OpenAI's API alike. The status calls keep their 3 seconds, and every connection its 2. Each client says how long it waits (`timeouts`).
    - **A load ahead on Ollama waits as long,** since it waits as a chat waits for its first line ([ADR 0026](0026-models-loaded-ahead-of-the-first-question.md)).
- **The files:** `config/models.toml` describes the key, and names no value: its Ollama keeps 90 seconds, which covers a model's cold load. `config/examples/free-cloud/models.toml` gives `groq` and `gemini` 30 each, with the reason in a comment: a cloud server that hasn't begun after half a minute has more likely hung than is still loading, and the model that stands in is asked sooner. Its local provider says nothing. The end-to-end stack's file is unchanged.

**Tests:**

- **the walk's** (`packages/llm/tests/test_router.py`), with scripted providers and holds, and a pause that waits nothing where a test needs it: a connection that failed, asked again at once with an equal request, and its answer the walk's; a wait of 0, of a fraction and of exactly 2 s waited, and none, 2.001, 20, `inf`, `nan` and -1 moving on; the pause as long as the server asked, by the event loop's clock, with nothing asked during it; each other failure asked once; twice at most, the second error told and counted; a second try for each link; the order of a hold, the tries, `on_again` and the next hold; a cancellation during the pause, and another exception from the second try; nothing asked again after something was handed on; and what a second try handed on being the link's answer, as a first try's is;
- **the adapters'** (`test_ollama.py`, `test_openai_compatible.py`): `ConnectError` from the transport is a `ConnectionFailedError`, for a chat and for a small call, and `ConnectTimeout`, `ReadError` and `RemoteProtocolError` a plain `ProviderUnavailableError`;
- **the API's:** a turn answered at its model's second try, streamed and stored once, with the log's line; a model that fails twice, with no stand-in and with one; a stop during a pause of 2 s, recorded as stopped within a second; no error's text in the new lines (`test_turns_stand_ins_api.py`); a title written at the second try, one put back after two failures, a pause of 2 s cut off by a title's time limit, and no word of the question in the log (`test_titles.py`); the key read for both kinds, its mistakes and the files' values, a server that hasn't begun within its 0.05 s found away, for either kind, and a provider that says nothing given 90 s (`test_catalog.py`).

**Known, and left:**

- **a limit on how long a whole answer may take** comes in the next step. A server that sends lines with no word in them is waited for until then;
- **the connect limit stays the catalog's 2 s,** for status calls and answers alike;
- **a 429 that asks for more than 2 s is not waited for:** the model that stands in answers instead. Groq asks for as long as its minute's tokens take to free, and the Gemini API for the rest of its minute (see Measured);
- **the log doesn't say how long a server asked to wait, when that isn't waited:** its line names the error's kind alone. Why a model wasn't asked once more can't be read there, as in the look below;
- **the pause holds the place at the gate,** so one of a gate's places serves nobody else for up to 2 s: behind a gate of one place, as an Ollama's is unless the file says more, that is the server;
- **a server that is restarting is not back for a second try at once,** and a pause before it wasn't added: the model that stands in is asked instead;
- **the title job's 30 seconds hold the second try too,** and its own waits between its tries are still its listing's: a server's longer wait isn't followed there ([ADR 0025](0025-titles-written-by-a-model.md)).

#### Measured

On 2026-11-25, on this tree: what the HTTP client, `httpx2` 2.16.0, raises with the timeouts `stream_lines` uses and the catalog's connect limit of 2 s, each for a POST, timed from the call.

| What the server does | After | Raised |
|---|---|---|
| Nothing listens on the port: refused | 0.06 s | `ConnectError` |
| Its name doesn't resolve | 0.03 s | `ConnectError` |
| TLS, and the server hangs up during the handshake | 0.00 s | `ConnectError` |
| Never answers the connection (an address nothing routes to) | 2.00 s | `ConnectTimeout` |
| TLS, and the server says nothing | 2.00 s | `ConnectTimeout` |
| Accepts, and closes at once | 0.00 s | `ReadError` |
| Accepts, reads the request's head, and closes | 0.00 s | `RemoteProtocolError` |
| Accepts, and resets | 0.00 s | `ReadError` |

`ConnectError` is a `NetworkError`, and `ConnectTimeout` a `TimeoutException`. Both are `TransportError`s, and neither is the other: so a connection that failed at once, with nothing sent, is exactly `httpx2.ConnectError`.

**On the real servers,** the same day, with free keys, each request a made-up text sent straight to the provider:

| What was asked | What came back |
|---|---|
| Groq, `openai/gpt-oss-20b`: a request that took about half of the minute's 8,000 tokens, then one that passed what was left by 61 | The second was refused after 0.13 s: "Limit 8000, Used 3710, Requested 4351. Please try again in 457.5ms", with `retry-after: 1`. Sent again 1.0 s later, it was taken, and answered in 0.41 s |
| The Gemini API, `gemini-3.5-flash-lite`: short requests one after the other, against its 15 requests a minute | Fifteen were taken. The sixteenth, 20.8 s after the first, was refused after 0.13 s: "Please retry in 49.846993202s", a `retryDelay` of `49s` in its body, and no `Retry-After` header. Fourteen of the fifteen were answered whole in 0.53 to 1.15 s, and one in 9.90 s |
| The same model: a prompt of 100,000 characters, and one of 400,000, which it counted as 113,754 tokens | The first line of the answer came after 1.82 s, and after 3.95 and 4.51 s in two tries |
| The same model: a prompt of 800,000 characters, near its 250,000 tokens a minute, four times in three minutes | Never answered: 503 after 2.80 and after 4.12 s, and 429 twice, one of them naming the minute's input tokens (`GenerateContentInputTokensPerModelPerMinute-FreeTier`) with "Please retry in 10.78691826s". The prompt of 400,000 characters, sent 19 s after the second 503, was refused for the same limit, "Please retry in 20.008768968s" |

So the second try does what it is for with Groq, which names the time its minute needs. The Gemini API's waits were 10 to 50 s, which are not waited: the model that stands in is asked. And of the requests these servers answered at all, the slowest took 9.90 s whole, a short one, and a prompt of more than 100,000 tokens began within 4.51 s: 30 seconds are three times the slowest.

**On the whole stack,** built from this tree, one guest through the API, with a models file made for the look: the repository's fake model on the machine, given 3 seconds to begin, with the local `qwen3.5:4b-q4_K_M` after it; and Groq's `openai/gpt-oss-120b`, with `gemini-3.5-flash-lite` and the local model after it, each cloud provider given 30 seconds.

| What was set up, and asked | What came back, and the API's log |
|---|---|
| The fake model, a question that it begins to answer after 10 s | The local model's answer, its first piece 6.24 s after the question, the turn over after 7.10 s. The log, 3 s after the question: "model fake-first failed (ProviderUnavailableError), and standard is asked next", then "model standard answered in place of fake-first". Without the delay, the fake's own answer began after 0.03 s |
| The fake model stopped, while its status still said `ready` | The local model's answer, its first piece after 0.54 s. The log: "model fake-first failed (ConnectionFailedError), and is asked once more", and 1.6 ms later "model fake-first failed (ConnectionFailedError), and standard is asked next" |
| Groq, a question of 14,532 characters, and then one of 19,799, sized to pass what Groq said was left of its minute by about 100 tokens | Groq's own answer to both, the second's first piece after 2.15 s. The log: "model groq-large failed (RateLimitedError), and is asked once more after 1 s", and nothing else |
| The same, the second question sized to pass by about 60 tokens | Groq took it at once: its first piece after 1.10 s, and nothing in the log |
| The same, the second question sized to pass by about 130 tokens | Groq refused it and named a wait that was not waited, so longer than 2 s; the log doesn't say how long. flash-lite, asked next, had not begun after its 30 seconds, by the log's two times 30.009 s apart, and the local model's answer was over 12.7 s after that: its first piece came 43.04 s after the question. Before this revision flash-lite would have had 90 seconds |

The three tries with Groq show how narrow its short wait is: 8,000 tokens a minute are 133 a second, so only a request that passes by 266 tokens or fewer is asked to wait 2 s or less, and what Groq takes a request for at the door is not its count afterwards: one request was refused as 4,057 tokens, and counted as 3,932 when it was answered a second later. Asked through the walk and the client alone, outside the stack, such a pair of requests had the second refused with a wait of 1 s, asked once more and answered, 2.32 s in all. Whether flash-lite would have begun after its 30 seconds is not known: it is one request.

**Mistakes planted on purpose.** After the build and the review, 46 mistakes were planted one at a time in a scratch copy of the tree, and the tests that should notice each were run. The two tests the review had found missing were written before that run: what a second try handed on, and a title's time limit over the pause. 45 of the 46 failed a test. The one that failed none:

- **a link asked again as often as it fails so, not once.** The walk's tests never ended, and were stopped after five minutes. Their stand-in for the pause returned without letting the event loop run, so the walk went round without end, and the time limit that every such test has could not fire. The stand-in now lets the loop run once, as the real pause does. Planted again, against each layer's tests alone, the mistake failed one of the walk's when its 5 seconds were over, one of a turn's and one of the title job's.

One more was planted for the first of the review's two tests, an error after a second try had handed something on taken for the link's failure: it failed that test, and none of the older ones. The second has no mistake of its own: where the walk stands inside the title's time limit can't be changed by replacing one text. On the tree as it was committed, all 49 plantings, the 47 mistakes with that one three ways, failed a test.

### 2026-11-25: the longest an answer may take

The decision, and the revision before this one, gave a server only so long to begin an answer (`begins_within_seconds`), and 20 seconds between two lines of it, and nothing for the whole. The revision named it under "Known, and left": "a limit on how long a whole answer may take comes in the next step. A server that sends lines with no word in them is waited for until then". It was left so that the second try and the time to begin could be built and looked at first. Measured with a throwaway server, both clients were still reading a server that sends a line every half second, with words or without, when the probe closed them after 8 seconds (see Measured). By the rules, nothing would have ended them, and a turn on such a server would keep its reader, and its place at the gate, for as long as the server sent.

**Where the clock runs:**

1. **Around the turn,** in `actio.chat.answering`, around the walk.
    - Con: it would count the wait at a gate, which isn't the server's.
    - Con: an answer cut there, with nothing handed on, couldn't move on to the model that stands in: the walk would be over.
2. **In the model client, for one request** (`actio_llm.wire.stream_lines`), by the event loop's clock, from the moment the request is sent: the same instant the first line's time counts from. Chosen.
    - Pro: a request with nothing handed on when its time is over is a failure of that model before the first word, and the walk moves on, as after any other.
    - Con: each model of a chain has a time of its own, and so has a second try (below, "Known, and left").

**One figure for every server, or a provider's own:**

1. **One figure.**
    - Con: wrong both ways. A slow machine's full answer takes longer: 2,048 tokens at 5 a second are 410 s. A fast server's takes a small part of it: Groq's `openai/gpt-oss-120b` wrote a full answer of 2,048 tokens in 4.3 s (see Measured).
2. **The client's own, 300 seconds, and a key of the provider, `answers_within_seconds`,** as its time to begin is said. Chosen.
    - **No rule ties the two keys together:** a file that gives less for the whole than to begin is no mistake, and the first line then has to come within the shorter.

**How a turn whose answer was cut is recorded,** when something of it was handed on:

1. **`failed`.**
    - Con: a failed turn costs nothing, and anyone can ask for a long answer on purpose.
2. **`stopped`.**
    - Con: nobody stopped it, and the page would say that the answer was stopped.
3. **`completed`, with a reason of its own, `time_limit`.** Chosen: an answer that reached a limit, as one at its longest size does, and is paid for as a stopped turn is, by guess, since a stream that is closed sends no count.

**Whether the clock leaves out the caller's time,** as the time between two lines does, so that a slow reader isn't taken for a slow server:

1. **Leave it out.**
    - Con: a server that sends faster than it is read would never be over. A line that was received already is read without a wait, so a time limit around a read lets every such line through, however late (see Measured).
2. **Count it.** Chosen: the limit is a bound. The clock is asked before each line is handed on, and nothing is handed on once the time is over, not even what was received already.

**What changed:**

- **The client** (`actio_llm`): `Timeouts.answer_s`, 300 s by default, from sending a chat request to the end of its answer. A new error, `TookTooLongError`, a `StreamInterruptedError`, so that it is in `FOR_NOW` by its parent and whoever doesn't know it takes it for an answer that broke off. Its text names the provider and the seconds, as the others do. `stream_lines` holds to these rules:
    - **before the first line,** whichever of the two times is over first ends the wait, with the `ProviderUnavailableError` of before, "didn't start answering within N s", N the shorter. Nothing came, so nothing changes for the walk;
    - **after the first line,** once `answer_s` is over, `TookTooLongError` is raised, whatever the server is doing: silent, still sending, or far ahead of its reader. No line is handed on after that;
    - **a silence that both times would end** is the one over first: `idle_s` with time left is the `StreamInterruptedError` of before, and the answer's time first is `TookTooLongError`;
    - **an answer that ends in time is untouched,** and raising closes the connection, as every other ending does.
- **A load ahead** on Ollama is read by the same function, and waits for its one line as an answer's first line is waited for: within the shorter of the two times. With the 300 seconds that is the time to begin, as before. A file that gives its server less for a whole answer than to begin one bounds its loads by that too.
- **The models file:** `answers_within_seconds`, in a `[providers.<name>]` table, for either kind of server, checked as `begins_within_seconds` is: 0, a negative number, a string, `inf` and `nan` are each a mistake in one line that names `providers.<name>.answers_within_seconds`. `ProviderEntry.timeouts` replaces each of the two values where its key is there. `config/models.toml` describes it, and names no value; `config/examples/free-cloud/models.toml` names none either: nothing measured supports another number for Groq or the Gemini API.
- **A turn** (`actio.chat.answering`): `TookTooLongError` out of the walk, after something was handed on, a word or a thought, ends the turn `completed`, with the `finish_reason` `time_limit`, no counts of a model's and no times. It is counted by guess (`_used`, unchanged), asks for its conversation's title as any completed turn does, and says one warning in the log, with the turn's id and the model that was answering, never the error's text: "model … was still writing when its server's time for an answer was over, and the answer stops here". Its stream ends as a completed turn's does, `finish-step` and `finish`, whose `finishReason` is `other`: the library's `length` is a limit on tokens, and the turn's record in the metadata says which limit.
- **With nothing handed on,** the walk has moved on to the next model already. When the error out of the walk is `TookTooLongError` all the same, of the one model or as what it raises of several, the turn is `failed`, costs nothing, and has a code of its own, `model_too_slow`: "The model took too long, and wrote no answer." Not `answer_interrupted`, whose words say the connection was lost.
- **The API's description** says `time_limit` under `finish_reason`, `model_too_slow` under `error`, and that `context_tokens` is empty for such a turn too.
- **The page** (`apps/web/lib/chat/outcomes.ts`) says it in the words of the size limit beside it: "The answer took the longest a single answer may take, and stops here."; with no words, for a turn asked to think, "The model was still thinking after the longest a single answer may take, and wrote no answer. Try again, or turn off “Think before answering” first."; and otherwise "The model took the longest a single answer may take, and wrote no answer." Another try is offered as for the size limit: after an answer without words, and not after one with them. `model_too_slow` has the API's words.
- **The end-to-end stack** offers the fake once more, `hurried`, behind a provider of its own at the same address that is given 3 seconds for an answer, and priced as `standard`.

**Tests:**

- **the client's** (`packages/llm/tests/test_ollama.py`, `test_openai_compatible.py`), with short times and a time limit of their own: a server that sends a line a hundred times a second and never ends, cut once its time is over and not before, its connection closed, for both kinds, with words and with lines that hold none (comments and empty pieces for the OpenAI one, empty content for Ollama's), the second with nothing handed on, and no more lines sent than the time has room for; a server that begins late, given what is left of its time and not the whole of it again; forty lines received at once, of which none is handed on after the time, which a time limit around the read alone would fail; the first line's wait ended by the shorter time, a load's too; a first line read after the time, the event loop having been held, taken for a server away; a silence that the gap ends, and one the answer's time ends first, each over when its own time is; an answer that ends in time read whole; and the default, 300;
- **the walk's** (`test_router.py`): `TookTooLongError` before anything was handed on moves on and isn't asked again, and after a word or a thought is raised as it came, with no later model asked;
- **the catalog's** (`test_catalog.py`): the key read for both kinds, alone and beside the other, its five mistakes, each value reaching the clients, the shipped and the example's files at 300 and the end-to-end file's `hurried` provider at 3, and a server still writing after its 0.2 s cut, for either kind;
- **the turn's:** after words, and after a thought alone, `completed` with `time_limit`, the answer streamed and stored once, its credits the guess and its row in the ledger marked as one, no counts and no figure of the window, the first word's time, `finish` with `other`, and the log's line without the error's text (`test_turns_api.py`); nothing written, `failed` with `model_too_slow`, free, in its own words; with a stand-in, the stand-in answering after nothing was written and not asked after a word, and a stand-in's own answer cut at its time, which the log says of the stand-in (`test_turns_stand_ins_api.py`); a title asked for (`test_titles.py`); and the code (`test_answering.py`);
- **a recorded stream,** `took_too_long.sse`, which the web app's contract test reads with the real `ai` package;
- **the page's:** the three sentences and the failure's words, another try offered or not, a stored answer and one that arrives (`components/chat/chat-app.test.tsx`); and the whole path on the stack: an answer cut after a few words, its sentence, no other try, its cost, and the same after a reload (`apps/web/e2e/time-limit.spec.ts`).

**Known, and left,** each worked out from the rules, not seen:

- **the time is one request's:** each model of a chain has its own, and so has a second try. A chain of three servers that each send lines with no word in them keeps a reader three times as long before it is told;
- **the wait at a gate isn't counted,** as it isn't for the first line;
- **the time between two lines stays 20 seconds** for every server;
- **an answer cut at its time is counted by guess,** since a stream that is closed sends no count: so its tokens, its rate and how full the window is are not said for it;
- **a model's thoughts count toward the same time;**
- **the title job's own 30 seconds are unchanged;**
- **a machine that writes fewer than 6.8 tokens a second doesn't finish a full answer of 2,048 tokens in the 300 seconds,** and one that writes a little more may not either, since loading the model and reading the conversation count toward the same time: its file raises the key.

#### Measured

On 2026-11-25, on the tree before this change, with a throwaway server outside the repository: both clients, given 3 s for the first line and 2 s of idle time, and a server that sends a line every 0.5 s. Each was still open when the probe closed it after 8 s.

| Client, and what the server sent | Events handed on | After 8 s |
|---|---|---|
| `OpenAICompatible`, comments only (`: keep-alive`) | 0 | still open |
| `OpenAICompatible`, chunks whose delta has no text | 0 | still open |
| `OpenAICompatible`, a word every 0.5 s, never a finish | 16 | still open |
| `Ollama`, lines whose content is empty | 0 | still open |
| `Ollama`, a word every 0.5 s, never done | 16 | still open |

With the same probe, a reader that falls behind:

| What was sent | What the reader did | What came |
|---|---|---|
| 400 lines in one write | took one, slept until a deadline was 0.3 s in the past, and read on under `asyncio.timeout_at(deadline)` | 399 more lines, then `TimeoutError` |

So a deadline of asyncio's alone does not hold for lines that were already received.

How fast answers are written here, by the local stack's own record of this day: the shipped local model 34.5 to 39.9 tokens a second in 12 answers, and 10.9 to 24.6 in 4 (cause not known), a full answer of 2,048 tokens in 52.9 s; Groq's `openai/gpt-oss-120b` 467 to 475 tokens a second in 3 answers, one of them a full 2,048 tokens in 4.3 s. No other cloud model was timed. Worked out, not seen: 2,048 tokens in 300 s are 6.8 tokens a second.

**On the whole stack,** built from this tree, with the shipped models file and one line added to it, `answers_within_seconds = 8` for its Ollama: one guest on the page, the local `qwen3.5:4b-q4_K_M`, and a made-up question that asks for an essay of about 1,500 words, once in each of three engines (Playwright 1.65's Chromium, Firefox and WebKit); then once more in Chromium with "Think before answering" on, and a question that asks to count primes.

| Engine, and what was asked | The turn was over after | What the page showed |
|---|---|---|
| Chromium, the essay, the model not loaded yet | 8.49 s | 704 characters of the answer, ending in the middle of a sentence. "The answer took the longest a single answer may take, and stops here." No "Try again". "first word after 4.4 s", "191 credits" |
| Firefox, the essay | 8.53 s | 1,497 characters, the same sentence, no "Try again". "first word after 300 ms", "360 credits" |
| WebKit, the essay | 8.45 s | 1,505 characters, the same sentence, no "Try again". "first word after 540 ms", "372 credits" |
| Chromium, thinking on | 8.44 s | The thought, folded away, and no answer. "The model was still thinking after the longest a single answer may take, and wrote no answer. Try again, or turn off “Think before answering” first." "Try again" offered. "first word after 490 ms", "302 credits" |

After a reload each page showed the same answer, the same sentence and the same figures. No request of the page's was refused, and no script on it failed. The list under each answer said no rate, and the line under the box nothing of the window: such a turn has no counts. The API's log had one line for each of the four turns: "model standard was still writing when its server's time for an answer was over, and the answer stops here". Ollama's own log has each of the four requests over after 8 s (`8s` three times, `8.01s` once): it stopped writing when the client hung up. The conversation in WebKit's picture had its title from the model.

**Not seen on a real server:** a turn that failed as `model_too_slow`. A server that sends lines with no word in them for its whole time can't be had on purpose from Ollama or from the two clouds: the tests hold it, with a scripted model and with a server of their own.

**The end-to-end tests,** on their own stack: 78 passed, the new one in 3.9 s, its answer cut after the fake's first words.

**Mistakes planted on purpose.** After the build and the review, 38 mistakes were planted one at a time in a scratch copy of the tree, and the tests that should notice each were run. 34 failed a test. The four that failed none were all in the client's clock:

- **a read after the first line that waits the idle time alone.** A silence was still said as the answer's time, but only once the idle time was over: the test of the two silences compared the error, not when it came. It now ends within a second, where the longer time is three;
- **an answer given twice its time.** The test's bound was a second, for a time of 0.3 s. The server of that test now counts the lines it sent, and the test holds the count: a busy machine sends fewer in a given time, never more, so the bound can't fail by the machine;
- **the time counted from the first line, not from the request.** No test's server began late. One now does: its first line after half a second, of one second for the answer;
- **the answer's error in the place of a server away, for a first line read after the time.** The review found the same branch by reading. A test now holds the event loop while that line arrives, so that only the clock can tell.

The tests for these four were written after that run, with the two the review asked for: a load cut at the shorter time, and a stand-in's answer cut at its time, for which one more mistake was planted, the log naming the asked model. On the tree as it was committed, all 39 failed a test.

### 2026-11-25: a turn says which model answered, and the page says so

The decision left it under "Known, and left": "which model answered is in the log, and nowhere else yet. The turn's record and the page come in a later step." Until then the page took a stand-in's answer for the asked model's in the two places that item names. The line under the box divided the tokens a stand-in counted by the asked model's window: seen on the real page, "100% full · 12,857 of 8,000 tokens" (see the decision's Measured). And an answer that reached the end of a stand-in's own window was said as the asked model's, with no other try, though the asked model, with a larger window, may answer one: worked out from the code, not seen. The title job's log, too, named the model that was passed over, and not the one that wrote the title. It was left so that the walk could be built and looked at first, with the turn as it was.

**Where it is kept:**

1. **A column of the turn, `answered_by`,** written with its outcome. Chosen.
    - Pro: every place that says a turn says it from the record, as it says when the first word came: the stream's last chunk, the conversation read, a stop's answer.
    - Con: a migration, and the turns recorded before it have none.
2. **Worked out where the turn's record is built,** as how much of the window a conversation takes is (ADR 0014, revision "a turn says how much of the window its conversation takes").
    - Con: nothing that is stored says it. Which model answered was the walk's to know, in the process that ran the turn, and is gone with it.
3. **The log alone,** as before.
    - Con: the page can't read it.

**What it holds:**

1. **The model that wrote the answer, whichever it was,** the asked one too. Chosen: empty means that no model answered, never "the one asked".
2. **A stand-in's name alone.**
    - Con: empty would mean two things, the asked model answered or no model did, and a turn recorded before the column would read as the asked model's own answer for certain.

**Whether a turn that was given up on says it:**

1. **Saved with the beat,** as the draft is.
    - Pro: a turn whose process died would say which model was writing it.
    - Con: a running turn's record would say it after its first beat and not before. And the beat would save one more thing that the answer doesn't need, which was decided against for the moment of the first word (ADR 0014, revision "a turn says when its first word came").
2. **Written with the outcome alone.** Chosen: a turn given up on says nothing of it, as it says nothing of its first word.

**What the line under the box says after a stand-in's answer:**

1. **Nothing.**
    - Con: a conversation too large for the asked model, which a stand-in answers each time, would never say how full it is.
2. **The stand-in's tokens over the asked model's window,** as before.
    - Con: what was seen: more tokens than the window holds.
3. **The stand-in's tokens over its own window,** when it is one of the models that stand in for the model that answers. Chosen: the tokens are the counting model's, and the window that held them is its own. A figure is said when its model may answer the next question: the model that answers, or one it names. Any other model's figure says nothing, as before.

**Where the page says who answered, and when:**

1. **The last item of the list under an answer,** "answered by" and the model's title, only when another model than the one asked wrote it, and once the models are read. Chosen.
    - Pro: the list is what an answer took, and which model wrote it is one of those things. Last, so that nothing else in the list moves when the models come.
    - Con: not said while the answer arrives, only once its turn has ended, as the rest of the list.
2. **Under every answer,** the asked model's too.
    - Con: a line that says the same of nearly every answer: what the empty conversation already says, "Answered by …".
3. **A notice.**
    - Con: a notice is for what needs a reader's attention. An answer that another model wrote is an answer.
4. **While the answer arrives,** in a chunk of the stream's own.
    - Con: which model answers is known at the first word, after `start` was sent. A new kind of chunk, for what the record says at the end.

**What changed:**

- **The record** (`actio.chat`): `turns.answered_by`, migration 0014, the name in the models file of the model whose answer the turn holds: the link of the walk that handed something on (`Walk.answered`), a word, a thought or its last event. `answering.answer` puts it into whatever `Ending` it records, once, before the outcome is written, and `turns._end` writes it as it writes `first_word_at`. Empty while the turn runs; when no model handed anything on, because every model failed before a word, or the turn was stopped or interrupted while it waited; for a turn given up on, with a draft or without; and for the turns recorded before. Not saved with the beat. Migration 0014 fills in nothing for the turns that exist.
- **The API:** `TurnOut.answered_by`, by the model's `id` in `GET /api/models`, in the stream's `finish` and `message-metadata` chunks, empty in `start`, in the conversation read and in a stop's answer: the stream's last chunk and the conversation read afterwards say the same. `model` is described as the model that was asked, and `context_tokens` as of the window of the model that answered. Every recorded stream's turn has the member now, and nothing else of them changed. One recording was added: a turn whose asked model failed before its first word and whose stand-in, with a window of its own, answered (`stood_in.sse`).
- **The title job:** one warning when a model asked in another's place answered, in the words of the turn's line: "conversation …: model second answered in place of standard". Names and ids alone. It says which model answered, not that a title was written: one that answered with nothing to make a title of is named too, and the line after it says that none came.
- **The page** (`apps/web/lib/chat`, `components/chat`):
    - **whose figures a turn's are** is written once, `answeredBy` in `lib/chat/figures.ts`: the model its record names, and the asked model for a turn whose record names none, as one recorded before;
    - **the list under an answer** (`figuresOf`) ends with "answered by" and that model's title in the models list, or its name in the record when the list no longer has it, when it isn't the asked model. Not before the models are read. The models reach the list through a React context (`ModelsOnOffer` in `components/chat/message.tsx`), read by `Took` alone: the models are read again every five seconds while the model that answers can't, and an answer's words are drawn again for none of it;
    - **the line under the box** (`contextFill`) takes the newest figure, as before, of the model that answered it: over that model's own window when it is the model that answers, or one that stands in for it, from the models list; and nothing otherwise, as before;
    - **a turn that a stand-in answered, and that filled the stand-in's window,** says "Another model answered in place of the one you asked, and the conversation has filled its context window: the answer stops here. Try again, or start a new chat to go on.", and with no words "… its context window: it wrote no answer. Try again, or start a new chat to go on.", and offers another try. The asked model's own window, and a turn whose record names no model, are said as before, with none.
- **The end-to-end stack** offers a fifth model, `away`, on a provider of its own at a port of the fake's container where nothing listens, so that its connection is refused at once (see Measured), with `then = ["standard"]`, a title of its own, "Away", and a window of its own, half of `standard`'s, priced as `standard`.

**Tests:**

- **the API's:** the record of each ending, in the stream's last chunk and in the conversation read after it, the two equal, and none in `start`: the asked model's own answer; a stand-in's; a stand-in's after the asked model was passed over, not ready; every model failed before a word; a stand-in that failed after a word, and after a thought alone; a stand-in's answer cut at its time; and a stand-in that finished and wrote nothing, whose turn has no answer (`test_turns_stand_ins_api.py`). A turn stopped while its stand-in writes, and while it waits at the stand-in's gate, in the stop's answer too; a turn given up on with a draft (`test_turns_running_api.py`) and without one (`test_turns.py`); a turn a shutdown interrupted, one cancelled from outside and one that failed inside Actio after a word, each naming the model that had written it (`test_turns_running_api.py`); the title job's line, for a stand-in that wrote the title and for one that answered with nothing to make one of, and none when the model named for titles answered (`test_titles.py`); the record of `stood_in.sse`; and the end-to-end file's `away` (`test_catalog.py`);
- **the page's:** `figuresOf`, the item last, by its title or the record's name, and none for the asked model's answer, a record that names none, models not read or a turn that runs (`lib/chat/figures.test.ts`); `contextFill`, each row of the rule, a turn that left its question open, a turn the model that answers wrote in another's place, and one whose record names none (`lib/chat/context.test.ts`); on the page, the item of an answer that arrives a frame after its last words, of a stored one at once, and of one whose models come later, after the rest of the list, which stays the same elements, with the answer's words drawn once, also when the models are read again (`components/chat/answered-by.test.tsx`); the line under the box of a stand-in's answer (`components/chat/context-meter.test.tsx`); the two sentences and another try, and the asked model's window and a record that names none as before (`components/chat/chat-app.test.tsx`); and `stood_in.sse`, read by the real `ai` package (`tests/contract/turn-stream.test.ts`);
- **end to end** (`apps/web/e2e/stand-in.spec.ts`): a question given the name `away` on its way out, the fake's answer, the turn's record, "answered by Fake model" last under it, the line under the box in the fake's window, and the same after a reload.

**Known, and left,** each worked out from the rules, and not seen unless it says so:

- **a turn that was given up on doesn't say which model was answering it;**
- **the turns recorded between the decision and this revision don't say either:** the page takes them for the asked model's, in the line under the box, the list and the notice;
- **the line under the box reads in the window of whoever answered last,** so in a chain it can change from one answer to the next. Seen in the look below: "100% full · 2,048 of 2,048 tokens", and after the next answer, the asked model's, "1% full · 129 of 8,192 tokens". And "start a new chat soon" is said of the window of whoever answered last, though another model of the chain, with more room, may go on;
- **the item comes when the models are read,** which for a stored conversation can be a moment after its answers are drawn: in a window too narrow for the list's one line, it can then take a second line, and what is under the list moves down by one;
- **"Try again" after a stand-in's full window may meet the same window again,** when the asked model still can't answer, and costs again;
- **an answer cut at a stand-in's own longest answer** (`length`) is said as the asked model's;
- **the usage ledger names the asked model,** whose price was paid;
- **who answered isn't said while the answer arrives,** only once its turn has ended.

#### Measured

On 2026-11-25, on this tree.

**On the real page,** the whole stack built from this tree, with a models file made for the look: its default model is the repository's fake, run on the machine itself, with a window of 8,192 tokens, and the local `qwen3.5:4b-q4_K_M` stands in for it, under the title "Qwen 3.5 4B, small window", with a window of 2,048 tokens and an answer of 2,000 at the longest. One guest, once in each of three engines (Playwright 1.65's Chromium, Firefox and WebKit), with made-up questions. What the page showed was the same in the three:

| What was set up, and asked | What the page showed, and the API's record of the turn |
|---|---|
| The fake not running. A short question | The local model's answer. Under it its rate, its first word and its credits, and last "answered by Qwen 3.5 4B, small window". Under the box "6% full · 126 of 2,048 tokens" (136 in Firefox, 125 in WebKit). The record: `model` the fake, `answered_by` the local model |
| Still not running. An essay of 2,500 words is asked for | 9,696 to 9,915 characters of it, with the same last item under them, and the notice "Another model answered in place of the one you asked, and the conversation has filled its context window: the answer stops here. Try again, or start a new chat to go on.", with "Try again". Under the box "100% full · 2,048 of 2,048 tokens · start a new chat soon". The record: `completed`, `context_window`, `answered_by` the local model, 1,901 to 1,909 credits |
| The fake started, its status given 16.5 s to be asked anew, and "Try again" | The fake's own answer in the essay's place, the question's echo, with nothing about who answered under it, and no notice. Under the box "1% full · 129 of 8,192 tokens" (135, 125). The record: `answered_by` the fake |
| A reload | The two answers, their lists and the line under the box as before. The lists read the same when the answers were first on the page and 2.5 s later |

No request of the page's was refused, and no script on it failed. The API's log for the first two turns of each run: in Chromium's, "model fake-first is unreachable, and is passed over", then "model small answered in place of fake-first"; in Firefox's and WebKit's, which began within the 15 seconds for which the fake's status of the run before was still reused, "model fake-first failed (ConnectionFailedError), and is asked once more", "… and small is asked next", and the same last line.

**The case that was seen before this revision,** on the same stack with the chain of the example: Groq's `openai/gpt-oss-120b` as the default, with `gemini-3.5-flash-lite` and then the local model in its `then`, and the decision's own made-up question of 39,893 characters, once, in Chromium. flash-lite's answer, and under it "first word after 1.2 s", "3,229 credits" and "answered by Gemini 3.5 Flash-Lite". Under the box "5% full · 12,849 of 250,000 tokens", where the decision's look had read "100% full · 12,857 of 8,000 tokens · start a new chat soon". The API's log: "model groq-large failed (ContextOverflowError), and gemini-lite is asked next", then "model gemini-lite answered in place of groq-large". It took one request that Groq refused, and one to flash-lite.

**The title job's line,** on the stack of the look above with the fake named for titles too, and not running: one question, and the worker's log said "conversation …: model fake-first is unreachable, and is passed over", then "conversation …: model small answered in place of fake-first". The conversation's title was the local model's, "Why needs bike chain oil".

**The end-to-end tests,** on their own stack: 79 passed, the new one in 0.7 s. The API's log of its turn: "model away is unreachable, and is passed over", then "model standard answered in place of away". From the API's container of that stack, a connection to the port of the fake's container where nothing listens was refused after 17.6 ms, and after 0.3 ms at the second and the third try.

**The page's test that waits for the models to be read again,** five seconds by the real clock (`components/chat/answered-by.test.tsx`), was run eight times beside eight processes that kept the machine's processors busy, at a load of 10: eight passed.

**Mistakes planted on purpose.** After the build and the review, 42 mistakes were planted one at a time in a scratch copy of the tree, 23 in the API and the end-to-end stack's files and 19 in the page, and the tests that should notice each were run: all 42 failed a test. Four of them were for what the review had found no test for, whose tests were written before the run: a turn that a shutdown interrupted, and one that failed inside Actio after a word, recorded without the model that had written them; the title job's line left out for a model that answered with nothing; and the end-to-end file's `away` given the window of the model that stands in for it.

### 2026-11-25: a ready model may be passed over for its room, and the listing gives the room

The decision asked every ready model of a chain, in order, and said of the listing that a model's `context_window` stays its own. It named "a check of capacity before asking" as to come. Its first half is [ADR 0029](0029-a-model-is-passed-over-for-a-request-too-large.md): a ready model whose room, its window or what its server takes in a minute, whichever is less, is smaller than a request by Actio's guess is passed over, as one that isn't ready is, while another ready model of the chain has room for it; and `GET /api/models` gives that room as `context_window`. The walk, and everything else decided here, is as it was.

### 2026-11-26: a model that keeps failing is asked after the others

"Known, and left" said that "a server's health is its status, asked at most every 15 seconds, until a circuit breaker says more". [ADR 0031](0031-a-model-that-keeps-failing-is-asked-last.md) is that breaker's first part: each try of a model is recorded in a breaker the process keeps for it, and a ready model that failed half or more of its last tries, five at the least within half an hour, is asked after the models of its chain that aren't failing, for 30 seconds to five minutes, and then tried in its place by one request. It is never passed over or refused for that. A model's status is as it was, and so is the walk, but that `hands_on`, its rule for what is handed on, is public, for the client that records the tries to read by.

### 2026-11-26: a server's longer wait is kept for the turns after

"Known, and left" in the revision "a model is asked once more, and a server says how long it may take to begin" said that a 429 asking for more than 2 s is not waited for. That is still so: the walk moves on to the next model at once, and asks once more only after a wait of `WAITS_AT_MOST_S` or less. What is new is that a longer wait is kept, for that model on its provider, until it is over, a day at the most, and the turns after it count the model as one with no budget left: asked after the others of its chain, or refused for now when none with room has budget ([ADR 0030](0030-the-providers-budgets-are-counted.md), revision "a provider's own wait is kept, and until it is over its model has no budget").
