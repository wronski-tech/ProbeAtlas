"""Database schema. Every tenant-owned table carries ``workspace_id``."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    type_annotation_map = {
        uuid.UUID: UUID(as_uuid=True),
        datetime: DateTime(timezone=True),
        dict[str, Any]: JSONB,
    }


def _id() -> Mapped[uuid.UUID]:
    return mapped_column(primary_key=True, default=uuid.uuid4)


def _created() -> Mapped[datetime]:
    return mapped_column(server_default=func.now())


def _ws_fk() -> Mapped[uuid.UUID]:
    return mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)


# --- identity -----------------------------------------------------------------


class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = _id()
    email: Mapped[str] = mapped_column(String(320), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = _created()
    last_login_at: Mapped[datetime | None]


class UserSession(Base):
    __tablename__ = "user_sessions"
    id: Mapped[uuid.UUID] = _id()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = _created()
    expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]


class Workspace(Base):
    __tablename__ = "workspaces"
    id: Mapped[uuid.UUID] = _id()
    name: Mapped[str] = mapped_column(String(100))
    max_monitors: Mapped[int]
    min_interval_seconds: Mapped[int]
    created_at: Mapped[datetime] = _created()


class Membership(Base):
    __tablename__ = "memberships"
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    role: Mapped[str] = mapped_column(String(20))  # owner | member
    created_at: Mapped[datetime] = _created()
    __table_args__ = (CheckConstraint("role in ('owner','member')", name="membership_role"),)


class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[uuid.UUID] = _id()
    workspace_id: Mapped[uuid.UUID] = _ws_fk()
    name: Mapped[str] = mapped_column(String(100))
    prefix: Mapped[str] = mapped_column(String(32), unique=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _created()
    last_used_at: Mapped[datetime | None]
    expires_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]


# --- monitoring configuration -------------------------------------------------


class Monitor(Base):
    __tablename__ = "monitors"
    id: Mapped[uuid.UUID] = _id()
    workspace_id: Mapped[uuid.UUID] = _ws_fk()
    name: Mapped[str] = mapped_column(String(100))
    check_type: Mapped[str] = mapped_column(String(10))
    target: Mapped[str] = mapped_column(String(2048))
    settings: Mapped[dict[str, Any]] = mapped_column(default=dict)
    interval_seconds: Mapped[int]
    timeout_ms: Mapped[int]
    latency_threshold_ms: Mapped[float | None]
    regions: Mapped[list[str]] = mapped_column(ARRAY(String(64)))
    is_paused: Mapped[bool] = mapped_column(default=False)
    # unknown | up | degraded | down | paused
    state: Mapped[str] = mapped_column(String(20), default="unknown")
    state_changed_at: Mapped[datetime | None]
    last_result_at: Mapped[datetime | None]
    next_run_at: Mapped[datetime] = mapped_column(server_default=func.now())
    config_version: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = _created()
    # Set explicitly on configuration changes, not on scheduler/state updates.
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())
    __table_args__ = (
        CheckConstraint("check_type in ('http','dns','tcp','icmp')", name="monitor_check_type"),
        Index("ix_monitors_due", "next_run_at", postgresql_where=text("NOT is_paused")),
    )


class AlertRule(Base):
    __tablename__ = "alert_rules"
    id: Mapped[uuid.UUID] = _id()
    workspace_id: Mapped[uuid.UUID] = _ws_fk()
    monitor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("monitors.id", ondelete="CASCADE"), index=True)
    # unavailable | http_status | latency | packet_loss
    kind: Mapped[str] = mapped_column(String(20))
    threshold: Mapped[float | None]
    consecutive_breaches: Mapped[int] = mapped_column(default=3)
    recovery_successes: Mapped[int] = mapped_column(default=2)
    min_regions: Mapped[int] = mapped_column(default=1)
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
    __table_args__ = (
        CheckConstraint("kind in ('unavailable','http_status','latency','packet_loss')", name="alert_rule_kind"),
    )


class AlertState(Base):
    """Per-(rule, region) streak counters used for hysteresis."""

    __tablename__ = "alert_states"
    rule_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("alert_rules.id", ondelete="CASCADE"), primary_key=True)
    region: Mapped[str] = mapped_column(String(64), primary_key=True)
    firing: Mapped[bool] = mapped_column(default=False)
    consecutive_breaches: Mapped[int] = mapped_column(default=0)
    consecutive_ok: Mapped[int] = mapped_column(default=0)
    last_result_id: Mapped[uuid.UUID | None]
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class Incident(Base):
    __tablename__ = "incidents"
    id: Mapped[uuid.UUID] = _id()
    workspace_id: Mapped[uuid.UUID] = _ws_fk()
    monitor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("monitors.id", ondelete="CASCADE"), index=True)
    rule_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("alert_rules.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(20), default="open")  # open | resolved
    kind: Mapped[str] = mapped_column(String(20))
    scope: Mapped[str] = mapped_column(String(20))  # regional | global
    affected_regions: Mapped[list[str]] = mapped_column(ARRAY(String(64)))
    summary: Mapped[str] = mapped_column(Text)
    opened_at: Mapped[datetime] = _created()
    resolved_at: Mapped[datetime | None]
    __table_args__ = (
        # At most one open incident per rule: repeated results cannot duplicate incidents.
        Index("uq_incident_open_rule", "rule_id", unique=True, postgresql_where=text("status = 'open'")),
    )


class IncidentEvent(Base):
    __tablename__ = "incident_events"
    id: Mapped[uuid.UUID] = _id()
    workspace_id: Mapped[uuid.UUID] = _ws_fk()
    incident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    message: Mapped[str] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = _created()


class NotificationChannel(Base):
    __tablename__ = "notification_channels"
    id: Mapped[uuid.UUID] = _id()
    workspace_id: Mapped[uuid.UUID] = _ws_fk()
    kind: Mapped[str] = mapped_column(String(20), default="webhook")
    name: Mapped[str] = mapped_column(String(100))
    url: Mapped[str] = mapped_column(String(2048))
    # HMAC signing secret; needed in plaintext to sign payloads.
    signing_secret: Mapped[str] = mapped_column(String(128))
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = _created()


class NotificationDelivery(Base):
    """Transactional outbox for notifications, drained by the worker."""

    __tablename__ = "notification_deliveries"
    id: Mapped[uuid.UUID] = _id()
    workspace_id: Mapped[uuid.UUID] = _ws_fk()
    channel_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("notification_channels.id", ondelete="CASCADE"))
    incident_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"))
    dedupe_key: Mapped[str] = mapped_column(String(200), unique=True)
    event: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict[str, Any]]
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | sent | failed
    attempts: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_error: Mapped[str | None] = mapped_column(Text)
    response_status: Mapped[int | None]
    created_at: Mapped[datetime] = _created()
    sent_at: Mapped[datetime | None]
    __table_args__ = (Index("ix_deliveries_pending", "next_attempt_at", postgresql_where=text("status = 'pending'")),)


# --- agents and measurements --------------------------------------------------


class EnrollmentToken(Base):
    __tablename__ = "enrollment_tokens"
    id: Mapped[uuid.UUID] = _id()
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    description: Mapped[str] = mapped_column(String(200))
    region: Mapped[str | None] = mapped_column(String(64))  # restricts enrolling agents to this region
    max_uses: Mapped[int] = mapped_column(default=1)
    uses: Mapped[int] = mapped_column(default=0)
    expires_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = _created()


class Agent(Base):
    """Platform-operated measurement agent. Location metadata is operator-configured."""

    __tablename__ = "agents"
    id: Mapped[uuid.UUID] = _id()
    name: Mapped[str] = mapped_column(String(100))
    region: Mapped[str] = mapped_column(String(64), index=True)
    country_code: Mapped[str | None] = mapped_column(String(2))
    city: Mapped[str | None] = mapped_column(String(100))
    provider: Mapped[str | None] = mapped_column(String(100))
    version: Mapped[str] = mapped_column(String(40))
    capabilities: Mapped[list[str]] = mapped_column(ARRAY(String(10)))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="online")  # online | offline | disabled
    last_heartbeat_at: Mapped[datetime | None]
    last_seen_ip: Mapped[str | None] = mapped_column(String(64))
    enrollment_token_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("enrollment_tokens.id", ondelete="SET NULL")
    )
    registered_at: Mapped[datetime] = _created()


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[uuid.UUID] = _id()
    workspace_id: Mapped[uuid.UUID] = _ws_fk()
    monitor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("monitors.id", ondelete="CASCADE"))
    region: Mapped[str] = mapped_column(String(64))
    check_type: Mapped[str] = mapped_column(String(10))
    payload: Mapped[dict[str, Any]]  # snapshot of target/settings at scheduling time
    scheduled_for: Mapped[datetime]
    expires_at: Mapped[datetime]  # after this the job is stale and never (re)dispatched
    # pending | claimed | completed | expired | cancelled
    status: Mapped[str] = mapped_column(String(20), default="pending")
    attempts: Mapped[int] = mapped_column(default=0)
    agent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agents.id", ondelete="SET NULL"))
    claimed_at: Mapped[datetime | None]
    lease_expires_at: Mapped[datetime | None]
    completed_at: Mapped[datetime | None]
    expire_reason: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = _created()
    __table_args__ = (
        UniqueConstraint("monitor_id", "region", "scheduled_for", name="uq_job_slot"),
        Index("ix_jobs_dispatch", "region", "scheduled_for", postgresql_where=text("status = 'pending'")),
        Index("ix_jobs_lease", "lease_expires_at", postgresql_where=text("status = 'claimed'")),
        Index("ix_jobs_monitor_time", "monitor_id", "scheduled_for"),
    )


class Result(Base):
    """Raw measurement result (schema_version 1). Never aggregated in place."""

    __tablename__ = "results"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)  # agent-generated result_id
    schema_version: Mapped[int] = mapped_column(default=1)
    job_id: Mapped[uuid.UUID] = mapped_column(unique=True)  # one result per job: idempotent ingestion
    workspace_id: Mapped[uuid.UUID] = _ws_fk()
    monitor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("monitors.id", ondelete="CASCADE"))
    agent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agents.id", ondelete="SET NULL"))
    region: Mapped[str] = mapped_column(String(64))
    check_type: Mapped[str] = mapped_column(String(10))
    target: Mapped[str] = mapped_column(String(2048))
    measured_at: Mapped[datetime]
    ingested_at: Mapped[datetime] = mapped_column(server_default=func.now())
    late: Mapped[bool] = mapped_column(default=False)
    success: Mapped[bool]
    failure_category: Mapped[str | None] = mapped_column(String(40))
    error_message: Mapped[str | None] = mapped_column(String(500))
    latency_ms: Mapped[float | None] = mapped_column(Float)
    http_status: Mapped[int | None] = mapped_column(Integer)
    dns_ms: Mapped[float | None] = mapped_column(Float)
    connect_ms: Mapped[float | None] = mapped_column(Float)
    tls_ms: Mapped[float | None] = mapped_column(Float)
    ttfb_ms: Mapped[float | None] = mapped_column(Float)
    total_ms: Mapped[float | None] = mapped_column(Float)
    packet_loss_pct: Mapped[float | None] = mapped_column(Float)
    packets_sent: Mapped[int | None] = mapped_column(Integer)
    packets_received: Mapped[int | None] = mapped_column(Integer)
    rtt_min_ms: Mapped[float | None] = mapped_column(Float)
    rtt_avg_ms: Mapped[float | None] = mapped_column(Float)
    rtt_max_ms: Mapped[float | None] = mapped_column(Float)
    remote_address: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)
    __table_args__ = (
        Index("ix_results_monitor_time", "monitor_id", "measured_at"),
        Index("ix_results_workspace_time", "workspace_id", "measured_at"),
        Index("ix_results_measured_at", "measured_at"),
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[uuid.UUID] = _id()
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    actor_type: Mapped[str] = mapped_column(String(20))  # user | api_key | agent | system
    actor_id: Mapped[str | None] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(60))
    target_type: Mapped[str | None] = mapped_column(String(40))
    target_id: Mapped[str | None] = mapped_column(String(64))
    ip: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), index=True)
