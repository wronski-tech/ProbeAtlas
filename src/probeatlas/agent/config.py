from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


@dataclass(frozen=True)
class AgentConfig:
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
    ca_file: str | None

    @classmethod
    def from_env(cls) -> AgentConfig:
        region = _env("PROBEATLAS_AGENT_REGION")
        if not region:
            raise SystemExit("PROBEATLAS_AGENT_REGION is required (operator-configured location label)")
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
            ca_file=_env("PROBEATLAS_AGENT_CA_FILE"),
        )
