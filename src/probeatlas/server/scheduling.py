"""Scheduling and job dispatch, backed by PostgreSQL.

* ``schedule_due`` turns due monitors into one job per selected region. The
  unique ``(monitor_id, region, scheduled_for)`` slot makes scheduling
  idempotent across scheduler restarts and concurrent schedulers.
* ``claim_jobs`` hands pending jobs to an agent in the job's region with a
  lease. ``FOR UPDATE SKIP LOCKED`` lets many agents claim concurrently
  without double-dispatching a job.
* ``reap_jobs`` requeues jobs whose lease expired (agent died or lost
  connectivity) and expires jobs that can no longer be run on time.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from probeatlas.common.logs import log_fields
from probeatlas.common.schemas import JobSpec

from . import metrics
from .config import get_settings
from .models import Agent, Job, Monitor

log = logging.getLogger("probeatlas.scheduler")


def utcnow() -> datetime:
    """Return the current UTC time."""
    return datetime.now(UTC)


def schedule_due(db: Session, now: datetime | None = None, batch: int = 500) -> int:
    """Create jobs for every unpaused monitor whose ``next_run_at`` has passed.

    One job is created per selected region. If the scheduler is more than one interval behind, missed slots
    are skipped rather than replayed. Safe to run concurrently and after restarts. Returns the number of jobs
    created.
    """
    now = now or utcnow()
    monitors = (
        db.execute(
            select(Monitor)
            .where(Monitor.is_paused.is_(False), Monitor.next_run_at <= now)
            .order_by(Monitor.next_run_at)
            .limit(batch)
            .with_for_update(skip_locked=True)
        )
        .scalars()
        .all()
    )
    created = 0
    for m in monitors:
        lag = (now - m.next_run_at).total_seconds()
        metrics.SCHEDULER_LAG.observe(max(lag, 0))
        # If we fell more than one interval behind (e.g. scheduler downtime),
        # skip the missed slots instead of flooding agents with a backlog.
        slot = (m.next_run_at if lag < m.interval_seconds else now).replace(microsecond=0)
        expires = slot + timedelta(seconds=max(m.interval_seconds, 30))
        payload = {
            "target": m.target,
            "settings": m.settings,
            "timeout_ms": m.timeout_ms,
            "config_version": m.config_version,
        }
        rows = [
            {
                "id": uuid.uuid4(),
                "workspace_id": m.workspace_id,
                "monitor_id": m.id,
                "region": region,
                "check_type": m.check_type,
                "payload": payload,
                "scheduled_for": slot,
                "expires_at": expires,
                "status": "pending",
                "attempts": 0,
            }
            for region in m.regions
        ]
        if rows:
            inserted = db.execute(
                pg_insert(Job).values(rows).on_conflict_do_nothing(constraint="uq_job_slot").returning(Job.id)
            ).all()
            created += len(inserted)
        db.execute(
            update(Monitor)
            .where(Monitor.id == m.id)
            .values(next_run_at=slot + timedelta(seconds=m.interval_seconds))
        )
        if lag > m.interval_seconds:
            log.warning("scheduler behind; skipped missed slots",
                        extra=log_fields(monitor_id=str(m.id), lag_s=round(lag, 1)))
    db.commit()
    metrics.JOBS_SCHEDULED.inc(created)
    return created


def claim_jobs(db: Session, agent: Agent, max_jobs: int, now: datetime | None = None) -> list[JobSpec]:
    """Lease up to ``max_jobs`` pending jobs to ``agent``.

    Only jobs in the agent's region, of a check type it supports, that are due and not expired are eligible.
    The lease lasts the job timeout plus ``job_lease_grace_seconds``.
    """
    now = now or utcnow()
    grace = get_settings().job_lease_grace_seconds
    jobs = (
        db.execute(
            select(Job)
            .where(
                Job.status == "pending",
                Job.region == agent.region,
                Job.check_type.in_(agent.capabilities),
                Job.scheduled_for <= now,
                Job.expires_at > now,
            )
            .order_by(Job.scheduled_for)
            .limit(max_jobs)
            .with_for_update(skip_locked=True)
        )
        .scalars()
        .all()
    )
    specs = []
    for job in jobs:
        timeout_ms = job.payload["timeout_ms"]
        job.status = "claimed"
        job.agent_id = agent.id
        job.attempts += 1
        job.claimed_at = now
        job.lease_expires_at = now + timedelta(milliseconds=timeout_ms, seconds=grace)
        specs.append(
            JobSpec(
                job_id=job.id,
                monitor_id=job.monitor_id,
                check_type=job.check_type,
                target=job.payload["target"],
                timeout_ms=timeout_ms,
                settings=job.payload["settings"],
                scheduled_for=job.scheduled_for,
                lease_expires_at=job.lease_expires_at,
            )
        )
    db.commit()
    metrics.JOBS_CLAIMED.inc(len(specs))
    return specs


def reap_jobs(db: Session, now: datetime | None = None) -> dict[str, int]:
    """Recover jobs whose lease expired and expire jobs that can no longer run.

    Expired leases go back to ``pending`` while attempts remain and the job is still valid; otherwise they
    become ``expired`` with reason ``lease_timeout`` (a late result may still be accepted). Pending jobs
    past ``expires_at`` become ``expired`` with reason ``unclaimed``, which the API reports as missing data.
    """
    now = now or utcnow()
    max_attempts = get_settings().job_max_attempts
    lease_expired = and_(Job.status == "claimed", Job.lease_expires_at < now)
    retried = db.execute(
        update(Job)
        .where(lease_expired, Job.attempts < max_attempts, Job.expires_at > now)
        .values(status="pending", agent_id=None, claimed_at=None, lease_expires_at=None)
    ).rowcount
    # Keep agent_id so a late result from the same agent can still be accepted.
    lease_timeout = db.execute(
        update(Job).where(lease_expired).values(status="expired", expire_reason="lease_timeout")
    ).rowcount
    unclaimed = db.execute(
        update(Job)
        .where(Job.status == "pending", Job.expires_at <= now)
        .values(status="expired", expire_reason="unclaimed")
    ).rowcount
    db.commit()
    metrics.JOBS_RETRIED.inc(retried)
    metrics.JOBS_EXPIRED.labels("lease_timeout").inc(lease_timeout)
    metrics.JOBS_EXPIRED.labels("unclaimed").inc(unclaimed)
    if retried or lease_timeout or unclaimed:
        log.info("reaped jobs", extra=log_fields(retried=retried, lease_timeout=lease_timeout, unclaimed=unclaimed))
    pending = db.execute(select(func.count()).select_from(Job).where(Job.status == "pending")).scalar_one()
    metrics.JOBS_PENDING.set(pending)
    return {"retried": retried, "lease_timeout": lease_timeout, "unclaimed": unclaimed}


def cancel_pending_jobs(db: Session, monitor_id: uuid.UUID) -> None:
    """Cancel a monitor's not-yet-claimed jobs (on pause, edit, or delete). Caller commits."""
    db.execute(
        update(Job)
        .where(Job.monitor_id == monitor_id, Job.status == "pending")
        .values(status="cancelled")
    )
