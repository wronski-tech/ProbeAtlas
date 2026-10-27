"""HTTP client for the control plane's agent protocol (``/agent/v1``)."""

from __future__ import annotations

import httpx

from probeatlas import __version__
from probeatlas.common.schemas import (
    AgentHeartbeat,
    AgentRegistration,
    ClaimResponse,
    JobSpec,
    MeasurementResult,
    ResultBatch,
    ResultBatchResponse,
)


class TransientError(Exception):
    """Network failure or 5xx: retry with backoff."""


class AuthError(Exception):
    """Credentials rejected: re-enroll or stop."""


class ControlPlaneClient:
    def __init__(self, api_url: str, timeout_s: float = 10.0, transport: httpx.BaseTransport | None = None):
        self._http = httpx.Client(
            base_url=api_url,
            timeout=timeout_s,
            transport=transport,
            headers={"User-Agent": f"ProbeAtlas-Agent/{__version__}"},
        )
        self.agent_id: str | None = None
        self._token: str | None = None

    def set_credentials(self, agent_id: str, token: str) -> None:
        self.agent_id = agent_id
        self._token = token
        self._http.headers["User-Agent"] = f"ProbeAtlas-Agent/{__version__} ({agent_id})"

    def _request(self, method: str, path: str, *, token: str | None = None, json: dict | None = None) -> dict:
        headers = {"Authorization": f"Bearer {token or self._token}"}
        try:
            resp = self._http.request(method, path, json=json, headers=headers)
        except httpx.HTTPError as exc:
            raise TransientError(f"control plane unreachable: {type(exc).__name__}") from exc
        if resp.status_code in (401, 403):
            raise AuthError(f"control plane rejected credentials ({resp.status_code})")
        if resp.status_code == 429 or resp.status_code >= 500:
            raise TransientError(f"control plane returned {resp.status_code}")
        if resp.status_code >= 400:
            raise ValueError(f"control plane rejected request ({resp.status_code}): {resp.text[:300]}")
        return resp.json()

    def register(self, enrollment_token: str, registration: AgentRegistration) -> tuple[str, str]:
        data = self._request("POST", "/agent/v1/register", token=enrollment_token,
                             json=registration.model_dump(mode="json"))
        return data["agent_id"], data["agent_token"]

    def heartbeat(self, hb: AgentHeartbeat) -> dict:
        return self._request("POST", "/agent/v1/heartbeat", json=hb.model_dump(mode="json"))

    def claim(self, max_jobs: int) -> list[JobSpec]:
        data = self._request("POST", "/agent/v1/jobs/claim", json={"max_jobs": max_jobs})
        return ClaimResponse.model_validate(data).jobs

    def submit(self, results: list[MeasurementResult]) -> ResultBatchResponse:
        batch = ResultBatch(results=results)
        data = self._request("POST", "/agent/v1/results", json=batch.model_dump(mode="json"))
        return ResultBatchResponse.model_validate(data)

    def close(self) -> None:
        self._http.close()
