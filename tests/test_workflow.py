"""End-to-end: real agent runner + control plane + controlled HTTP endpoint.

Flow: enroll agent -> create monitor -> schedule -> agent claims and measures ->
results ingested -> dashboard APIs -> failure -> incident -> webhook -> recovery.
"""

from __future__ import annotations

import threading
import time

import pytest
from sqlalchemy import text

from probeatlas.agent.client import ControlPlaneClient, TransientError
from probeatlas.agent.config import AgentConfig
from probeatlas.agent.runner import Agent
from probeatlas.server.notifications import deliver_pending
from tests.conftest import BOOTSTRAP_TOKEN, TargetServer, force_due, run_scheduler


def make_agent(client, tmp_path, region="test-a") -> Agent:
    """Create and enroll a real :class:`Agent` whose HTTP traffic goes to the test app."""
    cfg = AgentConfig(
        api_url="http://testserver", enrollment_token=BOOTSTRAP_TOKEN, name=f"e2e-{region}", region=region,
        country_code="PL", city="Warsaw", provider="local-dev", state_file=str(tmp_path / f"{region}.json"),
        concurrency=4, poll_interval_s=0.1, heartbeat_interval_s=0.5,
        allowed_private_cidrs="127.0.0.0/8,::1/128", ca_file=None,
    )
    cp = ControlPlaneClient(cfg.api_url)
    cp._http = client  # route agent traffic into the in-process ASGI app
    agent = Agent(cfg, cp)
    agent.ensure_registered()
    return agent


def run_round(agent: Agent, monitor_id: str) -> None:
    """Schedule the monitor, let the agent claim and run the check, and flush its result."""
    force_due(monitor_id)
    run_scheduler()
    assert agent.step() >= 1
    deadline = time.time() + 10
    while agent.inflight and time.time() < deadline:
        time.sleep(0.02)
        agent._collect()
    agent.step()  # flush results


def test_end_to_end_monitoring_workflow(client, account, target_server, tmp_path, settings, db):
    settings.set(allow_private_targets=True, webhook_allowed_private_cidrs="127.0.0.0/8")
    hook = TargetServer()
    try:
        agent = make_agent(client, tmp_path)
        assert account.get("/v1/agents").json()[0]["city"] == "Warsaw"
        account.post("/v1/notification-channels", {"name": "ops", "url": hook.url("/hook")})
        m = account.create_monitor(name="demo", target=f"http://localhost:{target_server.port}/ok",
                                   timeout_ms=3000)
        account.post("/v1/alert-rules", {"monitor_id": m["id"], "kind": "unavailable",
                                         "consecutive_breaches": 2, "recovery_successes": 1})

        run_round(agent, m["id"])
        items = account.get(f"/v1/measurements?monitor_id={m['id']}").json()["items"]
        assert len(items) == 1
        res = items[0]
        assert res["success"] and res["http_status"] == 200 and res["agent_id"] == agent.client.agent_id
        assert res["dns_ms"] is not None and res["connect_ms"] is not None and res["ttfb_ms"] is not None
        assert target_server.requests[-1]["headers"]["User-Agent"].startswith("ProbeAtlas-Agent/")
        assert account.get(f"/v1/monitors/{m['id']}").json()["state"] == "up"

        target_server.status = 503
        run_round(agent, m["id"])
        run_round(agent, m["id"])
        mon = account.get(f"/v1/monitors/{m['id']}").json()
        assert mon["state"] == "down"
        incidents = account.get("/v1/incidents").json()["items"]
        assert len(incidents) == 1 and incidents[0]["status"] == "open"
        failed = account.get(f"/v1/measurements?monitor_id={m['id']}&success=false").json()["items"]
        assert len(failed) == 2 and failed[0]["failure_category"] == "http_status" and failed[0]["http_status"] == 503

        deliver_pending(db)
        assert hook.requests and b"incident.opened" in hook.requests[-1]["body"]

        target_server.status = 200
        run_round(agent, m["id"])
        assert account.get("/v1/incidents").json()["items"][0]["status"] == "resolved"
        perf = account.get(f"/v1/monitors/{m['id']}/regional-performance").json()
        assert perf["regions"][0]["total"] == 4 and perf["regions"][0]["successes"] == 2
        assert perf["failure_categories"] == {"http_status": 2}
    finally:
        hook.close()


def test_agent_buffers_results_while_control_plane_is_unreachable(client, account, target_server, tmp_path,
                                                                   settings, db):
    settings.set(allow_private_targets=True)
    agent = make_agent(client, tmp_path)
    m = account.create_monitor(target=f"http://localhost:{target_server.port}/ok", timeout_ms=3000)
    force_due(m["id"])
    run_scheduler()
    agent.step()
    while agent.inflight:
        time.sleep(0.02)
        agent._collect()
    assert len(agent.outbox) == 1

    real_submit = agent.client.submit

    def unreachable(_results):
        """Simulate a control-plane outage on result submission."""
        raise TransientError("connection refused")

    agent.client.submit = unreachable
    with pytest.raises(TransientError):
        agent.step()
    assert len(agent.outbox) == 1  # nothing lost
    agent.client.submit = real_submit
    agent.step()
    assert len(agent.outbox) == 0
    assert len(account.get("/v1/measurements").json()["items"]) == 1


def test_agent_reenrolls_when_identity_is_revoked(client, account, tmp_path, db):
    agent = make_agent(client, tmp_path)
    old_id = agent.client.agent_id
    db.execute(text("UPDATE agents SET status = 'disabled' WHERE id = :id"), {"id": old_id})
    db.commit()
    from probeatlas.agent.client import AuthError

    with pytest.raises(AuthError):
        agent.step()
    agent._reenroll()
    assert agent.client.agent_id != old_id
    assert agent.step() == 0


def test_agent_graceful_shutdown(client, account, target_server, tmp_path, settings):
    settings.set(allow_private_targets=True)
    agent = make_agent(client, tmp_path)
    m = account.create_monitor(target=f"http://localhost:{target_server.port}/slow?ms=300", timeout_ms=3000)
    force_due(m["id"])
    run_scheduler()
    agent.client.close = lambda: None  # the TestClient is shared with the assertions below
    t = threading.Thread(target=agent.run)
    t.start()
    deadline = time.time() + 5
    while not agent.inflight and time.time() < deadline:
        time.sleep(0.01)
    agent.stop_event.set()  # stop while the check is in flight
    t.join(timeout=10)
    assert not t.is_alive()
    items = account.get("/v1/measurements").json()["items"]
    assert len(items) == 1 and items[0]["success"]  # in-flight check finished and was flushed
