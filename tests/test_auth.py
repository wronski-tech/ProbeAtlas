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
    account.invite(bob)
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


def test_forwarded_client_ip_is_used_only_from_trusted_proxies(settings, db):
    from fastapi.testclient import TestClient

    from probeatlas.server.main import create_app

    def failed_logins(client, ip, n):
        """Send ``n`` failed logins that claim to come from ``ip`` and return the status codes."""
        return [client.post("/v1/auth/login", headers={"X-Forwarded-For": ip},
                            json={"email": "x@example.com", "password": "whatever123"}).status_code
                for _ in range(n)]

    settings.set(auth_rate_limit_per_minute=3, trusted_proxies="")
    with TestClient(create_app()) as direct:
        # Untrusted peers cannot pick a fresh rate-limit bucket by inventing X-Forwarded-For values.
        assert failed_logins(direct, "203.0.113.7", 3) == [401, 401, 401]
        assert failed_logins(direct, "198.51.100.9", 1) == [429]

    from probeatlas.server.ratelimit import limiter

    limiter.reset()
    settings.set(trusted_proxies="testclient")  # the TestClient peer plays the web server / load balancer
    with TestClient(create_app()) as behind_proxy:
        # One client's failures no longer lock out everyone behind the same proxy.
        assert failed_logins(behind_proxy, "203.0.113.7", 4) == [401, 401, 401, 429]
        assert failed_logins(behind_proxy, "198.51.100.9", 1) == [401]
    ips = db.execute(text("SELECT DISTINCT ip FROM audit_events WHERE action = 'user.login_failed'")).scalars()
    assert set(ips) == {"testclient", "203.0.113.7", "198.51.100.9"}


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


def test_invitations_need_the_invitees_consent_and_do_not_reveal_accounts(client, account, db):
    from datetime import timedelta

    from tests.conftest import Account

    bob = Account(client, "bob@example.com")
    carol = Account(client, "carol@example.com")
    bob_auth = {"Authorization": f"Bearer {bob.token}"}

    # Same response for a registered and an unregistered email: invitations cannot enumerate accounts.
    for_bob = account.post("/v1/invitations", {"email": "Bob@Example.com"})
    for_nobody = account.post("/v1/invitations", {"email": "nobody@example.com"})
    assert for_bob.status_code == for_nobody.status_code == 201
    assert set(for_bob.json()) == set(for_nobody.json())
    token = for_bob.json()["token"]
    assert token.startswith("pai_")

    # Nothing changes for Bob until he accepts.
    assert [w["id"] for w in client.get("/v1/workspaces", headers=bob_auth).json()] == [bob.workspace_id]
    assert len(account.get("/v1/members").json()) == 1

    # Only the invited account can accept, and only once.
    wrong = client.post("/v1/invitations/accept", json={"token": token},
                        headers={"Authorization": f"Bearer {carol.token}"})
    assert wrong.status_code == 404
    ok = client.post("/v1/invitations/accept", json={"token": token}, headers=bob_auth)
    assert ok.status_code == 200 and ok.json()["id"] == account.workspace_id and ok.json()["role"] == "member"
    assert client.post("/v1/invitations/accept", json={"token": token}, headers=bob_auth).status_code == 404
    assert {m["email"] for m in account.get("/v1/members").json()} == {"alice@example.com", "bob@example.com"}
    # His own workspace stays first, so signing in does not drop him into someone else's workspace.
    assert client.get("/v1/workspaces", headers=bob_auth).json()[0]["id"] == bob.workspace_id

    # Revoked and expired invitations cannot be used; API keys cannot accept on anyone's behalf.
    def accept_as(auth, invitation):
        """Try to accept ``invitation`` with the given Authorization header and return the status code."""
        return client.post("/v1/invitations/accept", json={"token": invitation["token"]}, headers=auth).status_code

    carol_auth = {"Authorization": f"Bearer {carol.token}"}
    revoked = account.post("/v1/invitations", {"email": "carol@example.com"}).json()
    assert account.delete(f"/v1/invitations/{revoked['id']}").status_code == 204
    assert accept_as(carol_auth, revoked) == 404
    expired = account.post("/v1/invitations", {"email": "carol@example.com"}).json()
    db.execute(text("UPDATE invitations SET expires_at = now() - :d WHERE id = :id"),
               {"d": timedelta(seconds=1), "id": expired["id"]})
    db.commit()
    assert accept_as(carol_auth, expired) == 404
    key_auth = {"Authorization": f"Bearer {carol.api_key()}"}
    assert accept_as(key_auth, expired) == 403

    pending = account.get("/v1/invitations").json()
    assert [i["email"] for i in pending] == ["nobody@example.com"]  # accepted, revoked, expired are gone
    assert account.post("/v1/members", {"email": "carol@example.com"}).status_code == 405  # no direct adds


def test_invite_only_signup_requires_a_matching_invitation(client, account, settings, db):
    from probeatlas.server.models import Workspace

    settings.set(registration="invite")

    def register(email, **extra):
        """Try to register ``email`` and return the response."""
        return client.post("/v1/auth/register", json={"email": email, "password": "correct horse battery",
                                                      "name": "New", **extra})

    r = register("dave@example.com")
    assert r.status_code == 403 and r.json()["error"]["code"] == "invitation_required"
    token = account.post("/v1/invitations", {"email": "dave@example.com"}).json()["token"]
    for wrong in ({"invitation_token": token, "email": "eve@example.com"}, {"invitation_token": "pai_nope"}):
        r = register(**{"email": "dave@example.com", **wrong})
        assert r.status_code == 403 and r.json()["error"]["code"] == "invalid_invitation"

    # The invited email signs up straight into the inviting workspace, with no workspace of its own.
    r = register("Dave@Example.com", invitation_token=token, workspace_name="should not exist")
    assert r.status_code == 201, r.text
    assert [(w["id"], w["role"]) for w in r.json()["workspaces"]] == [(account.workspace_id, "member")]
    assert db.execute(select(Workspace).where(Workspace.name == "should not exist")).first() is None
    # The invitation is used up.
    assert register("dave@example.com", invitation_token=token).json()["error"]["code"] == "invalid_invitation"
    assert account.get("/v1/invitations").json() == []


def test_operator_onboards_a_customer_with_an_owner_invitation(client, settings, db):
    import pytest
    from pydantic import ValidationError

    from probeatlas.server.admin import create_workspace

    settings.set(registration="invite")
    with pytest.raises(ValidationError):
        create_workspace(db, "Acme", "not-an-email")
    out = create_workspace(db, "Acme", "Ops@Acme.example")
    assert out["owner_email"] == "ops@acme.example" and out["invite_path"] == f"/invite/{out['invitation_token']}"
    r = client.post("/v1/auth/register", json={"email": "ops@acme.example", "password": "correct horse battery",
                                               "name": "Ops", "invitation_token": out["invitation_token"]})
    assert r.status_code == 201, r.text
    assert [(w["id"], w["name"], w["role"]) for w in r.json()["workspaces"]] == [
        (out["workspace_id"], "Acme", "owner")]


def test_bootstrap_enrollment_token_is_refused_in_production(settings):
    import pytest

    from probeatlas.server.main import bootstrap_enrollment_token

    settings.set(env="production")
    with pytest.raises(SystemExit, match="local development only"):
        bootstrap_enrollment_token()
