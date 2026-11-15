"""The job-search agent loop: keep every candidate's matches current and say what to do next.

The agent runs only for a candidate who has granted the `automatic_matching` consent and has
a current profile version. Each refresh is deterministic and local. It matches active,
visible, non-duplicate jobs whose latest snapshot has no match for the candidate's current
profile version, preference version, matching policy, and extractor. Nothing is drafted,
approved, or sent. CV drafting stays a candidate action until a labeled evaluation meets its
threshold (`docs/matching-engine.md`, "Acceptance criteria"), and every application still
needs the candidate's own approval.

Triggers, each a durable task (`agent_match_refresh`) with an idempotency key derived from
the inputs, so a burst of triggers runs one refresh:

- an ingestion run that created, updated, or reopened postings;
- a new profile version or preference version;
- granting the consent.

A resolved eligibility review is carried forward only to a new match on the same job
snapshot and the same profile version, because the candidate's decision concerned exactly
those inputs. Any change to either needs a new review.

Design: `docs/architecture.md`, "Agent loop".
"""

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.db.models import (
    Application,
    AuditEvent,
    CandidateProfile,
    Consent,
    CVVersion,
    JobPosting,
    JobSnapshot,
    MatchReview,
    MatchRun,
    PreferenceVersion,
    ProfileFact,
    SourceConnector,
    SourceRun,
    Task,
)
from app.services.applications import is_applied
from app.services.audit import record_audit
from app.services.catalog import visible_to_user
from app.services.matching import POLICY_VERSION, MatchInputError, match_job_for_user
from app.services.requirements import EXTRACTOR_VERSION
from app.services.tasks import (
    claim_next_task,
    complete_task,
    enqueue_task,
    reschedule_task,
)

logger = logging.getLogger(__name__)

AUTO_MATCH_PURPOSE = "automatic_matching"
AGENT_TASK_KIND = "agent_match_refresh"
AGENT_LEASE_SECONDS = 300
MAX_JOBS_PER_REFRESH = 500
COMMIT_EVERY = 25
# The rank among eligible jobs combines the two independent scores with fixed, visible
# weights. Eligibility is never part of it: ineligible jobs are not ranked at all.
RANK_FIT_WEIGHT = 0.6
RANK_PREFERENCE_WEIGHT = 0.4


# --- consent and triggers ----------------------------------------------------------------


def auto_matching_enabled(db: Session, user_id: str) -> bool:
    return (
        db.scalar(
            select(Consent.id)
            .where(
                Consent.user_id == user_id,
                Consent.purpose == AUTO_MATCH_PURPOSE,
                Consent.withdrawn_at.is_(None),
            )
            .limit(1)
        )
        is not None
    )


def _current_preference_id(db: Session, user_id: str) -> str | None:
    return db.scalar(
        select(PreferenceVersion.id)
        .where(PreferenceVersion.user_id == user_id, PreferenceVersion.status == "active")
        .order_by(PreferenceVersion.created_at.desc())
        .limit(1)
    )


def _catalog_marker(db: Session, user_id: str) -> str:
    count, newest = db.execute(
        select(func.count(JobSnapshot.id), func.max(JobSnapshot.retrieved_at))
        .join(JobPosting, JobPosting.id == JobSnapshot.job_id)
        .where(visible_to_user(user_id))
    ).one()
    return f"{count}:{newest.isoformat() if newest else '-'}"


def enqueue_match_refresh(db: Session, user_id: str, *, reason: str) -> Task | None:
    """Queue one refresh for the candidate's current inputs. Returns None when the agent is
    off for them or there is nothing to match against yet."""
    if not auto_matching_enabled(db, user_id):
        return None
    profile = db.get(CandidateProfile, user_id)
    if profile is None or profile.current_version_id is None:
        return None
    inputs = "|".join(
        [
            profile.current_version_id,
            _current_preference_id(db, user_id) or "-",
            POLICY_VERSION,
            EXTRACTOR_VERSION,
            _catalog_marker(db, user_id),
        ]
    )
    digest = hashlib.sha256(inputs.encode("utf-8")).hexdigest()[:32]
    return enqueue_task(
        db,
        kind=AGENT_TASK_KIND,
        idempotency_key=f"{AGENT_TASK_KIND}:{user_id}:{digest}",
        payload={"user_id": user_id, "reason": reason},
    )


def enqueue_for_catalog_change(db: Session, connector: SourceConnector) -> int:
    """After an ingestion run changed postings: one refresh per candidate who can see them."""
    users = select(Consent.user_id).where(
        Consent.purpose == AUTO_MATCH_PURPOSE, Consent.withdrawn_at.is_(None)
    )
    if connector.owner_user_id is not None:
        users = users.where(Consent.user_id == connector.owner_user_id)
    queued = 0
    for user_id in sorted(set(db.scalars(users))):
        if enqueue_match_refresh(db, user_id, reason="catalog_changed") is not None:
            queued += 1
    return queued


# --- refresh -----------------------------------------------------------------------------


@dataclass
class RefreshResult:
    matched: int = 0
    already_current: int = 0
    carried_reviews: int = 0
    errors: int = 0
    truncated: bool = False

    def summary(self) -> dict[str, Any]:
        return {
            "matched": self.matched,
            "already_current": self.already_current,
            "carried_reviews": self.carried_reviews,
            "errors": self.errors,
            "truncated": self.truncated,
        }


def _latest_snapshots(db: Session, user_id: str) -> list[tuple[JobPosting, JobSnapshot]]:
    newest = (
        select(JobSnapshot.job_id, func.max(JobSnapshot.version).label("version"))
        .group_by(JobSnapshot.job_id)
        .subquery()
    )
    rows = db.execute(
        select(JobPosting, JobSnapshot)
        .join(newest, newest.c.job_id == JobPosting.id)
        .join(
            JobSnapshot,
            and_(JobSnapshot.job_id == JobPosting.id, JobSnapshot.version == newest.c.version),
        )
        .where(
            visible_to_user(user_id),
            JobPosting.status == "active",
            JobPosting.duplicate_of_job_id.is_(None),
        )
        .order_by(JobPosting.last_seen_at.desc(), JobPosting.id)
    ).all()
    return [(job, snapshot) for job, snapshot in rows]


def _has_current_match(
    db: Session,
    user_id: str,
    snapshot_id: str,
    profile_version_id: str,
    preference_version_id: str | None,
) -> bool:
    preference_match = (
        MatchRun.preference_version_id.is_(None)
        if preference_version_id is None
        else MatchRun.preference_version_id == preference_version_id
    )
    return (
        db.scalar(
            select(MatchRun.id)
            .where(
                MatchRun.user_id == user_id,
                MatchRun.job_snapshot_id == snapshot_id,
                MatchRun.profile_version_id == profile_version_id,
                preference_match,
                MatchRun.policy_version == POLICY_VERSION,
                MatchRun.extractor_version == EXTRACTOR_VERSION,
            )
            .limit(1)
        )
        is not None
    )


def _carried_review_context(
    db: Session, user_id: str, snapshot_id: str, profile_version_id: str
) -> dict[str, Any] | None:
    rerun = db.scalar(
        select(MatchRun)
        .join(MatchReview, MatchReview.rerun_match_id == MatchRun.id)
        .where(
            MatchReview.user_id == user_id,
            MatchReview.status == "resolved",
            MatchRun.job_snapshot_id == snapshot_id,
            MatchRun.profile_version_id == profile_version_id,
        )
        .order_by(MatchReview.resolved_at.desc())
        .limit(1)
    )
    if rerun is None:
        return None
    context = dict(rerun.scores_json.get("review") or {})
    return context or None


def refresh_matches(
    db: Session, user_id: str, *, limit: int = MAX_JOBS_PER_REFRESH, actor: str = "agent"
) -> RefreshResult:
    """Bring the candidate's matches up to date. Commits in batches; idempotent."""
    result = RefreshResult()
    profile = db.get(CandidateProfile, user_id)
    if profile is None or profile.current_version_id is None:
        return result
    profile_version_id = profile.current_version_id
    preference_version_id = _current_preference_id(db, user_id)
    pending = 0
    for job, snapshot in _latest_snapshots(db, user_id):
        if _has_current_match(db, user_id, snapshot.id, profile_version_id, preference_version_id):
            result.already_current += 1
            continue
        if result.matched + result.errors >= limit:
            result.truncated = True
            break
        context = _carried_review_context(db, user_id, snapshot.id, profile_version_id)
        try:
            with db.begin_nested():
                match_job_for_user(
                    db,
                    user_id=user_id,
                    job_id=job.id,
                    snapshot_id=snapshot.id,
                    review_context=context,
                )
        except MatchInputError:
            result.errors += 1
            continue
        result.matched += 1
        result.carried_reviews += 1 if context else 0
        pending += 1
        if pending >= COMMIT_EVERY:
            db.commit()
            pending = 0
    record_audit(
        db,
        action="agent.matches_refreshed",
        actor=actor,
        user_id=user_id,
        object_type="profile_version",
        object_id=profile_version_id,
        metadata={**result.summary(), "policy_version": POLICY_VERSION},
    )
    db.commit()
    return result


def run_next_agent_task(db: Session, *, worker_id: str) -> Task | None:
    task = claim_next_task(
        db, worker_id=worker_id, lease_seconds=AGENT_LEASE_SECONDS, kinds=[AGENT_TASK_KIND]
    )
    if task is None:
        db.rollback()
        return None
    db.commit()
    user_id = str((task.payload_json or {}).get("user_id", ""))
    try:
        # Consent may have been withdrawn after the task was queued.
        if user_id and auto_matching_enabled(db, user_id):
            refresh_matches(db, user_id, actor=f"agent:{worker_id}"[:64])
        complete_task(db, task, worker_id=worker_id)
        db.commit()
    except Exception as exc:
        logger.error("Agent refresh failed", extra={"error_type": type(exc).__name__})
        db.rollback()
        reschedule_task(db, task, worker_id=worker_id, delay_seconds=60, max_attempts=3)
        db.commit()
    return task


# --- recommendations ---------------------------------------------------------------------


def rank_score(fit_score: float, preference_score: float) -> float:
    return round(RANK_FIT_WEIGHT * fit_score + RANK_PREFERENCE_WEIGHT * preference_score, 2)


@dataclass
class Recommendation:
    job: JobPosting
    match: MatchRun | None
    current: bool
    application_id: str | None
    applied: bool
    cv_status: str | None
    bucket: str  # ready | review | ineligible | unmatched
    rank: float | None


def _latest_matches(db: Session, user_id: str, job_ids: list[str]) -> dict[str, MatchRun]:
    if not job_ids:
        return {}
    rows = db.execute(
        select(JobSnapshot.job_id, MatchRun)
        .join(JobSnapshot, JobSnapshot.id == MatchRun.job_snapshot_id)
        .where(MatchRun.user_id == user_id, JobSnapshot.job_id.in_(job_ids))
        .order_by(MatchRun.created_at.desc(), MatchRun.id.desc())
    ).all()
    latest: dict[str, MatchRun] = {}
    for job_id, match in rows:
        latest.setdefault(job_id, match)
    return latest


def recommendations(db: Session, user_id: str) -> list[Recommendation]:
    """Every active job the candidate can see, with its latest match, bucketed and ranked:
    eligible jobs by rank, then jobs needing review, then unmatched, then ineligible."""
    pairs = _latest_snapshots(db, user_id)
    job_ids = [job.id for job, _ in pairs]
    matches = _latest_matches(db, user_id, job_ids)
    profile = db.get(CandidateProfile, user_id)
    profile_version_id = profile.current_version_id if profile else None
    preference_version_id = _current_preference_id(db, user_id)
    applications = {
        application.job_id: application
        for application in db.scalars(
            select(Application).where(
                Application.user_id == user_id, Application.job_id.in_(job_ids or [""])
            )
        )
    }
    cv_status: dict[str, str] = {}
    for version in db.scalars(
        select(CVVersion)
        .where(CVVersion.user_id == user_id, CVVersion.job_id.in_(job_ids or [""]))
        .order_by(CVVersion.created_at)
    ):
        cv_status[version.job_id] = version.status
    items: list[Recommendation] = []
    for job, snapshot in pairs:
        match = matches.get(job.id)
        current = bool(
            match
            and match.job_snapshot_id == snapshot.id
            and match.profile_version_id == profile_version_id
            and match.preference_version_id == preference_version_id
            and match.policy_version == POLICY_VERSION
        )
        if match is None:
            bucket, rank = "unmatched", None
        elif match.eligibility_status == "eligible":
            bucket, rank = "ready", rank_score(match.fit_score, match.preference_score)
        elif match.eligibility_status in {"needs_review", "unknown"}:
            bucket, rank = "review", rank_score(match.fit_score, match.preference_score)
        else:
            bucket, rank = "ineligible", None
        application = applications.get(job.id)
        items.append(
            Recommendation(
                job=job,
                match=match,
                current=current,
                application_id=application.id if application else None,
                applied=bool(application and is_applied(db, application)),
                cv_status=cv_status.get(job.id),
                bucket=bucket,
                rank=rank,
            )
        )
    order = {"ready": 0, "review": 1, "unmatched": 2, "ineligible": 3}
    items.sort(key=lambda item: (order[item.bucket], -(item.rank or 0.0), item.job.title))
    return items


# --- status and next actions -------------------------------------------------------------


@dataclass
class AgentStatus:
    auto_matching: bool
    profile_ready: bool
    queued_refreshes: int
    last_refresh_at: datetime | None
    last_ingestion_at: datetime | None
    counts: dict[str, int]
    next_actions: list[dict[str, Any]] = field(default_factory=list)


def _action(code: str, message: str, *, count: int | None = None, target: str) -> dict[str, Any]:
    return {"code": code, "message": message, "count": count, "target": target}


def agent_status(db: Session, user_id: str) -> AgentStatus:
    profile = db.get(CandidateProfile, user_id)
    profile_version_id = profile.current_version_id if profile else None
    facts = (
        list(
            db.scalars(
                select(ProfileFact).where(
                    ProfileFact.profile_version_id == profile_version_id,
                    ProfileFact.confirmation_status == "confirmed",
                )
            )
        )
        if profile_version_id
        else []
    )
    fact_types = {fact.fact_type for fact in facts}
    preference = db.scalar(
        select(PreferenceVersion)
        .where(PreferenceVersion.user_id == user_id, PreferenceVersion.status == "active")
        .order_by(PreferenceVersion.created_at.desc())
        .limit(1)
    )
    items = recommendations(db, user_id)
    counts = {bucket: 0 for bucket in ("ready", "review", "unmatched", "ineligible")}
    for item in items:
        counts[item.bucket] += 1
    counts["stale"] = sum(1 for item in items if item.match is not None and not item.current)
    counts["applied"] = sum(1 for item in items if item.applied)
    pending_reviews = db.scalar(
        select(func.count(MatchReview.id)).where(
            MatchReview.user_id == user_id, MatchReview.status == "pending"
        )
    ) or 0
    cvs_to_review = db.scalar(
        select(func.count(CVVersion.id)).where(
            CVVersion.user_id == user_id, CVVersion.status == "validated"
        )
    ) or 0
    queued = db.scalar(
        select(func.count(Task.id)).where(
            Task.kind == AGENT_TASK_KIND,
            Task.state.in_(["queued", "running"]),
            Task.payload_json["user_id"].as_string() == user_id,
        )
    ) or 0
    last_refresh = db.scalar(
        select(func.max(AuditEvent.occurred_at)).where(
            AuditEvent.action == "agent.matches_refreshed", AuditEvent.user_id == user_id
        )
    )
    last_ingestion = db.scalar(
        select(func.max(SourceRun.finished_at))
        .join(SourceConnector, SourceConnector.id == SourceRun.connector_id)
        .where(
            or_(
                SourceConnector.owner_user_id.is_(None),
                SourceConnector.owner_user_id == user_id,
            ),
            SourceRun.status.in_(["succeeded", "partial"]),
        )
    )
    enabled = auto_matching_enabled(db, user_id)
    actions: list[dict[str, Any]] = []
    if profile_version_id is None:
        actions.append(_action("add_profile", "Add your profile facts so jobs can be matched.",
                               target="profile"))
    else:
        if "work_authorization" not in fact_types:
            actions.append(_action(
                "confirm_authorization",
                "Confirm your work authorization. Until then every job needs a review, "
                "because authorization is never assumed.",
                target="profile",
            ))
        if "skill" not in fact_types:
            actions.append(_action("add_skills", "Add and confirm your skills to score fit.",
                                   target="profile"))
    if preference is None or not (preference.rules_json or {}).get("location_policy"):
        actions.append(_action(
            "set_location_ladder",
            "Set your location ladder (remote, hybrid, on-site, and preferred cities).",
            target="preferences",
        ))
    if not enabled:
        actions.append(_action(
            "enable_agent",
            "Turn on automatic matching so new jobs are matched as soon as they arrive.",
            target="overview",
        ))
    if counts["stale"] or counts["unmatched"]:
        actions.append(_action(
            "refresh_matches",
            "Some jobs have no current match. Refresh to score them with your latest profile.",
            count=counts["stale"] + counts["unmatched"],
            target="overview",
        ))
    if pending_reviews:
        actions.append(_action("resolve_reviews", "Jobs are waiting for your eligibility review.",
                               count=int(pending_reviews), target="jobs"))
    if cvs_to_review:
        actions.append(_action("review_cvs", "Tailored CV drafts are waiting for your approval.",
                               count=int(cvs_to_review), target="cvs"))
    if counts["ready"]:
        actions.append(_action("review_ready", "Eligible jobs are ranked and ready for you.",
                               count=counts["ready"], target="jobs"))
    return AgentStatus(
        auto_matching=enabled,
        profile_ready=profile_version_id is not None,
        queued_refreshes=int(queued),
        last_refresh_at=last_refresh,
        last_ingestion_at=last_ingestion,
        counts=counts,
        next_actions=actions,
    )


__all__ = [
    "AGENT_TASK_KIND",
    "AUTO_MATCH_PURPOSE",
    "AgentStatus",
    "Recommendation",
    "agent_status",
    "auto_matching_enabled",
    "enqueue_for_catalog_change",
    "enqueue_match_refresh",
    "rank_score",
    "recommendations",
    "refresh_matches",
    "run_next_agent_task",
]
