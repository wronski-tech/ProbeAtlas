# AGENTS.md — guide for AI coding agents

ProbeAtlas is a multi-tenant SaaS that measures availability and latency of public endpoints from
operator-placed agents. Read this file before changing code. Human-oriented docs live in
[CONTRIBUTING.md](CONTRIBUTING.md) and [docs/](docs/README.md).

## Layout

| Path | What it is |
| --- | --- |
| `src/probeatlas/common/` | Shared by agent and control plane: `schemas.py` (wire schema v1), `netpolicy.py` (SSRF policy), `safehttp.py` (policy-enforcing HTTP client), `logs.py` (JSON logs with redaction) |
| `src/probeatlas/agent/` | Agent process: `runner.py` (loop), `client.py` (`/agent/v1` client), `checks/` (one pure function per protocol) |
| `src/probeatlas/server/` | Control plane: `api/` routers, `deps.py` (auth → `Principal`), `scheduling.py`, `ingestion.py`, `alerting.py`, `notifications.py`, `stats.py`, `housekeeping.py`, `worker.py`, `models.py`, `migrations/` |
| `src/probeatlas/cli/` | `probeatlas` CLI (JSON output) |
| `web/` | Next.js 16 dashboard; talks to the API only through `web/src/app/api/proxy` with an httpOnly session cookie |
| `tests/` | pytest against real PostgreSQL; local HTTP/DNS servers, never public endpoints |
| `deploy/` | Dockerfiles, demo target, `backup/pgbackup.sh` |
| `docs/` | Architecture, security, threat model and review, API, operations, backups, Terraform design |
| `scripts/check_docstrings.py` | Fails CI if any Python def/class/module or TS function/class lacks documentation |

## Commands

```bash
make venv                                  # .venv with all Python extras (uv)
sudo docker compose up -d db               # PostgreSQL for tests (orb: use sudo)
.venv/bin/pytest                           # backend tests (creates probeatlas_test)
make lint                                  # ruff (incl. pydocstyle) + documentation coverage
cd web && npm run typecheck && npm test && npm run build
make openapi                               # regenerate docs/openapi.json after any API or schema change
sudo docker compose up -d --build          # full stack: web :3000, api :8000, worker metrics :9100
make restore-drill COMPOSE="sudo docker compose"   # backup + restore verification
```

CI (`.github/workflows/ci.yml`) runs all of the above and fails if `docs/openapi.json` is stale or
`alembic check` finds model/migration drift.

## Invariants — do not break these

1. **Tenant isolation.** Every workspace-scoped endpoint depends on `get_principal` (or `require_owner`)
   and filters every query by `principal.workspace_id`. Fetch single rows with
   `tenant_get(db, Model, id, principal, "what")`, which returns 404 for other tenants' rows. Never
   return 403 for another tenant's resource (it leaks existence). Add a case to `tests/test_tenancy.py`
   for new tenant-owned resources.
2. **Agents never choose the tenant.** Ingestion takes workspace, monitor, region, and target from the
   server-side `jobs` row. Do not add tenant fields to `MeasurementResult`.
3. **All outbound connections go through the policy.** Agent checks and webhook delivery must use
   `netpolicy.resolve_checked` / `safehttp.connect_checked` / `safehttp.http_request`, connect to the
   validated IP (no second DNS lookup), and revalidate every redirect. Never call `socket.create_connection`,
   `httpx`, or `requests` on a customer-supplied destination. `http_request` enforces a hard overall
   deadline with a watchdog; keep it.
4. **No fabricated data.** A field that does not apply or was not measured is `null`, never `0`.
   Unsupported checks report `failure_category="unsupported"`. Missing results (expired jobs) are
   reported as `missing`, never as success. Seeded or fixture data must be labeled as such.
5. **Percentiles need samples**: P50 ≥ 5, P95 ≥ 20, P99 ≥ 100 successful samples (`stats.MIN_SAMPLES`);
   otherwise `null`.
6. **Units**: durations in ms (float), percentages 0–100, timestamps timezone-aware UTC.
   `measured_at` comes from the agent, `ingested_at` from the server.
7. **Idempotency**: one result per job (`results.job_id` unique), one job per
   `(monitor_id, region, scheduled_for)`, one open incident per rule, notification `dedupe_key`.
   Preserve these when touching scheduling, ingestion, or alerting.
8. **Secrets**: tokens are stored as SHA-256 hashes, passwords as scrypt; secrets are shown once. Never log
   tokens, Authorization headers, response bodies, or SQL parameters (`hide_parameters=True`). Use
   `log_fields(...)` for structured context; values are redacted.
9. **Wire compatibility**: `common/schemas.py` is versioned (`RESULT_SCHEMA_VERSION`,
   `AGENT_PROTOCOL_VERSION`). Add optional fields only; never rename or remove fields or
   `FailureCategory` values.
10. **Location honesty**: agent region/country/city/provider are operator-configured labels. Never infer
    them from IP addresses or claim coverage that does not exist.

## Documentation rules (enforced)

* Every Python module, class, and function — including private and nested ones — has a docstring
  (Google style, summary line ending with a period). Test functions named `test_*` are exempt.
* Every TypeScript `function` and `class` in `web/src` has a `/** ... */` JSDoc block directly above it.
* FastAPI endpoint docstrings and Pydantic model docstrings become the OpenAPI descriptions: write them
  for API users, then run `make openapi`.
* New Alembic revisions get docstrings from the template; replace the `TODO` in `upgrade()`.

## Common tasks

**Add a check type**: write `run_<type>(job, ctx) -> MeasurementResult` in `agent/checks/<type>.py` using
`failure(...)` for errors and the policy helpers for connections; register it in `checks/CHECKS` and
`detect_capabilities`; add the value to `CheckType`; add a settings model to `server/schemas.py`
(`SETTINGS_MODELS`) and validation in `server/targets.py`; extend the `monitor_check_type` constraint with a
migration; add tests with a local server in `tests/test_checks.py`; update the dashboard form
(`web/src/components/MonitorForm.tsx`) and `docs/api.md`.

**Add an endpoint**: put it in the router that owns the resource under `server/api/`, depend on
`get_principal`/`require_owner`, filter by workspace, write an audit event for security-relevant changes,
use `Page[...]` with `encode_cursor`/`decode_cursor` for lists, raise `APIError` for errors, add tests
(including cross-tenant access), and run `make openapi`.

**Change the schema**: edit `server/models.py`, then
`.venv/bin/alembic revision --autogenerate -m "describe change"` (needs a migrated DB in
`PROBEATLAS_DATABASE_URL`), review the generated file, add docstrings, and run `alembic check`.

## Testing conventions

* Tests use the fixtures in `tests/conftest.py`: `client`, `account`/`other_account` (two tenants),
  `agent_a`/`agent_b` (enrolled agents in `test-a`/`test-b`), `target_server` (local HTTP server),
  `cycle(...)` (schedule → claim → submit one result), `settings.set(...)` (temporary settings).
* Tables are truncated after each test. Do not depend on test order.
* Use controlled local servers (`TargetServer`, `FakeDNS`), never public endpoints.
* A bug fix gets a regression test that fails without the fix.

## Environment notes (Amp orbs)

* Docker runs as `docker.service`; use `sudo docker compose ...`. Host port 8081 is taken, so the demo
  target's control port is `127.0.0.1:18081`.
* Outbound TCP from orbs goes through an egress proxy: timings to public hosts are not realistic.
