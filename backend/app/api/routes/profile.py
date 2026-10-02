from datetime import UTC, datetime

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user, require_csrf
from app.core.config import get_settings
from app.db.models import (
    CandidateProfile,
    Consent,
    CVClaim,
    CVVersion,
    DataRequest,
    MatchReview,
    MatchRun,
    PreferenceVersion,
    ProfileFact,
    ProfileVersion,
    StoredArtifact,
    User,
)
from app.db.session import get_db
from app.schemas import (
    ArtifactResponse,
    ConsentCreate,
    ConsentResponse,
    DataRequestResponse,
    Preferences,
    PreferencesResponse,
    ProfileVersionCreate,
    ProfileVersionResponse,
)
from app.services.audit import record_audit
from app.services.cv_invalidation import supersede_cv_versions
from app.services.storage import ArtifactStorage

router = APIRouter(prefix="/profile", tags=["profile"])


def artifact_storage() -> ArtifactStorage:
    return ArtifactStorage(get_settings())


def get_current_profile(db: Session, user_id: str) -> ProfileVersion | None:
    profile = db.get(CandidateProfile, user_id)
    return (
        db.get(ProfileVersion, profile.current_version_id)
        if profile and profile.current_version_id
        else None
    )


@router.get("", response_model=ProfileVersionResponse | None)
def get_profile(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    profile = get_current_profile(db, user.id)
    return profile


@router.post(
    "/source-document", response_model=ArtifactResponse, status_code=status.HTTP_201_CREATED
)
def upload_source_document(
    request: Request,
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    require_csrf(request)
    settings = get_settings()
    allowed_types = {
        "application/pdf",
        "text/plain",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
    if file.content_type not in allowed_types:
        raise HTTPException(status_code=415, detail="Unsupported document type")
    content = file.file.read(settings.max_upload_bytes + 1)
    if len(content) > settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail="Document exceeds configured size limit")
    filename = (file.filename or "").lower()
    if file.content_type == "application/pdf" and not content.startswith(b"%PDF"):
        raise HTTPException(status_code=400, detail="Document content does not match its type")
    if (
        file.content_type
        == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        and not content.startswith(b"PK")
    ):
        raise HTTPException(status_code=400, detail="Document content does not match its type")
    if file.content_type == "text/plain" and filename.endswith((".pdf", ".docx")):
        raise HTTPException(status_code=400, detail="Document filename does not match its type")
    storage = artifact_storage()
    stored = storage.put_bytes(content, kind="profile-source", content_type=file.content_type)
    artifact = StoredArtifact(
        id=str(stored["id"]),
        owner_user_id=user.id,
        kind="profile-source",
        object_key=str(stored["object_key"]),
        content_type=file.content_type,
        size_bytes=int(stored["size_bytes"]),
        sha256=str(stored["sha256"]),
    )
    db.add(artifact)
    record_audit(
        db,
        action="profile.source_document_uploaded",
        actor="user",
        user_id=user.id,
        object_type="stored_artifact",
        object_id=artifact.id,
        metadata={"content_type": file.content_type, "size_bytes": artifact.size_bytes},
        request_id=request.state.request_id,
    )
    db.commit()
    return artifact


@router.post(
    "/versions", response_model=ProfileVersionResponse, status_code=status.HTTP_201_CREATED
)
def create_profile_version(
    payload: ProfileVersionCreate,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    require_csrf(request)
    if payload.source_document_id:
        artifact = db.scalar(
            select(StoredArtifact).where(
                StoredArtifact.id == payload.source_document_id,
                StoredArtifact.owner_user_id == user.id,
                StoredArtifact.kind == "profile-source",
            )
        )
        if artifact is None:
            raise HTTPException(status_code=404, detail="Source document not found")
    current = get_current_profile(db, user.id)
    if current:
        current.status = "superseded"
    version = ProfileVersion(
        user_id=user.id,
        source_document_id=payload.source_document_id,
        summary=payload.summary,
        normalized_json={"fact_count": len(payload.facts)},
    )
    db.add(version)
    db.flush()
    # CVs are drafted from a specific profile version's confirmed facts.
    supersede_cv_versions(
        db,
        reason="profile_version_changed",
        detail={
            "previous_profile_version_id": current.id if current else None,
            "profile_version_id": version.id,
        },
        actor="user",
        user_id=user.id,
    )
    for fact in payload.facts:
        db.add(
            ProfileFact(
                profile_version_id=version.id,
                fact_type=fact.fact_type,
                value_json=fact.value,
                provenance=fact.provenance,
                confirmation_status=fact.confirmation_status,
                source_ref=fact.source_ref,
                evidence_text=fact.evidence_text,
            )
        )
    profile = db.get(CandidateProfile, user.id)
    if profile is None:
        profile = CandidateProfile(user_id=user.id)
        db.add(profile)
    profile.current_version_id = version.id
    record_audit(
        db,
        action="profile.version_created",
        actor="user",
        user_id=user.id,
        object_type="profile_version",
        object_id=version.id,
        metadata={"fact_count": len(payload.facts)},
        request_id=request.state.request_id,
    )
    db.commit()
    db.refresh(version)
    return version


@router.get("/preferences", response_model=PreferencesResponse | None)
def get_preferences(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    preference = db.scalar(
        select(PreferenceVersion)
        .where(PreferenceVersion.user_id == user.id, PreferenceVersion.status == "active")
        .order_by(PreferenceVersion.created_at.desc())
    )
    if not preference:
        return None
    return PreferencesResponse(
        id=preference.id,
        rules=Preferences.model_validate(preference.rules_json),
        created_at=preference.created_at,
        status=preference.status,
    )


@router.put("/preferences", response_model=PreferencesResponse)
def update_preferences(
    payload: Preferences,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    require_csrf(request)
    db.query(PreferenceVersion).filter(
        PreferenceVersion.user_id == user.id, PreferenceVersion.status == "active"
    ).update({"status": "superseded"})
    preference = PreferenceVersion(user_id=user.id, rules_json=payload.model_dump())
    db.add(preference)
    record_audit(
        db,
        action="profile.preferences_updated",
        actor="user",
        user_id=user.id,
        object_type="preference_version",
        object_id=preference.id,
        request_id=request.state.request_id,
    )
    db.commit()
    db.refresh(preference)
    return PreferencesResponse(
        id=preference.id,
        rules=payload,
        created_at=preference.created_at,
        status=preference.status,
    )


@router.get("/consents", response_model=list[ConsentResponse])
def list_consents(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return db.scalars(
        select(Consent).where(Consent.user_id == user.id).order_by(Consent.granted_at.desc())
    ).all()


@router.post("/consents", response_model=ConsentResponse, status_code=status.HTTP_201_CREATED)
def grant_consent(
    payload: ConsentCreate,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    require_csrf(request)
    consent = Consent(
        user_id=user.id,
        purpose=payload.purpose,
        scope=payload.scope,
        policy_version=payload.policy_version,
    )
    db.add(consent)
    record_audit(
        db,
        action="consent.granted",
        actor="user",
        user_id=user.id,
        object_type="consent",
        object_id=consent.id,
        request_id=request.state.request_id,
    )
    db.commit()
    db.refresh(consent)
    return consent


@router.post("/consents/{purpose}/withdraw", response_model=ConsentResponse)
def withdraw_consent(
    purpose: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    require_csrf(request)
    consent = db.scalar(
        select(Consent)
        .where(
            Consent.user_id == user.id, Consent.purpose == purpose, Consent.withdrawn_at.is_(None)
        )
        .order_by(Consent.granted_at.desc())
    )
    if consent is None:
        raise HTTPException(status_code=404, detail="Active consent not found")
    consent.withdrawn_at = datetime.now(UTC)
    record_audit(
        db,
        action="consent.withdrawn",
        actor="user",
        user_id=user.id,
        object_type="consent",
        object_id=consent.id,
        request_id=request.state.request_id,
    )
    db.commit()
    return consent


@router.get("/export")
def export_profile(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    profile = get_current_profile(db, user.id)
    preferences = db.scalar(
        select(PreferenceVersion)
        .where(PreferenceVersion.user_id == user.id)
        .order_by(PreferenceVersion.created_at.desc())
    )
    consents = db.scalars(select(Consent).where(Consent.user_id == user.id)).all()
    cv_versions = db.scalars(
        select(CVVersion)
        .where(CVVersion.user_id == user.id)
        .order_by(CVVersion.created_at.asc())
    ).all()
    return {
        "user": {"id": user.id, "email": user.email},
        "profile": ProfileVersionResponse.model_validate(profile).model_dump(mode="json")
        if profile
        else None,
        "preferences": Preferences.model_validate(preferences.rules_json).model_dump(mode="json")
        if preferences
        else None,
        "consents": [
            ConsentResponse.model_validate(item).model_dump(mode="json") for item in consents
        ],
        "cv_versions": [
            {
                "id": version.id,
                "job_id": version.job_id,
                "job_snapshot_id": version.job_snapshot_id,
                "profile_version_id": version.profile_version_id,
                "match_run_id": version.match_run_id,
                "generator_version": version.generator_version,
                "template_version": version.template_version,
                "content_hash": version.content_hash,
                "status": version.status,
                "source_manifest": version.source_manifest_json,
                "claims": [
                    {
                        "id": claim.id,
                        "section": claim.section,
                        "text": claim.text,
                        "source_fact_ids": claim.source_fact_ids,
                        "job_requirement_ids": claim.job_requirement_ids,
                        "validation_status": claim.validation_status,
                        "validation_reason": claim.validation_reason,
                    }
                    for claim in db.scalars(
                        select(CVClaim).where(CVClaim.cv_version_id == version.id)
                    ).all()
                ],
            }
            for version in cv_versions
        ],
    }


@router.delete("", response_model=DataRequestResponse)
def delete_profile(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    require_csrf(request)
    storage = artifact_storage()
    db.execute(delete(CVVersion).where(CVVersion.user_id == user.id))
    artifacts = db.scalars(
        select(StoredArtifact).where(StoredArtifact.owner_user_id == user.id)
    ).all()
    for artifact in artifacts:
        storage.delete(artifact.object_key)
    db.execute(delete(StoredArtifact).where(StoredArtifact.owner_user_id == user.id))
    profile_versions = db.scalars(
        select(ProfileVersion).where(ProfileVersion.user_id == user.id)
    ).all()
    for version in profile_versions:
        version.source_document_id = None
        version.summary = None
        version.normalized_json = {}
        version.status = "deleted"
        version.deleted_at = datetime.now(UTC)
        db.execute(delete(ProfileFact).where(ProfileFact.profile_version_id == version.id))
    preferences = db.scalars(
        select(PreferenceVersion).where(PreferenceVersion.user_id == user.id)
    ).all()
    for preference in preferences:
        preference.rules_json = {}
        preference.status = "deleted"
        preference.deleted_at = datetime.now(UTC)
    profile = db.get(CandidateProfile, user.id)
    if profile:
        profile.current_version_id = None
    db.execute(delete(MatchReview).where(MatchReview.user_id == user.id))
    db.execute(delete(MatchRun).where(MatchRun.user_id == user.id))
    data_request = DataRequest(
        user_id=user.id,
        request_type="profile_delete",
        status="completed",
        completed_at=datetime.now(UTC),
    )
    db.add(data_request)
    record_audit(
        db,
        action="profile.deleted",
        actor="user",
        user_id=user.id,
        object_type="data_request",
        object_id=data_request.id,
        metadata={"artifact_count": len(artifacts)},
        request_id=request.state.request_id,
    )
    db.commit()
    return data_request
