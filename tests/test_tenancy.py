"""Tenant isolation: no workspace can read or modify another workspace's data."""

from __future__ import annotations

from tests.conftest import cycle


def test_monitors_are_invisible_across_workspaces(client, account, other_account, agent_a):
    m = account.create_monitor()
    mid = m["id"]
    for path in (f"/v1/monitors/{mid}", f"/v1/monitors/{mid}/regional-performance",
                 f"/v1/monitors/{mid}/timeseries", f"/v1/monitors/{mid}/events"):
        r = other_account.get(path)
        assert r.status_code == 404, path
    assert other_account.patch(f"/v1/monitors/{mid}", {"name": "pwned"}).status_code == 404
    assert other_account.post(f"/v1/monitors/{mid}/pause").status_code == 404
    assert other_account.delete(f"/v1/monitors/{mid}").status_code == 404
    assert other_account.get("/v1/monitors").json()["items"] == []
    assert account.get(f"/v1/monitors/{mid}").json()["name"] == "site"


def test_measurements_incidents_and_rules_are_isolated(client, account, other_account, agent_a):
    m = account.create_monitor()
    rule = account.post("/v1/alert-rules", {"monitor_id": m["id"], "kind": "unavailable",
                                            "consecutive_breaches": 1}).json()
    cycle(client, agent_a, m["id"], success=False, category="timeout", http_status=None)
    assert account.get("/v1/measurements").json()["items"]
    assert account.get("/v1/incidents").json()["items"]

    assert other_account.get("/v1/measurements").json()["items"] == []
    assert other_account.get(f"/v1/measurements?monitor_id={m['id']}").json()["items"] == []
    assert other_account.get("/v1/incidents").json()["items"] == []
    inc_id = account.get("/v1/incidents").json()["items"][0]["id"]
    assert other_account.get(f"/v1/incidents/{inc_id}").status_code == 404
    assert other_account.get(f"/v1/alert-rules/{rule['id']}").status_code == 404
    assert other_account.delete(f"/v1/alert-rules/{rule['id']}").status_code == 404
    r = other_account.post("/v1/alert-rules", {"monitor_id": m["id"], "kind": "unavailable"})
    assert r.status_code == 404
    assert other_account.get("/v1/notification-deliveries").json() == []


def test_session_cannot_select_foreign_workspace(client, account, other_account):
    h = {"Authorization": f"Bearer {other_account.token}", "X-Workspace-Id": account.workspace_id}
    assert client.get("/v1/monitors", headers=h).status_code == 404


def test_api_key_is_bound_to_its_workspace(client, account, other_account):
    key = other_account.api_key()
    h = {"Authorization": f"Bearer {key}", "X-Workspace-Id": account.workspace_id}
    assert client.get("/v1/monitors", headers=h).status_code == 403


def test_api_keys_cannot_be_revoked_across_workspaces(account, other_account):
    account.api_key()
    key_id = account.get("/v1/api-keys").json()[0]["id"]
    assert other_account.delete(f"/v1/api-keys/{key_id}").status_code == 404
    assert other_account.post(f"/v1/api-keys/{key_id}/rotate").status_code == 404
    assert account.get("/v1/api-keys").json()[0]["revoked_at"] is None
