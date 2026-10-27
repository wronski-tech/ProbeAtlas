"""ProbeAtlas command-line client. All output is JSON on stdout; errors are JSON on stderr.

Configuration precedence: flags > environment (PROBEATLAS_API_KEY, PROBEATLAS_API_URL) >
config file written by ``probeatlas login`` (~/.config/probeatlas/config.json, mode 0600).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx

DEFAULT_URL = "http://localhost:8000"


def config_path() -> Path:
    """Return the CLI config file path (``$XDG_CONFIG_HOME/probeatlas/config.json``)."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "probeatlas" / "config.json"


def load_config() -> dict[str, str]:
    """Load the config file and apply ``PROBEATLAS_API_KEY`` / ``PROBEATLAS_API_URL`` overrides."""
    path = config_path()
    cfg = json.loads(path.read_text()) if path.exists() else {}
    if os.environ.get("PROBEATLAS_API_KEY"):
        cfg["api_key"] = os.environ["PROBEATLAS_API_KEY"]
    if os.environ.get("PROBEATLAS_API_URL"):
        cfg["api_url"] = os.environ["PROBEATLAS_API_URL"]
    cfg.setdefault("api_url", DEFAULT_URL)
    return cfg


def save_config(cfg: dict[str, str]) -> None:
    """Write the CLI config file with mode 0600, since it contains an API key."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(cfg, fh)


def make_client(api_url: str, api_key: str) -> httpx.Client:
    """Return an HTTP client that authenticates to ``api_url`` with ``api_key``."""
    return httpx.Client(base_url=api_url, timeout=30, headers={"Authorization": f"Bearer {api_key}",
                                                               "User-Agent": "probeatlas-cli/0.1"})


class CliError(Exception):
    """An error to print as JSON on stderr before exiting with ``code`` (1 = API error, 2 = usage)."""

    def __init__(self, payload: Any, code: int = 1):
        """Store the JSON-serializable error ``payload`` and the process exit ``code``."""
        super().__init__(str(payload))
        self.payload, self.code = payload, code


def call(client: httpx.Client, method: str, path: str, **kw) -> Any:
    """Send an API request and return the decoded JSON body.

    Returns ``{"ok": true}`` for 204 responses. Raises :class:`CliError` with the API's error envelope for
    4xx/5xx responses and with ``connection_error`` when the API is unreachable.
    """
    try:
        resp = client.request(method, path, **kw)
    except httpx.HTTPError as exc:
        raise CliError({"error": {"code": "connection_error", "message": str(exc)}}) from exc
    if resp.status_code == 204:
        return {"ok": True}
    try:
        body = resp.json()
    except ValueError:
        body = {"error": {"code": "invalid_response", "message": resp.text[:300]}}
    if resp.status_code >= 400:
        raise CliError(body)
    return body


def _monitor_body(a: argparse.Namespace) -> dict[str, Any]:
    """Translate ``monitors create`` arguments into a ``POST /v1/monitors`` request body."""
    settings: dict[str, Any] = {}
    if a.type == "http":
        if a.expected_status:
            settings["expected_status_codes"] = a.expected_status
        if a.method:
            settings["method"] = a.method
    elif a.type == "tcp":
        if a.port is None:
            raise CliError({"error": {"code": "usage", "message": "--port is required for tcp monitors"}}, 2)
        settings["port"] = a.port
    elif a.type == "dns" and a.record_type:
        settings["record_type"] = a.record_type
    elif a.type == "icmp" and a.packet_count:
        settings["packet_count"] = a.packet_count
    body: dict[str, Any] = {"name": a.name, "type": a.type, "target": a.target, "regions": a.region,
                            "interval_seconds": a.interval, "timeout_ms": a.timeout_ms, "settings": settings}
    if a.latency_threshold_ms:
        body["latency_threshold_ms"] = a.latency_threshold_ms
    return body


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for every CLI command."""
    p = argparse.ArgumentParser(prog="probeatlas", description="ProbeAtlas CLI (JSON output)")
    p.add_argument("--api-url", help=f"control plane URL (default {DEFAULT_URL})")
    p.add_argument("--api-key", help="API key (pak_...)")
    p.add_argument("--compact", action="store_true", help="single-line JSON")
    sub = p.add_subparsers(dest="cmd", required=True)

    login = sub.add_parser("login", help="verify and store an API key")
    login.add_argument("--api-key", dest="login_key", required=True)

    mon = sub.add_parser("monitors", help="manage monitors").add_subparsers(dest="action", required=True)
    ls = mon.add_parser("list")
    ls.add_argument("--state", choices=["up", "degraded", "down", "unknown", "paused"])
    ls.add_argument("--limit", type=int, default=50)
    ls.add_argument("--cursor")
    cr = mon.add_parser("create")
    cr.add_argument("--name", required=True)
    cr.add_argument("--type", required=True, choices=["http", "dns", "tcp", "icmp"])
    cr.add_argument("--target", required=True, help="URL for http; hostname or IP otherwise")
    cr.add_argument("--region", action="append", required=True, help="repeat for multiple regions")
    cr.add_argument("--interval", type=int, default=60)
    cr.add_argument("--timeout-ms", type=int, default=10_000)
    cr.add_argument("--latency-threshold-ms", type=float)
    cr.add_argument("--expected-status", action="append", help="e.g. 200, 200-299, 2xx (repeatable)")
    cr.add_argument("--method", choices=["GET", "HEAD"])
    cr.add_argument("--port", type=int)
    cr.add_argument("--record-type", choices=["A", "AAAA", "CNAME", "MX", "TXT", "NS"])
    cr.add_argument("--packet-count", type=int)
    for name in ("get", "status", "pause", "resume", "delete"):
        mon.add_parser(name).add_argument("monitor_id")

    meas = sub.add_parser("measurements", help="query measurement history").add_subparsers(dest="action",
                                                                                          required=True)
    ml = meas.add_parser("list")
    ml.add_argument("--monitor")
    ml.add_argument("--region")
    ml.add_argument("--agent")
    ml.add_argument("--type", choices=["http", "dns", "tcp", "icmp"])
    ml.add_argument("--since", help="ISO 8601 timestamp")
    ml.add_argument("--until", help="ISO 8601 timestamp")
    ml.add_argument("--failed", action="store_true", help="only failed measurements")
    ml.add_argument("--limit", type=int, default=20)

    sub.add_parser("locations", help="list monitoring regions")
    inc = sub.add_parser("incidents", help="list incidents")
    inc.add_argument("--status", choices=["open", "resolved"])
    return p


def run(argv: list[str] | None = None) -> Any:
    """Execute the CLI command in ``argv`` and return its JSON-serializable result."""
    args = build_parser().parse_args(argv)
    cfg = load_config()
    api_url = args.api_url or cfg["api_url"]

    if args.cmd == "login":
        with make_client(api_url, args.login_key) as c:
            call(c, "GET", "/v1/monitors", params={"limit": 1})
        save_config({"api_url": api_url, "api_key": args.login_key})
        return {"ok": True, "api_url": api_url, "config": str(config_path())}

    api_key = args.api_key or cfg.get("api_key")
    if not api_key:
        raise CliError({"error": {"code": "not_authenticated",
                                  "message": "run `probeatlas login --api-key ...` or set PROBEATLAS_API_KEY"}}, 2)
    with make_client(api_url, api_key) as c:
        if args.cmd == "monitors":
            if args.action == "list":
                params = {"limit": args.limit, "state": args.state, "cursor": args.cursor}
                return call(c, "GET", "/v1/monitors", params={k: v for k, v in params.items() if v})
            if args.action == "create":
                return call(c, "POST", "/v1/monitors", json=_monitor_body(args))
            mid = args.monitor_id
            if args.action == "get":
                return call(c, "GET", f"/v1/monitors/{mid}")
            if args.action == "status":
                monitor = call(c, "GET", f"/v1/monitors/{mid}")
                perf = call(c, "GET", f"/v1/monitors/{mid}/regional-performance")
                open_inc = call(c, "GET", "/v1/incidents", params={"monitor_id": mid, "status": "open"})
                return {
                    "monitor": {k: monitor[k] for k in ("id", "name", "type", "target", "state", "last_result_at")},
                    "window": {"since": perf["since"], "until": perf["until"]},
                    "assessment": perf["assessment"],
                    "slow_regions": perf["slow_regions"],
                    "regions": [{"region": g["region"], "success_rate": g["success_rate"], "samples": g["total"],
                                 "missing": g["missing"], "p50_ms": g["latency"]["p50_ms"],
                                 "p95_ms": g["latency"]["p95_ms"], "last_measured_at": g["last_measured_at"]}
                                for g in perf["regions"]],
                    "open_incidents": open_inc["items"],
                }
            if args.action in ("pause", "resume"):
                return call(c, "POST", f"/v1/monitors/{mid}/{args.action}")
            if args.action == "delete":
                return call(c, "DELETE", f"/v1/monitors/{mid}")
        if args.cmd == "measurements":
            params = {"monitor_id": args.monitor, "region": args.region, "agent_id": args.agent,
                      "check_type": args.type, "since": args.since, "until": args.until, "limit": args.limit}
            if args.failed:
                params["success"] = "false"
            return call(c, "GET", "/v1/measurements", params={k: v for k, v in params.items() if v})
        if args.cmd == "locations":
            return call(c, "GET", "/v1/locations")
        if args.cmd == "incidents":
            return call(c, "GET", "/v1/incidents", params={"status": args.status} if args.status else {})
    raise CliError({"error": {"code": "usage", "message": "unknown command"}}, 2)


def main(argv: list[str] | None = None) -> None:
    """CLI entry point (``probeatlas``): print the result as JSON, or the error as JSON on stderr."""
    compact = "--compact" in (argv if argv is not None else sys.argv[1:])
    try:
        out = run(argv)
    except CliError as exc:
        print(json.dumps(exc.payload, indent=None if compact else 2), file=sys.stderr)
        sys.exit(exc.code)
    print(json.dumps(out, indent=None if compact else 2, default=str))


if __name__ == "__main__":
    main()
