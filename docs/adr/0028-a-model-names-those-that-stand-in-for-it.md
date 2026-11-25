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
