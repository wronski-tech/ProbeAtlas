"""Authentication, sessions, API keys, and rate limiting."""

from __future__ import annotations

from sqlalchemy import select, text

from probeatlas.server.models import ApiKey


def test_register_login_me_logout(client, account):
    r = client.post("/v1/auth/login", json={"email": "ALICE@example.com", "password": "correct horse battery"})
    assert r.status_code == 200
    token = r.json()["token"]
    assert token.startswith("pas_")
    me = client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.json()["user"]["email"] == "alice@example.com"
    assert me.json()["workspaces"][0]["role"] == "owner"
    assert client.post("/v1/auth/logout", headers={"Authorization": f"Bearer {token}"}).status_code == 204
    assert client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_bad_credentials_and_duplicates(client, account):
    r = client.post("/v1/auth/login", json={"email": "alice@example.com", "password": "wrong password!"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_credentials"
    r = client.post("/v1/auth/login", json={"email": "nobody@example.com", "password": "wrong password!"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_credentials"
    r = client.post("/v1/auth/register", json={"email": "alice@example.com", "password": "another long pw",
                                               "name": "A"})
    assert r.status_code == 409
    r = client.post("/v1/auth/register", json={"email": "bob@example.com", "password": "short", "name": "B"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"


def test_passwords_and_keys_are_not_stored_in_plaintext(account, db):
    key = account.api_key()
    row = db.execute(select(ApiKey)).scalar_one()
    assert key not in (row.key_hash, row.prefix) and key.startswith(row.prefix + "_")
    pw = db.execute(text("SELECT password_hash FROM users")).scalar_one()
    assert pw.startswith("scrypt$") and "correct horse" not in pw


def test_api_key_lifecycle(client, account):
    key = account.api_key()
    h = {"Authorization": f"Bearer {key}"}
    assert client.get("/v1/monitors", headers=h).status_code == 200

    keys = account.get("/v1/api-keys").json()
    assert len(keys) == 1 and "key" not in keys[0]
    key_id = keys[0]["id"]

    rotated = account.post(f"/v1/api-keys/{key_id}/rotate")
    assert rotated.status_code == 201
    new_key = rotated.json()["key"]
    assert client.get("/v1/monitors", headers=h).status_code == 401
    h2 = {"Authorization": f"Bearer {new_key}"}
    assert client.get("/v1/monitors", headers=h2).status_code == 200

    assert account.delete(f"/v1/api-keys/{rotated.json()['id']}").status_code == 204
    r = client.get("/v1/monitors", headers=h2)
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"


def test_api_key_cannot_manage_keys_or_sessions(client, account):
    h = {"Authorization": f"Bearer {account.api_key()}"}
    assert client.post("/v1/api-keys", headers=h, json={"name": "escalate"}).status_code == 403
    assert client.get("/v1/auth/me", headers=h).status_code == 403


def test_expired_api_key_rejected(client, account, db):
    key = account.api_key()
    db.execute(text("UPDATE api_keys SET expires_at = now() - interval '1 minute'"))
    db.commit()
    assert client.get("/v1/monitors", headers={"Authorization": f"Bearer {key}"}).status_code == 401


def test_missing_and_malformed_tokens(client, account):
    assert client.get("/v1/monitors").status_code == 401
    assert client.get("/v1/monitors", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/v1/monitors", headers={"Authorization": "Bearer pak_abc_def"}).status_code == 401
    # Session tokens need an explicit workspace.
    r = client.get("/v1/monitors", headers={"Authorization": f"Bearer {account.token}"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "workspace_required"


def test_member_role_cannot_perform_owner_actions(client, account):
    from tests.conftest import Account

    bob = Account(client, "bob@example.com")
    assert account.post("/v1/members", {"email": "bob@example.com"}).status_code == 201
    bob_in_alice = {"Authorization": f"Bearer {bob.token}", "X-Workspace-Id": account.workspace_id}
    assert client.get("/v1/monitors", headers=bob_in_alice).status_code == 200
    assert client.post("/v1/api-keys", headers=bob_in_alice, json={"name": "x"}).status_code == 403
    # The last owner cannot be removed.
    members = account.get("/v1/members").json()
    owner_id = next(m["user_id"] for m in members if m["role"] == "owner")
    assert account.delete(f"/v1/members/{owner_id}").status_code == 409


def test_auth_endpoints_are_rate_limited(client, settings):
    settings.set(auth_rate_limit_per_minute=3)
    codes = [client.post("/v1/auth/login", json={"email": "x@example.com", "password": "whatever123"}).status_code
             for _ in range(5)]
    assert codes[:3] == [401, 401, 401] and codes[3] == 429
    r = client.post("/v1/auth/login", json={"email": "x@example.com", "password": "whatever123"})
    assert r.json()["error"]["code"] == "rate_limited" and int(r.headers["Retry-After"]) >= 1


def test_workspace_api_rate_limit(client, account, settings):
    settings.set(rate_limit_per_minute=5)
    codes = [account.get("/v1/monitors").status_code for _ in range(7)]
    assert codes.count(200) == 5 and codes[-1] == 429


def test_audit_log_records_security_events(account):
    account.api_key()
    actions = {e["action"] for e in account.get("/v1/audit-events").json()}
    assert {"user.registered", "api_key.created"} <= actions


def test_secrets_are_redacted_from_log_messages_and_structured_fields():
    import json
    import logging

    from probeatlas.common.logs import JsonFormatter, log_fields

    key = "pak_0123456789ab_" + "s" * 40
    record = logging.LogRecord("t", logging.WARNING, __file__, 1, f"rejected {key}", None, None)
    record.fields = log_fields(error=f"401 Authorization: Bearer {key}", count=3)["fields"]
    out = json.loads(JsonFormatter("api").format(record))
    assert key not in json.dumps(out)
    assert out["msg"] == "rejected [REDACTED]" and out["count"] == 3
