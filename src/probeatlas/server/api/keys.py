"""API key management. Owner sessions only: a leaked key cannot mint new keys."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..audit import audit
from ..db import get_db
from ..deps import Principal, require_owner, tenant_get
from ..errors import APIError
from ..models import ApiKey
from ..schemas import ApiKeyCreate, ApiKeyIssued, ApiKeyOut
from ..security import new_api_key, token_hash

router = APIRouter(prefix="/v1/api-keys", tags=["api keys"])


def _issue(db: Session, p: Principal, name: str, expires_at: datetime | None) -> ApiKeyIssued:
    prefix, key = new_api_key()
    row = ApiKey(workspace_id=p.workspace_id, name=name, prefix=prefix, key_hash=token_hash(key),
                 created_by=p.user_id, expires_at=expires_at)
    db.add(row)
    db.flush()
    db.refresh(row)
    return ApiKeyIssued(**ApiKeyOut.model_validate(row).model_dump(), key=key)


@router.get("", response_model=list[ApiKeyOut])
def list_keys(p: Principal = Depends(require_owner), db: Session = Depends(get_db)):
    return db.execute(
        select(ApiKey).where(ApiKey.workspace_id == p.workspace_id).order_by(ApiKey.created_at.desc())
    ).scalars().all()


@router.post("", response_model=ApiKeyIssued, status_code=201)
def create_key(body: ApiKeyCreate, p: Principal = Depends(require_owner), db: Session = Depends(get_db)):
    active = db.execute(select(ApiKey.id).where(ApiKey.workspace_id == p.workspace_id,
                                                ApiKey.revoked_at.is_(None))).all()
    if len(active) >= 50:
        raise APIError(403, "limit_exceeded", "at most 50 active API keys per workspace")
    expires = datetime.now(UTC) + timedelta(days=body.expires_in_days) if body.expires_in_days else None
    issued = _issue(db, p, body.name, expires)
    audit(db, "api_key.created", workspace_id=p.workspace_id, actor_type=p.actor_type, actor_id=p.actor_id,
          target_type="api_key", target_id=issued.id, ip=p.ip, details={"prefix": issued.prefix})
    db.commit()
    return issued


@router.delete("/{key_id}", status_code=204)
def revoke_key(key_id: uuid.UUID, p: Principal = Depends(require_owner), db: Session = Depends(get_db)):
    key = tenant_get(db, ApiKey, key_id, p, "API key")
    if key.revoked_at is None:
        key.revoked_at = datetime.now(UTC)
        audit(db, "api_key.revoked", workspace_id=p.workspace_id, actor_type=p.actor_type, actor_id=p.actor_id,
              target_type="api_key", target_id=key.id, ip=p.ip, details={"prefix": key.prefix})
        db.commit()


@router.post("/{key_id}/rotate", response_model=ApiKeyIssued, status_code=201)
def rotate_key(key_id: uuid.UUID, p: Principal = Depends(require_owner), db: Session = Depends(get_db)):
    """Revoke the key immediately and issue a replacement with the same name and expiry policy."""
    old = tenant_get(db, ApiKey, key_id, p, "API key")
    if old.revoked_at is not None:
        raise APIError(409, "key_revoked", "cannot rotate a revoked key")
    old.revoked_at = datetime.now(UTC)
    expires = None
    if old.expires_at:
        expires = datetime.now(UTC) + (old.expires_at - old.created_at)
    issued = _issue(db, p, old.name, expires)
    audit(db, "api_key.rotated", workspace_id=p.workspace_id, actor_type=p.actor_type, actor_id=p.actor_id,
          target_type="api_key", target_id=issued.id, ip=p.ip,
          details={"old_prefix": old.prefix, "new_prefix": issued.prefix})
    db.commit()
    return issued
