from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from probeatlas.common.netpolicy import NetworkPolicy
from probeatlas.common.schemas import CheckType, FailureCategory, JobSpec, MeasurementResult


@dataclass
class CheckContext:
    policy: NetworkPolicy
    capabilities: list[CheckType]
    user_agent: str = "ProbeAtlas-Agent/0.1"
    ca_file: str | None = None
    log: logging.Logger = field(default_factory=lambda: logging.getLogger("probeatlas.agent.checks"))


def failure(
    job: JobSpec, measured_at: datetime, category: FailureCategory, message: str, **fields
) -> MeasurementResult:
    return MeasurementResult(
        result_id=uuid.uuid4(),
        job_id=job.job_id,
        check_type=job.check_type,
        target=job.target,
        measured_at=measured_at,
        success=False,
        failure_category=category,
        error_message=message[:500],
        **fields,
    )
