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

## Revisions

### 2026-11-26: a turn is refused for now when no model has budget left, and a model without budget is asked last

The decision left the refusal of a turn "for now", when no model of its chain has budget, to a step of its own, with what the page says of it, and asked every model as before meanwhile. Under "Known, and left" it named a consequence of passing a model over for its budget: such a model is not asked as a last resort, so when the models that have budget all fail, the turn fails, where the model passed over might have answered. This is that step, and it takes that consequence back. A notice of the page's that counts the wait down is the next one.

**When no model with room for the request has budget:**

1. **Ask every one, as before.**
    - Con: a provider that has nothing left refuses, each in turn, and when all do, the question stands in the conversation with a failed turn (worked out).
2. **Refuse the turn for now, before it starts.** Chosen.
3. **Hold the request on the server until a model has budget.**
    - Con: a wait can be a day's, and a second rule would be needed for which waits are short enough.

**The status:**

1. **503.**
    - Con: here it says that something is away or broken, a model or Valkey, with no time known. This is a rate, with a known wait.
2. **429.** Chosen.

**What tells it from the other 429s of a turn:**

1. **A `type` of its own.**
    - Con: every problem here is `about:blank`, and told by its members.
2. **A member that names the provider's limit.**
    - Con: it tells whoever asks about the installation's plan. The log says it.
3. **`model`, the model asked, as the 503 has it.** Chosen. A usage limit names a `window`, too many answers at once says `at_once`, and too many messages neither.

**Which models count:**

1. **Every ready one.**
    - Con: a model passed over for its room isn't sent the request while another has room. With budget, it would keep the request from being refused, and the models that are asked would refuse it in turn; with a shorter wait, it would name a time after which it still isn't asked.
2. **Those with room alone,** those the room's rule leaves. Chosen. When none has room, every ready model is asked and none is refused, as before: a request too large by the guess is asked all the same, and its model's own answer about its size is truer than a wait ([ADR 0029](0029-a-model-is-passed-over-for-a-request-too-large.md)).

**A model without budget, while another has some:**

1. **Not asked,** as it was.
    - Con: when those that have budget fail before their first word, the turn fails.
2. **Asked last,** after those that have budget. Chosen: Actio keeps a tenth of each figure back, so a model its count calls spent may still answer, and the other outcome is a failed turn.

**A provider's own 429 that names a wait:**

1. **Close the model's budget until then, here.**
    - Con: it is what the provider says, not what Actio counted, and it needs a "not asked until" of its own.
2. **With the circuit breaker.** Chosen.

**Decision: when no model with room for a request has budget left for it, a turn is refused for now, before it starts, with a 429 that says how long to wait, and the title job asks for its title again later. A model without budget is asked last, after those that have some, where it was passed over. A guess still never refuses: when no ready model has room, every one is asked, as before.**

**The rule** (`Chain.asked_with`, pure):

1. **the room first, unchanged** (ADR 0029): the ready models with room for the guess, or every ready one when none has. Those are "those left";
2. **then the budget, of those left.** A model has budget when `spent` doesn't name it:
    - **while at least one has,** those are asked first, in the chain's order, and then those without, in the chain's order. `Asked.spent` names those without, each with what the look said: they are asked last now, not passed over;
    - **when none has, and at least one ready model has room,** nobody is asked: the request is refused for now. `Asked.refused` is the shortest wait among those left, with its limit, and of two as short the first in the chain's order; `Asked.spent` still names each of them, in the chain's order;
    - **when none has, and no ready model has room,** every ready model is asked, in the chain's order, and none is named, as before;
3. **unchanged:** a model passed over for its room is not also named for its budget, and its wait is never the refusal's; a model that isn't ready counts for nothing. A chain with no ready model has nobody to ask, and that is no refusal: the route answers 503 for it before the rule is reached.

**A turn** (`actio.api.turns.start_turn`):

- **the rule is asked right after the look,** before the credits are held. When it refuses, the log says so in one warning, and the turn is answered 429. Nothing was committed: the question, and the conversation a first question creates, are rolled back with the request's transaction, no credits are held, and nothing is taken from a budget. The other lines of the log stay where they were, after the commit, with the turn's id;
- **the 429:**
    - `Retry-After` and `retry_after`, the shortest wait in whole seconds, rounded up and 1 at the least;
    - `model`, the name of the model asked, as the 503 names it;
    - no `window`, `resets_at`, `alternatives` or `at_once`;
    - `detail`, the wait in the words of the rate limits' refusals (`in_words`): "No model can take this message right now: this installation has reached the limits of its models' providers. Try again in 60 seconds.";
- **its line in the log,** a warning, by the conversation's id, since the turn's row is rolled back, with names and numbers from the models file alone, never a word of the question, each model left with its own wait and its limit in words, in the chain's order: "conversation …: no model has budget left for the request, which is refused for about … s: groq-large for about … s (tokens a minute), gemini-lite for about … s (requests a day)";
- **a model asked last** says so in the place of "it is passed over": "turn …: model groq-large has no budget left for the request for about … s (tokens a minute): it is asked after those that have", after the room's lines and the line of a look that couldn't be had, as before;
- **a look that can't be had refuses nothing:** `spent` is empty, as before. Its line now ends "…, and each is taken to have some left", a turn's and a title's, where it said "and none is passed over for its budget": nothing is passed over for a budget any more;
- **everything else is as before:** the 503, the 422 for reasoning, which stays the first ready model's whoever is then asked last, the credits, each model's request, the counted clients, the record and `answered_by`.

**The title job** (`actio.chat.titles._ask`) follows the same rule. When it refuses, no model is asked, the log says "conversation …: no title, no model has budget left for the request: groq-large for about … s (tokens a minute)", each model left named so in the chain's order, after the room's lines, and `NotWrittenError` is raised with that text, as for a server that is unreachable. The job is asked for again by its own waits (`ASKED_AGAIN_AFTER`), which don't follow the budget's. A model asked last says "conversation …: model … has no budget left for the request for about … s (…): it is asked after those that have".

**The API's description:** the 429 of `POST /api/conversations/{conversation_id}/turns` says the fourth reason: no model that could answer can take the message right now, the installation having reached the limits of its models' providers; `model` names the model asked, and `Retry-After` says how long to wait. `Problem`'s description lists `model` for that 429 beside the 503's.

**The recorded refusal:** `apps/api/tests/fixtures/turns/no_budget.json`, recorded as `limit_reached.json` is ([ADR 0021](0021-usage-limits-in-the-page.md)): the 429 a question gets when its one model's budget of one request a minute was spent at the test's own moment, a wait of 60 seconds.

**The page's source is unchanged.** A 429 that names no window is that question's own problem: the page says the API's `detail` in a notice with "Dismiss", and the question is back in the box. A component test answers with the recorded refusal, and holds that.

**Unchanged:** the look, the counted clients, the buckets and their script, the models file's keys, the tables. No migration.

**Tests:**

- **the rule** (`test_catalog.py`): those with budget asked first and those without after them, each group in the chain's order, the asked model among those without; none of those with room with budget, nobody asked, the shortest wait, each named in the chain's order, and of two as short the first; a model without room that neither shortens the wait nor, with budget, keeps the refusal away; none with room nor budget, every ready one asked and none named; none with room and one with budget, asked first; a model that isn't ready never counted for budget or for its wait, and a chain with none ready no refusal; a look that said nothing, refusing nothing;
- **a turn** (`test_turns_budgets_api.py`): the 429 whole, with its header; nothing left behind, each shown apart: a first question's conversation that doesn't exist afterwards, a later question's conversation that reads back as it was, the usage as before, no model sent anything, and no bucket holding more taken than before; a question answered again whose earlier answer is still the one shown; the same question, under the same id, answered once the wait is over by the test's clock; each of the four limits with its own wait, a day's in hours; two models without budget, the shorter wait whichever comes first, and the log's line whole, naming both in the chain's order, with no word of the question in the log; the asked model without budget asked after a stand-in that fails before its first word, answering by its own name, with the log's lines in order, and sent nothing when the stand-in answers; none with room nor budget, answered as before, with nothing said of budgets; a chain whose other model isn't ready, refused and not answered 503; budgets that can't be read, refusing nothing, with the buckets spent where a look would have found them; and an account at its usage limit, told of the budgets, with nothing held;
- **a title** (`test_titles.py`): none with budget, nothing asked or taken, the log's line, the job put back, and the title written at its next try once a budget has come back; the named model without budget writing the title after its stand-in with budget failed; and none with room nor budget, asked as before;
- **the recorded refusal** (`test_turn_fixtures.py`), and the page's test of it (`components/usage/usage.test.tsx`).

**Known, and left:**

- **the page says the API's sentence once, and counts nothing down** (read in the code: `explain` in `lib/chat/outcomes.ts`): its notice stays after the wait is over, until it is dismissed or the next question is sent. A notice that counts down is the next step;
- **the wait is this request's, at the moment it was refused** (worked out): another request may take what came back meanwhile, and a shorter question may fit sooner;
- **each refused try counts against "too many messages"** (read in the code: that limit, `limits.turn`, is taken before the look);
- **Actio refuses by its own count,** which keeps a tenth of each figure back, and counts a prompt by its guess, which was seen on both sides of the providers' counts (above: 1.07 to 1.12 of Groq's count of long prompts and 0.63 of its count of one sentence, 0.90 and 0.98 of flash-lite's). So a question may be refused that its provider would have taken, and one let through that its provider refuses (worked out);
- **with a day's tokens spent, a long question waits long** (worked out): with `tokens_a_day = 200000`, 180,000 are kept, about 2 tokens a second, and a request of 5,000 tokens then waits 40 minutes at the most;
- **the Gemini API's day starts over at midnight Pacific time,** and a bucket refills steadily: Actio may name a wait of minutes where the provider's is hours. The provider's own refusal then moves the walk on, or fails the turn (worked out);
- **a model asked last that its provider refuses is counted as a request and its guess all the same** (read in the code: `counted` gives them back only after a refused connection), and at the Gemini API a refused request counted against its minute (seen, ADR 0028's look);
- **a provider's own 429 still closes nothing:** that is for the circuit breaker (read in the code);
- **the title job's waits are its own,** so a title whose models stay without budget past its fourth try is not written (read in the code: `ASKED_AGAIN_AFTER`);
- **a refusal of one second is still a refusal:** nothing waits on the server (read in the code: `Spent.seconds` is 1 at the least, and the rule refuses whatever the wait);
- **the look and the refusal are inside the turn's transaction** (read in the code): the question is written, and under a plan that limits answers at once the account's lock of admissions taken, before the look, and both are held while Valkey is asked, until the refusal rolls them back.

**How to reverse it:** for an installation, leave the four keys out: nothing is counted, and nothing refused. In the code: `Asked.refused`, and the branch of `Chain.asked_with` that sets it, which asks every model left again when none has budget; the refusal in `actio.api.turns.start_turn` (`_no_budget`, `_waits`) and in `actio.chat.titles._ask`; for "asked last", `asked_with` giving those with budget alone again, with the words "it is passed over" in the two lines; the 429's sentence in the route's `responses` and `ProblemDocument`'s, with `make openapi`; and `no_budget.json` with its two tests.

**Measured,** on the real stack, built from the tree as it was committed, on 2026-11-26 by the machine's clock (from 22:48 UTC on the 25th), with models files made for the look and the shipped local model, `qwen3.5:4b-q4_K_M` on Ollama. No request went to a cloud provider: the refusal is by Actio's own count. Each of these was seen once, unless it says otherwise.

**A refusal.** The model alone in its chain, stating `requests_a_minute = 2`, of which 1 is kept, and naming titles too. One guest, through the API:

| When, UTC | What was asked | What came back |
|---|---|---|
| 22:48:05 | A short question | The model's answer after 4.61 s, for 19 credits |
| 22:48:09 | A first question, in a conversation of its own | 429, as `application/problem+json`, with `Retry-After: 56`: "No model can take this message right now: this installation has reached the limits of its models' providers. Try again in 56 seconds.", `retry_after` 56 and `model` "standard". Reading the conversation answered 404, and the usage said 19 credits, as before. The log: "conversation …: no model has budget left for the request, which is refused for about 56 s: standard for about 56 s (requests a minute)". A second later Valkey held one bucket, its one request taken, full again in 54.7 s |

**The first conversation's title,** by the worker's log. Its job began at 22:48:09.77 and said "conversation …: no title, no model has budget left for the request: standard for about 56 s (requests a minute)": to run again, after 0.135 s. It ran again at 22:48:39.98, 30 s later: "… for about 26 s", after 0.044 s. And at 22:50:40.67, two minutes later, it ended well after 0.584 s: the conversation had a title, written by the model. That request took the minute's one: seven seconds later Valkey said full again in 51.5 s.

**The page,** in Chromium at http://localhost:8080, twice, at 22:51:43 and 22:52:46. The first run read the page at the very instant the refusal's sentence appeared, and found the box still empty and the question still shown as a message; the second waited for the question, which was back in the box 54 ms after the sentence was first seen, at each of its two refusals. Otherwise the two runs were alike:

- a first question was answered. A second in the same conversation was refused, with `Retry-After: 60`: the page said the API's sentence, "… Try again in 60 seconds.", in a notice with "Dismiss". With the question back in the box, the conversation showed its two messages, and the API had two;
- "Dismiss" took the notice away, and the question stayed in the box;
- a first question in a new chat was refused, with `Retry-After: 59`: with the question back in the box, no message was shown, the address was still `/`, and the list of conversations was as long as before (read in the second run);
- no error of the page's script.

**A model asked last.** The same model with the same budget, and the repository's fake model, run on the same machine, standing in for it without a budget; no model for titles. One guest, through the API, at 22:53:48 and 22:53:49:

1. a question: the local model's own answer, after 0.35 s;
2. a question that tells the fake to fail (`[[fail: 503]]`): the log said "turn …: model standard has no budget left for the request for about 60 s (requests a minute): it is asked after those that have", then "turn …: model fake failed (ProviderUnavailableError), and standard is asked next", and the local model answered after 0.40 s, the turn's `answered_by` its own name;
3. a third question: the first of those lines again, then "turn …: model fake answered in place of standard", and the fake's answer after 0.06 s.

**The end-to-end tests,** on their own stack: 79 passed, none of them new. That stack's models state no limit, and the page's source is unchanged.

**Mistakes planted on purpose.** After the build, 36 mistakes were planted one at a time in a scratch copy of the tree, 35 in the API and one in the page (any 429 with a wait taken for a usage limit), and the tests that should notice each were run: every one failed a test. Two of them proved less than they seemed, as they were first planted: the refusal raised only after the credits were held, and only after the turn was committed, had also lost their line in the log, and the test of that line is what failed. Planted again with the line as it is, the first failed three tests and the second five. Five mistakes of the rule were then planted against a turn's tests alone and a title's alone, since a rule that must hold at three levels can be held at one: nine of those ten failed a test, and the tenth is no mistake where it was run, the longest wait in the place of the shortest at a title, which names each model's wait and never the refusal's own. On the tree as it was committed, after the review's corrections, all 46 plantings were run once more: 45 failed a test, and the one that failed none is that one.

**Not measured:** a refusal on a cloud provider's figures, a wait of a day, which the tests hold by the test's clock, and the page in Firefox and WebKit, whose source is unchanged.

### 2026-11-26: the page counts the wait down

"Known, and left" in the revision above said that the page says the API's sentence once, and counts nothing down. [ADR 0021](0021-usage-limits-in-the-page.md)'s revision "a refusal for now is counted down" ends that: the page tells this 429 from the others by its `model` beside its `retry_after`, with no `window`, says it in words of its own, and counts the wait down above the box until it is over, in the conversation it was asked in. Sending stays open meanwhile, and nothing is sent by itself. The 429 is unchanged.

### 2026-11-26: `make preflight` holds the models file against what the providers state

"Harder" in the decision said that the models file states figures that change with a provider's plan. Actio counts by them, and nothing held them against the provider: `make preflight` printed what each provider states, of its own models, and a person compared.

**Decision: `make preflight` reads the models file, asks the models that file offers at Groq and the Gemini API, fails when the file states more than the provider does, and names each figure of the file it couldn't check.**

1. **The file:** the one `--models-file` names, or else `ACTIO_MODELS_FILE`'s, or else the API's own default, `config/models.toml`, taken from `Settings` and seen from the directory the script runs in. The variable is taken as the API takes it, whenever it is set: set and empty, it names the directory itself, which is no file, and neither the run nor the API starts with it (seen: `Settings` reads it as `.`). The run names the file before anything about a provider. It is read by `actio.catalog.read_file`, with all of its checks: a file that isn't there, isn't TOML or holds a mistake is one line, the reader's own text whole, status 1, and nothing is asked.
2. **The checks:** each `[providers.<name>]` table whose `url` has Groq's host, `api.groq.com`, or the Gemini API's, `generativelanguage.googleapis.com`, the whole host in any case, is one check of that provider, in the file's order, Groq's first. A provider the file has no table of is checked as itself, with its own variable and its own models, which are the example's cloud models, and the run says that the file offers none of its models.
3. **The key:** the one in the variable the table names (`api_key_env`), read as before. A table that names none fails, and nothing is asked for it: Actio would ask that server without a key.
4. **The questions:** at the script's own addresses, never one made from the file. A table's `url` that isn't its provider's OpenAI-compatible address is said, with both, and is no failure; it is printed with the check's key hidden in it, whole or in a piece of six characters or more, as a provider's text is, since a `url` may carry a key. The file's models on the table are asked, each id once; models named on the command line take their place, and one of them the file doesn't have on the table is said, and compared with nothing. A model of the file on any other server is asked nothing.
5. **What is held against what,** for a model that answered: `context_window` against Groq's `context_window` and the Gemini API's `inputTokenLimit`, `max_output_tokens` against Groq's `max_completion_tokens` and the Gemini API's `outputTokenLimit`, each model's own; and the limit of each header of Groq's answer against the key of what `Provider.counts` says it counts, `requests_a_day` and `tokens_a_minute`, said once for the models of the file that are one model on one table.
6. **What it says of each figure:** one larger than the provider's fails, in a line that names `models.<name>`, the key and both figures, and every such figure is said. The same or less passes, in one line, with the provider's figure beside one that is less. A limit the provider states and the file leaves out is said, and passes. A figure held against nothing is named as not checked, with the key, the file's figure, why and where a person reads it, and passes, whatever it states: Groq's `requests_a_minute` and `tokens_a_day`, every limit of a Gemini model, what this answer or list didn't state, and what it stated that can't be read as a number. A model of the file on another server that states any of the four limits has them named as not checked, unless the command line names providers. A model that failed before its answer compares nothing.
7. **The last line and the status:** the last line says of each check, named by its provider and its table, whether it passed, and of one that passed with figures not checked, how many. The status is 0 when every check that had a key passed, and 1 when one failed, a figure too large included, when none had a key, and when the file can't be read.

**Why:**

- **the API's own reader,** so that a file the API would refuse is refused here in the same words, and a key added to the file later is read here without a second set of rules. It is the first script that imports the application, `actio.catalog` and `actio.core.settings` alone: the import took 0.13 to 0.15 s (see Measured);
- **the table's own key variable,** since the figures are an account's, and the account is the key's;
- **questions at the script's own addresses,** so that a key goes nowhere a file names;
- **a limit left out is no failure,** since leaving it out is how an installation turns the counting off;
- **a figure too large fails,** since Actio then counts by more than the provider gives, and its refusal for now never comes before the provider's.

**Known, and left** (each worked out or read in the code, none seen, unless it says so):

- **Groq's requests a minute and tokens a day, and every limit of the Gemini API, are still read by a person,** and a file that states more of them than the account has passes;
- **the check is of the moment it is run,** and nothing runs it by itself: not `make check`, CI, or the API's start;
- **the file's figure is held against the provider's whole figure,** not against the nine tenths Actio keeps;
- **a figure smaller than the provider's passes,** and Actio then keeps to less than it could;
- **the window a Gemini model is held to is its `inputTokenLimit`;**
- **`thinking` isn't held against what the Gemini API says of a model;**
- **a table's `url` that differs is said and not followed,** so a wrong path in the file passes with a line;
- **a model that fails its question compares nothing,** though its window and longest answer were read before it, and nothing is asked a second time: a model too busy fails its check, and the run (seen, see Measured);
- **what `make preflight` asks isn't counted in Actio's budgets,** as said above;
- **the variable's name is read in capitals alone:** the API's settings take `actio_models_file` too (seen), and the script doesn't;
- **another secret in a table's `url` than the check's key is printed as the file has it,** such as a password before the host: the file is the reader's own, and the script hides the key it was given.

**Found on the way, and fixed with it:** the API's own reader let three files out as the TOML parser raised them, bytes that aren't UTF-8, a whole number of more than 4,300 digits, and lists nested too deep, where it promises one line. [ADR 0013](0013-the-models-on-offer-are-a-file.md)'s revision of this date has it.

**How to reverse it:** in `scripts/preflight.py`, `models_file`, `read`, `checks_of`, `Check`, `Stated`, `Description`, `compared`, `limits_stated`, `counted_by`, `elsewhere`, `summary` and the option `--models-file`, with `Provider.openai_at`, `models_at`, `Asking.variable` and `run` as they were before; their tests in `scripts/tests/test_preflight.py`; and what `docs/development.md` and `docs/getting-started.md` say of the run.

**Measured** (2026-11-26, one account's free keys; times are UTC):

- **Before the step,** one run of the script as it was (09:16): Groq's answers' headers stated 1,000 requests a day and 8,000 tokens a minute for `openai/gpt-oss-120b` and `openai/gpt-oss-20b`, and its list a window of 131,072 tokens and answers up to 65,536 for both; the Gemini API described `gemini-3.6-flash` and `gemini-3.5-flash-lite` with 1,048,576 and 65,536 each, and its answers stated no limit. So the example's figures are no larger than any the providers state.
- **The example,** `make preflight args="--models-file config/examples/free-cloud/models.toml"`, twice (09:56 on the first build, and 10:10 with the script as it is committed):
    - Groq's check passed both times, each model's four figures held, `max_output_tokens` 2,048 with Groq's 65,536 beside it, and its last line said "passed, with 4 figures of the models file not checked", the two models' `requests_a_minute` and `tokens_a_day`;
    - `gemini-3.5-flash-lite` answered both times, its two sizes held and its three limits named as not checked;
    - `gemini-3.6-flash` was refused both times, and a third time at 10:12 asked alone, with 503, "This model is currently experiencing high demand", so the Gemini API's check failed and the run ended with status 1 each time. It had answered once that morning, after 27.31 s of the 30 a question is given. The Gemini API's check passing whole was seen against the tests' fake alone.
- **A file that states too much,** a scratch copy with `requests_a_day = 2000` for the larger of Groq's models, and `tokens_a_minute = 6000` and no `requests_a_day` for the smaller (10:10): "FAILED: for models.groq-large, requests_a_day 2,000 is more than the 1,000 Groq states"; for the smaller, "tokens_a_minute 6,000 (Groq states 8,000)" among those held, and "Groq states requests_a_day 1,000, and the models file states none: Actio counts nothing by it"; status 1.
- **Groq's check alone,** the same command with `groq` after the file, on the tree as committed (10:23): "Groq (providers.groq) passed, with 4 figures of the models file not checked", and status 0.
- **What the step asked of the providers:** 10 questions of Groq and 8 of the Gemini API, 4 of them of `gemini-3.6-flash`, of which a free account has 20 a day.
- **The import** of `actio.catalog` and `actio.core.settings` took 0.13, 0.14 and 0.15 s in three runs, and `preflight.py --help` 0.19 to 0.20 s whole.
- **Odd inputs,** 71 runs through a transport that answers in the providers' place: a directory, an empty file, bytes that aren't UTF-8, a byte order mark, the file named twice or not at all, urls with a port, a password, a dot after the host, brackets the parser refuses and hosts that only look like a provider's, tables without a key variable or with another's, ids with escape sequences and with a slash, a question mark and a hash, figures of thirty digits, and headers and sizes that are zero, negative, text, empty, sixteen and 5,000 digits long. None raised, none printed a control character or a piece of a key, and no request went anywhere but the script's two addresses.
- **Mistakes planted on purpose,** each alone in a scratch copy, against the step's tests:
    - on the first build, 77 mistakes in the script, of which 68 failed a test. The nine that failed nothing each got a test: a table's `url` that differs by its scheme alone, by its path alone, or not at all but for capitals in its host; a `url` and the file's path printed as they came; a size that isn't a number; the limits of an id two models share said for the first alone; a provider's own models listed as on another server; and a check that failed said to have passed with figures not checked;
    - on the tree as committed, 85, seven of them in the API's reader: all failed a test. On the way, two planted in the reader had failed nothing: one because a clause there had become another's twin, and the two were made one; and one that cut the parser's message short, which got a test of where in the file a mistake is.
- **The review** found no fault that had to be fixed and seven smaller ones, and each was looked into. What changed by them: a sentence of the guide that said more than the run checks; a table's `url` that was printed with a key in it; a third file the API's reader let out, a number too long, which the review worked out and a run confirmed; tests that read why a figure wasn't checked, and that a shared id's limits are said once; and an `ACTIO_MODELS_FILE` that is set and empty, which the first build took for the default where the API takes it for no file. The two addresses the run names for a model's sizes were opened, and each answered.
