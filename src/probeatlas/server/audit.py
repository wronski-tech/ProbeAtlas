from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from .models import AuditEvent


def audit(
    db: Session,
    action: str,
    *,
    workspace_id: uuid.UUID | None,
    actor_type: str,
    actor_id: Any = None,
    target_type: str | None = None,
    target_id: Any = None,
    ip: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Record a security-relevant event in the caller's transaction."""
    db.add(
        AuditEvent(
            workspace_id=workspace_id,
            actor_type=actor_type,
            actor_id=str(actor_id) if actor_id else None,
            action=action,
            target_type=target_type,
            target_id=str(target_id) if target_id else None,
            ip=ip,
            details=details or {},
        )
    )
