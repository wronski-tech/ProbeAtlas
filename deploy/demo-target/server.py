"""Controllable HTTP endpoint for local demos (development only).

Agents measure this server for real; its behaviour is switched through a
control API so failures and regional outages can be demonstrated
deterministically.

    GET  :8080/            -> 200 (or the configured failure)
    GET  :8080/slow?ms=N   -> delays the response
    GET  :8081/control     -> current mode (published on the host as 127.0.0.1:18081)
    POST :8081/control     {"mode": "ok" | "fail" | "slow", "status": 503, "delay_ms": 800,
                            "clients": ["10.203.10.11"]}   # optional: only affect these agent IPs
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

STATE = {"mode": "ok", "status": 503, "delay_ms": 800, "clients": []}
LOCK = threading.Lock()


def _affected(client_ip: str) -> bool:
    return not STATE["clients"] or client_ip in STATE["clients"]


class Target(BaseHTTPRequestHandler):
    server_version = "probeatlas-demo-target"

    def log_message(self, fmt, *args):
        print(json.dumps({"client": self.client_address[0], "request": fmt % args}), flush=True)

    def _send(self, code: int, body: dict):
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def do_GET(self):
        parts = urlsplit(self.path)
        with LOCK:
            state = dict(STATE)
        if parts.path == "/slow":
            time.sleep(int(parse_qs(parts.query).get("ms", ["500"])[0]) / 1000)
        if _affected(self.client_address[0]):
            if state["mode"] == "fail":
                return self._send(state["status"], {"ok": False, "mode": "fail"})
            if state["mode"] == "slow":
                time.sleep(state["delay_ms"] / 1000)
        self._send(200, {"ok": True, "path": parts.path})

    do_HEAD = do_GET


class Control(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body: dict, code: int = 200):
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        with LOCK:
            self._send(dict(STATE))

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if body.get("mode", "ok") not in ("ok", "fail", "slow"):
            return self._send({"error": "mode must be ok, fail or slow"}, 400)
        with LOCK:
            STATE.update({"mode": body.get("mode", "ok"), "status": int(body.get("status", 503)),
                          "delay_ms": int(body.get("delay_ms", 800)), "clients": list(body.get("clients", []))})
            print(json.dumps({"control": STATE}), flush=True)
            self._send(dict(STATE))


if __name__ == "__main__":
    threading.Thread(target=ThreadingHTTPServer(("0.0.0.0", 8081), Control).serve_forever, daemon=True).start()  # noqa: S104
    print("demo target listening on :8080 (control :8081)", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 8080), Target).serve_forever()  # noqa: S104
