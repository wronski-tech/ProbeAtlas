"""Result submission: validation, authorization, idempotency, late results."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, text

from probeatlas.server.models import Result
from probeatlas.server.scheduling import reap_jobs
from tests.conftest import enroll_agent, make_result, run_scheduler


def _setup(client, account, agent):
    m = account.create_monitor()
    run_scheduler()
    job = client.post("/agent/v1/jobs/claim", headers=agent["headers"], json={}).json()["jobs"][0]
    return m, job


def _submit(client, agent, *results):
    r = client.post("/agent/v1/results", headers=agent["headers"], json={"results": list(results)})
    assert r.status_code == 200, r.text
    return r.json()["acks"]


def _count(db):
    return db.execute(select(func.count()).select_from(Result)).scalar_one()


def test_result_is_persisted_with_both_timestamps(client, account, agent_a, db):
    m, job = _setup(client, account, agent_a)
    measured = datetime.now(UTC) - timedelta(seconds=3)
    res = make_result(job, latency=123.4, dns_ms=1.5, connect_ms=10.0, ttfb_ms=50.0, total_ms=123.4,
                      measured_at=measured.isoformat())
    assert _submit(client, agent_a, res)[0]["status"] == "accepted"
    row = db.execute(select(Result)).scalar_one()
    assert str(row.workspace_id) == account.workspace_id and str(row.monitor_id) == m["id"]
    assert row.region == "test-a" and str(row.agent_id) == agent_a["agent_id"]
    assert abs((row.measured_at - measured).total_seconds()) < 0.01
    assert row.ingested_at > row.measured_at
    assert row.latency_ms == 123.4 and row.tls_ms is None and row.packet_loss_pct is None
    items = account.get(f"/v1/measurements?monitor_id={m['id']}").json()["items"]
    assert items[0]["total_ms"] == 123.4 and items[0]["job_id"] == job["job_id"]
    assert account.get(f"/v1/monitors/{m['id']}").json()["state"] == "up"


def test_duplicate_submissions_are_idempotent(client, account, agent_a, db):
    _, job = _setup(client, account, agent_a)
    res = make_result(job)
    assert _submit(client, agent_a, res)[0]["status"] == "accepted"
    assert _submit(client, agent_a, res)[0]["status"] == "duplicate"
    conflicting = make_result(job, success=False, category="timeout", http_status=None)
    ack = _submit(client, agent_a, conflicting)[0]
    assert ack["status"] == "rejected" and ack["reason"] == "job_already_has_result"
    # Same result twice in one batch.
    assert _count(db) == 1


def test_results_from_unassigned_agents_are_rejected(client, account, agent_a, db):
    _, job = _setup(client, account, agent_a)
    intruder = enroll_agent(client, "test-a")
    ack = _submit(client, intruder, make_result(job))[0]
    assert ack["status"] == "rejected" and ack["reason"] == "not_assigned_to_agent"
    ack = _submit(client, agent_a, make_result({**job, "job_id": "00000000-0000-0000-0000-000000000000"}))[0]
    assert ack["reason"] == "unknown_job"
    assert _count(db) == 0


def test_check_type_and_timestamp_validation(client, account, agent_a, db):
    _, job = _setup(client, account, agent_a)
    assert _submit(client, agent_a, make_result({**job, "check_type": "tcp"}))[0]["reason"] == "check_type_mismatch"
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    assert _submit(client, agent_a, make_result(job, measured_at=future))[0]["reason"] == "implausible_measured_at"
    assert _count(db) == 0


def test_malformed_payloads_rejected_by_schema(client, account, agent_a):
    _, job = _setup(client, account, agent_a)
    for bad in (
        make_result(job, latency=-5),
        make_result(job, packet_loss_pct=150),
        make_result(job, workspace_id="11111111-1111-1111-1111-111111111111"),  # no tenant injection
        {**make_result(job), "schema_version": 99},
        {**make_result(job), "measured_at": "2026-01-01T00:00:00"},  # naive timestamp
    ):
        r = client.post("/agent/v1/results", headers=agent_a["headers"], json={"results": [bad]})
        assert r.status_code == 422, bad


def test_late_result_from_original_agent_is_accepted_and_flagged(client, account, agent_a, db, settings):
    settings.set(job_max_attempts=1)
    _, job = _setup(client, account, agent_a)
    db.execute(text("UPDATE jobs SET lease_expires_at = now() - interval '5 seconds'"))
    db.commit()
    reap_jobs(db)
    ack = _submit(client, agent_a, make_result(job))[0]
    assert ack["status"] == "accepted" and ack["reason"] == "late"
    assert db.execute(select(Result.late)).scalar_one() is True


def test_result_after_reassignment_is_rejected(client, account, agent_a, db):
    _, job = _setup(client, account, agent_a)
    db.execute(text("UPDATE jobs SET lease_expires_at = now() - interval '5 seconds'"))
    db.commit()
    reap_jobs(db)  # retried: back to pending, unassigned
    other = enroll_agent(client, "test-a")
    client.post("/agent/v1/jobs/claim", headers=other["headers"], json={})
    assert _submit(client, agent_a, make_result(job))[0]["reason"] == "not_assigned_to_agent"
    assert _submit(client, other, make_result(job))[0]["status"] == "accepted"


def test_error_messages_are_sanitized(client, account, agent_a, db):
    _, job = _setup(client, account, agent_a)
    res = make_result(job, success=False, category="protocol_error", http_status=None,
                      error_message="bad\x00thing Bearer abc.def.ghi pak_123456_secretsecret")
    _submit(client, agent_a, res)
    msg = db.execute(select(Result.error_message)).scalar_one()
    assert "\x00" not in msg and "abc.def.ghi" not in msg and "secretsecret" not in msg


def test_agent_auth_required(client, account, agent_a, db):
    assert client.post("/agent/v1/jobs/claim", json={}).status_code == 401
    user_key = account.api_key()
    assert client.post("/agent/v1/jobs/claim", json={},
                       headers={"Authorization": f"Bearer {user_key}"}).status_code == 401
    db.execute(text("UPDATE agents SET status = 'disabled'"))
    db.commit()
    assert client.post("/agent/v1/jobs/claim", json={}, headers=agent_a["headers"]).status_code == 401


def test_enrollment_token_rules(client, db):
    from probeatlas.server.models import EnrollmentToken
    from probeatlas.server.security import token_hash

    bad = client.post("/agent/v1/register", headers={"Authorization": "Bearer pae_wrong"},
                      json={"name": "x", "region": "test-a", "version": "1", "capabilities": ["http"]})
    assert bad.status_code == 401
    db.add(EnrollmentToken(token_hash=token_hash("pae_single-use-token-xxxxxxxx"), description="t",
                           region="eu-west", max_uses=1))
    db.commit()
    h = {"Authorization": "Bearer pae_single-use-token-xxxxxxxx"}
    body = {"name": "x", "region": "us-east", "version": "1", "capabilities": ["http"]}
    assert client.post("/agent/v1/register", headers=h, json=body).status_code == 403
    body["region"] = "eu-west"
    assert client.post("/agent/v1/register", headers=h, json=body).status_code == 201
    assert client.post("/agent/v1/register", headers=h, json=body).status_code == 401  # exhausted
