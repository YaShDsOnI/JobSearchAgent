"""Phase 6 sandbox adapter routes. The only destination is the local sandbox ATS.

Nothing is sent from the API process: these routes record the candidate's decisions and
queue work for the isolated runner (`python -m app.runner`).
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user, require_csrf
from app.db.models import ApplicationSubmission, User
from app.db.session import get_db
from app.schemas import (
    SubmissionConfirm,
    SubmissionField,
    SubmissionResolve,
    SubmissionResponse,
    SubmissionStart,
)
from app.services.applications import ApplicationError, owned_application
from app.services.submission.service import (
    CANCELLABLE,
    cancel_submission,
    confirm_submission,
    latest_submission,
    resolve_submission,
    start_submission,
)

router = APIRouter(tags=["submissions"])


def _raise(exc: ApplicationError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail={"code": exc.code,
                                                              "message": str(exc)})


def _response(submission: ApplicationSubmission) -> SubmissionResponse:
    manifest: dict[str, Any] = submission.fill_manifest_json or {}
    return SubmissionResponse(
        id=submission.id,
        state=submission.state,  # type: ignore[arg-type]
        pause_reason=submission.pause_reason,
        error_code=submission.error_code,
        destination_url=submission.destination_url,
        fields=[
            SubmissionField(
                question_key=item["question_key"],
                label=item.get("label") or item["question_key"],
                display_value=item.get("display_value") or "",
                provenance=item.get("provenance"),
                sent=bool(item.get("sent")),
            )
            for item in manifest.get("fields", [])
        ],
        resume_sha256=(manifest.get("resume") or {}).get("sha256"),
        fill_hash=submission.fill_hash,
        confirmation_id=submission.confirmation_id,
        verification=submission.verification,  # type: ignore[arg-type]
        submit_clicked_at=submission.submit_clicked_at,
        can_confirm=submission.state == "final_review",
        can_cancel=submission.submit_clicked_at is None and submission.state in CANCELLABLE,
        can_resolve=submission.state == "submitted_unverified",
        created_at=submission.created_at,
        updated_at=submission.updated_at,
    )


@router.post("/applications/{application_id}/submission", response_model=SubmissionResponse,
             status_code=status.HTTP_202_ACCEPTED)
def start(
    application_id: str,
    payload: SubmissionStart,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Queue the sandbox runner to fill the form for your review. Nothing is sent yet."""
    require_csrf(request)
    try:
        submission = start_submission(
            db, user_id=user.id, application_id=application_id,
            review_hash=payload.review_hash, request_id=request.state.request_id,
        )
    except ApplicationError as exc:
        db.rollback()
        raise _raise(exc) from exc
    db.commit()
    return _response(submission)


@router.get("/applications/{application_id}/submission", response_model=SubmissionResponse)
def show(
    application_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        application = owned_application(db, application_id, user.id)
    except ApplicationError as exc:
        raise _raise(exc) from exc
    submission = latest_submission(db, application.id)
    if submission is None:
        raise HTTPException(status_code=404, detail={"code": "submission_not_found",
                                                     "message": "No submission exists"})
    return _response(submission)


@router.post("/applications/{application_id}/submission/confirm",
             response_model=SubmissionResponse)
def confirm(
    application_id: str,
    payload: SubmissionConfirm,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Your explicit decision after reviewing the filled form: send it to the sandbox."""
    require_csrf(request)
    try:
        submission = confirm_submission(
            db, user_id=user.id, application_id=application_id, fill_hash=payload.fill_hash,
            request_id=request.state.request_id,
        )
    except ApplicationError as exc:
        db.rollback()
        raise _raise(exc) from exc
    db.commit()
    return _response(submission)


@router.post("/applications/{application_id}/submission/cancel",
             response_model=SubmissionResponse)
def cancel(
    application_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    require_csrf(request)
    try:
        submission = cancel_submission(db, user_id=user.id, application_id=application_id,
                                       request_id=request.state.request_id)
    except ApplicationError as exc:
        db.rollback()
        raise _raise(exc) from exc
    db.commit()
    return _response(submission)


@router.post("/applications/{application_id}/submission/resolve",
             response_model=SubmissionResponse)
def resolve(
    application_id: str,
    payload: SubmissionResolve,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """For an unverified outcome: tell us whether the sandbox received it."""
    require_csrf(request)
    try:
        submission = resolve_submission(db, user_id=user.id, application_id=application_id,
                                        received=payload.received,
                                        request_id=request.state.request_id)
    except ApplicationError as exc:
        db.rollback()
        raise _raise(exc) from exc
    db.commit()
    return _response(submission)
