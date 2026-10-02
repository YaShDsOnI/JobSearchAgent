"""Deciding when a job or profile change invalidates drafted/approved CV versions.

Supersession only changes a CV's status and records why; the CV rows, claims, rendered
artifacts, hashes, and approval metadata are never modified.
"""

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import CVVersion, JobSnapshot, now_utc
from app.services.audit import record_audit
from app.services.sources.normalize import RecordRejected, normalize_record

ACTIVE_CV_STATUSES = ("validated", "approved")

# Normalized job fields that feed eligibility, fit, preference scoring, or CV tailoring.
# A change to any of them can make an existing CV draft wrong for the job.
MATERIAL_JOB_FIELDS = (
    "title",
    "employer",
    "description",
    "requirements",
    "location_text",
    "secondary_locations",
    "location_country",
    "work_mode",
    "remote_geography",
    "permitted_countries",
    "work_authorization_requirements",
    "sponsorship",
    "relocation_assistance",
    "compensation",
    "minimum_experience_years",
    "education",
    "employment_type",
)
# Not material for the CV itself: schema_version, source_job_id, canonical_url, benefits,
# posted_at/updated_at/closing_at, and status. Closure is tracked by the posting lifecycle;
# later application steps must gate on it rather than on CV status.


def comparable_job_content(snapshot: JobSnapshot) -> dict[str, Any] | None:
    """Re-normalize a stored snapshot with the current normalizer.

    This removes representation-only differences (e.g. a `legacy-v0` snapshot that stored
    the raw record) so only genuine source changes count. Returns None if the stored
    content no longer normalizes, which callers treat as a material change.
    """
    try:
        return normalize_record(dict(snapshot.normalized_json)).content
    except RecordRejected:
        return None


def changed_material_fields(
    previous: Mapping[str, Any] | None, current: Mapping[str, Any]
) -> list[str]:
    if previous is None:
        return list(MATERIAL_JOB_FIELDS)
    return [field for field in MATERIAL_JOB_FIELDS if previous.get(field) != current.get(field)]


def supersede_cv_versions(
    db: Session,
    *,
    reason: str,
    detail: dict[str, Any],
    actor: str,
    job_id: str | None = None,
    user_id: str | None = None,
    now: datetime | None = None,
) -> list[str]:
    if job_id is None and user_id is None:
        raise ValueError("A job or user scope is required")
    query = select(CVVersion).where(CVVersion.status.in_(ACTIVE_CV_STATUSES))
    if job_id is not None:
        query = query.where(CVVersion.job_id == job_id)
    if user_id is not None:
        query = query.where(CVVersion.user_id == user_id)
    now = now or now_utc()
    superseded: list[str] = []
    for version in db.scalars(query.order_by(CVVersion.created_at, CVVersion.id)).all():
        previous_status = version.status
        version.status = "superseded"
        version.superseded_at = now
        version.superseded_reason = reason
        version.superseded_detail_json = dict(detail)
        record_audit(
            db,
            action="cv.superseded",
            actor=actor,
            user_id=version.user_id,
            object_type="cv_version",
            object_id=version.id,
            metadata={"reason": reason, "previous_status": previous_status, **detail},
        )
        superseded.append(version.id)
    db.flush()
    return superseded
