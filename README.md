# ProbeAtlas

Developer-first monitoring of public internet infrastructure from distributed
measurement agents. ProbeAtlas measures availability and latency of HTTP(S),
DNS, TCP, and ICMP targets from operator-placed agents, compares results across
regions, and alerts when checks fail.

> **Status: MVP.** Everything described here runs locally and is covered by
> tests. ProbeAtlas does **not** yet operate a global agent network: the local
> environment runs two agents on one machine with the region labels
> `dev-local-a` and `dev-local-b`. They produce real measurements, but they do
> not represent geographically distinct vantage points.

## What it does

* **Monitors** for HTTP/HTTPS (status, DNS, connect, TLS, TTFB, and total time), DNS
  resolution, TCP connect, and ICMP ping (packet loss, RTT), each run from one
  or more selected regions.
* **Agents** that run independently, enroll with a token, heartbeat, claim jobs,
  measure, and submit results. Operators set each agent's location.
* **Regional comparison**: min/P50/P95/P99, success rate, missing data, and packet
  loss per region and agent, plus whether a problem is global or regional.
* **Alerting** with consecutive-failure and recovery thresholds per region,
  incidents with history, and signed webhook notifications.
* **Multi-tenant SaaS foundations**: users, workspaces, roles, hashed API keys
  with rotation, tenant isolation on every endpoint, audit log, and rate limits.
* **REST API** (`/v1`, OpenAPI at `/docs`), a **CLI** with JSON output, and a
  **dashboard** (Next.js).

## Architecture at a glance

```
            ┌──────────────┐     /api/proxy (session cookie)     ┌──────────────────────────┐
 browser ──▶│ web (Next.js)│ ──────────────────────────────────▶ │ api (FastAPI)            │
            └──────────────┘                                     │  auth, tenants, monitors │
 CLI / API clients ── Bearer pak_… ────────────────────────────▶ │  queries, agent protocol │
                                                                 └────────────┬─────────────┘
 ┌───────────────────┐   POST /agent/v1/jobs/claim, /results                  │
 │ agent (per region)│ ◀─────────────────────────────────────────────────────▶│
 │  http/dns/tcp/icmp│                                                        ▼
 └─────────┬─────────┘                                             ┌──────────────────────┐
           │ outbound checks (SSRF-guarded)                        │ PostgreSQL           │
           ▼                                                       │  config, jobs, raw   │
     customer targets                                              │  results, incidents  │
                                                                   └──────────▲───────────┘
                                                 ┌────────────────────────────┘
                                                 │ worker: schedule jobs, recover leases,
                                                 │ agent health, notifications, retention
```

Details: [docs/architecture.md](docs/architecture.md) · Security:
[docs/security.md](docs/security.md) · API guide: [docs/api.md](docs/api.md) ·
Operations: [docs/operations.md](docs/operations.md) · Terraform design:
[docs/terraform.md](docs/terraform.md)

## Quick start (Docker Compose)

Requirements: Docker with Compose v2. No paid services or external accounts.

```bash
docker compose up -d --build     # db, migrate, api, worker, web, agent-a, agent-b, demo-target
docker compose ps                # api should be "healthy", agents "running"
```

| Service | URL |
| --- | --- |
| Dashboard | http://localhost:3000 |
| API + OpenAPI UI | http://localhost:8000/docs |
| Worker metrics | http://localhost:9100/metrics |
| Demo target control | http://localhost:18081/control |

Stop with `docker compose down`; delete all data with `docker compose down -v`.

Migrations run automatically through the `migrate` service. To run them by
hand: `docker compose run --rm migrate` (or `probeatlas-admin migrate` with
`PROBEATLAS_DATABASE_URL` set).

### Demo walkthrough

1. Open http://localhost:3000, register, and you land in your workspace.
2. **New monitor** → type HTTP, URL `http://demo-target:8080/`, regions
   `dev-local-a` and `dev-local-b`, interval 10 s. Keep the default alert rule.
   (`demo-target` is a private container; it is accepted only because the
   compose file sets the development flag `PROBEATLAS_ALLOW_PRIVATE_TARGETS=true`
   and the agents allowlist only the demo network `10.203.10.0/24`.)
3. Within ~10 s the monitor shows real measurements from both agents.
4. Simulate a **regional** outage (only agent-a gets HTTP 503):
   `make demo-fail-regional`. After 3 failed checks the monitor becomes
   `degraded`, the regional comparison says *Regional*, and an incident opens
   for `dev-local-a`.
5. Simulate a **global** outage: `make demo-fail`. The incident's scope becomes
   `global` and the monitor goes `down`.
6. Recover: `make demo-ok`. After 2 successful checks per region the incident
   resolves.

To see the SSRF guard work, create an HTTP monitor for `http://api:8000/healthz`:
the control plane accepts the name in development mode, but the agent refuses
to connect and records `blocked_target` ("destination resolves to a private
address").

### API and CLI

Create an API key under **Settings → API keys** (owners only), then:

```bash
export PROBEATLAS_API_URL=http://localhost:8000
export PROBEATLAS_API_KEY=pak_...

curl -s -H "Authorization: Bearer $PROBEATLAS_API_KEY" $PROBEATLAS_API_URL/v1/monitors | jq

# CLI (install: pip install -e ".[cli]")
probeatlas login --api-key "$PROBEATLAS_API_KEY"
probeatlas locations
probeatlas monitors create --name api --type http --target https://example.com/ \
  --region dev-local-a --region dev-local-b --interval 30
probeatlas monitors list
probeatlas monitors status <monitor-id>
probeatlas measurements list --monitor <monitor-id> --limit 5
probeatlas measurements list --failed --since 2026-10-27T00:00:00Z
```

All CLI output is JSON (stdout); errors are JSON on stderr with exit code 1
(API error) or 2 (usage). More examples in [docs/api.md](docs/api.md).

## Development

```bash
make venv                        # .venv with server, agent, CLI and dev extras (uses uv)
docker compose up -d db          # PostgreSQL on localhost:5432
make test-backend                # pytest (creates database probeatlas_test)
make lint                        # ruff
cd web && npm install && npm run typecheck && npm test && npm run build
```

Run components outside Docker:

```bash
export PROBEATLAS_DATABASE_URL=postgresql+psycopg://probeatlas:probeatlas@localhost:5432/probeatlas
export PROBEATLAS_BOOTSTRAP_ENROLLMENT_TOKEN=pae_local-dev-only-enrollment-token
.venv/bin/probeatlas-admin migrate
.venv/bin/probeatlas-api                      # :8000
.venv/bin/probeatlas-worker                   # scheduler + housekeeping, metrics :9100
PROBEATLAS_AGENT_REGION=dev-laptop \
PROBEATLAS_AGENT_ENROLLMENT_TOKEN=$PROBEATLAS_BOOTSTRAP_ENROLLMENT_TOKEN \
  .venv/bin/probeatlas-agent
cd web && PROBEATLAS_API_URL=http://localhost:8000 npm run dev   # :3000
```

New database migration after changing `models.py`:
`.venv/bin/alembic revision --autogenerate -m "describe change"`, then review
the file in `src/probeatlas/server/migrations/versions/`.

Regenerate the published OpenAPI document: `make openapi` (writes
`docs/openapi.json`).

### Repository layout

```
src/probeatlas/common/   wire schema (v1), outbound network policy, safe HTTP client, logging
src/probeatlas/agent/    agent runner, control-plane client, checks/{http,dns,tcp,icmp}
src/probeatlas/server/   API routers, auth, scheduling, ingestion, alerting, stats, worker
src/probeatlas/cli/      CLI
web/                     Next.js dashboard
deploy/                  Dockerfiles and the demo target
tests/                   pytest suite (real PostgreSQL, local test servers)
docs/                    architecture, security, API, operations, Terraform design
```

## Configuration

Control plane (`PROBEATLAS_*`, see [src/probeatlas/server/config.py](src/probeatlas/server/config.py)
and [.env.example](.env.example)):

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | local postgres | SQLAlchemy URL (`postgresql+psycopg://…`) |
| `ENV` | `development` | `production` enables secure-cookie expectations and warnings |
| `ALLOW_REGISTRATION` | `true` | Self-service sign-up |
| `ALLOW_PRIVATE_TARGETS` | `false` | **Dev only.** Accept private hostnames as targets |
| `WEBHOOK_ALLOWED_PRIVATE_CIDRS` | empty | Private ranges webhooks may reach (dev/test) |
| `BOOTSTRAP_ENROLLMENT_TOKEN` | empty | Multi-use enrollment token for local agents |
| `DEFAULT_MAX_MONITORS` / `MIN_INTERVAL_SECONDS` | 25 / 30 | Per-workspace limits for new workspaces |
| `RATE_LIMIT_PER_MINUTE` / `AUTH_RATE_LIMIT_PER_MINUTE` | 300 / 20 | API rate limits |
| `AGENT_OFFLINE_AFTER_SECONDS` | 60 | Heartbeat staleness threshold |
| `JOB_MAX_ATTEMPTS` / `JOB_LEASE_GRACE_SECONDS` | 2 / 30 | Retry policy for lost jobs |
| `RESULT_RETENTION_DAYS` / `JOB_RETENTION_DAYS` | 30 / 7 | Retention |

Agent (`PROBEATLAS_AGENT_*` plus `PROBEATLAS_API_URL`): `REGION` (required),
`ENROLLMENT_TOKEN`, `NAME`, `COUNTRY_CODE`, `CITY`, `PROVIDER`, `STATE_FILE`,
`CONCURRENCY` (8), `POLL_INTERVAL` (2 s), `HEARTBEAT_INTERVAL` (15 s),
`ALLOWED_PRIVATE_CIDRS` (empty). See [docs/operations.md](docs/operations.md#running-an-agent).

## Current limitations

* No real multi-region network yet; the local agents share one host. Check
  timings measured from inside some sandboxes and corporate networks pass
  through egress proxies and do not reflect the real path (for example, TCP
  connect times near 0 ms to remote hosts).
* Rate limits are per API process (in memory).
* ICMP needs `net.ipv4.ping_group_range` to include the agent's group; otherwise
  the agent does not advertise ICMP and reports such jobs as `unsupported`.
* Aggregates are computed at query time from raw results (fine for MVP volumes;
  rollups are a documented next step).
* Agents are platform-operated; customer-hosted private agents are not supported.
* Email notifications, billing, SSO, status pages, and traceroute are out of scope.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `unknown monitoring region(s)` when creating a monitor | No agent with that region is enrolled. Check `docker compose logs agent-a` and **Agents**. |
| Monitor stays `unknown` | No results yet. Check agents are `online`, the region matches, and the worker is running (`docker compose logs worker`). Missing results are shown as **Missing**, never as success. |
| Every check fails with `blocked_target` | The destination resolves to a private, loopback, or metadata address. This is intentional; see [docs/security.md](docs/security.md). |
| ICMP checks report `unsupported` | Set the sysctl `net.ipv4.ping_group_range="0 2147483647"` for the agent (already set in compose). |
| Agent logs `credentials rejected; re-enrolling` | The agent was disabled or its state file belongs to another database; it re-enrolls if an enrollment token is configured. |
| Port already in use | Change the published port in `docker-compose.yml` (5432, 8000, 3000, 9100, 18081). |
| Tests fail to connect to PostgreSQL | `docker compose up -d db`, or set `PROBEATLAS_TEST_DATABASE_URL`. |
| HTTP 429 `rate_limited` | Wait for `Retry-After` seconds or raise `PROBEATLAS_RATE_LIMIT_PER_MINUTE`. |
