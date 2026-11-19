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

## Revisions

### 2026-11-14: a third image, for tests only

The end-to-end tests run this stack with a fake model as one more container ([ADR 0017](0017-end-to-end-tests-on-the-real-stack.md)). Its image, `packages/fakellm/Dockerfile`, is built like the API's, and its container follows every rule above: the same test checks the two compose files together. No installation ships it.

### 2026-11-18: a fourth image, the proxy with its config

Caddy's config now holds a limit that protects the API: the size of a request ([ADR 0023](0023-answers-that-wait-their-turn.md)). On a stack that was running, `make up` left that limit off, and nothing said so.

- **Why.** The proxy ran the pinned Caddy image, with the Caddyfile mounted into it as one file. An editor or git replaces a file with a new one, which a mount of the old file never shows: the container no longer found its Caddyfile at all. And Compose doesn't recreate a container because a file it mounts has changed, whether the file is a bind mount or one of Compose's `configs:` (tried on Compose 5.7.0). The proxy went on with the config it had started with.
- **Now the config is part of an image,** built from `deploy/caddy/Dockerfile`: the same pinned Caddy image, with the Caddyfile copied in, as the API's and the web app's code are copied into theirs. A change to the file is a new image, and `make up` or `make dev` recreates the proxy from it; `make dev` builds it too now. Nothing is mounted into the proxy.
- **The build checks the config** (`caddy validate`): a mistake in it stops the build, and the proxy that is running goes on as it was.
- **Watching the file was tried first,** with the folder mounted and Caddy started with `--watch`. Caddy's documentation says that the feature "is intended for use only in local development environments!", and a watcher can catch a file half-written. So it was dropped for the image.
- **The rules above hold for it,** as for the other images: its base is pinned by digest, and its container runs read-only, as a non-root user, without capabilities, with its two folders in memory. A test in `deploy/tests/` holds it to being built from `deploy/caddy/`, with nothing mounted, and to a build that checks the config.

### 2026-11-19: the worker, a container with a restart policy and a file for its health

`make up` runs one more container from the API's image: the worker, which runs the background jobs ([ADR 0024](0024-background-jobs-in-a-queue-in-postgres.md), revision of the same day). The rules above hold for it, and the tests in `deploy/tests/` check them as for every other container. Three things are new.

- **A container that nothing reaches.** It sits on `backend` alone, beside Postgres and Valkey, and publishes no port. "Only the API can reach the data", above, was already untrue of the migration job ([ADR 0008](0008-postgres-through-sqlalchemy-and-psycopg.md)); the worker reaches it too. The three containers on `edge` are still Caddy, the web app and the API.
- **The first health check that is a file.** With no port to probe, the worker touches a file on its own tmpfs every five seconds while it works, and its health check, `python -m actio.core.alive`, says whether the file was touched within the last twenty.
- **The first restart policy other than `no`.** The worker ends when it can't go on, and `restart: on-failure` starts it again then, and not when it ended well. So it stays down with the rest of the stack after Docker itself was restarted, since Docker stops it first and it ends well; one that Docker couldn't stop comes back alone ([ADR 0024](0024-background-jobs-in-a-queue-in-postgres.md) has what was measured). It is the only one: a test fails if another container gets a policy.

It is also given 25 seconds to stop, longer than its grace for the job in hand, and an init process in front of it, as the web app has.
