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
SEND_TIMEOUT_S = 10


@dataclass
class SendOutcome:
    """Result of one delivery attempt: whether it succeeded, the HTTP status, and a safe error message."""

    ok: bool
    status: int | None
    error: str | None


Sender = Callable[[NotificationChannel, str, str, dict], SendOutcome]


def sign(secret: str, timestamp: str, body: str) -> str:
    """Return the ``sha256=<hex>`` HMAC signature of ``"<timestamp>.<body>"`` keyed with ``secret``."""
    return "sha256=" + hmac.new(secret.encode(), f"{timestamp}.{body}".encode(), hashlib.sha256).hexdigest()


def send_webhook(channel: NotificationChannel, delivery_id: str, event: str, payload: dict) -> SendOutcome:
    """POST a signed JSON payload to a webhook channel through the SSRF-safe client.

    Redirects are not followed and only 2xx counts as delivered. Never raises for network errors.
    """
    body = json.dumps({**payload, "delivery_id": delivery_id, "sent_at": datetime.now(UTC).isoformat()})
    ts = str(int(time.time()))
    out = http_request(
        channel.url,
        policy=get_settings().webhook_policy(),
        timeout_s=SEND_TIMEOUT_S,
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
    """Send due notification deliveries from the outbox, retrying with exponential backoff.

    Rows are leased for longer than the batch can take to send, and their locks are released before any
    network call. After ``MAX_ATTEMPTS`` a delivery is marked ``failed`` and a ``notification_failed`` event
    is added to the incident.
    ``sender`` is injectable so tests never send real notifications. Returns the number of deliveries
    processed.
    """
    now = now or datetime.now(UTC)
    rows = db.execute(
        select(NotificationDelivery)
        .where(NotificationDelivery.status == "pending", NotificationDelivery.next_attempt_at <= now)
        .order_by(NotificationDelivery.next_attempt_at)
        .limit(batch)
        .with_for_update(skip_locked=True)
    ).scalars().all()
    # Lease the rows for longer than sending the whole batch can take, then release the row locks before
    # making network calls. A shorter lease would let another worker pick up and resend rows still queued here.
    lease = timedelta(seconds=SEND_TIMEOUT_S * len(rows) + 60)
    for d in rows:
        d.next_attempt_at = now + lease
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
    """Send a ``test`` event to ``channel`` immediately and return the outcome."""
    payload = {"event": "test", "message": "ProbeAtlas test notification", "channel_id": str(channel.id)}
    return send_webhook(channel, str(uuid.uuid4()), "test", payload)
