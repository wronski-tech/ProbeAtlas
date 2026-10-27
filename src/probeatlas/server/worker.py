"""Background worker: scheduling, job recovery, agent health, notifications, retention.

All state lives in PostgreSQL, so restarting the worker loses nothing: due
monitors are picked up from ``next_run_at`` and in-flight jobs are recovered
by lease expiry. Several workers may run concurrently (row locks with
SKIP LOCKED and idempotent inserts prevent double work).
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from collections.abc import Callable

from prometheus_client import start_http_server
from sqlalchemy.orm import Session

from probeatlas.common.logs import log_fields, setup_logging

from . import metrics
from .config import get_settings
from .db import new_session
from .housekeeping import apply_retention, mark_no_data_monitors, mark_stale_agents
from .notifications import deliver_pending
from .scheduling import reap_jobs, schedule_due

log = logging.getLogger("probeatlas.worker")

RETENTION_EVERY_S = 3600


def run_task(name: str, fn: Callable[[Session], object]) -> None:
    start = time.perf_counter()
    with new_session() as db:
        try:
            fn(db)
        except Exception:
            db.rollback()
            metrics.WORKER_TASK_ERRORS.labels(name).inc()
            log.exception("worker task failed", extra=log_fields(task=name))
    metrics.WORKER_TASK_SECONDS.labels(name).observe(time.perf_counter() - start)


def tick() -> None:
    run_task("schedule", schedule_due)
    run_task("reap_jobs", reap_jobs)
    run_task("agent_health", mark_stale_agents)
    run_task("monitor_staleness", mark_no_data_monitors)
    run_task("notifications", deliver_pending)


def main() -> None:
    setup_logging("worker")
    settings = get_settings()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    if settings.worker_metrics_port:
        start_http_server(settings.worker_metrics_port)
    log.info("worker started", extra=log_fields(tick_s=settings.worker_tick_seconds,
                                                metrics_port=settings.worker_metrics_port))
    last_retention = 0.0
    while not stop.is_set():
        tick()
        if time.monotonic() - last_retention > RETENTION_EVERY_S:
            run_task("retention", apply_retention)
            last_retention = time.monotonic()
        stop.wait(settings.worker_tick_seconds)
    log.info("worker stopped")


if __name__ == "__main__":
    main()
