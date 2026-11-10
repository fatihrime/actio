#!/usr/bin/env bash
# Checks a running stack (`make dev` or `make up`) through Caddy: the web page, the API's
# health and readiness, and the API docs. Retries for up to a minute while the stack starts.
# Usage: scripts/smoke-test.sh [base URL]    (default: http://localhost:8080)
set -uo pipefail

base="${1:-http://localhost:${ACTIO_HTTP_PORT:-8080}}"
failures=0

# check NAME PATH TEXT: passes when GET PATH succeeds and the body contains TEXT.
check() {
  local name=$1 path=$2 expected=$3 body
  if body="$(curl --silent --show-error --fail --max-time 5 \
    --retry 30 --retry-delay 2 --retry-all-errors "$base$path")" &&
    [[ $body == *"$expected"* ]]; then
    printf '  ✓ %s\n' "$name"
  else
    printf '  ✗ %s: GET %s did not answer with: %s\n' "$name" "$base$path" "$expected"
    failures=$((failures + 1))
  fi
}

echo "Smoke test against $base"
check "web page" / '>Actio</h1>'
check "API health" /api/healthz '"status":"ok"'
# Ready means: the database is reachable and migrated, and Valkey answers.
check "API readiness" /api/readyz '"status":"ready"'
check "API docs" /api/docs 'swagger-ui'

# A guest session, through the proxy: start one, read it back with its cookie, end it.
session_round_trip() {
  local jar started me
  jar="$(mktemp)"
  started="$(curl --silent --max-time 5 --cookie-jar "$jar" --request POST "$base/api/session/guest")"
  me="$(curl --silent --max-time 5 --cookie "$jar" "$base/api/me")"
  curl --silent --max-time 5 --cookie "$jar" --request DELETE "$base/api/session" >/dev/null
  rm -f "$jar"
  [[ $started == *'"is_guest":true'* && $me == "$started" ]]
}
if session_round_trip; then
  printf '  ✓ %s\n' "guest session"
else
  printf '  ✗ %s: starting a guest session and reading it back did not work\n' "guest session"
  failures=$((failures + 1))
fi

if [ "$failures" -eq 0 ]; then
  echo "All checks passed."
else
  echo "$failures check(s) failed."
  exit 1
fi
