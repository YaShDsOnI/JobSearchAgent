from typing import Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user, require_csrf
from app.db.models import (
    JobEvidence,
    JobSnapshot,
    MatchReview,
    MatchRun,
    ProfileFact,
    User,
)
from app.db.session import get_db
from app.schemas import (
    FactResponse,
    JobEvidenceResponse,
    MatchReviewResponse,
    ReviewResolution,
)
from app.services.audit import record_audit
from app.services.reviews import ReviewInputError, create_review, resolve_review

router = APIRouter(tags=["reviews"])


@router.post(
    "/jobs/{job_id}/match/{match_id}/review",
    response_model=MatchReviewResponse,
    status_code=status.HTTP_201_CREATED,
)
def open_review(
    job_id: str,
    match_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    require_csrf(request)
    match = db.scalar(
        select(MatchRun).where(MatchRun.id == match_id, MatchRun.user_id == user.id)
    )
    if match is None:
        raise HTTPException(status_code=404, detail="Match not found")
    snapshot = db.get(JobSnapshot, match.job_snapshot_id)
    if snapshot is None or snapshot.job_id != job_id:
        raise HTTPException(status_code=404, detail="Match not found")
    try:
        review = create_review(db, user_id=user.id, match_run_id=match_id)
    except ReviewInputError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    record_audit(
        db,
        action="match.review_created",
        actor="user",
        user_id=user.id,
        object_type="match_review",
        object_id=review.id,
        request_id=request.state.request_id,
    )
    db.commit()
    return _review_response(db, review)


@router.get("/reviews", response_model=list[MatchReviewResponse])
def list_reviews(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    reviews = db.scalars(
        select(MatchReview)
        .where(MatchReview.user_id == user.id)
        .order_by(MatchReview.created_at.desc())
    ).all()
    return [_review_response(db, review) for review in reviews]


@router.get("/reviews/{review_id}", response_model=MatchReviewResponse)
def get_review(
    review_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    review = _owned_review(db, review_id, user.id)
    return _review_response(db, review)


@router.post("/reviews/{review_id}/resolve", response_model=MatchReviewResponse)
def resolve_review_route(
    review_id: str,
    payload: ReviewResolution,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    require_csrf(request)
    review = _owned_review(db, review_id, user.id)
    try:
        review = resolve_review(
            db,
            review=review,
            user_id=user.id,
            decision=payload.decision,
            note=payload.note,
        )
    except ReviewInputError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    record_audit(
        db,
        action="match.review_resolved",
        actor="user",
        user_id=user.id,
        object_type="match_review",
        object_id=review.id,
        metadata={"decision": payload.decision, "rerun_match_id": review.rerun_match_id},
        request_id=request.state.request_id,
    )
    db.commit()
    return _review_response(db, review)


def _owned_review(db: Session, review_id: str, user_id: str) -> MatchReview:
    review = db.scalar(
        select(MatchReview).where(MatchReview.id == review_id, MatchReview.user_id == user_id)
    )
    if review is None:
        raise HTTPException(status_code=404, detail="Review not found")
    return review


def _review_response(db: Session, review: MatchReview) -> MatchReviewResponse:
    match = db.get(MatchRun, review.match_run_id)
    if match is None:
        raise HTTPException(status_code=404, detail="Match not found")
    snapshot = db.get(JobSnapshot, match.job_snapshot_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Job snapshot not found")
    reasons = review.reason_json.get("reasons", [])
    candidate_ids = {
        fact_id
        for reason in reasons
        for fact_id in reason.get("candidate_fact_ids", [])
    }
    evidence_ids = {
        evidence_id
        for reason in reasons
        for evidence_id in reason.get("job_evidence_ids", [])
    }
    candidate_facts = db.scalars(
        select(ProfileFact).where(ProfileFact.id.in_(candidate_ids))
    ).all() if candidate_ids else []
    job_evidence = db.scalars(
        select(JobEvidence).where(JobEvidence.id.in_(evidence_ids))
    ).all() if evidence_ids else []
    return MatchReviewResponse(
        id=review.id,
        match_run_id=review.match_run_id,
        job_id=snapshot.job_id,
        status=cast(Literal["pending", "resolved"], review.status),
        reasons=reasons,
        job_evidence=[
            JobEvidenceResponse.model_validate(item, from_attributes=True)
            for item in job_evidence
        ],
        candidate_facts=[
            FactResponse.model_validate(item, from_attributes=True) for item in candidate_facts
        ],
        resolution=(
            ReviewResolution.model_validate(review.resolution_json)
            if review.resolution_json
            else None
        ),
        resolved_by_user_id=review.resolved_by_user_id,
        resolved_at=review.resolved_at,
        rerun_match_id=review.rerun_match_id,
        created_at=review.created_at,
    )
