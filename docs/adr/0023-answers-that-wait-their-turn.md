# 0023. Answers that wait their turn: a gate in front of a model's server, and limits on what one account runs at once

- **Status:** accepted
- **Date:** 2026-11-18

## Context

An answer is written at the server in a task of its own, and every reader follows its stream in Valkey ([ADR 0022](0022-turns-that-run-apart-from-their-request.md)). Since that decision's revision, the page reads along with an answer wherever the conversation is opened. So one account can easily have several answers being written at once, in several conversations, with several windows reading each. Nothing limited any of it.

- **A local model's server writes one answer at a time.** Actio sent it every turn as it came. A second turn's request waited inside the server, while the model client's patience for the first word, 90 seconds (`Timeouts.first_event_s`), was already running. Behind a long answer, a turn failed as `model_unavailable` though nothing was wrong.
- **Other servers write many at once.** vLLM batches requests. LM Studio and llama.cpp's server can be set to take several in parallel, and so can Ollama.
- **One server can run several models,** and its capacity is shared by all of them.
- **An answer is paid for** ([ADR 0020](0020-usage-limits-on-turns.md)). Its credits are held from the moment it is admitted. A turn that is running says every 15 seconds that it is alive, which renews its hold and its stream; one that falls silent for a minute is given up on ([ADR 0022](0022-turns-that-run-apart-from-their-request.md)).
- **A reader holds a connection** for as long as the answer it reads takes, and asks Valkey every second while it waits.
- **The API reads a request's body whole** before it can say that the body is too large. The longest question it takes is 100,000 characters (`MAX_QUESTION_CHARS`).
- **One process serves the API,** and writes every answer ([ADR 0001](0001-one-app-several-process-roles.md)).
- **The page already copes with a refusal.** It shows a refused question's `detail` and puts the question back in the box, and a join that is refused falls back to looking every second and a half.

## Options considered

**Where an answer waits while its server is busy:**

1. **Inside the server,** as before.
    - Con: the client's patience for the first word runs out while the answer waits its turn.
2. **At a gate in the API process,** in front of the server. The server is asked only once it has room.
    - Pro: the wait is Actio's to see, and to end.
    - Con: only this process knows the gate. Two processes would each let an answer through.
3. **At a gate in Valkey,** shared by every process.
    - Con: a place there is a lease, to renew while the answer is written and to give back, and one that a dead process leaves behind has to run out first.
    - Con: one process writes every answer today, so it would only add a round trip before each one.

**Which answers share a gate:**

1. **Each model has its own.** Two models on one Ollama would each find the server free.
2. **Each provider has one,** shared by every model that runs there. The capacity is the server's.

**How many answers one account may have being written, counted:**

1. **As leases in Valkey that run out.**
    - Con: a second record of what is running, beside the turns' rows. It has to be renewed, given back on every way a turn ends, and rebuilt after a restart of Valkey.
2. **From the turns' rows in Postgres.**
    - Pro: the rows are the truth about what is running, and already say whether a turn is alive (`alive_at`). Nothing to renew, give back or rebuild.
    - Con: a query, and a lock, at every admission.
3. **In a counter in the process.**
    - Con: forgotten at a restart, wrong with a second process, and kept right by every way a turn ends.

**What becomes of one answer too many:**

1. **It is queued** until one of the others ends.
    - Con: the page would show an answer that doesn't start, for as long as another takes. And one account could fill the gate's queue for everyone.
2. **It is refused,** with words that say what to do.

**Two questions of one account at the same moment:** a turn that is being started is in nobody's count until its transaction is committed. Counted side by side, both would find room.

1. **Lock the account's row.**
    - Con: anything else that writes the account waits too.
2. **A Postgres advisory lock for the transaction,** keyed by the account.
    - Pro: no table. Other accounts take other keys.

**Readers:**

1. **Counted in Valkey,** across processes. A second counter to keep right, for one process.
2. **Counted in the process.** A reader is a connection to it.

And which of them can be refused:

1. **Every reader,** the request's own too.
    - Con: a question that was admitted, and is being paid for, could start without its reader.
2. **Only one who joins.**

**The size of a request:**

1. **The API refuses it.** It has read the body by then.
2. **The proxy refuses it,** before the API gets much of it, or any.

## Decision

**A turn waits at a gate in front of its model's server, in the API process. A plan may limit how many answers one account has being written at once, counted from the turns' rows; one more is refused. An account has six readers at most; a seventh may not join. The proxy refuses a body over 2 MB on the API's routes, and its config is built into its image.**

**The gate** (`actio.catalog.Gate`, `actio.chat.answering`):

- **Per provider,** in the models file: `answers_at_once`, at least 1, how many answers that server writes at the same time. Left out, it is 1 for `kind = "ollama"`, and there is no gate for `kind = "openai-compatible"`, which may batch many. Whoever runs a server that takes more sets the number.
- **The catalog builds one gate for each provider that has one,** and every model on that provider shares it (`OfferedModel.gate`). A model without one is asked at once, as before.
- **A turn waits at the gate after its beat has started, and inside its `Halt`'s scope.** Its model is asked only once it has passed. So the client's patience for the first word starts when the server is free.
- **It gives its place back as soon as the model's stream is closed,** however that came about: finished, failed, stopped, halted by a shutdown, or cancelled. That is before the turn is recorded.
- **First come, first served.** A turn that leaves the queue, stopped while it waits, leaves the others in their order.
- **A turn that waits is a running turn like any other.** It says it is alive, which keeps its hold on its credits and its stream. Its readers get the line that keeps a silent connection open. Stop ends it at once, as `stopped`, with nothing written and nothing charged; a shutdown records it as `interrupted`.
- **A turn that had to wait leaves a line in the log,** with how long: `turn …: waited 12.3 s for the model's server`. One that was stopped, or ended by a shutdown, while it waited says so too: `…, and didn't get its turn`. The line is a warning: as the web server sets up the log, only Actio's warnings and errors reach it. Its info lines don't, such as the two in `actio.usage.limits`.
- **The end-to-end stack's fake model has no gate.** Its tests run side by side on one fake ([ADR 0017](0017-end-to-end-tests-on-the-real-stack.md)).

**Answers at once, per account** (`actio.chat.turns`, `actio.api.turns`):

- **In the plans file:** `[plans.<name>]` takes `answers_at_once`, at least 1. Left out, that plan has no such limit, as its other limits read. A guest may have 2, an account 3.
- **What is counted:** the account's turns, in all its conversations, that are `running` and were heard from within a minute (`turns.STALE_AFTER`). That is the rule by which a silent turn is given up on. A new turn counts from its start, and a turn that waits at a gate counts: it is running. A turn of a process that died stops counting a minute after it was last heard from, with nothing to clean up.
- **One account's admissions come one after the other,** from the count until the turn's transaction ends: each takes a Postgres advisory lock for the transaction, keyed by the last 64 bits of the account's id. Other accounts take other keys, and never wait for it. Those keys are negative: the variant bits of the ids Actio makes see to that, and the migrations' lock is positive.
- **Refused, not queued.** The check comes after the one for an answer already being written in the same conversation (409), and before anything is kept or held: the question isn't stored, no credits are held, and no stream is opened. It holds for a new question and for another answer to an old one alike.
- **The refusal is a 429,** with words for a person and the plan's limit as `at_once`:

    > 2 answers are already being written for you, as many as your plan allows at once. Wait for one to finish, or stop one.

    It has no `window`, by which the page knows a usage limit ([ADR 0021](0021-usage-limits-in-the-page.md)), and no `Retry-After`: nobody knows when an answer ends.

**Readers, per account** (`actio.chat.readers`, `actio.api.turns`):

- **Six at once** (`READERS_PER_ACCOUNT`): as many connections as a browser opens to one address over HTTP/1.1, where each reader takes one.
- **Counted in the API process,** over both routes that stream: the response of the request that starts a turn, and `GET …/turns/{turn_id}/stream`. The count is held in the app's state, like the runner.
- **Only joining is refused:** `GET …/stream` answers 429 when the account already has six. The request that starts a turn is never refused for it, and its reader counts. So an account can have more than six, and the refusal says how many there are, and how many to close:

    > You already have 7 windows or tabs reading answers as they are written, and an account may have 6 at once. Close 2 of them, and try again.

- **A reader counts until its response is over,** however it ends: the stream ended, the client left, something failed, or the client was gone before anything could be sent. The count is given back when the response's own call returns, not by the stream it reads, which may never have begun. A 204 or a 404 never counted.

**The size of a request** (`deploy/caddy/Caddyfile`):

- **The API's routes, `/api/*` and `/v1/*`, take a body of at most 2 MB:** two million bytes, as Caddy reads `2MB`. Every request the API would accept fits. The largest question is 100,000 characters, and a character takes at most twelve bytes in JSON: one beyond the first 65,536, written as two `\u` escapes, as a client that escapes everything outside ASCII writes it (Python's `json.dumps` does by default). The API counts it as one character. That is 1.2 MB, and a little more around it. A test in `deploy/tests/` holds the two numbers together.
- **Checked on the pinned Caddy, 2.11.9:** a body that says its length, and is too long, is refused before any of it is passed on to the API. A body sent in chunks is passed on as it comes, cut off at the limit, and refused the same way; the API sees a request that broke off. Either way the refusal is an error with status 413, which `handle_errors 413` answers. Other errors, such as a 502 when the API is down, are answered as before.
- **The refusal is a problem document,** like every error of the API (`application/problem+json`), so that the page has words to show:

    > This is too much to send at once: Actio takes at most 2 MB in one request. Shorten the message, and send it again.

- **The API's own limit stays what says why a long message is refused:** a 422 for a message over 100,000 characters.
- **The proxy's config is built into an image of its own** (`deploy/caddy/Dockerfile`), as the API's and the web app's code are built into theirs: the pinned Caddy image, with the Caddyfile copied in. `make up`, `make dev` and `make e2e` build it. A change to the Caddyfile is a new image, and starting the stack again recreates the proxy from it. Nothing is mounted into the proxy, and nothing watches the file.
    - **The build checks the config** (`caddy validate`). A mistake in it stops the build, with Caddy's own words, and the proxy that is running goes on serving with the config it has.
    - **Why.** The Caddyfile was mounted into Caddy's container from the repository, as one file. On a stack that was running, `make up` with the limit first built here, 1 MB, let a body of 1.5 MB through to the API, which answered 422: the limit wasn't in effect. The container no longer found its Caddyfile at all. An editor or git replaces a file with a new one, which a mount of the old file never shows; and Compose doesn't recreate a container because a file it mounts has changed, whether the file is a bind mount or one of Compose's `configs:` (tried on Compose 5.7.0). After `docker compose restart caddy`, 1,500,127 and 1,000,127 bytes were refused with the problem document, and 110,127 bytes were answered 422 by the API. A limit that is silently off on a stack that is updated in place protects nothing.
    - **Tried first, and dropped: Caddy watching its file** (`caddy run --watch`), with the folder mounted, so that a replaced file is seen. It worked: a file replaced by a rename was in effect within a second. But Caddy's documentation says of `--watch`: "This feature is intended for use only in local development environments!", and `compose.yaml` is the stack shaped like production. A watcher that looks at a file every so often can also catch it half-written.
    - **Checked** in a throwaway project of the stack's own proxy, built from a copy of the repository: with 2 MB in the file, 1.5 MB went through and 2.5 MB was refused. With 1 MB in the file, starting the stack again (`docker compose up --detach --wait --build`) said "Recreated", and 1.5 MB was refused. With a mistake in the file, the build stopped at `caddy validate`, exit 1, and the proxy that was running was the same container, healthy, still refusing 1.5 MB.
    - A test in `deploy/tests/` holds the proxy to this: built from `deploy/caddy/`, from a pinned base, with nothing mounted, and with a build that checks the config.

**The page didn't change.** A refusal for answers at once has no window, so the page shows it as that question's own problem: the API's words, the question back in the box, and nothing that counts down. A component test says so. A join that is refused falls back to looking, as one that fails does.

**Tested in a real browser** (`apps/web/e2e/turns.spec.ts`): a guest with two slow answers being written asks a third, is refused in the API's words, stops one of the two, and has the third answered while the other is still written. A request over the limit, two million characters, is answered 413 by the proxy, with a problem's `detail`; one under it, with a message longer than the API takes, reaches the API and is answered 422. The gate isn't tested there: the page can't choose a model, and the stack's model has to stay without a gate for the tests to run side by side.

## Measured

On a MacBook Pro (M1 Pro, 16 GB), the machine of [ADR 0022](0022-turns-that-run-apart-from-their-request.md)'s measurements, 2026-11-18, through the proxy of `make up`, with the default model (`qwen3.5:4b-q4_K_M`), whose server, Ollama, writes one answer at a time.

**Three long questions asked at the same moment by one guest,** each in a conversation of its own. Each answer runs to the model's 2,048 tokens: about 54 seconds of writing.

| | The first asked | The second | The third |
|---|---|---|---|
| Before this step: the code of step 9b, the same day | First word after 57.0 s, behind the second | First word after 3.8 s: the model had to be loaded | Failed after 90.4 s |
| With the gate, and a guest allowed three at once (a plans file made for the measurement) | First word after 0.4 s, ended after 54.5 s | First word after 54.6 s, ended after 108.5 s | First word after 108.7 s, ended after 162.1 s |
| As shipped: a guest allowed two at once | First word after 4.3 s: the model had to be loaded. Ended after 58.5 s | First word after 58.7 s | Refused 0.1 s after it was asked |

- **Before, the third failed though nothing was wrong:** "The model's server can't be reached, or is too busy. Try again in a moment." The model client's 90 seconds for a first word had run out while its request waited inside the model's server.
- **With the gate, all three were answered whole, in the order asked.** The second began 0.1 s after the first had ended, and the third 0.2 s after the second. The third had waited 108 s, longer than those 90, and its reader was sent seven of the lines that keep a silent connection open; the second's, three.
- **As shipped, the third was refused:** "2 answers are already being written for you, as many as your plan allows at once. Wait for one to finish, or stop one."

**In the page,** in a headless Chromium, with the same guest's two limits:

- a second conversation asked while the first's answer is written shows the wait for the model, with Stop;
- a third question is refused in the sentence above, and is back in the box;
- Stop pressed for the answer that waits: the page said so 0.13 s later. The turn was `stopped`, with nothing written and nothing charged, and the first answer went on;
- the refused question, asked again, was answered 0.5 s after the first answer had ended.

**Readers:** with the turn's own reader and five that joined, one more was refused with 429 and "You already have 6 windows or tabs reading answers as they are written, and an account may have 6 at once. Close one of them, and try again." After one had left, the next join was let in at the first try, 0.1 s later.

**Size:**

| A body of | Answered |
|---|---|
| 2,500,127 bytes | 413, by the proxy, with its sentence |
| 2,000,127 bytes | 413, by the proxy, with its sentence |
| 1,500,127 bytes | Passed on, and 422 by the API: too many characters |

After `make up` on a stack that was running, the proxy had been recreated from its image, and the limit was in effect at once. With the mounted file it had not been, until the proxy was restarted by hand: see the decision above.

**Thirteen mistakes were planted,** one at a time, in a copy of the tree, to see the tests fail:

| The mistake | Tests of the API and of the containers that failed |
|---|---|
| A turn gives its place at the gate back before its model's stream is closed | 1 |
| Every model has a gate of its own, also two models on one server | 1 |
| A server may be said to write no answers at once | 1 |
| Other accounts' answers count against an account's limit | 1 |
| One answer more than the plan allows at once is let in | 7 |
| A plan may allow no answers at once | 1 |
| A seventh reader is let in | 5 |
| The readers of all accounts are counted together | 1 |
| The proxy has no limit on a request's size | 1 |
| The proxy's limit is smaller than the largest question the API takes | 1 |
| The proxy's image is built without checking its config | 1 |
| The proxy is given its config as a mounted file again, over the one in its image | 1 |
| The proxy's image starts from a base that isn't pinned by its digest | 1 |

And at the whole stack: the end-to-end tests of `turns.spec.ts`, run against a stack without the limit on answers at once and without the proxy's limit, failed exactly the two new tests, and the other six passed.

**Three of the thirteen failed nothing at first,** and each got a test:

- **The place at the gate given back before the model's stream is closed.** A scripted model's stream closed the moment it was told to, so the order of the two couldn't be seen. Its test now has a model whose stream takes a while to close, as a real server's connection does: the model asks for a tool, which no turn offers it, the turn ends with the stream still open, and Actio closes it. The next turn's model is not asked until that stream has closed.
- **Other accounts' answers counted against an account's limit.** Every test of the limit asked as one account. Now an account at its limit doesn't keep another account from asking.
- **All accounts' readers counted together.** Every test of the readers read as one account. Now six readers of one account don't keep another account, in the same process, from joining a turn of its own.

**A test that failed now and then,** found while this was built:

- **The live tests' read helper** kept what it had read of each response in a table for the whole module, under the response's address in memory, and never took anything out. Once a test had ended and its responses were gone, a later test's response could be given the same address, and the helper took the old response's text and its closed reader for the new one's.
- **It dated from the first step of turns** ([ADR 0014](0014-turns-streamed-in-the-ai-sdks-format.md)), and turns that run apart from their request ([ADR 0022](0022-turns-that-run-apart-from-their-request.md)) used it too. Their tests read few responses each. The tests of readers here open six to nine, and it showed: in 2 of 30 runs of three of the test files this step changed (`test_turns_running_api.py`, `test_turns_live.py` and `test_compose_policy.py`).
- **What it reads is now kept with the response itself,** in its `extensions`, and goes with it. After the fix the three files were run 40 times over, twice: 40 of 40 passed each time.

**Every end-to-end test six times over, two at a time: 198 of 198 passed.**

## Consequences

- **Easier:**
    - a turn behind a long answer on a local model waits, and is answered, where it failed;
    - several people sharing one machine's model are answered in the order they asked;
    - a server that writes several answers at once is used so, with one line in the models file;
    - one account can't have more answers being written, or more readers open, than its share;
    - what a turn costs while it waits is held for it, and it can be stopped like any other.
- **Harder:**
    - **an answer that waits looks like one whose model is slow to start.** The page shows the wait for the first word, and doesn't say that the server is busy, or how many answers are ahead;
    - **the gate is in the process,** and so is the count of readers. A second API process would let a second answer through to the same server, and an account would have six readers there too;
    - **every admission under a limit takes a lock and a count** in Postgres;
    - **a request over 2 MB isn't let through to the API,** on any of its routes: one that says its length is refused before any of it is passed on, and one sent in chunks is cut off at 2 MB, which the API sees as a request that broke off. The developer gateway may need more for a long context: its routes would get a limit of their own;
    - **a change to the proxy's config takes a build,** and a start of the stack: `make up` or `make dev` again. Until then the proxy runs the config it was built with. That is what the API's code does too;
    - **a fourth image to build,** a small one: the pinned Caddy image and one file;
    - **two files have a field more,** and a provider of `kind = "ollama"` gets a gate unless the file says otherwise.
- **Not done yet:**
    - **telling the reader that an answer waits,** and at which place. It comes with the router, which will know more about the servers;
    - **fairness between accounts.** The gate takes turns in the order they came, whoever asked them;
    - **a limit on the queue's own length.** The limit per account bounds what one account can put in it, and a guest is a new account ([ADR 0011](0011-rate-limits-as-token-buckets-in-valkey.md) limits new guests per network address);
    - **a gate, and a count of readers, shared by several API processes.** One process writes every answer today, as [ADR 0022](0022-turns-that-run-apart-from-their-request.md) says of Stop.
- **How to reverse it:**
    - **the gate:** a provider without a gate is asked at once, so `answers_at_once` set high puts things back as they were. Removing it is the gate's few lines in `actio.chat.answering`;
    - **the limit per account:** leave the line out of a plan;
    - **readers:** `READERS_PER_ACCOUNT`, or the `join` in `follow_turn`;
    - **the size:** the `request_body` block, and `handle_errors 413`, in the Caddyfile;
    - **the proxy's image:** the `caddy` service's `build`, in `compose.yaml`, back to the pinned `image` it was, with the Caddyfile mounted. Then the stack has to be told to restart the proxy after every change to the file.

## Revisions

### 2026-11-26: a place that comes free goes to the account that has had the least

The decision's "Not done yet" said: "**fairness between accounts.** The gate takes turns in the order they came, whoever asked them." It is done now, with the router and before the developer gateway, whose requests will wait at the same gate, and whose clients may send several at once. First come, first served, one account that asks three long questions at once holds up everyone who asks after it, for all three: on the running stack, a guest who asked one question a second after another guest's two had its first word after 37.0 s, behind both (Measured, below).

**Worked out** over made-up traffic, by a throwaway script outside the repository, and not seen on a real server. Four rules for which waiting answer gets a place that comes free:

- **first come, first served:** the gate as it was;
- **a ring:** the accounts that have answers waiting, each in turn, a new one at the back;
- **the least served, forgotten when absent:** the account holding the fewest places, then the one given a place the longest ago, then the one that came first; an account is forgotten as soon as it has nothing at the gate;
- **the least served, remembered while anyone waits:** the same, an account remembered while it has something at the gate, and the others until nobody waits.

| Made-up traffic | First come, first served | A ring | Least served, forgotten when absent | Least served, remembered while anyone waits |
|---|---|---|---|---|
| One answer at a time, 54 s each. A asks three at 0 s, B one at 1 s, C one at 2 s | A1 A2 A3 B1 C1. B waited 161 s, C 214 s, A's last 108 s | A1 A2 B1 C1 A3. B 107 s, C 160 s, A's last 216 s | A1 B1 C1 A2 A3. B 53 s, C 106 s, A's last 216 s | the same |
| One at a time, 30 s each. A asks two at 0 s. B and C each ask one question, and the next one 10 s after its answer ended, for an hour | A's second waited 30 s | 30 s | **never passed within the hour:** A got 1 answer, B 60, C 60 | 90 s; A 2, B 60, C 59 |
| One at a time, an hour, 200 runs. H keeps three questions at the gate all the time; three others each ask one question, and the next 1 to 4 minutes after its answer ended; answers of 20 to 60 s | the others: median wait 145 s, 9 in 10 within 192 s, longest 256 s, 10.5 answers each; H 59.2 answers | 81 s, 124 s, 206 s, 13.1 each; H 51.5 | 28 s, 60 s, 136 s, 16.3 each; H 41.6 | 27 s, 66 s, 157 s, 16.2 each; H 42.0 |
| One at a time, 30 s each. A asks two at 0 s; then a new account asks one question every 25 s, for an hour (more than the server can write) | A's second waited 30 s | 30 s | not within the hour | not within the hour |

And for a server that writes four at once: A asks three at 0 s, 60 s each; B one at 1 s, 10 s long; at 2 s A asks a fourth, at 3 s B a second. When B's first ends at 11 s, A holds three places and B none.

**Which waiting answer gets a place that comes free:**

1. **The one that came first, whoever asked it,** as before.
    - Con: one account's questions at once hold up everyone after them. In the first case B waited 161 s and C 214 s, behind all three of A's. Where H keeps three questions at the gate all the time, the three others waited 145 s at the median, and 256 s at the longest, and had 10.5 answers each in the hour to H's 59.2.
2. **A ring.**
    - Con: an account that comes goes to the back, behind accounts that have had answers already: in the first case B waited 107 s and C 160 s, behind A's first two. Where H keeps three at the gate, the others waited 81 s at the median and 206 s at the longest, and had 13.1 answers each to H's 51.5.
3. **The least served, forgotten when absent.**
    - Con: an account that asks again soon after each answer comes back as one never given a place, every time, and goes before an answer that has waited all along. Where B and C each ask again 10 s after their answer, A's second didn't pass within the hour: A had 1 answer, B and C 60 each.
4. **The least served, remembered while anyone waits.** Chosen.
    - Pro: in the first case B waited 53 s and C 106 s; where H keeps three at the gate, the others waited 27 s at the median and 157 s at the longest, and had 16.2 answers each to H's 42.0; where B and C ask again 10 s after each answer, A's second passed after 90 s.
    - Con: A's last answer waits longest, 216 s in the first case where first come, first served had it wait 108 s; and A's second waited 90 s where B and C keep asking, where it waited 30 s first come, first served. Where a new account asks every 25 s for answers of 30 s, more than the server can write, A's second didn't pass within the hour, where first come, first served had it wait 30 s: every account that comes is one never given a place, and goes first.

**What counts first, the places an account holds or when it was last given one:**

1. **When it was last given one, alone.**
    - Con: in the case of four at once, A's fourth gets the place, A was given its last one before B was: A then holds four places and B none, and B's second waits 57 s.
2. **The places it holds, and of those that hold as few, when it was last given one.** Chosen: B's second gets the place, and waits 8 s.

**Whose answer it is:**

1. **The conversation's.**
    - Con: one account's many conversations are the burst: each of the questions above was in a conversation of its own.
2. **The network address's.**
    - Con: the gate doesn't know it, and people behind one router share it.
3. **The account's,** a guest's too. Chosen.

**Where:** in the process, as the decision above has the gate. Unchanged.

**Decision: a place that is given back while answers wait goes on at once, to the waiting answer of the account that holds the fewest places at the gate; of those that hold as few, the account given a place there the longest ago, one of which nothing is remembered before every one that is; of those, the one whose answer came first. One account's own answers pass in the order they came. An account is remembered while it holds a place or has an answer waiting, and beyond that until nobody waits. A turn waits as the answer of the account that asked it, and a title as the answer of the account its conversation belongs to.**

**The rule** (`actio.catalog.Gate`):

- **While a place is free, an answer that comes passes at once,** without waiting for anything. A place is never free while an answer waits: one that is given back goes on in the same step.
- **A place given back while answers wait goes on at once,** to the waiting answer of the account that holds the fewest places at the gate at that moment; of those that hold as few, the one given a place there the longest ago, an account of which nothing is remembered before every one that is; of those, the one whose answer came first. Of one account's waiting answers, the one that came first.
- **"Given a place" is counted, not timed:** the gate numbers the places it gives, at once or after a wait, and remembers each account's last number. It reads no clock.
- **What is remembered, and for how long:** an account's last number, while the account holds a place or has an answer waiting at the gate, and beyond that until nobody waits there. Whenever nobody waits, the gate keeps nothing at all of an account that holds no place: no number, and no entry that counts to nothing. Guests are accounts, and there is no end of them.
- **An answer that gives up waiting,** stopped, ended by a shutdown or past a title's time, leaves, and changes nothing for the others: who holds what and what is remembered stay as they were, but that the absent are forgotten when it was the last one waiting. One that was cancelled while it waited, and hasn't left its line yet when a place is given back in that same step of the event loop, isn't handed the place: it goes to the next one by the rule, and is free when nobody else waits.
- **A place is never lost and never doubled:** never more than `answers_at_once` are held, and none is free while an answer waits. That includes an answer handed a place that is cancelled before it could go on with it, in the same step of the event loop: it counts as given that place, and gives it back at once, as one that ended at once would, and the place goes on by the rule. So its account's next answer waits as that of an account given a place just then. Not counting it would take a second record of each account's number before the last, for a moment that only a cancellation in that very step makes.
- **A place is given back however the answer ended,** in a step that awaits nothing, as before.
- **`Gate.full`** still says whether an answer that comes now has to wait: true while every place is held, one handed on and not yet gone on with included. `Catalog.load_ahead` reads it, as before. **`Gate.waiting`** is still how many answers wait.
- **The way in takes the account, and there is none without one:** `async with gate.passing(account):`. The gate is given a key that it only compares and hashes, and knows nothing of accounts: `actio.catalog` doesn't import `actio.identity`. `Gate.accounts`, how many accounts it keeps anything of, is for the tests.

**Whose answer it is:**

- **a turn's** is the account that asked it, `principal.user_id`, a guest's too: the route gives it to `answering.answer`, and the turn waits as that account's answer at its own model's gate and at each stand-in's;
- **a title's** is the account its conversation belongs to, from the conversation's row, which the job read already;
- **a load ahead** takes no place, and reads `Gate.full`, as before.

**Unchanged:** `answers_at_once` in the models file, and one gate for each provider that has one; a plan's `answers_at_once`, counted from the turns' rows; the beat, the hold on the credits and the stream of a turn that waits; Stop and the shutdown; the place kept through a second try, and given back before the next model's gate is waited at; the walk and `packages/llm`; the breaker, the budgets and a provider's own wait; what a turn streams; the log's lines, `turn …: waited 12.3 s for the model's server` and `…, and didn't get its turn`; the routes and `apps/api/openapi.json`; the page; the tables, with no migration; the settings; the containers.

**Tests:**

- **the gate's** (`test_catalog.py`): one account's answers in the order they came, as many as the server takes; one account's three at once, and another account's first answered second; two accounts with answers waiting taking turns, each in the order it asked; four at once, B's second before A's fourth, by the places held; an account that left remembered while somebody waits, and forgotten once nobody waits, and one that holds a place remembered then all the same; forty accounts that came, waited, gave up and went, after which the gate keeps what a new one keeps; an answer that gives up waiting, which changes nothing for the others, what is remembered of an absent account included; an answer handed a place and cancelled in that step, whose place goes on by the rule, with `full` and `waiting` right, and whose account's next answer passes as that of an account given the place; an answer cancelled in the step in which a place is given back, which isn't handed it, and a place that is free when only such a one waits; the last answer waiting giving up, after which the absent are forgotten; of two accounts alike, the one whose waiting answer came first, though the other has waited there for longer; three at once, where a place given back no longer counts as its account's when the next is chosen; a place given back however the answer ended; and a property test against a gate written the plain way, without an event loop: four accounts' answers that come, end, end with the answer handed their place cancelled in that step, and give up waiting, at a gate of one to four places;
- **a turn's** (`test_turns_running_api.py`): another guest's question answered before the second of the guest that came first, its model asked before, with the log's lines as before; one account's turns in the order they came; a turn stopped while it waits, and a shutdown, each turn another guest's, which leave the gate as before and keep nothing there;
- **a stand-in's** (`test_turns_stand_ins_api.py`): a turn that waits at its stand-in's gate as its own guest's answer, after another guest's that came later;
- **a title's** (`test_titles.py`): a title that waits as its conversation's account's answer: with the place held, a turn of A's, then the title of A's conversation, then a turn of B's pass A's turn, B's turn, the title; and a title given up on at the gate that keeps nothing there.

**Known, and left,** each worked out, and not seen, unless it says so:

- **an account's second answer at once can be passed by every account that has had no place since somebody last waited, however many they are:** with a new account asking every 25 s, for answers of 30 s, more than the server can write, it didn't pass within the hour, where first come, first served had it wait 30 s. New guests are limited to 30 an hour for a network address ([ADR 0011](0011-rate-limits-as-token-buckets-in-valkey.md));
- **one person with several guest sessions is several accounts,** each with a share;
- **a place is a place, whatever the answer costs the server:** a long answer and a short one count alike;
- **the gate is in the process, as before:** the API's and a separate worker's are apart, so under `make up`, where the worker is a container of its own, a title's request doesn't wait at the API's gate, and no account's share there counts it (read in the code; the decision above says the same of a second API process);
- **a reader isn't told that its answer waits, nor at which place:** the next step;
- **the queue has no limit of its own,** as before;
- **while somebody always waits, the gate remembers every account given a place since nobody last waited:** a number each, with no limit of its own. With a new account every 25 s, as above, that is 144 an hour;
- **a turn stopped in the very step in which it is handed a place goes on with it until its next wait,** and stops there, as before: its cancel scope leaves a task alone whose wait is already over, and tries again on the loop's next round (read in anyio 4.16.0). The log says that it waited, and the place comes back when it stops;
- **the log's `waited` line says how long, not who passed;**
- **the developer gateway's requests will wait as the answers of their key's account,** when they come.

**How to reverse it:** in the code, `Gate` back to the `asyncio.Semaphore` it was, behind `async with gate:`, without `_Waiting`; and the account that `actio.api.turns.start_turn` and `_write`, `answering.answer` and `_through`, and `titles.write`, `_first_question` and `_ask` hand to it. Or, keeping the gate, the same key for every answer: all the answers are then one account's, and pass in the order they came.

**Measured**

On 2026-11-26, on a MacBook Pro (M1 Pro, 16 GB), the machine of the measurements above. No cloud provider was asked anything for this step.

**On the running stack** (`make up`, the shipped config, the local model `qwen3.5:4b-q4_K_M` on Ollama, which writes one answer at a time), through the API, by a script outside the repository. Guest A asked two questions in the same moment, each in a conversation of its own; guest B one, a second later; guest C one, a second after that. Each asked for about 400 words. The model was loaded before, by a fourth guest's question. Each row is one run.

| | A's first | A's second | B's | C's | Answered in the order |
|---|---|---|---|---|---|
| Asked at | 0.0 s | 0.1 to 0.2 s | 1.0 s | 2.0 s | |
| First word after, before the step: the tree at `852b3b9` | 0.8 s | 16.3 s | 37.0 s | 73.2 s | A1, A2, B1, C1 |
| On the step's first build | 0.2 s | 48.3 s | 16.1 s | 29.9 s | A1, B1, C1, A2 |
| On the tree as it was committed | 0.1 s | 81.2 s | 16.1 s | 33.9 s | A1, B1, C1, A2 |

- **The API's log, before:** `turn …: waited 16.2 s for the model's server` (A's second), `… waited 35.3 s …` (B's), `… waited 72.7 s …` (C's). **On the tree as it was committed:** `… waited 15.3 s …` (B's), `… waited 33.3 s …` (C's), `… waited 80.9 s …` (A's second). The lines are as they were.
- **The model didn't write at one speed,** so a wait is not a number of answers: the answers, of 449 to 561 words, took 14 to 45 s to write, the later ones of a run the longest. Before the step 16, 20, 37 and 41 s, in the order they were written; on the tree as it was committed 16, 18, 45 and 38 s, so that A's second waited 81.2 s behind three answers. The cause wasn't looked for.
- **Seen beside it, and older than this step:** under `make up` the worker is a container of its own, and its requests for the conversations' titles waited inside Ollama, behind the answers, where no gate of the API's counts them ("Known, and left", above). Ollama's own log shows five such requests, in the three runs, ended after 30 seconds without an answer, each behind an answer that took longer to write; the worker's log said of the last `no title, its model took longer than 30 s`, asked for it again half a minute later, and wrote it.

**Mistakes planted on purpose,** each alone in a scratch copy of the tree, with the tests that should notice run until the first one failed. On the step's first build, while the review read it, 35: 33 failed a test. The two that failed none:

- **the answers that come to wait not numbered:** of two accounts alike, the gate then takes the one whose line is older, which is the one whose answer came first unless that account's first answer gave up meanwhile. No test had an answer give up ahead of another account's. One does now;
- **the rule asked with the place just given back still counted as its account's:** it shows only at three places or more, where the account that gives one back holds as many afterwards as another. A test of three at once holds it now.

Two more failed one test alone, of 583, and that one a turn's, not the gate's (`test_a_shutdown_ends_the_turns_that_wait_too_and_leaves_the_gate_free`): **a place handed to an answer that was cancelled in that step and hadn't left its line,** and **the absent still remembered after the last answer waiting gave up.** The property test drew neither: it lets the loop run after each step, so it never has two things happen in one, and the second needs five steps in one order. Each has a test of the gate by itself now.

On the tree as it was committed, 36, one more among them, for what an answer handed a place and cancelled leaves of its account: 35 failed a test. The one that failed none was **an account that holds a place forgotten too, once nobody waits,** which the property test had caught on the first build and drew no case for this time: it draws new cases at every run, and what it alone catches isn't caught every time. It got a test of its own, which it then failed.

**The review,** by a reviewer that had not seen the build, once: nothing that had to be fixed, and four smaller points, all taken. That no test of the gate by itself had an answer cancelled in the step in which a place is given back, which the planting showed too; that the test of an answer handed a place and cancelled didn't show what counting it as given does, for which that account's next answer now stands in line; a time limit around the ways in that two tests take by hand; and, under "Known, and left", what the gate remembers while somebody always waits.

**Not measured:** a server that writes several answers at once, whose part of the rule the tests hold alone; more than three accounts, and an hour of people asking, on a real server: the table of made-up traffic is worked out; the developer gateway's requests, which don't exist yet; and the page, whose source did not change. The end-to-end tests were not run for this step: nothing of the chat, a route, a stream, the containers or the fake model changed, and that stack's models have no gate.

### 2026-11-26: a turn says that it waits, and behind how many answers

The decision's "Not done yet" said: "**telling the reader that an answer waits,** and at which place. It comes with the router, which will know more about the servers". The revision before this one said, under "Known, and left": "**a reader isn't told that its answer waits, nor at which place:** the next step". Until now a turn that comes while its model's server is full waits at the gate with a stream of `start`, `start-step`, and then nothing but the line that keeps a silent connection open, and the page says "Waiting for the model…", as it does for a model that is slow to start.

What a reader wants to know is how long, and nobody knows that. What the gate does know is the order. So the number is **how many answers have to end before this one starts:** the answer's place in the order in which the waiting answers would pass, by the gate's own rule played forward from its state now. The places held come back in the order they were given, since nothing better is known of when each ends; each that comes back goes to the waiting answer the rule chooses, with the places held and the last places as they would then be, and that answer then holds the newest place; an answer that was cancelled while it waited, and hasn't left its line yet, counts for nobody. The answer that is the third to pass is told 3: three answers have to end, whichever they are. At a server that writes one answer at a time, that is the one being written and the two that go before it.

**It is exact at one answer at a time, while nobody comes and nobody leaves. At more than one it is a guess,** since the rule counts the places each account holds, and that depends on which answer ends first. **And it can go up:** by the rule, an account that has had less goes first, so somebody who comes later can go before an answer that waits already.

**Worked out** over made-up traffic, by a throwaway script outside the repository, and not seen on a real server:

- **Nobody comes or leaves after the first moment:** 2,000 made-up lines for each case, of up to eight more answers than the server takes, of five accounts. With the places coming back in the order they were given, the answers passed in the order told in 2,000 of 2,000, at one, two and four at once. With answers of 20 to 60 s, so that the places come back in any order: 2,000 of 2,000 at one at once, 1,930 at two, 1,409 at four.
- **People share one server for an hour, 200 runs:** each asks one question, and the next 20 to 90 s after its answer ended, and one of them keeps two at the gate; answers of 20 to 60 s. At each change every waiting answer is told its number anew.

| | The number first told was how many ended before it passed | Within one of it | An answer whose number went up at least once | The most that ended beyond what was first told |
|---|---|---|---|---|
| One at a time, 4 people | 70.9 % | 76.3 % | 29.1 % | 5 |
| One at a time, 8 people | 80.8 % | 88.0 % | 19.2 % | 10 |
| Two at once, 8 people | 62.6 % | 82.3 % | 37.4 % | 11 |
| Two at once, 12 people | 64.6 % | 85.6 % | 35.4 % | 14 |
| Four at once, 16 people | 46.2 % | 68.3 % | 53.8 % | 22 |
| Four at once, 24 people | 47.6 % | 69.5 % | 52.4 % | 30 |

In every one of them the last number told before an answer passed was 1.

- **Whose numbers went up, in that traffic.** At one at a time, nearly all were the answers of the account that keeps two at the gate, which everyone who has had less goes before, as the revision before this one says under "Known, and left": the number went up for 97.6 % and 98.4 % of its answers that waited (4 and 8 people), and for 6.4 % and 8.3 % of the others'; two or more answers ended beyond what was first told for 95.2 % and 97.3 % of its answers, and for 5 and 35 of the others' 13,515 and 15,805. At more than one at once the others' went up too: for 28.8 % and 29.7 % of them at two at once, and 50.8 % and 50.4 % at four, where two or more ended beyond the first number for 6.4 % and 6.9 %, and for 27.3 % and 27.6 %.
- **The number never overstated in that traffic; in the made-up lines of the first case it did,** at more than one at once. The rule counts the places an account holds, so where an account holds a place while another of its answers waits, which answer ends first changes who goes next. In a second drawing of such lines, with the places coming back in any order, 50 of 2,000 at two at once passed in another order than told, 42 of them with an account that held a place and had an answer waiting at the first moment; at four at once, 584 of 2,000, and 583 of them. The others weren't looked into: an account whose first answer passes while its second waits comes to hold and wait later.

So the page says the number as it stands, and not a promise.

**What is told:**

1. **That the answer waits, and no number.**
    - Con: it says less than the gate knows.
2. **Its number in the order the answers came.**
    - Con: since the revision before this one, that is not the order they pass in.
3. **The waiting answers sorted by the rule as the gate stands now,** without playing it forward.
    - Con: wrong even at one at a time, since the places held and the last places change with every place that is given.
4. **The rule played forward.** Chosen.

**When it is told:**

1. **With the turn's beat,** every 15 s.
    - Con: a reader would see the number up to 15 s late, and all the changes in between as one.
2. **At each change of the line,** by the gate. Chosen.

**How it reaches a reader:**

1. **A member of the turn's record** (`TurnOut`).
    - Con: a reader would have to ask again and again, and a turn's record is of what was recorded.
2. **A chunk the library hands to the page once and forgets,** `transient`, as `data-usage-warning` is.
    - Con: the page would keep the number in a state of its own beside the message, to be cleared on every way a reading ends.
3. **A chunk the library keeps in the message, and replaces in place.** Chosen: the number lives and goes with the message the page shows, and someone who joins later reads the stream from its start and ends with the newest.

**A screen reader:** the line is a `role="status"` already. Its words change when the number does, which is when an answer ends, or somebody comes or leaves, never by the clock: news each time, so it is left to say them. A time that counts down is a `role="timer"` in this app, never inside a status; this is no countdown.

**Decision: a gate tells each answer that waits there, and asked to be told, how many answers have to end before it starts, by its own rule played forward with the places held coming back in the order they were given: when it comes to wait, and whenever that number changes, in the step in which it does. A turn hands that on to its readers through its stream of events, as a chunk of Actio's own, `data-waits`, which the chat library keeps in the message and replaces in place, and says the same way that its turn has come. The page says "Waiting behind 2 other answers…" in the place of "Waiting for the model…". A turn that didn't wait streams what it streamed before.**

**The gate** (`actio.catalog.Gate`):

- **`gate.passing(account, placed=None)`:** `placed` is a function of one whole number. The gate calls it, in a step that awaits nothing, when the answer comes to wait, before it waits, with its number, and whenever its number is another than the one it was last told, in the step in which that came about: a place was given back and went on, an answer came that goes before it, one before it gave up waiting. Never for an answer that passes at once, never with a number it was just told, never with 0, and never once the answer has passed or left its line: that its turn has come, the caller sees by being let in.
- **The forward play is written beside the rule, and by the same ranking:** the function that chooses who a place goes to (`_chosen`) is what `Gate._next` asks for the place given back, and what `Gate._order` asks for each place as it plays the rule forward, on copies of the places held, the last places and the lines. So the two can't drift apart. The gate now keeps the places held by their numbers, in the order they were given (`Gate._places`, of which `full` counts), and a place is given back by its number: the future of an answer handed a place carries it.
- **The gate's books are complete before anybody is told:** all that the gate changes for one thing that happened, an answer that came, one that gave up, a place given back and handed on, first, and then the telling. Two such things in one step of the event loop are told one after the other, each as the gate stood then: a turn that gives its place back at its model's gate and comes to wait at a stand-in's, where both are on one server, is such a step. A `placed` that raises is logged with its trace, "an answer that waits at a gate couldn't be told its number", with nothing of whose answer it is, and the others are told all the same: a place is never lost over it, and nothing reaches the task whose place was given back.
- **Nothing is played forward while no waiting answer asked to be told:** a title's answers, alone at a gate, cost what they cost before.

**The turn** (`actio.chat.answering`):

- **A step of its own, `Waits(behind)`,** handed on through `emit` like the steps of the answer's text: so nothing of a turn emits beside the one way, and `actio.chat` still knows nothing of the stream's format. `Answer` makes only the text's steps (`Written`).
- **`_through` passes the gate a `placed`, which notes the newest number and awaits nothing** (`_Told`). A task of the turn's own, `waits-<the turn's id>`, started by the first number, hands them on one at a time, each the newest noted by then: one that a newer number overtook before it could be handed on is skipped.
- **Once the turn has passed,** that task is told that it is over: a number noted and not handed on is dropped, the step under way is waited for, never cut off, and only then, if a number was handed on, `Waits(0)` is: before the model is asked, shielded as the answer's steps are. A turn that passed at once starts no task, and hands on nothing.
- **A turn stopped, or ended by a shutdown, while it waits** waits for that task the same way, under a shield, and hands on no 0: its stream goes on to `finish` as before, and its record is as before.
- **Each gate a turn waits at says its own:** a turn that waited at its model's gate, passed, failed before its first word, and waits at a stand-in's gate hands on its numbers there, and a 0 again. A second try of one model keeps its place, and says nothing new.
- **The log is as it was:** `turn …: waited 12.3 s for the model's server`, and `…, and didn't get its turn`, at the same moments. No new line.

**The chunk** (`actio.api.ui_stream`):

```text
data: {"type":"data-waits","id":"waits","data":{"behind":2}}
```

- **Not `transient`, and the same `id` for every such chunk of a turn,** so that the library keeps one part of the message for them, and replaces its `data` with each (read in the installed `ai` package, 7.0.166). `behind` 1 or more: so many answers have to end before this one starts. 0: its turn has come.
- **The library takes the first one for the answer's beginning:** the chat's `status` becomes `streaming` there, where before it did at the first piece of the answer. The page reads it in two places: whether an answer is being asked for or read, which is unchanged, and whether one arrives, which reads the list of conversations again. So a first question whose answer waits its turn is in the list while it waits.
- **A recording,** `apps/api/tests/fixtures/turns/waited.sse`: `start`, `start-step`, `data-waits` 2, 1 and 0, a short answer, `finish`, `[DONE]`, from a server that writes one answer at a time, whose place the test holds, with another account's answer before the turn's. The other recordings didn't change by a byte.

**The page** (`lib/chat/waits.ts`, `components/chat/message.tsx`):

- **How many answers an answer waits behind** is read from its message alone, from its `data-waits` part (`waitsBehind`): a whole number from 1 up, and 0, which says nothing, for anything else: no such part, 0, a fraction, a negative number, a string, `null`, a member missing. The page keeps nothing of it beside the message.
- **The words:** "Waiting behind 1 other answer…", "Waiting behind 2 other answers…" (`waitingBehind`).
- **`Waiting` says them when it is given a number above 0,** and "Waiting for the model…" otherwise: one `<p role="status">`, the same classes, one element whose words change. `Answer` gives it the number only where it draws that line because nothing of the answer is drawn yet: never while the line stands in for the renderer, never for an answer that isn't being written, never once a thought or a word is drawn. `Message` has no new property: it is memoized, and `Answer` reads the number from the message.
- **Nothing else of the page changed:** the line under a question that has no answer yet (`chat.tsx`) says what it said, the article is the model's words and nothing else once they are there, no element comes or goes, sending is not blocked, and Stop is where it is. Nothing here is an action, so the keyboard stays where it is.

**The end-to-end stack:** the decision's sentence "The end-to-end stack's fake model has no gate" is still true of the names it had. The fake is offered once more, as `queued`, on a provider of its own at the fake's address with `answers_at_once = 1`, and priced like `standard`, so that it isn't offered at a limit (`deploy/e2e/config/`): on the fake's own provider, every test's answers would wait for each other's. `e2e/waits.spec.ts` gives each of its requests for a turn that model's name on their way out. A guest asks a question whose answer the fake writes slowly, and in a new conversation a second: the page says "Waiting behind … other answer…" under it, and again after a reload, which joins the turn's stream in Valkey from its start; once the first answer has ended, the second is written. The number is matched by a pattern, and the waits are long enough for another run of the same test at that gate. The model that names conversations is on the provider without a gate: a title waits for nothing there.

**Unchanged:** the rule, `full`, `waiting` and `accounts`, what is remembered and forgotten, and every case of a cancellation above; `passing(account)` without `placed`, as a title waits; the title job's code; the beat, Stop and the shutdown; the log's lines; the routes and `apps/api/openapi.json`; the tables, with no migration; the settings; the containers; `packages/llm` and `packages/fakellm`; and what a turn that didn't wait streams, byte for byte, which every other recorded stream holds.

**Tests:**

- **the gate's** (`test_catalog.py`): an answer that passes at once told nothing, and one that comes told its number in the step in which it comes; the gate's books complete when it tells; at one at a time, three accounts' answers told 1, 2 and 3 in the order the rule has them pass, A's second behind B's and C's first, and one less each as each place goes on, nobody the same number twice; a number that goes up, when another account's first comes; an answer that gives up waiting, those behind it told, those before it not; an answer cancelled in the step in which a place is given back, which counts for nobody's number and is told nothing more; an answer cancelled before its account's next one, not yet out of its line when another comes, which isn't played forward in the next one's place; an answer handed a place and cancelled in that step, after which the others' numbers are right; two at once, where the numbers are those of the places coming back in the order they were given; a `placed` that raises, logged, the others told, the place not lost; a title's answer among those that are told, counted in their numbers; nothing played forward while none asked, and again once those that asked have gone; and a property test beside the one against the plain gate: after any history of answers that come, end, are cancelled and give up, at a gate of one to four places, with nobody coming or leaving and the places given back in the order they were given, each waiting answer that asked passes as the one whose number it was last told, with an example of its own for the place a forward play gives being counted as held;
- **a turn's** (`test_turns_running_api.py`): a turn that waits behind two, then one, then passes, whose stream has `data-waits` 2, 1, 0 between `start-step` and the answer's first chunk and nothing else new, with the log as before, beside a turn that passed at once and says nothing, and one that waited behind one; a number that goes up reaching the stream; a number noted while the one before it is handed on slowly, dropped once the turn passes, with never a number after the 0; whoever joins a turn that waits reading the numbers from the first one on; a turn stopped while it waits, which hands on no 0, ends as before and leaves no task; a turn stopped while its number is being handed on slowly, whose stop waits for the number, with nothing of the turn after it but its end; a turn stopped once it has passed, while its number is still being handed on, which says the number and then that its turn came before anything else, its model not asked meanwhile; and a shutdown, after which the turn that waited, and had said behind how many, hands on no 0 and leaves no task;
- **a stand-in's** (`test_turns_stand_ins_api.py`): a turn that waits at its stand-in's gate says so there, and one that waited at both gates says both;
- **the stream's** (`test_turn_fixtures.py`, `tests/contract/turn-stream.test.ts`): `waited.sse` recorded, and read by the real `ai` package into a message with one `data-waits` part, whose `data` ends as `{ behind: 0 }`, and the answer's words;
- **the page's** (`lib/chat/waits.test.ts`, `components/chat/waits.test.tsx`): the number from every shape of `data`, and the two forms of the words; the answer's line, one and the same element, saying "Waiting for the model…", behind two, behind one, "Waiting for the model…" again, and then the model's words alone; a conversation opened while its answer waits, ending with the newest number, also in StrictMode; Stop while it waits, after which nothing is said of a number; a stream that breaks off while it waits, after which no number is left; and a first question whose answer waits, in the list of conversations while it waits;
- **the end-to-end stack's** (`e2e/waits.spec.ts`, and `test_catalog.py` for its models file): above.

**Known, and left,** each worked out, and not seen, unless it says so:

- **the number is a guess at more than one answer at a time,** as the table above shows, and exact only at one at a time while nobody comes or leaves;
- **it can go up,** when somebody who has had less comes;
- **it is not a time:** a short answer and a long one count alike, as a place does;
- **a number that a newer one overtook before the turn could hand it on is never said:** a reader may read 3 and then 1. And a number that was true when one place had gone on, and no longer when the next had, in the same round of the event loop, may be said before the newer one;
- **a 0 follows a number, and never stands alone,** by the code: `Waits(0)` only once a number was handed on. As asyncio runs what is ready in the order it was made ready, the task that hands on is at its first number before the turn can pass, so a turn that came to wait says a number: reasoned from that order, and held by no test, since no test can have a turn pass sooner;
- **a turn stopped once it has passed, while its number is still being handed on, says the number and then that its turn came,** before it stops: its model is asked, and the request ends at its first wait. As before the step, for a turn stopped in the step in which it is handed its place;
- **cancelled from outside, without a `Halt`,** as when the process goes down faster than it could say so, a turn doesn't wait for the number under way: that one is handed on by itself, and its task ends then, read by nobody;
- **an answer the page doesn't read along with, but only looks for** ("An answer to this is still being written…"), says no number: a turn's record has none;
- **a title has no reader,** and is told nothing; its answer counts in the turns' numbers at the same gate, as any answer does;
- **two API processes have two gates, as before,** and each says the order of its own; under `make up` a title's request at the worker's own gate is in no number of the API's;
- **each change of the line plays the rule forward once, for all who wait:** for each answer that waits, a look at every account that has one waiting, so the work grows with the answers waiting times the accounts they belong to, on every place given and every answer that comes or leaves, while any of them asked, as every turn does. Not measured: an account has two or three answers at once at the most, and the line is as long as the people asking.

**How to reverse it:** in the code, `placed`, `_order`, `_tell`, `_to_tell` and `_places` in `actio.catalog.Gate`, with `_chosen` back inside `_next` and the places held counted again; `Waits`, `Written` and `_Told` in `actio.chat.answering`, and the `emit` that `_through` takes; `waits` in `actio.api.ui_stream`; `lib/chat/waits.ts` and `Waiting`'s `behind`; `waited.sse` and its test. On the end-to-end stack, `queued`, its provider and its price in `deploy/e2e/config/`, its spec, and what `test_catalog.py` holds of it. The page then says "Waiting for the model…" again, and a stream with the chunk would be read as before: the library keeps a part the page doesn't draw.

**Measured**

On 2026-11-26, on a MacBook Pro (M1 Pro, 16 GB), the machine of the measurements above. No cloud provider was asked anything for this step.

**On the running stack** (`make up` from the tree as it was committed, the shipped config, the local model `qwen3.5:4b-q4_K_M` on Ollama, which writes one answer at a time), in Chromium, by a script outside the repository that notes every change of what a page's status lines say, with the time. Guest A asked for about 350 words. Once that answer had its first words, A asked a short question in a second window, guest B one 1.8 s later, and guest C one 1.8 s after that; C's page was loaded again while its answer waited. One run; the times are seconds from the script's start.

| | Asked at | What the answer's line said, and from when | First words at |
|---|---|---|---|
| A's first | 0.8 | "Waiting for the model…" | 4.6; its place went on at 19.0, after 457 words |
| A's second | 4.9 | behind 1 at 4.9, behind 2 at 6.7, behind 3 at 8.6, behind 2 at 19.0, behind 1 at 19.6, "Waiting for the model…" at 20.3 | 20.7 |
| B's | 6.7 | behind 1 at 6.7, "Waiting for the model…" at 19.0 | 19.2 |
| C's | 8.5 | behind 2 at 8.6; the page loaded again at 10.2, and behind 2 at 10.3; behind 1 at 19.0, "Waiting for the model…" at 19.6 | 20.1 |

- **A's second answer's number went up twice,** as B's question came and as C's did: each had had nothing at the gate, and went before it. They were answered A's first, B's, C's, A's second.
- **Each page had two status lines, one after the other:** the line under a question that has no answer yet, for about a tenth of a second, as before the step, and then the answer's own, which was one and the same element from its first number to "Waiting for the model…".
- **B's question, its conversation's first, was in B's list of conversations while its answer waited,** in a picture of the page taken then.
- **The API's log, as before the step:** `turn …: waited 12.3 s for the model's server` (B's), `… waited 11.0 s …` (C's), `… waited 15.3 s …` (A's second).
- **In Firefox and WebKit,** a shorter look, a long question of A's and a short one of B's: B's line said "Waiting behind 1 other answer…" from the moment it was asked, for 16.4 s and 12.7 s, then "Waiting for the model…" for 0.2 s and 0.3 s, and then B's words.

**The end-to-end tests,** on a stack of their own: 81 of 81 passed. The new one eight times over, two at a time, so that two runs of it meet at the gate: 8 of 8, in about 24 s each, where it takes 12 s alone.

**Mistakes planted on purpose,** each alone in a scratch copy of the tree, with the tests that should notice run until the first one failed.

On the step's first build, while the review read it, 31 in the gate, the turn and the chunk. 23 failed a test. One, the task that hands on never clearing its note, has that task spin without ever giving the loop back, so no test could end: the runner's own time limit ended it. Seven failed nothing:

- **three were gaps, and each got a test:** the forward play counting an answer that was cancelled and hasn't left its line, which shows only where the cancelled one stands before another of its own account in its line; a turn stopped while it waits not waiting, shielded, for the number under way; and the same for a turn that has passed;
- **two were lines nothing can reach, and came out of the source:** a guard against a number said after a turn's wait is over, which the gate never says, and the note emptied when the wait is over, which the task that hands on never reads again;
- **two were no mistakes:** a 0 said for a wait in which a task was started and no number handed on, and the place at which that task notes that it handed one on. By the order in which asyncio runs what is ready, that task is at its first number before the turn can pass, and the note is read only once the task has ended ("Known, and left", above).

And 10 in the page, with the recorded stream among them: 8 failed a test. The two that failed none: the number said also where the line stands in for the renderer, which no stream the server writes can show, since a 0 comes before an answer's first word, and the guard stays; and the page taking any data part of a message for the number, where a message has no other.

On the tree as it was committed, 30 in the gate, the turn and the chunk: without the two whose lines came out and the one that never gives the loop back, and with two more, a turn that gave up waiting saying that its turn came, and a 0 said before the number under way. 28 failed a test, and the two that are no mistakes failed none. The page's source did not change after its run.

The new end-to-end test, against a stack built from the scratch copy with a mistake planted: with the page never saying a number, and with the turn not asking its gate to be told, it failed each time, at the line it looks for.

**The review,** by a reviewer that had not seen the build, once: nothing that had to be fixed, and seven smaller points, all taken. A test's account of why the order of the places matters, which was wrong; the stop's shielded wait and the forward play's skipping of a cancelled answer, untested, which the planting showed too; a shutdown's test that would have passed with no number said; what the forward play costs, said too lightly here; two sentences about the made-up traffic that went beyond its figures, which a second run of the throwaway then settled as they stand above; that the books are complete for each change, not for each step of the loop; and a version of the chat library in a docstring, which had been 7.0.169 since that file was written, where 7.0.166 is the one installed and pinned.

**Not measured:** a server that writes several answers at once, on a real stack: its numbers are the made-up traffic's, and the tests'; the forward play with very many answers waiting; what a screen reader says: that the line is a status, and one element whose words change, the tests hold and the look saw, and nobody listened to one; and the new end-to-end test on CI's machines, where the first answer's ten seconds have to cover a question, a look, a reload and a second look, which took about two seconds here.

### 2026-11-28: the refusal of a body too large, on the developer gateway

"The refusal is a problem document, like every error of the API", in the decision, holds beside the developer gateway alone. On it, a path that starts with `/v1/`, every error is OpenAI's, which OpenAI's SDKs read ([ADR 0034](0034-the-gateway-answers-in-openais-form.md)), and so is the proxy's refusal there: `{"error": {"message": "This is too much to send at once: Actio takes at most 2 MB in one request.", "type": "invalid_request_error", "param": null, "code": null}}`, as `application/json`. The last sentence of the chat's words, "Shorten the message, and send it again.", is the chat's advice, and is left out. The limit, and the refusal elsewhere, byte for byte, are as they were. `handle_errors 413` tells the two apart by the request's path, with the matcher by which the proxy sends `/v1/*` to the API.
