# 0011. Rate limits on the open routes, as token buckets in Valkey

- **Status:** accepted
- **Date:** 2026-11-11

## Context

Three routes can be called by anyone, without a session. Each costs something:

- **Creating a guest** writes rows, and later gives each guest a usage allowance of its own.
- **Signing up** hashes a password, which takes 60 ms and 64 MiB ([ADR 0010](0010-accounts-with-a-password.md)). It also says whether an email address is already registered.
- **Logging in** hashes a password too, and is where passwords get guessed.

A fourth route, changing the password, needs a session but asks for the current password. Whoever holds a stolen session could use it to guess that password.

- Limits must hold across several API processes.
- Actio is self-hosted: there may be no CDN or firewall in front of it that does this.
- Valkey is already part of the stack, for the usage limits ([ADR 0006](0006-usage-limits-engine.md)).
- The developer gateway will need the same kind of limit later, per API key.

Separately, a guest expires after a day ([ADR 0009](0009-sessions-in-the-database.md)), but nothing deleted its rows.

## Options considered

**Where to count:**

1. **In the reverse proxy.**
   - Pro: a refused request never reaches the API.
   - Con: Caddy ships without rate limiting. The pinned image, 2.11.9, has 136 modules and none for it (checked 2026-11-11). Adding one means building and maintaining a custom Caddy image.
   - Con: the proxy can't count by the email address typed into a login.
2. **In each API process's memory.**
   - Pro: nothing else to run.
   - Con: each process counts alone, and a restart forgets everything.
3. **In Postgres.**
   - Pro: already there, and durable.
   - Con: a write to the main database for every attempt, heaviest exactly when someone is hammering a route. Old rows need deleting.
4. **In Valkey, with a Lua script.**
   - Pro: atomic across every process, and keys expire by themselves.
   - Pro: the same pattern as the usage limits, so one thing to understand.
   - Con: these routes now need Valkey to be up.
5. **A rate-limiting library for the web framework.**
   - Pro: ready-made.
   - Con: such libraries count per request: by address and path. A login must also be counted by a value from its body, together with the address, as one decision.

**How to count:**

1. **A counter per fixed window,** such as per clock hour.
   - Pro: two commands.
   - Con: twice the limit fits around a window's edge: the end of one window and the start of the next.
2. **A log of every attempt's time,** counted over the last hour.
   - Pro: exact.
   - Con: stores one entry per attempt.
3. **A token bucket.** It holds up to N tokens and gets one back at a steady pace. Each attempt takes one.
   - Pro: one number per bucket, whatever the limit.
   - Pro: allows a burst, then a steady pace, and knows exactly how long until the next token.

**What to count for logins:**

1. **Per client address only.** Guessing one account's password from many addresses is never limited.
2. **Per email address only.** Trying one leaked password on many accounts is never limited.
3. **Both.**

**When Valkey doesn't answer:**

1. **Let the request through.** Then an outage is the moment every limit is off.
2. **Refuse it.** Then an outage stops new logins.

## Decision

**Token buckets in Valkey, charged by one Lua script.** Logins are counted per address and per email address. Without Valkey, the limited routes answer 503.

**The limiter** (`actio.core.rate_limit`):

- **A limit is written as `30/hour`:** a bucket of 30 tokens that gets one back every two minutes. So 30 at once are fine, and so are 30 an hour for as long as you like.
- **A bucket is stored as one number:** the moment it will be full again. Taking a token moves that moment one interval later. The key expires at that moment, so a full bucket takes no memory at all. This way of storing a token bucket is known as the generic cell rate algorithm (GCRA).
- **Several limits are charged in one call, all or nothing.** A login takes a token from its address's bucket and from its email's bucket, or from neither. A refused attempt costs nothing.
- **Time is an argument,** as in the usage limits: nothing here reads a clock, and tests move time by hand.
  - A stored moment further away than an empty bucket's can't come from this limit and this clock. The script treats that bucket as empty and corrects it. So a limit that is loosened takes effect at once, and one process with a wrong clock can't close a bucket for long.
- **It is tested against the plain definition.** A second implementation, written the way token buckets are usually described, plays the same random sequences of attempts. The script must give the same answer at every step.
- **Keys don't hold what people typed.** A key names the limit and a hash of the subject: `actio:rl:logins-email:487a9d7b…`. Valkey never holds email addresses or network addresses.

**The limits** (`actio.api.rate_limits`), each a setting:

| Setting | Default | Counts |
|---|---|---|
| `ACTIO_LIMIT_GUESTS_PER_ADDRESS` | `30/hour` | New guests from one address |
| `ACTIO_LIMIT_SIGNUPS_PER_ADDRESS` | `10/hour` | Sign-up attempts from one address |
| `ACTIO_LIMIT_LOGINS_PER_ADDRESS` | `30/hour` | Login attempts from one address, for any account |
| `ACTIO_LIMIT_LOGINS_PER_EMAIL` | `10/hour` | Password attempts for one email address, from anywhere: logins, and changes of the password |

- **Every attempt counts, right or wrong, and is counted before the work.** Counting only failures would need a second step after the password check, and attempts arriving together could all pass before any was counted.
  - Measured through the proxy on 2026-11-11: a checked login takes 61 ms; a refused one takes 3 ms and hashes nothing.
  - A request that is refused for its form, such as a password that is too short, isn't counted: it cost nothing.
- **An email address counts whether or not an account has it.** So a 429 doesn't tell who has an account.
- **An address is an IPv4 address, or an IPv6 /64 network.** One home or one machine is usually given a whole /64, and could otherwise use a new address for every attempt.
- **A refusal is a 429** in the usual error format, with a `Retry-After` header and the same number of seconds as `retry_after` in the body.

**The client's address** comes from `X-Forwarded-For`, which the reverse proxy sets.

- Caddy writes that header itself and discards what the client sent. Checked through the stack on 2026-11-11: requests with a forged `X-Forwarded-For`, `Forwarded` and `X-Real-IP` were still counted under the address Caddy saw.
- This depends on three things together: the API is reachable only through Caddy, Caddy has no `trusted_proxies`, and the API trusts the header from anyone who can reach it. A test in `deploy/tests/` fails if one of them changes alone.

**Expired guests** are deleted by the next guest that is created:

- after the response, in batches of 100, oldest first. Rows that another transaction holds are skipped, so sweeps never wait for each other;
- expired sessions go the same way;
- migration `0004` adds the two indexes this needs.

## Consequences

- **Easier:**
  - a new limit is one line in a route, and one setting;
  - the gateway's limits per API key can use the same limiter;
  - a public demo can lower the guest limit to `3/day` without a code change.
- **Harder:**
  - **logging in needs Valkey.** Existing sessions don't: they live in Postgres, so people who are logged in stay logged in through an outage;
  - **people behind one router share an allowance.** On Docker Desktop it is wider still: every browser on the machine reaches Caddy from one address, so the per-address limits are limits for the whole machine. `make smoke` creates one guest per run;
  - **an account's owner can be kept out by someone else.** Whoever keeps spending the attempts for an email address keeps its owner from logging in, for as long as they keep at it. The owner's existing sessions keep working, and the block ends minutes after the attempts stop. Counting per address only would avoid this, and would leave guessing from many addresses unlimited;
  - **behind another proxy or a CDN,** the address Caddy sees is that proxy's. Then Caddy needs `trusted_proxies`, and `ACTIO_FORWARDED_ALLOW_IPS` must name the proxies instead of `*`, in one change. Otherwise every client is one address, or the address is the client's to choose;
  - a Valkey that loses its data refills every bucket. That is acceptable for limits measured in minutes.
- **Not done yet:**
  - **a scheduled sweep.** On an installation where no new guest arrives, expired guests stay until one does. A periodic job comes with the worker process;
  - a challenge for guests on a public demo, and a limit on requests in general: the routes that cost little are not limited;
  - limits per API key, which come with the gateway.
- **How to reverse it:** routes ask for a limit by name, such as `await limits.log_in(email)`. Counting somewhere else changes `actio.core.rate_limit`, and nothing above it.
