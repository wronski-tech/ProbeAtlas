from __future__ import annotations

import uuid

from probeatlas.common.safehttp import http_request, status_matches
from probeatlas.common.schemas import FailureCategory, JobSpec, MeasurementResult, utcnow

from .base import CheckContext


def run_http(job: JobSpec, ctx: CheckContext) -> MeasurementResult:
    s = job.settings
    measured_at = utcnow()
    out = http_request(
        job.target,
        policy=ctx.policy,
        timeout_s=job.timeout_ms / 1000,
        method=s.get("method", "GET"),
        follow_redirects=bool(s.get("follow_redirects", True)),
        max_redirects=min(int(s.get("max_redirects", 5)), 10),
        user_agent=ctx.user_agent,
        ca_file=ctx.ca_file,
    )
    expected = s.get("expected_status_codes") or ["2xx"]
    success = out.error is None and out.status is not None and status_matches(out.status, expected)
    category = out.error.category if out.error else (None if success else FailureCategory.HTTP_STATUS)
    message = out.error.message if out.error else (None if success else f"unexpected HTTP status {out.status}")
    details = {"redirects": out.redirects, "body_bytes": out.body_bytes}
    if out.tls_version:
        details["tls_version"] = out.tls_version
    if out.cert_not_after:
        details["cert_not_after"] = out.cert_not_after
    return MeasurementResult(
        result_id=uuid.uuid4(),
        job_id=job.job_id,
        check_type=job.check_type,
        target=job.target,
        measured_at=measured_at,
        success=success,
        failure_category=category,
        error_message=message,
        # Total duration is only a meaningful latency when a response arrived.
        latency_ms=out.total_ms if out.status is not None else None,
        http_status=out.status,
        dns_ms=out.dns_ms,
        connect_ms=out.connect_ms,
        tls_ms=out.tls_ms,
        ttfb_ms=out.ttfb_ms,
        total_ms=out.total_ms,
        remote_address=out.remote_address,
        details=details,
    )
