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
