# Actio developer commands. Run `make help` to list targets.
SHELL := /bin/bash
.DEFAULT_GOAL := help
# Opt out of Next.js's anonymous usage telemetry in everything make runs.
export NEXT_TELEMETRY_DISABLED := 1
# `make dev` layers the development overlay on top of compose.yaml.
DEV_COMPOSE := docker compose --file compose.yaml --file compose.dev.yaml

.PHONY: help doctor bootstrap install up dev down smoke migration openapi docs check lint fmt test test-live build

help: ## List available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
	    awk 'BEGIN {FS = ":.*?## "} {printf "  %-12s %s\n", $$1, $$2}'

doctor: ## Check that required tools are installed (installs nothing)
	@./scripts/doctor.sh

bootstrap: doctor install .env ## Set up a fresh clone: check tools, install everything, create .env, pull images
	docker compose pull --ignore-buildable

install: ## Install the Python and JavaScript workspaces, dev tools and git hooks
	uv sync
	pnpm install
	uv run pre-commit install

up: .env ## Run the whole stack in containers, shaped like production, at http://localhost:8080
	docker compose up --detach --wait --build
	@echo "Open http://localhost:8080 (API docs: /api/docs). 'make down' stops everything."

dev: .env ## Run the stack at http://localhost:8080 with the API and web app native; Ctrl-C stops them
	docker compose stop api web # in case `make up` left them running
	$(DEV_COMPOSE) up --detach --wait postgres valkey caddy
	./scripts/with-dev-env.sh uv run actio --role migrate
	@echo "Open http://localhost:8080 (API docs: /api/docs). 'make down' stops the containers."
	./scripts/with-dev-env.sh pnpm exec concurrently --kill-others --names api,web \
	    --prefix-colors cyan,magenta \
	    "uv run actio --role api --reload" \
	    "pnpm --dir apps/web dev"

down: ## Stop all containers; their data is kept
	$(DEV_COMPOSE) down

smoke: ## Check the running stack through Caddy: the web page, and that the API is healthy and ready
	@./scripts/smoke-test.sh

# Compares the table models with a database at the latest migration, and writes what
# differs into a new numbered file in apps/api/src/actio/migrations/versions/. Read it
# before committing: it is a draft.
migration: .env ## Write a new database migration from the table models: make migration name="add users"
	@test -n "$(name)" || { echo 'Usage: make migration name="what it does"'; exit 1; }
	$(DEV_COMPOSE) up --detach --wait postgres
	./scripts/with-dev-env.sh uv run actio --role migrate
	./scripts/with-dev-env.sh uv run alembic --config apps/api/alembic.ini revision --autogenerate \
	    --rev-id "$$(printf '%04d' $$(( $$(ls apps/api/src/actio/migrations/versions/*.py | wc -l) + 1 )))" \
	    --message "$(name)"

# The API's description of itself is kept as a file, and the web app's types are
# generated from it. Tests on both sides fail when either is out of date (ADR 0015).
openapi: ## Record the API's description again (apps/api/openapi.json), and the web app's types from it
	ACTIO_RECORD_OPENAPI=1 uv run pytest --quiet --no-cov apps/api/tests/test_api_description.py
	pnpm --dir apps/web run api-types

docs: ## Preview the documentation site at http://localhost:8001, reloading on edits
	uv run zensical serve --dev-addr 127.0.0.1:8001

check: lint test build ## Run everything CI runs: static checks, tests, then the builds

lint: ## Run every static check on all files; fixes what it safely can
	uv run pre-commit run --all-files --show-diff-on-failure

fmt: ## Format code and apply safe lint fixes
	uv run ruff format .
	uv run ruff check --fix .
	pnpm --dir apps/web run format
	pnpm --dir apps/web run lint --fix

# The quota tests need Docker for Valkey. Without it they'd be skipped and the coverage
# gate would fail with a misleading number, so require it and fail with the real reason.
test: ## Run the Python and web tests (needs Docker); fails below 90% coverage of the quota engine
	ACTIO_REQUIRE_DOCKER=1 uv run pytest --cov
	pnpm --dir apps/web run test

# Not part of `make check`: CI has no Ollama. Pull the model first: ollama pull granite4:micro
test-live: ## Run the model-client tests against a real Ollama server, with granite4:micro
	ACTIO_TEST_OLLAMA_URL=$${ACTIO_TEST_OLLAMA_URL:-http://127.0.0.1:11434} uv run pytest packages/llm

build: ## Build the web app for production, and the documentation site (strict: warnings fail)
	pnpm --dir apps/web run build
	uv run zensical build --strict

# Created once and never overwritten: Postgres keeps the password it started with.
.env:
	@umask 077 && sed \
	    -e "s/^POSTGRES_PASSWORD=$$/POSTGRES_PASSWORD=$$(openssl rand -hex 24)/" \
	    -e "s/^VALKEY_PASSWORD=$$/VALKEY_PASSWORD=$$(openssl rand -hex 24)/" \
	    .env.example > .env
	@echo "Created .env with random passwords. It is git-ignored; keep it private."
