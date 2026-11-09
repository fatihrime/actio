# Actio developer commands. Run `make help` to list targets.
SHELL := /bin/bash
.DEFAULT_GOAL := help
# Opt out of Next.js's anonymous usage telemetry in everything make runs.
export NEXT_TELEMETRY_DISABLED := 1

.PHONY: help doctor install check lint fmt test build

help: ## List available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
	    awk 'BEGIN {FS = ":.*?## "} {printf "  %-12s %s\n", $$1, $$2}'

doctor: ## Check that required tools are installed (installs nothing)
	@./scripts/doctor.sh

install: ## Install the Python and JavaScript workspaces, dev tools and git hooks
	uv sync
	pnpm install
	uv run pre-commit install

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
