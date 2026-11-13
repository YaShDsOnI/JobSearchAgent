"""Application tracker: where the candidate has applied (Phase 6).

Records what the candidate did. No route here sends, uploads, or fetches anything.
"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user, require_csrf
from app.db.models import TrackedApplication, User
from app.db.session import get_db
from app.schemas import (
    TrackerCreate,
    TrackerEntryResponse,
    TrackerListResponse,
    TrackerMarkApplied,
    TrackerSummaryResponse,
    TrackerUpdate,
)
from app.services.tracker import (
    TrackerError,
    create_external,
    delete_entry,
    list_entries,
    mark_applied,
    summarize,
    update_entry,
)

router = APIRouter(tags=["tracker"])


def _raise(exc: TrackerError) -> HTTPException:
    detail: dict[str, str] = {"code": exc.code, "message": str(exc)}
    if exc.existing_id:
        detail["existing_id"] = exc.existing_id
    return HTTPException(status_code=exc.status_code, detail=detail)


def _response(entry: TrackedApplication) -> TrackerEntryResponse:
    return TrackerEntryResponse.model_validate(entry, from_attributes=True)


@router.get("/tracker", response_model=TrackerListResponse)
def list_tracker(
    stage: Literal["applied", "interviewing", "offer", "accepted", "rejected", "withdrawn"]
    | None = Query(default=None),
    q: str | None = Query(default=None, max_length=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    entries = list_entries(db, user.id, stage=stage, query=q)
    # The summary always covers every entry, so filters never hide the totals.
    summary = summarize(list_entries(db, user.id))
    return TrackerListResponse(
        entries=[_response(entry) for entry in entries],
        summary=TrackerSummaryResponse(
            total=summary.total, companies=summary.companies, by_stage=summary.by_stage
        ),
    )


@router.post("/tracker", response_model=TrackerEntryResponse, status_code=status.HTTP_201_CREATED)
def add_entry(
    payload: TrackerCreate,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Track an application made outside JobFinderAgent."""
    require_csrf(request)
    try:
        entry = create_external(
            db,
            user_id=user.id,
            company=payload.company,
            role_title=payload.role_title,
            location=payload.location,
            job_url=payload.job_url,
            applied_on=payload.applied_on,
            stage=payload.stage,
            notes=payload.notes,
            allow_duplicate=payload.allow_duplicate,
            request_id=request.state.request_id,
        )
    except TrackerError as exc:
        db.rollback()
        raise _raise(exc) from exc
    db.commit()
    return _response(entry)


@router.post(
    "/applications/{application_id}/applied",
    response_model=TrackerEntryResponse,
    status_code=status.HTTP_201_CREATED,
)
def mark_application_applied(
    application_id: str,
    payload: TrackerMarkApplied,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The candidate applied on the employer's site themselves; nothing is sent from here."""
    require_csrf(request)
    try:
        entry = mark_applied(
            db,
            user_id=user.id,
            application_id=application_id,
            applied_on=payload.applied_on,
            notes=payload.notes,
            request_id=request.state.request_id,
        )
    except TrackerError as exc:
        db.rollback()
        raise _raise(exc) from exc
    db.commit()
    return _response(entry)


@router.patch("/tracker/{entry_id}", response_model=TrackerEntryResponse)
def change_entry(
    entry_id: str,
    payload: TrackerUpdate,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    require_csrf(request)
    try:
        entry = update_entry(
            db,
            user_id=user.id,
            entry_id=entry_id,
            changes=payload.model_dump(exclude_unset=True),
            request_id=request.state.request_id,
        )
    except TrackerError as exc:
        db.rollback()
        raise _raise(exc) from exc
    db.commit()
    return _response(entry)


@router.delete("/tracker/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_entry(
    entry_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    require_csrf(request)
    try:
        delete_entry(db, user_id=user.id, entry_id=entry_id, request_id=request.state.request_id)
    except TrackerError as exc:
        db.rollback()
        raise _raise(exc) from exc
    db.commit()
