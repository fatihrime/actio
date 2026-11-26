# 0029. A model is passed over when a request is larger than its room

- **Status:** accepted
- **Date:** 2026-11-25

## Context

A model of the models file names the models that stand in for it, asked in its place, in order, when it can't answer before its first word ([ADR 0028](0028-a-model-names-those-that-stand-in-for-it.md)). That decision left the rest of the router for later: "A second try of the same model, the providers' own timeouts, a check of capacity before asking, budgets and a circuit breaker come later." The second try and the timeouts came in its revisions. This is the first half of the check of capacity: whether one request can fit a model at all. The second half, counting what was used in a minute and in a day, is a later step.

Until now every ready model of a chain was asked, in order, and a request too large for one was found out only by asking it:

- **Groq's free plan takes 8,000 tokens a minute for each model, and refuses a request larger than that whole,** with 413, which the client calls a `ContextOverflowError`. Such a request was sent to Groq, refused, and only then was the next model asked.
- **A model on Ollama whose window is too small was sent the request too,** and refused it only once the model was loaded: 3.30 s after the question when it wasn't loaded yet (see Measured).
- **The example's file wrote the tokens a minute in the place of the window:** `context_window = 8000` for Groq's models and `250000` for the Gemini API's, to stand for what their free plans take in a minute. Their own windows are 131,072 and 1,048,576. But the window of a request says which limit an answer reached (`reason_for` in `actio.chat.answering`, [ADR 0014](0014-turns-streamed-in-the-ai-sdks-format.md), revision "a turn says which limit its answer reached"): with a made-up window of 8,000, an answer cut at its longest size, 2,048 tokens, after a prompt of 6,000, is recorded as having filled the context window, which it had not (read in the code, not seen). And an Ollama loads a model by the window a request carries ([ADR 0026](0026-models-loaded-ahead-of-the-first-question.md)). One number stood for two things.

What the choice had to fit:

- **Actio knows a request's tokens only by its own guess,** `actio_llm.prompt_tokens`, before any server has counted them. The guess came to between 0.66 and 1.47 of two local models' own counts ([the model client's page](../reference/llm.md)), and to 1.10 to 1.12 of Groq's on made-up English notes (see Measured).
- **A turn holds its credits by that same guess,** before a model is asked ([ADR 0020](0020-usage-limits-on-turns.md)).
- **Groq was seen to admit a request by its prompt alone,** not by the room asked for its answer (see Measured).
- **Both providers limit each model by itself.** Groq's answers stated 8,000 tokens a minute for each of its two models in the example, and its documentation lists the limits by model, for an organization. The Gemini API named a limit of a model's own when it refused a request (`GenerateContentInputTokensPerModelPerMinute-FreeTier`, ADR 0028's revision of 2026-11-25), and its documentation applies the limits to a project.
- **No turn is refused for what a model's server might do with it:** of the models' states, only none of the chain ready refuses a turn before a model is asked, with the 503 (ADR 0028).
- **The page divides a conversation's tokens by the `context_window` that `GET /api/models` gives** ([ADR 0015](0015-the-web-chat-calls-the-api-from-the-browser.md), revision "the page says how full the model's window is").

## Options considered

**Where the figure is written:**

1. **`context_window` stands in for it,** as the example did.
    - Con: which limit an answer reached, and an Ollama's load, read the window: one number for two things.
2. **A key of the provider.**
    - Con: Groq and the Gemini API both limit each model by itself.
3. **A key of the model, `tokens_a_minute`.** Chosen.

**What "can't fit" compares:**

1. **The prompt and the longest answer,** the plan's first idea.
    - Con: Groq was seen to admit a request by its prompt alone. A request it would take would be passed over.
2. **The prompt alone.** Chosen.
    - Pro: a model that has room for the question and a short answer still answers, as before.

**What happens when the guess says that no model has room:**

1. **Refuse the turn.**
    - Con: the guess was between 0.66 and 1.47 of two local models' own counts, and 1.10 to 1.12 of Groq's. An installation with one local model would refuse questions its model would have answered.
2. **Pass over only what is surely too large: a guess over the room by half.**
    - Con: Groq's 8,000 would then be asked up to a guess of 12,000. The question of ADR 0028's look that Groq refused as too large was 11,431 tokens by the guess: it would still be sent there first.
3. **Pass over by the plain guess while another model has room, and ask them all when none has.** Chosen: the guess chooses who is asked, and never whether a turn fails.

**What `GET /api/models` says:**

1. **The window, with the room as a new member.**
    - Con: the page would have to learn it, for the one line under the box.
2. **The room, under `context_window`.** Chosen: it is what the example's file said there before.

## Decision

**A model in the models file may say how many tokens its server takes from the installation's account in a minute, `tokens_a_minute`. Its room for one request is the smaller of that and its context window. Before a chain is asked, a ready model whose room is smaller than the request's prompt, by Actio's guess, is passed over, with a line in the log, while another ready model of the chain has room for it. When none has, every ready model is asked, as before: a guess never refuses a turn.**

**The file** (`config/models.toml`, `actio.catalog`):

- **`tokens_a_minute`,** in a `[models.<name>]` table, for either kind of provider. It is the provider's own figure, not a share of it: this step compares one request with the whole figure. Whether to stay under it by a margin is for the step that counts what was used in a minute.
- **A whole number of 1 or more.** A string, a fraction (`8000.5`, and `8000.0` too), `true`, 0 and a negative number are each a mistake in one line that names `models.<name>.tokens_a_minute`, as the file's other mistakes are.
- **Nothing ties it to `context_window` or `max_output_tokens`:** either may be the smaller.
- **Left out,** the server has no such limit, and the room is the window. `config/models.toml` describes the key and names no value: its one model is local. The end-to-end stack's file names none either.

**The room** is written once: `OfferedModel.tokens_a_minute`, and `OfferedModel.room`, the smaller of `context_window` and `tokens_a_minute`, or the window alone.

**The rule** is written once too, `Chain.asked_with(tokens)` in `actio.catalog`, which gives the models to ask, in the chain's order, each with its status, and those passed over for their room (`Asked`):

- a model **has room** when the number is no larger than its room;
- while at least one ready model has room, those that haven't are passed over, and the others are asked, in the chain's order;
- when none has, every ready model is asked, in order, and none is passed over;
- only ready models count. A model that isn't ready is passed over as before, whatever its room, and never counts as one that has room.

**The number is Actio's guess at the prompt,** `actio_llm.prompt_tokens` of the messages the models are sent: for a turn, the instructions and the conversation as it goes to a model, every earlier question and answer of its branch with the new question. Never the room an answer needs, nor a server's count. In a turn it is computed once, and is the very number the credits are held by.

**A turn** (`actio.api.turns.start_turn`) asks the models the rule gives. Everything else is as before:

- the 503 when no model of the chain is ready;
- the 422 for reasoning, which is the first ready model's to say, whether or not that model is then passed over for its room;
- the credits, held for the asked model, at its price and up to its longest answer, and the turn's `model`, the asked one;
- each model's request: its own name, its own window and not its room, `thinking` only when it can reason, and no more output than its own longest answer nor than was held.

A model passed over for its room is said in the log, in a warning, beside the lines for models that aren't ready, with numbers alone, never a word of the question:

> `turn …: model groq-large has room for 8000 tokens, and the request is about 11494: it is passed over`

When none has room, nothing is said, and nothing changes.

**The title job** (`actio.chat.titles`) follows the same rule, by the guess at its own request, the one message it sends, its instructions with the conversation's first question, and says `conversation …: model … has room for … tokens, and the request is about …: it is passed over`. Everything else of it is as before: none ready, the walk, which errors put its job back, the 30 seconds.

**`GET /api/models`** gives a model's room as its `context_window`, and the API's description says so. The page divides by it, for the line under the box, and no code of the page changed.

**Unchanged:** the warm-up, the gates, the walk (`actio_llm.router`), the model client, the stream and the turn's record. No migration.

**The example** (`config/examples/free-cloud/models.toml`): Groq's two models have `context_window = 131072` and `tokens_a_minute = 8000`, and the Gemini API's two `context_window = 1048576` and `tokens_a_minute = 250000`. So a question of 11,000 tokens by the guess, to `groq-large`, goes to flash-lite alone when flash-lite is ready: Groq's room is 8,000, and the local model's window 8,192.

**Tests:** the key read for a model of each kind of server, and left out; its six mistakes; the room smaller by the minute, smaller by the window, equal, and the window alone; the rule at a room's edge, with some models that have room, none, a stand-in without room, and a model that isn't ready with the only room large enough; the three files' values (`test_catalog.py`). The listing (`test_catalog_api.py`). A turn whose asked model is passed over, by its tokens a minute and by its window alone, with the stand-in's request, the price, the record and the log's line, its number the guess at the very messages the stand-in was sent; none with room; a model with room for the request and not for the request with its longest answer, asked all the same; a stand-in without room, and the asked model's failure the turn's; three models with the middle one too small; the 422 for reasoning, and a turn that may reason answered by a stand-in that can't; a model that isn't ready and one without room in one chain (`test_turns_stand_ins_api.py`). A title written by a stand-in when the named model has no room; the named model asked when none has, with its own window in the request; and a stand-in without room not asked for a title its model couldn't give for now, which is asked for again later (`test_titles.py`).

## Consequences

- **Easier:**
    - a request too large for Groq's free plan goes to a model with room, without a request that Groq refuses first;
    - a request too large for a local model's window isn't sent to it while another can take it, and its server doesn't load the model to refuse it;
    - the example's windows are the models' own: an answer cut at its longest size is recorded as such.
- **Harder:**
    - **a guess decides who is asked,** and it can be wrong near a room's edge either way (below);
    - **the line under the box divides by the room:** for Groq's models it reads "of 8,000 tokens", though their window is 131,072, as it did with the example's old file (ADR 0028's look: "150 of 8,000 tokens").
- **Known, and left:**
    - **a request near a model's room may be passed over though its server would take it** (seen once, see Measured: 8,198 tokens by the guess, which Groq then took as 7,323), **or asked though its server refuses it** (worked out), since the guess is not the server's count. How near is as far as the guess is out: about a tenth for Groq's models on made-up English notes, and as much as a third under or a half over for the two local models' counts of other languages, code and numbers (seen: the two sets of ratios above). So a model on Ollama that stands in for another may be passed over for a request its window would hold, or asked for one it then refuses;
    - **worked out: the guess isn't put right by what a provider counted** for the conversation's earlier turns;
    - **worked out: when no model has room every one is asked,** so a request that is too large for all of them is refused by each, as before;
    - **worked out: a turn that asks for reasoning is refused when its first ready model can't reason, also when that model would be passed over for its room** and the model that would answer can reason; and when the first ready model can reason and is passed over, a stand-in that can't answers, without a thought;
    - **by the Gemini API's documentation its tokens a minute are input tokens.** A request larger than its minute's was not sent on purpose, so what it answers is not known (ADR 0028's look saw 503 and 429 for prompts near the limit);
    - **nothing is counted over a minute or a day yet:** a model that has room for a request by itself is asked though its minute is spent, and refuses it (429), as before. The next step counts;
    - **the page says nothing of a model passed over,** but who answered (ADR 0028, revision "a turn says which model answered, and the page says so"), and the log says why.
- **How to reverse it:** for an installation, leave `tokens_a_minute` out: the room is then the window, and only a model whose window is smaller than a request is passed over. In the code: `ModelEntry.tokens_a_minute`, `OfferedModel.tokens_a_minute` and `room`, `Chain.asked_with` and `Asked` in `actio.catalog`; the models asked back to `chain.ready` in `actio.api.turns.start_turn` and `actio.chat.titles._ask`, with their log lines; and `ModelOut.context_window` back to the window, with `make openapi`.

## Measured

On 2026-11-25. Each fact says how it was got.

**Before the build:**

- **Seen, Groq's free plan, `openai/gpt-oss-120b`:** the same made-up prompt of 41,341 characters was refused with 413 twice, once with room for an answer of 16 tokens and once with 1,016: "Limit 8000, Requested 9851" and "Requested 9863". So the room asked for an answer isn't counted at the door.
- **Seen, the same day, `openai/gpt-oss-20b`:** a prompt it then counted as 6,203 tokens, with room for an answer of 2,048, was taken (200) on an unused minute: together they pass 8,000. And a prompt of 7,442 characters with room for 7,000 was taken, 1,817 tokens by its count ([ADR 0027](0027-cloud-models-through-their-openai-compatible-layers.md), "Measured").
- **Seen:** Groq refused one request as 4,057 tokens, with 429, its minute having fewer left, and counted the same prompt as 3,932 when it took it a second later: its figure at the door was 1.03 of its count, once.
- **Seen, three requests:** Actio's guess (`prompt_tokens`) for made-up English notes with numbers in them was 6,915, 4,188 and 4,321, where Groq counted 6,203, 3,816 and 3,932: 1.10 to 1.12 of the count.
- **Seen, the same day, the running stack with the shipped models file, one guest through the API:** a made-up question of 40,085 characters to the local `qwen3.5:4b-q4_K_M` (window 8,192) failed as `context_overflow` after 3.30 s when no model was loaded, Ollama having loaded the model first, and after 0.09 s when it was asked again with the model loaded.
- **By Groq's documentation** (its rate limits page, read 2026-11-25): limits are requests and tokens a minute and a day, at the level of the organization; "Cached tokens do not count towards your rate limits"; the page names 429 for a limit that is passed, and says nothing of 413.
- **By the Gemini API's documentation** (the same day): "Tokens per minute (input) (TPM)"; "Rate limits are applied per project, not per API key".

**On the real stack,** built from the tree as it was committed, with a models file made for the look: the example's chain, with Groq's `openai/gpt-oss-120b` as the default, room 8,000, and `gemini-3.5-flash-lite`, room 250,000, then the local `qwen3.5:4b-q4_K_M`, window 8,192, standing in for it. One guest, with made-up questions, through the API unless it says the page.

| What was asked | What came back, and the API's log |
|---|---|
| `GET /api/models` | `context_window` 8,000 for Groq's two models, 250,000 for the Gemini API's two, 8,192 for the local one |
| A question of 45 characters | Groq's own answer, the turn over after 1.19 s, 158 tokens. Nothing in the log |
| ADR 0028's question of 39,893 characters: 11,494 tokens by the guess, with the instructions | flash-lite's answer, which counted 12,828 tokens read, its first piece after 9.73 s. The log: "model groq-large has room for 8000 tokens, and the request is about 11494: it is passed over", the same of `standard` with 8192, then "model gemini-lite answered in place of groq-large". Nothing was sent to Groq, where before this step the same question was refused first |
| The same question on the page, in Chromium (Playwright 1.65), 40 s later | flash-lite's answer, the turn over after 1.39 s. Under it "first word after 1.3 s", "3,230 credits" and "answered by Gemini 3.5 Flash-Lite"; under the box "5% full · 12,850 of 250,000 tokens". No request of the page's was refused, and no script on it failed |
| A question of 30,635 characters: 8,198 tokens by the guess, a little more than Groq's room | Passed over as well, with the same two lines, and flash-lite's answer after 6.98 s: it counted 8,363 tokens read |
| The same messages, the instructions and that question, sent straight to Groq 18 s later, once | Taken: answered after 0.78 s, counted as 7,323 tokens. The guess was 1.12 of Groq's count: Groq would have answered a request that Actio passed over |

The two waits of 9.73 and 6.98 s were flash-lite's own: nothing else was asked for those turns, and the first of the two questions took 1.39 s on the page.

**The local model passed over for its window,** on the same stack with a second file: the local model as the default, and flash-lite standing in for it. The question of 39,893 characters, once: flash-lite's answer, the turn over after 2.93 s. The log: "model standard has room for 8192 tokens, and the request is about 11494: it is passed over", then "model gemini-lite answered in place of standard". Ollama's own log has one request in that time, answered with 200 in 0.40 s as the turn ended: the title's, by the worker's log. Before the build, the local model alone took 3.30 s to refuse such a question when it wasn't loaded, and 0.09 s when it was (above).

The looks took two requests of Groq's, one of them the one sent straight, and four of flash-lite's.

**The end-to-end tests,** on their own stack: 79 passed, none of them new. Nothing of the page changed, and that stack's models have no `tokens_a_minute`.

**Mistakes planted on purpose.** After the build and the review, 37 mistakes were planted one at a time in a scratch copy of the tree, and the tests that should notice each were run: 34 failed a test. The three that failed none:

- **a turn's rule given the prompt and the longest answer.** The rule's own edge was held, but no turn had a model with room for the request and not for the request with its answer;
- **the title job's request carrying the model's room in its window's place.** The review found the same by reading: no title's test looked at the request of a model whose room is smaller than its window;
- **the title job's "no other is left to ask" said by how many models were ready,** not by how many were asked.

A test was written for each after that run. On the tree as it was committed, all 37 failed a test.

## Revisions

### 2026-11-25: what is used in a minute and a day is counted

"Known, and left" said that nothing was counted over a minute or a day yet, and that the next step would count. [ADR 0030](0030-the-providers-budgets-are-counted.md) is that step: a model may state its provider's requests a minute and a day and tokens a day beside its tokens a minute, Actio counts in Valkey what it sends to the model, and a ready model whose budget has nothing left for a request is passed over while another model of the chain has budget. The rule of the room comes first and is as it was: `tokens_a_minute` is still the whole figure for a model's room, and a model passed over for its room isn't also said to be over its budget. `Chain.asked_with` takes what the look at the budgets said as its second argument.

### 2026-11-26: a count refuses a turn for now; a guess still doesn't

"A guess never refuses a turn" holds: when no ready model has room for a request, every one is asked, as before, whatever their budgets. A count now refuses one for now: when no model with room for it has budget left for it, the turn is answered 429 with how long to wait, and only the models with room count for that ([ADR 0030](0030-the-providers-budgets-are-counted.md), revision of 2026-11-26).
