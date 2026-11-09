# actio-quota

Actio's usage-limits engine, inspired by the session and weekly limits of Claude's plans. This package holds the parts that need no server:

- **credit math** (`cost`);
- **calendar reset windows** (`windows`), such as "every Monday at 00:00 in Europe/Istanbul";
- **the engine's inputs and results** (`model`);
- **a reference model of the engine** (`oracle`): a plain-Python version that is the specification.

The production engine runs the same operations as Lua scripts inside Valkey, so each one is atomic across all API and worker processes. Those scripts arrive in the next step; differential tests will then run identical random event sequences against both and require identical results. The design and its trade-offs are in [ADR 0006](https://fatihrime.github.io/actio/adr/0006-usage-limits-engine/).

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

## Tests

```sh
uv run pytest packages/quota                              # 100 random sequences per property
HYPOTHESIS_PROFILE=nightly uv run pytest packages/quota   # 5,000
```

- Example tests pin down each rule.
- Property tests check the credit math and that reset windows tile time exactly, across DST changes in six time zones.
- A state machine fires random reserves, settles, releases, retries, crashes, clock jumps and data loss at the engine. After every step it checks that admission is exact, `reserved` matches the live reservations, a denial writes nothing, retries change nothing, and counters never exceed the ledger.
- `make test` fails if this package's test coverage drops below 90%.
