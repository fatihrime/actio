# Actio developer commands. Run `make help` to list targets.
SHELL := /bin/bash
.DEFAULT_GOAL := help

.PHONY: help doctor install check lint fmt test

help: ## List available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
	    awk 'BEGIN {FS = ":.*?## "} {printf "  %-12s %s\n", $$1, $$2}'

doctor: ## Check that required tools are installed (installs nothing)
	@./scripts/doctor.sh

install: ## Install the Python workspace, dev tools and git hooks
	uv sync
	uv run pre-commit install

check: lint test ## Run everything CI runs: static checks, then tests

lint: ## Run every static check on all files; fixes what it safely can
	uv run pre-commit run --all-files --show-diff-on-failure

fmt: ## Format code and apply safe lint fixes
	uv run ruff format .
	uv run ruff check --fix .

test: ## Run the Python tests
	uv run pytest
