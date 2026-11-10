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
