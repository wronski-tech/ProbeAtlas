"""Measurement implementations against controlled local servers."""

from __future__ import annotations

import socket
import ssl
import threading
import uuid
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import dns.message
import dns.rcode
import dns.rrset
import pytest

from probeatlas.agent.checks import execute
from probeatlas.agent.checks import icmp as icmp_mod
from probeatlas.agent.checks.base import CheckContext
from probeatlas.common.netpolicy import NetworkPolicy
from probeatlas.common.schemas import CheckType, FailureCategory, JobSpec

LOCAL = NetworkPolicy.from_cidrs("127.0.0.0/8,::1/128", allow_private_hostnames=True)
ALL = [CheckType.HTTP, CheckType.DNS, CheckType.TCP, CheckType.ICMP]


def job(check_type: CheckType, target: str, timeout_ms: int = 3000, **settings) -> JobSpec:
    now = datetime.now(UTC)
    return JobSpec(job_id=uuid.uuid4(), monitor_id=uuid.uuid4(), check_type=check_type, target=target,
                   timeout_ms=timeout_ms, settings=settings, scheduled_for=now,
                   lease_expires_at=now + timedelta(minutes=1))


def ctx(**kw) -> CheckContext:
    return CheckContext(policy=kw.pop("policy", LOCAL), capabilities=kw.pop("capabilities", ALL), **kw)


# --- HTTP ----------------------------------------------------------------------


def test_http_success_records_phase_timings(target_server):
    r = execute(job(CheckType.HTTP, target_server.url("/ok"), expected_status_codes=["2xx"]), ctx())
    assert r.success and r.http_status == 200 and r.failure_category is None
    assert r.connect_ms is not None and r.ttfb_ms is not None and r.total_ms is not None
    assert r.dns_ms is None  # IP literal: no DNS lookup happened, so no DNS timing is reported
    assert r.tls_ms is None  # plain HTTP
    assert r.latency_ms == r.total_ms
    assert r.remote_address == "127.0.0.1"


def test_http_unexpected_status_is_a_failure_with_status(target_server):
    r = execute(job(CheckType.HTTP, target_server.url("/status/503")), ctx())
    assert not r.success and r.failure_category == FailureCategory.HTTP_STATUS and r.http_status == 503
    assert r.latency_ms is not None  # a response arrived, so the duration is real


def test_http_expected_status_patterns(target_server):
    r = execute(job(CheckType.HTTP, target_server.url("/status/404"), expected_status_codes=["200", "400-404"]),
                ctx())
    assert r.success


def test_http_timeout(target_server):
    r = execute(job(CheckType.HTTP, target_server.url("/slow?ms=1500"), timeout_ms=500), ctx())
    assert not r.success and r.failure_category == FailureCategory.TIMEOUT
    assert r.http_status is None and r.latency_ms is None


def test_http_connection_refused():
    port = _free_port()
    r = execute(job(CheckType.HTTP, f"http://127.0.0.1:{port}/"), ctx())
    assert r.failure_category == FailureCategory.CONNECTION_REFUSED and r.latency_ms is None


def test_https_measures_tls_and_verifies_certificate(tmp_path):
    trustme = pytest.importorskip("trustme")
    ca = trustme.CA()
    cert = ca.issue_cert("localhost")

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    cert.configure_cert(sctx)
    httpd.socket = sctx.wrap_socket(httpd.socket, server_side=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        url = f"https://localhost:{httpd.server_address[1]}/"
        ca_file = tmp_path / "ca.pem"
        ca.cert_pem.write_to_path(str(ca_file))
        ok = execute(job(CheckType.HTTP, url), ctx(ca_file=str(ca_file)))
        assert ok.success, ok.error_message
        assert ok.tls_ms is not None and ok.dns_ms is not None and ok.details["tls_version"].startswith("TLS")
        untrusted = execute(job(CheckType.HTTP, url), ctx())
        assert untrusted.failure_category == FailureCategory.TLS_CERTIFICATE
    finally:
        httpd.shutdown()


# --- TCP -----------------------------------------------------------------------


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_tcp_connect_success_and_refused():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    try:
        r = execute(job(CheckType.TCP, "127.0.0.1", port=srv.getsockname()[1]), ctx())
        assert r.success and r.connect_ms is not None and r.latency_ms == r.connect_ms
    finally:
        srv.close()
    r = execute(job(CheckType.TCP, "127.0.0.1", port=_free_port()), ctx())
    assert r.failure_category == FailureCategory.CONNECTION_REFUSED


def test_tcp_blocked_without_allowlist():
    r = execute(job(CheckType.TCP, "127.0.0.1", port=22), ctx(policy=NetworkPolicy()))
    assert r.failure_category == FailureCategory.BLOCKED_TARGET


# --- DNS (local authoritative test server) ----------------------------------


class FakeDNS:
    """Minimal UDP DNS server returning fixed answers."""

    def __init__(self, answers: dict[str, list[str]]):
        self.answers = answers
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while True:
            try:
                data, addr = self.sock.recvfrom(4096)
            except OSError:
                return
            q = dns.message.from_wire(data)
            resp = dns.message.make_response(q)
            name = q.question[0].name.to_text().rstrip(".")
            if name in self.answers:
                resp.answer.append(dns.rrset.from_text(q.question[0].name, 60, "IN", "A", *self.answers[name]))
            else:
                resp.set_rcode(dns.rcode.NXDOMAIN)
            self.sock.sendto(resp.to_wire(), addr)

    def close(self):
        self.sock.close()


@pytest.fixture
def fake_dns(monkeypatch):
    server = FakeDNS({"public.test": ["93.184.216.34"], "sneaky.test": ["93.184.216.34", "10.0.0.5"]})
    import dns.resolver

    original = dns.resolver.Resolver.__init__

    def patched(self, *a, **kw):
        original(self, *a, **kw)
        self.port = server.port

    monkeypatch.setattr(dns.resolver.Resolver, "__init__", patched)
    yield server
    server.close()


def test_dns_success(fake_dns):
    r = execute(job(CheckType.DNS, "public.test", record_type="A", nameserver="127.0.0.1"), ctx())
    assert r.success and r.dns_ms is not None and r.details["answers"] == ["93.184.216.34"]


def test_dns_nxdomain(fake_dns):
    r = execute(job(CheckType.DNS, "missing.test", nameserver="127.0.0.1"), ctx())
    assert r.failure_category == FailureCategory.DNS_NXDOMAIN


def test_dns_answers_with_private_addresses_are_not_revealed(fake_dns):
    policy = NetworkPolicy.from_cidrs("127.0.0.1/32", allow_private_hostnames=True)
    r = execute(job(CheckType.DNS, "sneaky.test", nameserver="127.0.0.1"), ctx(policy=policy))
    assert r.failure_category == FailureCategory.BLOCKED_TARGET
    assert "10.0.0.5" not in (r.error_message or "") and "answers" not in r.details


def test_dns_nameserver_must_pass_policy():
    r = execute(job(CheckType.DNS, "example.com", nameserver="10.0.0.53"), ctx(policy=NetworkPolicy()))
    assert r.failure_category == FailureCategory.BLOCKED_TARGET


# --- ICMP ----------------------------------------------------------------------


def test_icmp_unsupported_environment_reports_unavailable_not_fake_values(monkeypatch):
    def refuse(_family):
        raise PermissionError("not permitted")

    monkeypatch.setattr(icmp_mod, "_open", refuse)
    assert icmp_mod.icmp_supported() is False
    r = execute(job(CheckType.ICMP, "127.0.0.1"), ctx())
    assert not r.success and r.failure_category == FailureCategory.UNSUPPORTED
    assert r.latency_ms is None and r.rtt_avg_ms is None and r.packet_loss_pct is None


def test_job_for_missing_capability_is_unsupported():
    r = execute(job(CheckType.ICMP, "127.0.0.1"), ctx(capabilities=[CheckType.HTTP]))
    assert r.failure_category == FailureCategory.UNSUPPORTED


@pytest.mark.skipif(not icmp_mod.icmp_supported(), reason="unprivileged ICMP sockets not permitted here")
def test_icmp_ping_loopback():
    r = execute(job(CheckType.ICMP, "127.0.0.1", packet_count=3), ctx())
    assert r.success, r.error_message
    assert r.packets_sent == 3 and r.packets_received == 3 and r.packet_loss_pct == 0
    assert r.rtt_min_ms <= r.rtt_avg_ms <= r.rtt_max_ms


def test_icmp_blocked_target():
    r = execute(job(CheckType.ICMP, "169.254.169.254"), ctx(policy=NetworkPolicy()))
    assert r.failure_category == FailureCategory.BLOCKED_TARGET


def test_check_crash_becomes_internal_error(monkeypatch):
    from probeatlas.agent import checks

    def boom(_job, _ctx):
        raise RuntimeError("bug")

    monkeypatch.setitem(checks.CHECKS, CheckType.HTTP, boom)
    r = execute(job(CheckType.HTTP, "http://127.0.0.1/"), ctx())
    assert r.failure_category == FailureCategory.INTERNAL_ERROR and not r.success
