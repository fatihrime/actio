# 0017. End-to-end tests drive the real stack in a real browser, with a fake model

- **Status:** accepted
- **Date:** 2026-11-14

## Context

The chat is tested at two levels so far. The API's tests call it in-process, against a real Postgres and Valkey. The web app's tests render the real components in a simulated browser, against a fake API that answers with the API's recorded streams ([ADR 0015](0015-the-web-chat-calls-the-api-from-the-browser.md)).

Between them is everything neither sees:

- **the pieces put together:** the page, the proxy that must pass an answer through word by word, the API, the database, and a model's server;
- **what only a real browser does:** lay a page out and scroll it, draw a diagram, open a link in another tab, and fetch a picture without being asked ([ADR 0016](0016-answers-shown-as-markdown.md));
- **timing.** A simulated browser waits for whatever a test waits for. A real one doesn't.

Until now this was tried by hand, with throwaway scripts that drove a headless browser against `make up` and a real model. That found real defects in each of the last two steps, and left nothing behind to find the next ones.

- **A real model can't be in these tests.** It is 3 GB, it answers differently each time, and CI has no graphics processor.
- **The repository has a fake one** (`packages/fakellm`): a server that speaks the same protocol as other model servers, answers "Echo:" and the question, and does what directives in the question tell it.
- **The tests must not touch a developer's data,** and by the project's rule they must not read `.env`.

## Options considered

**What the browser is pointed at:**

1. **The web app alone,** with the API's answers made up in the browser.
    - Pro: no containers. Fast.
    - Con: tests the page against what its author believes the API does. The component tests already do that, better.
2. **`make dev`:** the API and the web app as native processes with hot reload.
    - Pro: starts in seconds.
    - Con: not what anyone runs. The development server compiles pages on demand and renders twice.
3. **The stack that `make up` runs,** in containers behind the proxy, with the model swapped.
    - Pro: the thing itself. What passes here works for the person who types `make up`.
    - Con: the images have to be built: a minute when little changed, several from nothing.

**Where the fake model runs:**

1. **On the machine itself,** reached from the API's container like Ollama.
    - Con: how a container reaches its host differs between macOS and Linux. On Linux the fake would have to listen on every interface of the machine.
2. **As one more container of the stack.**
    - Pro: the same on a laptop and in CI, and it can sit on a network with no way out.
    - Con: a third image to build.

**How a test tells the fake what to do:**

1. **A control API** on the fake: "the next answer fails".
    - Con: state between requests, so tests can't run side by side.
2. **Directives in the question,** which the fake already reads. A test types them into the chat box.

**How the test stack relates to the developer's own:**

1. **The same one:** `make up`, then run the tests.
    - Con: test guests and conversations in the developer's database, and the API switched from the real model to the fake one.
2. **A stack of its own:** another project name, another port, its own data, started and removed by the test run.

**The tool:** Playwright, which the plan named. It waits for what it asserts, records a trace of a failed test, and drives the headless Chromium that the earlier hand checks used.

## Decision

**`make e2e` starts a stack of its own, as `make up` would, with the fake model as one more container. Playwright drives it in a real browser, and the stack is removed afterwards.**

**The stack** is `compose.yaml` with an overlay, `compose.e2e.yaml`, which changes three things:

- **It adds `fakellm`,** built from `packages/fakellm/Dockerfile`. It is locked down like every other container, and sits on a network of its own with the API, with no way out.
- **It mounts another models file** where the API expects one (`deploy/e2e/config/models.toml`). It offers the fake under the same name, `standard`, so the web app asks for the default model as always.
- **It raises two rate limits.** Every test is a new guest, and all of them come from one address. The limits themselves are tested where a clock can be moved.

Everything else is the stack people run: the same images, the proxy, the migration job, the hardening. A test holds the overlay to that, and to every rule of [ADR 0005](0005-container-images.md).

**It runs beside a developer's own stack and never touches it** (`scripts/e2e.sh`): project name `actio-e2e`, port 8090, its own volumes, removed after the run. It reads no `.env`: its passwords are made up for the run.

**A test scripts the model by typing.** The fake gained one directive for this, `[[pace: 0.1]]`: wait between the pieces of an answer. Without it an answer is there at once, and nothing could be stopped half-way or followed down the page.

**Two things are checked in every test,** whatever it is about (`apps/web/e2e/chat.ts`):

- **Nothing is asked of another site.** Every such request is refused and noted, and a test fails unless it said which to expect. One test expects one: the address of a link, after "Open link".
- **No script on the page failed.**

**What the tests cover** is what was tried by hand before, and what the component tests can't see:

| File | Covers |
|---|---|
| `conversation.spec.ts` | A first visit asks at once and reads the answer while it is written; a reload; a follow-up; New chat and Back; someone else's conversation doesn't open |
| `answers.spec.ts` | Markdown in a real browser: formulas, a table, coloured code, a drawn diagram and a broken one. HTML is not obeyed, a picture is not fetched, a link asks first and opens in a tab that can't reach back |
| `turns.spec.ts` | Stop keeps what was written; a failed answer, tried again and reworded; an answer that breaks off; reasoning shown and folded away. After each, a reload shows the same |
| `scrolling.spec.ts` | The page stays at the newest words of a long answer, leaves a reader who scrolled up 6 pixels, and follows again after the button |

**No test is run again when it fails.** A test that fails now and then is a mistake to find: in the test, or in the page.

**CI runs it as a job of its own,** beside `make check` and the quickstart, and keeps the trace of a failed run for a week.

## Measured

On a MacBook Pro (M1 Pro, 16 GB), 2026-11-14:

- **`make e2e` takes 35 to 45 seconds** when the images are built: about 20 to start the stack, 16 for the 13 tests, run side by side.
- **Running each test several times found a defect in the page.** One test failed in three runs of four: a question sent right after a failed answer named a message the server didn't have. See the second revision of 2026-11-14 in [ADR 0015](0015-the-web-chat-calls-the-api-from-the-browser.md).
- **With that fixed, each test six times over, two at a time as a CI runner does: 78 of 78 passed.**
- **Two mistakes were planted** in the web app, to see the tests fail: pictures let through again, and a small scroll up ignored. Each failed the one test meant for it, and no other.

## Consequences

- **Easier:**
    - a change is tried the way a person would meet it, by one command, without a model;
    - what an answer can't do is proved in the browser it would do it in;
    - the next defect of timing has somewhere to show up.
- **Harder:**
    - **a third image** to keep building, though only tests use it;
    - **`make e2e` needs Docker and a minute,** so it isn't part of `make check`. CI runs it on every push;
    - **Playwright downloads a browser** the first time: Chromium's headless build, about 90 MB. A run that shows the browser (`--headed`) needs the whole of it, about 200 MB more. The script fetches whichever the run needs;
    - **a test waits on a real page.** A test written carelessly fails now and then. The tool's own waiting is used throughout, and one fixed tolerance is explained where it stands (`scrolling.spec.ts`).
- **Not done yet:**
    - only Chromium, at a desktop's size. A phone's size comes with the mobile layout;
    - no test with a real model. The model clients have theirs (`make test-live`);
    - the model's server being away, and the limits being reached, are tested below this level only;
    - the time Actio adds before an answer's first word isn't measured here yet.
- **How to reverse it:** the tests are plain Playwright, and know the stack only by its address (`ACTIO_E2E_URL`). They would run against any other way of starting it. The overlay, the image and the script are three files.

## Revisions

### 2026-11-14: the list of conversations, and stopping a stack that was kept

**One more file of tests,** `sidebar.spec.ts`, for the list of conversations ([ADR 0018](0018-the-list-of-conversations-is-the-servers.md)):

- a conversation is listed once it was asked, and the one continued last is on top;
- a rename and a delete are still true after a reload, and a deleted conversation is gone at its address;
- a list longer than a page is read in two pages, with 31 conversations made through the API;
- leaving a conversation stops its answer;
- **one test at a phone's width,** where the list lies over the chat. "Only at a desktop's size", under "Not done yet", is no longer quite true: this one test is what the list needs, not a set of tests for phones.

That makes 19 tests, in about 16 seconds.

**`make e2e args=down` stops a stack that `ACTIO_E2E_KEEP=1` left running.**

- **What was wrong.** The script printed a `docker compose … down` command for that. It failed as printed: Compose reads the stack's two passwords even to stop it, and outside the script they aren't set.
- **Why it surfaced only now.** The printed command was never run as printed. A kept stack was always followed by the next `make e2e`, which removes what it finds before it starts. It was run for the first time while measuring for this step.

### 2026-11-14: accounts, and two more limits raised

**One more file of tests,** `account.spec.ts`, for signing up and logging in ([ADR 0019](0019-accounts-in-the-one-screen.md)). These need the whole stack more than most: a real cookie, a real password check, and the database moving a guest's conversations.

- a guest who signs up keeps their conversation, and is still logged in after a reload;
- logging out leaves nothing of the account on the page, and logging in brings it back;
- what a guest started goes along into the account they log in to;
- a wrong password is refused, and an address signs up only once, however it is typed;
- changing the password logs another browser out, and only the new password works there;
- each of the three places opens by its address, and one test runs at a phone's width.

That makes 26 tests, in about 18 seconds. Each test signs up under an address made up for it.

**The overlay raises two more limits:** sign-ups and logins per network address. The decision above raised two "because every test is a new guest, and all of them come from one address"; the same holds for accounts.

- **Not raised: password attempts per email address.** Every test has an address of its own, so it has that limit to itself, as a person would. A test of the containers' rules holds the overlay to exactly this.

### 2026-11-15: the usage limits, with a limit within reach and a model for nothing

**One more file of tests,** `usage.spec.ts`, for the usage limits in the page ([ADR 0021](0021-usage-limits-in-the-page.md)). Only the whole stack counts credits, refuses a question and names a model that costs nothing:

- a guest's meter starts empty, and has counted the first answer;
- an account asks two long questions, is told that half of its session is used, and has the third refused: with the time left, nothing more sent, and the fake under its second name to carry on with. That model answers the refused question, the counts stay as they were, and a reload shows the same counts;
- a question that leaves little room is answered as far as the room goes, and the page says that the limit is why.

That makes 29 tests, in about 18 seconds.

**The stack's two files changed for it** (`deploy/e2e/config/`). The decision above said the models file "offers the fake under the same name, `standard`"; it now offers it twice.

- **The fake a second time, as `local`,** which the plans file prices at nothing. The API then names it in a refusal.
- **An account may use 500 credits a session,** at a credit a word: two or three long questions. A guest's 100,000 are out of reach, as every other test needs. So a test of the limits signs up first, and no test has to share an allowance.
- **The smallest answer worth giving is 64 words there,** not 256, so that 500 credits can be asked something at all.
- **A test in the API's suite holds the two files to this,** so that a change to them fails there, in a second, and not here.

**The numbers in the tests are measured, with room to spare.** What a question costs on this stack is exact: the fake counts words.

- **Two questions of 40 words cost 106 and 187 credits.** The third would have 227 words to read, and 207 credits are left. With questions of 30 or of 50 words the same two fit and the third doesn't; only the share that is passed differs.
- **An answer is cut for a question of 190 to 280 words.** At 180 the whole answer fits, and at 290 the question alone is more than the plan allows. The test asks 220.

### 2026-11-16: an answer that outlives its page

Turns run apart from their requests now ([ADR 0022](0022-turns-that-run-apart-from-their-request.md)), and what only the whole stack shows about that is tested here.

- **"Leaving a conversation stops its answer",** in the list of 2026-11-14 above, was a test of what is no longer so. It now waits for the opposite: the answer that was left is whole when its conversation is opened again.
- **One test more,** in `turns.spec.ts`: the page is reloaded in the middle of an answer, and the answer is whole afterwards. The fake model writes a word every 0.3 seconds there, so the answer takes longer than the reload.
- **The test of Stop reads as before,** and tests more than before: that the page asks the API to stop, and that the API does. A page that only stopped reading would now show the whole answer after its reload.
- **That makes 30 tests,** in about 20 seconds.
- **Not tested here:** what a shutdown does to an answer. Stopping the stack's own API in the middle of a test would take the other tests with it. The API's suite sends a real stop signal to a real server with a reader connected, and the decision's "Measured" has the container stopped by hand.

### 2026-11-17: the page reads along again

The revision above said the page "looks every second and a half" for an answer it isn't reading, and that reading along again was the next step. The page now joins the turn's stream ([ADR 0022](0022-turns-that-run-apart-from-their-request.md), revision of the same day), and the tests that waited for a whole answer now watch it arrive.

- **The reload in the middle of an answer** (`turns.spec.ts`): after the reload, the words written so far are on the page while Stop is offered and the last word is not, and then the whole answer. A page that only looked would show nothing of the answer until it was done.
- **Coming back from another conversation** (`sidebar.spec.ts`): the same, with Stop offered while the answer is written.
- **The answer in these two is twenty words at half a second each,** where it was twelve at 0.3. The page has to be back and reading along before the answer ends, or the join is answered 204 and the test fails on a page that did nothing wrong. Twenty words leave about eight seconds for that, which a slow runner shouldn't use up.
- **One test more,** in `turns.spec.ts`: a second tab of the same browser opens the conversation while its answer is written, reads along, and stops it. Both tabs then say that it was stopped and show the same words, and a reload shows them still. Two readers told the same thing by the server are what only the whole stack shows.
- **That makes 31 tests.**

### 2026-11-18: answers at once, and a request too large for the proxy

An account may have only so many answers being written at once, and the proxy refuses a request over 2 MB ([ADR 0023](0023-answers-that-wait-their-turn.md)). Two tests more, in `turns.spec.ts`:

- **a guest with two answers being written,** in two conversations, asks a third. It is refused in the API's words, and the question is back in the box. One of the two is stopped from its conversation, and the same question is then answered, while the other is still being written: the stop made the room. The two answers take twenty seconds each, at a word a second, where the test needs about eight;
- **through the proxy,** with the browser's own session: a body over 2 MB is answered 413, as a problem document with words for the page, and one under it, whose message is a character longer than the API takes, reaches the API and is answered 422. These are requests the page wouldn't send, so the test sends them itself, with the browser context's own requests.

The plans file of the stack gives guests and accounts what people get, two and three answers at once (`deploy/e2e/config/plans.toml`). A test in the API's suite holds it to that.

**Not tested here: the gate in front of a model's server.** The page can't choose a model, and the stack's model, the fake, has to stay without a gate: the tests run side by side, and would otherwise wait for each other's answers. The API's suite tests the gate.

That makes 33 tests, in about 20 seconds. The two new ones were run five times over, five at a time: 10 of 10 passed.

### 2026-11-19: CI runs them without Ubuntu's package archive when it has to

The job that runs these tests in CI installed the browser with `playwright install --with-deps`, which first asks Ubuntu's package archive for the system packages a browser can use. On 2026-11-19 the archive answered 503 for a quarter of an hour and more, and the job failed twice, at 12:18 and at 12:27 UTC, before a single test had run. Nothing in the repository had changed that: the other three jobs of the same run passed.

- **What the archive adds is fonts.** The job's own log says so: every library the browser needs was on the runner already, which ships browsers of its own, and nine packages of fonts were to be fetched. No test here depends on a font: they read what the page says, and pass on a laptop with other fonts.
- **So a failure of that step is a warning now, not the job's end.** The step tries the same command as before. When it fails, it says so in the run's summary and installs the browser alone. Playwright installs the packages first and the browser after, so one that failed on the packages has downloaded no browser yet.
- **Why only now:** the job has run on every push since 2026-11-14, and until this day the archive had always answered.

### 2026-11-20: a second model in the fake, which names conversations

A model writes a conversation's title now, after its first answer, and the page shows it when it arrives ([ADR 0025](0025-titles-written-by-a-model.md), revision of the same day). The stack's models file named no model for titles: the tests find a conversation in the list by its first question's first line.

- **The fake's echo couldn't name conversations.** A title is asked for with the same instructions each time and the question quoted after them, so an echo would have named every conversation alike.
- **So the fake serves a second model,** `fake-quiet`, beside `fake-chat`. It says nothing by itself: its reply has no text, and ends as a finished reply does. Every directive works for both models.
- **One more directive, `[[say: Flat tyres]]`,** for the quiet model: the reply's text. Several are joined by a space. The model that answers leaves it out of its echo, like every directive. The request for a title quotes the question, directives and all, so a test names its conversation by typing.
- **The stack names a model for titles now:** `namer`, the quiet model, priced like `standard`, since a model that costs nothing is offered at a usage limit. Its conversations keep their first line, as the tests need, unless a question says otherwise. The worker waits for the fake to be healthy, as the API does, and a test of the containers' rules holds the overlay to that.
- **One test more,** in `sidebar.spec.ts`: a question that carries `[[say: Flat tyres]]` is listed by its first line while its answer is written, then by `Flat tyres` with nothing touched, and after a reload still.
- **That makes 34 tests.** The other 33 didn't change.
