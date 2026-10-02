"""When job re-ingestion or profile edits invalidate drafted/approved CV versions."""

import hashlib
import json
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import AuditEvent, CVClaim, CVVersion, JobSnapshot, StoredArtifact
from app.services.catalog import ingest_fixture_records, latest_snapshot
from app.services.sources.normalize import stable_hash
from app.services.storage import ArtifactStorage
from tests.conftest import TestingSessionLocal, csrf_headers
from tests.test_cv_engine import _create_cv_profile
from tests.test_phase2_matching import register, seed_job


def _approved_cv(client: TestClient, email: str) -> tuple[str, str]:
    register(client, email)
    job_id = seed_job()
    _create_cv_profile(client)
    match = client.post(f"/api/v1/jobs/{job_id}/match", headers=csrf_headers(client)).json()
    created = client.post(
        f"/api/v1/jobs/{job_id}/cv-drafts",
        headers=csrf_headers(client),
        json={"match_run_id": match["id"]},
    )
    assert created.status_code == 201
    version_id = created.json()["id"]
    approved = client.post(
        f"/api/v1/cv-versions/{version_id}/approve", headers=csrf_headers(client)
    )
    assert approved.status_code == 200
    return job_id, version_id


def _raw_record(job_id: str) -> dict[str, Any]:
    storage = ArtifactStorage(get_settings())
    with TestingSessionLocal() as db:
        snapshot = latest_snapshot(db, job_id)
        assert snapshot is not None
        artifact = db.get(StoredArtifact, snapshot.raw_artifact_id)
        assert artifact is not None
        return json.loads(storage.read_bytes(artifact.object_key))


def _reingest(record: dict[str, Any]) -> dict[str, Any]:
    with TestingSessionLocal() as db:
        return ingest_fixture_records(db, ArtifactStorage(get_settings()), [record]).stats


def _artifact_fingerprint(version_id: str) -> dict[str, Any]:
    storage = ArtifactStorage(get_settings())
    with TestingSessionLocal() as db:
        version = db.get(CVVersion, version_id)
        assert version is not None
        blobs = {}
        for artifact_id in (version.docx_artifact_id, version.pdf_artifact_id):
            artifact = db.get(StoredArtifact, artifact_id)
            assert artifact is not None
            content = storage.read_bytes(artifact.object_key)
            blobs[artifact.kind] = (artifact.sha256, hashlib.sha256(content).hexdigest())
        claims = db.scalars(
            select(CVClaim.text).where(CVClaim.cv_version_id == version_id).order_by(CVClaim.id)
        ).all()
        return {
            "content_hash": version.content_hash,
            "job_snapshot_id": version.job_snapshot_id,
            "approved_at": version.approved_at,
            "approved_by": version.approved_by_user_id,
            "blobs": blobs,
            "claims": list(claims),
        }


def test_material_job_change_supersedes_with_recorded_reason_and_immutable_artifacts(client):
    job_id, version_id = _approved_cv(client, "material@example.test")
    before = _artifact_fingerprint(version_id)
    record = _raw_record(job_id)
    record["requirements"] = record["requirements"] + [
        {"skill": "Python", "kind": "required", "evidence": "Python is required"}
    ]

    stats = _reingest(record)

    assert stats["updated"] == 1 and stats["cv_versions_superseded"] == 1
    body = client.get(f"/api/v1/cv-versions/{version_id}").json()
    assert body["status"] == "superseded"
    assert body["superseded_reason"] == "job_material_change"
    assert body["superseded_detail"]["changed_material_fields"] == ["requirements"]
    assert body["superseded_at"] is not None
    with TestingSessionLocal() as db:
        new_snapshot = latest_snapshot(db, job_id)
        assert new_snapshot is not None
        assert body["superseded_detail"]["job_snapshot_id"] == new_snapshot.id
        event = db.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "cv.superseded", AuditEvent.object_id == version_id
            )
        )
        assert event is not None
        assert event.metadata_json["previous_status"] == "approved"
        assert event.metadata_json["reason"] == "job_material_change"
    # The approved artifact, its claims, hashes, and approval record are untouched.
    assert _artifact_fingerprint(version_id) == before


def test_harmless_renormalization_of_legacy_snapshot_keeps_cv_approved(client):
    job_id, version_id = _approved_cv(client, "renormalize@example.test")
    record = _raw_record(job_id)
    with TestingSessionLocal() as db:
        # Recreate what a pre-Phase-4 database holds: the raw record stored as-is.
        snapshot = latest_snapshot(db, job_id)
        assert snapshot is not None
        snapshot.normalized_json = record
        snapshot.content_hash = stable_hash(record)
        snapshot.normalizer_version = "legacy-v0"
        db.commit()

    stats = _reingest(record)

    assert stats["updated"] == 1  # A new, re-normalized snapshot version is recorded ...
    assert stats.get("cv_versions_superseded", 0) == 0  # ... without invalidating the CV.
    assert client.get(f"/api/v1/cv-versions/{version_id}").json()["status"] == "approved"
    with TestingSessionLocal() as db:
        versions = db.scalars(
            select(JobSnapshot.version).where(JobSnapshot.job_id == job_id)
        ).all()
        assert sorted(versions) == [1, 2]
        update = db.scalars(
            select(AuditEvent)
            .where(AuditEvent.action == "job.updated", AuditEvent.object_id == job_id)
        ).one()
        assert update.metadata_json["material_change"] is False


def test_non_material_source_edits_do_not_invalidate_cv(client):
    job_id, version_id = _approved_cv(client, "cosmetic@example.test")
    record = _raw_record(job_id)
    record.update(
        {
            "posted_at": "2026-09-01",
            "closing_at": "2026-12-31",
            "benefits": ["Learning budget"],
            "canonical_url": record["canonical_url"] + "?utm_source=newsletter",
        }
    )
    stats = _reingest(record)
    assert stats["updated"] == 1
    assert stats.get("cv_versions_superseded", 0) == 0
    assert client.get(f"/api/v1/cv-versions/{version_id}").json()["status"] == "approved"


def test_profile_change_supersedes_with_reason_but_leaves_rejected_cvs(client):
    job_id, approved_id = _approved_cv(client, "profile-change@example.test")
    match = client.post(f"/api/v1/jobs/{job_id}/match", headers=csrf_headers(client)).json()
    rejected_id = client.post(
        f"/api/v1/jobs/{job_id}/cv-drafts",
        headers=csrf_headers(client),
        json={"match_run_id": match["id"]},
    ).json()["id"]
    assert (
        client.post(
            f"/api/v1/cv-versions/{rejected_id}/reject",
            headers=csrf_headers(client),
            json={"note": "Not this version"},
        ).status_code
        == 200
    )

    _create_cv_profile(client)

    approved = client.get(f"/api/v1/cv-versions/{approved_id}").json()
    assert approved["status"] == "superseded"
    assert approved["superseded_reason"] == "profile_version_changed"
    assert approved["superseded_detail"]["profile_version_id"] != approved["profile_version_id"]
    rejected = client.get(f"/api/v1/cv-versions/{rejected_id}").json()
    assert rejected["status"] == "rejected" and rejected["superseded_reason"] is None
