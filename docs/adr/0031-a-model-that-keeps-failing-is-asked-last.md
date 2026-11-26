# 0031. A model that keeps failing is asked after the others of its chain

- **Status:** accepted
- **Date:** 2026-11-26

## Context

A model of the models file may name others that stand in for it, asked in its place when it can't answer before its first word ([ADR 0028](0028-a-model-names-those-that-stand-in-for-it.md)). Each turn finds that out for itself: nothing remembers a model's failure from one turn to the next. A model whose server lists it, and so is `ready`, and which then fails every question, is asked first by every turn, and each turn pays for the failure before a stand-in is asked:

- **a 503 of the Gemini API's took 4.63, 5.52 and 4.91 s to come** (seen, 2026-11-26, [ADR 0030](0030-the-providers-budgets-are-counted.md), revision "a model too busy to answer fails nothing in `make preflight`");
- **a server that hangs holds a turn for its time to begin an answer:** 30 seconds for the example's two cloud providers, 90 by default (ADR 0028, revision of 2026-11-25).

ADR 0028 left it under "Known, and left": "a server's health is its status, asked at most every 15 seconds, until a circuit breaker says more". ADR 0030's revision of 2026-11-26 left "a provider's own 429 still closes nothing: that is for the circuit breaker". This is the breaker's first part: what it watches, when it says that a model is failing, and what that changes. A provider's own wait, what `GET /api/models` and the page say of a failing model, and the status check, come later.

What the choice had to fit:

- **A model's status is its server's answer about its list of models** (`Catalog.status`, reused for 15 seconds): a model too busy to answer is still listed, and `ready`.
- **The walk tells its caller only of a link that failed before anything was handed on,** once, after its last try (`on_failed`). It says nothing of a try that answered, nor of one that broke off after its first word.
- **A guess never refuses a turn** ([ADR 0029](0029-a-model-is-passed-over-for-a-request-too-large.md)), and a count refuses one only for now (ADR 0030, revision of 2026-11-26).
- **The shipped installation offers one model,** and an installation of one person asks a model every few minutes, not every few seconds.
- **The API's process and the worker's each build a catalog of their own;** `--role all` gives its worker the app's own.
- **A request too large, a request refused and a stop are whoever asked's to bring about,** not the model's failures.

## Options considered

**Where the state is kept:**

1. **In Valkey, as the budgets are.**
    - Con: a budget is the provider's count of one account, which must add up across processes. A breaker is what a process saw, cheap to learn again, in five calls. And the decision that guards against a dependency being away would itself depend on one, with a script and a round trip more for each turn.
2. **In each process, beside the gate.** Chosen.
    - Con: the API and a separate worker each learn on their own, and a restart forgets.

**What an open breaker does:**

1. **Its model is passed over, as one that isn't ready is, and the turn is answered 503 when none is left.**
    - Con: it opens at half of the calls failing, so the other half may answer; a local model that took longer than its server's time to load answers the next try; and the shipped installation has one model, which would then refuse every question for as long as its breaker is open, up to five minutes at a time.
2. **Refused for now when every model is failing, with the wait, as for budgets.**
    - Con: a budget's wait is a count against a stated figure; a breaker's is a guess, and ADR 0029 holds that a guess never refuses a turn.
3. **Its model is asked after those that aren't failing, and never refused.** Chosen.

**The window it counts over:**

1. **The plan's: the last 20 calls or 60 seconds, 5 at the least.**
    - Con: sized for traffic an installation of one person hasn't got (worked out, below).
2. **The last 20 calls of the last 30 minutes, 5 at the least, half or more of them failed.** Chosen.
3. **The same, 3 at the least.**
    - Con: little gained (below), on thinner evidence.

Worked out over made-up traffic by a throwaway outside the repository, not seen on a real server: questions arriving at random, 200 runs each, to a chain of the model watched and a stand-in that always answers.

| Who asks, and the model | The plan's 60 s, 5 calls | 30 minutes, 5 calls | 30 minutes, 3 calls |
|---|---|---|---|
| One person, every 45 s for an hour; it fails every time | never opened in 18 of 200 runs, otherwise after a median of 21 failed questions; 39.1 of 81.6 questions met the failure | at the 5th, in every run; 16.6 of 81.5 | at the 3rd; 14.8 of 81.4 |
| One person, every 5 minutes for two hours; it fails every time | never, in 200 of 200 | at the 5th, in every run; 16.3 of 24.9 | at the 3rd; 15.1 of 25.1 |
| One person, every 45 s for an hour; it fails 1 call in 5 | opened in 18 of 200 runs | opened in 37 of 200 runs, 0.2 times a run; 0.3 of 81.9 questions went to the stand-in first | 50 of 200 runs, 0.3 times a run |
| Ten people, every 4.5 s for an hour; it fails every time | at the 5th; 18.6 of 802.4 | the same | at the 3rd; 16.8 of 802.4 |

With 10 minutes in the place of 30, the one who asks every 5 minutes opened it after a median of 11 failed questions, and in 33 of 200 runs never.

**What a failure is:**

1. **Only what the walk moves on for, a failure before the first word.**
    - Con: on 2026-11-25 the Gemini API's flash-lite, asked to reason, was too busy for 11 of 17 answers, 8 of them in the middle of its thoughts ([ADR 0027](0027-cloud-models-through-their-openai-compatible-layers.md)): those would count as answers.
2. **Every `LLMError`.**
    - Con: a request too large or refused is the request's own, and whoever sends such requests could have a model asked last for everyone.
3. **The errors another try may not meet again (`FOR_NOW`), before the first word or after it, and an answer that ended without saying why. An answer cut at its time after words is an answer.** Chosen.

**What tries a model once its wait is over:**

1. **The status call, the list of models.**
    - Con: a model too busy to answer is still listed, which is the very case the breaker is for.
2. **A real request, asked of it in its place, one at a time.** Chosen.

**What a breaker is of:**

1. **A provider.**
    - Con: on 2026-11-26 `gemini-3.6-flash` answered 503 to eight of nine questions in 110 minutes, while flash-lite, on the same provider, answered its question in both runs of the example in that time (ADR 0030, the Measured of its revision "a model too busy to answer fails nothing in `make preflight`").
2. **A model on a provider, as a budget is.** Chosen.

**The order beside the budgets:**

1. **The failing models last of all.**
    - Con: a model whose budget Actio counted as spent would be asked before one with budget.
2. **Within each of the budget's groups.** Chosen.

## Decision

**Each process keeps a circuit breaker for each model on a provider, which every try of that model by a turn or a title feeds. A model that failed half or more of its last calls, five of them at the least within half an hour, is asked after the models of its chain that aren't failing, for 30 seconds. Then one request asks it in its place again: an answer closes the breaker, and a failure keeps the model last for longer, up to five minutes at a time. A breaker never refuses a turn or a title, and never keeps a model from being asked: it changes the order alone.**

**The breaker** (`actio_llm.breaker`, which knows nothing of the application, reads no clock, and logs nothing): a `Breaker` is one model's, told of each call when it begins (`begins`) and how it ended (`Call.ends`), each at the moment it is given. A call is one try of the model, one stream of its client: a link asked twice is two calls. It ends answered, failed, or with nothing to say. The numbers are constants of the module: `LAST_CALLS = 20`, `WITHIN = 30 minutes`, `AT_LEAST = 5`, and `WAITS` 30 seconds, then 1, 2, 4 and 5 minutes. The rules:

1. **Closed,** each call that ends answered or failed is kept with the moment it ended: the last 20, none that ended more than 30 minutes ago.
2. **It opens** when a call ends failed and, with it, 5 calls or more are kept and half or more of them failed (`failed * 2 >= calls`, in whole numbers). It is then open for the first wait, 30 seconds, and the calls kept are forgotten. An answer never opens it, whatever the share.
3. **Open, before its wait is over,** the model is failing. It may still be asked, as a last resort or as a chain's only model: a call that ends answered then closes the breaker, and one that ends failed changes nothing. The wait neither grows nor starts again.
4. **Open, once its wait is over,** the model is asked in its place again, one request at a time: from the moment a call begins at or after the wait's end until that call ends, the model is failing for everyone else, with no wait to name. The first call that ends failed at or after the wait's end opens it again, for the next wait, and 5 minutes from the fifth on. A call that ends with nothing to say changes nothing but that it is no longer under way: the next request asks the model in its place.
5. **An answer closes it,** from any open state. The calls kept start anew with that answer, and the next opening waits 30 seconds again.
6. **Calls end in any order.** One that began as a request that tries the model, and ends after the breaker closed or opened again, is no longer counted as trying it by then, and takes no other call off the count, which never goes below nothing. A call ends once.

**What a call ends as** (`asked_through` in `actio.catalog`): the client a turn's and a title's links ask a model through is one that records each try in the model's breaker, around the one `counted` gives, which counts its budget (ADR 0030). One function gives both, so that no caller has one without the other. A try begins when its `stream` is called, which the walk does once the model's gate is passed, and ends when that stream is over, however it ended, each by the clock handed down; it is recorded in a `finally` with nothing awaited in it, so that a stop or a cancellation records it too. It hands on every event, and every error, as it came, the same objects. It ends:

- **failed,** when the stream raised an error in `FOR_NOW`, before or after anything was handed on, but a `TookTooLongError` after something was handed on; and when the stream ended without its `Finish`, whether or not anything was handed on;
- **answered,** otherwise, when something of it was handed on, by the walk's own rule, now public (`actio_llm.hands_on`): it finished, it was closed before its end by a stop, a cut or a cancellation, or its answer was cut at its time;
- **with nothing to say,** otherwise: closed or cancelled before anything was handed on, an `LLMError` that isn't in `FOR_NOW` (a request refused or too large, a model the server hasn't got, a server too old, a feature it can't do), or any other exception.

What a call's end changed is said in the log, a warning, by the model's name in Actio and numbers alone:

- `model groq-large: 5 of its last 5 requests failed, and for 30 s it is asked after models that aren't failing`;
- `model groq-large: it failed again, and for 60 s more it is asked after models that aren't failing`;
- `model groq-large: it answered, and is asked in its place again`.

**Where a breaker lives:** `OfferedModel.breaker`, in the process, beside the gate. `catalog.build` gives one to each model on a provider: two models of the file that name the same provider and the same `model` share one, as they share a budget, and the same `model` on two providers has two. An `OfferedModel` made without one gets one of its own. Nothing of Valkey, `actio.core` or `Resources` changes.

**The order** (`Chain.failing`, `Chain.asked_with`, both pure):

- **`Chain.failing(now)`** says which ready models of the chain are failing at `now`, by name, each with what its breaker said (`Failing`: the wait left, or none while a request tries the model).
- **`Chain.asked_with(tokens, spent, failing)`** takes it as a third argument, required as `spent` is. The room first, and the budget's rule as it was, refusal included: a breaker never changes who is refused, a failing model with budget still keeps a refusal away, and a failing model without budget counts for the refusal's wait as before. Then, within each group the budget's rule made, those with budget and those without, the models that aren't failing first and the failing ones after them, each in the chain's order. When no ready model has room, every one is asked, in the chain's order, with the failing ones after those that aren't.
- **`Asked.failing`** names the models the breakers moved, in the order they are asked: each failing model asked after a model that the order without breakers had after it. A failing model left where it was is not named: a chain's only model, one of a chain whose every model asked is failing, one that was last already, and one that its budget alone put last, which `Asked.spent` names. So a lone model's log is as before.

**A turn** (`actio.api.turns.start_turn`) asks the breakers right after the look at the budgets, at the request's time, once: the order is decided when the turn starts, and nothing is asked of a breaker later in the walk. After the lines of the budgets, after the commit, one warning for each of `Asked.failing`, names and numbers alone:

- `turn …: model groq-large is failing, and for about 30 s more it is asked after models that aren't`;
- `turn …: model groq-large is failing, and it is asked after models that aren't while one request tries it`.

**The title job** (`actio.chat.titles._ask`) does the same, at its job's time, with `conversation …:`, after its lines of the budgets.

**Unchanged:** the 503 when no model of a chain is ready, the 422 for reasoning, which stays the first ready model's whoever is then asked last, the credits, each model's request, the record and `answered_by`; the walk's rules; `counted`; `Catalog.status`, the warm-up and a load ahead, none of which is recorded in a breaker; the routes, the API's description, what a turn streams, the tables, the page, the models file's keys and the settings. No migration.

**Tests:**

- **the breaker's** (`packages/llm/tests/test_breaker.py`): each rule by example, each number by its value (four failures open nothing and the fifth does, half opens it and a third never does, the tenth failure after twenty answers makes 10 of 20, a call 30 minutes old counts and one a second older doesn't, the waits 30, 60, 120, 240 and 300 seconds and 300 again); a failure before the wait is over that changes nothing, and an answer that closes; the one request that tries the model, with no wait to name, and what each of its endings does; a last resort from before the wait's end that fails after it; two calls under way that end in either order, and one that tried the model ending after it opened again; a call that ends twice; and a property test against a breaker written the plain way in the test, over made-up sequences of calls that begin and end in any order and of moments that move on by milliseconds or by most of an hour, which holds what always holds;
- **the catalog's** (`test_catalog.py`): the rule in each of its branches, with budgets and without, `Asked.failing` naming only what moved, a breaker that neither refuses nor keeps a refusal away, a failing model passed over for its room, and a property test over made-up chains, rooms, statuses, budgets and breakers: the same models asked, passed over, said without budget and refused as with no model failing, in the order of the rule; `Chain.failing` naming ready models alone; one breaker for two names of one model on one provider, through `build`; and the recording client: each way a try ends, closed or cut off by a stop, the stop and a cancellation passed on, the requests that are their senders' own, ten times running, that never have a model taken for failing, the same objects handed on with the counted client inside, a refused connection given back to the budget and counted as a failure, each line of the log whole without an error's text, and two tries through a walk, in the walk's own order of streams and holds;
- **a turn's** (`test_turns_breaker_api.py`): after five failed questions the stand-in asked first and the failing model sent nothing, with the log's lines whole and in order; the wait said as what is left of it; once it is over by the test's clock, the model asked in its place, failing and asked last for a minute, or answering and asked first from then on; another question while the one request tries it; the failing model answering as a last resort and back; a chain's only model failing ten turns running and asked at the eleventh, as before; a chain whose every model is failing, each asked in its order; the 422 for reasoning; requests too large ten times running, and turns stopped before and after the first word, never taken for failing; a model asked once more that fails twice; two names of one model; the four groups of the rule with budgets; the order kept while a turn waits at a gate; and a stop while the one request that tries a model writes;
- **a title's** (`test_titles.py`): a model that failed turns of the same catalog asked after its stand-in for a title, with the line by the conversation's id; and a title's own failures recorded.

## Consequences

- **Easier:**
    - a model that fails every question costs the first five, and then a question every 30 seconds to five minutes, instead of every question's wait for its failure;
    - a model that comes back is asked in its place at the next request after its wait, without a restart or a change of the file;
    - nothing more is refused than before, and a chain's only model is asked as it was.
- **Harder:**
    - **the order a turn asks its models in depends on what the process saw before it,** and two processes may ask in two orders;
    - **a question may go to a stand-in's server where before it went to its own model's,** on the strength of failures other people's questions met.
- **Known, and left** (each worked out, or read in the code, not seen):
    - **a provider's own 429 that names a wait still closes nothing by that wait:** it counts as one failed call. That is the next step;
    - **`GET /api/models` and the page say nothing of a failing model,** and the status check is as it was, the 2 seconds a hung stand-in's server costs a turn's start included. A later step;
    - **the order is decided when a turn starts:** a turn that was waiting at a gate when a breaker opened asks in the order it had;
    - **the API's and a separate worker's breakers are apart, and a restart forgets them;**
    - **a chain's only model, and a chain whose every model is failing, are asked as before:** nothing fails fast;
    - **fewer than five calls in thirty minutes never open one,** and the first five failed questions are paid for;
    - **a title cut at the job's own 30 seconds, before its model handed anything on, records no failure,** since its stream is cancelled: a local server given 90 seconds to begin is never recorded as failing by titles alone. Cut after a word or a thought, it is an answer, as a stop's is;
    - **a model that is slow on some requests alone can be put last, 30 seconds at first, by whoever sends such requests:** a long one that it is slow to begin on, or a hard one that a model which reasons without sending its thoughts stays silent over, past its server's time to begin an answer or the 20 seconds between two lines. Each ends as a failure with nothing handed on, and is its sender's to bring about. Not seen: the nearest was one short question that `gemini-3.6-flash` took 27.31 s to answer, of the example's 30 (ADR 0030);
    - **requests that start in the same moment as the one that tries a model are each asked of it in its place,** since each decided its order before that one began;
    - **a call that began as a last resort and ended failed after the wait was over counts as the one that tried the model;**
    - **a clock set back holds a breaker open for as long as it was set back,** since a wait is kept as the moment it is over;
    - **the numbers are constants of the code, not settings;**
    - **no end-to-end test opens a breaker:** the fake's `[[fail: …]]` fails its stand-in too, which is asked the same question.
- **How to reverse it:** `Asked.failing` and the third argument of `Chain.asked_with`, `Chain.failing`, `OfferedModel.breaker` and the breakers in `catalog.build`, `asked_through` and `_Recorded`, with `counted` back in the links of `actio.chat.answering.answer` and `actio.chat.titles._ask`, the lines in `actio.api.turns.start_turn` and `_ask`, `actio_llm.breaker`, and `hands_on` private again.

## Measured

On 2026-11-26, on the tree as it was committed. No cloud provider was asked anything for this step.

**On the real stack,** built from this tree, with models files made for the look. The model asked was the repository's fake, run on the same machine, which fails a question that tells it to (`[[fail: 503]]`); the other was the shipped local model, `qwen3.5:4b-q4_K_M` on Ollama. One guest, through the API, each question in a conversation of its own, and the fake's own log counted the requests it was sent. Each was seen once on this tree, and once before on the step's first build, alike.

**A chain: the fake, with the local model standing in for it.** Sixteen questions, of which the fake was sent twelve:

| When, UTC | What was asked | What came back, and what the API's log said |
|---|---|---|
| 12:27:02 to 12:27:09 | Five questions that tell the fake to fail | Each answered by the local model, the first after 4.33 s, which loaded it, the others after 0.76 to 1.01 s, each turn's `answered_by` "standard". At the fifth failure: "model fake: 5 of its last 5 requests failed, and for 30 s it is asked after models that aren't failing" |
| 12:27:10 | A plain question | The local model's answer, after 0.94 s: "turn …: model fake is failing, and for about 29 s more it is asked after models that aren't". The fake was sent nothing: 5 requests, as before |
| 12:27:41, the 30 seconds over | A plain question | The fake's answer, after 0.14 s, its 6th request: "model fake: it answered, and is asked in its place again" |
| 12:27:42 to 12:27:44 | A plain question, then three that tell the fake to fail | The fake's answer, after 0.04 s, then the local model's three times. At the third failure: "model fake: 3 of its last 5 requests failed, and for 30 s it is asked after models that aren't failing": the two answers and the three failures since it closed |
| 12:27:45 | A plain question | The local model's answer, after 0.73 s: "… for about 30 s more …". The fake was sent nothing: 10 requests, as before |
| 12:28:17, the 30 seconds over | A question that tells the fake to fail | The fake was asked, its 11th request, and failed: "model fake: it failed again, and for 60 s more it is asked after models that aren't failing". The local model answered, after 1.18 s |
| 12:28:18 and 12:28:45 | Two plain questions | The local model's answers, after 1.28 s each: "… for about 59 s more …", then "… for about 33 s more …". The fake was sent nothing |
| 12:29:23, the 60 seconds over | A plain question | The fake's answer, after 0.12 s, its 12th request: "model fake: it answered, and is asked in its place again" |

**A model alone: the fake, with nobody standing in for it.** Nine questions at 12:29:40, and nine requests to the fake:

1. seven that tell it to fail: seven failed turns, `model_unavailable`, each after 0.02 to 0.05 s, as before this step. The log said the opening at the fifth, "model fake: 5 of its last 5 requests failed, and for 30 s …", and the sixth and seventh were asked of it all the same. No turn's line named a breaker, and nothing was refused;
2. a plain one, inside those 30 seconds: the fake's answer, after 0.03 s, and "model fake: it answered, and is asked in its place again";
3. one the fake refuses as too large (`[[fail: 400 context_length_exceeded]]`): a failed turn, `context_overflow`, and no line of the breaker's.

**The end-to-end tests,** on their own stack: 80 passed, none of them new. Read in that stack's models file, and not in this run's log: its one chain begins with a model at an address nothing listens at, which its status says, so that it is passed over without being asked (ADR 0028), and no breaker hears of it.

**Mistakes planted on purpose,** each alone in a scratch copy of the tree, with the tests that should notice run. On the step's first build, while the review read it, 67: 62 failed a test. The five that failed none:

- **an opening that kept the calls it had kept:** nothing reads them while a breaker is open, and the answer that closes it starts them anew. The line that cleared them could not be missed, and came out;
- **a wait said as 0 seconds:** no wait a breaker gives is 0, so "1 at the least" shows only for a `Failing` made by hand, which the test now makes;
- **the opening's line with its two numbers the wrong way round:** every test opened a breaker with five failures of five. The test of the log's lines now opens one with three of five, as the look above did;
- **the opening's line saying 30 seconds whatever the wait:** an opening's wait is always the first, so this is no mistake where it was run;
- **a turn asking the breakers by its clock, not by the request's time:** the same moment in every test, and milliseconds apart on a server. No mistake either.

On the tree as it was committed, 68, with one more test's two lines among them: 66 failed a test, and the two that failed none are the last two above.

**The review,** by a reviewer that had not seen the build, once: one fault that had to be fixed, in a document: the revision this step adds to ADR 0030 stood inside the revision before it, above that one's own measurements, and was moved to the file's end. Of its eight smaller points, six changed sentences, here and in the pages: that a title cut after a word is an answer, what a call that tried a model leaves behind when it ends late, a figure of `gemini-3.6-flash` that the step's brief had as seven of eight where ADR 0030 records eight of nine, and, under "Known, and left", a hard question beside a long one. One added a test, of the title job's line while one request tries its model. One was left: `Opened`, `OpenedAgain` and `Closed` are imported from the breaker's module and not from the package's root, where two of them would stand beside a turn's steps of the same names.

**Not measured:** a cloud provider's model failing through a breaker; the waits from two minutes on, which the tests hold by their clock; the API's process and a separate worker's each with a breaker of its own, which is read in the code; and the page, whose source did not change.

## Revisions

### 2026-11-26: a provider's own wait is kept

"Known, and left" said that a provider's own 429 that names a wait closes nothing by that wait, and counts as one failed call. Now a wait longer than the walk's 2 s is kept beside the model's breaker, in the process, until it is over, and meanwhile the model counts as one with no budget left, by the budget's rule: asked after the models with budget, or refused for now ([ADR 0030](0030-the-providers-budgets-are-counted.md), revision "a provider's own wait is kept, and until it is over its model has no budget"). The breaker is as it was: that 429 is still a failed call, and the client that records each try notes the wait in the same place, after the breaker's line.

### 2026-11-26: a status that is known isn't waited for

"Known, and left" said that the status check was as it was, the 2 seconds a hung stand-in's server costs a turn's start included. Now a turn's start uses a status that is known without waiting, for up to five minutes, while its server is asked again behind the request, and nothing is refused by a status older than 15 seconds: a hung stand-in's server holds a turn's start only when no model of its chain is known to be ready, or when nothing younger than five minutes is known of the stand-in itself. The list of models, the warm-up and the title job wait for it as before ([ADR 0028](0028-a-model-names-those-that-stand-in-for-it.md), revision "a status that is known isn't waited for"). The breaker is as it was, and nothing it says changes a status.
