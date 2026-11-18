# 0022. Turns that run apart from their request: a stream to read along with, and a way to stop

- **Status:** accepted
- **Date:** 2026-11-16

## Context

A turn lived inside the request that started it ([ADR 0014](0014-turns-streamed-in-the-ai-sdks-format.md)). The request asked the model and sent the answer back as it came. When the connection closed, the server took that for "stop", closed the model's stream, and kept what had arrived.

That made every way of leaving the same thing. Reloading the page stopped the answer. So did a connection lost for a second, and so did opening another conversation, which [ADR 0018](0018-the-list-of-conversations-is-the-servers.md) called "the least good part of this step, and temporary". An answer could be read only by the request that had asked for it, and only once.

- **People leave without meaning to stop.** A laptop's lid closes, a phone changes networks, someone looks at another conversation while a long answer is written.
- **An answer takes a while here.** A small model on a laptop writes about 40 tokens a second, after up to a minute of loading.
- **The page reads answers with a library,** the AI SDK, pinned to one version ([ADR 0015](0015-the-web-chat-calls-the-api-from-the-browser.md)). It has its own way of joining a stream again: a `GET`, answered with the stream, or with 204 when there is nothing to join. It takes what it gets for the whole answer from its first event, unless told otherwise.
- **An answer is paid for** ([ADR 0020](0020-usage-limits-on-turns.md)). Credits are held while it is written, and what it used is counted when it ends. A hold lasted ten minutes, the longest a turn could be `running`.
- **Valkey keeps nothing on disk** ([ADR 0020](0020-usage-limits-on-turns.md)). What is in it is gone after a restart.
- **One process serves the API.** A worker process comes later ([ADR 0001](0001-one-app-several-process-roles.md)).
- **A process is told to stop** whenever the stack is updated, and can be killed without being told.

## Options considered

**Where a turn runs:**

1. **Inside its request,** as before.
2. **In the worker process,** taken from a queue of jobs.
    - Pro: the API only reads and writes.
    - Con: there is no worker yet, and a queue in Postgres is looked at every so often: its wait would come before every answer's first word.
3. **In the API process, as a task of its own,** beside the requests.
    - Con: the process that serves requests also has answers to finish when it stops.

**How a reader gets what a turn writes:**

1. **From the turn itself,** in memory, with a copy kept somewhere for whoever joins late.
    - Con: two ways of reading, one for the first reader and one for the rest, and the second is the one that is rarely used.
2. **From rows in Postgres,** a row for each piece.
    - Con: a write to disk for every word, for something that is thrown away a minute later.
3. **From a stream in Valkey,** one per turn, that every reader follows.
    - Pro: one way of reading. The request that started the turn reads like anyone else.
    - Pro: it is the same from any process.
    - Con: every piece of every answer passes through Valkey.

**What someone who joins gets:**

1. **Only what comes from then on.** The page would have to get the answer so far from somewhere else, and join the two without a seam.
2. **Everything from where they left off,** named by the id of the last event they saw.
    - Pro: nothing is sent twice.
    - Con: the library keeps no such id, and can go on with a half-read message only inside the page that began it: not after a reload, which is the case that matters most.
3. **Everything, from the answer's first event.**
    - Pro: what the library expects, and the stream can be read by itself, by a page that has just been opened.
    - Con: what was read before is sent again. That is one answer at most.

**What ends a turn before its model has finished:**

1. **Its reader's connection closing,** as before.
2. **Being told to,** by a request of its own.

**How a turn that is being written is told from one that was left behind** by a process that died:

1. **By its age.** A turn older than ten minutes was taken for dead, as before.
    - Con: wrong both ways. A dead turn blocks its conversation for ten minutes, and a turn that is alive after ten minutes is given up on while it is written.
2. **By a key in Valkey that runs out** unless the turn renews it.
    - Con: after a restart of Valkey every running turn looks dead, though each is being written and will be stored.
3. **By a time on the turn's own row,** which the process renews while it writes.
    - Con: a small write to the database every few seconds, for every turn that is running.

**What a shutdown does with the answers that are being written:**

1. **Nothing.** They die with the process, stay `running`, and are given up on later, with what they had written lost.
2. **Wait for them,** however long they take. Whatever stops the process kills it before that.
3. **Wait for a while, then end them** and record them.

**When the answers learn that the process is stopping:**

1. **When the web server shuts the application down.** That is the moment a web server has for it, and it comes after the requests it was answering have ended.
    - Con: a reader's request ends when its answer does. So the server waits for the readers until its patience is over, cuts them off, and only then are the answers ended. A reader is never told how the answer ended. Built first, and seen on the first try with a real model: see "Measured".
2. **When the process gets the signal.** The API listens for it itself, in front of the web server.
    - Con: it depends on how the web server handles signals.

**What an answer costs that a shutdown ended:**

1. **What it had written,** like one its reader stopped.
2. **Nothing.** Its reader didn't stop it: Actio did.

## Decision

**A turn runs in a task of its own in the API process. What it writes goes to a stream in Valkey, which every reader follows from its beginning. A reader who leaves ends nothing. A turn ends early only when it is told to: by a request to stop it, or by the process shutting down. While it runs, it says every 15 seconds that it is alive.**

**Starting a turn** (`actio.api.turns`). Everything that can refuse comes before anything is kept:

1. The conversation, the question and the turn are written, and not committed.
2. The credits are held ([ADR 0020](0020-usage-limits-on-turns.md)).
3. The turn's stream is opened, with its first event: the turn as it starts.
4. The transaction is committed.
5. The task that writes the answer is started (`actio.chat.running`), and the response begins to read.

So whoever finds a turn `running` also finds a stream to follow. If Valkey doesn't take the stream, the answer is 503, the hold is given back, and nothing is stored: an answer that nobody could read along with isn't started.

**The stream** (`actio.chat.events`):

- **One Valkey stream per turn.** An event is the bytes that go to the browser, in the format of [ADR 0014](0014-turns-streamed-in-the-ai-sdks-format.md). What reads and writes the stream doesn't know what is in them.
- **Every reader starts at the first event,** takes what is there, and waits for more, a second at a time. The turn's last event is marked, and readers stop after it.
- **It is not a record.** The answer is in the database when the turn has ended. The stream is deleted a minute after its last event, and a minute after the turn last said it was alive.
- **A stream that is gone doesn't start again.** After a restart of Valkey the turn's next piece finds no stream, and is not written to a new one: an answer without its beginning would be worse than none. The turn runs on unread, and is recorded as ever.
- **A reader who loses the stream is told so,** with an `error` chunk, and reads the conversation instead. The turn may well be running.

**Reading along:**

- **`GET /api/conversations/{id}/turns/{turn_id}/stream`** answers with the same stream as the turn's own request, from its first event. For a turn that has ended it answers 204: read the conversation.
- **The request that started the turn is a reader like any other.** Nothing reads from the turn itself.
- **A reader who hears nothing for 15 seconds is sent a line that is no event,** `: keep-alive`. A model that is being loaded is silent for up to a minute, and a proxy may close a connection that is. The library skips the line; a recorded stream with it is read in the web app's tests.
- **A reader holds no connection to the database** while it reads.

**Stopping:**

- **`POST /api/conversations/{id}/turns/{turn_id}/cancel`** closes the model's stream at whatever it was waiting for, and answers once the turn is recorded, with the turn: `stopped`, and what it had written kept. Whoever reads the conversation right after finds it so.
- **Whoever is reading along is told too:** the stream ends as a whole answer's does, and the turn in its last chunk says `stopped`.
- **A stopped answer is counted** by guess for what it wrote, as before ([ADR 0020](0020-usage-limits-on-turns.md)).
- **A turn that had ended is left as it ended,** and answered the same way. A stop that comes a moment too late is no error.
- **Deleting a conversation stops the answer that is being written in it** first. Nobody could read it.
- **A step that has come from the model is never cut in two.** It is stored and streamed whole, and the stop takes hold at the next wait for the model. What readers were shown is what is kept.

**Alive:**

- **A running turn says every 15 seconds that it is alive:** a time on its row, `alive_at`.
- **One that has been silent for a minute is given up on,** as `interrupted`, the next time someone reads its conversation, asks in it, or tries to read along or stop. Before, that took ten minutes from when the turn began, however alive it was.
- **The same beat renews the hold on the turn's credits** and keeps its stream. A hold lasts a minute now, not ten: when a process dies, its credits are back a minute later.
- **A turn that ends after it was given up on is left as it was.** Its answer isn't stored: the conversation may have moved on. And it isn't charged for. Before, it was.

**A shutdown:**

1. The process is told to stop, by Docker's signal or by Ctrl-C. The API hears it at that moment (`actio.core.signals`), and the web server right after: it takes no new requests, and waits for the ones it is answering.
2. The answers that are being written get 15 seconds to finish.
3. What is left is halted, and recorded as `interrupted` with what it had written. Whoever was reading along is told so in the stream, and their request ends.
4. With its requests ended, the web server shuts the application down. Anything still being written then is ended the same way.

- **An interrupted answer costs nothing.** What it wrote is kept, and the page offers to try again.
- **A turn started in those 15 seconds** has what is left of them.
- **Three waits fit inside each other,** and tests fail when they don't:
    - the answers': 15 seconds, and up to 5 to record them;
    - the web server's, for its requests: 25 seconds. If it were the shorter one, a reader would be cut off before the answer had ended;
    - Docker's, before it kills the container: 35 seconds (`stop_grace_period`), where its own patience is 10.

**The page,** in this step:

- **Stop** ends the reading at once. Then the page reads the conversation, stops the turn that it says is running, and reads again: what it shows is what the server kept.
- **Leaving a conversation, reloading, closing the tab** end the reading, and tell the server nothing.
- **Coming back to an answer that is still being written** shows that it is, and looks again every second and a half until it is done, as the page already did for an answer asked for in another window. Reading along from the page comes in the next step.

**One process.** A request to stop reaches a turn only in the process that is writing it, which today is the only one. A turn that the process isn't writing can't be stopped, only given up on.

## Measured

On a MacBook Pro (M1 Pro, 16 GB), 2026-11-16, through the proxy of `make up`, with the default model (`qwen3.5:4b-q4_K_M`).

**An answer on its way through Valkey:**

| | |
|---|---|
| First event of an answer, model not loaded | 0.02 s, as before this change |
| Pieces arriving at the reader | 38.8 a second, where the model reports writing 39.0 tokens a second: the detour costs nothing that shows |
| Joining an answer in its middle, a second after leaving it | first event after 0.01 s, and the 66 pieces written until then within 0.05 s |
| What was read along, joined or not, against what was stored | the same 1,740 characters |
| An answer's stream in Valkey | 29 kB for an answer of 250 words, in 335 events; gone a minute after its end |

**Stopping:**

| | |
|---|---|
| From the request to stop until it is answered, the turn recorded | 0.03 s |
| First words of the next question, asked right after | 0.26 s later: the model had stopped writing |
| In the page, from the click on Stop to the notice | 0.09 s, and 0.23 s on a second try with the machine busy. Seven requests in all: the conversation, the stop, the conversation again, and the list and the usage counts twice each |
| The words on the page after Stop, and after a reload three seconds later | the same 29 |

**A page that goes away,** in a headless Chromium:

- **Reloaded with 28 words of an answer on it,** the page said that an answer was still being written. 8.1 seconds later it showed all 252.
- **Left for another conversation with 22 words on it,** and opened again a second and a half later: still being written, then all 237. Nothing asked the server to stop.

**Stopping the API's container while it writes a long answer** (`docker compose stop api`):

| | Took | The reader | The turn afterwards |
|---|---|---|---|
| As first built, someone reading | 25.9 s | cut off after 20 s, by a broken connection | `interrupted`, 5 s after its reader was gone |
| As first built, nobody reading | 5.7 s | | `interrupted` |
| As decided, someone reading | 15.7 s | read on for 15 s, then was told `interrupted` in the stream | `interrupted`, with its 3,067 characters, no credits, nothing held |
| As decided, nobody reading | 15.5 s | | the same, with 2,910 characters |

The first two rows are what the web server's own order gives: it waited 20 seconds for the reader, cut the connection, and only then let the application end the answer. That is why the API listens for the signal itself.

**Eighteen mistakes were planted,** one at a time, to see the tests fail:

| The mistake | Tests that failed |
|---|---|
| A running turn never says that it is alive | Three: the turn given up on while it is written, the hold that runs out, the stream that runs out |
| A turn is given up on by its age, not by its silence | Two |
| A turn that was given up on is charged when it ends after all | One |
| A shutdown records turns as stopped by their reader | Six |
| An answer that a shutdown ended is charged for | Two |
| Answers learn of a stop only when the web server shuts the application down | One: a real server with a reader connected, sent a real signal |
| A reader keeps its connection to the database while it reads | One: twelve readers, and a request beside them |
| A stream that is gone starts again in the middle | Two |
| A reader waits for ever for a stream that is gone | Four |
| A turn that has ended still offers a stream to read along with | Two |
| A reader who hears nothing is never sent a line | Two |
| The stream is opened after the turn is committed | One |
| A stop answers before the turn is recorded | Nine |
| A stop cuts a piece off between storing it and streaming it | One |
| Deleting a conversation leaves its answer being written | Three |
| The page stops reading, and asks nobody to stop writing | Four |
| The page stops an answer whenever it stops reading it | Two |
| The page forgets an answer that ended while nobody read it | One |

Two of them showed gaps in the tests first, which were closed:

- **A stop that cuts a piece off failed nothing.** Every test stopped a turn while its model was silent, and none in the middle of a piece. A test now holds a piece back on its way to the readers, stops the turn, and compares what was streamed with what was stored.
- **Two mistakes made tests wait for ever instead of failing:** a reader of a stream that never ends, in a test without a time limit. Every request that could wait for ever on a broken server has one now.

**Every test in a real browser six times over, two at a time: 180 of 180 passed.**

## Consequences

- **Easier:**
    - an answer survives a reload, a lost connection and a look at another conversation;
    - a second window, and later a reconnecting page, read the same answer by the same route;
    - a stop is a fact the server confirms, not something inferred from a closed connection;
    - a dead turn frees its conversation, and its credits, after one minute;
    - updating the stack doesn't leave turns `running`.
- **Harder:**
    - **an answer goes on being written, and paid for, when nobody reads it.** Only Stop ends it, or deleting its conversation;
    - **every piece of an answer takes a round trip to Valkey** before the next is asked for. See "Measured";
    - **Valkey is in the middle of every answer.** If it goes away while one is written, the answer is finished unread, and its reader sees the conversation instead of the words arriving;
    - **a reader asks Valkey once a second** while it waits, and a turn writes to the database every 15 seconds;
    - **three waits have to fit inside each other,** one of them in compose.yaml. A test holds them together;
    - **the API listens for the stop signal in front of the web server,** which is the web server's business everywhere else. A test sends a real signal to a real server with a reader connected, and fails if the reader isn't told.
- **Not done yet:**
    - **the page doesn't read along again.** After a reload it waits for the answer to be done. The next step joins the stream;
    - **nothing limits how many answers one account has written at once,** and a local model that answers one at a time gets them all at once. A gate in front of the model, and a limit per account, come with it;
    - **what a killed process had written is lost,** though the stream may still hold it for a minute. The job that goes through stale turns comes with the worker process ([ADR 0020](0020-usage-limits-on-turns.md));
    - **a stop reaches only the process that writes the turn.** With several API processes it would have to travel through Valkey;
    - **nothing limits how many readers one account has open;**
    - **a reader can't go on from the middle.** Every join is sent the whole answer so far;
    - **a hold that ran out still shows as held** in `GET /api/usage` until the account's next question: the engine clears expired holds when it makes a new one ([ADR 0006](0006-usage-limits-engine.md)). The page's meter shows what was used, not what is held.
- **How to reverse it:**
    - **running turns in the worker** changes who calls `TurnRunner.start`. The stream, the two routes and the page stay as they are: they never read from the turn itself;
    - **another place for the events** than Valkey is another `actio.chat.events`;
    - **stopping on leave again** would be the page's choice: it can call the stop route when it stops reading.

## Revisions

### 2026-11-17: the page reads along again

The page joins a turn's stream now. "The page doesn't read along again", under "Not done yet", is done, and the third point of "The page, in this step", that coming back looks every second and a half, is overtaken.

**What the page does now.** When the conversation it shows says that an answer is being written, and the page isn't reading it, the page asks for that turn's stream: after a reload, on coming back from another conversation, in a second window, and after its own reading broke off while the server can still be reached. The words arrive as they are written, from the answer's first event, and Stop works as for an answer the page asked for itself.

- **While the stream is asked for,** the page shows what it shows right after a question was sent: the question, and the wait for the model. The box is blocked, as it is while an answer is written elsewhere.
- **A stream that ends cleanly** brings the turn with its last chunk, however the turn ended: completed, stopped elsewhere, interrupted. Nothing more is read.
- **Nothing to join,** a 204, means that the turn ended between the read and the join. The page reads the conversation again and shows what it left, and reads the usage counts and the list again.
- **When the join can't be made,** or the stream it reads ends in an error, lost or broken off, the page reads the conversation again. If the same turn is still running, the page does what it did before this revision: it says that an answer is still being written, blocks the box, and looks every second and a half until the turn is over.

**Decided:**

- **The page joins by hand, with the turn's id.** The library can join by itself whenever its component appears (`useChat({ resume: true })`), asking for a stream that the conversation alone names. Actio's streams are a turn's, and the page already knows from the conversation whether anything is being written. So nothing is asked when nothing is being written, which is nearly always.
- **One try for each running turn, each time the conversation is shown. If it doesn't work, the old way.** A stream that is gone stays gone: asking again would go in a circle. A look costs one read of the conversation, and always comes to an end. A request that the page itself gave up doesn't count as the try: React's StrictMode, which Next.js runs in development, starts a component's effects, undoes them and starts them again, and only the second request is read.
- **A join that fails says nothing by itself.** The read of the conversation that follows says what is wrong if something is: the server can't be reached, the session has ended, the conversation is gone.
- **The notice of a reading that broke off goes once the page reads along again.** When the page's own answer broke off with the connection, the page said that the server can't be reached. Then it reached the server twice, read the conversation and joined, and the notice was no longer true, under an answer that went on arriving. It goes once a join's stream is being read, whichever turn's, and at the latest when that reading ends. Only that notice goes, the one set for the error that broke the reading, and only for a reading that had begun, whose answer carried its turn: a Stop that was refused or never sent, a session that ended, a read that failed, and a question that never reached the server keep theirs. So does the break, for as long as no join gets to read.
- **Stop is offered once the library reads,** not while the stream is still being asked for. Before the stream has begun the library has nothing to stop, and that moment is one round trip long.
- **After a Stop that the API refused,** the conversation says that the turn is still running, and the page reads along with it again, where it used to look every second and a half: it hasn't tried to join that turn yet. The refusal stays on the page. An answer that was itself read along with is looked for instead.

**Measured:** on the same MacBook Pro (M1 Pro, 16 GB), 2026-11-17, through the proxy of `make up`, with the default model (`qwen3.5:4b-q4_K_M`), in a headless Chromium.

**A page that comes back:**

- **Reloaded with 25 words of an answer on it,** the page showed the question and 35 words of the answer 0.33 s after the reload began, with Stop. A second and a half later it showed 84 words, and at the end all 295, the same as stored. No notice said that an answer was still being written. Before this revision, in "Measured" above, the page said so, and showed the whole answer 8.1 s later.
- **A second window opened on the conversation while its answer was being written** showed its first words after 0.25 s, 25 at once. A second later both windows showed 56 words.
- **Stop pressed in that second window:** both windows said that the answer was stopped 0.13 s later, with the same 59 words, and showed the same after a reload.
- **Left for another conversation with 15 words on the page, and opened again a second and a half later,** the page showed 68 words 0.09 s after the click. Nothing asked the server to stop. At the end it showed all 314, the same as stored.
- **The page's own connection broken in the middle of an answer,** with 60 words on the page, by a small proxy in front of the stack that cut that one connection while everything else went through: the page read along again 0.05 s later, with 62 words. The notice that the server can't be reached was on the page for about that long, seen at 0.03 s and gone by 0.05 s. At the end the page showed all 308 words, the same as stored.
- **In each of these the page asked once to join:** no second try, and no look.

**Seventeen mistakes were planted,** one at a time, in a copy of the tree, to see the tests fail. The numbers are of component tests; the end-to-end tests were run against the first mistake too:

| The mistake | Tests that failed |
|---|---|
| The page never reads along, and only looks | 19, and of the end-to-end tests the three about reading along |
| The join asks at the library's own address, named by the conversation alone | 6 |
| The turn's id goes into the address as it is written | 1 |
| Whatever ends the reading stops the answer, leaving the conversation too | 4 |
| A turn that couldn't be read along with is never looked at again | 3 |
| A stream that ends in an error is joined again, as often as the server says the turn is running | 1 |
| A join that the page itself gave up counts as the one try | 2 |
| After an answer was read along with, its turn is still taken for one that nobody reads | 8 |
| A join that fails is shown as a problem of its own | 3 |
| After a 204 the usage counts and the list are not read again | 1 |
| What the server says about an earlier state is shown after the next question | 2 |
| Nothing shows that an answer is on its way while the join is asked for | 5 |
| A question can be sent while the join is asked for | 1 |
| The notice of a broken reading stays while the answer is read along with again | 1 |
| The same notice stays when the join's stream was read whole at once | 1 |
| A join takes away any notice that the server can't be reached | 2 |
| A question that never reached the server counts as a reading that broke off | 1 |

**One test tested less than its name said,** found while the notice was built. The component tests break a stream with a helper, `brokenAfter`, which queued the stream's first events and made the stream fail at once. A stream that fails drops whatever is still queued, so the page never read those events: "the page's own answer breaks off" had been a stream that broke before it began, and its answer never carried its turn. The helper now makes the stream fail only once its events have been read, and the test reads along with an answer that had begun.

**Every end-to-end test six times over, two at a time: 186 of 186 passed.**

**Still not done:**

- **the page doesn't keep trying while the server can't be reached.** A look that fails stops the looking; a reload starts it again;
- **there is no Stop while the page only looks;**
- **a conversation that is already open in one window isn't told of an answer asked for in another.** It learns of it the next time it reads the conversation;
- **a join is still sent the whole answer so far,** as "a reader can't go on from the middle", above, says.

### 2026-11-18: answers wait their turn, and an account has only so many

Two items of "Not done yet" are done, in [ADR 0023](0023-answers-that-wait-their-turn.md):

- **"Nothing limits how many answers one account has written at once, and a local model that answers one at a time gets them all at once."** A turn now waits at a gate in front of its model's server, inside Actio, and its model is asked only once the server has room. A plan may say how many answers one account has being written at once, and one more is refused.
- **"Nothing limits how many readers one account has open."** An account has six at most; a seventh may not join. The request that starts a turn is never refused for it.

What is decided above holds for a turn that waits, as for one that is written: it says every 15 seconds that it is alive, which keeps its hold and its stream; Stop ends it at once, as `stopped`, with nothing written; a shutdown records it as `interrupted`. "One process" holds for the gate and the count of readers too: both are in the process, as the turns are.
