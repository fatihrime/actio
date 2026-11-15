# 0020. Usage limits on turns: credits held before an answer, and the ledger written before the counters

- **Status:** accepted
- **Date:** 2026-11-14

## Context

Actio has an engine for usage limits ([ADR 0006](0006-usage-limits-engine.md)): it holds credits for a request, counts what the request used, and can be rebuilt from a ledger. Nothing used it. Turns were answered without limit ([ADR 0014](0014-turns-streamed-in-the-ai-sdks-format.md)), and a model had no price ([ADR 0013](0013-the-models-on-offer-are-a-file.md)). This decision connects the three.

- **What a turn costs is known only when it has ended,** and some turns never say. A reader who leaves stops the model, and a stream that is closed never sends its token counts. Some servers report none at all.
- **The counters can be lost.** They live in Valkey, in memory. The engine was built for that: counters that are missing are rebuilt from the ledger.
- **A refusal has to be clean.** A question that doesn't fit must not leave half a turn behind: a stored question with nothing to answer it.
- **The model is a small one on a laptop.** It writes about 40 tokens a second. Limits sized for a data centre would never be reached, and a limit nobody can reach shows nothing.
- **Whoever runs an installation decides** what a model costs and how much people may use, without changing code.

## Options considered

**Where prices and plans are written:**

1. **In the code,** or in environment variables. A price list with several plans doesn't fit either.
2. **In the database,** edited on admin pages. No restart needed, but the pages don't exist yet, and the first start needs prices before anyone can open them.
3. **In a file,** beside the models file and in the same format.

**How much a credit is:**

1. **About a thousand tokens.** A turn then costs two or three credits, so rounding up, which happens once per request, adds a third to a small turn. And a session of tens of thousands of credits is tens of millions of tokens: days of this laptop writing without a pause.
2. **About one token of an answer.** A turn costs hundreds of credits, rounding is lost in them, and a limit reads as what it is: so many tokens of answers.

**When a turn is admitted:**

1. **After its question is stored.** The cost of a turn depends on the conversation behind it, which is read when the turn starts. But a turn that is then refused leaves its question behind, with a turn that must be ended somehow.
2. **Before anything is committed,** in the transaction that starts the turn. A refusal rolls it back.

**What a prompt costs before any model has counted it:**

1. **Its bytes, three to a token.** Simple, and wrong where it matters: see "Measured".
2. **The model's own tokenizer.** Exact, but every provider has another, and not every provider can be asked.
3. **A guess by a few rules, with a margin,** corrected by the provider's count when the turn ends.

**What a turn costs that ended without a count:**

1. **Nothing.** Then stopping an answer at its last word makes it free.
2. **The pieces that arrived, counted.** Exact where a server sends a token at a time, as Ollama does. Other servers send several tokens in a piece, and would be undercounted many times over.
3. **A guess from the text that was written.** The same for every provider.

**What a failed turn costs:**

1. **What it had written.** But the fault was the model's, or Actio's own.
2. **Nothing.**

**Which is told first, the counters or the ledger:**

1. **The counters.** A crash between the two leaves usage in the counters that no ledger row explains. After the next rebuild it is gone, and nobody can say which number was right.
2. **The ledger.** A crash between the two leaves usage in the ledger that the counters miss until the next rebuild. The ledger is never behind.

**Whether Valkey keeps its data on disk:**

1. **Yes.** A restart brings the counters back. But what comes back is as old as the last write to disk: counters that are there, and stale. The engine rebuilds only what is missing, so nothing would ever correct them.
2. **No.** A restart always means "missing", and every account is rebuilt from the ledger the next time it is needed.

**Whose clock the engine is told:**

1. **Valkey's own,** asked before each operation. One clock for every process, at the price of one more round trip, and of a second idea of "now" beside the one the rest of the application has.
2. **The application's,** as everywhere else: the time a request arrived, which tests can move.

**How the ledger is kept from being changed:**

1. **By nothing but care.**
2. **By a database role** that may only add and read. A second role, a second password, for every installation.
3. **By a trigger** that refuses to change or delete a row, whoever asks.

## Decision

**A turn holds the credits it may cost before a model is asked, and is refused if they don't fit. When it ends, what it used is written to the ledger with its outcome, and then counted. Prices and plans are a file.**

**The file,** `config/plans.toml` (`actio.usage.plans`):

- **A price for every model on offer,** in credits per million tokens: for what the model reads, for what it writes, and for what it reads again from its server's cache.
    - **A credit is one token of an answer** at the default prices. Reading costs a quarter of that, and reading from the cache a tenth of reading.
    - **A model priced at nothing is free.** It is never refused, and what it used is still recorded.
- **Plans:** how many credits a session allows, and a week. Guests get one plan and accounts another, named in the file. A limit that is left out doesn't exist; a plan with neither limits nothing.
- **Two windows,** for every plan: a session, which opens with the first request and lasts five hours, and the week, which starts over at a moment the file names, such as Monday 00:00 in Istanbul.
- **The file is read when the API starts,** and a mistake stops it with one line, as for the models file. The two are checked against each other: a model without a price stops the start, and so does a price for a model that isn't offered, which is most likely a misspelt name. A model can't become free by being forgotten.
- **By default** a guest may use 25,000 credits a session and 100,000 a week; an account 100,000 and 600,000. At 40 tokens a second, 25,000 credits are about ten minutes of the model writing.

**Before a model is asked** (`actio.api.turns`, `actio.usage.limits`):

- **The turn is started, and its credits are held, in one transaction.** The conversation, the question and the turn are written but not committed. Then the engine is asked to hold the turn's largest cost in both windows. Refused, the transaction is rolled back: the question was never asked, and can be sent again unchanged.
- **The largest cost is a guess at the prompt, plus the longest answer the model allows.**
    - The guess counts a digit or a punctuation mark as a token, and other text at four bytes to a token (`actio_llm.estimate`).
    - A quarter more than the guess is held, because the guess can be short.
- **Near a limit an answer gets less room,** down to 256 tokens. The model is told the smaller limit, and the turn says so (`capped_by_usage`), so that a page can explain an answer that was cut. With less than 256 tokens left, the turn is refused.
- **A refusal is a 429** in the usual error format. It names the window that is full, says when that window starts over, in words and as `resets_at`, and lists the models that cost nothing. `Retry-After` says it in seconds.
    - When both windows are full, it names the one that starts over last.
    - A message that alone needs more than a whole window allows is refused without a time: waiting won't help.
- **The hold lasts ten minutes,** as long as a turn may stay `running` ([ADR 0014](0014-turns-streamed-in-the-ai-sdks-format.md)). A turn whose process dies settles nothing, and its credits come back by themselves.
- **Without Valkey, turns answer 503.** A limit that can't be counted isn't waived.

**When the turn ends** (`actio.chat.answering`):

- **One transaction writes how the turn ended and what it used:** the turn's outcome, the answer, and a row in the ledger. Only after it is committed are the counters told.
- **What is counted:**

    | The turn | Costs |
    |---|---|
    | Completed, and the provider counted | What the provider counted |
    | Stopped by its reader, or completed without a count | The guess at the prompt, without the margin, and a guess at what was written. The ledger marks it as estimated |
    | Failed, at any point | Nothing. What it had written is kept |
    | Stopped before the model wrote anything | Nothing |

- **If the counters can't be told,** the turn is still recorded and its answer still delivered. The log says so. The hold runs out by itself, and the usage is in the ledger for the next rebuild.
- **If the outcome can't be written,** nothing is counted, and the hold is given back.
- **The turn's record gains what it cost** (`credits`), beside its tokens and its times.
- **Passing 50, 75 or 90 percent of a window is said once,** in the stream of the turn that passed it: a chunk of Actio's own, `data-usage-warning`, just before the stream finishes. The engine keeps track of what was said.

**The ledger** (`usage_ledger`):

- **A row per turn that is counted,** a free model's turns among them: whose, which model, the credits, the tokens, whether they were estimated, and the windows that admitted it. Its key is the turn's own id, so a turn can't be written twice.
- **Rows are only added.** A trigger refuses UPDATE, DELETE and TRUNCATE.
- **A row names an account by its id, without a foreign key.** A guest's account is deleted after a day, and its rows stay, naming an id that belongs to nobody.

**The counters:**

- **Valkey keeps nothing on disk:** no snapshots, no append-only file, no volume. A test of the container rules fails if one comes back.
- **Missing counters are rebuilt when they are next needed,** by a turn or by `GET /api/usage`, from the ledger's rows since the oldest window that is still open.
- **The engine is told the application's time,** as the rate limiter is ([ADR 0011](0011-rate-limits-as-token-buckets-in-valkey.md)). The engine was built to be told the time, and to stay right when two callers disagree about it ([ADR 0006](0006-usage-limits-engine.md), revision of 2026-11-10). Today every process is on one machine and reads one clock.

**`GET /api/usage`** answers with the caller's plan and its windows: the limit, what is used, what is held for answers being written, and when each starts over.

## Measured

On a MacBook Pro (M1 Pro, 16 GB), 2026-11-14.

**The guess at a prompt's tokens,** as guess ÷ the server's own count, for whole prompts on Ollama:

| | English | Turkish | Python | Japanese | JSON full of numbers |
|---|---|---|---|---|---|
| `qwen3.5:4b`, the default model | 1.05 | 0.90 | 1.11 | 1.26 | 1.06 |
| `granite4:micro` | 1.05 | 0.70 | 1.16 | 0.66 | 1.47 |
| Bytes ÷ 3, for the text alone, on the default model | 1.56 | 1.25 | 1.14 | 1.74 | **0.46** |

- **Counting bytes came out at under half for a page of numbers.** The model reads every digit as a token. That is why digits and punctuation are counted one by one.
- **No rule fits every tokenizer.** The second model splits Turkish and Japanese finer, and keeps digits together. With a quarter held on top, the default model's prompts were always covered; the second model's Turkish and Japanese were still short, by an eighth and a sixth.
- **What a short guess costs:** a prompt is charged a quarter of a credit a token, so even the worst case here, on a prompt that fills the model's window, is a few hundred credits past a limit. They are counted, not lost.

**Through the proxy, with the default model:**

| | |
|---|---|
| First event of an answer, model not loaded | 0.03 s (0.02 s before this change) |
| First event of a follow-up | 0.02 s (0.01 s before) |
| `GET /api/usage`, the median of 20 | 5 ms |
| A question answered in 56 tokens, after 68 read | 73 credits |
| Its follow-up: 141 read, 64 of them from the cache, 24 written | 45 credits |

- **Valkey was restarted** after those two turns. It came back with no keys. The next `GET /api/usage` answered as before the restart: 118 credits used in both windows, and the same moments to start over.
- **With the guest's session set to 350 credits,** the third question was refused: 429, "You've reached your usage limit for this session. It resets in 5 hours.", and no row was written for it anywhere.

**Six mistakes were planted,** to see the tests fail:

| The mistake | Tests that failed |
|---|---|
| The counters told before the ledger is committed | The turn whose outcome can't be written, and the warning said once |
| The question committed before it is admitted | Four: a refusal that leaves something behind |
| A failed turn charged for what it had written | One |
| An answer without a count taken for free | Two: the stopped answer, and the model that reports nothing |
| The model not told its smaller room | One |
| A rebuild that reads the ledger only since the newest window opened | One |

The first of them had no test at first. Listing the mistakes to plant showed the gap: nothing checked a turn whose outcome can't be written. The test was written then, and failed with the mistake in place.

## Consequences

- **Easier:**
    - a model's price, a plan's limits and the week's reset change in a file, with a restart;
    - whatever becomes of Valkey, the limits come back as the ledger has them;
    - a turn's record says what it cost, and the ledger says what anyone used, in tokens too;
    - other things that cost something, such as the developer gateway's requests, hold and settle the same way.
- **Harder:**
    - **two files have to agree.** Offering a model means pricing it, or the API doesn't start;
    - **a turn now needs Valkey twice,** to be admitted and to be counted. Without it no turn starts;
    - **an account can pass a limit by a little.** A prompt can be larger than was held for it, and several answers written at once are each counted for what they really used;
    - **estimated counts are guesses,** as far off as the table above shows: on the default model a tenth low for Turkish, and a quarter high for Japanese;
    - **the ledger grows with every turn,** and nothing removes a row.
- **Not done yet:**
    - **the page shows none of this.** The meter, the countdown when a limit is reached and the offer of a free model come next;
    - **nothing settles a turn whose process died.** Its hold runs out, and what it had written was lost with it ([ADR 0014](0014-turns-streamed-in-the-ai-sdks-format.md)). A job that goes through stale turns comes with the worker process;
    - **nothing repeats a count that Valkey missed.** Usage that was written to the ledger while Valkey was away reaches the counters only at the next rebuild. The same job will repeat it, and a nightly comparison will say how far the two are apart;
    - **a plan per account.** Every account has the plan the file names. Changing one account's plan, or granting it credits, comes with the admin pages; so does applying such a change at once;
    - **a guess that learns.** The rules are the same for every model. Correcting them per model, from what providers report, comes with the router;
    - **a guest who starts over as a new guest** has a new allowance. New guests are limited per network address ([ADR 0011](0011-rate-limits-as-token-buckets-in-valkey.md)); a public installation has to set that limit low;
    - a limit on how many answers one account has written at once, and a hold that is renewed while a long answer is written: with turns that run apart from their request;
    - removing old rows from the ledger, and an account's own from it when the account is deleted.
- **How to reverse it:** the turn's code calls four things: `reserve`, `record`, `settle` and `release` (`actio.usage.limits`). Limits kept somewhere else, or none at all, change what those four do, and the ledger stays as the record of what was used.
