"""TCP connect check."""

from __future__ import annotations

import time
import uuid

from probeatlas.common.safehttp import CheckError, connect_checked
from probeatlas.common.schemas import JobSpec, MeasurementResult, utcnow

from .base import CheckContext, failure


def run_tcp(job: JobSpec, ctx: CheckContext) -> MeasurementResult:
    """Measure TCP handshake time. No application data is sent."""
    measured_at = utcnow()
    port = int(job.settings["port"])
    deadline = time.perf_counter() + job.timeout_ms / 1000
    try:
        sock, ip, dns_ms, connect_ms = connect_checked(job.target, port, ctx.policy, deadline)
    except CheckError as exc:
        return failure(job, measured_at, exc.category, exc.message)
    sock.close()
    return MeasurementResult(
        result_id=uuid.uuid4(),
        job_id=job.job_id,
        check_type=job.check_type,
        target=job.target,
        measured_at=measured_at,
        success=True,
        latency_ms=connect_ms,
        dns_ms=dns_ms,
        connect_ms=connect_ms,
        remote_address=ip,
        details={"port": port},
    )
