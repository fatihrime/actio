# 0002. A Python workspace managed by uv, on uv's own CPython

- **Status:** accepted
- **Date:** 2026-11-08

## Context

The backend will grow from one app into several packages: the usage-limits engine, the model router, and a fake LLM server for tests.
- Each package must be importable on its own.
- All of them must be tested together, against one set of dependency versions.

Developer machines have assorted Pythons installed: Homebrew, pyenv, conda. On the first machine set up for this project, an auto-activated conda environment made the tools build the project on conda's interpreter.

## Options considered

1. **pip + venv + requirements files.**
   - Pro: universal.
   - Con: no lockfile for the whole dependency graph without extra tools.
   - Con: no workspaces.
2. **Poetry.**
   - Pro: a mature lockfile and dependency groups.
   - Con: noticeably slower.
   - Con: multi-package repositories have traditionally relied on plugins.
3. **A uv workspace.**
   - Pro: one lockfile for all members.
   - Pro: standard `pyproject.toml` metadata and dependency groups (PEP 735).
   - Pro: very fast installs.
   - Pro: it can download and pin the Python interpreter itself.

## Decision

We chose option 3:
- **The root `pyproject.toml` is not a package.** It lists the members (`apps/*`, and later `packages/*`) and holds the shared dev tools and their settings.
- **`uv.lock` is committed.** CI sets `UV_LOCKED=1`, so a lockfile that doesn't match `pyproject.toml` fails the build.
- **`python-preference = "only-managed"`.** uv always uses its own CPython build, so every laptop and CI run the same interpreter.
- **Python 3.13.** We checked on 2026-11-08:
  - every planned dependency ships Python 3.14 wheels for Linux;
  - onnxruntime, which local speech-to-text needs, has none for Apple Silicon yet.

  We'll revisit once it does.

## Consequences

- **Easier:**
  - one command, `uv sync`, sets up every package and tool;
  - installs are reproducible;
  - "it works with my Python" problems go away.
- **Harder:**
  - contributors need uv;
  - the first sync downloads an interpreter (about 20 MB);
  - IDEs must be pointed at `.venv`. In PyCharm, that's a uv interpreter.
- **How to reverse it:**
  - the metadata is standard (PEP 621), so other tools can read it;
  - removing `python-preference` brings back system interpreters;
  - changing the Python version means editing `.python-version`, `requires-python` and the tools' target versions.
