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
