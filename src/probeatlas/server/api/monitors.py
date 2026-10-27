"""Monitor management and per-monitor analytics."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from probeatlas.common.schemas import CheckType

from .. import stats
from ..audit import audit
from ..config import Settings, get_settings
from ..db import get_db
from ..deps import Principal, get_principal, tenant_get
from ..errors import APIError
from ..models import Agent, AuditEvent, Incident, IncidentEvent, Monitor, Workspace
from ..scheduling import cancel_pending_jobs
from ..schemas import (
    MonitorCreate,
    MonitorEvent,
    MonitorOut,
    MonitorUpdate,
    Page,
    RegionalPerformance,
    Timeseries,
    decode_cursor,
    encode_cursor,
    validate_settings,
)
from ..targets import normalize_target

router = APIRouter(prefix="/v1/monitors", tags=["monitors"])


def _validate_regions(db: Session, regions: list[str], settings: Settings) -> None:
    if len(regions) > settings.max_regions_per_monitor:
        raise APIError(422, "validation_error", f"at most {settings.max_regions_per_monitor} regions per monitor")
    known = set(db.execute(select(Agent.region).where(Agent.status != "disabled").distinct()).scalars())
    unknown = sorted(set(regions) - known)
    if unknown:
        raise APIError(422, "validation_error", "unknown monitoring region(s)",
                       [{"loc": ["body", "regions"], "msg": f"unknown regions: {', '.join(unknown)}",
                         "type": "value_error", "available": sorted(known)}])


def _validate_limits(ws: Workspace, interval: int, timeout_ms: int, settings: Settings) -> None:
    if interval < ws.min_interval_seconds:
        raise APIError(422, "validation_error",
                       f"interval_seconds must be at least {ws.min_interval_seconds} for this workspace")
    if timeout_ms > settings.max_timeout_ms:
        raise APIError(422, "validation_error", f"timeout_ms may not exceed {settings.max_timeout_ms}")
    if timeout_ms > interval * 1000:
        raise APIError(422, "validation_error", "timeout_ms must not exceed the check interval")


@router.get("", response_model=Page[MonitorOut])
def list_monitors(
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = None,
    state: str | None = Query(None, pattern="^(unknown|up|degraded|down|paused)$"),
    type: CheckType | None = None,  # noqa: A002
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    q = select(Monitor).where(Monitor.workspace_id == p.workspace_id)
    if state == "paused":
        q = q.where(Monitor.is_paused.is_(True))
    elif state:
        q = q.where(Monitor.state == state, Monitor.is_paused.is_(False))
    if type:
        q = q.where(Monitor.check_type == type.value)
    if cursor:
        ts, mid = decode_cursor(cursor)
        c_ts, c_id = datetime.fromisoformat(ts), uuid.UUID(mid)
        q = q.where(or_(Monitor.created_at < c_ts, and_(Monitor.created_at == c_ts, Monitor.id < c_id)))
    rows = db.execute(q.order_by(Monitor.created_at.desc(), Monitor.id.desc()).limit(limit + 1)).scalars().all()
    nxt = encode_cursor(rows[limit - 1].created_at, rows[limit - 1].id) if len(rows) > limit else None
    return Page(items=[MonitorOut.of(m) for m in rows[:limit]], next_cursor=nxt)


@router.post("", response_model=MonitorOut, status_code=201)
def create_monitor(body: MonitorCreate, p: Principal = Depends(get_principal), db: Session = Depends(get_db),
                   settings: Settings = Depends(get_settings)):
    ws = db.get(Workspace, p.workspace_id)
    count = db.execute(select(func.count()).select_from(Monitor).where(Monitor.workspace_id == ws.id)).scalar_one()
    if count >= ws.max_monitors:
        raise APIError(403, "limit_exceeded", f"workspace monitor limit reached ({ws.max_monitors})")
    body.settings = validate_settings(body.type, body.settings)
    _validate_limits(ws, body.interval_seconds, body.timeout_ms, settings)
    _validate_regions(db, body.regions, settings)
    target = normalize_target(body.type, body.target, body.settings, settings.target_policy())
    now = datetime.now(UTC)
    m = Monitor(
        workspace_id=p.workspace_id, name=body.name, check_type=body.type.value, target=target,
        settings=body.settings, interval_seconds=body.interval_seconds, timeout_ms=body.timeout_ms,
        latency_threshold_ms=body.latency_threshold_ms, regions=body.regions, is_paused=body.paused,
        state="unknown", next_run_at=now, created_at=now, updated_at=now,
    )
    db.add(m)
    db.flush()
    audit(db, "monitor.created", workspace_id=p.workspace_id, actor_type=p.actor_type, actor_id=p.actor_id,
          target_type="monitor", target_id=m.id, ip=p.ip, details={"type": m.check_type, "target": target})
    db.commit()
    return MonitorOut.of(m)


@router.get("/{monitor_id}", response_model=MonitorOut)
def get_monitor(monitor_id: uuid.UUID, p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return MonitorOut.of(tenant_get(db, Monitor, monitor_id, p, "monitor"))


@router.patch("/{monitor_id}", response_model=MonitorOut)
def update_monitor(monitor_id: uuid.UUID, body: MonitorUpdate, p: Principal = Depends(get_principal),
                   db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    m = tenant_get(db, Monitor, monitor_id, p, "monitor")
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        return MonitorOut.of(m)
    ws = db.get(Workspace, p.workspace_id)
    check_type = CheckType(m.check_type)
    new_settings = m.settings
    if "settings" in changes:
        new_settings = validate_settings(check_type, changes["settings"] or {})
    _validate_limits(ws, changes.get("interval_seconds") or m.interval_seconds,
                     changes.get("timeout_ms") or m.timeout_ms, settings)
    if changes.get("regions"):
        _validate_regions(db, changes["regions"], settings)
    if "target" in changes or "settings" in changes:
        m.target = normalize_target(check_type, changes.get("target") or m.target, new_settings,
                                    settings.target_policy())
    m.settings = new_settings
    for field in ("name", "interval_seconds", "timeout_ms", "regions"):
        if changes.get(field) is not None:
            setattr(m, field, changes[field])
    if "latency_threshold_ms" in changes:
        m.latency_threshold_ms = changes["latency_threshold_ms"]
    m.config_version += 1
    m.updated_at = datetime.now(UTC)
    audit(db, "monitor.updated", workspace_id=p.workspace_id, actor_type=p.actor_type, actor_id=p.actor_id,
          target_type="monitor", target_id=m.id, ip=p.ip, details={"fields": sorted(changes)})
    db.commit()
    return MonitorOut.of(m)


@router.delete("/{monitor_id}", status_code=204)
def delete_monitor(monitor_id: uuid.UUID, p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """Delete the monitor together with its jobs, results, rules, and incidents."""
    m = tenant_get(db, Monitor, monitor_id, p, "monitor")
    audit(db, "monitor.deleted", workspace_id=p.workspace_id, actor_type=p.actor_type, actor_id=p.actor_id,
          target_type="monitor", target_id=m.id, ip=p.ip, details={"name": m.name, "target": m.target})
    db.delete(m)
    db.commit()


def _set_paused(monitor_id: uuid.UUID, paused: bool, p: Principal, db: Session) -> MonitorOut:
    m = tenant_get(db, Monitor, monitor_id, p, "monitor")
    if m.is_paused != paused:
        m.is_paused = paused
        m.updated_at = datetime.now(UTC)
        if paused:
            cancel_pending_jobs(db, m.id)
        else:
            m.next_run_at = datetime.now(UTC)
            m.state, m.state_changed_at = "unknown", datetime.now(UTC)
        audit(db, "monitor.paused" if paused else "monitor.resumed", workspace_id=p.workspace_id,
              actor_type=p.actor_type, actor_id=p.actor_id, target_type="monitor", target_id=m.id, ip=p.ip)
        db.commit()
    return MonitorOut.of(m)


@router.post("/{monitor_id}/pause", response_model=MonitorOut)
def pause_monitor(monitor_id: uuid.UUID, p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return _set_paused(monitor_id, True, p, db)


@router.post("/{monitor_id}/resume", response_model=MonitorOut)
def resume_monitor(monitor_id: uuid.UUID, p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return _set_paused(monitor_id, False, p, db)


@router.get("/{monitor_id}/regional-performance", response_model=RegionalPerformance)
def regional_performance(
    monitor_id: uuid.UUID,
    since: datetime | None = None,
    until: datetime | None = None,
    region: str | None = None,
    agent_id: uuid.UUID | None = None,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    """Compare a monitor across regions and agents over a time window (default: last 24h)."""
    m = tenant_get(db, Monitor, monitor_id, p, "monitor")
    since, until = stats.parse_window(since, until)
    return stats.regional_performance(db, m.id, m.regions, since, until, region=region, agent_id=agent_id)


@router.get("/{monitor_id}/timeseries", response_model=Timeseries)
def monitor_timeseries(
    monitor_id: uuid.UUID,
    since: datetime | None = None,
    until: datetime | None = None,
    group_by: Literal["region", "agent", "none"] = "region",
    region: str | None = None,
    agent_id: uuid.UUID | None = None,
    bucket_seconds: int | None = Query(None, ge=60, le=86_400),
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    m = tenant_get(db, Monitor, monitor_id, p, "monitor")
    since, until = stats.parse_window(since, until)
    return stats.timeseries(db, m.id, since, until, group_by, region=region, agent_id=agent_id,
                            bucket_seconds=bucket_seconds)


@router.get("/{monitor_id}/events", response_model=list[MonitorEvent])
def monitor_events(monitor_id: uuid.UUID, limit: int = Query(50, ge=1, le=200),
                   p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """Incident events and configuration changes for this monitor, newest first."""
    m = tenant_get(db, Monitor, monitor_id, p, "monitor")
    inc = db.execute(
        select(IncidentEvent).join(Incident, Incident.id == IncidentEvent.incident_id)
        .where(Incident.monitor_id == m.id, Incident.workspace_id == p.workspace_id)
        .order_by(IncidentEvent.created_at.desc()).limit(limit)
    ).scalars().all()
    aud = db.execute(
        select(AuditEvent).where(AuditEvent.workspace_id == p.workspace_id, AuditEvent.target_type == "monitor",
                                 AuditEvent.target_id == str(m.id))
        .order_by(AuditEvent.created_at.desc()).limit(limit)
    ).scalars().all()
    events = [MonitorEvent(at=e.created_at, source="incident", kind=e.kind, message=e.message) for e in inc]
    events += [MonitorEvent(at=a.created_at, source="audit", kind=a.action, message=a.action.replace(".", " "))
               for a in aud]
    return sorted(events, key=lambda e: e.at, reverse=True)[:limit]
