# Bowra Community development commands.
.DEFAULT_GOAL := help
.ONESHELL:
SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
UV ?= uv
FRONTEND_DIR := packages/bowhead_frontend
PY_GUARDS := scripts/check_theme_surface_coverage.py scripts/check_chart_style_registry.py scripts/check_third_party_notices.py scripts/check_configuration_coverage.py

.PHONY: help install install-frontend frontend-assemble run-gateway run-frontend fmt lint test typecheck-frontend build-frontend third-party-notices

help: ## List available commands
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z0-9_-]+:.*?## / {printf "  %-24s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

install: ## Install Python workspace and development dependencies
	$(UV) sync --frozen --all-packages --all-extras

install-frontend: ## Install the locked frontend dependencies
	cd $(FRONTEND_DIR) && npm ci

frontend-assemble: ## Materialize the fixed Community frontend before repository guards
	cd $(FRONTEND_DIR) && node scripts/assemble-edition.mjs

run-gateway: ## Prepare the database and run the API gateway with local configuration
	set -a; source .env; set +a
	BOWHEAD_STARTUP_BOWHEAD_BIN="$$(pwd)/.venv/bin/bowhead" BOWHEAD_STARTUP_PYTHON_BIN="$$(pwd)/.venv/bin/python" BOWHEAD_STARTUP_UVICORN_BIN="$$(pwd)/.venv/bin/uvicorn" sh deploy/start-app.sh

run-frontend: ## Start frontend development against the gateway
	cd $(FRONTEND_DIR) && NEXT_PUBLIC_BOWHEAD_SSE_MODE=sse BOWHEAD_DEV_PROXY_TARGET=http://localhost:8080 npm run dev

fmt: frontend-assemble ## Format source and documentation
	$(UV) run python scripts/check_theme_surface_coverage.py --write
	$(UV) run python scripts/check_chart_style_registry.py --write
	@npx prettier --write --print-width 150 "*.{md,yml,yaml}" "**/*.{md,yml,yaml}" --no-error-on-unmatched-pattern
	uvx ruff check packages $(PY_GUARDS) --fix
	uvx ruff format packages $(PY_GUARDS)

lint: frontend-assemble ## Verify shared contracts, notices and Python formatting
	$(UV) run python scripts/check_configuration_coverage.py --check
	$(UV) run python scripts/check_theme_surface_coverage.py --check
	$(UV) run python scripts/check_chart_style_registry.py --check
	$(UV) run python scripts/check_third_party_notices.py --check
	uvx ruff check packages $(PY_GUARDS)
	uvx ruff format --check packages $(PY_GUARDS)

test: ## Run supported package tests
	$(UV) run pytest

typecheck-frontend: ## Check frontend TypeScript
	cd $(FRONTEND_DIR) && npm run typecheck

build-frontend: ## Build the Community static frontend
	cd $(FRONTEND_DIR) && NEXT_STATIC_EXPORT=1 npm run build

third-party-notices: ## Regenerate notices from locally installed dependency metadata
	$(UV) run python scripts/check_third_party_notices.py --write
	@npx prettier --write --print-width 150 THIRD_PARTY_NOTICES.md docs/third-party-license-review.md "docs/第三方许可审查.md"
