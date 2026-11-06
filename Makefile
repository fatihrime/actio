# Actio developer commands. Run `make help` to list targets.
SHELL := /bin/bash
.DEFAULT_GOAL := help

.PHONY: help doctor

help: ## List available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
	    awk 'BEGIN {FS = ":.*?## "} {printf "  %-12s %s\n", $$1, $$2}'

doctor: ## Check that required tools are installed (installs nothing)
	@./scripts/doctor.sh
