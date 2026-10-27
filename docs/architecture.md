# Architecture

ProbeAtlas is a **modular monolith control plane** plus **independently
deployable agents**, with PostgreSQL as the only stateful dependency.

| Process | Code | Responsibility |
| --- | --- | --- |
| `api` | `probeatlas.server.main` | Public REST API (`/v1`), agent protocol (`/agent/v1`), auth, tenant isolation |
| `worker` | `probeatlas.server.worker` | Scheduling, lease recovery, agent health, stale-state detection, retention; webhook delivery in a separate thread so slow receivers never delay scheduling |
| `agent` | `probeatlas.agent` | Enrollment, heartbeat, job polling, measurement, result submission |
| `web` | `web/` (Next.js) | Dashboard; same-origin proxy to the API using an httpOnly session cookie |
| `cli` | `probeatlas.cli` | API-key client with JSON output |

The API and worker share one package and image; they are separate processes so
the API can scale on request load and the worker on scheduling load. The agent
image installs only the `agent` extra (pydantic, httpx, dnspython); the Docker
build fails if server dependencies leak into it.

## Logical boundaries

```
probeatlas/
  common/          shared by agent and control plane
    schemas.py     versioned wire schema: JobSpec, MeasurementResult (schema_version 1)
    netpolicy.py   which destinations may be contacted (SSRF policy)
    safehttp.py    policy-enforcing HTTP client with phase timings
  agent/
    checks/        pure measurement functions: JobSpec -> MeasurementResult
    runner.py      loop: enroll, heartbeat, claim, execute (thread pool), submit, backoff
    client.py      HTTP client for /agent/v1
  server/
    api/           routers: auth & workspaces, api keys, monitors, measurements,
                   alerting (rules, incidents, channels), agents, overview
    deps.py        authentication and tenant resolution (Principal)
    targets.py     configuration-time target validation
    scheduling.py  schedule_due, claim_jobs, reap_jobs
    ingestion.py   result validation, idempotent persistence, monitor state
    alerting.py    per-region hysteresis, incidents, notification outbox
    notifications.py  webhook delivery (signed, retried)
    stats.py       query-time aggregation (percentiles, regional comparison, timeseries)
    housekeeping.py   agent health, no-data detection, retention
```

Adding a protocol means writing one function in `agent/checks/`, adding a
settings model in `server/schemas.py`, and adding the type to `CheckType`.
Scheduling, transport, storage, and alerting are protocol-agnostic.

## Measurement flow

```
 worker                    PostgreSQL                         agent (region R)
 ──────                    ──────────                         ────────────────
 schedule_due ──insert──▶ jobs(pending, region R,
   (due monitors,           unique monitor+region+slot)
    FOR UPDATE SKIP LOCKED)                         ◀──claim── POST /agent/v1/jobs/claim
                          jobs(claimed, agent, lease)  ──────▶ JobSpec[]
                                                               execute check (thread pool)
                          results (unique job_id)   ◀──submit─ POST /agent/v1/results
                          jobs(completed)                      (buffered + retried on failure)
                          monitors.state (latest per region)
                          alert_states / incidents / notification_deliveries (same transaction)
 deliver_pending ◀─outbox─
 reap_jobs: lease expired -> pending (retry) or expired(lease_timeout); never claimed -> expired(unclaimed)
```

1. **Scheduling.** Each monitor has `next_run_at`. The worker locks due monitors
   (`FOR UPDATE SKIP LOCKED`, so several workers can run), inserts one job per
   selected region with `ON CONFLICT DO NOTHING` on `(monitor_id, region,
   scheduled_for)`, and advances `next_run_at` by the interval. If the scheduler
   fell more than one interval behind, missed slots are skipped rather than
   replayed. Each job stores a snapshot of the target and settings
   (`config_version`), so editing a monitor never changes in-flight jobs.
2. **Dispatch.** Agents poll `POST /agent/v1/jobs/claim`. Only pending jobs for
   the agent's region and advertised capabilities, inside their validity window,
   are returned. Claiming sets a lease of `timeout + grace`.
3. **Execution.** Checks never run customer code or shell commands. Inputs are a
   fixed set of validated settings per type.
4. **Ingestion.** Tenant, monitor, region, and target come from the server-side
   job row, not from the agent. A result is accepted only from the agent that
   holds the job; `results.job_id` is unique, so retries are idempotent.
5. **Recovery.** Lost leases are retried up to `JOB_MAX_ATTEMPTS` while the job
   is still valid. Late results from the original agent are accepted (flagged
   `late`) within `LATE_RESULT_WINDOW_SECONDS`; results for reassigned jobs are
   rejected. Jobs that expire without a result are counted as **missing**.
6. **State and alerts.** Monitor state is derived from the most recent result
   per region in the last three intervals. Alert evaluation and the notification
   outbox are written in the ingestion transaction.

## Data model

* Identity: `users`, `user_sessions`, `workspaces`, `memberships`, `api_keys`.
* Configuration: `monitors`, `alert_rules`, `notification_channels`.
* Agents: `enrollment_tokens`, `agents` (platform-operated; location set by operator).
* Execution: `jobs`, `results` (raw, one row per job; aggregates are never written back).
* Alerting: `alert_states` (per rule and region), `incidents`, `incident_events`,
  `notification_deliveries` (outbox).
* `audit_events` for security-relevant actions.

Every tenant-owned table has `workspace_id`. Single-row access goes through
`tenant_get`, and list queries filter on `principal.workspace_id`.

### Result schema (v1) and units

`MeasurementResult` in `common/schemas.py` is the contract. Durations are
milliseconds, percentages 0–100, timestamps UTC. `measured_at` is set by the
agent when the measurement starts; `ingested_at` by the control plane. Fields
that do not apply or could not be measured are `null`, never zero.

`latency_ms` is the primary metric per type: HTTP total duration (all hops and
body read), TCP connect time, DNS resolution time, ICMP average RTT. HTTP phase
fields (`dns_ms`, `connect_ms`, `tls_ms`, `ttfb_ms`) describe the final hop;
`ttfb_ms` is from sending the request to receiving response headers.

## Aggregation rules

* Latency statistics use successful results only.
* Percentiles need at least 5 (P50), 20 (P95), or 100 (P99) samples; otherwise `null`.
* `success_rate = successes / results`. Expired jobs are reported separately as
  `missing` and are not counted as successes or failures.
* Regional assessment: a region is degraded below 95% success. `global` when all
  regions with data are degraded (and there is more than one), `regional` when
  only some are, `degraded` when the monitor has data from only one region.
  `slow_regions` lists regions whose P50 is over twice the median P50 of the others.

## Alerting

Rules: `unavailable` (any failed check), `http_status` (HTTP response outside the
expected codes; connection failures are ignored), `latency` (successful check
slower than the threshold; failed checks are ignored), `packet_loss` (ICMP loss
above the threshold). Each region fires after `consecutive_breaches` and
recovers after `recovery_successes`. An incident opens when at least
`min_regions` regions fire, records affected regions and scope
(`single_region`, `regional`, `global`), and resolves when fewer than
`min_regions` remain. A partial unique index allows only one open incident per
rule.

Notifications go to every enabled webhook in the workspace on `incident.opened`
and `incident.resolved`, through the outbox with exponential backoff (6 attempts).

## Trade-offs

* **PostgreSQL as the queue.** Job dispatch uses `SKIP LOCKED` rows instead of
  Redis or Kafka. This handles thousands of checks per minute on modest hardware
  and keeps one stateful system. Revisit when dispatch exceeds what one primary
  can handle.
* **Polling agents.** Agents poll every 2 s instead of holding a stream. Polling
  is simple, works through NAT and proxies, and the cost is bounded by agent count.
* **Query-time aggregates.** Percentiles are computed with `percentile_cont`
  over raw rows. Hourly rollups (with t-digest or histogram buckets) should be
  added before results reach tens of millions of rows per tenant.
* **Platform-operated agents.** Agents serve all tenants; there is no
  per-tenant private agent yet, which keeps the trust model simple.
* **In-process rate limiting.** Simple and dependency-free, but per replica.
