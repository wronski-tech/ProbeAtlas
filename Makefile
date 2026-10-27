.PHONY: up down reset logs venv migrate test test-backend test-web lint docs-check check build-web openapi backup restore restore-drill demo-fail demo-fail-regional demo-ok

COMPOSE ?= docker compose
PY := .venv/bin

up:            ## Build and start the full local stack
	$(COMPOSE) up -d --build

down:          ## Stop the stack (data is kept)
	$(COMPOSE) down

reset:         ## Stop the stack and delete all data volumes
	$(COMPOSE) down -v

logs:
	$(COMPOSE) logs -f api worker agent-a agent-b

venv:          ## Local Python environment for tests and the CLI
	uv venv -q .venv && uv pip install -q -p .venv -e ".[server,agent,cli,dev]"

migrate:       ## Apply migrations to PROBEATLAS_DATABASE_URL
	$(PY)/probeatlas-admin migrate

test: test-backend test-web

test-backend:  ## Needs PostgreSQL (e.g. `docker compose up -d db`)
	$(PY)/pytest

test-web:
	cd web && npm run typecheck && npm test

lint:          ## Ruff (style, bugs, security, pydocstyle) and documentation coverage
	$(PY)/ruff check src tests scripts
	$(MAKE) docs-check

docs-check:    ## Every Python module/class/function and TS function/class is documented
	$(PY)/python scripts/check_docstrings.py src tests web/src

check: lint test build-web  ## Everything CI runs, except container builds

build-web:
	cd web && npm run build

openapi:       ## Regenerate docs/openapi.json from the code
	$(PY)/python -c "import json; from probeatlas.server.main import app; print(json.dumps(app.openapi(), indent=2))" > docs/openapi.json

backup:        ## Dump the database to backups/ (PGURL=... for a remote database)
	COMPOSE="$(COMPOSE)" deploy/backup/pgbackup.sh backup backups

restore:       ## Restore FILE=backups/x.dump into a new database DB=probeatlas_restore
	COMPOSE="$(COMPOSE)" deploy/backup/pgbackup.sh restore $(FILE) $(or $(DB),probeatlas_restore)

restore-drill: ## Backup, restore into a scratch database, compare row counts, drop it
	COMPOSE="$(COMPOSE)" deploy/backup/pgbackup.sh drill backups

demo-fail:     ## Make the demo target return HTTP 503 to every agent
	curl -s -X POST localhost:18081/control -d '{"mode":"fail","status":503}'; echo

demo-fail-regional: ## Fail only for agent-a (region dev-local-a)
	curl -s -X POST localhost:18081/control -d '{"mode":"fail","status":503,"clients":["10.203.10.11"]}'; echo

demo-ok:       ## Restore the demo target
	curl -s -X POST localhost:18081/control -d '{"mode":"ok"}'; echo
