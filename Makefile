# Actio developer commands. Run `make help` to list targets.
SHELL := /bin/bash
.DEFAULT_GOAL := help
# Opt out of Next.js's anonymous usage telemetry in everything make runs.
export NEXT_TELEMETRY_DISABLED := 1

.PHONY: help doctor bootstrap install dev down check lint fmt test build

help: ## List available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
	    awk 'BEGIN {FS = ":.*?## "} {printf "  %-12s %s\n", $$1, $$2}'

doctor: ## Check that required tools are installed (installs nothing)
	@./scripts/doctor.sh

bootstrap: doctor install .env ## Set up a fresh clone: check tools, install everything, create .env, pull images
	docker compose pull

install: ## Install the Python and JavaScript workspaces, dev tools and git hooks
	uv sync
	pnpm install
	uv run pre-commit install

dev: .env ## Run the stack at http://localhost:8080; Ctrl-C stops the API and web app
	docker compose up --detach --wait
	@echo "Open http://localhost:8080 (API docs: /api/docs). 'make down' stops the containers."
	pnpm exec concurrently --kill-others --names api,web --prefix-colors cyan,magenta \
	    "uv run actio --role api --reload" \
	    "pnpm --dir apps/web dev"

down: ## Stop the Docker services; their data is kept
	docker compose down

check: lint test build ## Run everything CI runs: static checks, tests, then the web build

lint: ## Run every static check on all files; fixes what it safely can
	uv run pre-commit run --all-files --show-diff-on-failure

fmt: ## Format code and apply safe lint fixes
	uv run ruff format .
	uv run ruff check --fix .
	pnpm --dir apps/web run format
	pnpm --dir apps/web run lint --fix

test: ## Run the Python and web tests
	uv run pytest
	pnpm --dir apps/web run test

build: ## Build the web app for production
	pnpm --dir apps/web run build

# Created once and never overwritten: Postgres keeps the password it started with.
.env:
	@umask 077 && sed \
	    -e "s/^POSTGRES_PASSWORD=$$/POSTGRES_PASSWORD=$$(openssl rand -hex 24)/" \
	    -e "s/^VALKEY_PASSWORD=$$/VALKEY_PASSWORD=$$(openssl rand -hex 24)/" \
	    .env.example > .env
	@echo "Created .env with random passwords. It is git-ignored; keep it private."
