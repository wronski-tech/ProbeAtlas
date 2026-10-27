"""Versioned wire schema between the control plane and measurement agents.

Conventions (apply to every field below):

* Durations are milliseconds as floating-point numbers.
* Percentages are 0-100.
* Timestamps are timezone-aware UTC (ISO 8601, ``Z`` suffix on the wire).
* ``measured_at`` is when the agent started the measurement; the control plane
  records its own ``ingested_at`` on receipt.
* A field that does not apply to a check type, or that could not be measured,
  is ``null``. It is never filled with a substitute value.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

RESULT_SCHEMA_VERSION = 1
AGENT_PROTOCOL_VERSION = 1


class CheckType(StrEnum):
    """Measurement protocols an agent can execute. Values are stable wire identifiers."""

    HTTP = "http"
    DNS = "dns"
    TCP = "tcp"
    ICMP = "icmp"


class FailureCategory(StrEnum):
    """Stable, machine-readable reason a measurement failed.

    Categories are part of the public API (dashboards, alert rules, and the ``failure_category`` filter rely
    on them). Add new values; never rename existing ones.
    """

    DNS_ERROR = "dns_error"
    DNS_NXDOMAIN = "dns_nxdomain"
    DNS_NO_ANSWER = "dns_no_answer"
    TIMEOUT = "timeout"
    CONNECTION_REFUSED = "connection_refused"
    CONNECTION_RESET = "connection_reset"
    NETWORK_UNREACHABLE = "network_unreachable"
    TLS_ERROR = "tls_error"
    TLS_CERTIFICATE = "tls_certificate"
    PROTOCOL_ERROR = "protocol_error"
    HTTP_STATUS = "http_status"
    REDIRECT_LIMIT = "redirect_limit"
    PACKET_LOSS = "packet_loss"
    BLOCKED_TARGET = "blocked_target"
    UNSUPPORTED = "unsupported"
    INTERNAL_ERROR = "internal_error"


def utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC ``datetime``."""
    return datetime.now(UTC)


class JobSpec(BaseModel):
    """A measurement assignment handed to an agent."""

    model_config = ConfigDict(extra="ignore")

    job_id: UUID
    monitor_id: UUID
    check_type: CheckType
    target: str
    timeout_ms: int = Field(ge=100, le=60_000)
    settings: dict[str, Any] = Field(default_factory=dict)
    scheduled_for: datetime
    lease_expires_at: datetime


class MeasurementResult(BaseModel):
    """Result of executing one job. ``schema_version`` 1."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = RESULT_SCHEMA_VERSION
    result_id: UUID
    job_id: UUID
    check_type: CheckType
    target: str = Field(max_length=2048)
    measured_at: datetime
    success: bool
    failure_category: FailureCategory | None = None
    error_message: str | None = Field(default=None, max_length=500)

    # Primary latency for the check type: HTTP total duration, TCP connect
    # time, DNS resolution time, or ICMP average round-trip time.
    latency_ms: float | None = Field(default=None, ge=0, le=600_000)
    http_status: int | None = Field(default=None, ge=100, le=599)
    dns_ms: float | None = Field(default=None, ge=0, le=600_000)
    connect_ms: float | None = Field(default=None, ge=0, le=600_000)
    tls_ms: float | None = Field(default=None, ge=0, le=600_000)
    ttfb_ms: float | None = Field(default=None, ge=0, le=600_000)
    total_ms: float | None = Field(default=None, ge=0, le=600_000)
    packet_loss_pct: float | None = Field(default=None, ge=0, le=100)
    packets_sent: int | None = Field(default=None, ge=0, le=100)
    packets_received: int | None = Field(default=None, ge=0, le=100)
    rtt_min_ms: float | None = Field(default=None, ge=0, le=600_000)
    rtt_avg_ms: float | None = Field(default=None, ge=0, le=600_000)
    rtt_max_ms: float | None = Field(default=None, ge=0, le=600_000)
    remote_address: str | None = Field(default=None, max_length=64)
    # Protocol-specific extras (redirect count, TLS version, DNS answers...).
    details: dict[str, Any] = Field(default_factory=dict)

    @field_validator("measured_at")
    @classmethod
    def _require_tz(cls, value: datetime) -> datetime:
        """Reject naive timestamps and normalize ``measured_at`` to UTC."""
        if value.tzinfo is None:
            raise ValueError("measured_at must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("details")
    @classmethod
    def _bounded_details(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Cap the size of the free-form ``details`` object at roughly 8 KB."""
        if len(repr(value)) > 8_000:
            raise ValueError("details too large")
        return value


class AgentRegistration(BaseModel):
    """Payload an agent sends once to ``POST /agent/v1/register`` with its enrollment token.

    Location fields (``region``, ``country_code``, ``city``, ``provider``) are operator-configured labels;
    the control plane does not verify or infer them.
    """

    name: str = Field(min_length=1, max_length=100)
    region: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,40}$")
    country_code: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    city: str | None = Field(default=None, max_length=100)
    provider: str | None = Field(default=None, max_length=100)
    version: str = Field(max_length=40)
    capabilities: list[CheckType]


class AgentHeartbeat(BaseModel):
    """Periodic liveness report sent to ``POST /agent/v1/heartbeat``."""

    version: str = Field(max_length=40)
    capabilities: list[CheckType]
    running_jobs: int = Field(default=0, ge=0)
    uptime_s: float = Field(default=0, ge=0)


class ClaimRequest(BaseModel):
    """Body of ``POST /agent/v1/jobs/claim``: how many jobs the agent can run right now."""

    max_jobs: int = Field(default=10, ge=1, le=100)


class ClaimResponse(BaseModel):
    """Jobs leased to the agent. Each must be executed and reported before its lease expires."""

    jobs: list[JobSpec]


class ResultBatch(BaseModel):
    """Body of ``POST /agent/v1/results``: up to 200 measurement results."""

    results: list[MeasurementResult] = Field(max_length=200)


class ResultAck(BaseModel):
    """Per-result outcome of a submission.

    ``accepted`` means stored, ``duplicate`` means the result or job was already recorded (safe to drop), and
    ``rejected`` means it will never be accepted (for example a job leased to another agent); ``reason``
    explains why.
    """

    job_id: UUID
    result_id: UUID
    status: Literal["accepted", "duplicate", "rejected"]
    reason: str | None = None


class ResultBatchResponse(BaseModel):
    """Acknowledgements for a :class:`ResultBatch`, one per submitted result."""

    acks: list[ResultAck]
