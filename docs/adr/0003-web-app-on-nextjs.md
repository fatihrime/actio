# 0003. A Next.js web app in a pnpm workspace, on a pinned Node.js

- **Status:** accepted
- **Date:** 2026-11-08

## Context

Actio needs a browser interface for chat, the usage meter and admin pages.
- Chat replies stream token by token.
- Later phases add branching conversations, citations and artifacts.
- The Python app ([ADR 0001](0001-one-app-several-process-roles.md)) owns the business logic, so the web app only renders the interface and calls the API.

The same constraints apply as for the backend: one maintainer, a 16 GB laptop, and identical tooling on laptops and in CI.

## Options considered

1. **Next.js (App Router) with React.**
   - Pro: the chat libraries we plan to use (the AI SDK's `useChat`, AI Elements, Streamdown) are React libraries, documented first for Next.js.
   - Pro: server rendering, which later helps public pages such as shared conversations.
   - Con: a Node.js server process at runtime.
   - Con: major versions bring breaking changes.
2. **A Vite + React single-page app, served as static files by Caddy.**
   - Pro: the lightest option, with no Node.js at runtime and the same React libraries.
   - Con: no server rendering; routing, code splitting and translations are ours to assemble.
3. **HTML rendered by FastAPI (Jinja templates and htmx).**
   - Pro: one language and no JavaScript build.
   - Con: a rich streaming chat interface needs hand-written JavaScript anyway, and the ready-made React components are out of reach.

## Decision

We chose option 1: a Next.js app in `apps/web`, inside a pnpm workspace at the repository root.
- **Node.js 26 LTS, pinned in the root `package.json`** (`devEngines.runtime`). pnpm downloads it and records its exact version and checksum in `pnpm-lock.yaml`, just as uv manages Python ([ADR 0002](0002-python-workspace-with-uv.md)). Contributors don't need a Node.js version manager.
- **pnpm 12, pinned with `packageManager`.** Its defaults already resist supply-chain attacks: it won't install a version published less than a day ago, and dependencies can't run install scripts unless `allowBuilds` lists them.
- **TypeScript 6.0 rather than 7, and ESLint 9 rather than 10.** We checked on 2026-11-08: typescript-eslint supports TypeScript below 6.1, and the React, accessibility and import plugins in eslint-config-next declare ESLint 9 at most. We'll move up once they support the new majors.
- **No web fonts downloaded at build time.** The system font stack keeps builds working offline.

## Consequences

- **Easier:**
  - one command, `pnpm install`, sets up the Node.js runtime, the app and its tools;
  - the React chat components are available off the shelf.
- **Harder:**
  - two toolchains, uv and pnpm. Each pins its own runtime in its own lockfile, and `make` hides the difference;
  - production runs a Node.js process next to the API;
  - Next.js upgrades need care. Pinned versions and the tests limit the damage.
- **How to reverse it:** the API speaks plain JSON and server-sent events, with nothing Next.js-specific. A Vite single-page app could replace `apps/web` without backend changes.
