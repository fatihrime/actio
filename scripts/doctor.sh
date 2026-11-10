#!/usr/bin/env bash
# Checks that the tools Actio needs are installed. Installs nothing.
# Exit code: 0 when every required tool is present, 1 otherwise.
# Works with the bash 3.2 that ships with macOS, so: no associative arrays.
set -u

if [ -t 1 ]; then G=$'\033[32m' Y=$'\033[33m' R=$'\033[31m' N=$'\033[0m'; else G='' Y='' R='' N=''; fi

missing=0
ok()   { printf '  %s✓%s %-14s %s\n' "$G" "$N" "$1" "$2"; }
warn() { printf '  %s!%s %-14s %s\n' "$Y" "$N" "$1" "$2"; }
fail() { printf '  %s✗%s %-14s %s\n' "$R" "$N" "$1" "$2"; missing=1; }

have() { command -v "$1" >/dev/null 2>&1; }

# First "X.Y" or "X.Y.Z" found in a command's output.
version_of() { "$@" 2>/dev/null | grep -Eo '[0-9]+\.[0-9]+(\.[0-9]+)?' | head -n 1; }

# True when dotted version $1 >= $2.
version_ge() {
  [ "$(printf '%s\n%s\n' "$2" "$1" | sort -t. -k1,1n -k2,2n -k3,3n | head -n 1)" = "$2" ]
}

echo "Required"

if have git; then ok git "$(version_of git --version)"; else fail git "install Git: https://git-scm.com/downloads"; fi

if have docker; then
  if docker compose version >/dev/null 2>&1; then
    ok docker "$(version_of docker --version), compose $(version_of docker compose version)"
  else
    fail docker "the Docker Compose v2 plugin ('docker compose') is missing"
  fi
  if ! docker info >/dev/null 2>&1; then
    warn docker-daemon "not running: start Docker Desktop (macOS) or the docker service (Linux)"
  fi
else
  fail docker "install Docker: https://docs.docker.com/get-docker/"
fi

if have uv; then
  ok uv "$(version_of uv --version)"
else
  fail uv "brew install uv   (or: curl -LsSf https://astral.sh/uv/install.sh | sh)"
fi

if have openssl; then
  ok openssl "$(version_of openssl version)"
else
  fail openssl "install OpenSSL; make uses it to generate the passwords in .env"
fi

# No separate Node.js check: pnpm installs the Node.js version pinned in package.json.
if have pnpm; then
  pnpm_version="$(version_of pnpm --version)"
  if version_ge "$pnpm_version" 12.0.0; then
    ok pnpm "$pnpm_version (installs the pinned Node.js itself)"
  else
    fail pnpm "found $pnpm_version, need >= 12: https://pnpm.io/installation"
  fi
else
  fail pnpm "install pnpm 12 or newer: https://pnpm.io/installation"
fi

echo
echo "Optional"

if have ollama; then
  # With no server running, `ollama --version` prints a warning line first; the client's
  # version is the last number on its last line.
  ollama_version="$(ollama --version 2>/dev/null | tail -n 1 | grep -Eo '[0-9]+\.[0-9]+(\.[0-9]+)?' | tail -n 1)"
  if ! version_ge "${ollama_version:-0}" 0.40.0; then
    warn ollama "found ${ollama_version:-an unknown version}; Actio needs 0.40 or newer: https://ollama.com/download"
  elif curl -fsS --max-time 2 http://localhost:11434/api/version >/dev/null 2>&1; then
    ok ollama "$ollama_version, server running"
  else
    warn ollama "$ollama_version installed but not serving: run 'ollama serve' or open the Ollama app"
  fi
else
  warn ollama "not installed; needed only for real local models (tests use a fake LLM): https://ollama.com/download"
fi

echo
if [ "$missing" -eq 0 ]; then
  echo "All required tools are present."
else
  echo "Some required tools are missing; see the hints above. Nothing was installed."
fi
exit "$missing"
