"""Agent health, stale monitor state, and data retention."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.orm import Session

from probeatlas.common.logs import log_fields

from . import metrics
from .config import get_settings
from .models import Agent, AuditEvent, Job, NotificationDelivery, Result, UserSession

log = logging.getLogger("probeatlas.housekeeping")


def mark_stale_agents(db: Session, now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(seconds=get_settings().agent_offline_after_seconds)
    stale = db.execute(
        update(Agent)
        .where(Agent.status == "online", Agent.last_heartbeat_at < cutoff)
        .values(status="offline")
        .returning(Agent.id, Agent.name, Agent.region)
    ).all()
    for agent_id, name, region in stale:
        log.warning("agent heartbeat expired", extra=log_fields(agent_id=str(agent_id), name=name, region=region))
    db.commit()
    metrics.AGENTS_MARKED_OFFLINE.inc(len(stale))
    for status, n in db.execute(select(Agent.status, func.count()).group_by(Agent.status)).all():
        metrics.AGENTS.labels(status).set(n)
    return len(stale)


def mark_no_data_monitors(db: Session, now: datetime | None = None) -> int:
    """Monitors without recent results go to ``unknown``; silence is not success."""
    now = now or datetime.now(UTC)
    n = db.execute(
        text(
            """
            UPDATE monitors SET state = 'unknown', state_changed_at = :now
            WHERE NOT is_paused AND state <> 'unknown'
              AND (last_result_at IS NULL
                   OR last_result_at < :now - make_interval(secs => GREATEST(interval_seconds * 3, 180) + 60))
            """
        ),
        {"now": now},
    ).rowcount
    db.commit()
    if n:
        log.info("monitors marked unknown (no recent data)", extra=log_fields(count=n))
    return n


def apply_retention(db: Session, now: datetime | None = None) -> dict[str, int]:
    s = get_settings()
    now = now or datetime.now(UTC)
    out = {
        "results": db.execute(
            delete(Result).where(Result.measured_at < now - timedelta(days=s.result_retention_days))
        ).rowcount,
        "jobs": db.execute(
            delete(Job).where(Job.status.in_(("completed", "expired", "cancelled")),
                              Job.scheduled_for < now - timedelta(days=s.job_retention_days))
        ).rowcount,
        "deliveries": db.execute(
            delete(NotificationDelivery).where(NotificationDelivery.status != "pending",
                                               NotificationDelivery.created_at < now - timedelta(days=90))
        ).rowcount,
        "sessions": db.execute(delete(UserSession).where(UserSession.expires_at < now)).rowcount,
        "audit_events": db.execute(
            delete(AuditEvent).where(AuditEvent.created_at < now - timedelta(days=s.audit_retention_days))
        ).rowcount,
    }
    db.commit()
    if any(out.values()):
        log.info("retention applied", extra=log_fields(**out))
    return out
