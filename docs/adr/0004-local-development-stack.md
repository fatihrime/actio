# 0004. A hybrid local stack: services in Docker, the apps native, Caddy in front

- **Status:** accepted
- **Date:** 2026-11-09

## Context

Development needs Postgres, Valkey and a reverse proxy alongside the API and the web app.

- Feedback must be fast: a code change should reload in about a second.
- Everything must fit on a 16 GB laptop next to an editor, a browser and, later, a local model.
- Production will run every process in a container behind Caddy. The browser must see one origin there: the web app and the API share cookies, and chat replies stream through the proxy.

## Options considered

1. **Everything in Docker,** with the code mounted into the containers.
    - Pro: the closest to production.
    - Con: on macOS, file syncing and watching through Docker Desktop are slower, and Node.js and Python containers add memory on top of the Docker VM.
2. **Everything native,** with Postgres, Valkey and Caddy installed through Homebrew.
    - Pro: the lightest option.
    - Con: versions drift between machines, and every contributor installs and manages three servers.
3. **Hybrid:** the data services and Caddy in Docker, from pinned images; the API and the web app as native processes with hot reload.

## Decision

We chose option 3. `make dev` starts the containers, then runs the API and the web app side by side.

- **Caddy is the single entry point, even in development:** `http://localhost:8080`.
    - `/api/*` and `/v1/*` (the future OpenAI-compatible gateway) go to the API; everything else goes to the web app.
    - Caddy reaches the native processes through `host.docker.internal`, which also reaches services bound to the host's 127.0.0.1. So the API and the web app still listen on 127.0.0.1 only.
    - Routing, streaming and WebSockets therefore behave in development as they will in production.
- **Postgres 17 with pgvector 0.8.7,** not 18. We checked on 2026-11-09: Supabase, where the planned public demo would keep its database, still runs Postgres 17, and Postgres 19 is only a release candidate. The app generates its own UUIDv7 ids, so it doesn't need Postgres 18's `uuidv7()`.
- **Valkey 9.1,** the BSD-licensed fork of Redis, which runs the Lua scripts the usage limits need. Valkey 9.2 is still a release candidate.
- **Hardened like production:**
    - every image is pinned by digest, and every port listens on 127.0.0.1 only;
    - containers run as non-root users, with no Linux capabilities and read-only filesystems;
    - each has a memory limit and a health check.
- **Random passwords per machine:** `make` creates `.env` once from `.env.example`. It is git-ignored, readable only by its owner, and never read by tests.
- **concurrently, not honcho, runs the native processes.** In a test, honcho 2.0 crashed in its own signal handler while stopping and left its child processes running. concurrently stops each child's whole process tree.

## Consequences

- **Easier:**
    - one command starts everything, and the data survives restarts in Docker volumes;
    - what you see in development (routing, streaming, WebSockets) is what production does.
- **Harder:**
    - `make dev` needs Docker Desktop. Under plain Docker Engine on Linux, containers can't reach the host's 127.0.0.1. A fully containerised `make up` will cover Linux and CI;
    - Postgres and Valkey publish their ports on 127.0.0.1 so the native processes can reach them. The production configuration won't publish them.
- **How to reverse it:**
    - each version is one line in `compose.yaml`;
    - the routing lives in one Caddyfile;
    - going fully native means installing the three servers and pointing the settings at them.

## Revisions

### 2026-11-19: `make dev` runs the worker too

The background jobs have a worker of their own ([ADR 0024](0024-background-jobs-in-a-queue-in-postgres.md)), which `make up` runs as a container. `make dev` now runs it as well, inside the API's process: `uv run actio --role all --reload` takes the place of `--role api --reload` ([ADR 0001](0001-one-app-several-process-roles.md), revision of the same day). Nothing else in `make dev` changed: it still stops the containers of `make up` first, migrates, and runs two processes, named `api` and `web`.

- **One process, not a third one.** A worker run beside the API would need a reloader of its own; without one it would go on running the code it started with after a file was saved, and run jobs that the code no longer has. Inside the API's process it reloads with the API, by the same watcher. And it is one process fewer on a 16 GB machine.
- **Its log is part of the API's,** in the same terminal, one line a record, as the worker's container writes it.
- **Saving a file stops the worker as Docker stops a container:** the job in hand gets its grace, as the answers being written get theirs, and then the new code starts.
- **Ctrl-C is rougher than a save.** concurrently sends the terminal's SIGINT on to the processes it started, which have had it from the terminal already, and uvicorn takes a second SIGINT as its force quit: the application's shutdown is skipped. An idle worker has stopped by then. A job in hand, like an answer being written, is cut off. That is older than this revision, and is still to be put right ([ADR 0024](0024-background-jobs-in-a-queue-in-postgres.md), revision of the same day, "Still not done").

### 2026-11-19: Ctrl-C shuts the application down

"Ctrl-C is rougher than a save", in the revision above, no longer holds. The server that `make dev` runs now takes the first signal it gets and lets later ones change nothing, so a Ctrl-C stops it as a save does: an answer being written gets its moment and is recorded, and the job in hand gets its grace ([ADR 0022](0022-turns-that-run-apart-from-their-request.md), revision of the same day, with what was measured). Nothing in `make dev`'s own command line changed: a process group of its own for the API was tried first, and didn't help.

A second Ctrl-C no longer quits at once there. Ctrl-\ does.

### 2026-11-22: the proxy compresses what the API answers as JSON

#### Context

Caddy passed the API's answers on as they came: its JSON went out as it is, whatever the client takes, and a conversation is read whole when it is opened, with the messages of the branch it shows. Measured on the committed stack through its proxy (Caddy 2.11.9, the pinned image), in a stack of its own with the fake model, on an M1 Pro, on 2026-11-22, the wire read raw, without a browser:

| Response | Bytes on the wire |
|---|---|
| A conversation of 20 messages | 37,282 |
| A conversation of 200 messages | 416,329 |
| A conversation of 600 messages | 1,229,452 |
| The API's description, `/api/openapi.json` | 32,027 |

The conversations are real prose with markdown, 110 to 260 words a message, asked of the fake model, which echoes: so each text is in the JSON twice, a question and its answer, and the ratios below are better than a real conversation's.

The web app's own files were compressed already, by Next itself: the page, its scripts and its stylesheet arrive as gzip (8,500 → 2,758 bytes, 182,912 → 48,865, 40,069 → 7,783) for a client that takes gzip. This revision doesn't touch them.

#### Options considered

The first three were built into the proxy's image and measured, in the same way:

1. **Caddy's `encode` with nothing named,** in the API's block: its own formats, zstd before gzip, and its own list of what is worth compressing, which has `application/json*` and `text/*`, and so takes the answers' streams, `text/event-stream`, too. Caddy's documentation says such responses are not held back for the minimum length, and are streamed as written. Measured: they are compressed, and flushed at each event.
    - Pro: JSON came out as with the others. To a client that takes zstd, an answer's stream was 3 times smaller (20,377 → about 6,800 bytes), and arrived as promptly: the first word and the longest silence were unchanged.
    - Con: to a client that takes gzip only, the same stream was larger than uncompressed, about 23,060 bytes against 20,377: every flushed event costs gzip a few bytes.
    - Con: each stream holds a compressor of its own for as long as its answer is written. With 40 answers streaming at once, the proxy's memory peaked at 36.9 MiB uncompressed, 60.0 MiB with gzip and 86.4 MiB with zstd; with 80 at once, at 42.5 MiB uncompressed, 84.7 MiB with gzip and 128.1 MiB with zstd, which is the container's limit. The proxy's CPU time for those 80 zstd streams was 44.3 s, against 3.4 s uncompressed; with 40, 20.1 s against 1.6 s.
    - An answer's stream is some 50 to 70 bytes a word, a few kB a second at the fastest: there is nothing to gain there that is worth that.
2. **JSON alone, zstd before gzip** (`encode zstd gzip { match { header Content-Type application/json* } }`).
    - Pro: zstd made the JSON 5 to 6 % smaller than gzip does (200 messages: 73,200 against 76,700 bytes; 600 messages: 207,400 against 220,700), and cost the proxy a little less CPU: 8.2 ms against 9 to 10 ms for a read of the 600-message conversation.
    - Con: more memory. One reader after another, the proxy peaked at 33 to 38 MiB with zstd, against 25 to 33 MiB with gzip and 23 to 26 MiB uncompressed; with 200 readers at once at 73.9 MiB, against 58 to 59 MiB with gzip and 50 to 51 MiB uncompressed; with 400 at once at 89.7 MiB, against 68 to 79 MiB with gzip and 70 to 72 MiB uncompressed.
3. **JSON alone, gzip alone.**
    - Pro: every browser takes gzip, and it costs the proxy little more memory than passing the bytes on as they are, 9 MiB at the most in what was measured: in two rounds, with 200 readers at once 58.3 and 58.9 MiB against 50.5 and 49.7, with 400 at once 68.1 and 78.5 MiB against 70.2 and 71.9.
    - Con: it goes without what zstd would add, which is 5 to 6 % of a response that is already several times smaller.
4. **The API compresses its own answers,** with a middleware in the API's process. Not built, and not measured.
    - Con: it would spend the CPU in the API's one process, which also writes the answers, and would have to keep the streams out by hand as well. The proxy is where the web app's and the API's responses already pass.
5. **Next's own compression switched off, and the proxy compressing the web app's files too.** Not built: the web app's files are gzip already, and they are not this revision's.

#### Decision

The third. **On the API's routes, a response whose `Content-Type` is `application/json` goes out compressed with gzip, to a client that takes gzip, once its body is over 512 bytes. The proxy compresses nothing else.**

- **In `deploy/caddy/Caddyfile`,** inside `handle @api`, between the limit on a request's body and `reverse_proxy`: `encode gzip { match { header Content-Type application/json* } }`. Caddy's default `minimum_length`, 512 bytes, is left as it is.
- **What it does,** measured on the pinned image in a scratch stack, with the config built into the proxy's image: a response of `/api/*` or `/v1/*` whose `Content-Type` is `application/json`, and whose body is over 512 bytes, goes out as `Content-Encoding: gzip` to a client whose `Accept-Encoding` has gzip, also to one that names zstd first. It gets `Vary: Accept-Encoding`, and loses its `Content-Length`. A client that takes only something else (`br`), or sends no `Accept-Encoding`, gets it as it is, with its `Content-Length`, and with `Vary: Accept-Encoding`.
- **Smaller JSON goes out as before:** `/api/healthz` (33 bytes), `/api/me` (125) and `/api/usage` (259) are under the minimum.
- **Untouched, as before, byte for byte:** an answer's stream (`text/event-stream; charset=utf-8`, with no `Content-Encoding` and no `Vary`), problem documents (`application/problem+json`: a 404, a 401, a 422, and the proxy's own 413), the API's docs page (`text/html`), and everything of the web app.
- **Matched by the response's type, and narrow on purpose:** Caddy's own list takes `text/*`, and with it the answers' streams (the first option).

**Why:** Caddy's own list compresses the answers' streams, which makes them larger for a client that takes gzip only, and with zstd took the proxy to its memory limit at 80 answers at once, for streams of a few kB a second. Of the two that compress JSON alone, gzip is taken by every browser, and costs the proxy little more memory than passing the bytes on as they are, 9 MiB at the most in what was measured; zstd's gain over it is 5 to 6 % of a response that is already several times smaller, for more memory at every count of readers measured. The API's own compression would spend the CPU of the process that writes the answers.

**What a compressed length can give away.** The attack known as BREACH reads a secret from the lengths of responses: where a response holds a secret next to text an attacker chooses, compressed, the attacker can guess the secret from the response's length, over many tries. Nothing the API answers offers it that. The session is a cookie (`HttpOnly`, `SameSite=Lax`), never in a body. No body holds a token that guards a request. And a request that changes something is refused when another site's page sent it (`actio.api.same_origin`), so another site can't put text of its choosing into a visitor's conversation.

#### Tests

- **In `deploy/tests/test_compose_policy.py`,** read from the Caddyfile without its comments: it has one `encode`, in the API's block; its format is gzip alone; it has a `match`, which takes a response by its `Content-Type` alone, takes `application/json`, what the API's JSON answers carry, and doesn't take the media type of an answer's stream, `ui_stream.MEDIA_TYPE`, bare or as the API sends it, `text/event-stream; charset=utf-8`, nor that of the API's errors, `problems.MEDIA_TYPE`. A pattern is read with Python's `fnmatch.fnmatchcase`, which takes a `*` at a value's start or end for anything, as Caddy's header matcher does. The test fails with the `encode` removed, with no `match` (Caddy's own list), with a `match` line for `text/*` or `text/event-stream*`, with a `match` that takes everything under `application/`, with `zstd` among the formats, and with a second `encode` outside the API's block.
- **End to end, in a real browser** (`e2e/compressed.spec.ts`): a guest asks a question of about 300 words. The response of the request that asks it is `text/event-stream`, with no `Content-Encoding`. Once the answer is complete, a reload reads the conversation: that response says `Content-Encoding: gzip`, and its `Vary` names `Accept-Encoding`; the browser's own count of it, its Resource Timing entry, has less than half as many bytes on the wire (`encodedBodySize`, which is more than nothing) as decoded (`decodedBodySize`); and the page shows the answer whole. The same conversation, read with the browser's session by a client that takes no gzip (`Accept-Encoding: identity`), comes with no `Content-Encoding`, with a `Vary` that names `Accept-Encoding` too, and is the same JSON as the page read, but for `title_pending`, which changes by itself 15 seconds after the first answer ([ADR 0025](0025-titles-written-by-a-model.md)).
- **Mistakes planted in a scratch copy of the config,** one at a time, eighteen of them: the test of the containers' rules failed for seventeen. The end-to-end test, run against a stack whose proxy was built from the copy, failed for the eleven it was run on: the proxy compressing nothing, the streams compressed in six ways, zstd before gzip, a type or a header misspelt, and the one mistake the first test doesn't hold, a `minimum_length` that nothing reaches.

#### Measured

In the same stack, through the proxy with this config, the wire read raw. A compressed size moves by a few dozen bytes from one read to the next, because the proxy flushes what it passes on as it comes.

| Response | Bytes on the wire before | With gzip |
|---|---|---|
| A conversation of 20 messages | 37,282 | 7,666 |
| A conversation of 200 messages | 416,329 | about 76,700 |
| A conversation of 600 messages | 1,229,452 | about 220,700 |
| `/api/openapi.json` | 32,027 | 6,198 |
| `/api/models` | 545 | 212 |

- **Reading,** in two rounds: the 600-message conversation, read 40 times in a row, took 44 to 46 ms at the median as it is, and 44 to 47 ms compressed; the 200-message one 22 ms either way.
- **The proxy's own CPU time** for one read, over 200 reads in a row, by the container's own counter, in two rounds: for the 600-message conversation about 2 ms as it is and 9 to 10 ms compressed; for the 200-message one 1.3 to 1.5 and 3.8 to 4.2 ms; for the 20-message one 0.8 to 1.0 and 1.2 to 1.5 ms.
- **An answer's stream,** of about 390 words at 50 words a second, five runs each: the longest silence between two pieces 34 to 38 ms at the median, before and after; the same 20,377 bytes, in the same 323 pieces. Its first word, timed back to back in three rounds of 30 short answers each: after 31 to 33 ms at the median with the proxy as it was, and after 31 to 32 ms with this config. Someone who joins an answer of some 3,000 words under way gets its stream as before too: about 200,000 bytes, not encoded.
- **The proxy's memory** (its container has `mem_limit: 128m`): the most it held, by the container's own counter, started anew for each run, in two rounds. With 200 readers at once reading the 200-message conversation over and over for 20 seconds, 50.5 and 49.7 MiB uncompressed, and 58.3 and 58.9 MiB with gzip; with 400 at once, 70.2 and 71.9 MiB uncompressed, and 68.1 and 78.5 MiB with gzip.
- **A real model's conversation is about three times smaller,** not five: the fake model's conversations above hold every text twice. On the stack of `make up`, with the default model (`qwen3.5:4b-q4_K_M`), a conversation of 40 messages, 20 questions about bicycles answered with prose, lists, tables and code, 3,800 words in all, was 43,478 bytes as it is, and 14,407 with gzip.
- **In three browsers,** Playwright's Chromium, Firefox and WebKit: Chromium and Firefox say `Accept-Encoding: gzip, deflate, br, zstd`, and WebKit `gzip, deflate`. Each got the 200-message conversation as gzip, about 77,000 bytes on the wire for its 416,329 by the browser's own count, and showed all 200 messages; each got the list of conversations as gzip, and an answer's stream not encoded, and showed the answer. None reported an error of the page.
- **On a slow connection,** in Chromium with the network held to 150 ms of latency and 1.6 Mbit/s down, the app's own files in already, from the click on a conversation in the list, five runs each, at the median:

    | | Before | With gzip |
    |---|---|---|
    | 200 messages: the conversation read | 2,153 ms | 537 ms |
    | 200 messages: its first message shown | 2,237 ms | 623 ms |
    | 200 messages: all of them shown | 2,534 ms | 903 ms |
    | 600 messages: the conversation read | 6,030 ms | 1,224 ms |
    | 600 messages: its first message shown | 6,121 ms | 1,311 ms |
    | 600 messages: all of them shown | 7,100 ms | 2,296 ms |

    These are the fake model's conversations. A real one, three times smaller where these are five, gains less.

#### Consequences

- **A JSON answer over 512 bytes has no `Content-Length` any more,** for a client that takes gzip: it arrives in chunks, and its length is known once it has all arrived.
- **Those answers carry `Vary: Accept-Encoding`,** to a client that takes gzip and to one that doesn't: a cache in between keeps the compressed and the plain ones apart.
- **The proxy spends CPU on it, and a little memory:** for one read, 9 to 10 ms where it spent about 2 ms for the 600-message conversation, about 4 where 1.5 ms for the 200-message one, and 1.2 to 1.5 where 0.8 to 1.0 ms for the 20-message one. Seen from outside, a read took as long: 44 to 47 ms at the median against 44 to 46 for the 600-message conversation, and 22 ms either way for the 200-message one. With hundreds of readers at once it held up to 9 MiB more than without, of its 128.
- **Only JSON is compressed.** A new kind of answer that should be has to be `application/json`; one that streams stays out by its media type, as an answer's stream does with `text/event-stream`. One that streamed as `application/json` would be taken by the match.
- **A client that takes only zstd or br gets the JSON uncompressed,** as before.
- **How to reverse it:** take the `encode` block out of `deploy/caddy/Caddyfile`, with the test in `deploy/tests/` that holds it and `apps/web/e2e/compressed.spec.ts`; `make up` or `make dev` again builds the proxy without it. zstd would be one more word in the directive, and a change to the test on purpose.
