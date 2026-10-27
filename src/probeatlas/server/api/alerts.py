"""Alert rules, incidents, and notification channels."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from probeatlas.common.netpolicy import BlockedTargetError
from probeatlas.common.safehttp import validate_http_url

from ..audit import audit
from ..config import Settings, get_settings
from ..db import get_db
from ..deps import Principal, get_principal, require_owner, tenant_get
from ..errors import APIError
from ..models import AlertRule, AlertState, Incident, IncidentEvent, Monitor, NotificationChannel, NotificationDelivery
from ..notifications import send_test
from ..schemas import (
    AlertRuleCreate,
    AlertRuleOut,
    AlertRuleUpdate,
    ChannelCreate,
    ChannelIssued,
    ChannelOut,
    ChannelTestResult,
    ChannelUpdate,
    DeliveryOut,
    IncidentDetail,
    IncidentEventOut,
    IncidentOut,
    Page,
    decode_cursor,
    encode_cursor,
)
from ..security import PREFIX_WEBHOOK_SECRET, new_token

router = APIRouter(prefix="/v1", tags=["alerting"])

# Every incident fans out to all channels, so this also bounds webhook traffic per incident.
MAX_CHANNELS_PER_WORKSPACE = 10


def _rule_out(db: Session, rule: AlertRule) -> AlertRuleOut:
    """Build the response for ``rule``, including the regions where it is currently firing."""
    firing = db.execute(
        select(AlertState.region).where(AlertState.rule_id == rule.id, AlertState.firing.is_(True))
    ).scalars().all()
    return AlertRuleOut.model_validate(rule).model_copy(update={"firing_regions": sorted(firing)})


@router.get("/alert-rules", response_model=list[AlertRuleOut], tags=["alerting"])
def list_rules(monitor_id: uuid.UUID | None = None, p: Principal = Depends(get_principal),
               db: Session = Depends(get_db)):
    """List the workspace's alert rules, optionally for one monitor."""
    q = select(AlertRule).where(AlertRule.workspace_id == p.workspace_id)
    if monitor_id:
        q = q.where(AlertRule.monitor_id == monitor_id)
    return [_rule_out(db, r) for r in db.execute(q.order_by(AlertRule.created_at)).scalars()]


@router.post("/alert-rules", response_model=AlertRuleOut, status_code=201)
def create_rule(body: AlertRuleCreate, p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """Create an alert rule for a monitor.

    ``http_status`` rules apply only to HTTP monitors and ``packet_loss`` rules only to ICMP monitors. A
    monitor can have at most 20 rules.
    """
    monitor = tenant_get(db, Monitor, body.monitor_id, p, "monitor")
    if body.kind == "http_status" and monitor.check_type != "http":
        raise APIError(422, "validation_error", "http_status rules only apply to http monitors")
    if body.kind == "packet_loss" and monitor.check_type != "icmp":
        raise APIError(422, "validation_error", "packet_loss rules only apply to icmp monitors")
    existing = db.execute(select(func.count()).select_from(AlertRule).where(AlertRule.monitor_id == monitor.id))
    if existing.scalar_one() >= 20:
        raise APIError(403, "limit_exceeded", "at most 20 alert rules per monitor")
    rule = AlertRule(workspace_id=p.workspace_id, **body.model_dump())
    db.add(rule)
    db.flush()
    audit(db, "alert_rule.created", workspace_id=p.workspace_id, actor_type=p.actor_type, actor_id=p.actor_id,
          target_type="alert_rule", target_id=rule.id, ip=p.ip, details={"kind": rule.kind})
    db.commit()
    db.refresh(rule)
    return _rule_out(db, rule)


@router.get("/alert-rules/{rule_id}", response_model=AlertRuleOut)
def get_rule(rule_id: uuid.UUID, p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """Get one alert rule and the regions where it is firing."""
    return _rule_out(db, tenant_get(db, AlertRule, rule_id, p, "alert rule"))


@router.patch("/alert-rules/{rule_id}", response_model=AlertRuleOut)
def update_rule(rule_id: uuid.UUID, body: AlertRuleUpdate, p: Principal = Depends(get_principal),
                db: Session = Depends(get_db)):
    """Update an alert rule's threshold, hysteresis, minimum regions, or enabled flag."""
    rule = tenant_get(db, AlertRule, rule_id, p, "alert rule")
    for k, v in body.model_dump(exclude_unset=True).items():
        if v is not None:
            setattr(rule, k, v)
    if rule.kind in ("latency", "packet_loss") and rule.threshold is None:
        raise APIError(422, "validation_error", "threshold is required")
    audit(db, "alert_rule.updated", workspace_id=p.workspace_id, actor_type=p.actor_type, actor_id=p.actor_id,
          target_type="alert_rule", target_id=rule.id, ip=p.ip)
    db.commit()
    db.refresh(rule)
    return _rule_out(db, rule)


@router.delete("/alert-rules/{rule_id}", status_code=204)
def delete_rule(rule_id: uuid.UUID, p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """Delete an alert rule together with its incidents and streak state."""
    rule = tenant_get(db, AlertRule, rule_id, p, "alert rule")
    audit(db, "alert_rule.deleted", workspace_id=p.workspace_id, actor_type=p.actor_type, actor_id=p.actor_id,
          target_type="alert_rule", target_id=rule.id, ip=p.ip)
    db.delete(rule)
    db.commit()


@router.get("/incidents", response_model=Page[IncidentOut])
def list_incidents(
    status: str | None = Query(None, pattern="^(open|resolved)$"),
    monitor_id: uuid.UUID | None = None,
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = None,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    """List incidents, newest first, optionally filtered by status and monitor. Cursor-paginated."""
    q = select(Incident).where(Incident.workspace_id == p.workspace_id)
    if status:
        q = q.where(Incident.status == status)
    if monitor_id:
        q = q.where(Incident.monitor_id == monitor_id)
    if cursor:
        ts, iid = decode_cursor(cursor)
        c_ts, c_id = datetime.fromisoformat(ts), uuid.UUID(iid)
        q = q.where(or_(Incident.opened_at < c_ts, and_(Incident.opened_at == c_ts, Incident.id < c_id)))
    rows = db.execute(q.order_by(Incident.opened_at.desc(), Incident.id.desc()).limit(limit + 1)).scalars().all()
    nxt = encode_cursor(rows[limit - 1].opened_at, rows[limit - 1].id) if len(rows) > limit else None
    return Page(items=[IncidentOut.model_validate(i) for i in rows[:limit]], next_cursor=nxt)


@router.get("/incidents/{incident_id}", response_model=IncidentDetail)
def get_incident(incident_id: uuid.UUID, p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """Get an incident with its full event timeline."""
    inc = tenant_get(db, Incident, incident_id, p, "incident")
    events = db.execute(
        select(IncidentEvent).where(IncidentEvent.incident_id == inc.id).order_by(IncidentEvent.created_at)
    ).scalars().all()
    return IncidentDetail(**IncidentOut.model_validate(inc).model_dump(),
                          events=[IncidentEventOut.model_validate(e) for e in events])


# --- notification channels ------------------------------------------------------


def _check_url(url: str, settings: Settings) -> str:
    """Validate a webhook URL against the webhook network policy or raise a 422 on ``body.url``."""
    try:
        return validate_http_url(url, settings.webhook_policy())
    except (ValueError, BlockedTargetError) as exc:
        raise APIError(422, "validation_error", "request validation failed",
                       [{"loc": ["body", "url"], "msg": f"webhook URL not allowed: {exc}", "type": "value_error"}]
                       ) from exc


@router.get("/notification-channels", response_model=list[ChannelOut])
def list_channels(p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """List the workspace's notification channels. Signing secrets are not included."""
    return db.execute(select(NotificationChannel).where(NotificationChannel.workspace_id == p.workspace_id)
                      .order_by(NotificationChannel.created_at)).scalars().all()


@router.post("/notification-channels", response_model=ChannelIssued, status_code=201)
def create_channel(body: ChannelCreate, p: Principal = Depends(require_owner), db: Session = Depends(get_db),
                   settings: Settings = Depends(get_settings)):
    """Create a webhook channel (owner only). The response includes the signing secret once.

    The URL must be http(s) and resolve to a public address. At most 10 channels per workspace.
    """
    url = _check_url(body.url, settings)
    count = db.execute(select(func.count()).select_from(NotificationChannel)
                       .where(NotificationChannel.workspace_id == p.workspace_id)).scalar_one()
    if count >= MAX_CHANNELS_PER_WORKSPACE:
        raise APIError(403, "limit_exceeded",
                       f"at most {MAX_CHANNELS_PER_WORKSPACE} notification channels per workspace")
    secret = new_token(PREFIX_WEBHOOK_SECRET)
    ch = NotificationChannel(workspace_id=p.workspace_id, kind="webhook", name=body.name, url=url,
                             signing_secret=secret, enabled=body.enabled)
    db.add(ch)
    db.flush()
    audit(db, "channel.created", workspace_id=p.workspace_id, actor_type=p.actor_type, actor_id=p.actor_id,
          target_type="notification_channel", target_id=ch.id, ip=p.ip)
    db.commit()
    return ChannelIssued(**ChannelOut.model_validate(ch).model_dump(), signing_secret=secret)


@router.patch("/notification-channels/{channel_id}", response_model=ChannelOut)
def update_channel(channel_id: uuid.UUID, body: ChannelUpdate, p: Principal = Depends(require_owner),
                   db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    """Update a channel's name, URL, or enabled flag (owner only)."""
    ch = tenant_get(db, NotificationChannel, channel_id, p, "notification channel")
    changes = body.model_dump(exclude_unset=True)
    if changes.get("url"):
        ch.url = _check_url(changes["url"], settings)
    if changes.get("name"):
        ch.name = changes["name"]
    if changes.get("enabled") is not None:
        ch.enabled = changes["enabled"]
    audit(db, "channel.updated", workspace_id=p.workspace_id, actor_type=p.actor_type, actor_id=p.actor_id,
          target_type="notification_channel", target_id=ch.id, ip=p.ip, details={"fields": sorted(changes)})
    db.commit()
    return ch


@router.delete("/notification-channels/{channel_id}", status_code=204)
def delete_channel(channel_id: uuid.UUID, p: Principal = Depends(require_owner), db: Session = Depends(get_db)):
    """Delete a notification channel and its pending deliveries (owner only)."""
    ch = tenant_get(db, NotificationChannel, channel_id, p, "notification channel")
    audit(db, "channel.deleted", workspace_id=p.workspace_id, actor_type=p.actor_type, actor_id=p.actor_id,
          target_type="notification_channel", target_id=ch.id, ip=p.ip)
    db.delete(ch)
    db.commit()


@router.post("/notification-channels/{channel_id}/test", response_model=ChannelTestResult)
def test_channel(channel_id: uuid.UUID, p: Principal = Depends(require_owner), db: Session = Depends(get_db)):
    """Send a signed test payload synchronously and report the receiver's response."""
    ch = tenant_get(db, NotificationChannel, channel_id, p, "notification channel")
    outcome = send_test(ch)
    return ChannelTestResult(delivered=outcome.ok, status_code=outcome.status, error=outcome.error)


@router.get("/notification-deliveries", response_model=list[DeliveryOut])
def list_deliveries(limit: int = Query(50, ge=1, le=200), p: Principal = Depends(get_principal),
                    db: Session = Depends(get_db)):
    """List recent notification deliveries with their status and last error."""
    return db.execute(select(NotificationDelivery).where(NotificationDelivery.workspace_id == p.workspace_id)
                      .order_by(NotificationDelivery.created_at.desc()).limit(limit)).scalars().all()
