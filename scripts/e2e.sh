#!/usr/bin/env bash
# Runs the end-to-end tests: starts the whole stack in containers with a fake model in
# Ollama's place, drives it in a real browser with Playwright, and takes it down again.
#
# The stack has its own project name, port and data, so it runs beside `make up` or
# `make dev` and never touches their database. It reads no .env: its passwords are made
# up for the run and gone with it.
#
# Usage: scripts/e2e.sh [arguments for `playwright test`]
#   --headed              show the browser. The first such run downloads the whole of
#                         Chromium; the tests otherwise drive its headless build
#   ACTIO_E2E_PORT=8090   the port the stack listens on, on 127.0.0.1
#   ACTIO_E2E_KEEP=1      leave the stack running afterwards, to look at it. Then run
#                         the tests again, without this script, with:
#                         pnpm --dir apps/web exec playwright test
#   down                  as the only argument: take down a stack that was left
#                         running, with its data, and run nothing
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1

port="${ACTIO_E2E_PORT:-8090}"
export ACTIO_HTTP_PORT="$port"
# .env.example stands for "no settings of your own": compose reads it instead of .env.
compose=(docker compose --project-name actio-e2e --env-file .env.example
  --file compose.yaml --file compose.e2e.yaml)

POSTGRES_PASSWORD="$(openssl rand -hex 24)"
VALKEY_PASSWORD="$(openssl rand -hex 24)"
export POSTGRES_PASSWORD VALKEY_PASSWORD

# shellcheck disable=SC2329  # called by the trap below, when the script ends
take_down() {
  if [ "${ACTIO_E2E_KEEP:-}" = 1 ]; then
    echo "The stack is still running at http://localhost:$port. To stop it:"
    echo "  make e2e args=down"
  else
    "${compose[@]}" down --volumes --remove-orphans >/dev/null 2>&1
  fi
}

# Compose reads the two passwords above even to stop a stack, so stopping one that was
# left running goes through this script too.
if [ "${1:-}" = down ]; then
  "${compose[@]}" down --volumes --remove-orphans
  exit
fi

# Data left by an earlier run was made with that run's passwords: start from nothing.
"${compose[@]}" down --volumes --remove-orphans >/dev/null 2>&1
trap take_down EXIT

echo "Starting the stack with a fake model at http://localhost:$port ..."
if ! "${compose[@]}" up --detach --wait --build --quiet-pull; then
  echo "The stack did not start. Its last words:" >&2
  "${compose[@]}" logs --no-color --tail 30 >&2
  exit 1
fi

# The browser the tests drive: downloaded once, then found again. Without a window,
# Chromium's headless build is enough (about 90 MB). A run that shows the browser
# needs the whole of it (about 200 MB more).
browser=(--only-shell chromium)
for argument in "$@"; do
  case "$argument" in
  --headed | --debug | --ui) browser=(chromium) ;;
  esac
done
pnpm --dir apps/web exec playwright install "${browser[@]}" || exit 1

ACTIO_E2E_URL="http://localhost:$port" pnpm --dir apps/web exec playwright test "$@"
status=$?

if [ "$status" -ne 0 ]; then
  echo "What the containers logged last:" >&2
  "${compose[@]}" logs --no-color --tail 30 api web caddy fakellm >&2
fi
exit "$status"
