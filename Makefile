# Sourcing Desk developer commands. Run from the repository root.
PY      ?= .venv/bin/python
PORT    ?= 8000
export SOURCING_DB ?= $(CURDIR)/sourcing.db

.PHONY: help install api worker web serve test test-cli build contracts docker clean

help:            ## List commands
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

install:         ## Create .venv, install the backend (with tests) and frontend dependencies
	python3 -m venv .venv
	$(PY) -m pip install -q -e "./backend[test]"
	cd frontend && npm ci

api:             ## Run the FastAPI service on $(PORT)
	cd backend && ../$(PY) -m uvicorn app.main:app --host 127.0.0.1 --port $(PORT) --reload

worker:          ## Run the durable workflow worker (same SOURCING_DB as the API)
	cd backend && ../$(PY) -m app.workflow.worker

web:             ## Run the chat UI dev server (proxies /api to $(PORT))
	cd frontend && API_PORT=$(PORT) npm run dev

serve: build     ## Production-style: API + worker + built UI on one port
	cd backend && FRONTEND_DIST=$(CURDIR)/frontend/dist ../$(PY) -m app.workflow.worker & \
	cd backend && FRONTEND_DIST=$(CURDIR)/frontend/dist ../$(PY) -m uvicorn app.main:app --host 127.0.0.1 --port $(PORT)

test:            ## Backend test suite (offline: no keys, no network)
	cd backend && ../$(PY) -m pytest -q

test-cli:        ## Team CLI tests (uses uv and a separate .venv-cli)
	UV_PROJECT_ENVIRONMENT=.venv-cli uv run pytest -q tests

build:           ## Type-check and build the UI into frontend/dist
	cd frontend && npm run build

contracts:       ## Regenerate docs/api/openapi.json and the frontend API types
	cd backend && ../$(PY) ../scripts/export_openapi.py
	cd frontend && npm run generate:api

docker:          ## Build the deployable image
	docker build -t sourcing-desk .

clean:           ## Remove local databases and build output
	rm -rf frontend/dist sourcing.db sourcing.db-* sourcing.db.*
