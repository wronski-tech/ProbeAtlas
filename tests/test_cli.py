"""CLI tests: the real argument parsing and output, routed to the in-process API."""

from __future__ import annotations

import json
import stat

import pytest

from probeatlas.cli import main as cli
from tests.conftest import cycle


@pytest.fixture
def cli_env(client, monkeypatch, tmp_path):
    """Isolate the CLI config directory and route CLI HTTP calls to the test app."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("PROBEATLAS_API_KEY", raising=False)

    class Wrapped:
        """Route CLI requests into the in-process app with the CLI's credentials."""

        def __init__(self, api_url, api_key):
            """Remember the API key the CLI would authenticate with."""
            self.headers = {"Authorization": f"Bearer {api_key}"}

        def request(self, method, path, **kw):
            """Forward the request to the test client with the CLI's credentials."""
            return client.request(method, path, headers=self.headers, **kw)

        def __enter__(self):
            """Support ``with make_client(...)``."""
            return self

        def __exit__(self, *a):
            """Do nothing on exit; the test client is owned by the fixture."""
            return False

    monkeypatch.setattr(cli, "make_client", Wrapped)
    return tmp_path


def run(capsys, *argv):
    """Run the CLI with ``argv`` and return its parsed JSON output."""
    cli.main(list(argv))
    return json.loads(capsys.readouterr().out)


def test_cli_workflow(cli_env, capsys, client, account, agent_a):
    key = account.api_key()
    out = run(capsys, "login", "--api-key", key)
    cfg = cli.config_path()
    assert out["ok"] and cfg.exists() and stat.S_IMODE(cfg.stat().st_mode) == 0o600

    m = run(capsys, "monitors", "create", "--name", "api", "--type", "http", "--target", "https://example.com/hz",
            "--region", "test-a", "--expected-status", "2xx", "--latency-threshold-ms", "250")
    assert m["target"] == "https://example.com/hz" and m["settings"]["expected_status_codes"] == ["2xx"]
    assert run(capsys, "monitors", "list")["items"][0]["id"] == m["id"]

    cycle(client, agent_a, m["id"], latency=88.0)
    status = run(capsys, "monitors", "status", m["id"])
    assert status["monitor"]["state"] == "up" and status["regions"][0]["samples"] == 1
    meas = run(capsys, "measurements", "list", "--monitor", m["id"])
    assert meas["items"][0]["latency_ms"] == 88.0
    assert run(capsys, "monitors", "pause", m["id"])["state"] == "paused"


def test_cli_errors_are_json_with_nonzero_exit(cli_env, capsys, account, agent_a):
    with pytest.raises(SystemExit) as exc:
        cli.main(["monitors", "list"])
    assert exc.value.code == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "not_authenticated"

    key = account.api_key()
    with pytest.raises(SystemExit) as exc:
        cli.main(["--api-key", key, "monitors", "create", "--name", "x", "--type", "http",
                  "--target", "http://169.254.169.254/", "--region", "test-a"])
    assert exc.value.code == 1
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "validation_error"
