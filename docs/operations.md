# Operations

## Processes and health

| Process | Health | Metrics |
| --- | --- | --- |
| api | `GET /healthz` (process), `GET /readyz` (database) | `GET /metrics` |
| worker | logs (`worker started`, task errors) | `:9100/metrics` (`PROBEATLAS_WORKER_METRICS_PORT`) |
| agent | heartbeat visible in `/v1/agents` (`status`, `last_heartbeat_at`) | structured logs |

`/metrics` endpoints are meant for an internal scraper; do not expose them publicly.
The API serves `/metrics` on its main port, so block that path at the load balancer.

Key metrics:

* Scheduling: `probeatlas_jobs_scheduled_total`, `probeatlas_scheduler_lag_seconds`, `probeatlas_jobs_pending`
* Execution: `probeatlas_jobs_claimed_total`, `probeatlas_jobs_retried_total`, `probeatlas_jobs_expired_total{reason}`
* Ingestion: `probeatlas_results_total{status=accepted|duplicate|rejected}`, `probeatlas_result_ingest_delay_seconds`
* Checks: `probeatlas_failed_checks_total{check_type,category}`
* Agents: `probeatlas_agents{status}`, `probeatlas_agents_marked_offline_total`, `probeatlas_agent_heartbeats_total`
* Alerting: `probeatlas_incidents_opened_total`, `probeatlas_notifications_total{status=sent|retry|failed}`
* Worker: `probeatlas_worker_task_seconds{task}`, `probeatlas_worker_task_errors_total{task}`

Suggested alerts for operators: pending jobs growing, `jobs_expired_total{reason="unclaimed"}`
rate above zero for a region (no capacity), scheduler lag p95 above 10 s, any
`worker_task_errors_total`, notifications in `failed`.

All processes log JSON lines to stdout with `ts`, `level`, `service`, `logger`,
`msg`, and context fields (`job_id`, `agent_id`, `monitor_id`, …). Tokens are
redacted.

## Running an agent

Agents are independent processes. To add a location:

```bash
# On the control plane host: issue a single-use, region-bound token valid for 24 h
probeatlas-admin create-enrollment-token --region eu-central --max-uses 1 --expires-in-hours 24

# On the agent host
docker run -d --name probeatlas-agent --restart unless-stopped \
  --cap-drop ALL --read-only --security-opt no-new-privileges:true \
  --sysctl net.ipv4.ping_group_range="0 2147483647" \
  -v probeatlas-agent:/var/lib/probeatlas-agent \
  -e PROBEATLAS_API_URL=https://api.probeatlas.example \
  -e PROBEATLAS_AGENT_ENROLLMENT_TOKEN=pae_... \
  -e PROBEATLAS_AGENT_REGION=eu-central \
  -e PROBEATLAS_AGENT_NAME=fra-1 \
  -e PROBEATLAS_AGENT_COUNTRY_CODE=DE \
  -e PROBEATLAS_AGENT_CITY=Frankfurt \
  -e PROBEATLAS_AGENT_PROVIDER="Hetzner AS24940" \
  probeatlas-agent:<version>
```

* The agent stores its identity in `PROBEATLAS_AGENT_STATE_FILE` (mode 0600).
  Keep the volume; after enrollment the token is no longer needed. If the
  identity is revoked the agent re-enrolls only if a valid enrollment token is
  still configured.
* Location fields are operator assertions shown to customers. Do not infer them
  from IP geolocation.
* Agents need outbound HTTPS to the API and outbound access to targets; they
  need no inbound ports.
* Shutdown (SIGTERM) stops claiming, finishes in-flight checks (bounded by their
  timeouts), and flushes results for up to 30 s.
* Network failures are retried with exponential backoff (1 s up to 30 s, with
  jitter). Results are buffered in memory (up to 2000) while the API is unreachable.
* Disable a compromised agent: `probeatlas-admin disable-agent <id>`.

### ICMP

The agent uses unprivileged ICMP datagram sockets. They work when the
process's group is within `net.ipv4.ping_group_range` (set the sysctl on the
container or host). Without it, the agent does not advertise `icmp`, and any
ICMP job it receives is reported as `unsupported` with no latency values.
Raw sockets (`CAP_NET_RAW`) are deliberately not used.

## Job lifecycle and recovery

| Situation | Behavior |
| --- | --- |
| Worker restarts | Due monitors are picked up from `next_run_at`; slot uniqueness prevents duplicates. Missed slots older than one interval are skipped, not replayed. |
| Agent dies with claimed jobs | Lease (`timeout + 30 s`) expires; job returns to `pending` and another agent in the region can take it (up to `JOB_MAX_ATTEMPTS`, default 2). |
| No agent in a region | Jobs expire as `unclaimed` and show as **missing**. Monitor state goes `unknown` if no region reports for 3 intervals. |
| Late result after lease expiry | Accepted from the original agent within 10 min and flagged `late`; rejected if the job was reassigned. |
| Duplicate submission | Same `result_id`: `duplicate` ack, no new row. Different `result_id` for a finished job: rejected. |
| Agent heartbeat stops | After `AGENT_OFFLINE_AFTER_SECONDS` (60 s) the agent is marked `offline`; a heartbeat brings it back. |

## Retention

Applied hourly by the worker:

| Data | Default | Setting |
| --- | --- | --- |
| Raw results (successes **and** failures) | 30 days | `PROBEATLAS_RESULT_RETENTION_DAYS` |
| Finished jobs | 7 days | `PROBEATLAS_JOB_RETENTION_DAYS` |
| Audit events | 365 days | `PROBEATLAS_AUDIT_RETENTION_DAYS` |
| Sent/failed notification deliveries | 90 days | fixed |
| Expired sessions | at expiry | fixed |

Incidents and their events are kept until their monitor is deleted.

## Backups

See [backup-restore.md](backup-restore.md): `deploy/backup/pgbackup.sh backup|restore|drill`, the
recommended PITR + daily dump policy, and the production restore runbook.

## Database migrations

Migrations are Alembic revisions in `src/probeatlas/server/migrations/versions`.

```bash
probeatlas-admin migrate                       # apply (idempotent)
alembic revision --autogenerate -m "change"    # author (from repo root, venv active)
alembic check                                  # verify models and migrations agree
```

In compose, the `migrate` one-shot service runs before `api` and `worker` start.

## Deployment (initial production)

A small deployment needs: managed PostgreSQL 16, one or two API containers,
one worker container (more are safe), the web container, and agents in each
region. Put the API and web behind a TLS-terminating load balancer.

Production settings:

```
PROBEATLAS_ENV=production
PROBEATLAS_DATABASE_URL=postgresql+psycopg://…?sslmode=require
PROBEATLAS_ALLOW_PRIVATE_TARGETS=false            # default
PROBEATLAS_BOOTSTRAP_ENROLLMENT_TOKEN=             # unset; use probeatlas-admin tokens
PROBEATLAS_WEBHOOK_ALLOWED_PRIVATE_CIDRS=          # unset
PROBEATLAS_ALLOW_REGISTRATION=false                # until you open sign-ups
FORWARDED_ALLOW_IPS=<load balancer CIDR>           # so per-IP rate limits see real API clients
```

### Production readiness checklist (not yet done)

- [ ] Shared rate limiting (PostgreSQL or Redis) for multiple API replicas.
- [ ] Encrypt webhook signing secrets at rest (KMS envelope).
- [ ] Email verification, password reset, and session management UI.
- [ ] Hourly rollups for long-range charts, and partitioning `results` by time.
- [ ] Agent token rotation and mTLS or signed requests between agents and API.
- [ ] Per-tenant concurrency fairness in `claim_jobs`.
- [x] Backup/restore tooling and a verified restore drill (`deploy/backup/pgbackup.sh`, [backup-restore.md](backup-restore.md)).
- [ ] Enable PITR on the managed database and schedule daily dumps + monthly drills.
- [x] CI: lint, documentation coverage, tests on PostgreSQL, migration drift, OpenAPI drift, image builds.
- [ ] Load test (target: 10k checks/minute) and dashboards for the metrics above.
- [ ] Abuse controls for sign-up (email verification, CAPTCHA) and outbound
      volume per destination.
- [x] Internal security review; critical and high findings fixed ([security-review.md](security-review.md)).
- [ ] Fix the open review findings (M1 forwarded client IP, M2 internal metrics port, H2 DNS resolver, L2, L4, L7).
- [ ] External penetration test (scope in [security-review.md](security-review.md#3-external-penetration-test-package)).
