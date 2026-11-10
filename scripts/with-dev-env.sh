#!/usr/bin/env bash
# Runs a command with the settings a natively running API needs in `make dev`: the
# addresses of the Postgres and Valkey containers, which compose.dev.yaml publishes on
# 127.0.0.1, with the passwords from .env.
# Usage: scripts/with-dev-env.sh COMMAND [ARGS...]
set -euo pipefail

cd "$(dirname "$0")/.."
if [ ! -f .env ]; then
  echo "No .env yet: run 'make bootstrap' first." >&2
  exit 1
fi

set -a
# shellcheck disable=SC1091  # .env is created by make and never committed
. ./.env
set +a

export ACTIO_DATABASE_URL="postgresql://actio:${POSTGRES_PASSWORD}@127.0.0.1:${ACTIO_POSTGRES_PORT:-5432}/actio"
export ACTIO_VALKEY_URL="valkey://:${VALKEY_PASSWORD}@127.0.0.1:${ACTIO_VALKEY_PORT:-6379}/0"
# The address the browser uses, through Caddy.
export ACTIO_PUBLIC_URL="http://localhost:${ACTIO_HTTP_PORT:-8080}"
exec "$@"
