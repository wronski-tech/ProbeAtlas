"""Accounts, sessions, workspaces, and memberships."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from ..audit import audit
from ..config import Settings, get_settings
from ..db import get_db
from ..deps import Principal, client_ip, get_current_user, get_principal, require_owner, tenant_get
from ..errors import APIError, not_found
from ..models import Invitation, Membership, User, UserSession, Workspace
from ..ratelimit import limiter
from ..schemas import (
    InvitationAccept,
    InvitationCreate,
    InvitationIssued,
    InvitationOut,
    LoginRequest,
    MemberOut,
    MeOut,
    RegisterRequest,
    SessionOut,
    UserOut,
    WorkspaceCreate,
    WorkspaceOut,
)
from ..security import (
    DUMMY_PASSWORD_HASH,
    PREFIX_INVITATION,
    PREFIX_SESSION,
    hash_password,
    new_token,
    token_hash,
    verify_password,
)

router = APIRouter(prefix="/v1", tags=["auth & workspaces"])


def _workspaces(db: Session, user_id: uuid.UUID) -> list[WorkspaceOut]:
    """Return the workspaces ``user_id`` belongs to, in the order they joined, with their role in each."""
    rows = db.execute(
        select(Workspace, Membership.role)
        .join(Membership, Membership.workspace_id == Workspace.id)
        .where(Membership.user_id == user_id)
        # Order by when the user joined: their own first workspace stays first, so the dashboard's default
        # workspace at sign-in never becomes one they were invited into.
        .order_by(Membership.created_at, Workspace.id)
    ).all()
    return [WorkspaceOut.model_validate(ws).model_copy(update={"role": role}) for ws, role in rows]


def new_workspace(db: Session, settings: Settings, name: str, owner: User | None) -> Workspace:
    """Create a workspace with default limits and make ``owner`` its owner (caller commits).

    ``owner`` is ``None`` only for operator-created workspaces, whose first owner joins by invitation.
    """
    ws = Workspace(name=name, max_monitors=settings.default_max_monitors,
                   min_interval_seconds=settings.min_interval_seconds)
    db.add(ws)
    db.flush()
    if owner is not None:
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
    """Create an account and return a session.

    With an ``invitation_token`` the account joins the inviting workspace with the invited role. Without
    one, a new workspace is created, which requires open registration on this server.

    Rate limited per client IP. Returns 403 ``invitation_required`` when sign-up is invite-only and no
    invitation was given, 403 ``invalid_invitation`` when the invitation is unknown, expired, used, revoked,
    or addressed to another email, and 409 if the email is taken.
    """
    ip = client_ip(request)
    limiter.hit(f"auth:{ip}", settings.auth_rate_limit_per_minute)
    email, now = body.email.lower(), datetime.now(UTC)
    inv = None
    if body.invitation_token:
        inv = db.execute(_pending(select(Invitation), now).where(
            Invitation.token_hash == token_hash(body.invitation_token)).with_for_update()).scalar_one_or_none()
        if inv is None or inv.email != email:
            raise APIError(403, "invalid_invitation",
                           "this invitation is invalid, expired, or was sent to a different email address")
    elif settings.registration != "open":
        raise APIError(403, "invitation_required", "sign-up is by invitation only")
    if db.execute(select(User.id).where(User.email == email)).first():
        raise APIError(409, "email_taken", "an account with this email already exists")
    user = User(email=email, name=body.name, password_hash=hash_password(body.password))
    db.add(user)
    db.flush()
    if inv is None:
        ws_id = new_workspace(db, settings, body.workspace_name, user).id
    else:
        ws_id = inv.workspace_id
        inv.accepted_at, inv.accepted_by = now, user.id
        db.add(Membership(workspace_id=ws_id, user_id=user.id, role=inv.role))
        audit(db, "invitation.accepted", workspace_id=ws_id, actor_type="user", actor_id=user.id,
              target_type="invitation", target_id=inv.id, ip=ip, details={"role": inv.role})
    audit(db, "user.registered", workspace_id=ws_id, actor_type="user", actor_id=user.id,
          target_type="user", target_id=user.id, ip=ip)
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
    ws = new_workspace(db, settings, body.name, user)
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


INVITATION_TTL = timedelta(days=7)
MAX_PENDING_INVITATIONS = 50


def _pending(query: Select, now: datetime) -> Select:
    """Restrict an invitation query to invitations that can still be accepted."""
    return query.where(Invitation.accepted_at.is_(None), Invitation.revoked_at.is_(None),
                          Invitation.expires_at > now)


@router.post("/invitations", response_model=InvitationIssued, status_code=201)
def create_invitation(body: InvitationCreate, p: Principal = Depends(require_owner), db: Session = Depends(get_db)):
    """Invite someone to the workspace by email (owner only). The secret token is returned once.

    Nobody is added until the invitee accepts with their own account, and the response is identical whether
    or not an account with this email exists. Invitations expire after 7 days; at most 50 can be pending.
    """
    email, now = body.email.lower(), datetime.now(UTC)
    already = db.execute(select(Membership.user_id).join(User, User.id == Membership.user_id).where(
        Membership.workspace_id == p.workspace_id, User.email == email)).first()
    if already:
        raise APIError(409, "already_member", "a member with this email is already in the workspace")
    pending = db.execute(_pending(select(func.count()).select_from(Invitation), now)
                         .where(Invitation.workspace_id == p.workspace_id)).scalar_one()
    if pending >= MAX_PENDING_INVITATIONS:
        raise APIError(403, "limit_exceeded", f"at most {MAX_PENDING_INVITATIONS} pending invitations")
    token = new_token(PREFIX_INVITATION)
    inv = Invitation(workspace_id=p.workspace_id, email=email, role=body.role, token_hash=token_hash(token),
                     created_by=p.user_id, created_at=now, expires_at=now + INVITATION_TTL)
    db.add(inv)
    db.flush()
    audit(db, "invitation.created", workspace_id=p.workspace_id, actor_type=p.actor_type, actor_id=p.actor_id,
          target_type="invitation", target_id=inv.id, ip=p.ip, details={"role": body.role})
    db.commit()
    return InvitationIssued(**InvitationOut.model_validate(inv).model_dump(), token=token)


@router.get("/invitations", response_model=list[InvitationOut])
def list_invitations(p: Principal = Depends(require_owner), db: Session = Depends(get_db)):
    """List the workspace's pending invitations (owner only)."""
    q = _pending(select(Invitation), datetime.now(UTC)).where(Invitation.workspace_id == p.workspace_id)
    return db.execute(q.order_by(Invitation.created_at.desc())).scalars().all()


@router.delete("/invitations/{invitation_id}", status_code=204)
def revoke_invitation(invitation_id: uuid.UUID, p: Principal = Depends(require_owner),
                      db: Session = Depends(get_db)):
    """Revoke a pending invitation so its token can no longer be used (owner only)."""
    inv = tenant_get(db, Invitation, invitation_id, p, "invitation")
    if inv.accepted_at is None and inv.revoked_at is None:
        inv.revoked_at = datetime.now(UTC)
        audit(db, "invitation.revoked", workspace_id=p.workspace_id, actor_type=p.actor_type,
              actor_id=p.actor_id, target_type="invitation", target_id=inv.id, ip=p.ip)
        db.commit()


@router.post("/invitations/accept", response_model=WorkspaceOut)
def accept_invitation(body: InvitationAccept, request: Request, user: User = Depends(get_current_user),
                      db: Session = Depends(get_db)):
    """Join a workspace by accepting an invitation (session tokens only).

    The invitation must be pending and addressed to the signed-in account's email. Unknown, expired,
    revoked, used, and mismatched tokens all get the same 404.
    """
    limiter.hit(f"invite:{user.id}", 20)
    now = datetime.now(UTC)
    inv = db.execute(_pending(select(Invitation), now).where(
        Invitation.token_hash == token_hash(body.token)).with_for_update()).scalar_one_or_none()
    if inv is None or inv.email != user.email.lower():
        raise not_found("invitation")
    if db.get(Membership, (inv.workspace_id, user.id)):
        raise APIError(409, "already_member", "you are already a member of this workspace")
    inv.accepted_at, inv.accepted_by = now, user.id
    db.add(Membership(workspace_id=inv.workspace_id, user_id=user.id, role=inv.role))
    audit(db, "invitation.accepted", workspace_id=inv.workspace_id, actor_type="user", actor_id=user.id,
          target_type="invitation", target_id=inv.id, ip=client_ip(request), details={"role": inv.role})
    db.commit()
    ws = db.get(Workspace, inv.workspace_id)
    return WorkspaceOut.model_validate(ws).model_copy(update={"role": inv.role})


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
