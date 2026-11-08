# Actio developer commands. Run `make help` to list targets.
SHELL := /bin/bash
.DEFAULT_GOAL := help

.PHONY: help doctor install check lint fmt test

help: ## List available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
	    awk 'BEGIN {FS = ":.*?## "} {printf "  %-12s %s\n", $$1, $$2}'

doctor: ## Check that required tools are installed (installs nothing)
	@./scripts/doctor.sh

install: ## Install the Python workspace and dev tools into .venv
	uv sync

check: lint test ## Run all checks: lint, formatting, types and tests

lint: ## Lint, check formatting and type-check (changes nothing)
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy

fmt: ## Format code and apply safe lint fixes
	uv run ruff format .
	uv run ruff check --fix .

test: ## Run the Python tests
	uv run pytest
