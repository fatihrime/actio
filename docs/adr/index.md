# Decisions

Each architecture decision record (ADR) captures one decision that would be hard to reverse:
- the context;
- the options weighed;
- what was chosen and why;
- how to undo it.

Facts that change over time, such as which versions are current, carry the date they were checked.

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-one-app-several-process-roles.md) | One application, several process roles | Accepted |
| [0002](0002-python-workspace-with-uv.md) | A Python workspace managed by uv, on uv's own CPython | Accepted |
| [0003](0003-web-app-on-nextjs.md) | A Next.js web app in a pnpm workspace, on a pinned Node.js | Accepted |
| [0004](0004-local-development-stack.md) | A hybrid local stack: services in Docker, the apps native, Caddy in front | Accepted |
| [0005](0005-container-images.md) | Container images and the production-shaped stack | Accepted |

To record a new decision, copy the [template](0000-template.md) to the next free number.
