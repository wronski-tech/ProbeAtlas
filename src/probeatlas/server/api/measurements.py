from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from probeatlas.common.schemas import CheckType

from ..db import get_db
from ..deps import Principal, get_principal
from ..errors import APIError
from ..models import Result
from ..schemas import MeasurementOut, Page, decode_cursor, encode_cursor

router = APIRouter(prefix="/v1/measurements", tags=["measurements"])


@router.get("", response_model=Page[MeasurementOut])
def list_measurements(
    monitor_id: uuid.UUID | None = None,
    region: str | None = None,
    agent_id: uuid.UUID | None = None,
    check_type: CheckType | None = None,
    success: bool | None = None,
    since: datetime | None = Query(None, description="Inclusive lower bound on measured_at (default: 24h ago)."),
    until: datetime | None = Query(None, description="Exclusive upper bound on measured_at (default: now)."),
    limit: int = Query(50, ge=1, le=500),
    cursor: str | None = None,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    """Raw measurement history, newest first, filtered to the caller's workspace."""
    until = until or datetime.now(UTC)
    since = since or until - timedelta(hours=24)
    if since >= until:
        raise APIError(422, "validation_error", "`since` must be before `until`")
    q = select(Result).where(
        Result.workspace_id == p.workspace_id, Result.measured_at >= since, Result.measured_at < until
    )
    if monitor_id:
        q = q.where(Result.monitor_id == monitor_id)
    if region:
        q = q.where(Result.region == region)
    if agent_id:
        q = q.where(Result.agent_id == agent_id)
    if check_type:
        q = q.where(Result.check_type == check_type.value)
    if success is not None:
        q = q.where(Result.success.is_(success))
    if cursor:
        ts, rid = decode_cursor(cursor)
        c_ts, c_id = datetime.fromisoformat(ts), uuid.UUID(rid)
        q = q.where(or_(Result.measured_at < c_ts, and_(Result.measured_at == c_ts, Result.id < c_id)))
    rows = db.execute(q.order_by(Result.measured_at.desc(), Result.id.desc()).limit(limit + 1)).scalars().all()
    nxt = encode_cursor(rows[limit - 1].measured_at, rows[limit - 1].id) if len(rows) > limit else None
    return Page(items=[MeasurementOut.model_validate(r) for r in rows[:limit]], next_cursor=nxt)
