# 0005. Container images and the production-shaped stack

- **Status:** accepted
- **Date:** 2026-11-09

## Context

[ADR 0004](0004-local-development-stack.md) runs the API and the web app natively during development. Three other situations need them in containers:
- production;
- CI, which should prove that the README's quickstart works on a clean Linux machine;
- anyone who wants to try Actio without installing Python or Node.js.

The images must be small, reproducible and locked down. The stack around them should already follow the rules production will.

## Options considered

**The API image:**
1. **The official Python image, with a virtual environment built by uv.**
   - Pro: a widely known base that gets regular security updates.
   - Pro: uv installs exactly what `uv.lock` pins.
2. **A plain Debian image with the same uv-managed CPython as development.**
   - Pro: byte-for-byte the interpreter we test with locally.
   - Con: we would maintain the Python layer ourselves.
3. **A distroless image.**
   - Pro: the smallest attack surface.
   - Con: no shell for debugging, and an extra moving part early on.

**The web image:**
1. **Next.js's standalone output on the official Node.js image.**
   - Pro: a 40 MB server with only the files it uses.
2. **The full `node_modules` and `next start`.**
   - Con: hundreds of MB, mostly build tools the server never runs.

## Decision

We chose option 1 for both images.
- **Built in two stages.** The first stage installs dependencies in their own layer, which is reused until the lockfile changes, then builds the app. The final stage holds only the result:
  - for the API, a virtual environment with the `actio` package and its locked dependencies, with bytecode compiled at build time;
  - for the web app, the standalone server and its static assets.
- **Same versions as development.** Python 3.13.18 and Node.js 26.13.0, matching uv's and pnpm's pins. Every base image is pinned by digest.
- **Locked-down containers:**
  - each runs as a non-root user;
  - the root filesystem is read-only, with small in-memory scratch space only where needed;
  - all Linux capabilities are dropped and privilege escalation is blocked;
  - each has a memory limit and a health check.
- **Two networks.**
  - `edge` joins Caddy, the web app and the API.
  - `backend` joins the API with Postgres and Valkey. It is `internal`, with no route out, so only the API can reach the data, and the data services can't reach the internet.
  - Only Caddy publishes a port, on 127.0.0.1.
- **Rules as tests.** `deploy/tests/test_compose_policy.py` checks every rule above against the configuration Compose resolves. The `make dev` overlay is checked for what it changes: its extra ports listen on 127.0.0.1 only.
- **CI runs the quickstart.** A job runs `make up` and the smoke test on a fresh Linux runner that has only Docker, `make` and `openssl`.

## Consequences

- **Easier:**
  - `make up` works on any machine with Docker;
  - CI proves the README on every change;
  - weakening the setup, for example by publishing Postgres or running as root, fails a test.
- **Harder:**
  - the first build downloads every dependency inside Docker, which takes minutes on a slow connection;
  - updating a base image means updating its digest. We'll automate this with Renovate later.
- **How to reverse it:**
  - a base image is one `FROM` line;
  - the images are standard OCI images, usable outside Compose;
  - a rule changes deliberately, by changing its test and saying why in an ADR.
