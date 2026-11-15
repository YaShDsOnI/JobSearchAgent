"""Candidate data deletion, shared by the API and by deletion replay after a restore.

Replay exists because a backup taken before a deletion still holds the deleted data. Every
restore, whatever produced it, must re-apply the deletions completed after the backup was
taken. See `docs/runbooks.md`, "Restore", and `docs/security.md`, "Backups".
"""

from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db.models import (
    Application,
    CandidateProfile,
    CVClaim,
    CVVersion,
    DataRequest,
    MatchReview,
    MatchRun,
    PreferenceVersion,
    ProfileFact,
    ProfileVersion,
    StoredArtifact,
)
from app.services.applications import delete_applications
from app.services.audit import record_audit
from app.services.storage import ArtifactStorage
from app.services.tracker import delete_all_entries


def delete_candidate_data(
    db: Session,
    storage: ArtifactStorage,
    user_id: str,
    *,
    actor: str,
    request_id: str | None = None,
    replay_of: str | None = None,
) -> DataRequest:
    """Delete a candidate's profile data and record a completed `profile_delete` request.

    The account row stays (it anchors the audit trail and the data request). The caller
    commits. `replay_of` is the ledger's data request ID when this re-applies a deletion
    after a restore.
    """
    # The tracker records where the candidate applied: personal data, deleted with the profile.
    tracker_count = delete_all_entries(db, user_id)
    # Packages embed profile-derived answers and reference CVs: delete them first.
    application_count = delete_applications(
        db,
        application_ids=list(
            db.scalars(select(Application.id).where(Application.user_id == user_id)).all()
        ),
    )
    db.execute(
        delete(CVClaim).where(
            CVClaim.cv_version_id.in_(select(CVVersion.id).where(CVVersion.user_id == user_id))
        )
    )
    db.execute(delete(CVVersion).where(CVVersion.user_id == user_id))
    artifacts = db.scalars(
        select(StoredArtifact).where(StoredArtifact.owner_user_id == user_id)
    ).all()
    for artifact in artifacts:
        storage.delete(artifact.object_key)
    db.execute(delete(StoredArtifact).where(StoredArtifact.owner_user_id == user_id))
    profile_versions = db.scalars(
        select(ProfileVersion).where(ProfileVersion.user_id == user_id)
    ).all()
    for version in profile_versions:
        version.source_document_id = None
        version.summary = None
        version.normalized_json = {}
        version.status = "deleted"
        version.deleted_at = datetime.now(UTC)
        db.execute(delete(ProfileFact).where(ProfileFact.profile_version_id == version.id))
    preferences = db.scalars(
        select(PreferenceVersion).where(PreferenceVersion.user_id == user_id)
    ).all()
    for preference in preferences:
        preference.rules_json = {}
        preference.status = "deleted"
        preference.deleted_at = datetime.now(UTC)
    profile = db.get(CandidateProfile, user_id)
    if profile:
        profile.current_version_id = None
    db.execute(delete(MatchReview).where(MatchReview.user_id == user_id))
    db.execute(delete(MatchRun).where(MatchRun.user_id == user_id))
    data_request = DataRequest(
        user_id=user_id,
        request_type="profile_delete",
        status="completed",
        completed_at=datetime.now(UTC),
    )
    db.add(data_request)
    db.flush()
    metadata: dict[str, object] = {
        "artifact_count": len(artifacts),
        "application_count": application_count,
        "tracker_count": tracker_count,
    }
    if replay_of is not None:
        metadata["replay_of"] = replay_of
    record_audit(
        db,
        action="profile.deletion_replayed" if replay_of is not None else "profile.deleted",
        actor=actor,
        user_id=user_id,
        object_type="data_request",
        object_id=data_request.id,
        metadata=metadata,
        request_id=request_id,
    )
    return data_request
