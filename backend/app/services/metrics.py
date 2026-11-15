"""Pilot metrics (Phase 7): aggregate, non-identifying measures of quality, effort, stability,
and harm across the deployment.

Every number is a count over all candidates. A count from 1 to `SUPPRESS_BELOW - 1` is
reported as "<5", so that in a small pilot no cell points at one person. No identifier,
email, fact value, job text, or document ever appears. The categories follow the Phase 7
brief in `docs/roadmap.md`:

- match quality: match runs by eligibility, reviews, mean fit and preference of eligible
  matches;
- factual edits: CV versions by status (a rejection is a candidate correction) and claims
  by validation result;
- user effort: profile versions per candidate, suggestions requested, packages prepared and
  approved, tracker entries;
- source stability: sources by health, ingestion runs and rejected records in 7 days;
- adapter failure: sandbox submissions by state;
- harmful events: unverified submissions, blocked sources, deletion replays, support views.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    Application,
    ApplicationSubmission,
    ApprovalRecord,
    AuditEvent,
    Consent,
    CVClaim,
    CVVersion,
    MatchReview,
    MatchRun,
    ProfileVersion,
    SourceConnector,
    SourceRun,
    TrackedApplication,
    User,
)

SUPPRESS_BELOW = 5
METRICS_VERSION = "pilot-metrics-v1"


def suppress(value: int) -> int | str:
    return f"<{SUPPRESS_BELOW}" if 0 < value < SUPPRESS_BELOW else value


def _grouped(db: Session, column: Any, *where: Any) -> dict[str, int | str]:
    query = select(column, func.count()).group_by(column)
    if where:
        query = query.where(*where)
    rows: Any = db.execute(query).all()
    return {str(row[0]): suppress(int(row[1])) for row in rows}


def _count(db: Session, query: Any) -> int:
    return int(db.scalar(query) or 0)


def _audit_count(db: Session, action: str, since: datetime | None = None) -> int | str:
    query = select(func.count(AuditEvent.id)).where(AuditEvent.action == action)
    if since is not None:
        query = query.where(AuditEvent.occurred_at >= since)
    return suppress(_count(db, query))


def _rejected_records(db: Session, since: datetime) -> int:
    total = 0
    rows: list[dict[str, Any]] = list(
        db.scalars(select(SourceRun.stats_json).where(SourceRun.started_at >= since))
    )
    for stats in rows:
        value = (stats or {}).get("rejected", 0)
        total += value if isinstance(value, int) else 0
    return total


def _model_tokens(db: Session, since: datetime) -> int:
    total = 0
    rows: list[dict[str, Any]] = list(
        db.scalars(
            select(AuditEvent.metadata_json).where(
                AuditEvent.action == "model.extraction_called", AuditEvent.occurred_at >= since
            )
        )
    )
    for metadata in rows:
        for key in ("input_tokens", "output_tokens"):
            value = (metadata or {}).get(key)
            total += value if isinstance(value, int) else 0
    return total


def pilot_metrics(db: Session, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    week = now - timedelta(days=7)
    candidates = _count(
        db,
        select(func.count(User.id)).where(User.role == "candidate", User.status == "active"),
    )
    profile_versions = _count(db, select(func.count(ProfileVersion.id)))
    eligible = select(MatchRun).where(MatchRun.eligibility_status == "eligible").subquery()
    means: Any = db.execute(
        select(func.avg(eligible.c.fit_score), func.avg(eligible.c.preference_score))
    ).one()
    mean_fit, mean_preference = means[0], means[1]
    return {
        "metrics_version": METRICS_VERSION,
        "generated_at": now.isoformat(),
        "suppression": f"counts from 1 to {SUPPRESS_BELOW - 1} are shown as <{SUPPRESS_BELOW}",
        "candidates": {
            "active": suppress(candidates),
            "with_agent_on": suppress(
                _count(
                    db,
                    select(func.count(func.distinct(Consent.user_id))).where(
                        Consent.purpose == "automatic_matching", Consent.withdrawn_at.is_(None)
                    ),
                )
            ),
        },
        "match_quality": {
            "match_runs_by_eligibility": _grouped(db, MatchRun.eligibility_status),
            "reviews_by_status": _grouped(db, MatchReview.status),
            "mean_fit_of_eligible": round(float(mean_fit), 1) if mean_fit is not None else None,
            "mean_preference_of_eligible": round(float(mean_preference), 1)
            if mean_preference is not None
            else None,
        },
        "factual_edits": {
            "cv_versions_by_status": _grouped(db, CVVersion.status),
            "cv_claims_by_validation": _grouped(db, CVClaim.validation_status),
        },
        "user_effort": {
            "profile_versions_per_candidate": round(profile_versions / candidates, 2)
            if candidates
            else None,
            "fact_suggestions_requested": _audit_count(db, "profile.suggestions_generated"),
            "packages_prepared": suppress(_count(db, select(func.count(Application.id)))),
            "package_approvals": suppress(_count(db, select(func.count(ApprovalRecord.id)))),
            "tracker_entries_by_stage": _grouped(db, TrackedApplication.stage),
        },
        "source_stability": {
            "sources_by_health": _grouped(db, SourceConnector.health_status),
            "runs_7d_by_status": _grouped(db, SourceRun.status, SourceRun.started_at >= week),
            "rejected_records_7d": suppress(_rejected_records(db, week)),
        },
        "adapter_failure": {
            "submissions_by_state": _grouped(db, ApplicationSubmission.state),
        },
        "model_usage": {
            "extraction_calls_7d": _audit_count(db, "model.extraction_called", week),
            "tokens_7d": _model_tokens(db, week),
        },
        "harmful_events": {
            "submissions_unverified_or_not_received": suppress(
                _count(
                    db,
                    select(func.count(ApplicationSubmission.id)).where(
                        ApplicationSubmission.state.in_(["submitted_unverified", "not_received"])
                    ),
                )
            ),
            "sources_blocked": suppress(
                _count(
                    db,
                    select(func.count(SourceConnector.id)).where(
                        SourceConnector.health_status.in_(["blocked", "suspended"])
                    ),
                )
            ),
            "deletion_replays": _audit_count(db, "profile.deletion_replayed"),
            "support_case_views_7d": _audit_count(db, "support.case_viewed", week),
        },
    }
