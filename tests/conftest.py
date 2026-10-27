"""Shared fixtures. Tests run against a real PostgreSQL database.

Set PROBEATLAS_TEST_DATABASE_URL to point elsewhere; the default matches
`docker compose up -d db` (see README).
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

TEST_DB = os.environ.get(
    "PROBEATLAS_TEST_DATABASE_URL", "postgresql+psycopg://probeatlas:probeatlas@localhost:5432/probeatlas_test"
)
BOOTSTRAP_TOKEN = "pae_test-bootstrap-token-0000000000"
os.environ["PROBEATLAS_DATABASE_URL"] = TEST_DB
os.environ["PROBEATLAS_BOOTSTRAP_ENROLLMENT_TOKEN"] = BOOTSTRAP_TOKEN
os.environ["PROBEATLAS_LOG_FORMAT"] = "text"

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from probeatlas.server.config import get_settings  # noqa: E402
from probeatlas.server.db import new_session  # noqa: E402
from probeatlas.server.ratelimit import limiter  # noqa: E402

TABLES = [
    "audit_events", "notification_deliveries", "notification_channels", "incident_events", "incidents",
    "alert_states", "alert_rules", "results", "jobs", "monitors", "agents", "enrollment_tokens", "api_keys",
    "memberships", "user_sessions", "workspaces", "users",
]


@pytest.fixture(scope="session", autouse=True)
def database():
    """Create the test database if needed and migrate it to head once per session."""
    url = make_url(TEST_DB)
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        exists = conn.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": url.database}).scalar()
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    admin.dispose()
    from probeatlas.server.admin import migrate

    migrate()
    yield


@pytest.fixture(autouse=True)
def clean_db():
    """Truncate every table and reset rate limits after each test."""
    yield
    with new_session() as db:
        db.execute(text(f"TRUNCATE {', '.join(TABLES)} CASCADE"))
        db.commit()
    limiter.reset()


@pytest.fixture
def settings(monkeypatch):
    """Return a helper that overrides settings fields for the duration of one test."""
    s = get_settings()
    return _SettingsPatcher(s, monkeypatch)


class _SettingsPatcher:
    """Wraps the cached settings so tests can override fields via ``monkeypatch``."""

    def __init__(self, s, mp):
        """Wrap settings ``s`` using the monkeypatch fixture ``mp``."""
        self._s, self._mp = s, mp

    def set(self, **kw):
        """Override the given settings fields until the test ends and return the settings."""
        for k, v in kw.items():
            self._mp.setattr(self._s, k, v)
        return self._s

    def __getattr__(self, item):
        """Read through to the wrapped settings."""
        return getattr(self._s, item)


@pytest.fixture
def client():
    """Return a ``TestClient`` for a freshly created app (runs startup hooks)."""
    from probeatlas.server.main import create_app

    with TestClient(create_app()) as c:
        yield c


@pytest.fixture
def db():
    """Return an ORM session on the test database."""
    with new_session() as s:
        yield s


# --- API helpers ---------------------------------------------------------------


class Account:
    """A registered user with their first workspace; sends session-authenticated requests."""

    def __init__(self, client: TestClient, email: str):
        """Register ``email`` through the API and store the session headers."""
        r = client.post("/v1/auth/register", json={"email": email, "password": "correct horse battery",
                                                   "name": email.split("@")[0], "workspace_name": f"ws-{email}"})
        assert r.status_code == 201, r.text
        body = r.json()
        self.client = client
        self.token = body["token"]
        self.workspace_id = body["workspaces"][0]["id"]
        self.headers = {"Authorization": f"Bearer {self.token}", "X-Workspace-Id": self.workspace_id}

    def get(self, path, **kw):
        """Send an authenticated GET."""
        return self.client.get(path, headers=self.headers, **kw)

    def post(self, path, json=None, **kw):
        """Send an authenticated POST with a JSON body."""
        return self.client.post(path, headers=self.headers, json=json, **kw)

    def patch(self, path, json=None):
        """Send an authenticated PATCH with a JSON body."""
        return self.client.patch(path, headers=self.headers, json=json)

    def delete(self, path):
        """Send an authenticated DELETE."""
        return self.client.delete(path, headers=self.headers)

    def api_key(self) -> str:
        """Create an API key in this account's workspace and return the secret."""
        r = self.post("/v1/api-keys", {"name": "test"})
        assert r.status_code == 201, r.text
        return r.json()["key"]

    def create_monitor(self, **overrides) -> dict:
        """Create an HTTP monitor in region ``test-a`` (fields can be overridden) and return it."""
        body = {"name": "site", "type": "http", "target": "https://example.com/health", "interval_seconds": 60,
                "timeout_ms": 5000, "regions": ["test-a"]}
        body.update(overrides)
        r = self.post("/v1/monitors", body)
        assert r.status_code == 201, r.text
        return r.json()


@pytest.fixture
def account(client):
    """The primary test tenant."""
    return Account(client, "alice@example.com")


@pytest.fixture
def other_account(client):
    """A second tenant, used to prove tenant isolation."""
    return Account(client, "mallory@example.org")


def enroll_agent(client: TestClient, region: str = "test-a", name: str | None = None,
                 capabilities=("http", "dns", "tcp")) -> dict:
    """Enroll an agent in ``region`` with the bootstrap token; returns its id, token, and headers."""
    r = client.post(
        "/agent/v1/register",
        headers={"Authorization": f"Bearer {BOOTSTRAP_TOKEN}"},
        json={"name": name or f"agent-{region}-{uuid.uuid4().hex[:4]}", "region": region, "version": "0.1.0",
              "capabilities": list(capabilities), "country_code": "DE", "city": "Frankfurt"},
    )
    assert r.status_code == 201, r.text
    data = r.json()
    data["headers"] = {"Authorization": f"Bearer {data['agent_token']}"}
    return data


@pytest.fixture
def agent_a(client):
    """An agent enrolled in region ``test-a``."""
    return enroll_agent(client, "test-a")


@pytest.fixture
def agent_b(client):
    """An agent enrolled in region ``test-b``."""
    return enroll_agent(client, "test-b")


def make_result(job: dict, *, success=True, latency=42.0, category=None, http_status=200, **extra) -> dict:
    """Build a result payload for a claimed ``job``; keyword arguments override fields."""
    out = {
        "schema_version": 1,
        "result_id": str(uuid.uuid4()),
        "job_id": job["job_id"],
        "check_type": job["check_type"],
        "target": job["target"],
        "measured_at": datetime.now(UTC).isoformat(),
        "success": success,
        "failure_category": category if not success else None,
        "latency_ms": latency if success else None,
        "http_status": http_status,
    }
    out.update(extra)
    return out


def run_scheduler(now: datetime | None = None) -> int:
    """Run one scheduling pass and return the number of jobs created."""
    from probeatlas.server.scheduling import schedule_due

    with new_session() as s:
        return schedule_due(s, now=now)


def force_due(monitor_id: str) -> None:
    """Make a monitor due now, using a slot earlier than any existing job so slots never collide."""
    with new_session() as s:
        # Each call uses a distinct (earlier) slot so rapid test cycles never collide on the
        # unique (monitor, region, scheduled_for) constraint.
        s.execute(text(
            "UPDATE monitors SET next_run_at = COALESCE("
            " (SELECT min(scheduled_for) FROM jobs WHERE monitor_id = :id) - interval '1 second',"
            " now() - interval '1 second') WHERE id = :id"), {"id": monitor_id})
        s.commit()


def cycle(client: TestClient, agent: dict, monitor_id: str, **result_kw) -> dict:
    """Schedule one round, claim the job as ``agent``, submit a result. Returns the ack."""
    force_due(monitor_id)
    run_scheduler()
    jobs = client.post("/agent/v1/jobs/claim", headers=agent["headers"], json={"max_jobs": 10}).json()["jobs"]
    jobs = [j for j in jobs if j["monitor_id"] == monitor_id]
    assert jobs, "no job claimed"
    r = client.post("/agent/v1/results", headers=agent["headers"],
                    json={"results": [make_result(jobs[0], **result_kw)]})
    assert r.status_code == 200, r.text
    return r.json()["acks"][0]


# --- controlled local HTTP server ----------------------------------------------


class TargetServer:
    """Deterministic local HTTP endpoint for checks and webhook delivery tests."""

    def __init__(self):
        """Start the server on a random localhost port in a background thread."""
        self.requests: list[dict] = []
        self.status = 200
        server = self

        class Handler(BaseHTTPRequestHandler):
            """Routes: ``/status/<code>``, ``/redirect?to=<url>``, ``/slow?ms=<n>``; others return ``status``."""

            def log_message(self, *a):
                """Silence the default request logging."""
                pass

            def _respond(self, method: str):
                """Record the request and answer according to the route."""
                parts = urlsplit(self.path)
                q = parse_qs(parts.query)
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                server.requests.append({"method": method, "path": parts.path, "headers": dict(self.headers),
                                        "body": body})
                if parts.path.startswith("/status/"):
                    code = int(parts.path.rsplit("/", 1)[1])
                elif parts.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", q["to"][0])
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                elif parts.path == "/slow":
                    time.sleep(int(q.get("ms", ["500"])[0]) / 1000)
                    code = 200
                else:
                    code = server.status
                payload = json.dumps({"ok": code < 400}).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if method != "HEAD":
                    self.wfile.write(payload)

            def do_GET(self):
                """Handle GET."""
                self._respond("GET")

            def do_HEAD(self):
                """Handle HEAD (no body)."""
                self._respond("HEAD")

            def do_POST(self):
                """Handle POST (used by webhook tests)."""
                self._respond("POST")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def url(self, path: str = "/", host: str = "127.0.0.1") -> str:
        """Return the URL of ``path`` on this server."""
        return f"http://{host}:{self.port}{path}"

    def close(self):
        """Stop the server and release its port."""
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def target_server():
    """A running :class:`TargetServer` for the duration of one test."""
    s = TargetServer()
    yield s
    s.close()


def past(seconds: float) -> datetime:
    """Return the UTC time ``seconds`` ago."""
    return datetime.now(UTC) - timedelta(seconds=seconds)
