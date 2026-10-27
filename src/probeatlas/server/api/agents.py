"""Agent directory (tenant-facing) and the agent protocol (``/agent/v1``)."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from probeatlas.common.schemas import (
    AGENT_PROTOCOL_VERSION,
    AgentHeartbeat,
    AgentRegistration,
    ClaimRequest,
    ClaimResponse,
    ResultBatch,
    ResultBatchResponse,
)

from .. import metrics
from ..audit import audit
from ..db import get_db
from ..deps import Principal, _bearer, client_ip, get_agent, get_principal
from ..errors import APIError
from ..ingestion import ingest_results
from ..models import Agent, EnrollmentToken
from ..ratelimit import limiter
from ..scheduling import claim_jobs
from ..schemas import AgentOut, LocationOut
from ..security import PREFIX_AGENT, PREFIX_ENROLLMENT, new_token, token_hash

router = APIRouter(prefix="/v1", tags=["agents"])
protocol = APIRouter(prefix="/agent/v1", tags=["agent protocol"])


@router.get("/agents", response_model=list[AgentOut])
def list_agents(p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """Platform measurement agents visible to all workspaces. Locations are operator-configured."""
    return db.execute(select(Agent).where(Agent.status != "disabled").order_by(Agent.region, Agent.name)).scalars()


@router.get("/locations", response_model=list[LocationOut])
def list_locations(p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """Regions that monitors can select, with current agent availability."""
    out: dict[str, LocationOut] = {}
    for a in db.execute(select(Agent).where(Agent.status != "disabled")).scalars():
        loc = out.setdefault(a.region, LocationOut(region=a.region, country_codes=[], cities=[], agents_total=0,
                                                   agents_online=0, capabilities=[]))
        loc.agents_total += 1
        loc.agents_online += a.status == "online"
        if a.country_code and a.country_code not in loc.country_codes:
            loc.country_codes.append(a.country_code)
        if a.city and a.city not in loc.cities:
            loc.cities.append(a.city)
        if a.status == "online":
            loc.capabilities = sorted(set(loc.capabilities) | set(a.capabilities))
    return sorted(out.values(), key=lambda loc: loc.region)


# --- agent protocol -------------------------------------------------------------


@protocol.post("/register", status_code=201)
def register(body: AgentRegistration, request: Request, authorization: str | None = Header(default=None),
             db: Session = Depends(get_db)):
    """Exchange an enrollment token for a long-lived agent identity."""
    limiter.hit(f"enroll:{client_ip(request)}", 10)
    token = _bearer(authorization)
    if not token.startswith(PREFIX_ENROLLMENT + "_"):
        raise APIError(401, "unauthorized", "enrollment token required")
    now = datetime.now(UTC)
    et = db.execute(
        select(EnrollmentToken).where(EnrollmentToken.token_hash == token_hash(token)).with_for_update()
    ).scalar_one_or_none()
    if (et is None or et.revoked_at is not None or (et.expires_at and et.expires_at <= now)
            or et.uses >= et.max_uses):
        audit(db, "agent.enrollment_rejected", workspace_id=None, actor_type="agent", ip=client_ip(request),
              details={"region": body.region})
        db.commit()
        raise APIError(401, "unauthorized", "invalid, expired, or exhausted enrollment token")
    if et.region and et.region != body.region:
        raise APIError(403, "forbidden", f"enrollment token is restricted to region {et.region}")
    et.uses += 1
    agent_token = new_token(PREFIX_AGENT)
    agent = Agent(name=body.name, region=body.region, country_code=body.country_code, city=body.city,
                  provider=body.provider, version=body.version, capabilities=[c.value for c in body.capabilities],
                  token_hash=token_hash(agent_token), status="online", last_heartbeat_at=now,
                  last_seen_ip=client_ip(request), enrollment_token_id=et.id)
    db.add(agent)
    db.flush()
    audit(db, "agent.enrolled", workspace_id=None, actor_type="agent", actor_id=agent.id, target_type="agent",
          target_id=agent.id, ip=client_ip(request), details={"region": body.region, "name": body.name})
    db.commit()
    return {"agent_id": str(agent.id), "agent_token": agent_token, "protocol_version": AGENT_PROTOCOL_VERSION}


@protocol.post("/heartbeat")
def heartbeat(body: AgentHeartbeat, request: Request, agent: Agent = Depends(get_agent),
              db: Session = Depends(get_db)):
    """Record an agent heartbeat and refresh its version, capabilities, and status.

    An agent marked offline becomes ``online`` again on its next heartbeat. Disabled agents are rejected.
    """
    limiter.hit(f"agent:{agent.id}:hb", 60)
    now = datetime.now(UTC)
    db.execute(
        # The status guard keeps a heartbeat racing `disable-agent` from re-enabling the agent.
        update(Agent).where(Agent.id == agent.id, Agent.status != "disabled").values(
            last_heartbeat_at=now, status="online", version=body.version,
            capabilities=[c.value for c in body.capabilities], last_seen_ip=client_ip(request),
        )
    )
    db.commit()
    metrics.AGENT_HEARTBEATS.inc()
    return {"status": "ok", "server_time": now.isoformat(), "protocol_version": AGENT_PROTOCOL_VERSION}


@protocol.post("/jobs/claim", response_model=ClaimResponse)
def claim(body: ClaimRequest, agent: Agent = Depends(get_agent), db: Session = Depends(get_db)):
    """Lease up to ``max_jobs`` due jobs in the agent's region that it is able to run.

    Each job must be reported before its ``lease_expires_at``; otherwise it may be retried by another agent.
    """
    limiter.hit(f"agent:{agent.id}:claim", 600)
    return ClaimResponse(jobs=claim_jobs(db, agent, body.max_jobs))


@protocol.post("/results", response_model=ResultBatchResponse)
def submit_results(body: ResultBatch, agent: Agent = Depends(get_agent), db: Session = Depends(get_db)):
    """Submit measurement results for leased jobs.

    Each result is acknowledged as ``accepted``, ``duplicate`` (already stored; safe to drop), or
    ``rejected`` with a reason. Tenant and target are taken from the job, never from the payload.
    """
    limiter.hit(f"agent:{agent.id}:results", 600)
    return ResultBatchResponse(acks=ingest_results(db, agent, body.results))
