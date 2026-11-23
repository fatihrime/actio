# 0026. A model is loaded ahead of the first question

- **Status:** accepted
- **Date:** 2026-11-23

## Context

A local model's server loads a model into memory at the first request for it, and lets it go a while after the last one. Ollama keeps it five minutes: the adapter sends `keep_alive` `5m` with every request ([ADR 0007](0007-local-models-through-ollamas-native-api.md)). Nothing in Actio loaded a model ahead, so a question asked after a pause of five minutes waited for the load before its first word.

Measured on 2026-11-23 on the maintainer's machine, a MacBook Pro (M1 Pro, 16 GB), with Ollama 0.40.16 and the default model, `qwen3.5:4b-q4_K_M`, in a real browser, the model unloaded as the page opened: the first word of the first answer came 2.1 to 4.1 s after the question was asked, by the server's clock, and 1.8 to 3.7 s of that was the load (`load_ms`). The figures are under [Measured](#measured).

What Ollama 0.40.16 does, measured the same day:

1. **A chat request with no messages loads the model,** and answers one object: `{"model": …, "created_at": …, "message": {"role": "assistant", "content": ""}, "done": true, "done_reason": "load"}`. With `"stream": true` it is that one line. Ollama's FAQ documents this much: "you can preload a model by sending the Ollama server an empty request. This works with both the /api/generate and /api/chat API endpoints". The shape of the answer, and everything below, is documented nowhere.
2. **A load that leaves out the chat's `"shift": false`, or names another window, makes the server load the model again at the next question.** A turn's request sends `"shift": false`, and its window as `options.num_ctx`.
    - A load without `"shift": false` was followed by an answer whose load took 1,354 to 3,375 ms: its first word came after 1.5 to 3.5 s. Such a load sent between two answers of a model that was loaded took 1.1 s itself, and cost the next answer 1.5 s, where it had taken 0.05 s.
    - A load with the flag and without the window was given Ollama's default window, 4,096 tokens (`/api/ps`). The answer after it, asked with 8,192, loaded the model again, for 1,614 ms.
    - `"truncate": false` alone made no difference, and neither did `think` or `num_predict`.
    - With `"shift": false`, `"truncate": false` and `num_ctx` as a chat sends them, the answer after the load took 0 to 4 ms to load, and its first word came after 0.12 to 0.18 s. On `granite4:micro`, the live tests' model: 6 to 13 ms, against 1,865 ms without the flag.
    - The MLX build, `qwen3.5:4b`, loads the same way: 2.6 s, then 1 ms for the answer.
3. **A load of a model that is loaded already is answered in 1 to 3 ms,** also while that model is writing an answer, which goes on undisturbed. It moves the moment the model is let go (`expires_at` in `/api/ps`).
4. **A load of another model, asked while one is writing an answer, waits for that answer to end, and then has its model unloaded.** Asked 3 s into an answer of 12.6 s, a load of `granite4:micro` was answered after 11.5 s. The answer was as long and took as long as one written alone. Afterwards only `granite4:micro` was in memory, though the two are 5.4 GB together on a machine with 16, and the next answer of the first model loaded it again, for 2,424 ms.
5. **A question asked 1.0 s into a load of its own model waits inside Ollama for that load,** and starts when it is over: its first word came 1.17 and 1.18 s after the load began, and its own load took 50 ms. No second load, and no error. A second load, asked 0.4 s into the first, ended with the first, and the answer after both took 1 to 3 ms to load.
6. **A load whose connection is closed after 0.5 s is abandoned:** nothing was in `/api/ps` 1, 3, 6 and 10 s later, and the next answer loaded the model from the start.
7. **A cold load took 0.8 to 3.1 s** here, with the model's file in the system's cache. A load of a model the server hasn't got is answered 404, `{"error":"model '…' not found"}`, in 3 ms. Asked during a load, what the model can do (`/api/show`) was answered in 2 to 5 ms.

What else the choice had to fit:

- **A model's server writes only so many answers at once,** and a turn waits at a gate in front of it, in the API's process ([ADR 0023](0023-answers-that-wait-their-turn.md)).
- **The catalog gives the small calls to a server 3 seconds** (`actio.catalog.TIMEOUTS`), and a turn 90 for its first word. A cold load can take longer than the first.
- **A route that anyone with a session can call counts its calls** ([ADR 0011](0011-rate-limits-as-token-buckets-in-valkey.md)).
- **What arrives with the session fills places that were there before it,** and nothing on the page moves when it comes ([ADR 0015](0015-the-web-chat-calls-the-api-from-the-browser.md), revision of 2026-11-21).
- **OpenAI's API has no call to load a model,** so a server that speaks only that can't be asked to.

## Options considered

**Where the load is asked from:**

1. **As a side effect of `GET /api/models`, or of starting a session.**
    - Con: a read that sets another server to work. The page reads the models every five seconds while the one that answers can't, and `make smoke` reads them too.
    - Con: it couldn't be limited apart from what it rides on.
2. **The worker, on a schedule, keeping the model loaded always.**
    - Con: the model takes its gigabytes for as long as Actio runs, whether anyone is there or not, on a laptop that also runs a browser and Docker.
3. **A route of its own, which the page calls.**
    - Pro: a model is loaded only while someone has the page open, and the page says when.
    - Pro: limited per account, as messages are.
    - Con: one more request at the start of every page, and one more thing anyone with a session can ask a model's server to do.
    - Con: whoever uses the API without the page has to ask for it too.

**Whether the route waits for the load:**

1. **It answers once the model is loaded.**
    - Con: the request holds one of the six connections a browser opens to one address over HTTP/1.1, for seconds, and holds up a shutdown.
    - Con: a load that is hung up on is abandoned (fact 6). A page that went away would take its load with it, and the load can't run apart from the request.
2. **It answers at once, and the load runs in a task of the API's own.**
    - Pro: nothing on the page waits for it.
    - Con: the page doesn't learn what came of it, which it has no use for.
    - Con: tasks to keep, and to cancel at a shutdown.

**What the load request is:**

1. **A bare request with no messages,** as Ollama's FAQ shows it.
    - Con: fact 2. The next question loads the model again, so a load ahead costs more than none: 1.1 s for itself, and 1.5 s more for an answer of a model that was loaded.
2. **A chat request with no messages, made by the same code as an answer's.**
    - Pro: what decides whether the server loads again can't drift apart between the two.
    - Con: the load carries members that mean nothing to it, such as the answer's token limit, and rests on what a chat happens to send.

**The gate:**

1. **A load waits there, as an answer does.**
    - Con: behind a long answer it waits as long as the answer takes. For the answer's own model that is a wait for nothing: the model is in memory. For another model on the same server, Ollama then unloaded the answer's model (fact 4).
2. **A load takes a place there while it runs.**
    - Con: a question to the same model, asked meanwhile, waits at the gate for the load, which Ollama does by itself (fact 5), and with one place it waits behind the load's request as well.
3. **A load takes no place, and none is asked while the gate is full.**
    - Pro: a question to the same model asked during a load is let through, and waits inside the server for that load (fact 5).
    - Pro: while the server writes as many answers as it takes, the model that writes is in memory already (fact 3), and a load of another one would wait for the answer and then have its model unloaded (fact 4).
    - Con: only this process knows its gate and its loads: a second API process, or the worker in its own container, doesn't.
    - Con: a question to another model on the same server, asked during a load, wasn't measured.

**When the page asks:**

1. **Once it knows the model that answers:** a question asked within seconds of opening the page finds the model loaded, or loading.
    - Con: every visit loads the model, also one that asks nothing.
2. **When something is written into the box:** someone is about to ask. That covers a page that was left open for longer than five minutes.
    - Con: a question that is pasted and sent at once gains nothing.
3. **On a timer, while the page is open.**
    - Con: the model stays loaded for as long as a tab is open somewhere, used or not.

## Decision

**The page asks the API to have the model that answers loaded: once it knows that model and the model is ready, and when something is written into the box, at most once a minute for one session and model. The API answers at once, and has the model's server load it with a chat request that has no messages, made as a chat's is. Never while the server writes as many answers as it takes, and with no place at the gate.**

**The model client** (`actio_llm`):

- **`Ollama.load(model, context_window=…)`** sends the chat request that `stream` would send for that model and window, with `"messages": []`: one function makes both (`_body`). It keeps the adapter's `keep_alive`, and goes through the check of the server's version, as a chat does.
- **It returns once the server says that the model is loaded:** the line with `"done": true`. It waits for that as long as a chat waits for its first line (`Timeouts.first_event_s`, 90 s), not as long as a small call (`request_s`, 3 s in the catalog). A model the server hasn't got raises `ModelNotFoundError`, and the rest of the adapter's typed errors are raised as for a chat.
- **`LoadsAhead`,** a protocol that can be checked at run time, says that a provider can do this: `isinstance(provider, LoadsAhead)`. The OpenAI-compatible adapter can't. A test's scripted model can, with a `load` of its own.

**The catalog** (`actio.catalog`):

- **`Catalog.load_ahead(name)` starts a load, and returns at once.** It says whether a load was started or is under way already (true), or there is nothing to do (false): the model's server can't load ahead, or its gate is full.
- **One load of a model at a time.** Asked again while one is under way, it starts nothing, and says that one is under way, also when the gate has filled meanwhile. Once that one has ended, well or not, the next ask starts another: a model that is loaded already is answered in milliseconds, and the time it is kept starts again.
- **A load takes no place at the gate,** and never waits there. How the gate treats answers and titles didn't change.
- **It asks with the model's context window,** as a turn does.
- **Nobody waits for a load.** One that fails is one warning in the log, with the model's name in Actio and the kind of error, never its text, which is the server's: `model standard: it wasn't loaded ahead (ProviderUnavailableError)`. An error of Actio's own is logged with its trace. A cancellation gets out as it came.
- **No load is left pending.** `Catalog.stop_loading` cancels the loads under way, and waits until they have ended. The catalog that `build` makes does so before the providers' clients are closed, and the app does so at its shutdown, also for a catalog it was handed, as tests hand it one.
- **The worker never loads ahead.**

**The route** (`actio.api.catalog`): `POST /api/models/{model_id}/warm-up`, for anyone with a session, a guest too, with no body.

- **In this order:** the model by its name, or 404; the limit per account; the model's status as a turn checks it, and a model that isn't `ready` is refused as a question to it is, with 503 and the same words and members (`model`, `model_status`), from one definition (`not_ready`); then the load.
- **It answers at once, with nothing:** 202 when a load was started or is under way, 204 when there is nothing to load.
- **The limit:** `ACTIO_LIMIT_WARM_UPS_PER_USER`, `120/hour` by default, per account as `ACTIO_LIMIT_TURNS_PER_USER` is: 120 at once, then one every 30 seconds. The page asks once a minute at most in each tab. Refused, the words are "Too many requests to load a model.", with how long to wait. The end-to-end stack keeps the default.
- **It costs nothing:** it touches no table, and the usage limits never hear of it.

**The page** (`apps/web`):

- **One module asks,** `lib/chat/warm-up.ts`, through `warmUpModel` in `lib/api/client.ts`.
- **At most once every 60 seconds for one session and model** (`WARM_UP_EVERY_MS`), counted from the ask, whatever came of it, by `Date.now()`. When it last asked is kept in the module, under the session's `holder` and the model's id: React's StrictMode runs an effect twice and asks once, and a new session asks anew.
- **Only for the model that answers,** the one that stands in at a usage limit included, and only while its status is `ready`.
- **At two moments:** when the page knows that model, so also when a model that wasn't ready becomes ready, and when another model comes to answer; and when something is written into the box, where the code that draws markdown is asked for too. A question that takes less than about four minutes to write finds its model loaded: its first letter asked for the load, unless one was asked for less than a minute before.
- **It says nothing.** Nothing new is shown, nothing waits for it, and nothing on the page moves. A refusal, or a request that fails, shows nothing and writes nothing to the console. A question asked while its model loads says "Waiting for the model…", as before.

**Tests:**

- **The model client:** a recorded exchange of a load, and of a model the server hasn't got, which fail when the request changes; a load's request equal to a chat's for the same model and window in every member but `messages`, so that a flag left out of the load alone fails a test; a server that answers a load later than a small call may take and sooner than a first word must, over a real connection; a load the server never says it made; a server too old. Live (`make test-live`): with the model unloaded first, the question after a load takes under 500 ms to load.
- **The API:** the load asked through the Ollama adapter, with the model's window; nothing for a server that speaks OpenAI's API, which is asked nothing more; nothing while the gate is full, and a load under way said as such also then; a question asked during a load asked at once; one load at a time, and another once it has ended; a load that fails, in the log by its kind alone, and one that fails in Actio's own code, with its trace; nothing held, recorded or charged, and nothing taken from what the account may ask in messages; a load under way cancelled when the app shuts down, and before the providers' clients close, each with a time limit, so that an app that waited for the load fails the test; 401, 404 before anything is counted, 503 as a turn's, and 429 with `Retry-After`.
- **The page:** asked once when the model is ready, also in StrictMode; not while it isn't, and once when it becomes ready; not again for what is written within the minute, and again after it, or at once after the clock was set back; for the model that stands in at a usage limit; anew in a new session; a load that was started, a refusal, a model that can't answer, one nobody offers, and a request that fails, after each of which a question is answered with nothing said of it, on the page or in the console.
- **End to end** (`e2e/warm-up.spec.ts`): a guest's page asks, and is answered 204 through the proxy, and so is the page loaded again. The stack's model is on a server that has nothing to load.

## Measured

On the machine of the [Context](#context), 2026-11-23, on the stack of `make up`, with the default model, before this step and after it. A guest in Chromium, driven with real keys; before each visit the model was unloaded at Ollama. The figure is the turn's own record: from the moment the question was asked to its first word, by the server's clock, which is what the page shows as "first word after". In brackets, how much of it the model's server said was the load.

| A visit | Before, two visits | After, four visits |
|---|---|---|
| The page is looked at for six seconds, then the question is written and sent: Enter 7.4 s after the page was asked for | 4.05 s (3,651 ms), 2.92 s (2,598 ms) | 0.42 s (3 ms), 0.36 s (1 ms), 0.39 s (7 ms), 0.45 s (3 ms) |
| The question is written at once: Enter 1.3 to 1.4 s after the page was asked for | 2.63 s (2,341 ms), 2.14 s (1,831 ms) | 1.37 s (1,107 ms), 0.35 s (53 ms), 0.29 s (0 ms), 0.37 s (54 ms) |
| Five minutes pass, then the first letter, four seconds of thought, the rest, and Enter. The page's clock was moved on, and the model unloaded at Ollama, as Ollama does by itself after that time | 2.92 s (2,608 ms), 2.89 s (2,594 ms) | 0.33 s (1 ms), 0.34 s (1 ms), 0.33 s (1 ms), 0.45 s (4 ms) |

- **From Enter to the first word on the page,** by the page's own clock: 2.19 to 4.22 s before. After, 0.37 to 0.55 s in eleven visits of twelve, and 1.40 s in the one below.
- **In one visit of the four in which the question was written at once, the load was still under way at Enter,** and the question waited for the rest of it: 1.37 s, where a question to a model that wasn't loaded had taken 2.14 and 2.63 s. In the other three the load had ended by then.
- **The page's request was answered 202,** 0.1 to 0.2 s after the page was asked for. In the third kind of visit a second one, at the first letter, was answered 202 too. No visit left an error on the page or in its console.
- **What is left:** through Actio the first word of an answer of a loaded model came after 0.29 to 0.45 s. Asked of Ollama itself, right after a load, it came after 0.12 to 0.18 s (fact 2). What the difference is made of wasn't measured here.

**Forty-four mistakes were planted,** one at a time, in a copy of the tree, to see the tests fail:

| The mistake | Tests that failed |
|---|---|
| The load is a bare request: the model, no messages, the window | 3 |
| The load leaves the chat's `shift` flag out | 3 |
| The load is sent without the window | 4 |
| The load is waited for only as long as a small call | 1 |
| A load is sent to a server too old, unchecked | 1 |
| A load the server never confirmed counts as made | 1 |
| The load keeps the model for Ollama's default time, not the adapter's | 1 |
| A load is asked for while the server writes as many answers as it takes | 1 |
| A second load of a model is started while one is under way | 2 |
| A model that was loaded once is never asked for again | 2 |
| A load takes a place at the gate while it runs | 2 |
| A load under way is said to be nothing once the gate has filled | 1 |
| A load is asked for without the model's window | 4 |
| A load is asked for by the model's name in Actio, not its server's | 4 |
| A provider that can't load ahead is asked to | 4 |
| A failed load's line in the log carries the error's text | 1 |
| A model server's error is logged as a mistake of Actio's, with its trace | 1 |
| A mistake in Actio's own code during a load is said without its trace | 1 |
| The loads under way are waited for at closing, and not cancelled | 2 |
| The catalog closes its providers' clients with a load still under way | 1 |
| The app leaves the loads of a catalog it was handed pending | 1 |
| The route says 202 when there was nothing to load | 5 |
| The route waits for the load | 4 |
| Loads aren't counted against the account | 2 |
| Loads are counted at the messages' rate | 1 |
| Loads are counted in the messages' own bucket | 1 |
| A model that can't answer is asked to be loaded all the same | 1 |
| The refusal of a model that can't answer leaves out why | 3 |
| A model nobody offers is counted against the account before it is refused | 1 |
| The setting's default is a tenth of what was decided | 2 |
| The setting can't be given to the container stack | 3 |
| The page asks every time, with no minute between | 4 |
| When it last asked is kept for the model alone, whoever has the session | 1 |
| When it last asked is kept for the session alone, whichever model answers | 1 |
| A model that can't answer is asked for | 1 |
| A request that fails is nobody's to catch | The run failed: errors nobody caught |
| The minute counts only from an ask that went well | 1 |
| Nothing asks when the page knows its model | 13, and the end-to-end test, on a stack built from the copy |
| Nothing asks when something is written | 3 |
| The page asks for the default model, also when another answers | 1 |
| Ten minutes between two asks | 3 |
| Six seconds between two asks | 1 |
| A clock set back keeps the page from asking until it has caught up | 1 |
| A failure is said in the console | 6 |

**Three of them failed nothing at first,** and each got a test, or a time limit:

- **The loads under way waited for at closing, and not cancelled.** The two tests of it never ended: an app, or a catalog, that waits for a load that doesn't end never closes. Each has a time limit now, and fails.
- **A model nobody offers, counted before it is refused.** No test asked for one more than once. Now three such requests, of an account allowed one an hour, leave that one.
- **A clock set back.** The page would have asked again only once the clock had caught up with the last time it asked. It asks at once, and a test sets the clock back two minutes.

**Two tests were written while the mistakes were listed,** before any was planted, because none of the tests would have noticed:

- **A load under way said to be nothing, once the gate had filled.** As first built, the gate was looked at before the loads: a second tab that asked while a question waited inside the server for the first tab's load was answered 204. The review found it by reading. The loads are looked at first now, and a test holds a load and an answer at once.
- **Loads counted in the messages' own bucket.** No test asked both of one account. Now an account that has used up its loads is still answered.

## Consequences

- **Easier:**
    - a question asked after a pause doesn't wait for its model to be loaded, when the page was opened or something was written a moment before;
    - the rule that a load is a chat's request is in one place, so a flag a later chat needs reaches the load too.
- **Harder:**
    - **every visit loads the model into memory for five minutes,** also one that asks nothing. And while someone writes, the model stays loaded: each minute of writing asks again, which moves the time it is kept;
    - **the page doesn't say that a model is loading:** a question asked then shows the wait for the model, as before;
    - **only the process that started a load knows of it.** A second API process would start a load of its own, and the worker in its own container asks for a title without knowing of one. With one model on the server, nothing is lost by that: a question, and a second load, wait inside the server for the load under way, and a load of a model that is loaded is answered in milliseconds (facts 3 and 5);
    - **with several models on one server, loads push each other's models out.** Ollama unloaded the model that had just answered when another was loaded, on a machine with room for both (fact 4). Anyone with a session, a guest too, can ask for any model on offer to be loaded, 120 times at once and then twice a minute, for no credits. A question to that model would do the same, and costs credits unless the model is one that costs nothing. With one model on the server, as shipped, there is nothing to push out. Where it matters, `ACTIO_LIMIT_WARM_UPS_PER_USER` is the limit to lower;
    - **a server that speaks OpenAI's API gets nothing,** and the page's request is answered 204;
    - **the end-to-end stack shows the route, and no load:** its model is the fake, on such a server;
    - **one more request at the start of every page,** and one a minute at most while someone writes;
    - **Ollama documents no more than that an empty request preloads.** That this request is answered as it is, and that the next question doesn't load the model again, rests on what was measured. The recorded exchange, the test that holds the load's request to a chat's, and the live test guard it.
- **How to reverse it:**
    - **the page:** the two calls to `warmUp`, in `components/chat/chat-app.tsx` and `components/chat/chat.tsx`, and the module;
    - **the API:** the route, `Catalog.load_ahead` and `stop_loading`, and the setting;
    - **the model client:** `Ollama.load` and `LoadsAhead`.
