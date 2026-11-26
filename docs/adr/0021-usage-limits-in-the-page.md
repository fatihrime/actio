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
