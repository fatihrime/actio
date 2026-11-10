# actio-quota

Actio's usage-limits engine, inspired by the session and weekly limits of Claude's plans.

- **The engine** (`engine.ValkeyQuota`) runs each operation as one Lua script inside Valkey (`lua/`), so it is atomic across every API and worker process.
- **A reference model** (`oracle.QuotaOracle`) implements the same operations in plain Python. It is the specification: differential tests run identical random event sequences against both engines and require identical results and identical stored state at every step.
- **Credit math** (`cost`), **calendar reset windows** (`windows`), such as "every Monday at 00:00 in Europe/Istanbul", and **the engines' inputs and results** (`model`).

The design and its trade-offs are in [ADR 0006](https://fatihrime.github.io/actio/adr/0006-usage-limits-engine/).

## The rules

**Credits.** Every model has integer prices in credits per million tokens. A request's cost is its exact cost, rounded up once:

```python
from actio_quota import Rates, Usage, cost_credits

standard = Rates(input=1_000, output=4_000, cached_input=250)
cost_credits(Usage(input_tokens=12_000, output_tokens=800), standard)  # 15.2, rounded up: 16
```

**Buckets.** A user's plan gives them buckets, and a request must fit in all of them:
- `session`: a rolling window. It opens at the first *admitted* request and resets 5 hours later.
- `week`: a calendar window that resets at a fixed local time, worked out with `CalendarReset`.
- Others follow the same two shapes, such as a premium-model bucket or a daily search bucket.

**Admission is strict.** A request is admitted only if `used + reserved + cost ≤ limit` holds in every bucket.
- An admitted request *reserves* its credits, so requests running at the same time can't overspend together.
- A denied request changes nothing. It doesn't even open a session window, and it says when the blocking bucket resets.
- A request that costs 0 credits, such as one to a free local model, is always admitted.

**Answers shrink before they're refused.** An `Estimate` has a fixed part (the prompt) and a per-unit part (output tokens). The engine grants the most output tokens that fit, down to a minimum.
- A step that may call tools sets that minimum to 512 tokens. Below it, the step is denied rather than shrunk, because a tool call cut off mid-JSON would fail anyway.

**Finishing.** When the request ends, it is *settled* with what it really cost, or *released* if it was cancelled before using anything.
- Usage counts toward the window that admitted the request. If that window has reset meanwhile, the new window isn't charged.
- Retrying a reserve, settle or release is safe: the engine remembers finished requests for 48 hours.
- Each bucket warns once per window, at 50%, 75% and 90%.

**Crashes heal themselves.** A running request keeps extending its reservation. If its process dies, the reservation expires, and the next reserve for that user releases it.

**The ledger is the truth.** Settled usage is first written to an append-only ledger in Postgres. If Valkey loses its data, each user's counters are rebuilt from the ledger the next time they're needed.

## Example

The reference model is synchronous, which keeps examples short. `ValkeyQuota` has the same methods as coroutines: `await engine.reserve(...)`.

```python
from actio_quota import Bucket, CalendarReset, Estimate, Fixed, QuotaOracle, Rates, Rolling

HOUR = 3_600_000
now = 1_800_000_000_000
week = Fixed(*CalendarReset.parse("MON 00:00 Europe/Istanbul").window(now))
buckets = [Bucket("session", 100, Rolling(5 * HOUR)), Bucket("week", 600, week)]

engine = QuotaOracle()
engine.rebuild("user-1", buckets, ledger=[], now_ms=now, if_epoch=None)  # a new user

estimate = Estimate.tokens(Rates(1_000, 4_000), input_tokens=20_000, max_output=50_000)
grant = engine.reserve("user-1", "turn-1", buckets, estimate, ttl_ms=600_000, now_ms=now)
# Granted(credits=100, units=20_000, ...): 20 credits of prompt leave room for 20,000 tokens.
engine.settle("user-1", "turn-1", 64, now_ms=now + 30_000)
```

## Storage

Each user has six keys, all under the hash tag `{u:<id>}`, so a cluster keeps them on one node:

| Key | Type | Holds |
|---|---|---|
| `q:{u:<id>}:meta` | hash | `epoch`. Missing means the counters must be rebuilt from the ledger |
| `q:{u:<id>}:b` | hash | Per bucket: `<name>/start`, `/reset`, `/limit`, `/used`, `/reserved`, `/warned` |
| `q:{u:<id>}:rsv` | hash | Running reservations: id → grant |
| `q:{u:<id>}:rsv_exp` | sorted set | Running reservations by expiry time |
| `q:{u:<id>}:done` | hash | Finished reservations: id → outcome and grant |
| `q:{u:<id>}:done_exp` | sorted set | Finished reservations by when they're forgotten |

Times come from the `now_ms` arguments, never from Valkey's clock, so tests can replay any sequence of events. Every write also sets all six keys to expire after 8 idle days; a returning user is rebuilt from the ledger.

## Tests

```sh
uv run pytest packages/quota                              # 100 random sequences per property
HYPOTHESIS_PROFILE=nightly uv run pytest packages/quota   # 5,000, as the nightly workflow runs
```

The Valkey tests start a throwaway container from the image pinned in `compose.yaml`, so Docker must be running. To use a server you already run, set `ACTIO_TEST_VALKEY_URL`. Without Docker, `uv run pytest` skips those tests; `make test`, and so CI, sets `ACTIO_REQUIRE_DOCKER=1`, which turns the skip into a failure that names the reason.

- **Rule tests** pin down each rule with one example, and run against both engines.
- **Property tests** check the credit math, and that reset windows tile time exactly across DST changes in six time zones.
- **A state machine** fires random reserves, settles, releases, retries, crashes, clock jumps and data loss. After every step it checks that admission is exact, `reserved` matches the running reservations, a denial writes nothing, retries change nothing, and counters never exceed the ledger.
- **The differential test** runs that state machine with every call sent to both engines, and compares results and complete stored state after every step. Planted bugs in the Lua (an off-by-one in admission, a missed rollover check, a smaller cleanup batch) each fail it or the rule tests.
- **Concurrency:** 256 simultaneous requests over 8 connections admit exactly ⌊limit / cost⌋. With 30% of them crashing, every held credit still comes back.
- `make test` fails if this package's coverage drops below 90%.
