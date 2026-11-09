"""Measurement implementations against controlled local servers."""

from __future__ import annotations

import ipaddress
import socket
import ssl
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import dns.message
import dns.rcode
import dns.rdatatype
import dns.rrset
import pytest

from probeatlas.agent.checks import execute
from probeatlas.agent.checks import icmp as icmp_mod
from probeatlas.agent.checks.base import CheckContext
from probeatlas.common.netpolicy import BlockedTargetError, NetworkPolicy, ResolutionError, resolve_checked
from probeatlas.common.schemas import CheckType, FailureCategory, JobSpec

LOCAL = NetworkPolicy.from_cidrs("127.0.0.0/8,::1/128", allow_private_hostnames=True)
ALL = [CheckType.HTTP, CheckType.DNS, CheckType.TCP, CheckType.ICMP]


def job(check_type: CheckType, target: str, timeout_ms: int = 3000, **settings) -> JobSpec:
    """Build a job spec for ``check_type`` against ``target`` with the given settings."""
    now = datetime.now(UTC)
    return JobSpec(job_id=uuid.uuid4(), monitor_id=uuid.uuid4(), check_type=check_type, target=target,
                   timeout_ms=timeout_ms, settings=settings, scheduled_for=now,
                   lease_expires_at=now + timedelta(minutes=1))


def ctx(**kw) -> CheckContext:
    """Build a check context that allows localhost targets and supports every check type by default."""
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


def test_http_timeout_is_a_hard_deadline_even_when_the_server_drips_bytes():
    # Each byte arrives well inside the socket timeout, so only a whole-request deadline can stop this.
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def drip():
        """Accept one connection and send a never-ending header one byte every 100 ms."""
        conn, _ = srv.accept()
        with conn:
            conn.recv(4096)
            try:
                conn.sendall(b"HTTP/1.1 200 OK\r\nX-Slow: ")
                for _ in range(80):
                    conn.sendall(b"a")
                    time.sleep(0.1)
            except OSError:
                pass

    threading.Thread(target=drip, daemon=True).start()
    start = time.monotonic()
    r = execute(job(CheckType.HTTP, f"http://127.0.0.1:{srv.getsockname()[1]}/", timeout_ms=1000), ctx())
    elapsed = time.monotonic() - start
    srv.close()
    assert r.failure_category == FailureCategory.TIMEOUT and not r.success
    assert elapsed < 2.5


def test_http_connection_refused():
    port = _free_port()
    r = execute(job(CheckType.HTTP, f"http://127.0.0.1:{port}/"), ctx())
    assert r.failure_category == FailureCategory.CONNECTION_REFUSED and r.latency_ms is None


def test_https_measures_tls_and_verifies_certificate(tmp_path):
    trustme = pytest.importorskip("trustme")
    ca = trustme.CA()
    cert = ca.issue_cert("localhost")

    class H(BaseHTTPRequestHandler):
        """Minimal handler that answers ``ok`` over TLS."""

        def log_message(self, *a):
            """Silence the default request logging."""
            pass

        def do_GET(self):
            """Return 200 with a two-byte body."""
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
    """Return a localhost TCP port that nothing is listening on."""
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
        """Start a UDP DNS server on localhost answering from ``{name: {record_type: [rdata, ...]}}``."""
        self.answers = answers
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        """Answer queries until the socket is closed; unknown names get NXDOMAIN."""
        while True:
            try:
                data, addr = self.sock.recvfrom(4096)
            except OSError:
                return
            q = dns.message.from_wire(data)
            resp = dns.message.make_response(q)
            question = q.question[0]
            name = question.name.to_text().rstrip(".")
            rdtype = dns.rdatatype.to_text(question.rdtype)
            if name not in self.answers:
                resp.set_rcode(dns.rcode.NXDOMAIN)
            elif rdtype in self.answers[name]:  # otherwise NOERROR with no answer, like a real server
                resp.answer.append(dns.rrset.from_text(question.name, 60, "IN", rdtype, *self.answers[name][rdtype]))
            self.sock.sendto(resp.to_wire(), addr)

    def close(self):
        """Stop the server."""
        self.sock.close()


@pytest.fixture
def fake_dns(monkeypatch):
    """Point every new dnspython resolver at a :class:`FakeDNS` instance for this test."""
    server = FakeDNS({
        "public.test": {"A": ["93.184.216.34"]},
        "sneaky.test": {"A": ["93.184.216.34", "10.0.0.5"]},
        "alias.test": {"CNAME": ["public.test."]},
        "leaky.test": {"CNAME": ["db.corp.internal."], "MX": ["10 mail.lan."]},
    })
    import dns.resolver

    original = dns.resolver.Resolver.__init__

    def patched(self, *a, **kw):
        """Initialize the resolver normally, then use the fake server's port."""
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


def test_dns_cname_and_mx_answers_naming_internal_hosts_are_not_revealed(fake_dns):
    policy = NetworkPolicy.from_cidrs("127.0.0.0/8")  # strict hostname rules; only the test nameserver allowed
    ok = execute(job(CheckType.DNS, "alias.test", record_type="CNAME", nameserver="127.0.0.1"), ctx(policy=policy))
    assert ok.success and ok.details["answers"] == ["public.test."]
    for record_type in ("CNAME", "MX"):
        r = execute(job(CheckType.DNS, "leaky.test", record_type=record_type, nameserver="127.0.0.1"),
                    ctx(policy=policy))
        assert r.failure_category == FailureCategory.BLOCKED_TARGET and not r.success
        assert "corp" not in (r.error_message or "") and "lan" not in (r.error_message or "") and not r.details


def test_dns_check_uses_the_agents_configured_resolvers(fake_dns):
    # No customer nameserver: the agent's resolvers (PROBEATLAS_AGENT_NAMESERVERS) are used, not the host's.
    policy = NetworkPolicy.from_cidrs("127.0.0.0/8", nameservers="127.0.0.1")
    r = execute(job(CheckType.DNS, "public.test"), ctx(policy=policy))
    assert r.success and r.details["answers"] == ["93.184.216.34"]


def test_resolution_uses_policy_resolvers_and_checks_every_address(fake_dns):
    policy = NetworkPolicy(nameservers=("127.0.0.1",))
    public = [(socket.AF_INET, ipaddress.ip_address("93.184.216.34"))]
    assert resolve_checked("public.test", policy, timeout_s=2) == public
    with pytest.raises(BlockedTargetError):
        resolve_checked("sneaky.test", policy, timeout_s=2)  # one private answer rejects the whole name
    with pytest.raises(ResolutionError) as missing:
        resolve_checked("missing.test", policy, timeout_s=2)
    assert missing.value.nxdomain


def test_unresponsive_nameservers_cannot_delay_other_lookups(fake_dns, monkeypatch):
    # A tenant can point targets at zones whose nameservers never answer. Each lookup must give up at its own
    # deadline without holding shared resources, so lookups for other targets stay fast.
    hole = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    hole.bind(("127.0.0.2", 0))  # receives queries, never answers
    import dns.resolver

    init_with_fake_port = dns.resolver.Resolver.__init__

    def init(self, *a, **kw):
        """Use the fake server for 127.0.0.1 and the black hole for 127.0.0.2."""
        init_with_fake_port(self, *a, **kw)
        self.nameserver_ports = {"127.0.0.2": hole.getsockname()[1]}

    monkeypatch.setattr(dns.resolver.Resolver, "__init__", init)
    hung: list[tuple[bool, float]] = []

    def lookup_black_hole():
        """Resolve through the black hole and record whether it timed out and how long it took."""
        start = time.monotonic()
        try:
            resolve_checked("slow.test", NetworkPolicy(nameservers=("127.0.0.2",)), timeout_s=1.0)
        except ResolutionError as exc:
            hung.append((exc.timeout, time.monotonic() - start))

    threads = [threading.Thread(target=lookup_black_hole) for _ in range(100)]
    for t in threads:
        t.start()
    time.sleep(0.1)
    start = time.monotonic()
    addrs = resolve_checked("public.test", NetworkPolicy(nameservers=("127.0.0.1",)), timeout_s=1.0)
    fast = time.monotonic() - start
    for t in threads:
        t.join()
    hole.close()
    assert addrs == [(socket.AF_INET, ipaddress.ip_address("93.184.216.34"))] and fast < 0.5
    assert len(hung) == 100 and all(timed_out and elapsed < 1.8 for timed_out, elapsed in hung)


def test_dns_nameserver_must_pass_policy():
    r = execute(job(CheckType.DNS, "example.com", nameserver="10.0.0.53"), ctx(policy=NetworkPolicy()))
    assert r.failure_category == FailureCategory.BLOCKED_TARGET


# --- ICMP ----------------------------------------------------------------------


def test_icmp_unsupported_environment_reports_unavailable_not_fake_values(monkeypatch):
    def refuse(_family):
        """Simulate a host that forbids ping sockets."""
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
        """A check implementation with a bug."""
        raise RuntimeError("bug")

    monkeypatch.setitem(checks.CHECKS, CheckType.HTTP, boom)
    r = execute(job(CheckType.HTTP, "http://127.0.0.1/"), ctx())
    assert r.failure_category == FailureCategory.INTERNAL_ERROR and not r.success
