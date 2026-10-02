"""Operator role management.

Roles are changed only through this service (used by the `app.admin` CLI, which requires
deployment-level database access). There is deliberately no HTTP endpoint, so a web
session can never escalate its own privileges.
"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import User
from app.services.audit import record_audit

ROLES = ("candidate", "operator")


class RoleChangeError(ValueError):
    pass


def set_user_role(db: Session, *, email: str, role: str, actor: str, reason: str) -> User:
    if role not in ROLES:
        raise RoleChangeError(f"Unknown role: {role}")
    if not reason.strip():
        raise RoleChangeError("A reason is required for the audit trail")
    user = db.scalar(select(User).where(User.email == email.strip().lower()))
    if user is None:
        raise RoleChangeError("No account with that email; the person must register first")
    if user.status != "active":
        raise RoleChangeError("Only active accounts can change role")
    previous = user.role
    if previous == role:
        return user
    if previous == "operator" and role != "operator":
        remaining = db.scalar(
            select(func.count()).select_from(User).where(
                User.role == "operator", User.status == "active", User.id != user.id
            )
        )
        if not remaining:
            raise RoleChangeError("Refusing to remove the last active operator")
    user.role = role
    record_audit(
        db,
        action="user.role_changed",
        actor=actor,
        user_id=user.id,
        object_type="user",
        object_id=user.id,
        metadata={"from": previous, "to": role, "reason": reason.strip()[:200]},
    )
    db.flush()
    return user


def list_operators(db: Session) -> list[User]:
    return list(
        db.scalars(select(User).where(User.role == "operator").order_by(User.email)).all()
    )
