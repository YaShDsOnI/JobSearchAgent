"""Encrypted artifact lifecycle: orphan reconciliation and private-source deletion.

Ordering rule for every deletion: database rows are removed and committed first, blobs
are deleted afterwards. A crash between the two leaves only unreferenced ciphertext,
which `reconcile_artifacts` later removes; the reverse order could leave rows pointing
at missing evidence.
"""

import logging
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.db.models import (
    CVClaim,
    CVVersion,
    JobAnalysis,
    JobEvidence,
    JobPosting,
    JobRequirement,
    JobSnapshot,
    MatchReview,
    MatchRun,
    SourceConnector,
    SourceRun,
    StoredArtifact,
    Task,
    now_utc,
)
from app.services.audit import record_audit
from app.services.sources.dedup import reconcile_duplicate_link
from app.services.sources.errors import (
    ConcurrentRunError,
    SourceErrorCode,
    SourcePolicyError,
)
from app.services.sources.policy import as_aware
from app.services.sources.worker import INGEST_TASK_KIND
from app.services.storage import ArtifactStorage

logger = logging.getLogger(__name__)

# Must exceed the longest time a blob can legitimately exist before its row commits
# (an ingestion run is considered abandoned after 15 minutes).
DEFAULT_ORPHAN_GRACE = timedelta(hours=1)
MAX_REPORTED_KEYS = 100


@dataclass
class ReconciliationReport:
    applied: bool
    orphan_files: list[str] = field(default_factory=list)
    unreferenced_snapshot_artifacts: list[str] = field(default_factory=list)
    missing_files: list[str] = field(default_factory=list)
    deleted_files: int = 0
    deleted_rows: int = 0

    def summary(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "orphan_files": len(self.orphan_files),
            "unreferenced_snapshot_artifacts": len(self.unreferenced_snapshot_artifacts),
            "missing_files": len(self.missing_files),
            "deleted_files": self.deleted_files,
            "deleted_rows": self.deleted_rows,
        }


def reconcile_artifacts(
    db: Session,
    storage: ArtifactStorage,
    *,
    apply: bool = False,
    grace: timedelta = DEFAULT_ORPHAN_GRACE,
    now: datetime | None = None,
    actor: str = "system",
) -> ReconciliationReport:
    """Find (and with `apply`, remove) encrypted blobs and rows left by interrupted work.

    - orphan files: blobs with no `stored_artifacts` row, older than `grace`.
    - unreferenced snapshot artifacts: `job-snapshot` rows no snapshot points to, older
      than `grace` (other kinds are owned by CV/profile records and are never touched).
    - missing files: rows whose blob is gone. Reported only; never auto-deleted, because
      that is an integrity incident to investigate, not garbage.
    """
    if grace < timedelta(minutes=30):
        raise ValueError("Grace period must cover in-flight ingestion runs")
    now = now or now_utc()
    cutoff = now - grace
    report = ReconciliationReport(applied=apply)

    known_keys = set(db.scalars(select(StoredArtifact.object_key)).all())
    for object_key, modified_at in storage.iter_objects():
        if object_key not in known_keys and modified_at < cutoff:
            report.orphan_files.append(object_key)

    referenced = select(JobSnapshot.raw_artifact_id).where(
        JobSnapshot.raw_artifact_id.is_not(None)
    )
    unreferenced = db.scalars(
        select(StoredArtifact).where(
            StoredArtifact.kind == "job-snapshot",
            StoredArtifact.id.not_in(referenced),
        )
    ).all()
    stale_rows = [row for row in unreferenced if as_aware(row.created_at) < cutoff]
    report.unreferenced_snapshot_artifacts = [row.object_key for row in stale_rows]

    for object_key in sorted(known_keys):
        if not storage.exists(object_key):
            report.missing_files.append(object_key)

    if apply:
        if stale_rows:
            db.execute(
                delete(StoredArtifact).where(StoredArtifact.id.in_([r.id for r in stale_rows]))
            )
            report.deleted_rows = len(stale_rows)
        record_audit(
            db,
            action="artifacts.reconciled",
            actor=actor,
            object_type="artifact_store",
            metadata=report.summary(),
        )
        db.commit()
        for object_key in report.orphan_files + report.unreferenced_snapshot_artifacts:
            if _delete_blob(storage, object_key):
                report.deleted_files += 1
    if report.missing_files:
        logger.error(
            "Artifact rows reference missing blobs", extra={"error_type": "missing_blob"}
        )
    report.orphan_files = report.orphan_files[:MAX_REPORTED_KEYS]
    report.unreferenced_snapshot_artifacts = report.unreferenced_snapshot_artifacts[
        :MAX_REPORTED_KEYS
    ]
    report.missing_files = report.missing_files[:MAX_REPORTED_KEYS]
    return report


def _delete_blob(storage: ArtifactStorage, object_key: str) -> bool:
    with suppress(OSError, ValueError):
        existed = storage.exists(object_key)
        storage.delete(object_key)
        return existed
    return False


def delete_private_source(
    db: Session,
    storage: ArtifactStorage,
    *,
    connector_id: str,
    requested_by_user_id: str,
    actor: str = "user",
    request_id: str | None = None,
) -> dict[str, int]:
    """Delete a tenant-private source and everything derived from it.

    Removes postings, snapshots, raw snapshot blobs, analyses, match runs/reviews, and CV
    versions (with their claims and rendered artifacts) for the source's jobs, plus its
    run history, and cancels queued ingestion tasks. Only the owner may do this; shared
    catalog sources are never deleted through this path. Audit events are retained: they
    hold identifiers and codes, not source content.
    """
    connector = db.get(SourceConnector, connector_id)
    if connector is None or connector.owner_user_id != requested_by_user_id:
        raise SourcePolicyError(SourceErrorCode.TENANT_MISMATCH, "Connector is not accessible")
    if connector.active_run_id is not None:
        active = db.get(SourceRun, connector.active_run_id)
        if active is not None and active.status == "running":
            raise ConcurrentRunError("Cannot delete a source while an ingestion run is active")

    job_ids = list(db.scalars(select(JobPosting.id).where(JobPosting.connector_id == connector.id)))
    snapshot_ids = list(
        db.scalars(select(JobSnapshot.id).where(JobSnapshot.job_id.in_(job_ids)))
    )
    cv_versions = list(db.scalars(select(CVVersion).where(CVVersion.job_id.in_(job_ids))))
    cv_ids = [version.id for version in cv_versions]
    cv_artifact_ids = [
        artifact_id
        for version in cv_versions
        for artifact_id in (version.docx_artifact_id, version.pdf_artifact_id)
        if artifact_id
    ]
    raw_artifact_ids = list(
        db.scalars(
            select(JobSnapshot.raw_artifact_id).where(
                JobSnapshot.id.in_(snapshot_ids), JobSnapshot.raw_artifact_id.is_not(None)
            )
        )
    )
    artifact_ids = [item for item in cv_artifact_ids + raw_artifact_ids if item]
    object_keys = list(
        db.scalars(select(StoredArtifact.object_key).where(StoredArtifact.id.in_(artifact_ids)))
    )
    match_ids = list(
        db.scalars(select(MatchRun.id).where(MatchRun.job_snapshot_id.in_(snapshot_ids)))
    )
    analysis_ids = list(
        db.scalars(select(JobAnalysis.id).where(JobAnalysis.job_snapshot_id.in_(snapshot_ids)))
    )
    external_dependents = list(
        db.scalars(
            select(JobPosting).where(
                JobPosting.duplicate_of_job_id.in_(job_ids),
                JobPosting.connector_id != connector.id,
            )
        )
    )

    # Explicit deletes in dependency order (SQLite does not enforce FK cascades).
    db.execute(delete(CVClaim).where(CVClaim.cv_version_id.in_(cv_ids)))
    db.execute(delete(CVVersion).where(CVVersion.id.in_(cv_ids)))
    db.execute(
        update(MatchReview)
        .where(MatchReview.rerun_match_id.in_(match_ids))
        .values(rerun_match_id=None)
    )
    db.execute(delete(MatchReview).where(MatchReview.match_run_id.in_(match_ids)))
    db.execute(delete(MatchRun).where(MatchRun.id.in_(match_ids)))
    db.execute(delete(JobRequirement).where(JobRequirement.analysis_id.in_(analysis_ids)))
    db.execute(delete(JobEvidence).where(JobEvidence.analysis_id.in_(analysis_ids)))
    db.execute(delete(JobAnalysis).where(JobAnalysis.id.in_(analysis_ids)))
    db.execute(delete(JobSnapshot).where(JobSnapshot.id.in_(snapshot_ids)))
    db.execute(delete(StoredArtifact).where(StoredArtifact.id.in_(artifact_ids)))
    for dependent in external_dependents:
        dependent.duplicate_of_job_id = None
        dependent.dedup_reason = None
    db.flush()
    db.execute(delete(JobPosting).where(JobPosting.id.in_(job_ids)))
    db.execute(delete(SourceRun).where(SourceRun.connector_id == connector.id))
    cancelled = 0
    for task in db.scalars(
        select(Task).where(Task.kind == INGEST_TASK_KIND, Task.state == "queued")
    ):
        if (task.payload_json or {}).get("connector_id") == connector.id:
            task.state = "cancelled"
            cancelled += 1
    db.delete(connector)
    db.flush()
    for dependent in external_dependents:
        reconcile_duplicate_link(db, dependent)

    counts = {
        "job_postings": len(job_ids),
        "job_snapshots": len(snapshot_ids),
        "cv_versions": len(cv_ids),
        "match_runs": len(match_ids),
        "artifacts": len(artifact_ids),
        "tasks_cancelled": cancelled,
    }
    record_audit(
        db,
        action="source.deleted",
        actor=actor,
        user_id=requested_by_user_id,
        object_type="source_connector",
        object_id=connector_id,
        metadata=counts,
        request_id=request_id,
    )
    db.commit()
    counts["blobs_deleted"] = sum(_delete_blob(storage, key) for key in object_keys)
    return counts
