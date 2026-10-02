"""Conservative cross-source deduplication.

Postings are linked, never merged: every source keeps its own identity, snapshots, and
provenance. Links are only made across different connectors within the same tenant
scope, on an exact normalized-URL match or an exact normalized-content match.
"""

import hashlib
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db.models import JobPosting
from app.services.sources.policy import as_aware

TRACKING_PARAMS = {
    "gh_src",
    "ref",
    "referrer",
    "source",
    "src",
    "lever-source",
    "lever-origin",
    "trk",
    "trackingid",
}


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    port = parts.port
    netloc = host if port in (None, 443 if parts.scheme == "https" else 80) else f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if len(path) > 1:
        path = path.rstrip("/")
    query = sorted(
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_PARAMS
    )
    return urlunsplit((parts.scheme.lower(), netloc, path, urlencode(query), ""))


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def url_fingerprint(url: str) -> str:
    return _digest(normalize_url(url))


def _fold(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def content_fingerprint(content: dict[str, Any]) -> str | None:
    # Title + employer alone is too weak (an employer can have several identical openings);
    # require an exact description match too.
    if not _fold(content.get("description")):
        return None
    keys = ("employer", "title", "location_text", "description")
    parts = [_fold(content.get(key)) for key in keys]
    return _digest("\x1f".join(parts))


def match_reason(job: JobPosting, other: JobPosting) -> str | None:
    if job.connector_id == other.connector_id or job.owner_user_id != other.owner_user_id:
        return None
    if job.url_fingerprint and job.url_fingerprint == other.url_fingerprint:
        return "canonical_url"
    if job.content_fingerprint and job.content_fingerprint == other.content_fingerprint:
        return "content"
    return None


def _owner_scope(owner_user_id: str | None):
    if owner_user_id is None:
        return JobPosting.owner_user_id.is_(None)
    return JobPosting.owner_user_id == owner_user_id


def find_primary(db: Session, job: JobPosting) -> tuple[JobPosting, str] | None:
    fingerprints = []
    if job.url_fingerprint:
        fingerprints.append(JobPosting.url_fingerprint == job.url_fingerprint)
    if job.content_fingerprint:
        fingerprints.append(JobPosting.content_fingerprint == job.content_fingerprint)
    if not fingerprints:
        return None
    candidates = db.scalars(
        select(JobPosting)
        .where(
            JobPosting.id != job.id,
            JobPosting.connector_id != job.connector_id,
            JobPosting.duplicate_of_job_id.is_(None),
            _owner_scope(job.owner_user_id),
            or_(*fingerprints),
        )
        .order_by(JobPosting.first_seen_at.asc(), JobPosting.id.asc())
    ).all()
    job_order = (as_aware(job.first_seen_at), job.id)
    for candidate in candidates:
        # Only an earlier posting can be primary, so links never form cycles or chains.
        if (as_aware(candidate.first_seen_at), candidate.id) >= job_order:
            continue
        reason = match_reason(job, candidate)
        if reason:
            return candidate, reason
    return None


def reconcile_duplicate_link(
    db: Session, job: JobPosting
) -> list[tuple[JobPosting, str, str | None]]:
    """Re-evaluate links for `job` and the postings linked to it.

    Returns (posting, change, reason) tuples where change is "linked" or "unlinked".
    """
    changes: list[tuple[JobPosting, str, str | None]] = []
    db.flush()
    dependents = db.scalars(
        select(JobPosting).where(JobPosting.duplicate_of_job_id == job.id)
    ).all()
    unlinked_dependents: list[JobPosting] = []
    for dependent in dependents:
        reason = match_reason(dependent, job)
        if reason is None:
            dependent.duplicate_of_job_id = None
            dependent.dedup_reason = None
            unlinked_dependents.append(dependent)
            changes.append((dependent, "unlinked", None))
        elif reason != dependent.dedup_reason:
            dependent.dedup_reason = reason
    still_has_dependents = len(unlinked_dependents) < len(dependents)

    if job.duplicate_of_job_id is not None:
        primary = db.get(JobPosting, job.duplicate_of_job_id)
        reason = match_reason(job, primary) if primary is not None else None
        if reason is not None and primary is not None and primary.duplicate_of_job_id is None:
            job.dedup_reason = reason
        else:
            job.duplicate_of_job_id = None
            job.dedup_reason = None
            changes.append((job, "unlinked", None))
    db.flush()

    if job.duplicate_of_job_id is None and not still_has_dependents:
        _link_to_primary(db, job, changes)
    for dependent in unlinked_dependents:
        _link_to_primary(db, dependent, changes)
    db.flush()
    return changes


def _link_to_primary(
    db: Session, job: JobPosting, changes: list[tuple[JobPosting, str, str | None]]
) -> None:
    found = find_primary(db, job)
    if found is not None:
        primary, reason = found
        job.duplicate_of_job_id = primary.id
        job.dedup_reason = reason
        changes.append((job, "linked", reason))
        db.flush()
