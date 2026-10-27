"""Result ingestion: validate, persist idempotently, update state, evaluate alerts.

Tenant, monitor, region, and target are taken from the server-side job record,
never from the agent payload, so an agent cannot write into another tenant.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from probeatlas.common.logs import log_fields, redact
from probeatlas.common.schemas import MeasurementResult, ResultAck

from . import metrics
from .alerting import evaluate_result
from .config import get_settings
from .models import Agent, Job, Monitor, Result

log = logging.getLogger("probeatlas.ingestion")

CLOCK_SKEW = timedelta(minutes=5)
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def sanitize_message(msg: str | None) -> str | None:
    """Strip control characters, redact secrets, and truncate an agent error message to 500 chars."""
    if not msg:
        return None
    return redact(_CONTROL_CHARS.sub(" ", msg))[:500]


def ingest_results(db: Session, agent: Agent, results: list[MeasurementResult]) -> list[ResultAck]:
    """Ingest a batch of results from ``agent``, committing each result independently.

    One bad result never blocks the rest of the batch. Returns one acknowledgement per result.
    """
    acks = []
    for r in results:
        try:
            ack = _ingest_one(db, agent, r)
            db.commit()
        except Exception:
            db.rollback()
            log.exception("result ingestion failed", extra=log_fields(job_id=str(r.job_id)))
            ack = ResultAck(job_id=r.job_id, result_id=r.result_id, status="rejected", reason="internal_error")
        metrics.RESULTS_INGESTED.labels(ack.status).inc()
        if ack.status == "rejected":
            log.warning("result rejected",
                        extra=log_fields(job_id=str(r.job_id), agent_id=str(agent.id), reason=ack.reason))
        acks.append(ack)
    return acks


def _ingest_one(db: Session, agent: Agent, r: MeasurementResult) -> ResultAck:
    """Validate and store one result, then update monitor state and evaluate alert rules.

    Rejects results for unknown jobs, jobs leased to another agent, cancelled jobs, mismatched check types,
    and implausible timestamps. A resubmitted result is acknowledged as ``duplicate``. Results that arrive
    after the lease (within ``late_result_window_seconds``) are stored with ``late=True``.
    """
    def reject(reason: str) -> ResultAck:
        """Build a ``rejected`` acknowledgement for this result with ``reason``."""
        return ResultAck(job_id=r.job_id, result_id=r.result_id, status="rejected", reason=reason)

    now = datetime.now(UTC)
    job = db.execute(select(Job).where(Job.id == r.job_id).with_for_update()).scalar_one_or_none()
    if job is None:
        return reject("unknown_job")
    existing = db.execute(select(Result.id).where(Result.job_id == job.id)).scalar_one_or_none()
    if existing is not None:
        if existing == r.result_id:
            return ResultAck(job_id=r.job_id, result_id=r.result_id, status="duplicate")
        return reject("job_already_has_result")
    if job.agent_id != agent.id:
        return reject("not_assigned_to_agent")

    late = False
    if job.status == "claimed":
        late = bool(job.lease_expires_at and now > job.lease_expires_at)
    elif job.status == "expired" and job.expire_reason == "lease_timeout":
        window = timedelta(seconds=get_settings().late_result_window_seconds)
        if job.lease_expires_at is None or now > job.lease_expires_at + window:
            return reject("job_expired")
        late = True
    else:
        return reject(f"job_{job.status}")

    if r.check_type.value != job.check_type:
        return reject("check_type_mismatch")
    if r.measured_at < job.scheduled_for - CLOCK_SKEW or r.measured_at > now + CLOCK_SKEW:
        return reject("implausible_measured_at")

    values = r.model_dump(exclude={"result_id", "target", "error_message"})
    values.update(
        id=r.result_id,
        check_type=job.check_type,
        workspace_id=job.workspace_id,
        monitor_id=job.monitor_id,
        agent_id=agent.id,
        region=job.region,
        target=job.payload["target"],
        error_message=sanitize_message(r.error_message),
        failure_category=r.failure_category.value if r.failure_category else None,
        ingested_at=now,
        late=late,
    )
    inserted = db.execute(pg_insert(Result).values(values).on_conflict_do_nothing()).rowcount
    if not inserted:
        return ResultAck(job_id=r.job_id, result_id=r.result_id, status="duplicate")
    job.status = "completed"
    job.completed_at = now

    metrics.INGEST_DELAY.observe(max((now - r.measured_at).total_seconds(), 0))
    if not r.success:
        metrics.FAILED_CHECKS.labels(job.check_type, values["failure_category"] or "unknown").inc()

    monitor = db.execute(select(Monitor).where(Monitor.id == job.monitor_id).with_for_update()).scalar_one()
    if monitor.last_result_at is None or r.measured_at > monitor.last_result_at:
        monitor.last_result_at = r.measured_at
    recompute_monitor_state(db, monitor, now)
    result_row = db.get(Result, r.result_id)
    evaluate_result(db, monitor, result_row)
    return ResultAck(job_id=r.job_id, result_id=r.result_id, status="accepted", reason="late" if late else None)


def recompute_monitor_state(db: Session, monitor: Monitor, now: datetime) -> None:
    """Derive up/degraded/down from the latest recent result in each region."""
    window_start = now - timedelta(seconds=max(monitor.interval_seconds * 3, 180))
    rows = db.execute(
        text(
            """
            SELECT DISTINCT ON (region) region, success, latency_ms
            FROM results
            WHERE monitor_id = :mid AND measured_at >= :since AND region = ANY(:regions)
            ORDER BY region, measured_at DESC
            """
        ),
        {"mid": monitor.id, "since": window_start, "regions": monitor.regions},
    ).all()
    if not rows:
        state = "unknown"
    else:
        failing = sum(1 for r in rows if not r.success)
        slow = monitor.latency_threshold_ms is not None and any(
            r.success and r.latency_ms is not None and r.latency_ms > monitor.latency_threshold_ms for r in rows
        )
        if failing == len(rows):
            state = "down"
        elif failing or slow:
            state = "degraded"
        else:
            state = "up"
    if state != monitor.state:
        log.info("monitor state changed",
                 extra=log_fields(monitor_id=str(monitor.id), old=monitor.state, new=state))
        monitor.state = state
        monitor.state_changed_at = now
