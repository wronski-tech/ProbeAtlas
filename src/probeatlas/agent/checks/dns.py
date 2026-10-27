from __future__ import annotations

import ipaddress
import time
import uuid

import dns.exception
import dns.resolver

from probeatlas.common.netpolicy import BlockedTargetError
from probeatlas.common.schemas import FailureCategory, JobSpec, MeasurementResult, utcnow

from .base import CheckContext, failure

MAX_ANSWERS = 20


def run_dns(job: JobSpec, ctx: CheckContext) -> MeasurementResult:
    """Resolve a record and time the lookup.

    Uses the agent's system resolver, or a customer-specified nameserver that
    must itself pass the network policy. A/AAAA answers that point at blocked
    address ranges are reported as ``blocked_target`` without revealing them,
    so DNS checks cannot be used to map private networks.
    """
    measured_at = utcnow()
    record_type = job.settings.get("record_type", "A")
    nameserver = job.settings.get("nameserver")
    timeout_s = job.timeout_ms / 1000
    resolver = dns.resolver.Resolver()
    if nameserver:
        try:
            ctx.policy.check_ip(nameserver)
        except (BlockedTargetError, ValueError) as exc:
            return failure(job, measured_at, FailureCategory.BLOCKED_TARGET, f"nameserver not allowed: {exc}")
        resolver.nameservers = [nameserver]
    resolver.lifetime = timeout_s
    resolver.timeout = timeout_s

    start = time.perf_counter()
    try:
        answer = resolver.resolve(job.target, record_type, raise_on_no_answer=True, search=False)
    except dns.resolver.NXDOMAIN:
        return failure(job, measured_at, FailureCategory.DNS_NXDOMAIN, "domain does not exist (NXDOMAIN)",
                       dns_ms=_ms(start))
    except dns.resolver.NoAnswer:
        return failure(job, measured_at, FailureCategory.DNS_NO_ANSWER, f"no {record_type} records", dns_ms=_ms(start))
    except dns.resolver.LifetimeTimeout:
        return failure(job, measured_at, FailureCategory.TIMEOUT, "DNS query timed out")
    except dns.resolver.NoNameservers:
        return failure(job, measured_at, FailureCategory.DNS_ERROR, "all nameservers failed (SERVFAIL/REFUSED)")
    except dns.exception.DNSException as exc:
        return failure(job, measured_at, FailureCategory.DNS_ERROR, f"DNS error: {type(exc).__name__}")
    dns_ms = _ms(start)

    records = [r.to_text() for r in answer][:MAX_ANSWERS]
    if record_type in ("A", "AAAA"):
        for rec in records:
            reason = ctx.policy.ip_block_reason(ipaddress.ip_address(rec))
            if reason:
                return failure(job, measured_at, FailureCategory.BLOCKED_TARGET,
                               f"name resolves to a {reason}", dns_ms=dns_ms)
    return MeasurementResult(
        result_id=uuid.uuid4(),
        job_id=job.job_id,
        check_type=job.check_type,
        target=job.target,
        measured_at=measured_at,
        success=True,
        latency_ms=dns_ms,
        dns_ms=dns_ms,
        details={"record_type": record_type, "answers": records, "ttl": answer.rrset.ttl if answer.rrset else None},
    )


def _ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 3)
