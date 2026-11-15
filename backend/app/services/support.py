"""Support access (Phase 7): candidate-granted, time-boxed, read-only, redacted, audited.

- **Grants come from the candidate.** Only the candidate can grant access, to one named
  account holding the `support` role, for 1 to 72 hours, with a stated reason. They can
  revoke it at any time. Grants are deleted with the profile.
- **What support sees.** A summary built only from counts, statuses, and timestamps:
  - which kinds of facts exist and whether they are confirmed, never their values;
  - match, CV, application, submission, and tracker counts;
  - recent activity names.

  No CV, document, answer, contact detail, fact value, job description, or note is ever
  included. The email address is masked.
- **Auditing.** Every view writes a `support.case_viewed` audit event on the candidate's
  record and increments the grant's view count. The candidate sees both.
- **Fail closed.** An expired, revoked, or foreign grant returns nothing.

Support accounts are created only with `python -m app.admin promote-support`, never over
HTTP. Design: `docs/security.md`, "Support access".
"""

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    Application,
    ApplicationSubmission,
    AuditEvent,
    CandidateProfile,
    CVVersion,
    MatchRun,
    PreferenceVersion,
    ProfileFact,
    ProfileVersion,
    StoredArtifact,
    SupportAccessGrant,
    TrackedApplication,
    User,
)
from app.services.agent import auto_matching_enabled
from app.services.applications import is_applied
from app.services.audit import record_audit

MAX_GRANT_HOURS = 72
RECENT_ACTIVITY = 30
# Actions whose names alone could reveal content are never listed.
_HIDDEN_ACTIONS = frozenset({"support.case_viewed"})


class SupportAccessError(ValueError):
    pass


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def grant_state(grant: SupportAccessGrant, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    if grant.revoked_at is not None:
        return "revoked"
    expires = _aware(grant.expires_at)
    return "expired" if expires is None or expires <= now else "active"


def mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


def grant_access(
    db: Session,
    *,
    user: User,
    support_email: str,
    hours: int,
    reason: str,
    request_id: str | None = None,
) -> SupportAccessGrant:
    if not 1 <= hours <= MAX_GRANT_HOURS:
        raise SupportAccessError(f"Access can last from 1 to {MAX_GRANT_HOURS} hours")
    if not reason.strip():
        raise SupportAccessError("Say why support needs access")
    support = db.scalar(select(User).where(User.email == support_email.strip().lower()))
    if support is None or support.role != "support" or support.status != "active":
        raise SupportAccessError("That address is not an active support account")
    if support.id == user.id:
        raise SupportAccessError("You cannot grant access to yourself")
    now = datetime.now(UTC)
    # One live grant per support account: a new grant replaces the old one.
    for previous in db.scalars(
        select(SupportAccessGrant).where(
            SupportAccessGrant.user_id == user.id,
            SupportAccessGrant.support_user_id == support.id,
            SupportAccessGrant.revoked_at.is_(None),
        )
    ):
        previous.revoked_at = now
    grant = SupportAccessGrant(
        user_id=user.id,
        support_user_id=support.id,
        scope="summary",
        reason=reason.strip()[:500],
        granted_at=now,
        expires_at=now + timedelta(hours=hours),
    )
    db.add(grant)
    db.flush()
    record_audit(
        db,
        action="support.access_granted",
        actor="user",
        user_id=user.id,
        object_type="support_access_grant",
        object_id=grant.id,
        metadata={"hours": hours, "scope": grant.scope, "support_user_id": support.id},
        request_id=request_id,
    )
    return grant


def revoke_access(
    db: Session, *, user: User, grant_id: str, request_id: str | None = None
) -> SupportAccessGrant:
    grant = db.get(SupportAccessGrant, grant_id)
    if grant is None or grant.user_id != user.id:
        raise SupportAccessError("Grant not found")
    if grant.revoked_at is None:
        grant.revoked_at = datetime.now(UTC)
        record_audit(
            db,
            action="support.access_revoked",
            actor="user",
            user_id=user.id,
            object_type="support_access_grant",
            object_id=grant.id,
            request_id=request_id,
        )
    return grant


def grants_for_candidate(db: Session, user_id: str) -> list[SupportAccessGrant]:
    return list(
        db.scalars(
            select(SupportAccessGrant)
            .where(SupportAccessGrant.user_id == user_id)
            .order_by(SupportAccessGrant.granted_at.desc())
        )
    )


def open_cases(db: Session, support_user: User) -> list[SupportAccessGrant]:
    if support_user.role != "support":
        return []
    now = datetime.now(UTC)
    return [
        grant
        for grant in db.scalars(
            select(SupportAccessGrant)
            .where(
                SupportAccessGrant.support_user_id == support_user.id,
                SupportAccessGrant.revoked_at.is_(None),
            )
            .order_by(SupportAccessGrant.expires_at)
        )
        if grant_state(grant, now) == "active"
    ]


def _counter(rows: Any) -> dict[str, int]:
    return {str(key): int(count) for key, count in rows}


def case_summary(
    db: Session, *, support_user: User, grant_id: str, request_id: str | None = None
) -> dict[str, Any]:
    """The redacted summary for one active grant. Raises when the grant does not allow it."""
    grant = db.get(SupportAccessGrant, grant_id)
    if (
        grant is None
        or support_user.role != "support"
        or grant.support_user_id != support_user.id
        or grant_state(grant) != "active"
    ):
        raise SupportAccessError("No active access to this case")
    candidate = db.get(User, grant.user_id)
    if candidate is None:
        raise SupportAccessError("No active access to this case")
    profile = db.get(CandidateProfile, candidate.id)
    current_version_id = profile.current_version_id if profile else None
    facts: dict[str, dict[str, int]] = {}
    if current_version_id:
        for fact_type, status, count in db.execute(
            select(ProfileFact.fact_type, ProfileFact.confirmation_status, func.count())
            .where(ProfileFact.profile_version_id == current_version_id)
            .group_by(ProfileFact.fact_type, ProfileFact.confirmation_status)
        ):
            facts.setdefault(fact_type, {})[status] = int(count)
    preference = db.scalar(
        select(PreferenceVersion)
        .where(PreferenceVersion.user_id == candidate.id, PreferenceVersion.status == "active")
        .order_by(PreferenceVersion.created_at.desc())
        .limit(1)
    )
    applications = list(db.scalars(select(Application).where(Application.user_id == candidate.id)))
    application_states = Counter(
        "applied" if is_applied(db, item) else item.status for item in applications
    )
    recent = [
        {"action": action, "occurred_at": occurred_at.isoformat() if occurred_at else None}
        for action, occurred_at in db.execute(
            select(AuditEvent.action, AuditEvent.occurred_at)
            .where(
                AuditEvent.user_id == candidate.id,
                AuditEvent.action.not_in(list(_HIDDEN_ACTIONS)),
            )
            .order_by(AuditEvent.occurred_at.desc())
            .limit(RECENT_ACTIVITY)
        )
    ]
    summary = {
        "grant": {
            "id": grant.id,
            "scope": grant.scope,
            "reason": grant.reason,
            "expires_at": _aware(grant.expires_at).isoformat(),  # type: ignore[union-attr]
        },
        "account": {
            "email": mask_email(candidate.email),
            "status": candidate.status,
            "created_at": _aware(candidate.created_at).isoformat(),  # type: ignore[union-attr]
        },
        "profile": {
            "versions": int(
                db.scalar(
                    select(func.count(ProfileVersion.id)).where(
                        ProfileVersion.user_id == candidate.id
                    )
                )
                or 0
            ),
            "has_current_version": current_version_id is not None,
            "facts_by_type": facts,
            "uploaded_documents": int(
                db.scalar(
                    select(func.count(StoredArtifact.id)).where(
                        StoredArtifact.owner_user_id == candidate.id,
                        StoredArtifact.kind == "profile-source",
                    )
                )
                or 0
            ),
            "preferences_set": preference is not None,
            "location_ladder_set": bool(
                preference and (preference.rules_json or {}).get("location_policy")
            ),
        },
        "agent": {"automatic_matching": auto_matching_enabled(db, candidate.id)},
        "matches_by_eligibility": _counter(
            db.execute(
                select(MatchRun.eligibility_status, func.count())
                .where(MatchRun.user_id == candidate.id)
                .group_by(MatchRun.eligibility_status)
            )
        ),
        "cvs_by_status": _counter(
            db.execute(
                select(CVVersion.status, func.count())
                .where(CVVersion.user_id == candidate.id)
                .group_by(CVVersion.status)
            )
        ),
        "applications_by_state": dict(application_states),
        "submissions_by_state": _counter(
            db.execute(
                select(ApplicationSubmission.state, func.count())
                .where(ApplicationSubmission.user_id == candidate.id)
                .group_by(ApplicationSubmission.state)
            )
        ),
        "tracker_by_stage": _counter(
            db.execute(
                select(TrackedApplication.stage, func.count())
                .where(TrackedApplication.user_id == candidate.id)
                .group_by(TrackedApplication.stage)
            )
        ),
        "recent_activity": recent,
    }
    grant.view_count += 1
    grant.last_viewed_at = datetime.now(UTC)
    record_audit(
        db,
        action="support.case_viewed",
        actor=f"support:{support_user.id}"[:64],
        user_id=candidate.id,
        object_type="support_access_grant",
        object_id=grant.id,
        metadata={"support_user_id": support_user.id, "scope": grant.scope},
        request_id=request_id,
    )
    return summary


def delete_grants(db: Session, user_id: str) -> int:
    grants = grants_for_candidate(db, user_id)
    for grant in grants:
        db.delete(grant)
    return len(grants)
