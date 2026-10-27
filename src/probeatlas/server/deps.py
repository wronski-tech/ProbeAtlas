"""Authentication and tenant-resolution dependencies.

Every workspace-scoped endpoint depends on ``get_principal``, which yields the
workspace the caller is authorized for. Endpoints must filter every query by
``principal.workspace_id``; ``tenant_get`` below does this for single rows.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TypeVar

from fastapi import Depends, Header, Request
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .db import get_db
from .errors import APIError, not_found
from .models import Agent, ApiKey, Membership, User, UserSession
from .ratelimit import limiter
from .security import PREFIX_AGENT, PREFIX_API_KEY, PREFIX_SESSION, token_hash

T = TypeVar("T")


@dataclass(frozen=True)
class Principal:
    workspace_id: uuid.UUID
    actor_type: str  # user | api_key
    actor_id: uuid.UUID
    role: str  # owner | member
    user_id: uuid.UUID | None
    ip: str | None

    @property
    def is_owner(self) -> bool:
        return self.role == "owner"


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise APIError(401, "unauthorized", "missing bearer token", headers={"WWW-Authenticate": "Bearer"})
    return authorization.split(" ", 1)[1].strip()


def _now() -> datetime:
    return datetime.now(UTC)


def _session_user(db: Session, token: str) -> User:
    row = db.execute(
        select(User)
        .join(UserSession, UserSession.user_id == User.id)
        .where(
            UserSession.token_hash == token_hash(token),
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > _now(),
            User.is_active.is_(True),
        )
    ).scalar_one_or_none()
    if row is None:
        raise APIError(401, "unauthorized", "invalid or expired session")
    return row


def get_current_user(
    authorization: str | None = Header(default=None), db: Session = Depends(get_db)
) -> User:
    """Session-only endpoints (account and workspace management)."""
    token = _bearer(authorization)
    if not token.startswith(PREFIX_SESSION + "_"):
        raise APIError(403, "forbidden", "this endpoint requires a user session, not an API key")
    return _session_user(db, token)


def get_principal(
    request: Request,
    authorization: str | None = Header(default=None),
    x_workspace_id: uuid.UUID | None = Header(default=None),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Principal:
    token = _bearer(authorization)
    ip = client_ip(request)
    if token.startswith(PREFIX_API_KEY + "_"):
        key = db.execute(
            select(ApiKey).where(ApiKey.key_hash == token_hash(token), ApiKey.revoked_at.is_(None))
        ).scalar_one_or_none()
        if key is None or (key.expires_at and key.expires_at <= _now()):
            raise APIError(401, "unauthorized", "invalid, expired, or revoked API key")
        if x_workspace_id and x_workspace_id != key.workspace_id:
            raise APIError(403, "forbidden", "API key does not belong to this workspace")
        db.execute(update(ApiKey).where(ApiKey.id == key.id).values(last_used_at=_now()))
        db.commit()
        principal = Principal(key.workspace_id, "api_key", key.id, "member", None, ip)
    elif token.startswith(PREFIX_SESSION + "_"):
        user = _session_user(db, token)
        if x_workspace_id is None:
            raise APIError(400, "workspace_required", "X-Workspace-Id header is required with session auth")
        membership = db.get(Membership, (x_workspace_id, user.id))
        if membership is None:
            # Same response as a missing workspace: do not reveal existence.
            raise not_found("workspace")
        principal = Principal(x_workspace_id, "user", user.id, membership.role, user.id, ip)
    else:
        raise APIError(401, "unauthorized", "unrecognized token type")
    limiter.hit(f"ws:{principal.workspace_id}", settings.rate_limit_per_minute)
    return principal


def require_owner(principal: Principal = Depends(get_principal)) -> Principal:
    if principal.actor_type != "user" or not principal.is_owner:
        raise APIError(403, "forbidden", "workspace owner role required")
    return principal


def tenant_get(db: Session, model: type[T], obj_id: uuid.UUID, principal: Principal, what: str) -> T:
    """Fetch a row by id only if it belongs to the caller's workspace."""
    obj = db.get(model, obj_id)
    if obj is None or getattr(obj, "workspace_id", None) != principal.workspace_id:
        raise not_found(what)
    return obj


def get_agent(
    authorization: str | None = Header(default=None), db: Session = Depends(get_db)
) -> Agent:
    token = _bearer(authorization)
    if not token.startswith(PREFIX_AGENT + "_"):
        raise APIError(401, "unauthorized", "agent token required")
    agent = db.execute(select(Agent).where(Agent.token_hash == token_hash(token))).scalar_one_or_none()
    if agent is None or agent.status == "disabled":
        raise APIError(401, "unauthorized", "invalid or disabled agent")
    return agent
