"""The candidate's agent: status, next actions, on/off, refresh, and ranked recommendations.

Everything here reads or writes the candidate's own data only. Turning the agent on grants
the `automatic_matching` consent, which lets the worker re-match jobs when the catalog,
profile, or preferences change. It never drafts, approves, or sends anything.
"""

from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user, require_csrf
from app.db.models import CandidateProfile, Consent, SourceConnector, User
from app.db.session import get_db
from app.schemas import StrictModel
from app.services.agent import (
    AUTO_MATCH_PURPOSE,
    RANK_FIT_WEIGHT,
    RANK_PREFERENCE_WEIGHT,
    Recommendation,
    agent_status,
    auto_matching_enabled,
    enqueue_match_refresh,
    recommendations,
    refresh_matches,
)
from app.services.audit import record_audit
from app.services.matching import POLICY_VERSION

router = APIRouter(tags=["agent"])

AGENT_CONSENT_POLICY_VERSION = "agent-v1"
INLINE_REFRESH_LIMIT = 200


class NextAction(StrictModel):
    code: str
    message: str
    count: int | None
    target: str


class AgentStatusResponse(StrictModel):
    auto_matching: bool
    profile_ready: bool
    queued_refreshes: int
    last_refresh_at: datetime | None
    last_ingestion_at: datetime | None
    counts: dict[str, int]
    next_actions: list[NextAction]
    policy_version: str
    rank_formula: str


class RefreshResponse(StrictModel):
    matched: int
    already_current: int
    carried_reviews: int
    errors: int
    truncated: bool


class RecommendationJob(StrictModel):
    id: str
    title: str
    employer: str
    location_text: str | None
    work_mode: str
    canonical_url: str
    first_seen_at: datetime
    source: str


class RecommendationMatch(StrictModel):
    id: str
    eligibility_status: str
    eligibility_reasons: list[dict[str, Any]]
    fit_score: float
    fit_details: dict[str, Any]
    preference_score: float
    preference_reasons: list[dict[str, Any]]
    location_category: str | None
    policy_version: str
    created_at: datetime


class RecommendationResponse(StrictModel):
    bucket: Literal["ready", "review", "unmatched", "ineligible"]
    rank: float | None = Field(description="Only for jobs not blocked by eligibility")
    current: bool
    job: RecommendationJob
    match: RecommendationMatch | None
    application_id: str | None
    applied: bool
    cv_status: str | None


def _status_response(db: Session, user: User) -> AgentStatusResponse:
    status = agent_status(db, user.id)
    return AgentStatusResponse(
        auto_matching=status.auto_matching,
        profile_ready=status.profile_ready,
        queued_refreshes=status.queued_refreshes,
        last_refresh_at=status.last_refresh_at,
        last_ingestion_at=status.last_ingestion_at,
        counts=status.counts,
        next_actions=[NextAction(**item) for item in status.next_actions],
        policy_version=POLICY_VERSION,
        rank_formula=(
            f"rank = {RANK_FIT_WEIGHT:g} x fit + {RANK_PREFERENCE_WEIGHT:g} x preference, "
            "for jobs not blocked by eligibility"
        ),
    )


@router.get("/agent/status", response_model=AgentStatusResponse)
def get_status(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return _status_response(db, user)


@router.post("/agent/enable", response_model=AgentStatusResponse)
def enable_agent(
    request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    require_csrf(request)
    if not auto_matching_enabled(db, user.id):
        consent = Consent(
            user_id=user.id,
            purpose=AUTO_MATCH_PURPOSE,
            scope={"actions": ["analyze_jobs", "match_jobs"], "sends_data": False},
            policy_version=AGENT_CONSENT_POLICY_VERSION,
        )
        db.add(consent)
        db.flush()
        record_audit(
            db,
            action="consent.granted",
            actor="user",
            user_id=user.id,
            object_type="consent",
            object_id=consent.id,
            metadata={"purpose": AUTO_MATCH_PURPOSE},
            request_id=request.state.request_id,
        )
        db.commit()
        enqueue_match_refresh(db, user.id, reason="agent_enabled")
        db.commit()
    return _status_response(db, user)


@router.post("/agent/disable", response_model=AgentStatusResponse)
def disable_agent(
    request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    require_csrf(request)
    for consent in db.scalars(
        select(Consent).where(
            Consent.user_id == user.id,
            Consent.purpose == AUTO_MATCH_PURPOSE,
            Consent.withdrawn_at.is_(None),
        )
    ):
        consent.withdrawn_at = datetime.now(UTC)
        record_audit(
            db,
            action="consent.withdrawn",
            actor="user",
            user_id=user.id,
            object_type="consent",
            object_id=consent.id,
            metadata={"purpose": AUTO_MATCH_PURPOSE},
            request_id=request.state.request_id,
        )
    db.commit()
    return _status_response(db, user)


@router.post("/agent/refresh", response_model=RefreshResponse)
def refresh_now(
    request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    """Match now, in the request, up to a bounded number of jobs. Works whether or not the
    agent is on: the candidate asked for it explicitly."""
    require_csrf(request)
    profile = db.get(CandidateProfile, user.id)
    if profile is None or profile.current_version_id is None:
        raise HTTPException(status_code=400, detail="Add a profile version before matching")
    result = refresh_matches(db, user.id, limit=INLINE_REFRESH_LIMIT, actor="user")
    return RefreshResponse(**result.summary())


def _recommendation_response(
    item: Recommendation, sources: dict[str, str]
) -> RecommendationResponse:
    match = item.match
    job = item.job
    match_body = None
    if match is not None:
        scores = match.scores_json or {}
        preference = scores.get("preference", {})
        match_body = RecommendationMatch(
            id=match.id,
            eligibility_status=match.eligibility_status,
            eligibility_reasons=list(scores.get("eligibility", {}).get("reasons", [])),
            fit_score=match.fit_score,
            fit_details=dict(scores.get("fit", {}).get("details", {})),
            preference_score=match.preference_score,
            preference_reasons=list(preference.get("reasons", [])),
            location_category=(preference.get("details") or {}).get("location_category"),
            policy_version=match.policy_version,
            created_at=match.created_at,
        )
    return RecommendationResponse(
        bucket=item.bucket,  # type: ignore[arg-type]
        rank=item.rank,
        current=item.current,
        job=RecommendationJob(
            id=job.id,
            title=job.title,
            employer=job.employer,
            location_text=job.location_text,
            work_mode=job.work_mode,
            canonical_url=job.canonical_url,
            first_seen_at=job.first_seen_at,
            source=sources.get(job.connector_id, "unknown"),
        ),
        match=match_body,
        application_id=item.application_id,
        applied=item.applied,
        cv_status=item.cv_status,
    )


@router.get("/recommendations", response_model=list[RecommendationResponse])
def list_recommendations(
    bucket: Literal["ready", "review", "unmatched", "ineligible", "all"] = Query(default="all"),
    include_ineligible: bool = Query(default=False),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    items = recommendations(db, user.id)
    if bucket != "all":
        items = [item for item in items if item.bucket == bucket]
    elif not include_ineligible:
        items = [item for item in items if item.bucket != "ineligible"]
    sources = {
        connector.id: connector.name
        for connector in db.scalars(
            select(SourceConnector).where(
                SourceConnector.id.in_({item.job.connector_id for item in items} or {""})
            )
        )
    }
    return [_recommendation_response(item, sources) for item in items]
