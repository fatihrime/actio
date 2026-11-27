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

### 2026-11-26: a model too busy to answer fails nothing in `make preflight`

"A model that fails its question compares nothing", and "a model too busy fails its check, and the run", in the revision above, are no longer what happens to a model whose provider answers its question with 503. On the day of that revision three runs on the example ended with status 1 for that alone: `gemini-3.6-flash` was too busy, and nothing was wrong with the key or with the file.

**Decision: a question answered with 503 is no failure.**

- **The model is said to be too busy to answer,** with what its provider said, and the wait it names when it names one.
- **Its window and its longest answer are held against the file all the same:** the provider states them before the question. A larger figure there still fails.
- **The limits an answer's headers would have stated,** Groq's requests a day and tokens a minute, are named as not checked, for that reason.
- **The last line counts such models,** before the figures not checked: "passed, with 1 model too busy to answer and 6 figures of the models file not checked".
- **Every other refusal of a question fails,** as before: a limit that was reached, a key refused, any other error of the provider's server.
- **Nothing is asked a second time,** as before. One more try after a pause was the other way, and what the hand-over of the revision above had named. Asked three times within 30 seconds, again 5 s after its answer and 15 s after the next, `gemini-3.6-flash` answered 503 each time, and named no wait (see Measured). So a second try was not seen to help, and it may spend one more question of a day's twenty: a request the Gemini API answered with 503 was seen to count against a minute's limit (above, "Context"), and whether it counts against a day's was not looked at.

**A refusal's message is printed up to 600 characters,** where every other text of a provider's stays at 200 (`REFUSAL_SHOWN_AT_MOST`). This settles what `make preflight` had left open since it was written, whether the Gemini API's refusal at a limit states the limit. It does, in its body alone: in two answers saved on 2026-11-25 the message named each limit that was reached, with its value, only after 236 characters of other text, and the wait last. One limit made 536 characters, "…requests per minute limit exceeded on Free Tier", "limit: 15", and the first of four ended at the 507th. Cut at 200, as the run cut a refusal's message until now, neither says its limit. So a limit of the Gemini API is stated only to a request that it refuses: no run that passes can read it, and those figures of the file stay a person's to read.

**The models file's variable, set and empty,** is in [ADR 0013](0013-the-models-on-offer-are-a-file.md)'s revision of this date.

**Known, and left** (each worked out or read in the code, unless it says seen):

- **a model too busy isn't shown to answer for this account:** a model the free tier doesn't include is refused with 429, and that isn't learnt while the model is busy. Its line says to run the check again later;
- **a run in which every model was too busy ends with status 0,** having checked the key, the list of models and the sizes;
- **only a question's 503 is taken so:** a list of models or a description answered with 503 fails its check, and so does a question that isn't answered within 30 seconds (seen once close to it, 27.31 s);
- **of a refusal at several limits the message is cut after the first,** and the wait at its end with it. The body's own members for them, `QuotaFailure` with a `quotaValue` and `RetryInfo` with a `retryDelay` (seen in the saved answers), aren't read: holding a limit that was reached against the file's figure would take reading them, by the Gemini API's own names for its limits;
- **Groq's 503 was not seen:** the one in the tests is made up.

**How to reverse it:** in `scripts/preflight.py`, `TooBusyError`, `TOO_BUSY`, `Result.busy` and its clause in `summary`; and `REFUSAL_SHOWN_AT_MOST` with the third argument of `printable`; with their tests.

**Measured** (2026-11-26, one account's free keys; times are UTC):

- **`gemini-3.6-flash`, asked the run's one question:** answered at 09:16, after 27.31 s; 503 at 09:56, 10:10 and 10:12; then, asked three times running, 503 at 10:36:49, at 10:36:58, 5 s after that answer came, and at 10:37:19, 15 s after the next, in 4.63, 5.52 and 4.91 s, with no `retry-after`; and 503 twice more, at 10:46 and 11:06, in the runs below. Eight of nine questions in 110 minutes.
- **The example,** `make preflight args="--models-file config/examples/free-cloud/models.toml"`, twice (10:46 on this revision's first build, and 11:06 on the tree as committed): status 0 both times. "Groq (providers.groq) passed, with 4 figures of the models file not checked; Gemini (providers.gemini) passed, with 1 model too busy to answer and 6 figures of the models file not checked." Under `gemini-3.6-flash`: that it was too busy to answer, with the Gemini API's sentence; its `context_window` and `max_output_tokens` held, the second with the API's 65,536 beside it; and its three limits named as not checked. A run in which every model of the Gemini API answered was still not seen.
- **The file's variable, set and empty:** the API's start, for the models file and for the plans file, the worker's, and `make preflight` each said the line of ADR 0013's revision, the first three with status 2 and the run with 1.
- **Odd inputs,** 78 runs through a transport that answers in the providers' place, the 71 of the revision above and seven more: every model too busy, a wait that isn't one, a busy provider's message with the key and escape sequences in it, a model too busy with a figure too large, a refusal of a million characters, one with the key where the message is cut, and a directory named by the variable. None raised, and none printed a control character or a piece of its check's own key: tried apart with the key at seven places around the 600th character, it was hidden whole each time.
- **Mistakes planted on purpose,** each alone in a scratch copy: 109, the 85 of the revision above with 24 for this one. 108 failed a test. The one that failed nothing puts the two counts of the last line the other way round, which is wording, and which the tests leave to the script.
- **The review** found two faults that had to be fixed, both in this revision's own first build. The reader asked a path whether it was a directory before anything was caught, so a name too long, or a file in a folder that can't be looked into, ended the API's start with a trace where it had been one line (seen with a file of each kind, then fixed, the test first). And no test held that a list of models or a description answered with 503 still fails: now one does. Of its six smaller points, five changed words or added a test; the order of the last line's two counts was left.

### 2026-11-26: a model that keeps failing is asked after the others of its group

[ADR 0031](0031-a-model-that-keeps-failing-is-asked-last.md) adds a circuit breaker to the rule's order: within each of its two groups, the models with budget and those without, a ready model whose breaker says that it keeps failing is asked after those that don't, each in the chain's order. So the order is with budget and not failing, with budget and failing, without and not failing, without and failing. The refusal for now is the budget's alone: a failing model with budget still keeps it away, and a failing model without counts for its wait as before. `Chain.asked_with` takes what the breakers said as its third argument, and `asked_through` gives the client a model is asked through, the counted one inside it. "A provider's own 429 still closes nothing" is still so: the breaker counts it as one failed try, and a wait the provider names is a later step.

### 2026-11-26: a provider's own wait is kept, and until it is over its model has no budget

The decision's "Known, and left" said that "what a provider answers is not heeded yet: a 429 that names a wait doesn't close the model's budget until then". The revision "a turn is refused for now …" weighed it, chose the circuit breaker for it, and left "a provider's own 429 still closes nothing: that is for the circuit breaker". [ADR 0031](0031-a-model-that-keeps-failing-is-asked-last.md) then built the breaker without it, and its "Known, and left" named it the next step: "a provider's own 429 that names a wait still closes nothing by that wait: it counts as one failed call". This is that step, and it takes the wait to the budget, not to the breaker.

Until now only the walk read a 429's wait (`RateLimitedError.retry_after_s`): a wait of 2 s or less has the model asked once more after it, and a longer one moves on to the next model at once ([ADR 0028](0028-a-model-names-those-that-stand-in-for-it.md), revision "a model is asked once more, and a server says how long it may take to begin"). Nothing remembered it, so the next turn asked the same model first again, and was refused again. A chain's only model whose provider said "retry in 23 hours" would have failed every question for those 23 hours, each with "try again in a moment" (worked out).

What the providers were seen to send, in the saved exchanges of earlier steps:

- **Groq,** for a request that passed its minute's tokens: `retry-after: 1` where its body said "try again in 457.5ms" (ADR 0028), and in three more saved refusals `retry-after: 2`, `2` and `1` where the bodies said 1.695 s, 1.77 s and 735 ms: the wait rounded up to whole seconds. The body says whose wait it is, "Limit 8000, Used 3171, Requested 5055": that request's, at its size;
- **the Gemini API,** for the sixteenth request of a minute to `gemini-3.5-flash-lite`: no `Retry-After`, "Please retry in 49.846993202s" and a `retryDelay` of `49s` (ADR 0028). For a model its free tier doesn't include, a `retryDelay` of `83280s`, 23 hours ([ADR 0027](0027-cloud-models-through-their-openai-compatible-layers.md));
- **six 429s,** five saved and one more in ADR 0028's table, came after 0.13 to 0.38 s;
- **a request the Gemini API refused still counted against its minute** (Context, above).

**Whose rule it is:**

1. **The breaker's: open until the provider's moment.**
    - Con: a breaker never refuses and never drops a model (ADR 0031), so a chain's only model whose day is spent would be asked at every question, and each would be a failed turn that says "try again in a moment", for 23 hours. And it is the provider's word about its count of the account, not a guess at the model's health.
2. **The budget's: until then the model has no budget.** Chosen: the order, the refusal for now and the page's countdown are there already.

**Where it is kept:**

1. **In Valkey, beside the buckets.**
    - Con: a third operation of the script, with its plain twin, and a read before every turn of a chain that states no limits, to save each other process one refused request a wait: the six 429s seen came after 0.13 to 0.38 s. And the example's titles go to the local model, so its worker meets none.
2. **In each process, beside the breaker.** Chosen.
    - Con: the API and a separate worker each learn it, and at the Gemini API the request that learns it counts against the minute (seen, above); a restart forgets it.

**Which waits:**

1. **Every one.**
    - Con: the walk waits 2 s or less out itself and asks once more, and Groq's header named 1 or 2 s each time it was seen. Kept, such a wait would refuse a lone model's next question for a second where the walk would have had it answered.
2. **Longer than the walk's, and a day at the most.** Chosen.

**A model told to wait, while another has budget:**

1. **Not asked at all.**
    - Con: Groq's wait is one request's, at its size, so a smaller one may be taken; and when the others fail, the turn fails.
2. **Asked last, as a model without budget is.** Chosen.

**What ends it before its time:**

1. **Nothing.**
    - Con: a wait that is wrong, a plan changed that day, keeps a model that answers last for up to a day.
2. **Any answer of the model's.**
    - Con: an answer to a request that was sent before the refusal says nothing of the refusal's wait. This was the step's first build, and the Gemini API showed it: of two questions under way, one was refused with a wait of 8 s, the other, sent a second before it, was answered 2 s later, and the wait was forgotten (seen, see Measured).
3. **An answer to a request sent after the wait was noted, and the provider's next wait of more than 2 s.** Chosen.

**Whether that 429 is still a failed call of the breaker's:**

1. **No.**
    - Con: a provider that names a short wait at every request would never be found out: each turn would pay the walk's wait and a second refusal.
2. **Yes, as before.** Chosen: the two rules don't know of each other, and each only moves a model later.

**Decision: a wait that a provider names in a 429, longer than the 2 seconds the walk waits out itself, is kept in the process for that model on that provider, until it is over, a day at the most; meanwhile the model has no budget left, by the rule the budgets have: it is asked after the models that have budget, and a turn that no model with room for it has budget for is refused for now, for what is left of the wait. An answer of the model to a request sent after the wait was noted ends it, and the provider's next wait of more than 2 seconds replaces it. `Chain.asked_with` is unchanged: the wait reaches it as one more `Spent`.**

**Which waits are believed** (`believed` in `actio.catalog`, pure):

- **none** for a 429 that names no wait, for `nan` and `inf`, and for a wait of `WAITS_AT_MOST_S`, 2 s, or less, negative ones among them: the walk waits those out itself and asks once more (`actio_llm.router`), so nothing is left of them to keep. `WAITS_AT_MOST_S` is imported from `actio_llm.router`, as `actio.catalog` imports from the breaker's module;
- **otherwise the wait,** and a day, `BELIEVED_AT_MOST`, when it is a day or more: a provider's longest period is a day, and a wait named longer is taken for a day, after which one request finds out;
- **comparisons alone decide,** before any `timedelta` is made: `timedelta(seconds=1e308)` raises `OverflowError`, and this runs where a try ends, which must never raise.

**Where it is kept** (`OwnWait`, `OfferedModel.own_wait`): what a model's provider last said of when it takes requests again, pure, with no clock and no log, every moment given. `told(until)` is the provider's newest word, and replaces whatever was kept, later or sooner; `left(now)` is how long until it is over, `None` when nothing is kept and from the moment itself on; `words` is how many waits of the provider's have been noted so far, which a try reads when it begins; `over(now, words=…)`, for a try that ended answered, forgets the wait when none has been noted since that try began, and says whether a wait that was still running was forgotten. A count, so that no two readings of a clock are compared. `catalog.build` gives one to each model on a provider, as it gives breakers: two models of the file that are one model on one provider share it, and the same `model` on two providers has two. An `OfferedModel` made without one gets one of its own. Nothing of Valkey, `actio.core` or `Resources` changes.

**Who notes it** (`_Recorded`, which `asked_through` gives, its signature unchanged): at the end of each try, in the same `finally` as the breaker's record, with nothing awaited, once, by the one moment the clock handed down gives there:

- **a `RateLimitedError` whose wait is believed:** the wait is told, from that moment, and one warning says it, by the model's name in Actio and the wait in whole seconds, rounded up, 1 at the least: `model gemini-lite: its provider said to wait 49 s, and until then it counts as having no budget left`;
- **a try that ended answered,** by the breaker's own outcome, a stop or a cut after something was handed on included, **while a wait was running:** it is over, and one warning says it: `model gemini-lite: it answered before its provider's wait was over, and counts as having budget again`. An answer with no wait running says nothing. Neither does an answer to a request that was sent before the wait was noted, which leaves the wait as it is: that request was sent before Actio heard the refusal, and says nothing of it;
- **anything else changes nothing:** a 429 without a wait, or with one of 2 s or less, any other error, a try closed or cancelled before anything was handed on;
- **the breaker's record is as it was:** that 429 is still a failed call. When one try's end has both lines, the breaker's comes first;
- the error is handed on as it came, the same object, and the counted client inside is untouched: the refused request and its guess stay counted.

**Who reads it** (`Chain.with_own_waits`, pure): what a look said, with each ready model of the chain whose wait isn't over as a `Spent` of what is left of it, its limit `PROVIDERS_WAIT`, whose words are "its provider's own wait". Of a model the look names too, the longer wait, and of two as long, the provider's own. A model that isn't ready is never added, and nobody is asked anything. `Spent.limit` is a bucket or `PROVIDERS_WAIT`, and both are said in words the same way.

**The two callers,** `actio.api.turns.start_turn` and `actio.chat.titles._ask`, each have one line more, right after the look and what it does when the look can't be had: `spent = chain.with_own_waits(spent, now)`. So a look that couldn't be had still gets the providers' own waits, and a chain with no budget at all gets them without Valkey being asked. Nothing else of them changes: the refusal, its 429, the log's lines and their order. Their lines read:

- `turn …: model gemini-lite has no budget left for the request for about 49 s (its provider's own wait): it is asked after those that have`;
- `conversation …: no model has budget left for the request, which is refused for about 49 s: gemini-lite for about 49 s (its provider's own wait)`;
- `conversation …: no title, no model has budget left for the request: gemini-lite for about 49 s (its provider's own wait)`.

**The fake** (`packages/fakellm`): `[[retry-after: 30]]` says how many seconds a scripted 429 names in its `Retry-After`, a whole number, 0 or more, and 1 without it, as before. Anything else is a directive the fake can't read, its 400. With another status, or without `[[fail: …]]`, it changes nothing. A directive's name may now hold a `-`.

**Unchanged:** `Chain.asked_with`'s rule and `Chain.look`, `counted` and its client, `actio_llm.breaker`, the walk and its rules, the adapters, the routes and the API's description, what a turn streams, the recorded fixtures, the tables, the page, the models file's keys, the settings, `actio.core` and `budget.lua`. No migration.

**Tests:**

- **the catalog's** (`test_catalog.py`): `believed` by its numbers, none, `nan`, `inf` and `-inf`, negative waits, 0, 1 and 2 s not kept, the next float above 2 kept, a day and anything longer, `1e308` and the largest float among them, a day; `OwnWait` by example, `left` before, at and after its moment, `told` replacing a later and a sooner one, `over` saying whether one was running and forgetting it, and leaving a wait that was named since the try began, a first one or a second; the recording client, a 429 of 49.2 s kept from the end of its try, by a clock that moved 10 s during it, with its line whole, in whole seconds rounded up, and the same error handed on, waits the walk waits out or none kept and said nowhere, a day or more kept for a day, the counted client keeping the request and the guess, five such 429s opening the breaker with its line first, an answer, a thought's, one closed after a word and one cut at its time ending a wait with their line, and a try closed before a word, a server too busy, a stream without its finish, a request too large and 429s of 1 s and of none leaving it, a stop before and after a word, a later 429 replacing a wait with a later and a sooner one, an answer with no wait running saying nothing, and an answer that was under way when another try was refused leaving that wait and saying nothing, with the next answer ending it; a wait said in whole seconds, rounded up, and 1 at the least for one made by hand; `Chain.with_own_waits` for ready models alone, with and without a budget, for what is left of the wait, the longer of the count's and the provider's, the provider's of two as long, two names of one model sharing a wait through `build` and the same name on two providers not; and through `asked_with`, unchanged, a told model asked after one that has budget, and alone refused for its wait;
- **a turn's** (`test_turns_own_wait_api.py`): a chain's model that answers 429 with 49 s, its turn answered by the stand-in, the next turns asking the stand-in first for what is left of the wait, by the test's clock, and the model first at the wait's very moment, the log's lines whole and in order, without the error's text or a word of the question; the wait dated when the try ended, by a server that took 10 s of the test's clock to refuse; 2.001 s kept, a wait of 0.01 s waited out and not kept, alone and in a chain, and a second try's 30 s kept; a 429 without a wait changing nothing; a lone model's turn failed as before, and the questions after it refused for what is left of the wait, rounded up, the 429 whole and its line; a day's wait in hours; nothing left behind by a refusal; models without a budget, with Valkey asked nothing; budgets that can't be read; the count's wait and the provider's, the longer said; a model passed over for its room, whose wait is never the refusal's; no model with room, each asked; a model that isn't ready, never named, and alone answered 503; the 422 for reasoning; a last resort that answers and ends the wait; two questions at once, the one that was being written when the other was refused answering after it, the wait left, and the question after them refused for now; a failure of another kind that leaves it; later 429s that replace it with a later and a sooner one; and two names of one model sharing it, and another model not;
- **a title's** (`test_titles.py`): its model told to wait by a turn of the same catalog asked after its stand-in, with the line by the conversation's id; alone, nothing asked, `NotWrittenError` with the line's text and the job put back; a title's own 429 kept for the turns of the same catalog; and with the budgets unreadable, the named model told to wait asked after its stand-in, the log's two lines in order;
- **the fake's** (`packages/fakellm/tests/test_chat.py`): the directive with a 429, named before or after `[[fail: 429]]`, and of 0; without it, 1, as before; with another status and without `[[fail: …]]`, nothing; and what it can't read, a negative number, a fraction, a sign, a word and nothing. **And through the adapter** (`packages/llm/tests/test_openai_compatible.py`): the fake's `[[fail: 429]] [[retry-after: 30]]` is a `RateLimitedError` whose `retry_after_s` is 30.

**Known, and left:**

- **the turn that meets the refusal is as before:** a lone model's fails with "try again in a moment", though its provider named a longer wait (read in the code);
- **the API's and a separate worker's waits are apart, and a restart forgets them,** which is also how a wait that is wrong is ended by hand (read in the code);
- **a lone model's wrong wait stands until it is over,** since nothing asks the model meanwhile (worked out);
- **Groq's wait is one request's, at its size:** a smaller question might have been taken sooner, and is asked last or refused all the same. So one person's large question can have everybody's smaller ones asked last, or refused, for that wait: up to about a minute for a minute's tokens, and longer for a day's (the body seen, and a short question refused after a long one's refusal seen twice, see Measured; the rest worked out). Telling a limit of tokens from one of requests, which each provider's refusal says in its own words, would let a smaller question through: a step of its own;
- **the provider's newest wait of more than 2 s replaces the one before it,** and one of 2 s or less, or a 429 that names none, leaves it: a short wait named for a small request ends a long one, and the next large request learns it again (worked out);
- **a request sent after the provider named a wait and before Actio noted it,** about as long as a refusal takes to arrive, leaves the wait when it is answered, though its provider took it after the refusal: the wait then runs to its time, or to the answer of a later request. This keeps a wait that could have ended, and never ends one that should stay (worked out);
- **a 429 that named a wait after something of its answer was handed on would be kept too:** no client raises one there, since a refusal comes with the response's status, before any line of an answer (read in the code);
- **`Retry-After` given as a date is not read** (read in the code: `_seconds` in `actio_llm.openai_compatible`);
- **waits of 2 s or less that keep coming cost each turn the walk's pause and a second refusal,** until the breaker opens (worked out);
- **the order is decided when a turn starts:** a turn waiting at a gate when a wait is noted asks in the order it had (read in the code);
- **a refusal of a second is still a refusal, and the title job's waits are its own,** as for the budgets (read in the code);
- **the refused request stays counted in Actio's own budget,** as before (read in the code);
- **a clock set back holds a wait that much longer** (worked out);
- **a turn refused with the budgets unreadable doesn't say that they couldn't be read:** that line comes after the commit, which a refusal doesn't reach; before this step nothing was refused when they couldn't be read. And where the line is said, a turn's or a title's, it says that each model is taken to have some left, which is of Actio's counts: the line after it may name a model without budget by its provider's own wait (read in the code);
- **the 2 s and the day are constants of the code;**
- **no end-to-end test:** a wait outlives its test in the stack's API, so it would need a model on a provider of its own there, as the budget's test has.

**How to reverse it:** `OwnWait`, `believed`, `BELIEVED_AT_MOST`, `OfferedModel.own_wait` and the waits in `catalog.build`; the two branches in `_Recorded` (`_heard`); `Chain.with_own_waits`, its line in `start_turn` and in `titles._ask`, and `ProvidersWait`, `PROVIDERS_WAIT`, the second kind of `Spent.limit`; the fake's directive, with the `-` its names may hold.

**Measured,** on 2026-11-26, on the real stack, built from the tree as it was committed unless it says "the first build", with models files made for the look. None of them states a limit, so Actio counted nothing, and a provider's own refusal was what was met. One guest, through the API, each question in a conversation of its own.

**The repository's fake model as the model asked,** run on the same machine: a question that holds `[[fail: 429]] [[retry-after: 30]]` has it refuse with that wait, and its own log counted the requests it was sent. Each of these was seen once on this tree, and once before on the first build, alike.

**A chain: the fake, with the shipped local model standing in,** `qwen3.5:4b-q4_K_M` on Ollama. Five questions, of which the fake was sent five requests:

| When, UTC | What was asked | What came back, and what the API's log said |
|---|---|---|
| 14:43:42 | A question that tells the fake to refuse with a wait of 30 s | The local model's answer, after 3.78 s, the turn's `answered_by` "standard". "model fake: its provider said to wait 30 s, and until then it counts as having no budget left", then "turn …: model fake failed (RateLimitedError), and standard is asked next" |
| 14:43:46 | A plain question | The local model's answer, after 0.34 s: "turn …: model fake has no budget left for the request for about 27 s (its provider's own wait): it is asked after those that have". The fake was sent nothing |
| 14:44:18, the 30 seconds over | A plain question | The fake's answer, after 0.12 s, its second request, and no line |
| 14:44:18 | A question that tells the fake to refuse with a wait of 1 s | "turn …: model fake failed (RateLimitedError), and is asked once more after 1 s", refused again, and the local model's answer after 1.51 s. Two requests to the fake, and nothing said of a provider's wait |
| 14:44:20 | A plain question | The fake's answer, after 0.03 s: asked first, as before |

**The fake alone,** with nobody standing in for it, from 14:44:37:

1. a question that tells it to refuse with a wait of 30 s: a failed turn, `model_unavailable`, after 0.06 s, as before this step, and the line of the wait;
2. a plain question at once: 429, as `application/problem+json`, with `Retry-After: 30`: "No model can take this message right now: this installation has reached the limits of its models' providers. Try again in 30 seconds.", `retry_after` 30 and `model` "fake". Reading its conversation answered 404. The log: "conversation …: no model has budget left for the request, which is refused for about 30 s: fake for about 30 s (its provider's own wait)". Ten seconds later, another: the same, with 20. The fake was sent nothing for either;
3. at 14:45:09, the wait over: a plain question, the fake's answer after 0.14 s;
4. a question that tells it to refuse with a wait of 100,000 s: a failed turn, and "model fake: its provider said to wait 86400 s, …". The question after it: 429, `Retry-After: 86400`, "… Try again in 24 hours.";
5. the API restarted, and the same question again: the fake's answer, after 0.05 s. The wait was forgotten.

**The page,** in Chromium at http://localhost:8080, the fake alone, once on this tree and once on the first build, alike. A first question that tells the fake to refuse with a wait of 30 s left a failed turn: "The model's server can't be reached, or is too busy. Try again in a moment.", with "Try again". A second question in that conversation, 2.6 s later, was refused with `Retry-After: 28`: the page said "No model can take this message right now.", with a timer that read "in 28 seconds", and 27, 26 and 25 at readings a second apart, and the question was back in the box. The notice went by itself 28.48 s after the refusal, and Enter then had the question answered. The console had one line, the browser's own for a response of 429. The page's source didn't change.

**The Gemini API,** `gemini-3.5-flash-lite` alone, on a free account whose page gives it 15 requests a minute. Short questions, four runs:

1. **the first build, one after the other:** seventeen were answered, in 0.72 to 8.89 s, and the eighteenth, 45 s after the first, was refused after 0.25 s: "model gemini-lite: its provider said to wait 16 s, …";
2. **the first build, 24 one after the other:** each was answered. The answers took long enough that no minute held sixteen requests;
3. **the first build, two at a time,** while the minute still held requests of the run before: the sixth question sent was refused after 0.29 s, "its provider said to wait 8 s". The fifth, sent a second before it, was being written then, and was answered about 2 s after the refusal: "model gemini-lite: it answered before its provider's wait was over, and counts as having budget again". The next question, about 3 s after the refusal, was sent to the provider, which refused it after 0.14 s, "said to wait 5 s": a failed turn, where a refusal for now was due. The review had worked the same case out by reading. The rule for what ends a wait was changed for it;
4. **this tree, two at a time,** from 14:46:00: the eighteenth question sent was refused after 0.33 s, "its provider said to wait 42 s". The seventeenth, sent a second before it, was answered about 2 s after the refusal, and the log said nothing of it: the wait stayed. The question after it, at 14:46:19, was refused by Actio after 0.03 s, with `Retry-After: 40`, and its conversation read 404; another at 14:46:24, with 36: "conversation …: no model has budget left for the request, which is refused for about 36 s: gemini-lite for about 36 s (its provider's own wait)". At 14:47:01, the wait over, a question was answered, after 2.99 s.

**Groq,** `openai/gpt-oss-20b` alone, whose minute takes 8,000 tokens by its headers (Context, above): two made-up questions of 19,950 and 20,423 characters, one after the other, from 14:48:01. The first was answered, after 2.67 s. The second was refused by Groq after 0.27 s: a failed turn, and "model groq-small: its provider said to wait 22 s, and until then it counts as having no budget left". A short question at once was refused by Actio, with `Retry-After: 22`; another 8 s later, with 14; and one at 14:48:29, the wait over, was answered by Groq after 0.96 s. On the first build the same, with 23 and 15. Whether Groq would have taken the short questions during that wait was not tried.

**What the two providers were asked for this step:** flash-lite 68 questions, of which it refused 4; Groq's smaller model 5, four of them long, of which it refused 2. A question refused for now by Actio was sent to nobody.

**The end-to-end tests,** on their own stack: 80 passed, none of them new. None of their questions has the fake refuse with a wait.

**Mistakes planted on purpose,** each alone in a scratch copy of the tree, with the tests that should notice run. On the step's first build, while the review read it, 45: 41 failed a test. The four that failed none:

- **the line of a wait, said rounded down:** every test's wait was a whole number of seconds. The test of that line now names 49.2 s, which is said as 50;
- **a wait in whole seconds that may be 0:** no wait that is kept or looked up is nothing, so "1 at the least" shows only for one made by hand, which a test now makes;
- **a title that reads the providers' waits only when the look at the budgets could be had:** a turn's test held that, and no title's. One was added;
- **a wait noted only before anything of an answer was handed on:** no client raises a 429 after that, so this is no mistake where it was run.

On the tree as it was committed, 51, six of them for the rule that an answer under way ends no wait: 50 failed a test, and the one that failed none is the last above.

**The review,** by a reviewer that had not seen the build, once, and once more for the one part that changed after it. The first read found one fault that had to be fixed, a sentence of the getting-started page that the change had made untrue: that a model which states no limit is never without budget. Of its six smaller points:

- **one changed the rule:** an answer to a request sent before a refusal ended the refusal's wait, which the Gemini API showed the same hour (above);
- **three changed sentences:** that a newer 429 replaces a wait only when it names more than 2 s, that a turn refused with the budgets unreadable is new with this step, and that one person's large question can have another's smaller one refused, each now as "Known, and left" has it;
- **one put back a section's heading** in a test file, which the new tests had taken the place of;
- **one was left:** a turn's test of a model that isn't ready passes without this step, since it holds what was so before, the 503, and the rule's own test holds that only ready models are added. Its neighbour's docstring was corrected.

The second read, of the rule for what ends a wait, found nothing to change in the code, and two points of wording: "noted" where the sentences said "named", since the count is of what Actio has heard, and one more line under "Known, and left".

**Not measured:** a wait of a day named by a provider, which the tests hold by their clock and the fake showed; a chain of cloud models one of which was told to wait; a separate worker's own wait, which is read in the code; and the page in Firefox and WebKit, whose source didn't change.

### 2026-11-27: the list says a spent budget and a provider's wait for the shortest question

`GET /api/models` now says, for each model, what the shortest question to it would meet right now, by the rule a turn is decided by (`Chain.asked_with`), with the reads a turn's start makes: a look at the budgets, for one request of one token, and the providers' own waits. A model without budget left, by the count or by its provider's word, is listed with the stand-in that has some as the model the question would be given to first (`asked_first`); one that is refused for now, how long for, in whole seconds, the very number a question's 429 names in that moment (`retry_after`). One look for the whole list asks Valkey once for every chain (`look_at_chains`), and none when no ready model has a budget. It writes nothing; with Valkey away the list says no count is spent, and nothing goes to the log. A row of the page's menu of models says "At its provider's limit for about 1 minute", or which model answers for now, as the list said it, never counted down ([ADR 0032](0032-the-reader-chooses-the-model-that-answers.md), revision "the list says what a question to a model would meet now, and the menu's rows say it").
