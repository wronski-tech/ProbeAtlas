# Contributing to ProbeAtlas

Thanks for working on ProbeAtlas. This guide covers the local setup, the checks every change must pass,
and the conventions reviewers look for. AI coding agents should also read [AGENTS.md](AGENTS.md), which
lists the invariants in more detail.

## Setup

Requirements: Docker with Compose v2, Python 3.11, [uv](https://docs.astral.sh/uv/), Node.js 22.

```bash
make venv                        # .venv with server, agent, CLI, and dev extras
docker compose up -d db          # PostgreSQL on 127.0.0.1:5432
cd web && npm ci && cd ..        # dashboard dependencies
```

Run the full stack with `docker compose up -d --build` (see the [README](README.md#quick-start-docker-compose)).

## Before you open a pull request

```bash
make lint          # ruff (style, bugs, security rules, docstrings) + documentation coverage
make test          # pytest against PostgreSQL + web typecheck and unit tests
make build-web     # production build of the dashboard
make openapi       # if you changed endpoints or request/response models
```

CI runs the same commands plus container builds, `alembic check`, and a check that `docs/openapi.json`
is current. A pull request must be green before review.

## Conventions

**Code**

* Keep the boundaries described in [docs/architecture.md](docs/architecture.md): checks are pure
  functions, scheduling and ingestion are protocol-agnostic, and routers own their resources.
* Every workspace-scoped query filters by the caller's workspace. Use `tenant_get` for single rows.
* Outbound connections to customer destinations only go through `netpolicy`/`safehttp`.
* Values that were not measured are `null`, never `0`.
* Prefer the standard library and existing dependencies. New infrastructure (queues, caches, analytical
  databases) needs a written justification in the pull request.

**Documentation**

* Every Python module, class, and function has a docstring, including private helpers and nested
  functions. Use Google style: a one-line summary ending with a period, then details if needed.
  `scripts/check_docstrings.py` and ruff's `D` rules enforce this.
* Every TypeScript function and class in `web/src` has a JSDoc comment.
* Endpoint and model docstrings appear in the public API reference; write them for API users.
* Update the relevant page in `docs/` in the same pull request as the behavior change.

**Tests**

* Bug fixes include a regression test that fails without the fix.
* New endpoints include a cross-tenant test (see `tests/test_tenancy.py`).
* Use the local test servers in `tests/conftest.py`; tests must not depend on the public internet.
* Assert observable behavior (API responses, stored rows), not internal call order.

**Database changes**

Edit `src/probeatlas/server/models.py`, then generate and review a migration:

```bash
export PROBEATLAS_DATABASE_URL=postgresql+psycopg://probeatlas:probeatlas@localhost:5432/probeatlas
.venv/bin/probeatlas-admin migrate
.venv/bin/alembic revision --autogenerate -m "describe change"
.venv/bin/alembic check
```

Migrations must be backward compatible with the previous release while both versions run during a
deploy (add columns as nullable or with defaults; drop columns in a later release).

**Commits**

Write commit messages in the imperative ("Add TCP check retries"). Keep refactors and behavior changes
in separate commits.

## Security issues

Do not open public issues for vulnerabilities. Email the maintainers and include reproduction steps.
The threat model and the current list of known findings are in
[docs/security-review.md](docs/security-review.md).
