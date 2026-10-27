"""Regional aggregation and percentile rules."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from probeatlas.server.models import Job, Result


def _insert(db, monitor: dict, workspace_id: str, agent_id: str, region: str, latencies, *, fail: int = 0,
            category="timeout", start=None, step_s=30):
    """Insert synthetic results directly: successes with ``latencies`` then ``fail`` failures."""
    start = start or datetime.now(UTC) - timedelta(hours=1)
    rows = [(lat, True) for lat in latencies] + [(None, False)] * fail
    for i, (lat, ok) in enumerate(rows):
        db.add(Result(id=uuid.uuid4(), job_id=uuid.uuid4(), workspace_id=workspace_id, monitor_id=monitor["id"],
                      agent_id=agent_id, region=region, check_type="http", target=monitor["target"],
                      measured_at=start + timedelta(seconds=i * step_s), success=ok,
                      failure_category=None if ok else category, latency_ms=lat, details={}))
    db.commit()


def test_regional_comparison_percentiles_and_missing_data(client, account, agent_a, agent_b, db):
    m = account.create_monitor(regions=["test-a", "test-b"])
    ws = account.workspace_id
    _insert(db, m, ws, agent_a["agent_id"], "test-a", [float(x) for x in range(1, 21)])
    _insert(db, m, ws, agent_b["agent_id"], "test-b", [100.0, 300.0], fail=1, category="connection_refused")
    # Two jobs in test-b never produced a result.
    for _ in range(2):
        db.add(Job(workspace_id=ws, monitor_id=m["id"], region="test-b", check_type="http", payload={},
                   scheduled_for=datetime.now(UTC) - timedelta(minutes=uuid.uuid4().int % 50 + 1),
                   expires_at=datetime.now(UTC), status="expired", expire_reason="unclaimed"))
    db.commit()

    r = account.get(f"/v1/monitors/{m['id']}/regional-performance").json()
    a, b = r["regions"]
    assert a["region"] == "test-a" and a["total"] == 20 and a["success_rate"] == 1.0
    assert a["latency"] == {"sample_count": 20, "min_ms": 1.0, "p50_ms": 10.5, "p95_ms": 19.05, "p99_ms": None,
                            "avg_ms": 10.5}
    assert a["insufficient_data"] is False

    assert b["total"] == 3 and b["successes"] == 2 and b["success_rate"] == round(2 / 3, 4)
    assert b["missing"] == 2
    assert b["latency"]["p50_ms"] is None and b["latency"]["min_ms"] == 100.0  # 2 samples: no median
    assert b["insufficient_data"] is True

    assert r["overall"]["total"] == 23 and r["overall"]["missing"] == 2
    assert r["failure_categories"] == {"connection_refused": 1}
    assert r["assessment"] == "regional"
    assert len(r["agents"]) == 2 and r["agents"][0]["agent_id"] in (agent_a["agent_id"], agent_b["agent_id"])

    only_a = account.get(f"/v1/monitors/{m['id']}/regional-performance?region=test-a").json()
    assert [g["region"] for g in only_a["regions"]] == ["test-a"] and only_a["overall"]["total"] == 20


def test_regions_without_data_are_reported_not_hidden(client, account, agent_a, agent_b, db):
    m = account.create_monitor(regions=["test-a", "test-b"])
    r = account.get(f"/v1/monitors/{m['id']}/regional-performance").json()
    assert r["assessment"] == "no_data"
    assert all(g["total"] == 0 and g["success_rate"] is None for g in r["regions"])


def test_global_outage_and_slow_region_detection(client, account, agent_a, agent_b, db):
    from tests.conftest import enroll_agent

    agent_c = enroll_agent(client, "test-c")
    m = account.create_monitor(regions=["test-a", "test-b", "test-c"])
    ws = account.workspace_id
    _insert(db, m, ws, agent_a["agent_id"], "test-a", [20.0] * 10)
    _insert(db, m, ws, agent_b["agent_id"], "test-b", [22.0] * 10)
    _insert(db, m, ws, agent_c["agent_id"], "test-c", [90.0] * 10)
    r = account.get(f"/v1/monitors/{m['id']}/regional-performance").json()
    assert r["assessment"] == "healthy" and r["slow_regions"] == ["test-c"]

    # A region that stopped reporting is not "healthy" even if its few results succeeded.
    m3 = account.create_monitor(regions=["test-a", "test-b"])
    _insert(db, m3, ws, agent_a["agent_id"], "test-a", [20.0] * 10)
    _insert(db, m3, ws, agent_b["agent_id"], "test-b", [20.0])
    for i in range(2):
        db.add(Job(workspace_id=ws, monitor_id=m3["id"], region="test-b", check_type="http", payload={},
                   scheduled_for=datetime.now(UTC) - timedelta(minutes=i + 1), expires_at=datetime.now(UTC),
                   status="expired", expire_reason="unclaimed"))
    db.commit()
    assert account.get(f"/v1/monitors/{m3['id']}/regional-performance").json()["assessment"] == "incomplete"

    m2 = account.create_monitor(regions=["test-a", "test-b"])
    _insert(db, m2, ws, agent_a["agent_id"], "test-a", [], fail=5)
    _insert(db, m2, ws, agent_b["agent_id"], "test-b", [10.0], fail=5)
    assert account.get(f"/v1/monitors/{m2['id']}/regional-performance").json()["assessment"] == "global"


def test_timeseries_buckets_per_region(client, account, agent_a, agent_b, db):
    m = account.create_monitor(regions=["test-a", "test-b"])
    start = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    _insert(db, m, account.workspace_id, agent_a["agent_id"], "test-a", [10.0, 20.0, 30.0, 40.0], start=start,
            step_s=30)  # 12:00:00, 12:00:30, 12:01:00, 12:01:30
    _insert(db, m, account.workspace_id, agent_b["agent_id"], "test-b", [5.0], fail=1, start=start, step_s=90)
    r = account.get(f"/v1/monitors/{m['id']}/timeseries", params={
        "since": "2026-10-01T12:00:00Z", "until": "2026-10-01T12:05:00Z", "bucket_seconds": 60}).json()
    assert r["bucket_seconds"] == 60
    series = {s["key"]: s["points"] for s in r["series"]}
    assert [(p["total"], p["avg_ms"]) for p in series["test-a"]] == [(2, 15.0), (2, 35.0)]
    assert [(p["total"], p["successes"]) for p in series["test-b"]] == [(1, 1), (1, 0)]
    assert series["test-a"][0]["p50_ms"] is None  # 2 samples per bucket: below the minimum

    bad = account.get(f"/v1/monitors/{m['id']}/timeseries",
                      params={"since": "2026-10-02T00:00:00Z", "until": "2026-10-01T00:00:00Z"})
    assert bad.status_code == 422


def test_overview(client, account, agent_a, db):
    m = account.create_monitor()
    _insert(db, m, account.workspace_id, agent_a["agent_id"], "test-a", [10.0] * 9, fail=1)
    o = account.get("/v1/overview").json()
    assert o["monitors"]["total"] == 1 and o["availability_pct"] == 90.0
    assert o["latency"]["sample_count"] == 9 and o["latency"]["p50_ms"] == 10.0 and o["latency"]["p95_ms"] is None
    assert o["regions"][0]["region"] == "test-a" and o["regions"][0]["total"] == 10
    assert len(o["recent_measurements"]) == 10 and o["agents"]["online"] == 1
