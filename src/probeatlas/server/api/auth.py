"""Accounts, sessions, workspaces, and memberships."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..audit import audit
from ..config import Settings, get_settings
from ..db import get_db
from ..deps import Principal, client_ip, get_current_user, get_principal, require_owner
from ..errors import APIError, not_found
from ..models import Membership, User, UserSession, Workspace
from ..ratelimit import limiter
from ..schemas import (
    LoginRequest,
    MemberAdd,
    MemberOut,
    MeOut,
    RegisterRequest,
    SessionOut,
    UserOut,
    WorkspaceCreate,
    WorkspaceOut,
)
from ..security import DUMMY_PASSWORD_HASH, PREFIX_SESSION, hash_password, new_token, token_hash, verify_password

router = APIRouter(prefix="/v1", tags=["auth & workspaces"])


def _workspaces(db: Session, user_id: uuid.UUID) -> list[WorkspaceOut]:
    """Return the workspaces ``user_id`` belongs to, with their role in each."""
    rows = db.execute(
        select(Workspace, Membership.role)
        .join(Membership, Membership.workspace_id == Workspace.id)
        .where(Membership.user_id == user_id)
        .order_by(Workspace.created_at)
    ).all()
    return [WorkspaceOut.model_validate(ws).model_copy(update={"role": role}) for ws, role in rows]


def _new_workspace(db: Session, settings: Settings, name: str, owner: User) -> Workspace:
    """Create a workspace with default limits and make ``owner`` its owner (caller commits)."""
    ws = Workspace(name=name, max_monitors=settings.default_max_monitors,
                   min_interval_seconds=settings.min_interval_seconds)
    db.add(ws)
    db.flush()
    db.add(Membership(workspace_id=ws.id, user_id=owner.id, role="owner"))
    return ws


def _issue_session(db: Session, settings: Settings, user: User) -> SessionOut:
    """Create a session for ``user``, record the login time, and commit."""
    token = new_token(PREFIX_SESSION)
    expires = datetime.now(UTC) + timedelta(hours=settings.session_ttl_hours)
    db.add(UserSession(user_id=user.id, token_hash=token_hash(token), expires_at=expires))
    user.last_login_at = datetime.now(UTC)
    db.commit()
    return SessionOut(token=token, expires_at=expires, user=UserOut.model_validate(user),
                      workspaces=_workspaces(db, user.id))


@router.post("/auth/register", response_model=SessionOut, status_code=201)
def register(body: RegisterRequest, request: Request, db: Session = Depends(get_db),
             settings: Settings = Depends(get_settings)):
    """Create an account and a first workspace, and return a session.

    Rate limited per client IP. Returns 403 when registration is disabled and 409 if the email is taken.
    """
    limiter.hit(f"auth:{client_ip(request)}", settings.auth_rate_limit_per_minute)
    if not settings.allow_registration:
        raise APIError(403, "registration_disabled", "self-service registration is disabled")
    email = body.email.lower()
    if db.execute(select(User.id).where(User.email == email)).first():
        raise APIError(409, "email_taken", "an account with this email already exists")
    user = User(email=email, name=body.name, password_hash=hash_password(body.password))
    db.add(user)
    db.flush()
    ws = _new_workspace(db, settings, body.workspace_name, user)
    audit(db, "user.registered", workspace_id=ws.id, actor_type="user", actor_id=user.id,
          target_type="user", target_id=user.id, ip=client_ip(request))
    return _issue_session(db, settings, user)


@router.post("/auth/login", response_model=SessionOut)
def login(body: LoginRequest, request: Request, db: Session = Depends(get_db),
          settings: Settings = Depends(get_settings)):
    """Exchange email and password for a session token.

    Rate limited per client IP. Unknown emails and wrong passwords get the same 401 response and the same
    hashing cost, so the endpoint does not reveal which accounts exist.
    """
    ip = client_ip(request)
    limiter.hit(f"auth:{ip}", settings.auth_rate_limit_per_minute)
    user = db.execute(select(User).where(User.email == body.email.lower())).scalar_one_or_none()
    ok = verify_password(body.password, user.password_hash if user else DUMMY_PASSWORD_HASH)
    if not user or not ok or not user.is_active:
        audit(db, "user.login_failed", workspace_id=None, actor_type="user", actor_id=user.id if user else None,
              ip=ip, details={"email_domain": body.email.split("@")[-1]})
        db.commit()
        raise APIError(401, "invalid_credentials", "invalid email or password")
    audit(db, "user.login", workspace_id=None, actor_type="user", actor_id=user.id, ip=ip)
    return _issue_session(db, settings, user)


@router.post("/auth/logout", status_code=204)
def logout(authorization: str | None = Header(default=None), user: User = Depends(get_current_user),
           db: Session = Depends(get_db)):
    """Revoke the current session token."""
    token = (authorization or "").split(" ", 1)[-1]
    sess = db.execute(select(UserSession).where(UserSession.token_hash == token_hash(token))).scalar_one_or_none()
    if sess:
        sess.revoked_at = datetime.now(UTC)
        db.commit()


@router.get("/auth/me", response_model=MeOut)
def me(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Return the signed-in user and their workspaces (session tokens only)."""
    return MeOut(user=UserOut.model_validate(user), workspaces=_workspaces(db, user.id))


@router.get("/workspaces", response_model=list[WorkspaceOut])
def list_workspaces(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """List the workspaces the signed-in user belongs to (session tokens only)."""
    return _workspaces(db, user.id)


@router.post("/workspaces", response_model=WorkspaceOut, status_code=201)
def create_workspace(body: WorkspaceCreate, request: Request, user: User = Depends(get_current_user),
                     db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    """Create a workspace owned by the signed-in user (at most 10 owned workspaces)."""
    count = db.execute(select(func.count()).select_from(Membership).where(Membership.user_id == user.id,
                                                                          Membership.role == "owner")).scalar_one()
    if count >= 10:
        raise APIError(403, "limit_exceeded", "a user may own at most 10 workspaces")
    ws = _new_workspace(db, settings, body.name, user)
    audit(db, "workspace.created", workspace_id=ws.id, actor_type="user", actor_id=user.id,
          target_type="workspace", target_id=ws.id, ip=client_ip(request))
    db.commit()
    return WorkspaceOut.model_validate(ws).model_copy(update={"role": "owner"})


@router.get("/members", response_model=list[MemberOut])
def list_members(p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """List the members of the current workspace and their roles."""
    rows = db.execute(
        select(Membership, User).join(User, User.id == Membership.user_id)
        .where(Membership.workspace_id == p.workspace_id).order_by(Membership.created_at)
    ).all()
    return [MemberOut(user_id=u.id, email=u.email, name=u.name, role=m.role, created_at=m.created_at)
            for m, u in rows]


@router.post("/members", response_model=MemberOut, status_code=201)
def add_member(body: MemberAdd, p: Principal = Depends(require_owner), db: Session = Depends(get_db)):
    """Add a registered user to the workspace by email (owner only)."""
    user = db.execute(select(User).where(User.email == body.email.lower())).scalar_one_or_none()
    if user is None:
        raise APIError(404, "user_not_found", "no user with this email; ask them to register first")
    if db.get(Membership, (p.workspace_id, user.id)):
        raise APIError(409, "already_member", "user is already a member")
    m = Membership(workspace_id=p.workspace_id, user_id=user.id, role=body.role)
    db.add(m)
    audit(db, "member.added", workspace_id=p.workspace_id, actor_type=p.actor_type, actor_id=p.actor_id,
          target_type="user", target_id=user.id, ip=p.ip, details={"role": body.role})
    db.commit()
    return MemberOut(user_id=user.id, email=user.email, name=user.name, role=m.role, created_at=m.created_at)


@router.delete("/members/{user_id}", status_code=204)
def remove_member(user_id: uuid.UUID, p: Principal = Depends(require_owner), db: Session = Depends(get_db)):
    """Remove a member from the workspace (owner only). The last owner cannot be removed."""
    m = db.get(Membership, (p.workspace_id, user_id))
    if m is None:
        raise not_found("member")
    if m.role == "owner":
        owners = db.execute(select(func.count()).select_from(Membership).where(
            Membership.workspace_id == p.workspace_id, Membership.role == "owner")).scalar_one()
        if owners <= 1:
            raise APIError(409, "last_owner", "cannot remove the last owner")
    db.delete(m)
    audit(db, "member.removed", workspace_id=p.workspace_id, actor_type=p.actor_type, actor_id=p.actor_id,
          target_type="user", target_id=user_id, ip=p.ip)
    db.commit()
