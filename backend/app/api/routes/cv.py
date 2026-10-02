from typing import Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user, require_csrf
from app.core.config import get_settings
from app.db.models import CVClaim, CVVersion, StoredArtifact, User
from app.db.session import get_db
from app.schemas import (
    ArtifactResponse,
    CVClaimResponse,
    CVDecision,
    CVDraftCreate,
    CVVersionResponse,
)
from app.services.audit import record_audit
from app.services.cv import CVInputError, approve_cv, create_cv_version, reject_cv
from app.services.storage import ArtifactStorage

router = APIRouter(tags=["cv"])


def artifact_storage() -> ArtifactStorage:
    return ArtifactStorage(get_settings())


@router.post(
    "/jobs/{job_id}/cv-drafts",
    response_model=CVVersionResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_cv_draft(
    job_id: str,
    payload: CVDraftCreate,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    require_csrf(request)
    try:
        version = create_cv_version(
            db,
            user_id=user.id,
            job_id=job_id,
            match_run_id=payload.match_run_id,
            storage=artifact_storage(),
        )
    except CVInputError as exc:
        detail = str(exc)
        raise HTTPException(
            status_code=404 if detail in {"Match not found", "Job not found"} else 400,
            detail=detail,
        ) from exc
    record_audit(
        db,
        action="cv.draft_created",
        actor="user",
        user_id=user.id,
        object_type="cv_version",
        object_id=version.id,
        metadata={"status": version.status, "generator_version": version.generator_version},
        request_id=request.state.request_id,
    )
    db.commit()
    return _cv_response(db, version)


@router.get("/cv-versions", response_model=list[CVVersionResponse])
def list_cv_versions(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    versions = db.scalars(
        select(CVVersion)
        .where(CVVersion.user_id == user.id)
        .order_by(CVVersion.created_at.desc())
    ).all()
    return [_cv_response(db, version) for version in versions]


@router.get("/cv-versions/{version_id}", response_model=CVVersionResponse)
def get_cv_version(
    version_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    return _cv_response(db, _owned_version(db, version_id, user.id))


@router.get("/cv-versions/{version_id}/artifact/{artifact_format}")
def download_cv_artifact(
    version_id: str,
    artifact_format: Literal["docx", "pdf"],
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    version = _owned_version(db, version_id, user.id)
    artifact_id = version.docx_artifact_id if artifact_format == "docx" else version.pdf_artifact_id
    artifact = db.scalar(
        select(StoredArtifact).where(
            StoredArtifact.id == artifact_id,
            StoredArtifact.owner_user_id == user.id,
        )
    )
    if artifact is None:
        raise HTTPException(status_code=404, detail="CV artifact not found")
    try:
        content = artifact_storage().read_bytes(artifact.object_key)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="CV artifact is unavailable") from exc
    filename = f"jobfinder-cv-{version.id}.{artifact_format}"
    return Response(
        content=content,
        media_type=artifact.content_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/cv-versions/{version_id}/approve", response_model=CVVersionResponse)
def approve_cv_version(
    version_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    require_csrf(request)
    version = _owned_version(db, version_id, user.id)
    try:
        version = approve_cv(db, version=version, user_id=user.id)
    except CVInputError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    record_audit(
        db,
        action="cv.approved",
        actor="user",
        user_id=user.id,
        object_type="cv_version",
        object_id=version.id,
        metadata={"content_hash": version.content_hash},
        request_id=request.state.request_id,
    )
    db.commit()
    return _cv_response(db, version)


@router.post("/cv-versions/{version_id}/reject", response_model=CVVersionResponse)
def reject_cv_version(
    version_id: str,
    payload: CVDecision,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    require_csrf(request)
    if not payload.note.strip():
        raise HTTPException(status_code=422, detail="A rejection note is required")
    version = _owned_version(db, version_id, user.id)
    try:
        version = reject_cv(db, version=version, user_id=user.id, note=payload.note)
    except CVInputError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    record_audit(
        db,
        action="cv.rejected",
        actor="user",
        user_id=user.id,
        object_type="cv_version",
        object_id=version.id,
        metadata={"content_hash": version.content_hash},
        request_id=request.state.request_id,
    )
    db.commit()
    return _cv_response(db, version)


def _owned_version(db: Session, version_id: str, user_id: str) -> CVVersion:
    version = db.scalar(
        select(CVVersion).where(CVVersion.id == version_id, CVVersion.user_id == user_id)
    )
    if version is None:
        raise HTTPException(status_code=404, detail="CV version not found")
    return version


def _cv_response(db: Session, version: CVVersion) -> CVVersionResponse:
    claims = db.scalars(
        select(CVClaim)
        .where(CVClaim.cv_version_id == version.id)
        .order_by(CVClaim.created_at.asc(), CVClaim.id.asc())
    ).all()
    docx = db.get(StoredArtifact, version.docx_artifact_id) if version.docx_artifact_id else None
    pdf = db.get(StoredArtifact, version.pdf_artifact_id) if version.pdf_artifact_id else None
    return CVVersionResponse(
        id=version.id,
        job_id=version.job_id,
        job_snapshot_id=version.job_snapshot_id,
        profile_version_id=version.profile_version_id,
        match_run_id=version.match_run_id,
        generator_version=version.generator_version,
        template_version=version.template_version,
        content_hash=version.content_hash,
        status=cast(Literal["validated", "approved", "rejected", "superseded"], version.status),
        unresolved_items=list(version.source_manifest_json.get("unresolved", [])),
        claims=[CVClaimResponse.model_validate(claim) for claim in claims],
        docx_artifact=ArtifactResponse.model_validate(docx) if docx else None,
        pdf_artifact=ArtifactResponse.model_validate(pdf) if pdf else None,
        approved_by_user_id=version.approved_by_user_id,
        approved_at=version.approved_at,
        rejected_at=version.rejected_at,
        rejection_note=version.rejection_note,
        superseded_at=version.superseded_at,
        superseded_reason=version.superseded_reason,
        superseded_detail=version.superseded_detail_json or {},
        created_at=version.created_at,
    )
