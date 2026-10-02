"""Phase 5C application review package endpoints.

Preparation only. No route here, or anywhere else, sends, uploads, or submits an
application; the destination is a link the candidate opens themselves.
"""

from typing import Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user, require_csrf
from app.db.models import Application, User
from app.db.session import get_db
from app.schemas import (
    ApplicationAnswerResponse,
    ApplicationAnswerUpdate,
    ApplicationApprove,
    ApplicationBinding,
    ApplicationDrift,
    ApplicationEventResponse,
    ApplicationPackageResponse,
    ApplicationPrepare,
    ApplicationResponse,
    ApplicationSkip,
    ApplicationStatus,
    ApplicationSummaryResponse,
    ApprovalResponse,
    ApprovalState,
)
from app.services.applications import (
    ApplicationError,
    approve_package,
    describe,
    owned_application,
    prepare_application,
    revoke_approval,
    set_candidate_answer,
    skip_application,
)

router = APIRouter(tags=["applications"])


def _raise(exc: ApplicationError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code, detail={"code": exc.code, "message": str(exc)}
    )


@router.post("/jobs/{job_id}/applications", response_model=ApplicationResponse)
def prepare(
    job_id: str,
    payload: ApplicationPrepare,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create, reopen, or rebuild the review package for a job. Idempotent for equal inputs."""
    require_csrf(request)
    try:
        application, _ = prepare_application(
            db,
            user_id=user.id,
            job_id=job_id,
            cv_version_id=payload.cv_version_id,
            request_id=request.state.request_id,
        )
    except ApplicationError as exc:
        raise _raise(exc) from exc
    db.commit()
    return _application_response(db, application)


@router.get("/applications", response_model=list[ApplicationSummaryResponse])
def list_applications(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    applications = db.scalars(
        select(Application)
        .where(Application.user_id == user.id)
        .order_by(Application.updated_at.desc(), Application.id.asc())
    ).all()
    summaries = []
    for application in applications:
        view = describe(db, application)
        content = view.package.package_json if view.package else {}
        job = content.get("job", {})
        summaries.append(
            ApplicationSummaryResponse(
                id=application.id,
                job_id=application.job_id,
                title=job.get("title"),
                employer=job.get("employer"),
                status=cast(ApplicationStatus, view.status),
                revision=view.package.revision if view.package else None,
                unresolved_count=len(content.get("unresolved", [])),
                updated_at=application.updated_at,
            )
        )
    return summaries


@router.get("/applications/{application_id}", response_model=ApplicationResponse)
def get_application(
    application_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    try:
        application = owned_application(db, application_id, user.id)
    except ApplicationError as exc:
        raise _raise(exc) from exc
    return _application_response(db, application)


@router.put(
    "/applications/{application_id}/answers/{question_key}", response_model=ApplicationResponse
)
def update_answer(
    application_id: str,
    question_key: str,
    payload: ApplicationAnswerUpdate,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    require_csrf(request)
    try:
        application = set_candidate_answer(
            db,
            user_id=user.id,
            application_id=application_id,
            question_key=question_key,
            value=payload.value,
            request_id=request.state.request_id,
        )
    except ApplicationError as exc:
        raise _raise(exc) from exc
    db.commit()
    return _application_response(db, application)


@router.post(
    "/applications/{application_id}/approve",
    response_model=ApplicationResponse,
    status_code=status.HTTP_200_OK,
)
def approve(
    application_id: str,
    payload: ApplicationApprove,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Record the candidate's approval of the exact package hash they reviewed.

    Nothing consumes this approval in Phase 5C; it is never a trigger to send anything.
    """
    require_csrf(request)
    try:
        approve_package(
            db,
            user_id=user.id,
            application_id=application_id,
            review_hash=payload.review_hash,
            request_id=request.state.request_id,
        )
        application = owned_application(db, application_id, user.id)
    except ApplicationError as exc:
        raise _raise(exc) from exc
    db.commit()
    return _application_response(db, application)


@router.post("/applications/{application_id}/approval/revoke", response_model=ApplicationResponse)
def revoke(
    application_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    require_csrf(request)
    try:
        application = revoke_approval(
            db,
            user_id=user.id,
            application_id=application_id,
            request_id=request.state.request_id,
        )
    except ApplicationError as exc:
        raise _raise(exc) from exc
    db.commit()
    return _application_response(db, application)


@router.post("/applications/{application_id}/skip", response_model=ApplicationResponse)
def skip(
    application_id: str,
    payload: ApplicationSkip,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    require_csrf(request)
    try:
        application = skip_application(
            db,
            user_id=user.id,
            application_id=application_id,
            reason=payload.reason,
            request_id=request.state.request_id,
        )
    except ApplicationError as exc:
        raise _raise(exc) from exc
    db.commit()
    return _application_response(db, application)


def _application_response(db: Session, application: Application) -> ApplicationResponse:
    view = describe(db, application)
    package = view.package
    content = package.package_json if package else {}
    package_response = (
        ApplicationPackageResponse(
            id=package.id,
            revision=package.revision,
            review_hash=package.review_hash,
            job_snapshot_id=package.job_snapshot_id,
            match_run_id=package.match_run_id,
            cv_version_id=package.cv_version_id,
            profile_version_id=package.profile_version_id,
            preference_version_id=package.preference_version_id,
            created_at=package.created_at,
            answers=[
                ApplicationAnswerResponse.model_validate(item)
                for item in content.get("answers", [])
            ],
            unresolved=list(content.get("unresolved", [])),
            warnings=list(content.get("warnings", [])),
            destination=content.get("destination"),
            content=content,
        )
        if package
        else None
    )
    approval = view.approval
    return ApplicationResponse(
        id=application.id,
        job_id=application.job_id,
        status=cast(ApplicationStatus, view.status),
        decision=cast(Literal["open", "skipped"], application.status),
        target_kind="link_out",
        skip_reason=application.skip_reason,
        can_approve=(
            application.status == "open"
            and package is not None
            and not view.drift
            and not content.get("unresolved")
            and view.approval_state != "active"
        ),
        package=package_response,
        binding=ApplicationBinding(
            state="stale" if view.drift else "current",
            drift=[ApplicationDrift(**item) for item in view.drift],
        ),
        approval=(
            ApprovalResponse(
                id=approval.id,
                package_id=approval.package_id,
                reviewed_payload_hash=approval.reviewed_payload_hash,
                state=cast(ApprovalState, view.approval_state),
                approved_at=approval.approved_at,
                expires_at=approval.expires_at,
                revoked_at=approval.revoked_at,
                revoked_reason=approval.revoked_reason,
            )
            if approval
            else None
        ),
        events=[
            ApplicationEventResponse(
                id=event.id,
                event_type=event.event_type,
                actor=event.actor,
                metadata=event.metadata_json or {},
                occurred_at=event.occurred_at,
            )
            for event in view.events
        ],
        created_at=application.created_at,
        updated_at=application.updated_at,
    )
