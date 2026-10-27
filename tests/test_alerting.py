"""Alert evaluation, incidents, and webhook notifications."""

from __future__ import annotations

import hashlib
import hmac
import json

from sqlalchemy import select, text

from probeatlas.server.housekeeping import mark_no_data_monitors, mark_stale_agents
from probeatlas.server.models import Incident, NotificationDelivery
from probeatlas.server.notifications import MAX_ATTEMPTS, deliver_pending
from tests.conftest import cycle

FAIL = {"success": False, "category": "timeout", "http_status": None}


def _rule(account, monitor_id, **kw):
    body = {"monitor_id": monitor_id, "kind": "unavailable", "consecutive_breaches": 2, "recovery_successes": 2}
    body.update(kw)
    r = account.post("/v1/alert-rules", body)
    assert r.status_code == 201, r.text
    return r.json()


def _incidents(account, **params):
    return account.get("/v1/incidents", params=params).json()["items"]


def test_consecutive_failures_open_one_incident_and_recovery_resolves_it(client, account, agent_a, db):
    m = account.create_monitor()
    rule = _rule(account, m["id"])

    cycle(client, agent_a, m["id"], **FAIL)
    assert _incidents(account) == []  # a single failure is not enough
    assert account.get(f"/v1/monitors/{m['id']}").json()["state"] == "down"

    cycle(client, agent_a, m["id"], **FAIL)
    incs = _incidents(account)
    assert len(incs) == 1 and incs[0]["status"] == "open" and incs[0]["affected_regions"] == ["test-a"]
    assert incs[0]["scope"] == "single_region"
    assert account.get(f"/v1/alert-rules/{rule['id']}").json()["firing_regions"] == ["test-a"]

    for _ in range(3):  # continued failures do not create duplicates
        cycle(client, agent_a, m["id"], **FAIL)
    assert len(_incidents(account)) == 1

    cycle(client, agent_a, m["id"])
    assert _incidents(account)[0]["status"] == "open"  # one success is not recovery
    cycle(client, agent_a, m["id"])
    inc = _incidents(account)[0]
    assert inc["status"] == "resolved" and inc["resolved_at"]
    events = [e["kind"] for e in account.get(f"/v1/incidents/{inc['id']}").json()["events"]]
    assert events == ["opened", "resolved"]
    assert account.get(f"/v1/monitors/{m['id']}").json()["state"] == "up"

    cycle(client, agent_a, m["id"], **FAIL)
    cycle(client, agent_a, m["id"], **FAIL)
    assert [i["status"] for i in _incidents(account)] == ["open", "resolved"]


def test_min_regions_and_global_scope(client, account, agent_a, agent_b, db):
    m = account.create_monitor(regions=["test-a", "test-b"])
    _rule(account, m["id"], consecutive_breaches=1, min_regions=2)
    cycle(client, agent_a, m["id"], **FAIL)
    assert _incidents(account) == []  # only one region failing
    assert account.get(f"/v1/monitors/{m['id']}").json()["state"] in ("down", "degraded")
    cycle(client, agent_b, m["id"], **FAIL)
    inc = _incidents(account)
    assert len(inc) == 1 and inc[0]["scope"] == "global" and inc[0]["affected_regions"] == ["test-a", "test-b"]


def test_regional_incident_scope(client, account, agent_a, agent_b):
    m = account.create_monitor(regions=["test-a", "test-b"])
    _rule(account, m["id"], consecutive_breaches=1)
    cycle(client, agent_b, m["id"])
    cycle(client, agent_a, m["id"], **FAIL)
    inc = _incidents(account)[0]
    assert inc["scope"] == "regional" and inc["affected_regions"] == ["test-a"]
    assert account.get(f"/v1/monitors/{m['id']}").json()["state"] == "degraded"
    # The same incident escalates to global when the other region fails too.
    cycle(client, agent_b, m["id"], **FAIL)
    inc = _incidents(account)
    assert len(inc) == 1 and inc[0]["scope"] == "global"
    assert inc[0]["summary"].endswith("in test-a, test-b")
    # When one region recovers, the incident stays open and keeps its widest scope.
    cycle(client, agent_b, m["id"])
    cycle(client, agent_b, m["id"])
    inc = _incidents(account)[0]
    assert inc["status"] == "open" and inc["scope"] == "global"


def test_http_status_rule_ignores_connection_failures(client, account, agent_a):
    m = account.create_monitor()
    _rule(account, m["id"], kind="http_status", consecutive_breaches=1)
    cycle(client, agent_a, m["id"], **FAIL)  # no HTTP response at all: not a status breach
    assert _incidents(account) == []
    cycle(client, agent_a, m["id"], success=False, category="http_status", http_status=500)
    assert _incidents(account)[0]["kind"] == "http_status"


def test_latency_rule_uses_threshold_and_skips_failed_checks(client, account, agent_a):
    m = account.create_monitor(latency_threshold_ms=200)
    _rule(account, m["id"], kind="latency", threshold=200, consecutive_breaches=2, recovery_successes=1)
    cycle(client, agent_a, m["id"], latency=450.0)
    assert account.get(f"/v1/monitors/{m['id']}").json()["state"] == "degraded"
    cycle(client, agent_a, m["id"], **FAIL)  # no latency: neither breach nor recovery
    assert _incidents(account) == []
    cycle(client, agent_a, m["id"], latency=500.0)
    assert _incidents(account)[0]["kind"] == "latency"
    cycle(client, agent_a, m["id"], latency=50.0)
    assert _incidents(account)[0]["status"] == "resolved"


def test_packet_loss_rule(client, account):
    from tests.conftest import enroll_agent

    pinger = enroll_agent(client, "test-a", capabilities=("icmp",))
    m = account.create_monitor(type="icmp", target="8.8.8.8")
    _rule(account, m["id"], kind="packet_loss", threshold=20, consecutive_breaches=1)
    cycle(client, pinger, m["id"], latency=10.0, http_status=None, packet_loss_pct=10.0, packets_sent=10,
          packets_received=9)
    assert _incidents(account) == []
    cycle(client, pinger, m["id"], latency=10.0, http_status=None, packet_loss_pct=40.0, packets_sent=10,
          packets_received=6)
    assert _incidents(account)[0]["kind"] == "packet_loss"


def test_rule_validation(account, agent_a):
    m = account.create_monitor()
    assert account.post("/v1/alert-rules", {"monitor_id": m["id"], "kind": "latency"}).status_code == 422
    r = account.post("/v1/alert-rules", {"monitor_id": m["id"], "kind": "packet_loss", "threshold": 5})
    assert r.status_code == 422  # http monitor


def test_webhook_delivery_signed_retried_and_failed(client, account, agent_a, target_server, settings, db):
    settings.set(webhook_allowed_private_cidrs="127.0.0.0/8")
    ch = account.post("/v1/notification-channels", {"name": "ops", "url": target_server.url("/hook")})
    assert ch.status_code == 201, ch.text
    secret = ch.json()["signing_secret"]

    test = account.post(f"/v1/notification-channels/{ch.json()['id']}/test").json()
    assert test == {"delivered": True, "status_code": 200, "error": None}
    target_server.requests.clear()

    m = account.create_monitor()
    _rule(account, m["id"], consecutive_breaches=1, recovery_successes=1)
    cycle(client, agent_a, m["id"], **FAIL)
    assert deliver_pending(db) == 1
    req = target_server.requests[-1]
    body = req["body"].decode()
    expected = "sha256=" + hmac.new(secret.encode(), f"{req['headers']['X-ProbeAtlas-Timestamp']}.{body}".encode(),
                                    hashlib.sha256).hexdigest()
    assert req["headers"]["X-ProbeAtlas-Signature"] == expected
    payload = json.loads(body)
    assert payload["event"] == "incident.opened" and payload["monitor"]["id"] == m["id"]
    assert deliver_pending(db) == 0  # nothing left; sent exactly once

    # Receiver failing: retried with backoff, then marked failed with an incident event.
    target_server.status = 500
    cycle(client, agent_a, m["id"])  # resolves -> incident.resolved queued
    for _ in range(MAX_ATTEMPTS):
        db.execute(text("UPDATE notification_deliveries SET next_attempt_at = now() WHERE status = 'pending'"))
        db.commit()
        deliver_pending(db)
    d = db.execute(select(NotificationDelivery).where(NotificationDelivery.event == "incident.resolved")).scalar_one()
    assert d.status == "failed" and d.attempts == MAX_ATTEMPTS and "HTTP 500" in d.last_error
    inc = db.execute(select(Incident)).scalar_one()
    kinds = [e["kind"] for e in account.get(f"/v1/incidents/{inc.id}").json()["events"]]
    assert "notification_failed" in kinds


def test_webhook_urls_are_ssrf_checked(account, settings):
    for url in ("http://127.0.0.1:9/hook", "http://169.254.169.254/", "http://localhost/hook", "ftp://x.com/"):
        assert account.post("/v1/notification-channels", {"name": "x", "url": url}).status_code == 422


def test_heartbeat_expiry_marks_agent_offline_and_heartbeat_restores_it(client, account, agent_a, db):
    db.execute(text("UPDATE agents SET last_heartbeat_at = now() - interval '10 minutes'"))
    db.commit()
    assert mark_stale_agents(db) == 1
    assert account.get("/v1/agents").json()[0]["status"] == "offline"
    assert account.get("/v1/locations").json()[0]["agents_online"] == 0
    hb = client.post("/agent/v1/heartbeat", headers=agent_a["headers"],
                     json={"version": "0.1.1", "capabilities": ["http"], "running_jobs": 0})
    assert hb.status_code == 200
    a = account.get("/v1/agents").json()[0]
    assert a["status"] == "online" and a["version"] == "0.1.1" and a["capabilities"] == ["http"]


def test_monitor_without_recent_data_becomes_unknown(client, account, agent_a, db):
    m = account.create_monitor()
    cycle(client, agent_a, m["id"])
    assert account.get(f"/v1/monitors/{m['id']}").json()["state"] == "up"
    db.execute(text("UPDATE monitors SET last_result_at = now() - interval '1 hour'"))
    db.commit()
    assert mark_no_data_monitors(db) == 1
    assert account.get(f"/v1/monitors/{m['id']}").json()["state"] == "unknown"
