# 0030. The providers' budgets are counted, and a model without budget for a request is passed over

- **Status:** accepted
- **Date:** 2026-11-25

## Context

A cloud provider's plan takes only so many requests and tokens from an account in a minute and in a day, for each model. Until now Actio learned that a model's share was spent only by asking it: the provider refused, with 429, and the next model of the chain was asked ([ADR 0028](0028-a-model-names-those-that-stand-in-for-it.md)). [ADR 0029](0029-a-model-is-passed-over-for-a-request-too-large.md) was the first half of a check of capacity, whether one request can fit a model at all, and left the second for later: "nothing is counted over a minute or a day yet". This is that half.

What is known of the two providers, each fact with how it was got (see Measured):

- **An answer's tokens count against Groq's minute, after the answer** (seen, once): 0.3 s after an answer it counted as 1,500 tokens had ended, the next request's headers said 6,587 of the minute's 8,000 tokens left, where by the first request's own headers the minute would have been full again before the answer was over. So 1,413 were gone, that next request's own share at the door among them. How Groq puts its estimate at the door and its count afterwards together was not seen.
- **At the door, Groq takes its own estimate of the prompt alone** (seen), not the room asked for the answer: 4,057 for a prompt it then counted as 3,932, and 192 for one it counted as 91.
- **A request Groq refused took nothing** (seen, twice). After one whose prompt it said was partly cached, nothing had come back to the minute 14 s later (seen, once).
- **Groq's limits refill steadily** (seen): one request of its 1,000 a day every 86.4 s, 133 tokens of its 8,000 a minute every second. Its headers state those two limits and no other (seen: each `x-ratelimit` header of that day's three answers was of one of the two). Its documentation adds 30 requests a minute and 200,000 tokens a day for each of the two models of the example, and says that limits are an organization's, by model.
- **The Gemini API states no limit in its answers.** Its documentation, and one free account's page, give `gemini-3.6-flash` 5 requests a minute, 250,000 input tokens a minute and 20 requests a day, and `gemini-3.5-flash-lite` 15, 250,000 and 500. Its limits are a project's, by model, and its day starts over at midnight Pacific time. A request it refused, or answered with 503, still counted against its minute (seen, ADR 0028's look).
- **Actio's guess at a prompt** (`actio_llm.prompt_tokens`) came to 1.10 to 1.12 of Groq's count, and 0.90 and 0.98 of flash-lite's, on made-up English notes (seen).

What the choice had to fit:

- **Several processes ask the same models:** the API's turns, and the worker's titles ([ADR 0025](0025-titles-written-by-a-model.md)), and possibly several API processes. What they send to one model adds up at its provider.
- **Valkey is already where Actio counts what has to hold across processes:** the rate limits ([ADR 0011](0011-rate-limits-as-token-buckets-in-valkey.md)) and the usage limits ([ADR 0020](0020-usage-limits-on-turns.md)). It may be away, and an answer must not stop for that: nothing of this is the reader's to pay for.
- **No turn is refused for a guess** (ADR 0029). The refusal of a turn "for now", when no model of its chain has budget, is a step of its own, with what the page then says: this one counts, and chooses who is asked.

## Options considered

**Where to count:**

1. **The rate limiter's script** (`rate_limit.lua`).
    - Con: it takes one token at a time; it keeps whole milliseconds a token, which a bucket of 225,000 tokens a minute, 3,750 a second, can't be; and it can't look without taking: an attempt that fits is taken, and nothing taken can be put right afterwards.
2. **The usage-limits engine** (`packages/quota`).
    - Con: its windows are an account's, with credits held and settled, and they start over at times of their own. A provider's limit is the installation's, by model, and refills steadily.
3. **A script of its own, beside the rate limiter's** (`actio.core.budget`). Chosen.

**How to count:**

1. **A counter for each clock minute and each clock day.**
    - Pro: two commands.
    - Con: twice a limit fits around a window's edge. And Groq's limits were not seen to be such windows: they refill steadily.
2. **A bucket that refills steadily,** stored as the moment it is full again, as the rate limiter's is. Chosen.

**When a request is taken:**

1. **At the door, for the whole chain,** with the credits.
    - Con: mostly one model of a chain is asked. The others would be charged for nothing, and given it back later.
2. **When a model is sent the request,** by the client it is asked through, and put right once the stream is over. Chosen: each try is a request, and a stand-in that answers is counted as itself.

**What happens when no model of the chain has budget:**

1. **Refuse the turn, for now.**
    - Con: that is the next step, with what the page says of it.
2. **Ask every one, as before this step.** Chosen, for now.

## Decision

**Actio counts in Valkey what it sends to each model whose provider's limits the models file states: its requests, and their tokens, in buckets that refill steadily. Before a chain is asked, one look says which ready models have nothing left for the request, and those are passed over while another model of the chain has budget. When none has, every one is asked, as before: this never refuses a turn or a title, and Valkey being away never stops an answer.**

**The file** (`config/models.toml`, `actio.catalog`):

- **Three keys of a model,** beside `tokens_a_minute`: `requests_a_minute`, `requests_a_day` and `tokens_a_day`. Each as `tokens_a_minute` is: optional, for either kind of provider, a whole number of 1 or more. A string, a fraction, `30.0`, `true`, 0 and a negative number are each a mistake in one line that names `models.<name>.<key>`. Each is the provider's own figure, not a share of it, and any of the four may be there without the others.
- **Two models of the file that name the same provider and the same model on it share one budget,** so they state the same four figures, a key left out being one. A difference is a mistake of the file, in one line that names both models and the key: "models.again: `requests_a_day` is left out here and 1000 in models.big, the same model on the same provider: the two share one budget, and state the same figures of it". `tokens_a_minute` is held to that too from now on.
- **`tokens_a_minute` keeps all it meant:** the room of ADR 0029 is still the whole figure.

**What is kept:** **90 % of each figure, rounded down, and 1 at the least** (`KEPT_PERCENT`, `kept`): 30 is 27, 1,000 is 900, 8,000 is 7,200, 200,000 is 180,000, 5 is 4, 20 is 18, 15 is 13, 500 is 450, 250,000 is 225,000, 1 is 1. The tenth left is for what Actio can't count: its guess at a prompt may be under the provider's count, and its clock isn't the provider's.

**The counters** (`actio.core.budget`, its script `budget.lua`):

- **A bucket** (`Bucket`) is a limit of a provider's model: what it counts, requests or tokens, its period, a minute or a day, the provider's name, the model's name on its server, and its size, what is kept. It is stored as one number, the moment it will be full again, in milliseconds, and its key expires at that moment: a full bucket takes no key.
- **An amount moves that moment by `amount * period / size` milliseconds, rounded down, whether it is taken or given back.** Rounded up, a bucket of 27 a minute gave 26 at once (see Measured). An amount given back is rounded as the same amount taken is, towards nothing, so that a take given back leaves its bucket as it was, unless the take was cut at empty (see "Known, and left"). A bucket goes no lower than empty, the moment a period from now, and no higher than full. A stored moment further away than an empty bucket's is taken for empty, as the rate limiter does.
- **Two operations, in one script, atomic across processes** (`Budgets`):
    - **`look`:** for each bucket given with an amount, how long until that amount fits, no time at all when it fits now. An amount larger than the bucket's size fits a full bucket. It writes nothing, not even a correction;
    - **`add`:** takes an amount from each bucket given, or gives one back when it is negative, without refusing.
- **Time is an argument,** as in the rate limiter. Nothing in it reads a clock.
- **Keys** are the prefix, the limit, the provider's name and the model's name on its server, last, since it may hold any character: `actio:b:tokens-a-minute:groq:openai/gpt-oss-20b`. Readable, not hashed: these are names from the models file, never what people typed.
- **Built once in `open_resources`,** as `Resources.budgets`, under `{ACTIO_VALKEY_PREFIX}:b`: the API's turns and the worker's titles count in the same buckets.
- **Valkey that doesn't answer** raises one error of the module's own, `BudgetsUnavailableError`.
- **Tested against the plain definition,** as the rate limiter is: a second implementation, written the way a token bucket is usually described, a level and when it was read, counted exactly in fractions, plays the same random sequences of looks and adds, with buckets of 27 and 13 requests a minute, 7,200 and 225,000 tokens a minute, 900 requests and 180,000 tokens a day. The script's waits may differ from it by less than a millisecond for each add since the bucket was last full for longer than that, and less than one more for the look's own rounding, and are held to that (`PlainBuckets` in `apps/api/tests/test_budget.py`). A key expires in real time while the test's clock is moved by hand, so the clock moves on by ten seconds at least after an add: a key that an add left a moment from full may be gone by the next step, and the script then finds that bucket full, as the plain one is by then.

**A model's budget** (`OfferedModel.budget`, `Budget`, `budget_of`): the buckets of the limits its table states, each at its kept size, keyed by its provider's name and its `model`. `None` when it states none.

**What a request takes** is written once, `counted` in `actio.catalog`: the client a model is asked through, the model's own when it has no budget, and otherwise one around it. `OfferedModel.chat` stays the bare client: whether a server has a model, and a load ahead, are asked of it as before, and aren't counted. The client:

1. **When its `stream` is called,** which the walk does once the model's gate is passed, and once for each try, takes 1 from each bucket of requests and the guess at the prompt from each bucket of tokens. The guess is the number its caller made once (`prompt_tokens`), for a turn the very number the credits are held by. A second try is a second request.
2. **Hands on the model's events as they come, unchanged, and its errors as they came,** the same objects: the walk sorts them by kind.
3. **Puts the tokens right when the stream is over, however it ended:**
    - after a `Finish` with the provider's count, by that count, `input_tokens + output_tokens`, the cached part included: the difference to the guess is added, or given back;
    - after a `ConnectionFailedError`, nothing was sent: the request and the guess are given back;
    - after anything else, another error, a `Finish` without a count, or a stream closed before its end by a stop, a cut or a cancellation: the guess stays, and a guess at what was handed on of the answer, `tokens_in` of its text and its reasoning, is added.
4. **Dates each by the clock when it happens,** the take when the request is sent and the rest when the stream is over: the clock is handed down from the turn, or the worker.
5. **Never fails an answer.** When Valkey doesn't answer, at the take or after the stream, one warning is logged, with the error's kind, and the stream goes on as if the model had no budget. When the take failed, nothing is put right afterwards: "model groq-large: what a request to it took of its budget couldn't be counted (ConnectionError)".
6. **Puts its tokens right also when a stop or a cancellation closes it,** shielded from it.

**The look, and the rule** (`Chain.look`, `Chain.asked_with`):

- **One look,** before a chain is asked, says of each ready model of the chain that has a budget whether one request and the guess at its prompt fit all of its buckets now: of those they don't fit, how long until they do, and the limit in the way, the one with the longest wait (`Spent`). When no ready model of the chain has a budget, Valkey is not asked.
- **The rule stays in `Chain.asked_with(tokens, spent)`,** pure, given the guess and what the look said:
    1. the room's rule first, unchanged (ADR 0029): the ready models with room for the guess, or every ready one when none has;
    2. of those, a model has budget when it has none, or the look said the request fits it now. While at least one has, those are asked, in the chain's order, and the others are passed over for their budget. When none has, all of step 1 are asked, and none is passed over for its budget;
    3. a model passed over for its room is not also said to be over its budget, and a model that isn't ready never counts as one that has budget.
- **`Asked.spent`** names those passed over for their budget, each with its wait and its limit.
- **When the look can't be had,** nothing is passed over for its budget, and one warning says so.

**A turn** (`actio.api.turns.start_turn`, `actio.chat.answering.answer`):

- the look comes right after the guess at the prompt is made, before the credits are held, at the request's time;
- the log's lines come after those of the room, a warning each, with names and numbers from the file alone, never a word of the question: "turn …: model groq-large has no budget left for the request for about 12 s (tokens a minute): it is passed over", the seconds rounded up and 1 at the least, the limit in words: requests a minute, requests a day, tokens a minute, tokens a day. When the look couldn't be had: "turn …: the models' budgets couldn't be read (ConnectionError), and none is passed over for its budget". When none has budget, nothing is said, as for the room;
- each model asked that has a budget is asked through its counted client, by the guess made once, with the turn's clock (`answer`'s `budgets` and `tokens`);
- everything else is as before: the 503 when no model of the chain is ready, the 422 for reasoning, which is the first ready model's to say whoever is then passed over, the credits, each model's request, the record and `answered_by`.

**The title job** (`actio.chat.titles.write`, `actio.worker.write_title`) follows the same look, rule and counted clients, under the same keys, by the guess at its own one message, with the lines "conversation …: model … has no budget left for the request for about … s (…): it is passed over" and "conversation …: the models' budgets couldn't be read (…), and none is passed over for its budget". The worker hands down `Resources.budgets` and its clock. Everything else of the job is as before.

**Unchanged:** the page, what a turn streams, the routes and the API's description, the tables, the model clients and the walk (`packages/llm`). No migration.

**The example** (`config/examples/free-cloud/models.toml`): Groq's two models state `requests_a_minute = 30`, `requests_a_day = 1000` and `tokens_a_day = 200000` beside their `tokens_a_minute = 8000`; `gemini-flash` states `requests_a_minute = 5` and `requests_a_day = 20`, and `gemini-lite` 15 and 500, beside their `tokens_a_minute = 250000`. `config/models.toml` describes the three keys and names no value, and the end-to-end stack's file states none of the four.

**Tests:** the engine's examples, a full bucket's whole size at once and one at a time and nothing more, what is left and what waits, more than a bucket holds, half a period, no less than empty and no more than full, a look that writes nothing, several buckets in one look, 225,000 a minute, a moment too far away, adds that arrive together, the keys and their expiry, Valkey unreachable, the script sent again after Valkey forgot it, and the API's and the worker's counting under the same keys, with the property test against the plain buckets (`test_budget.py`). The three keys, their six mistakes each, two models that share a budget and differ or agree, two providers of one model's name, what is kept, the rule's cases, the look, and the counted client's events, closes, dates and failures, with a stop that closes it while its count has to wait (`test_catalog.py`). A turn whose model has no budget left, by each of the four limits; the order of the log's lines, the room's first; none with budget; what a turn takes, a failure before the first word, a refused connection, a second try, a stand-in, a stop, an answer cut at its time, a `Finish` without a count, two models that share a budget, the budget come back after a minute, a lineup without budgets, and Valkey away (`test_turns_budgets_api.py`). A title written by a stand-in when the named model has no budget, the order of its lines, none with budget, Valkey away, what a title's request takes dated by the worker's clock as it goes, and a title's request that a turn finds taken (`test_titles.py`).

## Consequences

- **Easier:**
    - a model whose provider's minute or day is spent for a request isn't sent it while another model of its chain has budget, and the next model answers without a refusal first;
    - what the API's turns and the worker's titles send to one model adds up in one place, whatever process sends it;
    - the next step can refuse a turn for now when no model has budget, with how long until one has: the look says it already.
- **Harder:**
    - **a turn or a title whose chain has a ready model with a budget asks Valkey once more before a model is asked, and twice for each request to such a model,** the take and what puts it right. A Valkey that doesn't answer holds them up for its time limit, two seconds, each time (seen with Valkey's container paused, see Measured);
    - **the models file states figures that change** with a provider's plan, and two models that are one share them.
- **Known, and left** (each worked out, not seen, unless it says so):
    - **the Gemini API's day starts over at midnight Pacific time, and a bucket refills steadily:** after an idle day a bucket lets through up to a bucket more than that day holds, and the API then refuses, as before this step;
    - **the Gemini API counts input tokens a minute by its documentation, and Actio counts the answer's too:** a little early, by 8,192 tokens of 250,000 at the most for one answer;
    - **Groq's documentation says cached tokens don't count;** in the one exchange seen, nothing had come back for them 14 s later, and Actio counts them;
    - **a refused request (429) took nothing at Groq,** seen twice, and Actio keeps it counted: early again, by one request and its prompt;
    - **what a provider answers is not heeded yet:** a 429 that names a wait doesn't close the model's budget until then;
    - **a request that fits a model's budget in a moment is passed over, not waited for;**
    - **a model passed over for its budget is not asked as a last resort:** when the models that have budget then all fail, the turn fails, where before this step the model passed over was asked too, and might have answered, since Actio's count runs ahead of its provider's by the tenth kept back and by what is above. Asking those passed over for their budget last, after those that have some, would end it: the rule's order, and its line in the log;
    - **Actio counts only what it sends:** another program on the same account, `make preflight` and `make test-cloud` are not counted, nor is asking a server for its list of models;
    - **a bucket holds no less than nothing:** an answer longer than what was left is counted only as far as empty. And what is given back after a take that was cut there comes back whole, a refused connection's request, or the difference when the provider counted less than the guess: the bucket then holds a little more than it should;
    - **a stop that arrives while a take is on its way to Valkey leaves it taken, though nothing was sent:** the take is not shielded, and nothing puts it right. And when a title's 30 seconds end while its request's tokens are being put right, the title is asked for again later though it had come;
    - **a bucket's moment is kept in whole milliseconds, and an amount moves it by a whole number of them, rounded down** (seen, see Measured). So of a limit of thousands of requests a minute more than nine tenths fit at one moment: 3,750 of a stated 4,000, all of a stated 10,000, and 60,000 of a stated 50,000; from 66,669 a minute a request moves nothing, and none is counted; and of 30,000,000 tokens a minute, a take of 300 isn't counted. The free plans' figures are far from there: 30 requests a minute keep 27. Keeping the moment in microseconds would end it: the script, the values its tests expect, and this record's figures;
    - **two models that share a budget must state the same figures, and two providers' tables for one server are counted apart;**
    - **when no model has budget every one is asked, and each refuses:** the next step.
- **How to reverse it:** for an installation, leave the four keys out: nothing is counted, and a model is passed over for its room alone. In the code: `actio.core.budget` and `budget.lua`, `Resources.budgets`; `ModelEntry`'s three keys, `CatalogFile._share_budgets_alike`, `KEPT_PERCENT`, `kept`, `budget_of`, `Budget`, `OfferedModel.budget`, `Spent`, `Asked.spent`, `Chain.look`, `counted` and `asked_with`'s second argument in `actio.catalog`; the look and its lines in `actio.api.turns.start_turn` and `actio.chat.titles._ask`; `counted` in the links of `actio.chat.answering.answer` and `_ask`.

## Measured

On 2026-11-25. Each fact says how it was got.

**Before the build:**

- **Seen, Groq's free plan, `openai/gpt-oss-20b`, three requests on one connection, at 19:57 UTC:** (1) a prompt Groq counted as 91 tokens, answered with 1,500: its headers, 0.39 s after the request was sent, said 7,808 of 8,000 tokens left, full again in 1.44 s, and 999 of 1,000 requests; the answer's stream ended at 2.90 s. (2) A short request right after, its headers at 3.22 s: 6,587 tokens left and 998 requests; counted as 77 and 26. (3) The same request, its headers at 8.92 s: 7,263 left and 997 requests. By the first headers the minute would have been full again 1.83 s after that request was sent, before its answer was over: so 1,413 tokens were gone by the second request's headers, its own share at the door among them. Between the second and the third, 85 were gone that the refill, 133 tokens a second, didn't give back. So an answer's tokens count against the minute, after the answer. How Groq puts its estimate at the door and its count afterwards together was not seen.
- **Seen, the same account, earlier that day:** at the door Groq takes its own estimate of the prompt alone, not the room asked for the answer: 4,057 for a prompt it then counted as 3,932 (ADR 0029, "Measured"), and 192 for one it counted as 91. A refused request (429) took nothing, twice: the requests left stayed 994 and 998, and the tokens left were exactly on the refill's line, 2,915 and then 4,829 after 14.355 s, and 3,764 and then 3,959 after 1.463 s. After a prompt of 5,035 tokens of which Groq said 1,792 were cached, 2,915 of the minute's 8,000 were left, and that request, 14 s later, found nothing given back.
- **Seen:** Groq's headers state the day's requests, 1,000, and the minute's tokens, 8,000, and both refill steadily: one request every 86.4 s, 133 tokens a second.
- **By Groq's documentation,** read that day: 30 requests a minute and 200,000 tokens a day for each of those models on the free plan; limits are an organization's, by model.
- **By the Gemini API's documentation, and one free account's page that day:** `gemini-3.6-flash` 5 requests a minute, 250,000 input tokens a minute, 20 requests a day; `gemini-3.5-flash-lite` 15, 250,000 and 500. For a project, by model. The day starts over at midnight Pacific time. No header of an answer states any of it.
- **Seen, in ADR 0028's look:** a request the Gemini API refused, or answered with 503, still counted against its minute.
- **Seen, Actio's guess** (`actio_llm.prompt_tokens`): 1.10 to 1.12 of Groq's count, and 0.90 and 0.98 of flash-lite's, on made-up English notes.
- **Seen, a throwaway script on Valkey 9.1.3:** a take rounded up to the millisecond let a bucket of 27 a minute give 26 at once, and one of 13 give 12; rounded down, 27 and 13, and then a wait of 2,216 and 4,610 ms. With 5,000 of 7,200 taken, exactly 2,200 fit when takes are rounded down, and waited 1 ms when they were rounded up. A bucket of 225,000 a minute, 3,750 tokens a second, more than one a millisecond, which the rate limiter refuses, worked with the same arithmetic.

**On the real stack,** built from the tree as it was committed, with a models file made for the look: the example's chain, with Groq's `openai/gpt-oss-120b` as the default, stating the example's four figures, and `gemini-3.5-flash-lite` and then the local `qwen3.5:4b-q4_K_M` standing in for it. One guest, with made-up questions, through the API, from 21:02 UTC. What Valkey held was read from the stack's own Valkey, each bucket's key and its time to live.

| What was asked | What came back, the API's log, and what Valkey held a second later |
|---|---|
| A question of 45 characters, 74 tokens by the guess | Groq's own answer, the turn over after 0.58 s: it counted 118 tokens read and 34 written. Of `groq:openai/gpt-oss-120b`: 1 of the day's 900 requests taken and 149 of its 180,000 tokens; the minute's request nearly back, full again in 1.0 s, and the key of its tokens gone, full again |
| Two seconds later, a question of 19,950 characters, 5,736 by the guess | Groq's own answer after 0.80 s: 5,359 read, 64 written. 5,228 of the minute's 7,200 tokens taken and not yet back |
| Two seconds later, another of 20,423 characters, 5,873 by the guess | flash-lite's answer, its first piece after 1.45 s, the turn over after 5.91 s: 6,549 read. The log: "model groq-large has no budget left for the request for about 33 s (tokens a minute): it is passed over", then "model gemini-lite answered in place of groq-large". Nothing was sent to Groq: its day's requests stayed at 2 taken. Of `gemini:gemini-3.5-flash-lite`: 1 of the day's 450 requests taken |
| 19 s after that, the second question again, with 4,318 of the 7,200 left a second before | Passed over again, "for about 12 s (tokens a minute)", and flash-lite's answer after 4.38 s: 6,395 read |
| 12 s later, the same question, with 5,751 left a second before | Groq's own answer after 0.69 s: 5,359 read, 62 written. 545 of the 7,200 left |

Groq refused nothing in the look: no line of the log says that a model failed. The guess was 1.07 of Groq's count of the long question and 0.90 of flash-lite's of both, and 0.63 of Groq's count of the question of one sentence: under it, by 44 tokens.

**With Valkey held still,** once, on the same stack with flash-lite as the model for titles: the worker was stopped, a guest asked a short question, which Groq answered, Valkey's container was paused (`docker pause`), and the worker started. Its title job began at 21:03:47.87. At 21:03:51.20 the log said "the models' budgets couldn't be read (TimeoutError), and none is passed over for its budget"; at 21:03:53.21, 2.01 s later, "model gemini-lite: what a request to it took of its budget couldn't be counted (TimeoutError)"; and 0.61 s after that the job ended well, having lasted 5.95 s. The conversation had its title, written by flash-lite. No second line said that anything couldn't be counted: after a take that failed, nothing is put right.

The looks took four requests of Groq's `openai/gpt-oss-120b` and three of flash-lite's, the title among them.

**Large figures,** with the module as it was committed, on a Valkey 9.1.3 of its own, read back through the module and from each bucket's stored moment: a stated 4,000 requests a minute keeps 3,600, one request moves the moment by 16 ms, and 3,750 fit at one moment; a stated 10,000 keeps 9,000, a request moves it by 6 ms, and 10,000 fit; a stated 50,000 keeps 45,000, a request moves it by 1 ms, and 60,000 fit; a stated 66,669 keeps 60,002, and after 100,001 requests at one moment the next still fits. Of a stated 30,000,000 tokens a minute, a take of 300 left the bucket full, and one of 5,000 was counted as 4,950; of 250,000, a take of 300 was counted as 300.

**The end-to-end tests,** on their own stack: 79 passed, none of them new. Nothing of the page changed, and that stack's models state no limit.

**Mistakes planted on purpose.** After the build, 75 mistakes were planted one at a time in a scratch copy of the tree, and the tests that should notice each were run: 73 failed a test. The two that failed none:

- **the shield taken off what puts the tokens right when a stream is over.** The test of a turn stopped while its answer is written still passed: there the count was on its way to Valkey before the stop's cancellation came again (worked out from the client's code, not seen). A count that has to wait a moment first, as for a connection that has to be made, is lost without the shield: the test written for it gives the counted client budgets that wait two turns of the event loop;
- **the worker dating a title's count by the time its job began.** No test moved the clock while a title was written.

A test was written for each after that run. The review then found that nothing held the order of the log's lines, the room's before the budget's: three tests were written for it, a turn's two and a title's, and after them three more mistakes were planted, each of which failed one. On the tree as it was committed, all 78 failed a test.
