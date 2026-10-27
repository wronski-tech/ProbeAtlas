"""Control-plane settings loaded from ``PROBEATLAS_*`` environment variables."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

from probeatlas.common.netpolicy import NetworkPolicy


class Settings(BaseSettings):
    """Control-plane configuration. Every variable is prefixed ``PROBEATLAS_``."""

    model_config = SettingsConfigDict(env_prefix="PROBEATLAS_", env_file=None, extra="ignore")

    env: str = "development"
    database_url: str = "postgresql+psycopg://probeatlas:probeatlas@localhost:5432/probeatlas"

    # Accounts
    allow_registration: bool = True
    session_ttl_hours: int = 24 * 7

    # Tenant limits (defaults applied to new workspaces)
    default_max_monitors: int = 25
    min_interval_seconds: int = 30
    max_timeout_ms: int = 30_000
    max_regions_per_monitor: int = 10
    rate_limit_per_minute: int = 300
    auth_rate_limit_per_minute: int = 20

    # Target policy. Private targets are a development/demo opt-in only.
    allow_private_targets: bool = False
    webhook_allowed_private_cidrs: str = ""

    # Agents and jobs
    bootstrap_enrollment_token: str | None = None
    bootstrap_enrollment_region: str | None = None
    agent_offline_after_seconds: int = 60
    job_lease_grace_seconds: int = 30
    job_max_attempts: int = 2
    late_result_window_seconds: int = 600

    # Retention
    result_retention_days: int = 30
    job_retention_days: int = 7
    audit_retention_days: int = 365

    # Worker
    worker_tick_seconds: float = 2.0
    worker_metrics_port: int = 9100

    cors_origins: str = ""

    @property
    def is_production(self) -> bool:
        """Whether ``PROBEATLAS_ENV`` is ``production`` (enables stricter startup checks)."""
        return self.env == "production"

    def target_policy(self) -> NetworkPolicy:
        """Syntactic validation policy for monitor targets."""
        return NetworkPolicy(allow_private_hostnames=self.allow_private_targets)

    def webhook_policy(self) -> NetworkPolicy:
        """Outbound network policy for webhook delivery.

        Private destinations are blocked unless ``PROBEATLAS_WEBHOOK_ALLOWED_PRIVATE_CIDRS`` allowlists them.
        """
        return NetworkPolicy.from_cidrs(
            self.webhook_allowed_private_cidrs,
            allow_private_hostnames=bool(self.webhook_allowed_private_cidrs),
        )


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings, read once from the environment and cached."""
    return Settings()
