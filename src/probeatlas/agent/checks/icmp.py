"""ICMP echo using unprivileged datagram ("ping") sockets.

Linux allows ``socket(AF_INET, SOCK_DGRAM, IPPROTO_ICMP)`` for processes whose
group is inside ``net.ipv4.ping_group_range``. This needs no root and no
CAP_NET_RAW. In containers, set the sysctl (see docker-compose.yml). If the
socket cannot be opened the agent does not advertise the ``icmp`` capability,
and any ICMP job it receives is reported as ``unsupported``.

Raw-socket fallback is intentionally not implemented: it requires
CAP_NET_RAW, which we do not want to grant by default.
"""

from __future__ import annotations

import os
import select
import socket
import struct
import time
import uuid

from probeatlas.common.netpolicy import BlockedTargetError, ResolutionError, resolve_checked
from probeatlas.common.schemas import FailureCategory, JobSpec, MeasurementResult, utcnow

from .base import CheckContext, failure

ICMP_ECHO_REQUEST = 8
ICMP_ECHO_REPLY = 0
ICMPV6_ECHO_REQUEST = 128
ICMPV6_ECHO_REPLY = 129


def _open(family: int) -> socket.socket:
    proto = socket.IPPROTO_ICMP if family == socket.AF_INET else socket.IPPROTO_ICMPV6
    return socket.socket(family, socket.SOCK_DGRAM, proto)


def icmp_supported() -> bool:
    if os.environ.get("PROBEATLAS_AGENT_DISABLE_ICMP") == "1":
        return False
    try:
        _open(socket.AF_INET).close()
        return True
    except OSError:
        return False


def _checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\0"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    total = (total >> 16) + (total & 0xFFFF)
    total += total >> 16
    return ~total & 0xFFFF


def run_icmp(job: JobSpec, ctx: CheckContext) -> MeasurementResult:
    measured_at = utcnow()
    count = max(1, min(int(job.settings.get("packet_count", 3)), 10))
    timeout_s = job.timeout_ms / 1000
    start = time.perf_counter()
    try:
        addrs = resolve_checked(job.target, 0, ctx.policy, timeout_s=timeout_s)
    except BlockedTargetError as exc:
        return failure(job, measured_at, FailureCategory.BLOCKED_TARGET, str(exc))
    except ResolutionError as exc:
        cat = FailureCategory.DNS_NXDOMAIN if exc.nxdomain else (
            FailureCategory.TIMEOUT if exc.timeout else FailureCategory.DNS_ERROR)
        return failure(job, measured_at, cat, str(exc))
    dns_ms = None if str(addrs[0][1]) == job.target else round((time.perf_counter() - start) * 1000, 3)
    family, ip = addrs[0]
    try:
        sock = _open(family)
    except OSError:
        return failure(job, measured_at, FailureCategory.UNSUPPORTED,
                       "ICMP sockets are not permitted in this environment", dns_ms=dns_ms)

    v6 = family == socket.AF_INET6
    req_type, reply_type = (ICMPV6_ECHO_REQUEST, ICMPV6_ECHO_REPLY) if v6 else (ICMP_ECHO_REQUEST, ICMP_ECHO_REPLY)
    token = os.urandom(8)
    # Split the remaining budget evenly across packets.
    per_packet = max((timeout_s - (time.perf_counter() - start)) / count, 0.05)
    rtts: list[float] = []
    try:
        for seq in range(1, count + 1):
            header = struct.pack("!BBHHH", req_type, 0, 0, 0, seq)
            payload = token + struct.pack("!d", time.perf_counter())
            packet = struct.pack("!BBHHH", req_type, 0, _checksum(header + payload), 0, seq) + payload
            sent = time.perf_counter()
            try:
                sock.sendto(packet, (str(ip), 0))
            except OSError as exc:
                return failure(job, measured_at, FailureCategory.NETWORK_UNREACHABLE,
                               f"ICMP send failed: {type(exc).__name__}", dns_ms=dns_ms)
            wait_until = sent + per_packet
            while (remaining := wait_until - time.perf_counter()) > 0:
                ready, _, _ = select.select([sock], [], [], remaining)
                if not ready:
                    break
                data, _addr = sock.recvfrom(2048)
                if len(data) < 8:
                    continue
                rtype, _code, _ck, _ident, rseq = struct.unpack("!BBHHH", data[:8])
                if rtype == reply_type and rseq == seq and data[8:16] == token:
                    rtts.append(round((time.perf_counter() - sent) * 1000, 3))
                    break
            # Pace packets so we do not burst at the target.
            time.sleep(max(0.0, min(0.2, wait_until - time.perf_counter())))
    finally:
        sock.close()

    received = len(rtts)
    loss = round((count - received) / count * 100, 2)
    fields = dict(
        dns_ms=dns_ms,
        packets_sent=count,
        packets_received=received,
        packet_loss_pct=loss,
        remote_address=str(ip),
    )
    if not rtts:
        return failure(job, measured_at, FailureCategory.PACKET_LOSS, "100% packet loss (no echo replies)", **fields)
    avg = round(sum(rtts) / received, 3)
    return MeasurementResult(
        result_id=uuid.uuid4(),
        job_id=job.job_id,
        check_type=job.check_type,
        target=job.target,
        measured_at=measured_at,
        success=True,
        latency_ms=avg,
        rtt_min_ms=min(rtts),
        rtt_avg_ms=avg,
        rtt_max_ms=max(rtts),
        **fields,
    )
