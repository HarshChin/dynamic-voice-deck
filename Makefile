# Dynamic Voice Deck — developer entry points.
#
# Canonical target list: docs/TRD.md §14 (TR-210, TR-214).
# CI runs `make lint` and `make test` (see .github/workflows/ci.yml);
# `make check` runs both in one go locally.
#
# Every target is .PHONY. That matters here: `backend` and `frontend` are also
# directory names, so without .PHONY make would consider those targets already
# up to date and do nothing.
#
# All recipes use relative paths (`cd backend`, `cd frontend`) so the repository
# may live under a path containing spaces.

SHELL := /bin/bash
.DEFAULT_GOAL := help

# Aggregate targets depend on ordered sub-targets (`check` = lint, then test),
# so never let `-j` reorder them.
.NOTPARALLEL:

.PHONY: help setup backend frontend \
        lint lint-backend lint-frontend \
        format format-backend format-frontend \
        test test-backend test-frontend \
        test-integration test-e2e evals check clean

## ---------------------------------------------------------------------------
## Help
## ---------------------------------------------------------------------------

help: ## Show this help
	@echo "Dynamic Voice Deck — make targets"
	@echo ""
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z0-9_-]+:.*## / {printf "  \033[36m%-17s\033[0m %s\n", $$1, $$2}' "$(firstword $(MAKEFILE_LIST))"
	@echo ""

## ---------------------------------------------------------------------------
## Setup and run
## ---------------------------------------------------------------------------

setup: ## Install backend (uv) and frontend (npm) dependencies
	cd backend && uv sync --all-extras
	cd frontend && npm install

backend: ## Run the FastAPI dev server with reload on :8000
	cd backend && uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

frontend: ## Run the Vite dev server on :5173
	cd frontend && npm run dev

## ---------------------------------------------------------------------------
## Quality gates
## ---------------------------------------------------------------------------

lint: lint-backend lint-frontend ## Lint and type-check both backend and frontend

lint-backend: ## ruff check + ruff format --check + mypy (backend)
	cd backend && uv run ruff check app tests
	cd backend && uv run ruff format --check app tests
	cd backend && uv run mypy app

lint-frontend: ## eslint + tsc --noEmit + prettier --check (frontend)
	cd frontend && npm run lint
	cd frontend && npm run typecheck
	cd frontend && npm run format:check

format: format-backend format-frontend ## Autoformat and autofix both backend and frontend

format-backend: ## ruff format + ruff check --fix (backend)
	cd backend && uv run ruff format app tests
	cd backend && uv run ruff check --fix app tests

format-frontend: ## prettier --write + eslint --fix (frontend)
	cd frontend && npm run format
	cd frontend && npm run lint:fix

test: test-backend test-frontend ## Run backend and frontend unit + contract tests

test-backend: ## pytest with coverage (backend)
	cd backend && uv run pytest

test-frontend: ## vitest run (frontend)
	cd frontend && npm run test

test-integration: ## Backend integration tests against real providers (needs GROQ_API_KEY)
	cd backend && uv run pytest -m integration

check: lint test ## Run lint then test — the same gates CI enforces

build: ## Build the frontend into frontend/dist, which the backend then serves at / (TR-212)
	cd frontend && npm run build

serve: build ## Build, then run one process serving both the API and the app on :8000
	cd backend && uv run uvicorn app.main:app --host 127.0.0.1 --port 8000

# Starts its own backend with fake providers, so nothing here reaches a real
# model and the assertions can name exact slides and exact words. Stop
# `make backend` first: the suite refuses to adopt a server it did not start.
test-e2e: ## Playwright end-to-end suite against fake providers (TC-E2E-001..004)
	cd frontend && npm run test:e2e

# Agent evals call real models and consume free-tier quota (TR-204), so they
# stay opt-in and out of CI.
evals: ## Agent eval suites E1-E6 against a real model (needs GROQ_API_KEY)
	cd backend && uv run python -m evals.run_evals $(if $(SUITE),--suite $(SUITE),) $(if $(MODEL),--model $(MODEL),)

## ---------------------------------------------------------------------------
## Housekeeping
## ---------------------------------------------------------------------------

# Deletes only build and cache artefacts, and only under this repository (every
# path below is relative to the Makefile's directory). node_modules/ and .venv/
# are pruned, not removed — reinstalling them is what `make setup` is for.
clean: ## Remove caches, coverage output, and frontend build artefacts
	find . \( -name node_modules -o -name .venv -o -name .git \) -prune \
	    -o -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache .mypy_cache
	rm -rf backend/.pytest_cache backend/.ruff_cache backend/.mypy_cache
	rm -rf backend/htmlcov backend/coverage.xml
	rm -f backend/.coverage backend/.coverage.*
	rm -rf htmlcov coverage.xml
	rm -f .coverage .coverage.*
	rm -rf frontend/dist frontend/coverage
