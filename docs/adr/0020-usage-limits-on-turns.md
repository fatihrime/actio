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

## Revisions

### 2026-11-15: the page shows the limits

"Not done yet" began with the page, which showed none of this. It does now: a meter, the limit that is reached with the time left, the model that costs nothing, and the shares passed on the way ([ADR 0021](0021-usage-limits-in-the-page.md)).

- **Nothing in the API changed for it.** The page reads `GET /api/usage`, and learns of a limit from the 429 described above.
- **Two tests were added here,** because the page's tests count on them:
    - a refusal names the model that costs nothing, and that model then answers the same question, for no credits. Until now only the limits below the API were tested for that;
    - the refusal itself is recorded as a file, `limit_reached.json`, beside the recorded streams ([ADR 0014](0014-turns-streamed-in-the-ai-sdks-format.md)). Its `window`, `resets_at` and `alternatives` are in no generated type, so the page's tests answer with the real document.

### 2026-11-16: a hold that is renewed, and two more turns that cost nothing

Turns now run apart from their requests ([ADR 0022](0022-turns-that-run-apart-from-their-request.md)). "Not done yet" named what that would bring here: "a hold that is renewed while a long answer is written". Four things changed in what is decided above.

- **The hold lasts one minute, and is renewed every 15 seconds** while the answer is written (`UsageLimits.extend`). It was ten minutes, once. A process that dies stops renewing, and its credits are back a minute later instead of ten. An answer can now take longer than any fixed hold.
- **"Stopped by its reader"** in the table of what is counted means a request to stop, where it meant a reader who left. Leaving stops nothing now, so an answer that nobody reads is written to its end and counted whole.
- **Two more rows for that table,** both costing nothing:

    | The turn | Costs |
    |---|---|
    | Interrupted by a shutdown | Nothing. What it had written is kept. Its reader didn't stop it: Actio did |
    | Ended after it had been given up on | Nothing, and its answer isn't stored |

    The second was charged before: its ledger row was written although its answer was dropped. It took a turn that outlived ten minutes to show, which no test did and no answer here ever took. With a turn given up on after one silent minute, it had to be right.

- **A conversation deleted while it is answered** had its answer written to the end, for a conversation that was gone, and counted whole. The API now stops the turn before it deletes, so it is counted like any stopped one: by guess, for what it had written.

Not changed: the order of ledger and counters, what a refusal says, and a hold that nobody settles still coming back by itself.

- **Still not done:** a limit on how many answers one account has written at once, which comes with the gate in front of a local model; and settling a turn whose process died, which comes with the worker.
- **Found on the way:** a hold that ran out still shows as `reserved` in `GET /api/usage` until the account's next question. The engine clears expired holds when it makes a new one ([ADR 0006](0006-usage-limits-engine.md)), not when it is only read. Nothing is refused for it, and the page's meter shows what was used.

### 2026-11-18: a plan limits how many answers are written at once

"A limit on how many answers one account has written at once", under "Not done yet" and in the revision above, is done, with the gate in front of a local model ([ADR 0023](0023-answers-that-wait-their-turn.md)).

- **The plans file has a third limit:** `[plans.<name>]` takes `answers_at_once`, how many answers one account may have being written at the same time, in all its conversations. Left out, there is none, as for the other two. So "a plan with neither limits nothing", above, is now a plan with none of the three. A guest may have 2 by default, an account 3.
- **It is no window, and holds no credits.** It counts the account's turns that are running, from their rows in the database, and refuses one more before anything is held: a 429 with `at_once`, and without `window`, `resets_at` or `Retry-After`. A refusal at a usage limit is as it was.
- **"Several answers written at once are each counted for what they really used"**, under "Harder", still holds, for fewer of them.

### 2026-11-19: the worker gives up on turns whose process died

"Nothing settles a turn whose process died", under "Not done yet", is answered by the job that goes through stale turns, which came with the worker ([ADR 0022](0022-turns-that-run-apart-from-their-request.md), revision "the worker gives up on turns nobody looks at").

- **A turn whose process died is now given up on by the worker too,** every ten minutes, wherever it is, and not only when someone looks at its conversation. It costs nothing, as any interrupted turn: no row is written to the ledger for it. Its answer was never recorded, so there is nothing to count.
- **Its hold needs no job.** The hold lasts a minute from the turn's last beat, and so does the turn's life: by the time the turn is stale, the hold has run out by itself. The job gives nothing back, and the worker has no usage-limits engine.
- **A turn that ends after the worker gave up on it** is left as it was, and isn't charged, as one that someone looking gave up on (the revision of 2026-11-16 above).
- **Still not done:** repeating a count that Valkey missed. "Not done yet" gave it to the same job; this one doesn't do it, and it is still open, with the nightly comparison. And a hold that ran out still shows as `reserved` in `GET /api/usage` until the account's next question, as found on the way in the revision of 2026-11-16.

### 2026-11-19: a turn whose process died keeps what it had written, and still costs nothing

"What it had written was lost with it", under "Not done yet", and "its answer was never recorded", in the revision above, no longer hold. A running turn saves what its answer holds with every beat, and a turn whose process died is given up on with that as its answer: what it had written by its last beat ([ADR 0022](0022-turns-that-run-apart-from-their-request.md), revision "what a dead process had written is kept").

- **It still costs nothing,** as any interrupted turn, whoever gives up on it: no row is written to the ledger for it, and nothing in `actio.usage` changed. Its reader didn't stop it, and the fault was Actio's own.
- **Its hold needs no job,** as before: it has run out by itself by the time the turn is stale.
- **"Ended after it had been given up on: nothing, and its answer isn't stored",** in the table of the revision of 2026-11-16, now means what the turn wrote after its last beat. What that beat had saved was kept as its answer when it was given up on. When it ends after all, nothing is added to that, and nothing is charged.
- **One more row** for the table of what is counted:

    | The turn | Costs |
    |---|---|
    | Given up on, because its process died | Nothing. What it had written by its last beat is kept |

### 2026-11-20: the counters catch up

Two things were still wrong between the ledger and the counters, and both were named as open here:

- **"Nothing repeats a count that Valkey missed",** under "Not done yet", which gave it to the job that goes through stale turns. That job came without it, as the first revision of 2026-11-19 says.
- **"A hold that ran out still shows as `reserved` in `GET /api/usage` until the account's next question",** found on the way in the revision of 2026-11-16, and said again at the end of the first revision of 2026-11-19.

Both are done, and nothing in the engine changed for them ([ADR 0006](0006-usage-limits-engine.md)).

**A hold that ran out is no longer shown as held** (`UsageLimits.status`):

- **Reading the usage gives back the account's holds that have run out,** before the windows are read, as a question does first: the engine's `collect_expired`, which nothing called until now. Up to 32 a read; the next read takes the next ones.
- **A Valkey that doesn't answer** is still a 503.
- **A hold that is renewed** is shown as held until its answer ends, as before.

**What a settle couldn't tell the counters is told again.** Until now, when the counters couldn't be told, the turn was recorded and its answer delivered, the log said so, and the usage reached the counters only at their next rebuild: for counters that survive, maybe never. The same when the process died between the ledger's commit and the settle.

Options considered:

1. **Go back over the ledger's last hour at every run,** and settle each row again. Simple. Every row is repeated many times, and when Valkey is away for longer than the look-back, the rows from the start of that time are never repeated.
2. **Keep a mark of how far the ledger has been repeated,** in Valkey or in Postgres. Every row is repeated once, by a job that then has to keep up with every turn. And it rests on rows appearing in the order of their times: a row whose transaction commits late lands behind the mark, and is never looked at.
3. **Note only the settles that failed.** Nothing extra for a turn that goes well. But the note has to be written at the very moment something is failing, and nothing notes a process that died between the ledger's commit and the settle.
4. **A row beside each ledger row, written with it, removed once the counters were told.** What has to happen after a failure is arranged before it. It is exact: no window of time to choose, no order of rows to rely on. The job finds only what was really left over. The price is one insert and one delete for every turn, and one more table.

The fourth is chosen, a transactional outbox: whatever happens after the ledger's commit, the note of what is left to do was committed with it.

**The note** (`usage_uncounted`, `Uncounted` in `actio.usage.models`, migration 0011):

- **A row for each row of the ledger that hasn't reached the counters,** as far as the database knows. It holds only the ledger row's id, as its key and as a foreign key: who used what stays in the ledger alone, so the two can't disagree.
- **Written with the ledger's row** (`ledger.add`), in the transaction that records how the turn ended. Every row of the ledger has one, a free model's too.
- **Taken away once the counters have answered.** `UsageLimits.settle` now says whether they were told: `None` when Valkey didn't answer, otherwise the warnings, possibly none. Any answer counts as told, one that changed nothing too: after `unknown`, `settled` or `released` there is nothing a repeat could add. After such a settle the turn takes its note away (`UsageLimits.counted`), in a short transaction of its own.
- **That transaction can't fail the turn.** When it can't be written, the log says so, as a warning with the turn's id, and the turn ends as ever: recorded, counted, and with the warnings for the shares of a window it passed. The job then settles it again, is answered `settled`, and takes the note away.
- **After a settle that didn't reach the counters,** the API does nothing more: the note stays, and the settle's own warning in the log is as it was.
- **Unlike the ledger's, these rows are deleted,** and no trigger guards them. A plain `TRUNCATE usage_ledger` is now refused for the foreign key, before the trigger is asked; with `CASCADE`, the trigger refuses it as before.
- **The rows the ledger held before the migration got none.** Whatever they missed, their next rebuild counts, as before.

**The job** (`count-missed-usage`, `worker.COUNT_MISSED`, running `actio.usage.missed.count_missed`):

- **Every ten minutes,** like the worker's other two. It takes the notes whose ledger rows were written a minute ago or longer (`COUNTED_WITHIN`), oldest first, 100 at most (`BATCH`), by a statement of its own that holds only the notes, and skips those another transaction holds. It settles each row again, in that order, at the job's time, then takes away the notes of the rows the counters answered, in the next statement ([ADR 0011](0011-rate-limits-as-token-buckets-in-valkey.md), revision of 2026-11-19). One batch a run.
- **The minute is left to the process that wrote the row.** Its settle has been answered, or has failed, a few seconds after the ledger's commit: the Valkey client makes no retries, and gives up after two seconds. A job that settled first would do no harm to the count, but the turn's own settle would then be answered "not applied", and the warning it says in its stream would be lost.
- **What it does with each answer:**

    | The engine answers | Because | The job |
    |---|---|---|
    | Applied | The counters still held the request, or its hold had run out | It is counted now, and nothing is held for it. The note goes |
    | `settled`, not applied | It was counted already: only the note was left | Nothing is counted twice. The note goes |
    | `unknown` or `released`, not applied | The counters were lost and rebuilt since, or forgot the request, or it had been released | Nothing a repeat could add. The next rebuild counts it while its window is open. The note goes |
    | Nothing: Valkey didn't answer | | It stops at that row: each further try would wait out its timeout. That row and the rest of the batch keep their notes, for the next run |
    | Nothing: the engine refuses the row before it asks Valkey | More credits than the engine counts exactly, which takes a model's server that reports billions of tokens for one answer | Nothing a repeat could add: the engine would refuse at every run. The note goes, and the log names the row, as a warning |

- **One row never stops the rows behind it.** The oldest comes first in every batch, so a run that failed at a row the engine refuses would fail there every ten minutes, and count nothing that came after. Nobody who uses the API can bring such a row about: the ids and the credits the engine is given are the server's own.

- **It says in the log what it did,** at the info level, and nothing when it did nothing: `usage the counters had missed, counted now: 2, counted before: 1, left to their next rebuild: 0`. When Valkey didn't answer, one warning, that what is left waits for the next run. A database error fails the job, as for the other two, and the next run tries again.
- **"The worker has no usage-limits engine",** in the first revision of 2026-11-19, no longer holds. The API and the worker take the engine from the process's resources (`Resources.quota`), built once beside the rate limiter, under the same prefix: the worker settles under the keys the API counts under. A test has the app miss a settle, the worker's job count it again on resources of its own, and the app read the usage.

**What it costs:**

- **one more insert and one more delete for every turn that is counted,** the delete with a transaction of its own;
- **a count that was missed reaches the counters up to ten minutes and `COUNTED_WITHIN` later.** Until then, once its hold has run out, the account has more room than it should;
- **a share of a window (50, 75, 90 percent) that a repeated count passes is marked as said, and nobody was told:** the turn's stream is over. The page's meter shows the usage all the same;
- **`GET /api/usage` now writes to Valkey:** giving back the holds that ran out also pushes back when the account's keys expire, which reading alone didn't.

**Still open:**

- **a request that was running while the counters were lost and rebuilt stays uncounted until their next rebuild,** in the account's favour and in the ledger ([ADR 0006](0006-usage-limits-engine.md)): a repeat is answered `unknown`. So does a row whose request the counters forgot, 48 hours after its hold was given back, and one that was released although its ledger row had been committed;
- **the nightly comparison of the two stores,** which will say how far apart they are.

**Measured,** on a MacBook Pro (M1 Pro, 16 GB), 2026-11-20:

- **What the note costs a turn that goes well,** against a Postgres and a Valkey from the pinned images, the median of 300: the transaction that writes the ledger's row took 1.98 ms alone, and 2.39 ms with its note. Taking the note away, in a transaction of its own, took 2.00 ms. So about 2.4 ms more at the end of a turn that is counted, beside a settle of 0.46 ms, and an answer that takes seconds to write.
- **`GET /api/usage`** through the proxy, the median of 20: 5.0 ms, and 4.9 ms before the change, read the same day.
- **The job's batch,** beside a ledger of 500,000 rows, with three notes: 0.04 ms. Postgres reads the notes and looks each one's ledger row up by its key, whether the notes' table was ever analysed or not. With no note it reads nothing of the ledger.
- **In containers,** the stack of the end-to-end tests, whose model writes a word a second when asked. A guest asked a question with an answer of 21 words, and four seconds in, Valkey's container was paused: it kept what it held, and answered nothing.
    - **The answer was written to its end and recorded,** for 68 credits, unread. The log said that what it streams couldn't be kept, that its hold couldn't be renewed, and that its row is in the ledger and couldn't be settled. The row's note stayed.
    - **Valkey was resumed** 33 seconds after it was paused. `GET /api/usage` said 0 used and 2,200 held, the most the answer could have cost. A minute after the question, with nothing asked in between, it said 0 used and 0 held: the hold had run out, and reading gave it back.
    - **At the worker's next ten-minute mark,** five and a half minutes after the answer ended, its log said `usage the counters had missed, counted now: 1, counted before: 0, left to their next rebuild: 0`. `GET /api/usage` then said 68 used and nothing held, and no note was left.
- **After the 33 end-to-end tests** that stack's ledger had 69 rows, and not one note was left.
- **Forty-four mistakes were planted,** one at a time, in a scratch copy of the tree: in the read that gives back the holds; in the note (left out, taken away although the counters weren't told, never taken away, its failure not caught or not said, a settle that Valkey didn't answer taken for one without warnings); in the job (the minute and its boundary, the order, the batch, the locks, going on after Valkey didn't answer, which notes it takes away, what it reports, a row the engine refuses); in the worker (the job not listed, without a schedule, never committed, under other keys than the API's, what it says); and in the migration. Each fails a test. Two failed nothing at first, and got one: the order of the job's rows, which the test's own rows happened to share, and a job that holds the ledger's rows as well as the notes.
- **Without the tie between the two tables in the model,** the relationship that `Uncounted` has to its ledger row, SQLAlchemy writes the note before the row it points at, and the database refuses it: tried once, in the scratch copy. A foreign key alone doesn't order the two.

### 2026-11-20: a count that can't be right is no count

The table of what is counted says "Completed, and the provider counted: what the provider counted". Nothing looked at that count before it was written down. A model's server is someone else's program, and its counts are its own numbers. Three kinds of count that can't be right were found while the job above was built, each with a failing test:

- **A negative count, or one beyond what a row holds,** broke the end of the turn. The answer had been written whole and shown to its reader. Then its outcome couldn't be recorded: the token columns of a turn and of the ledger are 32-bit integers, and the engine takes no negative count. The turn stayed `running`, and was given up on a minute later, as interrupted.
- **A count whose cost is more than the engine counts exactly** was recorded, and its settle was refused at the end of the turn. The job would be refused it again, and since the revision above takes such a row's note away and says so in the log.
- **A count of nothing written, beside an answer that has words,** made that answer cost only its prompt, or nothing at all. The Ollama adapter reads a counter that the server left out as 0.

The review of this change found a fourth of the same kind: **a time longer than a row holds.** A model's server also says where the time went, in milliseconds, and a turn's record keeps that in 32-bit columns too. A time of 25 days or more broke the end of the turn as the first kind of count did.

Options considered:

1. **Believe a count up to the model's context window.** It would catch more: a count that is a thousand times too large fits in a row. But a true count can be larger than the window the models file states. Ollama's MLX engine reads a prompt three times the window in full ([ADR 0007](0007-local-models-through-ollamas-native-api.md), revision of 2026-11-10), and an OpenAI-compatible server has a window of its own. A true count would then be replaced by a guess.
2. **Believe a count up to a multiple of what was held.** No multiple is the right one: the guess at a prompt came out at 0.66 to 1.47 of the real count on two models, as measured above, and a third model can be further off.
3. **Believe a count unless it can't be right,** whatever the server and the model.

The third is chosen.

- **A count is dropped where the model's last event is taken in** (`_believed` in `actio.chat.answering`), when:
    - one of its three numbers is negative;
    - one of them is more than a row holds: 2,147,483,647 tokens;
    - their cost is more than the engine counts exactly;
    - it says that nothing was written, and the answer has words: by the same guess that would count the turn instead. An answer of nothing but line breaks has no tokens by that guess, so its count is believed, and it costs what was read.
- **The first three are one question, which the usage limits answer:** `can_be_recorded` in `actio.usage.limits`. Whatever else costs credits one day asks it before `record`.
- **A turn whose count was dropped is a turn without a count.** It is counted by guess, as a stopped one is, and the ledger marks it as estimated. Its own record keeps no count, so the page shows none. The log says what the server reported, as a warning.
- **A count that passes is taken as it is,** as before. This asks whether a count can be written down, not whether it is likely.
- **Times that a turn's record can't hold are left out the same way** (`_kept`, beside it): a negative one, or one longer than 2,147,483,647 ms. The turn then keeps no times, and nothing else about it changes: they are shown, never counted.
- **One more row** for the table of what is counted:

    | The turn | Costs |
    |---|---|
    | Completed, with a count that can't be right | The guess at the prompt, without the margin, and a guess at what was written. The ledger marks it as estimated |

- **Why it surfaced only now:** every test's model reported counts that could be true, and so did the two real ones. The job that settles the ledger's rows again was the first code to meet a row that the engine refuses, and looking for where such a row could come from led here.
- **Eighteen mistakes were planted,** one at a time, in a scratch copy of the tree: a count or a time taken as it comes, each bound left out or moved by one, the rule about nothing written dropped or applied to every answer, an answer taken to have words whenever it has parts, a dropped count kept all the same, the log's lines left out. Each fails a test.

**Still open:** a count that is wrong and can be recorded is believed. A server that reported a hundred million tokens for an answer would have its account charged for them, and nothing takes a row of the ledger back: changing what an account has used comes with the admin pages, as "Not done yet" says of granting credits.
