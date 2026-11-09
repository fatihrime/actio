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
- **A reference model is the specification.** `actio_quota.oracle` implements every operation in plain Python. Random-sequence tests check its invariants; differential tests will run the same sequences against the Lua scripts and require identical results and identical stored state.

## Consequences

- Limits hold exactly under concurrency, except for one measured error: the prompt-length estimate. A request can use more than it reserved if its prompt was longer than estimated; the excess is recorded, not hidden, and its size will be measured and published.
- The Lua must mirror the Python step for step, including details such as which expired reservations are collected first. The differential tests make any divergence fail loudly.
- Two stores can drift. A nightly job will compare them and export the drift as a metric.
- **How to reverse it:** the engine sits behind a small interface (reserve, settle, release, extend, rebuild, snapshot). A Postgres-only implementation could replace the Lua scripts and run against the same differential tests.
