"""Webhook notification delivery (outbox drained by the worker).

Each request is signed: ``X-ProbeAtlas-Signature: sha256=<hex>`` where the HMAC
input is ``"<X-ProbeAtlas-Timestamp>.<raw body>"`` keyed with the channel's
signing secret. Delivery goes through the same SSRF-safe HTTP client as agent
checks, redirects are not followed, and failures retry with exponential
backoff before the delivery is marked failed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from probeatlas import __version__
from probeatlas.common.logs import log_fields
from probeatlas.common.safehttp import http_request

from . import metrics
from .config import get_settings
from .models import IncidentEvent, NotificationChannel, NotificationDelivery

log = logging.getLogger("probeatlas.notifications")

MAX_ATTEMPTS = 6


@dataclass
class SendOutcome:
    ok: bool
    status: int | None
    error: str | None


Sender = Callable[[NotificationChannel, str, str, dict], SendOutcome]


def sign(secret: str, timestamp: str, body: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), f"{timestamp}.{body}".encode(), hashlib.sha256).hexdigest()


def send_webhook(channel: NotificationChannel, delivery_id: str, event: str, payload: dict) -> SendOutcome:
    body = json.dumps({**payload, "delivery_id": delivery_id, "sent_at": datetime.now(UTC).isoformat()})
    ts = str(int(time.time()))
    out = http_request(
        channel.url,
        policy=get_settings().webhook_policy(),
        timeout_s=10,
        method="POST",
        body=body.encode(),
        headers={
            "Content-Type": "application/json",
            "X-ProbeAtlas-Event": event,
            "X-ProbeAtlas-Delivery": delivery_id,
            "X-ProbeAtlas-Timestamp": ts,
            "X-ProbeAtlas-Signature": sign(channel.signing_secret, ts, body),
        },
        follow_redirects=False,
        max_body_bytes=4096,
        user_agent=f"ProbeAtlas-Webhooks/{__version__}",
    )
    if out.error:
        return SendOutcome(False, None, out.error.message)
    if out.status is not None and 200 <= out.status < 300:
        return SendOutcome(True, out.status, None)
    return SendOutcome(False, out.status, f"receiver returned HTTP {out.status}")


def deliver_pending(db: Session, sender: Sender = send_webhook, now: datetime | None = None, batch: int = 50) -> int:
    now = now or datetime.now(UTC)
    rows = db.execute(
        select(NotificationDelivery)
        .where(NotificationDelivery.status == "pending", NotificationDelivery.next_attempt_at <= now)
        .order_by(NotificationDelivery.next_attempt_at)
        .limit(batch)
        .with_for_update(skip_locked=True)
    ).scalars().all()
    # Lease the rows, then release locks before making network calls.
    for d in rows:
        d.next_attempt_at = now + timedelta(seconds=60)
    db.commit()

    for d in rows:
        channel = db.get(NotificationChannel, d.channel_id)
        if channel is None or not channel.enabled:
            d.status, d.last_error = "failed", "channel disabled or deleted"
            db.commit()
            continue
        outcome = sender(channel, str(d.id), d.event, d.payload)
        d.attempts += 1
        d.response_status = outcome.status
        if outcome.ok:
            d.status, d.sent_at, d.last_error = "sent", datetime.now(UTC), None
            metrics.NOTIFICATIONS.labels("sent").inc()
        else:
            d.last_error = (outcome.error or "unknown error")[:500]
            if d.attempts >= MAX_ATTEMPTS:
                d.status = "failed"
                metrics.NOTIFICATIONS.labels("failed").inc()
                if d.incident_id:
                    db.add(IncidentEvent(workspace_id=d.workspace_id, incident_id=d.incident_id,
                                         kind="notification_failed",
                                         message=f"notification to '{channel.name}' failed: {d.last_error}",
                                         details={"delivery_id": str(d.id)}))
            else:
                d.next_attempt_at = datetime.now(UTC) + timedelta(seconds=30 * 2 ** (d.attempts - 1))
                metrics.NOTIFICATIONS.labels("retry").inc()
            log.warning("notification delivery failed",
                        extra=log_fields(delivery_id=str(d.id), channel_id=str(channel.id),
                                         attempt=d.attempts, error=d.last_error))
        db.commit()
    return len(rows)


def send_test(channel: NotificationChannel) -> SendOutcome:
    payload = {"event": "test", "message": "ProbeAtlas test notification", "channel_id": str(channel.id)}
    return send_webhook(channel, str(uuid.uuid4()), "test", payload)
