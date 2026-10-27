"""Measurement implementations.

Each check is a pure function ``(JobSpec, CheckContext) -> MeasurementResult``.
Checks know nothing about scheduling, transport, or persistence, so new
protocols are added by writing one function and registering it in ``CHECKS``.
"""

from __future__ import annotations

import traceback
import uuid
from collections.abc import Callable

from probeatlas.common.schemas import CheckType, FailureCategory, JobSpec, MeasurementResult, utcnow

from .base import CheckContext, failure
from .dns import run_dns
from .http import run_http
from .icmp import icmp_supported, run_icmp
from .tcp import run_tcp

CheckFn = Callable[[JobSpec, CheckContext], MeasurementResult]

CHECKS: dict[CheckType, CheckFn] = {
    CheckType.HTTP: run_http,
    CheckType.DNS: run_dns,
    CheckType.TCP: run_tcp,
    CheckType.ICMP: run_icmp,
}


def detect_capabilities() -> list[CheckType]:
    """Return the check types this host can run. ICMP is included only if ping sockets work."""
    caps = [CheckType.HTTP, CheckType.DNS, CheckType.TCP]
    if icmp_supported():
        caps.append(CheckType.ICMP)
    return caps


def execute(job: JobSpec, ctx: CheckContext) -> MeasurementResult:
    """Run a job, converting any unexpected exception into a failed result."""
    measured_at = utcnow()
    if job.check_type not in ctx.capabilities:
        return failure(
            job, measured_at, FailureCategory.UNSUPPORTED, f"{job.check_type} checks are not supported by this agent"
        )
    try:
        return CHECKS[job.check_type](job, ctx)
    except Exception as exc:  # noqa: BLE001 - a broken check must not crash the agent
        ctx.log.error("check crashed job_id=%s error=%s\n%s", job.job_id, type(exc).__name__, traceback.format_exc())
        return MeasurementResult(
            result_id=uuid.uuid4(),
            job_id=job.job_id,
            check_type=job.check_type,
            target=job.target,
            measured_at=measured_at,
            success=False,
            failure_category=FailureCategory.INTERNAL_ERROR,
            error_message=f"agent error: {type(exc).__name__}",
        )
