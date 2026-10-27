"""Workspace dashboard summary and audit log."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from .. import stats
from ..db import get_db
from ..deps import Principal, get_principal, require_owner
from ..models import Agent, AuditEvent, Incident, Monitor, Result
from ..schemas import AuditEventOut, IncidentOut, MeasurementOut, MonitorOut, Overview

router = APIRouter(prefix="/v1", tags=["overview"])


@router.get("/overview", response_model=Overview)
def overview(window_hours: int = Query(24, ge=1, le=24 * 30), p: Principal = Depends(get_principal),
             db: Session = Depends(get_db)):
    """Everything the main dashboard shows, in one request.

    ``latency`` and per-region latency cover HTTP checks only, so that DNS,
    TCP, and ICMP timings are not mixed into one distribution.
    """
    since = datetime.now(UTC) - timedelta(hours=window_hours)
    ws = p.workspace_id
    monitors = db.execute(select(Monitor).where(Monitor.workspace_id == ws)).scalars().all()
    counts = {"total": len(monitors), "up": 0, "degraded": 0, "down": 0, "unknown": 0, "paused": 0}
    for m in monitors:
        counts["paused" if m.is_paused else m.state] += 1
    failing = [MonitorOut.of(m) for m in monitors if not m.is_paused and m.state in ("down", "degraded")]

    open_incidents = db.execute(select(func.count()).select_from(Incident).where(
        Incident.workspace_id == ws, Incident.status == "open")).scalar_one()
    recent_incidents = db.execute(select(Incident).where(Incident.workspace_id == ws)
                                  .order_by(Incident.opened_at.desc()).limit(10)).scalars().all()

    params = {"ws": ws, "since": since}
    totals = db.execute(text(
        "SELECT count(*) AS total, count(*) FILTER (WHERE success) AS ok FROM results "
        "WHERE workspace_id = :ws AND measured_at >= :since"), params).one()
    http_only = "success AND check_type = 'http'"
    http_row = db.execute(text(
        f"SELECT {stats.agg_columns(http_only)} FROM results WHERE workspace_id = :ws AND measured_at >= :since"),
        params).one()
    region_rows = db.execute(text(
        f"SELECT region, {stats.agg_columns(http_only)} FROM results "
        "WHERE workspace_id = :ws AND measured_at >= :since GROUP BY region ORDER BY region"), params).all()
    recent = db.execute(select(Result).where(Result.workspace_id == ws).order_by(Result.measured_at.desc())
                        .limit(15)).scalars().all()
    agent_counts = {"online": 0, "offline": 0}
    for status, n in db.execute(select(Agent.status, func.count()).where(Agent.status != "disabled")
                                .group_by(Agent.status)).all():
        agent_counts[status] = n

    return Overview(
        window_hours=window_hours,
        monitors=counts,
        failing_monitors=failing,
        open_incidents=open_incidents,
        recent_incidents=[IncidentOut.model_validate(i) for i in recent_incidents],
        availability_pct=round(totals.ok / totals.total * 100, 3) if totals.total else None,
        latency=stats.latency_stats(http_row),
        recent_measurements=[MeasurementOut.model_validate(r) for r in recent],
        agents=agent_counts,
        regions=[stats.group_performance(r, r.region, r.region, 0) for r in region_rows],
    )


@router.get("/audit-events", response_model=list[AuditEventOut])
def audit_events(limit: int = Query(100, ge=1, le=500), p: Principal = Depends(require_owner),
                 db: Session = Depends(get_db)):
    """List the workspace audit log, newest first (owner only)."""
    return db.execute(select(AuditEvent).where(AuditEvent.workspace_id == p.workspace_id)
                      .order_by(AuditEvent.created_at.desc()).limit(limit)).scalars().all()
