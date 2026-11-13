"""Agent configuration, read from ``PROBEATLAS_AGENT_*`` environment variables."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass


def _env(name: str, default: str | None = None) -> str | None:
    """Return environment variable ``name``, treating an empty value as unset."""
    value = os.environ.get(name)
    return value if value not in (None, "") else default


@dataclass(frozen=True)
class AgentConfig:
    """Immutable agent settings.

    ``region``, ``country_code``, ``city``, and ``provider`` are labels the operator sets for where the agent
    runs; they are reported as configured and never inferred. ``allowed_private_cidrs`` opts into
    monitoring private addresses and should stay empty for public agents. ``nameservers`` (comma-separated
    IPs) replaces the host's resolver for every lookup; public agents should use public resolvers so the
    hosting network's private or split-horizon zones are never consulted. ``info_url`` is a public page that
    explains the agent's traffic and how to reach its operator; it is added to the HTTP User-Agent so owners
    of monitored servers can find out who is checking them.
    """

    api_url: str
    enrollment_token: str | None
    name: str
    region: str
    country_code: str | None
    city: str | None
    provider: str | None
    state_file: str
    concurrency: int
    poll_interval_s: float
    heartbeat_interval_s: float
    allowed_private_cidrs: str
    nameservers: str
    ca_file: str | None
    info_url: str | None = None

    @classmethod
    def from_env(cls) -> AgentConfig:
        """Load the configuration from the environment; exits if ``PROBEATLAS_AGENT_REGION`` is missing."""
        region = _env("PROBEATLAS_AGENT_REGION")
        if not region:
            raise SystemExit("PROBEATLAS_AGENT_REGION is required (operator-configured location label)")
        info_url = _env("PROBEATLAS_AGENT_INFO_URL")
        if info_url and not re.fullmatch(r"https?://[^\s()]+", info_url):
            raise SystemExit("PROBEATLAS_AGENT_INFO_URL must be an http(s) URL without spaces or parentheses")
        return cls(
            api_url=(_env("PROBEATLAS_API_URL", "http://localhost:8000") or "").rstrip("/"),
            enrollment_token=_env("PROBEATLAS_AGENT_ENROLLMENT_TOKEN"),
            name=_env("PROBEATLAS_AGENT_NAME", os.uname().nodename) or "agent",
            region=region,
            country_code=_env("PROBEATLAS_AGENT_COUNTRY_CODE"),
            city=_env("PROBEATLAS_AGENT_CITY"),
            provider=_env("PROBEATLAS_AGENT_PROVIDER"),
            state_file=_env("PROBEATLAS_AGENT_STATE_FILE", "./agent-state.json") or "./agent-state.json",
            concurrency=int(_env("PROBEATLAS_AGENT_CONCURRENCY", "8") or 8),
            poll_interval_s=float(_env("PROBEATLAS_AGENT_POLL_INTERVAL", "2") or 2),
            heartbeat_interval_s=float(_env("PROBEATLAS_AGENT_HEARTBEAT_INTERVAL", "15") or 15),
            allowed_private_cidrs=_env("PROBEATLAS_AGENT_ALLOWED_PRIVATE_CIDRS", "") or "",
            nameservers=_env("PROBEATLAS_AGENT_NAMESERVERS", "") or "",
            ca_file=_env("PROBEATLAS_AGENT_CA_FILE"),
            info_url=info_url,
        )
