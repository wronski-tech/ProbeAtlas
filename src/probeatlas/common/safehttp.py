"""Policy-enforcing HTTP client with per-phase timings.

Used by the agent for HTTP checks and by the control plane for webhook
delivery. It deliberately avoids connection pooling and high-level clients so
that every hop:

* resolves the hostname and validates every address against the policy,
* connects to the exact validated address (no second DNS lookup),
* verifies TLS against the original hostname (SNI + certificate check),
* revalidates redirect destinations before following them.
"""

from __future__ import annotations

import contextlib
import http.client
import os
import socket
import ssl
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import SplitResult, urljoin, urlsplit

from .netpolicy import (
    BlockedTargetError,
    NetworkPolicy,
    ResolutionError,
    http_port_allowed,
    resolve_checked,
    validate_hostname,
)
from .schemas import FailureCategory

REDIRECT_STATUSES = {301, 302, 303, 307, 308}
ALLOWED_METHODS = {"GET", "HEAD", "POST"}


class CheckError(Exception):
    """A measurement failure with a stable category and a safe message."""

    def __init__(self, category: FailureCategory, message: str):
        """Store the failure ``category`` and a message that is safe to show to customers."""
        super().__init__(message)
        self.category = category
        self.message = message


@dataclass
class HttpOutcome:
    """Mutable result of :func:`http_request`, filled in hop by hop.

    Timing fields are milliseconds and describe the final hop, except ``total_ms`` which spans the whole
    request including redirects. Fields that were not reached (for example ``tls_ms`` on plain HTTP or after
    a connect failure) stay ``None``. ``error`` is set instead of raising.
    """

    status: int | None = None
    final_url: str | None = None
    redirects: int = 0
    remote_address: str | None = None
    dns_ms: float | None = None
    connect_ms: float | None = None
    tls_ms: float | None = None
    ttfb_ms: float | None = None
    total_ms: float | None = None
    tls_version: str | None = None
    cert_not_after: str | None = None
    body_bytes: int = 0
    error: CheckError | None = None
    details: dict = field(default_factory=dict)


def _ms(start: float, end: float) -> float:
    """Return the elapsed time between two ``perf_counter`` readings in milliseconds (3 decimals)."""
    return round((end - start) * 1000, 3)


def validate_http_url(url: str, policy: NetworkPolicy) -> str:
    """Normalize an http(s) URL; raise ValueError/BlockedTargetError if invalid."""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https"):
        raise ValueError("only http and https URLs are supported")
    if parts.username or parts.password:
        raise ValueError("credentials in URLs are not supported")
    if not parts.hostname:
        raise ValueError("URL must include a hostname")
    host = validate_hostname(parts.hostname, policy)
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("URL has an invalid port") from exc
    if port is not None and not http_port_allowed(port):
        raise BlockedTargetError("port is not allowed for HTTP checks (use 80, 443 or 1024-65535)")
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc += f":{port}"
    path = parts.path or "/"
    return f"{parts.scheme}://{netloc}{path}" + (f"?{parts.query}" if parts.query else "")


def connect_checked(
    host: str, port: int, policy: NetworkPolicy, deadline: float
) -> tuple[socket.socket, str, float | None, float]:
    """Resolve, validate, and connect. Returns (sock, ip, dns_ms, connect_ms)."""
    t0 = time.perf_counter()
    try:
        addrs = resolve_checked(host, port, policy, timeout_s=deadline - t0)
    except ResolutionError as exc:
        if exc.timeout:
            raise CheckError(FailureCategory.TIMEOUT, "DNS resolution timed out") from exc
        cat = FailureCategory.DNS_NXDOMAIN if exc.nxdomain else FailureCategory.DNS_ERROR
        raise CheckError(cat, str(exc)) from exc
    except BlockedTargetError as exc:
        raise CheckError(FailureCategory.BLOCKED_TARGET, str(exc)) from exc
    t_dns = time.perf_counter()
    dns_ms = None if addrs and str(addrs[0][1]) == host.strip("[]") else _ms(t0, t_dns)

    last_exc: Exception | None = None
    for fam, ip in addrs:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            break
        sock = socket.socket(fam, socket.SOCK_STREAM)
        sock.settimeout(remaining)
        t_conn = time.perf_counter()
        try:
            sock.connect((str(ip), port))
        except OSError as exc:
            sock.close()
            last_exc = exc
            continue
        return sock, str(ip), dns_ms, _ms(t_conn, time.perf_counter())
    raise _classify_os_error(last_exc) if last_exc else CheckError(FailureCategory.TIMEOUT, "connect timed out")


def _classify_os_error(exc: BaseException) -> CheckError:
    """Map a low-level network, TLS, or HTTP parsing exception to a categorized :class:`CheckError`.

    Messages are generic on purpose: they never include response bodies, headers, or resolved private
    addresses.
    """
    if isinstance(exc, CheckError):
        return exc
    if isinstance(exc, TimeoutError | socket.timeout):
        return CheckError(FailureCategory.TIMEOUT, "operation timed out")
    if isinstance(exc, ConnectionRefusedError):
        return CheckError(FailureCategory.CONNECTION_REFUSED, "connection refused")
    if isinstance(exc, ConnectionResetError | BrokenPipeError | ConnectionAbortedError):
        return CheckError(FailureCategory.CONNECTION_RESET, "connection reset by peer")
    if isinstance(exc, ssl.SSLCertVerificationError):
        return CheckError(FailureCategory.TLS_CERTIFICATE, f"certificate verification failed: {exc.verify_message}")
    if isinstance(exc, ssl.SSLError):
        return CheckError(FailureCategory.TLS_ERROR, f"TLS handshake failed: {exc.reason or 'error'}")
    if isinstance(exc, http.client.HTTPException):
        return CheckError(FailureCategory.PROTOCOL_ERROR, f"invalid HTTP response: {type(exc).__name__}")
    if isinstance(exc, OSError) and exc.errno in (101, 113, 51, 65):  # ENETUNREACH / EHOSTUNREACH
        return CheckError(FailureCategory.NETWORK_UNREACHABLE, "network or host unreachable")
    return CheckError(FailureCategory.INTERNAL_ERROR, f"network error: {type(exc).__name__}")


def http_request(
    url: str,
    *,
    policy: NetworkPolicy,
    timeout_s: float,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    follow_redirects: bool = True,
    max_redirects: int = 5,
    max_body_bytes: int = 64 * 1024,
    user_agent: str = "ProbeAtlas/0.1",
    ca_file: str | None = None,
) -> HttpOutcome:
    """Perform an HTTP request; never raises for network errors.

    Phase timings (dns, connect, tls, ttfb) describe the final hop. ``ttfb_ms``
    is the time from sending the request to receiving the response headers.
    ``total_ms`` covers all hops including redirects and the body read.
    """
    method = method.upper()
    if method not in ALLOWED_METHODS:
        raise ValueError(f"HTTP method {method} is not allowed")
    out = HttpOutcome()
    start = time.perf_counter()
    deadline = start + timeout_s
    current = url
    try:
        while True:
            current = validate_http_url(current, policy)
            _one_hop(current, method, headers, body, policy, deadline, max_body_bytes, user_agent, ca_file, out)
            location = out.details.pop("_location", None)
            if follow_redirects and out.status in REDIRECT_STATUSES and location:
                if out.redirects >= max_redirects:
                    raise CheckError(FailureCategory.REDIRECT_LIMIT, f"more than {max_redirects} redirects")
                out.redirects += 1
                current = urljoin(current, location)
                if out.status == 303 or (out.status in (301, 302) and method == "POST"):
                    method, body = "GET", None
                continue
            break
        out.final_url = current
    except CheckError as exc:
        out.error = exc
    except ssl.SSLError as exc:  # SSLCertVerificationError is also a ValueError: classify it first
        out.error = _classify_os_error(exc)
    except (ValueError, BlockedTargetError) as exc:
        cat = FailureCategory.BLOCKED_TARGET if isinstance(exc, BlockedTargetError) else FailureCategory.PROTOCOL_ERROR
        msg = str(exc) if out.redirects == 0 else f"redirect rejected: {exc}"
        out.error = CheckError(cat, msg)
    except Exception as exc:  # noqa: BLE001 - convert every network failure into a result
        out.error = _classify_os_error(exc)
    out.total_ms = _ms(start, time.perf_counter())
    return out


def _one_hop(
    url: str,
    method: str,
    headers: dict[str, str] | None,
    body: bytes | None,
    policy: NetworkPolicy,
    deadline: float,
    max_body_bytes: int,
    user_agent: str,
    ca_file: str | None,
    out: HttpOutcome,
) -> None:
    """Send one HTTP request to ``url`` (no redirect handling) and record timings into ``out``.

    Connects to a policy-validated address, performs TLS with SNI and certificate verification against the
    original hostname, reads at most ``max_body_bytes`` of the body, and stores a redirect ``Location`` in
    ``out.details["_location"]`` for :func:`http_request` to validate and follow.
    """
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    out.status = None
    out.tls_ms = out.ttfb_ms = None
    sock, ip, out.dns_ms, out.connect_ms = connect_checked(host, port, policy, deadline)
    out.remote_address = ip
    # Socket timeouts apply per recv(), so a peer that drips one byte at a time would never trip them.
    # A watchdog shuts the connection down at the deadline instead; a duplicate descriptor keeps working
    # after the TLS wrap detaches the original socket object.
    expired = threading.Event()
    watch = socket.socket(fileno=os.dup(sock.fileno()))

    def expire() -> None:
        """Mark the request as timed out and unblock any pending read or write."""
        expired.set()
        with contextlib.suppress(OSError):
            watch.shutdown(socket.SHUT_RDWR)

    timer = threading.Timer(max(deadline - time.perf_counter(), 0.0), expire)
    timer.daemon = True
    timer.start()
    try:
        _exchange(sock, parts, host, port, method, headers, body, deadline, max_body_bytes, user_agent, ca_file, out)
    except Exception as exc:
        if expired.is_set():
            raise CheckError(FailureCategory.TIMEOUT, "request timed out") from exc
        raise
    finally:
        timer.cancel()
        watch.close()
    if expired.is_set():  # the body read may have been cut short
        raise CheckError(FailureCategory.TIMEOUT, "request timed out")


def _exchange(
    sock: socket.socket,
    parts: SplitResult,
    host: str,
    port: int,
    method: str,
    headers: dict[str, str] | None,
    body: bytes | None,
    deadline: float,
    max_body_bytes: int,
    user_agent: str,
    ca_file: str | None,
    out: HttpOutcome,
) -> None:
    """Perform TLS (for https), send the request, and read the response over a connected socket.

    Always closes ``sock``. The caller enforces the overall deadline.
    """
    try:
        if parts.scheme == "https":
            ctx = ssl.create_default_context(cafile=ca_file)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            sock.settimeout(max(deadline - time.perf_counter(), 0.001))
            t_tls = time.perf_counter()
            sock = ctx.wrap_socket(sock, server_hostname=host)
            out.tls_ms = _ms(t_tls, time.perf_counter())
            out.tls_version = sock.version()
            cert = sock.getpeercert() or {}
            out.cert_not_after = cert.get("notAfter")
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            raise CheckError(FailureCategory.TIMEOUT, "request timed out")
        sock.settimeout(remaining)
        conn = http.client.HTTPConnection(host, port, timeout=remaining)
        conn.sock = sock
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        conn.putrequest(method, path, skip_accept_encoding=True)
        conn.putheader("User-Agent", user_agent)
        conn.putheader("Accept", "*/*")
        conn.putheader("Connection", "close")
        for k, v in (headers or {}).items():
            conn.putheader(k, v)
        if body is not None:
            conn.putheader("Content-Length", str(len(body)))
        t_req = time.perf_counter()
        conn.endheaders(body)
        resp = conn.getresponse()
        out.ttfb_ms = _ms(t_req, time.perf_counter())
        out.status = resp.status
        if resp.status in REDIRECT_STATUSES:
            out.details["_location"] = resp.getheader("Location")
        if method != "HEAD":
            sock.settimeout(max(deadline - time.perf_counter(), 0.001))
            out.body_bytes = len(resp.read(max_body_bytes))
        resp.close()
        conn.close()
    finally:
        sock.close()


def status_matches(status: int, expected: list[str]) -> bool:
    """Match a status code against patterns like ``200``, ``200-299``, ``2xx``."""
    for pattern in expected:
        p = pattern.strip().lower()
        if p.endswith("xx") and len(p) == 3 and p[0].isdigit():
            if status // 100 == int(p[0]):
                return True
        elif "-" in p:
            lo, hi = p.split("-", 1)
            if int(lo) <= status <= int(hi):
                return True
        elif p.isdigit() and int(p) == status:
            return True
    return False
