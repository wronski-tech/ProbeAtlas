"""Job scheduling, dispatch to agents, lease expiry and recovery."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text

from probeatlas.server.models import Job
from probeatlas.server.scheduling import reap_jobs
from tests.conftest import enroll_agent, force_due, past, run_scheduler


def _claim(client, agent, n=10):
    r = client.post("/agent/v1/jobs/claim", headers=agent["headers"], json={"max_jobs": n})
    assert r.status_code == 200, r.text
    return r.json()["jobs"]


def _jobs(db):
    db.expire_all()
    return db.execute(select(Job).order_by(Job.region)).scalars().all()


def test_one_job_per_region_and_no_duplicate_scheduling(client, account, agent_a, agent_b, db):
    m = account.create_monitor(regions=["test-a", "test-b"])
    assert run_scheduler() == 2
    assert run_scheduler() == 0  # next_run_at moved forward
    jobs = _jobs(db)
    assert [j.region for j in jobs] == ["test-a", "test-b"]
    assert all(j.payload["target"] == m["target"] and j.payload["config_version"] == 1 for j in jobs)

    # Replaying the same slot (e.g. a scheduler restarted mid-transaction) is idempotent.
    db.execute(text("UPDATE monitors SET next_run_at = :t"), {"t": jobs[0].scheduled_for})
    db.commit()
    assert run_scheduler() == 0
    assert len(_jobs(db)) == 2


def test_agents_only_receive_jobs_for_their_region_and_capabilities(client, account, agent_a, agent_b):
    account.create_monitor(regions=["test-a", "test-b"])
    no_icmp = agent_a  # capabilities: http, dns, tcp
    icmp = account.create_monitor(type="icmp", target="8.8.8.8", regions=["test-a"])
    run_scheduler()
    a_jobs = _claim(client, no_icmp)
    assert {j["check_type"] for j in a_jobs} == {"http"}
    assert len(_claim(client, agent_b)) == 1
    pinger = enroll_agent(client, "test-a", capabilities=("http", "icmp"))
    got = _claim(client, pinger)
    assert [j["monitor_id"] for j in got] == [icmp["id"]]


def test_concurrent_claims_never_share_a_job(client, account, agent_a):
    other = enroll_agent(client, "test-a")
    for i in range(6):
        account.create_monitor(name=f"m{i}")
    run_scheduler()
    first = _claim(client, agent_a, 3)
    second = _claim(client, other, 10)
    assert len(first) == 3 and len(second) == 3
    assert not {j["job_id"] for j in first} & {j["job_id"] for j in second}
    assert _claim(client, agent_a) == []


def test_expired_lease_is_retried_then_expired(client, account, agent_a, db, settings):
    settings.set(job_max_attempts=2)
    account.create_monitor()
    run_scheduler()
    job_id = _claim(client, agent_a)[0]["job_id"]
    db.execute(text("UPDATE jobs SET lease_expires_at = now() - interval '1 second'"))
    db.commit()
    assert reap_jobs(db)["retried"] == 1
    job = _jobs(db)[0]
    assert job.status == "pending" and job.agent_id is None and job.attempts == 1

    other = enroll_agent(client, "test-a")
    assert _claim(client, other)[0]["job_id"] == job_id
    db.execute(text("UPDATE jobs SET lease_expires_at = now() - interval '1 second'"))
    db.commit()
    assert reap_jobs(db)["lease_timeout"] == 1
    job = _jobs(db)[0]
    assert job.status == "expired" and job.expire_reason == "lease_timeout" and job.attempts == 2


def test_unclaimed_jobs_expire_as_missing_data(client, account, agent_a, db):
    account.create_monitor()
    run_scheduler()
    db.execute(text("UPDATE jobs SET expires_at = now() - interval '1 second'"))
    db.commit()
    assert reap_jobs(db)["unclaimed"] == 1
    assert _jobs(db)[0].expire_reason == "unclaimed"
    assert _claim(client, agent_a) == []


def test_paused_monitors_are_not_scheduled_and_pending_jobs_cancelled(client, account, agent_a, db):
    m = account.create_monitor()
    run_scheduler()
    account.post(f"/v1/monitors/{m['id']}/pause")
    assert _jobs(db)[0].status == "cancelled"
    force_due(m["id"])
    assert run_scheduler() == 0


def test_scheduler_skips_missed_slots_after_downtime(client, account, agent_a, db):
    m = account.create_monitor(interval_seconds=60)
    db.execute(text("UPDATE monitors SET next_run_at = :t WHERE id = :id"), {"t": past(3600), "id": m["id"]})
    db.commit()
    now = datetime.now(UTC)
    assert run_scheduler(now=now) == 1  # one job, not 60 backlogged ones
    job = _jobs(db)[0]
    assert abs((job.scheduled_for - now).total_seconds()) < 2
    next_run = db.execute(text("SELECT next_run_at FROM monitors")).scalar_one()
    assert timedelta(seconds=58) < next_run - now < timedelta(seconds=61)


def test_job_payload_snapshot_survives_monitor_edits(client, account, agent_a):
    m = account.create_monitor(target="https://old.example.com/")
    run_scheduler()
    account.patch(f"/v1/monitors/{m['id']}", {"target": "https://new.example.com/"})
    job = _claim(client, agent_a)[0]
    assert job["target"] == "https://old.example.com/"
