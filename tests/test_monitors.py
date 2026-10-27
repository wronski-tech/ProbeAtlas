"""Monitor CRUD, validation, SSRF checks at configuration time, and pagination."""

from __future__ import annotations

import pytest


def test_create_get_update_pause_resume_delete(account, agent_a, agent_b):
    m = account.create_monitor(name="API", target="https://API.example.com/health?x=1",
                               regions=["test-a", "TEST-B", "test-a"],
                               settings={"expected_status_codes": ["200", "3xx"]}, latency_threshold_ms=300)
    assert m["target"] == "https://api.example.com/health?x=1"
    assert m["regions"] == ["test-a", "test-b"]
    assert m["state"] == "unknown" and m["config_version"] == 1
    assert m["settings"] == {"method": "GET", "expected_status_codes": ["200", "3xx"], "follow_redirects": True,
                             "max_redirects": 5}

    r = account.patch(f"/v1/monitors/{m['id']}", {"interval_seconds": 120, "regions": ["test-b"]})
    assert r.status_code == 200
    assert r.json()["interval_seconds"] == 120 and r.json()["regions"] == ["test-b"]
    assert r.json()["config_version"] == 2

    assert account.post(f"/v1/monitors/{m['id']}/pause").json()["state"] == "paused"
    assert account.get("/v1/monitors?state=paused").json()["items"][0]["id"] == m["id"]
    assert account.post(f"/v1/monitors/{m['id']}/resume").json()["state"] == "unknown"
    assert account.delete(f"/v1/monitors/{m['id']}").status_code == 204
    assert account.get(f"/v1/monitors/{m['id']}").status_code == 404


@pytest.mark.parametrize(
    "type_,target,settings",
    [
        ("dns", "example.com", {"record_type": "AAAA"}),
        ("tcp", "db.example.com", {"port": 5432}),
        ("icmp", "8.8.8.8", {"packet_count": 5}),
    ],
)
def test_other_check_types(account, agent_a, type_, target, settings):
    m = account.create_monitor(type=type_, target=target, settings=settings)
    assert m["type"] == type_ and m["target"] == target


@pytest.mark.parametrize(
    "target",
    ["http://127.0.0.1/", "http://169.254.169.254/latest/meta-data/", "http://localhost:8080/",
     "http://metadata.google.internal/", "http://[::1]/", "file:///etc/passwd", "gopher://example.com",
     "http://10.0.0.1/", "http://example.com:22/", "http://admin:pw@example.com/"],
)
def test_prohibited_http_targets_rejected(account, agent_a, target):
    r = account.post("/v1/monitors", {"name": "x", "type": "http", "target": target, "regions": ["test-a"]})
    assert r.status_code == 422, target
    err = r.json()["error"]
    assert err["code"] == "validation_error" and err["details"][0]["loc"] == ["body", "target"]


def test_prohibited_host_targets_rejected(account, agent_a):
    for body in ({"type": "tcp", "target": "192.168.1.1", "settings": {"port": 22}},
                 {"type": "icmp", "target": "localhost"},
                 {"type": "dns", "target": "https://example.com/"},
                 {"type": "dns", "target": "example.com", "settings": {"nameserver": "10.0.0.2"}}):
        r = account.post("/v1/monitors", {"name": "x", "regions": ["test-a"], **body})
        assert r.status_code == 422, body


def test_private_targets_allowed_only_with_explicit_opt_in(account, agent_a, settings):
    body = {"name": "demo", "type": "http", "target": "http://demo-target:8080/ok", "regions": ["test-a"]}
    assert account.post("/v1/monitors", body).status_code == 422
    settings.set(allow_private_targets=True)
    assert account.post("/v1/monitors", body).status_code == 201
    # Even in this mode, metadata IP literals stay blocked.
    body["target"] = "http://169.254.169.254/"
    assert account.post("/v1/monitors", body).status_code == 422


def test_validation_errors(account, agent_a):
    cases = [
        ({"type": "http", "target": "https://example.com", "regions": []}, "regions"),
        ({"type": "http", "target": "https://example.com", "regions": ["nowhere"]}, "regions"),
        ({"type": "tcp", "target": "example.com", "regions": ["test-a"]}, "settings"),  # missing port
        ({"type": "http", "target": "https://example.com", "regions": ["test-a"],
          "settings": {"expected_status_codes": ["2zz"]}}, "settings"),
        ({"type": "http", "target": "https://example.com", "regions": ["test-a"],
          "settings": {"command": "rm -rf /"}}, "settings"),  # unknown settings are rejected
        ({"type": "http", "target": "https://example.com", "regions": ["test-a"], "interval_seconds": 5},
         "interval_seconds"),
        ({"type": "smtp", "target": "example.com", "regions": ["test-a"]}, "type"),
    ]
    for body, field in cases:
        r = account.post("/v1/monitors", {"name": "x", **body})
        assert r.status_code == 422, (body, r.text)
        assert r.json()["error"]["code"] == "validation_error"
        assert field in r.text


def test_workspace_limits(account, agent_a, db):
    from sqlalchemy import text

    db.execute(text("UPDATE workspaces SET max_monitors = 2, min_interval_seconds = 120"))
    db.commit()
    r = account.post("/v1/monitors", {"name": "x", "type": "http", "target": "https://example.com",
                                      "regions": ["test-a"], "interval_seconds": 60})
    assert r.status_code == 422 and "at least 120" in r.json()["error"]["message"]
    account.create_monitor(interval_seconds=120)
    account.create_monitor(interval_seconds=120)
    r = account.post("/v1/monitors", {"name": "x", "type": "http", "target": "https://example.com",
                                      "regions": ["test-a"], "interval_seconds": 120})
    assert r.status_code == 403 and r.json()["error"]["code"] == "limit_exceeded"


def test_cursor_pagination(account, agent_a):
    created = [account.create_monitor(name=f"m{i}")["id"] for i in range(5)]
    seen, cursor, pages = [], None, 0
    while True:
        r = account.get("/v1/monitors", params={"limit": 2, **({"cursor": cursor} if cursor else {})}).json()
        seen += [m["id"] for m in r["items"]]
        pages += 1
        cursor = r["next_cursor"]
        if not cursor:
            break
    assert pages == 3 and len(seen) == 5 and set(seen) == set(created)
    assert seen == list(reversed(created))  # newest first

    r = account.get("/v1/monitors", params={"cursor": "not-a-cursor"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"
    r = account.get("/v1/monitors", params={"limit": 1000})
    assert r.status_code == 422 and r.json()["error"]["details"][0]["loc"] == ["query", "limit"]


def test_type_is_immutable(account, agent_a):
    m = account.create_monitor()
    r = account.patch(f"/v1/monitors/{m['id']}", {"type": "tcp"})
    assert r.status_code == 422


def test_locations_and_agents_listing(account, agent_a, agent_b):
    locs = account.get("/v1/locations").json()
    assert [loc["region"] for loc in locs] == ["test-a", "test-b"]
    assert locs[0]["agents_online"] == 1 and locs[0]["country_codes"] == ["DE"]
    agents = account.get("/v1/agents").json()
    assert {a["region"] for a in agents} == {"test-a", "test-b"}
    assert "token_hash" not in agents[0]


def test_openapi_published(client):
    spec = client.get("/openapi.json").json()
    assert spec["info"]["title"] == "ProbeAtlas API"
    for path in ("/v1/monitors", "/v1/measurements", "/v1/monitors/{monitor_id}/regional-performance",
                 "/v1/alert-rules", "/v1/incidents", "/v1/api-keys", "/v1/agents"):
        assert path in spec["paths"]
