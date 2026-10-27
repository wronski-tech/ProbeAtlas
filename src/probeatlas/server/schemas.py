"""Public API request/response models (``/v1``)."""

from __future__ import annotations

import base64
import json
import re
import uuid
from datetime import datetime
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, EmailStr, Field, IPvAnyAddress, field_validator, model_validator

from probeatlas.common.schemas import CheckType

T = TypeVar("T")

STATUS_PATTERN = r"^([1-5]xx|[1-5]\d\d(-[1-5]\d\d)?)$"
REGION_PATTERN = r"^[a-z0-9][a-z0-9-]{1,40}$"


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel, Generic[T]):
    items: list[T]
    next_cursor: str | None = Field(
        default=None, description="Opaque cursor; pass as `cursor` to fetch the next page. Null on the last page."
    )


def encode_cursor(*values: Any) -> str:
    raw = json.dumps([v.isoformat() if isinstance(v, datetime) else str(v) for v in values])
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> list[str]:
    from .errors import APIError

    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        values = json.loads(base64.urlsafe_b64decode(padded.encode()))
        if not isinstance(values, list):
            raise ValueError
        return values
    except (ValueError, json.JSONDecodeError) as exc:
        raise APIError(422, "validation_error", "invalid cursor") from exc


# --- auth & workspaces ----------------------------------------------------------


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=256)
    name: str = Field(min_length=1, max_length=100)
    workspace_name: str = Field(default="My workspace", min_length=1, max_length=100)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(max_length=256)


class WorkspaceOut(ORM):
    id: uuid.UUID
    name: str
    role: str | None = None
    max_monitors: int
    min_interval_seconds: int
    created_at: datetime


class UserOut(ORM):
    id: uuid.UUID
    email: str
    name: str
    created_at: datetime


class SessionOut(BaseModel):
    token: str
    expires_at: datetime
    user: UserOut
    workspaces: list[WorkspaceOut]


class MeOut(BaseModel):
    user: UserOut
    workspaces: list[WorkspaceOut]


class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class MemberAdd(BaseModel):
    email: EmailStr
    role: Literal["owner", "member"] = "member"


class MemberOut(BaseModel):
    user_id: uuid.UUID
    email: str
    name: str
    role: str
    created_at: datetime


# --- API keys -------------------------------------------------------------------


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class ApiKeyOut(ORM):
    id: uuid.UUID
    name: str
    prefix: str
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None


class ApiKeyIssued(ApiKeyOut):
    key: str = Field(description="The secret. Shown only once; store it securely.")


# --- monitors -------------------------------------------------------------------


class HttpSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    method: Literal["GET", "HEAD"] = "GET"
    expected_status_codes: list[str] = Field(default_factory=lambda: ["2xx"], min_length=1, max_length=20)
    follow_redirects: bool = True
    max_redirects: int = Field(default=5, ge=0, le=10)

    @field_validator("expected_status_codes")
    @classmethod
    def _patterns(cls, v: list[str]) -> list[str]:
        for p in v:
            if not re.match(STATUS_PATTERN, p):
                raise ValueError(f"invalid status pattern {p!r}; use 200, 200-299 or 2xx")
        return v


class DnsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    record_type: Literal["A", "AAAA", "CNAME", "MX", "TXT", "NS"] = "A"
    nameserver: IPvAnyAddress | None = None


class TcpSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    port: int = Field(ge=1, le=65535)


class IcmpSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    packet_count: int = Field(default=3, ge=1, le=10)


SETTINGS_MODELS: dict[CheckType, type[BaseModel]] = {
    CheckType.HTTP: HttpSettings,
    CheckType.DNS: DnsSettings,
    CheckType.TCP: TcpSettings,
    CheckType.ICMP: IcmpSettings,
}


def _normalize_regions(v: list[str]) -> list[str]:
    out = list(dict.fromkeys(r.strip().lower() for r in v))
    for r in out:
        if not re.match(REGION_PATTERN, r):
            raise ValueError(f"invalid region {r!r}")
    return out


class MonitorBase(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    interval_seconds: int = Field(default=60, ge=10, le=86_400)
    timeout_ms: int = Field(default=10_000, ge=500, le=60_000)
    regions: list[str] = Field(min_length=1, max_length=50)
    latency_threshold_ms: float | None = Field(
        default=None, gt=0, le=600_000, description="Successful checks slower than this mark the monitor degraded."
    )

    @field_validator("regions")
    @classmethod
    def _regions(cls, v: list[str]) -> list[str]:
        return _normalize_regions(v)


class MonitorCreate(MonitorBase):
    type: CheckType
    target: str = Field(min_length=1, max_length=2048, description="URL for http, hostname or IP otherwise.")
    settings: dict[str, Any] = Field(default_factory=dict)
    paused: bool = False



def validate_settings(check_type: CheckType, settings: dict[str, Any]) -> dict[str, Any]:
    """Validate type-specific settings; errors are reported under ``body.settings``."""
    from pydantic import ValidationError

    from .errors import APIError

    try:
        return SETTINGS_MODELS[check_type].model_validate(settings).model_dump(mode="json")
    except ValidationError as exc:
        details = [{"loc": ["body", "settings", *[str(x) for x in e["loc"]]], "msg": e["msg"], "type": e["type"]}
                   for e in exc.errors()]
        raise APIError(422, "validation_error", "request validation failed", details) from exc


class MonitorUpdate(BaseModel):
    """Partial update. ``type`` is immutable; recreate the monitor to change it."""

    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=100)
    target: str | None = Field(default=None, min_length=1, max_length=2048)
    settings: dict[str, Any] | None = None
    interval_seconds: int | None = Field(default=None, ge=10, le=86_400)
    timeout_ms: int | None = Field(default=None, ge=500, le=60_000)
    regions: list[str] | None = Field(default=None, min_length=1, max_length=50)
    latency_threshold_ms: float | None = Field(default=None, gt=0, le=600_000)

    @field_validator("regions")
    @classmethod
    def _regions(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else _normalize_regions(v)


class MonitorOut(BaseModel):
    id: uuid.UUID
    name: str
    type: str
    target: str
    settings: dict[str, Any]
    interval_seconds: int
    timeout_ms: int
    latency_threshold_ms: float | None
    regions: list[str]
    paused: bool
    state: str
    state_changed_at: datetime | None
    last_result_at: datetime | None
    config_version: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, m: Any) -> MonitorOut:
        return cls(
            id=m.id,
            name=m.name,
            type=m.check_type,
            target=m.target,
            settings=m.settings,
            interval_seconds=m.interval_seconds,
            timeout_ms=m.timeout_ms,
            latency_threshold_ms=m.latency_threshold_ms,
            regions=m.regions,
            paused=m.is_paused,
            state="paused" if m.is_paused else m.state,
            state_changed_at=m.state_changed_at,
            last_result_at=m.last_result_at,
            config_version=m.config_version,
            created_at=m.created_at,
            updated_at=m.updated_at,
        )


# --- results --------------------------------------------------------------------


class MeasurementOut(ORM):
    id: uuid.UUID
    schema_version: int
    job_id: uuid.UUID
    monitor_id: uuid.UUID
    agent_id: uuid.UUID | None
    region: str
    check_type: str
    target: str
    measured_at: datetime
    ingested_at: datetime
    late: bool
    success: bool
    failure_category: str | None
    error_message: str | None
    latency_ms: float | None
    http_status: int | None
    dns_ms: float | None
    connect_ms: float | None
    tls_ms: float | None
    ttfb_ms: float | None
    total_ms: float | None
    packet_loss_pct: float | None
    packets_sent: int | None
    packets_received: int | None
    rtt_min_ms: float | None
    rtt_avg_ms: float | None
    rtt_max_ms: float | None
    remote_address: str | None
    details: dict[str, Any]


class LatencyStats(BaseModel):
    sample_count: int = Field(description="Successful samples with a latency value.")
    min_ms: float | None
    p50_ms: float | None = Field(description=">= 5 samples required, otherwise null.")
    p95_ms: float | None = Field(description=">= 20 samples required, otherwise null.")
    p99_ms: float | None = Field(description=">= 100 samples required, otherwise null.")
    avg_ms: float | None


class GroupPerformance(BaseModel):
    key: str
    region: str
    agent_id: uuid.UUID | None = None
    agent_name: str | None = None
    total: int
    successes: int
    success_rate: float | None
    missing: int = Field(description="Jobs that expired without a result (no data, not success).")
    latency: LatencyStats
    packet_loss_avg_pct: float | None
    last_measured_at: datetime | None
    insufficient_data: bool


class RegionalPerformance(BaseModel):
    monitor_id: uuid.UUID
    since: datetime
    until: datetime
    overall: GroupPerformance
    regions: list[GroupPerformance]
    agents: list[GroupPerformance]
    failure_categories: dict[str, int]
    assessment: str = Field(
        description="healthy | incomplete (a region missed at least half of its checks) | degraded (single region) "
        "| regional | global | no_data, from per-region success rate and missing checks."
    )
    slow_regions: list[str] = Field(description="Regions whose P50 is more than 2x the median P50 of the others.")


class TimeseriesPoint(BaseModel):
    t: datetime
    total: int
    successes: int
    avg_ms: float | None
    p50_ms: float | None
    p95_ms: float | None
    max_ms: float | None


class TimeseriesSeries(BaseModel):
    key: str
    points: list[TimeseriesPoint]


class Timeseries(BaseModel):
    monitor_id: uuid.UUID
    since: datetime
    until: datetime
    bucket_seconds: int
    series: list[TimeseriesSeries]


# --- alerting -------------------------------------------------------------------


class AlertRuleBase(BaseModel):
    kind: Literal["unavailable", "http_status", "latency", "packet_loss"]
    threshold: float | None = Field(default=None, gt=0, description="ms for latency, percent for packet_loss.")
    consecutive_breaches: int = Field(default=3, ge=1, le=20)
    recovery_successes: int = Field(default=2, ge=1, le=20)
    min_regions: int = Field(default=1, ge=1, le=50)
    enabled: bool = True

    @model_validator(mode="after")
    def _threshold(self):
        if self.kind in ("latency", "packet_loss") and self.threshold is None:
            raise ValueError(f"threshold is required for {self.kind} rules")
        if self.kind == "packet_loss" and self.threshold is not None and self.threshold > 100:
            raise ValueError("packet_loss threshold is a percentage (0-100)")
        return self


class AlertRuleCreate(AlertRuleBase):
    monitor_id: uuid.UUID


class AlertRuleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    threshold: float | None = Field(default=None, gt=0)
    consecutive_breaches: int | None = Field(default=None, ge=1, le=20)
    recovery_successes: int | None = Field(default=None, ge=1, le=20)
    min_regions: int | None = Field(default=None, ge=1, le=50)
    enabled: bool | None = None


class AlertRuleOut(ORM):
    id: uuid.UUID
    monitor_id: uuid.UUID
    kind: str
    threshold: float | None
    consecutive_breaches: int
    recovery_successes: int
    min_regions: int
    enabled: bool
    created_at: datetime
    updated_at: datetime
    firing_regions: list[str] = []


class IncidentOut(ORM):
    id: uuid.UUID
    monitor_id: uuid.UUID
    rule_id: uuid.UUID
    status: str
    kind: str
    scope: str
    affected_regions: list[str]
    summary: str
    opened_at: datetime
    resolved_at: datetime | None


class IncidentEventOut(ORM):
    id: uuid.UUID
    incident_id: uuid.UUID
    kind: str
    message: str
    details: dict[str, Any]
    created_at: datetime


class IncidentDetail(IncidentOut):
    events: list[IncidentEventOut]


class ChannelCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    url: str = Field(min_length=1, max_length=2048)
    enabled: bool = True


class ChannelUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=100)
    url: str | None = Field(default=None, min_length=1, max_length=2048)
    enabled: bool | None = None


class ChannelOut(ORM):
    id: uuid.UUID
    kind: str
    name: str
    url: str
    enabled: bool
    created_at: datetime


class ChannelIssued(ChannelOut):
    signing_secret: str = Field(description="HMAC-SHA256 secret for X-ProbeAtlas-Signature. Shown only once.")


class DeliveryOut(ORM):
    id: uuid.UUID
    channel_id: uuid.UUID
    incident_id: uuid.UUID | None
    event: str
    status: str
    attempts: int
    last_error: str | None
    response_status: int | None
    created_at: datetime
    sent_at: datetime | None


class ChannelTestResult(BaseModel):
    delivered: bool
    status_code: int | None
    error: str | None


# --- agents ---------------------------------------------------------------------


class AgentOut(ORM):
    id: uuid.UUID
    name: str
    region: str
    country_code: str | None
    city: str | None
    provider: str | None
    version: str
    capabilities: list[str]
    status: str
    last_heartbeat_at: datetime | None
    registered_at: datetime


class LocationOut(BaseModel):
    region: str
    country_codes: list[str]
    cities: list[str]
    agents_total: int
    agents_online: int
    capabilities: list[str]


class AuditEventOut(ORM):
    id: uuid.UUID
    actor_type: str
    actor_id: str | None
    action: str
    target_type: str | None
    target_id: str | None
    ip: str | None
    details: dict[str, Any]
    created_at: datetime


class MonitorEvent(BaseModel):
    at: datetime
    source: Literal["incident", "audit"]
    kind: str
    message: str


class Overview(BaseModel):
    window_hours: int
    monitors: dict[str, int]
    failing_monitors: list[MonitorOut]
    open_incidents: int
    recent_incidents: list[IncidentOut]
    availability_pct: float | None
    latency: LatencyStats
    recent_measurements: list[MeasurementOut]
    agents: dict[str, int]
    regions: list[GroupPerformance]
