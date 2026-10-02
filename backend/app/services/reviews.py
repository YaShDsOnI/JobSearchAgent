from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import JobSnapshot, MatchReview, MatchRun
from app.services.matching import MatchInputError, match_job_for_user


class ReviewInputError(ValueError):
    pass


def _reason_codes(review: MatchReview) -> set[str]:
    return {
        str(reason.get("code"))
        for reason in review.reason_json.get("reasons", [])
        if isinstance(reason, dict)
    }


def create_review(db: Session, *, user_id: str, match_run_id: str) -> MatchReview:
    match = db.scalar(
        select(MatchRun).where(MatchRun.id == match_run_id, MatchRun.user_id == user_id)
    )
    if match is None:
        raise ReviewInputError("Match not found")
    if match.eligibility_status != "needs_review":
        raise ReviewInputError("Only needs_review matches can enter human review")
    existing = db.scalar(select(MatchReview).where(MatchReview.match_run_id == match.id))
    if existing:
        return existing
    reasons = match.scores_json.get("eligibility", {}).get("reasons", [])
    review = MatchReview(
        user_id=user_id,
        match_run_id=match.id,
        status="pending",
        reason_json={"reasons": reasons},
    )
    db.add(review)
    db.flush()
    return review


def resolve_review(
    db: Session,
    *,
    review: MatchReview,
    user_id: str,
    decision: str,
    note: str,
) -> MatchReview:
    if review.user_id != user_id:
        raise ReviewInputError("Review not found")
    if review.status != "pending":
        raise ReviewInputError("Review is already resolved")
    codes = _reason_codes(review)
    required_codes = {
        "authorize_for_job_geography": {"authorization_unconfirmed"},
        "accept_remote_geography": {"authorization_geography_unknown"},
        "confirm_sponsorship_compatible": {"sponsorship_unknown"},
        "mark_ineligible": set(),
        "leave_unresolved": set(),
    }
    if decision not in required_codes:
        raise ReviewInputError("Unsupported review decision")
    if required_codes[decision] and not codes.intersection(required_codes[decision]):
        raise ReviewInputError("Review decision does not address this review reason")

    match = db.get(MatchRun, review.match_run_id)
    if match is None or match.user_id != user_id:
        raise ReviewInputError("Match not found")
    snapshot = db.get(JobSnapshot, match.job_snapshot_id)
    if snapshot is None:
        raise ReviewInputError("Job snapshot not found")
    context: dict[str, Any] = {"review_id": review.id}
    if decision == "authorize_for_job_geography":
        context["authorization_confirmed"] = True
    elif decision == "accept_remote_geography":
        context["remote_geography_accepted"] = True
    elif decision == "confirm_sponsorship_compatible":
        context["sponsorship_compatible"] = True
    elif decision == "mark_ineligible":
        context["force_ineligible"] = True

    review.resolution_json = {"decision": decision, "note": note}
    review.status = "resolved"
    review.resolved_by_user_id = user_id
    review.resolved_at = datetime.now(UTC)
    db.flush()
    try:
        rerun = match_job_for_user(
            db,
            user_id=user_id,
            job_id=snapshot.job_id,
            profile_version_id=match.profile_version_id,
            preference_version_id=match.preference_version_id,
            preserve_preference_snapshot=True,
            snapshot_id=match.job_snapshot_id,
            review_context=context,
        )
    except MatchInputError:
        raise
    review.rerun_match_id = rerun.id
    db.flush()
    return review
