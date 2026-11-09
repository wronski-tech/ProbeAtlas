# Operations

## Processes and health

| Process | Health | Metrics |
| --- | --- | --- |
| api | `GET /healthz` (process), `GET /readyz` (database) | `:9101/metrics` (`PROBEATLAS_API_METRICS_PORT`) |
| worker | logs (`worker started`, task errors) | `:9100/metrics` (`PROBEATLAS_WORKER_METRICS_PORT`) |
| agent | heartbeat visible in `/v1/agents` (`status`, `last_heartbeat_at`) | structured logs |

Metrics listeners are separate from the public ports and meant for an internal scraper: keep 9100 and
9101 on the private network (compose publishes them on 127.0.0.1 only). The public API port does not
serve `/metrics`.

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
  -e PROBEATLAS_AGENT_NAMESERVERS=1.1.1.1,8.8.8.8 \
  probeatlas-agent:<version>
```

* The agent stores its identity in `PROBEATLAS_AGENT_STATE_FILE` (mode 0600).
  Keep the volume; after enrollment the token is no longer needed. If the
  identity is revoked the agent re-enrolls only if a valid enrollment token is
  still configured.
* Set `PROBEATLAS_AGENT_NAMESERVERS` to public recursive resolvers (for example
  `1.1.1.1,8.8.8.8`, or the provider's public resolver) so lookups never reach the
  hosting network's private or split-horizon zones. Leave it empty only where the
  host resolver is needed, such as the local compose demo, which relies on Docker DNS for
  `demo-target`.
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

The staging environment (AWS control plane, agents on Azure, AWS, GCP, and OVHcloud) is fully scripted:
see [staging.md](staging.md). The settings below apply to any deployment.

A small deployment needs: managed PostgreSQL 16, one or two API containers,
one worker container (more are safe), the web container, and agents in each
region. Put the API and web behind a TLS-terminating load balancer.

Production settings:

```
PROBEATLAS_ENV=production
PROBEATLAS_DATABASE_URL=postgresql+psycopg://…?sslmode=require
PROBEATLAS_ALLOW_PRIVATE_TARGETS=false            # default
PROBEATLAS_BOOTSTRAP_ENROLLMENT_TOKEN=             # unset (the API refuses to start with it in production)
PROBEATLAS_WEBHOOK_ALLOWED_PRIVATE_CIDRS=          # unset
PROBEATLAS_REGISTRATION=invite                     # default; customers are onboarded by invitation
PROBEATLAS_TRUSTED_PROXIES=<web server IPs>,<API load balancer CIDR>   # who may set X-Forwarded-For
PROBEATLAS_WEB_TRUSTED_PROXY_HOPS=1                # web container: proxies in front of it (e.g. the LB)
PROBEATLAS_WEBHOOK_NAMESERVERS=1.1.1.1,8.8.8.8     # public resolvers for webhook destinations
```

### Onboarding customers (invite-only sign-up)

With `PROBEATLAS_REGISTRATION=invite` (the default) nobody can create an account without an invitation.
Onboard a new customer with:

```bash
probeatlas-admin create-workspace --name "Acme" --owner-email ops@acme.example
# -> {"workspace_id": "...", "owner_email": "ops@acme.example", "invitation_token": "pai_...",
#     "invite_path": "/invite/pai_...", "expires_at": "..."}
```

Send `https://<dashboard>/invite/pai_...` to the customer. They register (or sign in) with that email
and become the workspace owner; they can then invite their own teammates from **Settings → Members**.
The link is single-use and expires after 7 days; run the command again to issue a new one.

### Outbound volume per destination

Each workspace may send at most `PROBEATLAS_MAX_CHECKS_PER_MINUTE_PER_DESTINATION` (default 120) checks
per minute to any one destination host, counted over its active monitors as regions × 60 / interval
(all check types, every path and port of that host). Creating, changing, or resuming a monitor beyond
that returns `403 limit_exceeded`. The budget is per workspace on purpose: a platform-wide budget would
let one tenant exhaust it for a host another customer needs to monitor, and would reveal what others
monitor. A user who owns several workspaces gets a budget in each.

### Client IP forwarding

Per-IP rate limits (login, registration, enrollment) and audit events need the real client address:

```
browser ─▶ load balancer ─(XFF: client)─▶ web ─(XFF: client)─▶ API
API client ─▶ load balancer ─(XFF: client)─▶ API
```

* **Web:** `PROBEATLAS_WEB_TRUSTED_PROXY_HOPS` is the number of proxies in front of the web server that
  append to `X-Forwarded-For`. The web server takes the entry that many positions from the right (entries
  further left come from the client and may be forged) and forwards only that IP to the API. With `0`
  (browsers connect directly, as in local compose) nothing is forwarded and all dashboard sign-ins share
  the web server's rate-limit bucket.
* **API:** `PROBEATLAS_TRUSTED_PROXIES` lists the web servers and the API load balancer. Forwarded headers
  from any other peer are ignored, so clients cannot pick a fresh rate-limit bucket by inventing an
  address. Never set it to `*` or to a range that includes agents or the internet.

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
- [x] Invite-only sign-up and a per-workspace check budget per destination host.
- [ ] Email verification, CAPTCHA, and per-email workspace caps before opening sign-up to everyone.
- [x] Internal security review; critical and high findings fixed ([security-review.md](security-review.md)).
- [x] Fix the internal review findings (all closed; see [security-review.md](security-review.md)).
- [ ] External penetration test (scope in [security-review.md](security-review.md#3-external-penetration-test-package)).
