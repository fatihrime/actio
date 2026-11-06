# CLAUDE.md

@AGENTS.md

## Claude Code specifics

- Before changing a subsystem, read its ADRs in `docs/adr/`.
- Prefer `make` targets over ad-hoc commands, so every step stays reproducible.
- Stop after each small step and summarize:
  - what changed;
  - why;
  - how to verify it;
  - a suggested commit message.
