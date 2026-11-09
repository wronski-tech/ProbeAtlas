"""Operator commands: migrations, customer onboarding, agent enrollment, agent management.

probeatlas-admin migrate
probeatlas-admin create-workspace --name "Acme" --owner-email ops@acme.example
probeatlas-admin create-enrollment-token --region eu-central --max-uses 1 --expires-in-hours 24
probeatlas-admin list-agents
probeatlas-admin disable-agent <agent-id>
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session


def migrate(revision: str = "head") -> None:
    """Apply Alembic migrations up to ``revision`` against ``PROBEATLAS_DATABASE_URL``."""
    from alembic import command
    from alembic.config import Config

    from .config import get_settings

    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url.replace("%", "%%"))
    command.upgrade(cfg, revision)


def create_workspace(db: Session, name: str, owner_email: str) -> dict[str, str]:
    """Create an ownerless workspace and an owner invitation for ``owner_email``; return the invitation.

    The invitee opens ``/invite/<token>`` in the dashboard and registers (or signs in) with that email to
    become the owner. This is how new customers get an account when sign-up is invite-only.
    """
    from .api.auth import INVITATION_TTL, new_workspace
    from .audit import audit
    from .config import get_settings
    from .models import Invitation
    from .schemas import InvitationCreate
    from .security import PREFIX_INVITATION, new_token, token_hash

    email = InvitationCreate(email=owner_email, role="owner").email.lower()  # validates the address
    ws = new_workspace(db, get_settings(), name, owner=None)
    token, now = new_token(PREFIX_INVITATION), datetime.now(UTC)
    inv = Invitation(workspace_id=ws.id, email=email, role="owner",
                     token_hash=token_hash(token), created_at=now, expires_at=now + INVITATION_TTL)
    db.add(inv)
    db.flush()
    audit(db, "workspace.created", workspace_id=ws.id, actor_type="system", target_type="workspace",
          target_id=ws.id, details={"name": name})
    audit(db, "invitation.created", workspace_id=ws.id, actor_type="system", target_type="invitation",
          target_id=inv.id, details={"role": "owner"})
    db.commit()
    return {"workspace_id": str(ws.id), "owner_email": inv.email, "invitation_token": token,
            "invite_path": f"/invite/{token}", "expires_at": inv.expires_at.isoformat()}


def main(argv: list[str] | None = None) -> None:
    """Parse ``probeatlas-admin`` arguments and run the selected operator command."""
    parser = argparse.ArgumentParser(prog="probeatlas-admin")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="apply database migrations")
    w = sub.add_parser("create-workspace",
                       help="onboard a customer: create a workspace and an owner invitation (invite-only sign-up)")
    w.add_argument("--name", required=True)
    w.add_argument("--owner-email", required=True)
    t = sub.add_parser("create-enrollment-token", help="issue a token that lets agents enroll")
    t.add_argument("--region", help="restrict enrollment to this region")
    t.add_argument("--max-uses", type=int, default=1)
    t.add_argument("--expires-in-hours", type=int, default=24)
    t.add_argument("--description", default="created by probeatlas-admin")
    sub.add_parser("list-agents")
    d = sub.add_parser("disable-agent", help="revoke an agent's identity")
    d.add_argument("agent_id", type=uuid.UUID)
    args = parser.parse_args(argv)

    if args.cmd == "migrate":
        migrate()
        print("migrations applied")
        return

    from .audit import audit
    from .db import new_session
    from .models import Agent, EnrollmentToken
    from .security import PREFIX_ENROLLMENT, new_token, token_hash

    with new_session() as db:
        if args.cmd == "create-workspace":
            print(json.dumps(create_workspace(db, args.name, args.owner_email), indent=2))
        elif args.cmd == "create-enrollment-token":
            token = new_token(PREFIX_ENROLLMENT)
            et = EnrollmentToken(
                token_hash=token_hash(token), description=args.description, region=args.region,
                max_uses=args.max_uses, expires_at=datetime.now(UTC) + timedelta(hours=args.expires_in_hours),
            )
            db.add(et)
            db.flush()
            audit(db, "enrollment_token.created", workspace_id=None, actor_type="system",
                  target_type="enrollment_token", target_id=et.id,
                  details={"region": args.region, "max_uses": args.max_uses})
            db.commit()
            print(json.dumps({"id": str(et.id), "token": token, "region": args.region,
                              "max_uses": args.max_uses, "expires_at": et.expires_at.isoformat()}, indent=2))
        elif args.cmd == "list-agents":
            for a in db.execute(select(Agent).order_by(Agent.region, Agent.name)).scalars():
                print(json.dumps({"id": str(a.id), "name": a.name, "region": a.region, "status": a.status,
                                  "version": a.version, "capabilities": a.capabilities,
                                  "last_heartbeat_at": a.last_heartbeat_at.isoformat() if a.last_heartbeat_at
                                  else None}))
        elif args.cmd == "disable-agent":
            agent = db.get(Agent, args.agent_id)
            if agent is None:
                sys.exit("agent not found")
            agent.status = "disabled"
            audit(db, "agent.disabled", workspace_id=None, actor_type="system", target_type="agent",
                  target_id=agent.id)
            db.commit()
            print(f"agent {agent.id} disabled")


if __name__ == "__main__":
    main()
