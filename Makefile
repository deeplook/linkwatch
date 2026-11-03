.DEFAULT_GOAL := help

# Keep the workspace environment local to this project directory.
export UV_PROJECT_ENVIRONMENT := $(CURDIR)/.venv

TEST_PLATFORM ?= $(shell uname -s)
ifeq ($(TEST_PLATFORM),Darwin)
SERVICE_TESTS := tests/test_launchd.py
else ifeq ($(TEST_PLATFORM),Linux)
SERVICE_TESTS := tests/test_systemd.py
else
SERVICE_TESTS :=
endif

.PHONY: help install test build install-tool clean

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*##' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  %-14s %s\n", $$1, $$2}'

install: ## Sync project and development dependencies
	uv sync --package linkwatch

test: ## Run the pytest suite
	@printf 'Testing Link Watch core and %s service backend\n' '$(if $(SERVICE_TESTS),$(TEST_PLATFORM),platform-independent)'
	uv run --package linkwatch python -m pytest -q tests/test_linkwatch.py tests/test_service.py $(SERVICE_TESTS)

build: ## Build wheel and source distribution into dist/
	uv build . --out-dir dist

install-tool: ## Install linkwatch globally as an editable uv tool
	uv tool install --editable --reinstall .

clean: ## Remove this project's build artifacts and test caches
	rm -rf dist build *.egg-info .pytest_cache .ruff_cache
