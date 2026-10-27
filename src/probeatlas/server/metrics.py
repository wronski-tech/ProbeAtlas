"""Prometheus metrics. Each process (API, worker) exposes its own registry."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

JOBS_SCHEDULED = Counter("probeatlas_jobs_scheduled_total", "Jobs created by the scheduler")
JOBS_CLAIMED = Counter("probeatlas_jobs_claimed_total", "Jobs handed to agents")
JOBS_RETRIED = Counter("probeatlas_jobs_retried_total", "Jobs re-queued after a lease expired")
JOBS_EXPIRED = Counter("probeatlas_jobs_expired_total", "Jobs that expired without a result", ["reason"])
SCHEDULER_LAG = Histogram(
    "probeatlas_scheduler_lag_seconds",
    "Delay between a monitor's due time and job creation",
    buckets=(0.5, 1, 2, 5, 10, 30, 60, 300, 1800),
)
RESULTS_INGESTED = Counter("probeatlas_results_total", "Result submissions by outcome", ["status"])
FAILED_CHECKS = Counter("probeatlas_failed_checks_total", "Failed measurements", ["check_type", "category"])
INGEST_DELAY = Histogram(
    "probeatlas_result_ingest_delay_seconds",
    "Time between measurement and ingestion",
    buckets=(0.5, 1, 2, 5, 10, 30, 60, 300, 900),
)
AGENT_HEARTBEATS = Counter("probeatlas_agent_heartbeats_total", "Agent heartbeats received")
AGENTS = Gauge("probeatlas_agents", "Agents by status", ["status"])
AGENTS_MARKED_OFFLINE = Counter("probeatlas_agents_marked_offline_total", "Agents whose heartbeat went stale")
JOBS_PENDING = Gauge("probeatlas_jobs_pending", "Jobs waiting for an agent")
NOTIFICATIONS = Counter("probeatlas_notifications_total", "Notification delivery attempts", ["status"])
INCIDENTS_OPENED = Counter("probeatlas_incidents_opened_total", "Incidents opened")
WORKER_TASK_SECONDS = Histogram("probeatlas_worker_task_seconds", "Worker task duration", ["task"])
WORKER_TASK_ERRORS = Counter("probeatlas_worker_task_errors_total", "Worker task failures", ["task"])
