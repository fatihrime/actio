# 0006. Usage limits: reservations in Valkey, a ledger in Postgres, a Python reference model

- **Status:** accepted
- **Date:** 2026-11-09

## Context

Actio limits how much each user can use, in the spirit of Claude's plans: a 5-hour session window that opens with the first message, plus a weekly limit. The same limits govern the chat app, the chat bots and the developer gateway.

- **Many processes at once:** API and worker processes admit requests in parallel, and a coding agent fires several calls at the same moment. Two requests must never both squeeze into the last credits.
- **Unknown cost up front:** a model call's price depends on output tokens that don't exist yet.
- **Failure:** processes crash mid-request, and the in-memory store can lose its data.
- **Proof:** correct limits are the project's headline claim, so they must be testable, not just plausible.

## Options considered

1. **Count in Postgres,** locking a row per user for each check.
    - Pro: one store, transactional.
    - Con: every request takes a row lock on the hot path, and a crashed request's held credits need a cleanup job.
2. **A counter in Valkey, charged after the fact** (`INCRBY` when a request ends).
    - Pro: simple and fast.
    - Con: parallel requests all pass the check before any of them is charged, so limits overshoot. That's the race this design exists to stop.
3. **Reservations in Valkey Lua scripts, with a ledger in Postgres.**
    - Pro: each script runs atomically, so admission is exact under any concurrency, in well under a millisecond.
    - Con: Lua is harder to read and test than Python, and two stores must agree.
4. **A rate-limiting library,** such as `limits`.
    - Pro: no code of our own to maintain.
    - Con: rate limiters count requests per interval. They don't hold credits for requests in flight, open a window on first use, shrink an answer to fit, or settle the real cost afterwards.

## Decision

We chose option 3, with a plain-Python reference model as its specification.

- **Reserve, then settle.**
    - A request reserves its worst-case credits in every bucket that applies, and is admitted only if `used + reserved + cost ≤ limit` holds in all of them.
    - When it ends, it settles its real cost or releases the reservation.
    - Running requests extend their reservation; a crashed one expires, and the next reserve releases it.
- **Shrink, don't refuse.** The engine grants the most output tokens that fit, so answers near a limit get shorter. Tool-calling steps need at least 512 output tokens, or they're denied: a truncated tool call is worse than a clear "limit reached".
- **Free requests always pass,** so a user blocked on paid models can keep chatting with a local model.
- **Integers only.** Credits and prices are integers; costs round up once per request. Lua numbers are 64-bit floats, so every value is kept below 2^53, where they are exact.
- **Time is an argument.** The engine never reads a clock. Python passes `now_ms`, and works out calendar windows such as "Monday 00:00 Europe/Istanbul" with `zoneinfo`, because Lua has no time zone data. Tests can then replay any sequence of events exactly, including jumps of hours or weeks.
- **The ledger is the truth.** A request's final message and its ledger row commit in one Postgres transaction before Valkey is settled. If Valkey loses a user's counters, the next request finds no epoch and rebuilds them from the ledger. Requests in flight at that moment go uncounted: in the user's favour, and still recorded in the ledger.
- **A reference model is the specification.** `actio_quota.oracle` implements every operation in plain Python. Random-sequence tests check its invariants, and a differential test runs the same sequences against the Lua scripts, requiring identical results and identical stored state after every step.

## Consequences

- Limits hold exactly under concurrency, except for one measured error: the prompt-length estimate. A request can use more than it reserved if its prompt was longer than estimated; the excess is recorded, not hidden, and its size will be measured and published.
- The Lua must mirror the Python step for step, including details such as which expired reservations are collected first. The differential tests make any divergence fail loudly.
- Tests of the Lua need a real Valkey server. They start a throwaway container from the image pinned in `compose.yaml`, so they test what production runs.
- Two stores can drift. A nightly job will compare them and export the drift as a metric.
- **How to reverse it:** the engine sits behind a small interface (reserve, settle, release, extend, rebuild, snapshot). A Postgres-only implementation could replace the Lua scripts and run against the same differential tests.

## Revisions

### 2026-11-10: three gaps found by walking the callers' flows through the engine

The first version was specified one operation at a time: what reserve does, what settle does. Before building the ledger and the chat pipeline on top, we walked their planned flows through the engine, step by step, asking who calls each operation and when. Three cases behaved wrongly. All three involve two actors, or two clocks, which is why specifying single operations hadn't shown them.

- **A settle after expiry is now charged.**
    - Before: once an expired reservation's credits had been given back, settling it changed nothing.
    - The flow that exposed it: a worker dies mid-answer, its reservation expires, and the user's next message releases it. A cleanup job then writes the partial answer to the ledger and settles it. The ledger had the usage, and the counters ignored it.
    - Now: expiry gives back the held credits but doesn't end the request. The first settle or release still decides how it ends, and a settle charges the windows that admitted it. This follows the rule above: the ledger is the truth, so every ledger row must reach the counters.
- **A calendar window never moves backward.**
    - Before: a request that carried an older window than the stored one reopened the old window, wiping the new one's counters.
    - The flow that exposed it: time is an argument, so two callers can disagree about it. Around the weekly reset, a request that read its clock just before midnight can reach Valkey after one that read it just after.
    - Now: the stored window stays, and the late request is admitted into it.
- **A denial that releases expired reservations refreshes the idle expiry.** Only successful writes did. The markers such a denial wrote could outlive the user's other keys, which are meant to vanish together. This is in the Lua only: the reference model has no key expiry.

**Why change it now:** nothing reads or writes these counters in a running system yet, so the fix is three small changes with no stored data to reconcile. Once the ledger exists, changing what "settled" means would also mean correcting rows already written.

**The tests got stricter with it.** The random state machine used to allow the counters to undercount the ledger. Now they must equal it, minus only the settles the engine itself reports as lost: after Valkey lost its data, or more than 48 hours late.

### 2026-11-14: the engine is in use

Turns hold and settle their credits through the engine since [ADR 0020](0020-usage-limits-on-turns.md). Nothing in the engine changed for it. That record settles what this one left open:

- **The ledger exists,** and is written before the counters are told, as decided above.
- **The one error has been measured.** The decision above says a request can use more than it reserved when its prompt was longer than estimated, and that the size of that error would be measured and published. The estimate, and how far it was off on two models, is in ADR 0020.
- **Valkey keeps nothing on disk.** "If Valkey loses a user's counters" is then every restart. A Valkey that restored an old file would bring back counters that are there and stale, which the engine has no way to notice.
- **The time the engine is told is the application's own,** the moment a request arrived. Taking it from Valkey instead was considered and left: see ADR 0020.

Still to come, as above: the nightly comparison of the two stores, and a job that settles what a process that died left held.

### 2026-11-19: no job settles what a dead process left held

"A job that settles what a process that died left held", still to come in the revision above, turned out not to be needed. The hold on a turn's credits lasts a minute, and is renewed while the turn is written ([ADR 0020](0020-usage-limits-on-turns.md), revision of 2026-11-16). So it runs out by itself a minute after its process died. The worker gives up on the turn itself, and gives nothing back ([ADR 0022](0022-turns-that-run-apart-from-their-request.md), revision "the worker gives up on turns nobody looks at").

Still to come: the nightly comparison of the two stores, and repeating a count that Valkey missed.

### 2026-11-20: a count that Valkey missed is repeated

"Repeating a count that Valkey missed", still to come in the revision above, is done ([ADR 0020](0020-usage-limits-on-turns.md), revision "the counters catch up"). Each row of the ledger is written with a note that the counters haven't been told of it yet, which goes once they have. What is left, the worker settles again every ten minutes.

- **Nothing in the engine changed for it.** A settle that is repeated is answered `settled`, not applied, so it never counts twice. One whose hold ran out is still charged, as the revision of 2026-11-10 decided: that is what lets a repeat count what was missed.
- **`collect_expired` is in use:** reading an account's usage first gives back the holds that ran out, so that they aren't shown as held.

Still to come: the nightly comparison of the two stores.

### 2026-11-22: the two stores are compared

"The nightly comparison of the two stores", still to come in the revision above, is done, every hour rather than every night ([ADR 0020](0020-usage-limits-on-turns.md), revision "the counters are compared with the ledger"). "Two stores can drift. A nightly job will compare them and export the drift as a metric", under "Consequences": the worker compares them, and says in its log how far apart they are, in each window, short and over.

- **The engine gained two reads, and no Lua changed.** `users` gives the ids of the users that have counters, by a SCAN of Valkey for their `meta` keys, a page at a time, so that a caller can stop where it likes. `buckets` gives a user's stored windows as they are, without a plan, as `snapshot` reads them with one. Neither writes, nor pushes back when a user's keys expire. The reference model has both, and the rule tests run them on both engines.
- **"How to reverse it"** names the engine's interface: the two reads are part of it now.

Still to come: exporting that as a metric, once there is a route to export it from.
