"""Alert evaluation with per-region hysteresis.

For every enabled rule on a monitor, each region keeps streak counters:

* a region starts *firing* after ``consecutive_breaches`` breaching results,
* and stops firing after ``recovery_successes`` non-breaching results.

An incident is open while at least ``min_regions`` regions are firing. At most
one incident can be open per rule (enforced by a partial unique index and a
row lock on the rule), so repeated or duplicate results never create duplicate
incidents. Results that do not apply to a rule (e.g. a latency rule on a failed
check) leave its counters untouched: missing data is not treated as healthy.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from probeatlas.common.logs import log_fields

from . import metrics
from .models import AlertRule, AlertState, Incident, IncidentEvent, Monitor, NotificationChannel, NotificationDelivery
from .models import Result as ResultRow

log = logging.getLogger("probeatlas.alerting")

KIND_LABEL = {
    "unavailable": "checks failing",
    "http_status": "unexpected HTTP status",
    "latency": "latency above threshold",
    "packet_loss": "packet loss above threshold",
}


def verdict(rule: AlertRule, r: ResultRow) -> bool | None:
    """True = breach, False = healthy, None = result does not apply to this rule."""
    if rule.kind == "unavailable":
        return not r.success
    if rule.kind == "http_status":
        if r.http_status is None:
            return None
        return r.failure_category == "http_status"
    if rule.kind == "latency":
        if not r.success or r.latency_ms is None or rule.threshold is None:
            return None
        return r.latency_ms > rule.threshold
    if rule.kind == "packet_loss":
        if r.packet_loss_pct is None or rule.threshold is None:
            return None
        return r.packet_loss_pct > rule.threshold
    return None


def evaluate_result(db: Session, monitor: Monitor, r: ResultRow) -> None:
    rules = db.execute(
        select(AlertRule).where(AlertRule.monitor_id == monitor.id, AlertRule.enabled.is_(True))
    ).scalars().all()
    for rule in rules:
        v = verdict(rule, r)
        if v is None:
            continue
        db.execute(
            pg_insert(AlertState).values(rule_id=rule.id, region=r.region).on_conflict_do_nothing()
        )
        state = db.execute(
            select(AlertState).where(AlertState.rule_id == rule.id, AlertState.region == r.region).with_for_update()
        ).scalar_one()
        if state.last_result_id == r.id:
            continue
        state.last_result_id = r.id
        changed = False
        if v:
            state.consecutive_breaches += 1
            state.consecutive_ok = 0
            if not state.firing and state.consecutive_breaches >= rule.consecutive_breaches:
                state.firing, changed = True, True
        else:
            state.consecutive_ok += 1
            state.consecutive_breaches = 0
            if state.firing and state.consecutive_ok >= rule.recovery_successes:
                state.firing, changed = False, True
        if changed:
            db.flush()
            reconcile_incident(db, monitor, rule, r, region_firing=state.firing, region=r.region)


def _scope(monitor: Monitor, firing: list[str]) -> str:
    if len(monitor.regions) <= 1:
        return "single_region"
    return "global" if set(monitor.regions) <= set(firing) else "regional"


def reconcile_incident(
    db: Session, monitor: Monitor, rule: AlertRule, r: ResultRow, *, region_firing: bool, region: str
) -> None:
    # Serialize incident changes per rule.
    db.execute(select(AlertRule.id).where(AlertRule.id == rule.id).with_for_update())
    firing = sorted(
        db.execute(
            select(AlertState.region).where(AlertState.rule_id == rule.id, AlertState.firing.is_(True))
        ).scalars()
    )
    incident = db.execute(
        select(Incident).where(Incident.rule_id == rule.id, Incident.status == "open")
    ).scalar_one_or_none()
    now = datetime.now(UTC)
    detail = {"region": region, "result_id": str(r.id), "failure_category": r.failure_category,
              "http_status": r.http_status, "latency_ms": r.latency_ms}

    if len(firing) >= rule.min_regions:
        scope = _scope(monitor, firing)
        if incident is None:
            incident = Incident(
                workspace_id=monitor.workspace_id,
                monitor_id=monitor.id,
                rule_id=rule.id,
                kind=rule.kind,
                scope=scope,
                affected_regions=firing,
                summary=f"{monitor.name}: {KIND_LABEL[rule.kind]} in {', '.join(firing)}",
                opened_at=now,
            )
            db.add(incident)
            db.flush()
            _event(db, incident, "opened", incident.summary, detail)
            enqueue_notifications(db, monitor, incident, "incident.opened")
            metrics.INCIDENTS_OPENED.inc()
            log.info("incident opened", extra=log_fields(incident_id=str(incident.id), monitor_id=str(monitor.id),
                                                         regions=firing, scope=scope))
        else:
            incident.affected_regions = sorted(set(incident.affected_regions) | set(firing))
            # Keep the widest scope observed so history shows that an outage was global.
            if incident.scope != "global":
                incident.scope = scope
            incident.summary = f"{monitor.name}: {KIND_LABEL[rule.kind]} in {', '.join(incident.affected_regions)}"
            kind = "region_firing" if region_firing else "region_recovered"
            verb = "started failing" if region_firing else "recovered"
            _event(db, incident, kind, f"region {region} {verb}", detail)
    elif incident is not None:
        if region_firing:
            _event(db, incident, "region_firing", f"region {region} started failing", detail)
            return
        incident.status = "resolved"
        incident.resolved_at = now
        _event(db, incident, "resolved", f"{monitor.name}: recovered", detail)
        enqueue_notifications(db, monitor, incident, "incident.resolved")
        log.info("incident resolved", extra=log_fields(incident_id=str(incident.id), monitor_id=str(monitor.id)))
    elif region_firing:
        log.info("region firing below min_regions",
                 extra=log_fields(rule_id=str(rule.id), region=region, firing=firing, min_regions=rule.min_regions))


def _event(db: Session, incident: Incident, kind: str, message: str, details: dict) -> None:
    db.add(IncidentEvent(workspace_id=incident.workspace_id, incident_id=incident.id, kind=kind,
                         message=message, details=details))


def incident_payload(monitor: Monitor, incident: Incident, event: str) -> dict:
    return {
        "event": event,
        "incident": {
            "id": str(incident.id),
            "status": incident.status,
            "kind": incident.kind,
            "scope": incident.scope,
            "affected_regions": incident.affected_regions,
            "summary": incident.summary,
            "opened_at": incident.opened_at.isoformat(),
            "resolved_at": incident.resolved_at.isoformat() if incident.resolved_at else None,
        },
        "monitor": {"id": str(monitor.id), "name": monitor.name, "type": monitor.check_type,
                    "target": monitor.target},
    }


def enqueue_notifications(db: Session, monitor: Monitor, incident: Incident, event: str) -> None:
    """Write deliveries to the outbox in the same transaction as the incident change."""
    channels = db.execute(
        select(NotificationChannel).where(
            NotificationChannel.workspace_id == monitor.workspace_id, NotificationChannel.enabled.is_(True)
        )
    ).scalars().all()
    payload = incident_payload(monitor, incident, event)
    for ch in channels:
        db.execute(
            pg_insert(NotificationDelivery)
            .values(
                workspace_id=monitor.workspace_id,
                channel_id=ch.id,
                incident_id=incident.id,
                dedupe_key=f"{incident.id}:{event}:{ch.id}",
                event=event,
                payload=payload,
            )
            .on_conflict_do_nothing(index_elements=["dedupe_key"])
        )
