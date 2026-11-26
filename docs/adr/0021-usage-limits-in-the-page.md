# 0021. Usage limits in the page: the server counts, and the page learns of a limit by being refused

- **Status:** accepted
- **Date:** 2026-11-15

## Context

The API holds every answer to usage limits ([ADR 0020](0020-usage-limits-on-turns.md)). An account has so many credits for a session and for a week. A question that doesn't fit is refused with a 429 that names the window, says when it starts over, and lists the models that cost nothing. Passing 50, 75 or 90 percent of a window is said once, in the stream of the turn that passed it. A turn says when its answer was given less room.

The page showed none of this. A refusal was a sentence to dismiss, and the next question was refused the same way.

- **The server is the truth,** about a conversation and about the list of them ([ADR 0015](0015-the-web-chat-calls-the-api-from-the-browser.md), [ADR 0018](0018-the-list-of-conversations-is-the-servers.md)). The page reads again instead of working out what the server has.
- **A page has several sessions in its life** ([ADR 0019](0019-accounts-in-the-one-screen.md)). What it reads for one must not show in the next.
- **Whether a question fits is the server's to say.** It depends on the question's length, on the model's price, on a quarter held on top of a guess, and on the smallest answer worth giving.
- **The browser's clock is not the server's.** It can be minutes off, and nobody notices.
- **A screen reader says a status again whenever something in it changes.** A time that counts down changes every second.
- **The page asks the default model,** and has nothing to choose a model with. That comes with the router.

## Options considered

**How the page knows how much is used:**

1. **It counts:** every turn says what it cost, and the page adds that up.
    - Pro: no request.
    - Con: it sees only the turns of its own window. A second window, a reload, and later the developer gateway, all use credits it never saw.
2. **It reads the counts,** from `GET /api/usage`, and again whenever they may have changed.
    - Con: one more request for each turn.
3. **The API sends the counts with every answer,** in the stream.
    - Con: a second way to learn the same thing. The first is still needed, for a page that was just opened.

**How the page knows that a limit is reached:**

1. **It works that out from the counts,** and stops sending when too little is left.
    - Con: the page would repeat the server's rule for what fits, and the two would drift apart. It would stop a question that fits, or send one that doesn't.
2. **It is told, by a refusal.** The 429 names the window and the time to wait.
    - Con: a question has to be refused before the page knows. A page that is loaded again has to ask once more.

**Until when a limit that was reached is held:**

1. **Until the moment the API named,** `resets_at`, by the browser's clock.
    - Con: a clock five minutes fast lets questions through five minutes early, to be refused again.
2. **For as long as the API said to wait,** `retry_after`, from the moment its answer came. The length of a wait is the same on every clock.

**Where it is held:**

1. **In the conversation that met it.**
    - Con: a new conversation asks again and is refused again, and a model chosen to carry on with has to be chosen again in each.
2. **For the session,** in every conversation.

**What the offer of a model that costs nothing does:**

1. **It sends the refused question to that model, once.**
    - Con: the next question is refused again.
2. **It has that model answer until the limit starts over,** and the usual one again afterwards.
3. **It opens a list of models to choose from.** That is the model picker, which comes with the router and with more than two models.

**How a share that was passed is said:**

1. **In a box that appears over the page and goes by itself** after some seconds.
    - Con: gone before a slow reader has read it, and it can't be asked to stay.
2. **In a notice where questions are written,** which stays until it is dismissed or the next question is sent.

## Decision

**The page shows what the server counted, and reads it again whenever it may have changed. It learns that a limit is reached by being refused. It holds that for the session until the wait is over, and offers the models that cost nothing meanwhile.**

**The meter** (`lib/usage.ts`, `components/usage/meter.tsx`), under the list of conversations:

- **A bar for each limit of the plan,** the session's and the week's, with the plan's name. A plan without limits shows none.
- **The share is said in whole percent, rounded down.** It is "under 1%" as soon as anything is used, and never 100% before everything is. Credits that are only held, for an answer being written, are not counted: they come back when it ends.
- **When a limit resets is said by the reader's clock:** a time alone when it is today, with the day when it isn't. The words are English, like the page. Twelve hours or twenty-four is the reader's own, by their browser's language. A session that isn't open says that it starts with the next question.
- **The counts are read** when a session begins, after every turn however it ended, when the window is returned to, and when a window starts over. Not when an answer begins, as the list is: nothing that is shown has changed by then.
- **When they can't be read,** the meter says so in a line, and the chat goes on. Nothing asks again and again.
- **They are held under the session,** like the list of conversations ([ADR 0019](0019-accounts-in-the-one-screen.md)).

**A limit that is reached** (`lib/usage.ts`, `components/usage/limit.tsx`):

- **A 429 that names a window and a time to wait is a usage limit.** One that names no window is the limit on how often questions are sent ([ADR 0011](0011-rate-limits-as-token-buckets-in-valkey.md)), and is said as before. One that names a window and no wait is a question too large for the plan: no waiting helps it, so it is said as that question's problem, and the next one can be sent.
- **The page says which limit,** when it resets and how long that is, where questions are written. Nothing is sent meanwhile. The question that was refused is back in the box: the server kept nothing of it.
- **It holds for the session, in every conversation,** and is gone with the session.
- **It ends when the wait is over,** counted from the moment the refusal arrived. The page looks at the clock at least every half minute, because a timer set for hours comes late when the computer has slept. Then the counts are read again.

**A model that costs nothing:**

- **The refusal's models are offered by their titles,** as far as the installation's list has them: "Use Local (free)".
- **Choosing one has it answer every question,** in every conversation, until the limit starts over. The page then names that model in what it asks. Afterwards it names none again, which asks for the default.
- **The notice then says which model is answering,** and that the limit is still reached.
- **The keyboard goes to the box,** where the refused question waits.

**A share that was passed** is said in a notice where questions are written: "over 50%", with when the limit resets. It stays until it is dismissed or the next question is sent. A turn that passes two shares says the smaller first, and the page keeps the larger.

**An answer that was cut** because the limit left it less room says so, instead of naming the model's length limit.

**What a screen reader is told.** The sentence that a limit is reached is a status, said once. The time left is a timer beside it: read when asked for, and not at every second.

**How it is tested:**

- **The refusal is a recorded one.** Its members beyond the usual ones are in no generated type. So the API's tests record the 429 they get at a limit, as they record the streams ([ADR 0014](0014-turns-streamed-in-the-ai-sdks-format.md)), and the page's tests answer with that file.
- **The component tests set the clock and leave time alone.** Timers run as ever, so a test moves the clock and waits a second for the page to look. They run in one time zone on every machine.
- **The stack of the end-to-end tests has a limit within reach** ([ADR 0017](0017-end-to-end-tests-on-the-real-stack.md)): an account may use 500 credits a session there, at a credit a word, and the fake model is offered a second time, for nothing.

## Measured

On a MacBook Pro (M1 Pro, 16 GB), in a headless Chromium against the stack of `make e2e`, 2026-11-15.

| | |
|---|---|
| What one turn asks of the API | The turn, the list twice, and the counts once |
| `GET /api/usage` from the page, the median of 20 | 5.1 ms |
| Scripts a first visit downloads | 239 kB, 3 kB more than before |

**On that stack,** where an account's session allows 500 credits:

- **Two questions of 40 words cost 106 and 187 credits.** The second passed half of the limit, and its stream said so. The third was refused, with five hours to wait and `local` to carry on with.
- **A question of 220 words was given room for 146 of its answer's 221.** The answer stopped there, the turn said that the limit was why, and its stream said 50 and then 75 percent.

**A defect that the first run in a real browser found, in what was already there.** After signing up, the page no longer knew its models: no switch for reasoning, no line saying which model answers, and so no model to offer at a limit.

- **The cause.** When a session ends, the page drops everything it read for it ([ADR 0019](0019-accounts-in-the-one-screen.md)). The list of models was held under a fixed name, not under the session. The library that holds it doesn't ask again for what it was told two seconds ago. So a page that changed hands within two seconds of reading the models had none, until the window was next returned to.
- **Why it surfaced only now.** Nothing a test looked at after a change of session depended on the models. A person takes longer than two seconds to sign up, and after that the models were read again: the same test, made to wait 2.3 seconds first, passed without the fix. The offer of a free model depends on the models, and a test is fast.
- **The fix.** The models are held under the session, like everything else. A component test signs up at once, and failed before.

**Six mistakes were planted,** to see the tests fail:

| The mistake | Tests that failed |
|---|---|
| The limit held until the server's moment, by the browser's clock | Three |
| Questions still sent while a limit is reached | Three |
| The model that costs nothing chosen, and not named in what is asked | One |
| The counts not read again after a turn | Three |
| A limit kept for the session after the one that met it | One |
| A limit that never ends | Two |

**Every test in a real browser passed, 29 of 29,** and the three of the limits eight times over, three at a time: 24 of 24.

## Consequences

- **Easier:**
    - someone near a limit sees it coming, and at the limit knows how long to wait;
    - where a model costs nothing, the conversation goes on with it at one press;
    - the page has no rule of its own for what fits. A change to prices, plans or the server's rule needs nothing here;
    - a second window, or another way to use credits, shows in the meter the next time it is read.
- **Harder:**
    - **a question has to be refused before the page knows of a limit.** A page that is loaded again lets one more be sent, and refused;
    - **the meter is with the list of conversations.** With the list put away, as in a narrow window, it isn't seen until the list is. What is said where questions are written, the limit and the shares passed, is always seen;
    - **whatever the page reads from the API has to be held under the session,** the models now among it. The rule was there ([ADR 0019](0019-accounts-in-the-one-screen.md)); one reader had not followed it.
- **Not done yet:**
    - what each answer cost, shown with the answer: with the other figures of a turn;
    - a page for the account's usage over time. The API has no route for it yet;
    - a limit that another window met, known here without asking. `GET /api/usage` could say that nothing fits until when;
    - a question too large for the plan is not offered the model that costs nothing, though the refusal names it;
    - choosing a model at any other time: with the router.
- **How to reverse it:** the page's part is `lib/usage.ts`, `components/usage/` and what `components/chat/chat.tsx` does with a refusal. Without them a usage limit is again a sentence to dismiss, and the API is unchanged.

## Revisions

### 2026-11-18: a 429 without a window is also too many answers at once

"One that names no window is the limit on how often questions are sent", in the decision above, isn't the only such 429 any more. A plan may now limit how many answers one account has being written at once, and the API refuses one more with a 429 that names no window and has no time to wait: nobody knows when an answer ends ([ADR 0023](0023-answers-that-wait-their-turn.md)).

The page didn't change for it, and treats it as the decision says of any 429 without a window: the API's words, the question back in the box, and the next question taken as soon as it is sent. No limit is held, and nothing counts down. A component test sends such a refusal, and fails if the page takes it for a usage limit.

### 2026-11-26: a 429 without a window is also no model with budget left

A 429 that names no window is now also the refusal of a turn that no model with room for it has budget left for, by Actio's count of what its models' providers take: it says how long to wait, and names the model asked (`model`), and no window ([ADR 0030](0030-the-providers-budgets-are-counted.md), revision of 2026-11-26). The page's source didn't change for it: it is shown in the API's words, the question back in the box, as the revision above says. A component test answers with the refusal the API was recorded giving (`no_budget.json`). A notice that counts that wait down is the next step.

### 2026-11-26: a refusal for now is counted down

The revision above left the API's refusal of a question that no model with room for it has budget left for, a 429 that says how long to wait and names the model asked, in the API's words: a notice with "Dismiss", whose "Try again in 60 seconds." stays as it was written, and stays after the wait is over. "A notice that counts that wait down is the next step." This is that step. The API is unchanged.

**Where the wait is held:**

1. **For the session, in every conversation, as a usage limit is.**
    - Con: the wait is one request's, by the size of its prompt and the model asked. Another conversation's shorter question would be told to wait, though it may be taken.
2. **In the conversation on the page, with the question it is about.** Chosen.

**Whether sending is blocked meanwhile:**

1. **Blocked, as at a usage limit.**
    - Con: the wait is one request's, at the moment it was refused, and a shorter question may fit sooner ([ADR 0030](0030-the-providers-budgets-are-counted.md), revision of 2026-11-26, "Known, and left"). Blocked, it couldn't be tried.
2. **Open.** Chosen.

**Whether the page sends the question again by itself when the wait is over:**

1. **Yes.**
    - Con: a wait can be hours, and the box may hold other words by then. Another request may have taken what came back, so that the question is refused again, and each refused try counts against "too many messages".
2. **Only after a short wait.**
    - Con: a second rule for what is short, and a question held by the page as if it were being answered. A wait of a second or two is better waited out on the server, where a model that asks to wait up to 2 s is already waited for (`WAITS_AT_MOST_S`; [ADR 0028](0028-a-model-names-those-that-stand-in-for-it.md), revision "a model is asked once more, and a server says how long it may take to begin").
3. **Never.** Chosen.

**The time of day isn't said** beside the time left, as it is for a usage limit: that limit starts over at a moment the server names, and this wait is an estimate for one request, mostly seconds or minutes.

**Decision: a question refused for now is said in the page's own words, with the time left counting down, in the conversation it was asked in and for as long as it is that conversation's problem, until the wait is over; sending stays open meanwhile, and the page never sends the question by itself.**

**Which refusal it is** (`refusedForNow` in `lib/chat/outcomes.ts`, pure): a 429 whose problem names no `window`, names a `model`, and says how long to wait, `retry_after`. Its wait is over that many seconds after the moment the refusal arrived, by this browser's clock, as a usage limit's is counted: the length of a wait is the same on every clock. Anything else is what it was: a usage limit names a window; too many messages names no model; too many answers at once says no wait; a 503 names a model, and is no 429; and an error that is no refusal.

**Where it is held, and what ends it** (`components/chat/chat.tsx`):

- in the state of the conversation on the page, beside the problem its notice shows, with the refusal it is of, and said only for as long as that refusal is the problem: whatever takes the problem's place, or clears it, takes the wait's too. The page notes the moment when its handler of errors puts the refusal on the page, after asking whether it is a usage limit, as before;
- **sending isn't blocked:** "Send", and "Try again" under the conversation, are as they were;
- **it ends, and its notice goes,** when the wait is over, which the page notices within a second, by looking at the clock and not by counting: after a computer slept, the next look is right; when the next question is sent, or another try is asked for; at "Dismiss"; when another error takes its place, a second refusal for now included, whose wait replaces the older one, counted from its own arrival: one notice, never two; and when its conversation leaves the page. Coming back to it, nothing of the wait is there;
- **the end of a wait takes away only its own notice:** one that ends after another problem took its place, or after the notice was dismissed, changes nothing;
- **the page never sends by itself:** when the wait is over, the question waits in the box for its writer;
- **nothing is read again at its end:** the usage counts aren't this refusal's.

**What it says** (`components/chat/refused-for-now.tsx`), where the problem's notice stands, above the box, and instead of it: the API's `detail` isn't shown, since its "in 60 seconds" would stand still beside a time that counts down. A warning, with "No model can take this message right now." first, then "This installation has reached the limits of its models’ providers. Try again in 42 seconds.", the time left in `timeLeft`'s words, read again every second, and "Dismiss". The same words whether the refused question is back in the box or, after "Try again", still stands in the conversation: "this message" is true of both. The clock is read in that component, not in the conversation's, which maps every message and would be drawn again every second with it. What is said to people names no provider and no limit: the log says those.

**The keyboard:** "Dismiss" puts it on the box, where the question waits. When the notice goes by itself while the keyboard is on its "Dismiss", it goes to the box too: the button is gone, and the focus would be nowhere. Anywhere else, the keyboard stays where it is.

**What a screen reader is told,** as at a usage limit: the first sentence is a status, said once, and the time left is a timer beside it, never inside it, read when asked for and not at every second. The notice marks the part to be said itself (`changing`).

**How it is tested:**

- **the component tests** (`components/chat/refused-for-now.test.tsx`), against the fake API with the recorded refusal (`no_budget.json`) and the clock set: the page's words as a status, and none of the API's; the timer, with no status around it; the wait counted down, and the notice gone when it is over, with the question still in the box, nothing sent, and the question answered once it is sent; sending open meanwhile, and the notice gone from the moment a question is sent; a second refusal's wait in the place of the first's, shorter and longer; the end of a wait that a newer one replaced, which changes nothing; the wait counted from the refusal's arrival, after minutes between the page being opened and the question being asked and half a minute between the question being sent and the refusal arriving; a day's wait and more, in hours and days; a wait of one second, gone at the next look; "Dismiss", and where the keyboard goes then and at the wait's end, from "Dismiss" and from elsewhere; a new chat, which knows nothing of the wait; another try that is refused, with "Try again" still offered; React's StrictMode; and the other 429s and a 503 with a model and a wait, each in the API's words, with "Dismiss" and no timer, and a usage limit that names a model too, still a usage limit. The rule alone in `lib/chat/outcomes.test.ts`, and `reachedBy`, which takes the refusal for no usage limit, in `lib/usage.test.ts`;
- **the end-to-end stack offers the fake once more,** as "rationed", with a budget of one request a minute, of which Actio keeps the one, on a provider of its own (`deploy/e2e/config/models.toml`): on the fake's, it would share one budget with the two other names of the same model there, and the three would have to state the same figures. `e2e/no-budget.spec.ts` gives each of its questions that model's name on its way out. The count is kept in the stack's Valkey for a minute, between tests too: a fresh stack has the request, and a stack kept running, or a run that repeats the test, may find it taken. So the test asks until a question is refused with at least ten seconds of its wait left, enough to look at: a question that is answered has just taken the minute's request, and the next, asked right after, is refused for about a minute; one refused for a few seconds more, the end of a minute that an earlier run began, is waited out, until its notice goes by itself, and the next is asked. That is three questions at the most. Of the refusal it then holds the 429 itself, its `Retry-After`, its `retry_after` and its `model`; the page's words and timer; the question back in the box, with "Send" enabled and as many questions as answers on the page; a time left that goes down; and "Dismiss", with the keyboard on the box.

**Known, and left** (each worked out or read in the code, not seen, unless it says otherwise):

- **the page keeps the wait for the conversation on the page alone:** after a reload, or after leaving the conversation and coming back, it knows nothing of it, and the box is empty. A question asked before the wait is over is refused again, with what is left of it;
- **a wait of a second shows a notice for a second:** nothing waits on the server, and the page doesn't send again;
- **a wait of hours says no time of day;**
- **when the wait is over, another request may have taken what came back:** the question is then refused again, with a new wait, and each refused try counts against "too many messages" ([ADR 0030](0030-the-providers-budgets-are-counted.md));
- **"too many messages" names a wait too, and still says the API's sentence once,** which stays after its wait is over: the same notice with another first sentence would count it down;
- **at the instant the notice appears, the refused question is still shown as a message,** and is back in the box a moment later (seen in the last step's look, 54 ms later; every refusal's);
- **on the end-to-end stack the count of "rationed" is kept between tests for a minute,** so its spec can't say which of its questions is refused, and on a stack that was kept running it may take a few seconds longer, waiting out the end of an earlier run's minute.

**How to reverse it:** `refusedForNow` in `lib/chat/outcomes.ts`, `components/chat/refused-for-now.tsx` and the wait in `components/chat/chat.tsx`, with their tests: the refusal is the API's sentence with "Dismiss" again, as the revision above says. On the end-to-end stack, "rationed", its provider and its price in `deploy/e2e/config/`, its spec, and what `test_catalog.py` holds of it. The API is unchanged.

**Measured,** on the real stack, built from the tree as it was committed, on 2026-11-26 from 08:31 UTC, with a models file made for the look: the shipped local model, `qwen3.5:4b-q4_K_M` on Ollama, alone in its chain, stating `requests_a_minute = 2`, of which 1 is kept, and no model that names conversations. No request went to a cloud provider. The page at http://localhost:8080, as a guest, in Chromium, Firefox and WebKit, one after the other, each once, and each a minute after the one before, so that the minute's request was back.

In each engine, in this order:

1. a first question was answered;
2. a second, in the same conversation, was refused, with `Retry-After` 57 in Chromium and 60 in the two others. The page said "No model can take this message right now.", a `role="status"`, then "This installation has reached the limits of its models’ providers. Try again in 57 seconds." in Chromium and "in 1 minute" in the two others, the time left a `role="timer"` with no status around it, and "Dismiss". The API's own sentence wasn't on the page. The question was back in the box at most 36 ms after the notice was first seen, by a look every 25 ms; "Send" was enabled, and the conversation showed its one question and its one answer. Read four times, a second apart, the timer said 57, 56, 55 and 54 seconds in Chromium, and "1 minute", then 59, 58 and 57 seconds in the two others;
3. Enter again, four seconds after the refusal: refused again, with `Retry-After` 52 in Chromium and 55 in the two others, and one notice, which said those seconds;
4. the keyboard was put on "Dismiss" with real keys, Shift+Tab from the box, and in WebKit Option+Shift+Tab, Shift+Tab alone not reaching the button there; then the wait was waited out. The notice was seen gone 52.47 s after that refusal arrived in Chromium, 55.06 s in Firefox and 55.49 s in WebKit, and the keyboard was on the box;
5. Enter then, 0.13 to 0.17 s later: the question, still in the box, was answered;
6. a third question at once: refused, for 60 s. "Dismiss", with the mouse, took the notice away, with the question in the box and the keyboard on the box;
7. "New chat", and a first question there: refused, for 59 s, with the notice, no message shown, and the address still `/`;
8. the earlier conversation, opened from the list: no notice. "New chat" again: none, and an empty box.

No script of the page failed in any of the three. The API's log had twelve lines of a refusal, four for each engine, as "conversation …: no model has budget left for the request, which is refused for about 57 s: standard for about 57 s (requests a minute)".

**How the notice looks** was read from pictures of step 2, taken in each engine light, dark, with forced colours and in a window 390 px wide. Five of them were looked at: Chromium light, with forced colours, where the notice is a line around its words, and 390 px wide, where "Dismiss" stands under the words; Firefox dark; and WebKit with forced colours.

**The end-to-end tests,** on their own stack: 80 passed, one of them new, which took 2.2 s. On that stack, kept running, the new test was then run 40 times, one after the other, over 1.7 minutes: 40 passed, 38 of them in 1.6 to 1.8 s, and two in 10.2 and 11.2 s, the time of a refusal with a few seconds left that is waited out. And 45 times, three at a time: 45 passed.

**Mistakes planted on purpose.** After the build, 31 mistakes were planted one at a time in a scratch copy of the tree, 26 in the page and 5 in the end-to-end stack's two files, and the tests that should notice each were run. 29 failed a test. Two failed none:

- **a `key` on the notice,** by which each refusal's notice was to be its own. A first refusal's notice is always gone before a second can arrive, since a question that is sent takes it away: the key did nothing, and it came out;
- **the end of a wait clearing whatever problem is there,** and not only its own refusal. The two differ only when another error arrives between the frame in which the wait ended and the effect that says so, which no test reaches. The check stays.

The review then named three broken versions that, by its reading, the tests would pass: the notice back after an answer that comes before the wait's end; a second refusal's wait counted from the first one's arrival; and the first wait's end, looked for on a beat of its own, taking a newer refusal's notice away. The first was planted against the tests as they were: the test meant for it passed, since it moved the clock past the wait's end before it looked, and one other test failed, by that version's own doing, which asks the clock whenever the conversation is drawn. Three tests were corrected: that one now looks with the clock unmoved, the tests of a second refusal move the clock between the two, and the test of a stale end looks a second time, a second later. Planted against the corrected tests, each of the three failed the test meant for it.

On the tree as it was committed, all 33 were planted once more, 28 in the page and 5 in the two files: 32 failed a test, and the one that failed none is the check above.

Five of them were then planted where only the whole stack shows them, each on an end-to-end stack built from the copy, with the new test run against it: the refusal not told apart, the time left standing still, "Dismiss" leaving the keyboard where it was, sending blocked during the wait, and "rationed" with no budget. The test passed on the unchanged copy, and failed on each of the five.

**Not measured:** a refusal on a cloud provider's figures; a wait of hours or a day, which the component tests hold by the test's clock; what a screen reader says of the notice; and the page in a tab in the background, where a browser may slow its timers.

### 2026-11-26: the wait counted down may be a provider's own

A refusal for now is also a provider's own wait now: a model whose provider refused a request with 429 and named a wait longer than 2 s has no budget left until that wait is over, and when no model with room for a question has budget, the question is refused with the same 429, its `retry_after` what is left of that wait ([ADR 0030](0030-the-providers-budgets-are-counted.md), revision "a provider's own wait is kept, and until it is over its model has no budget"). The page counts it down as it counts any refusal for now. Its source didn't change.
