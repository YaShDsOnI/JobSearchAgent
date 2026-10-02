"""Artifact reconciliation, private-source deletion, and operator role management."""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.admin as admin_cli
from app.core.config import get_settings
from app.db.models import (
    AuditEvent,
    CVVersion,
    JobPosting,
    JobSnapshot,
    MatchRun,
    SourceConnector,
    SourceRun,
    StoredArtifact,
    Task,
    User,
)
from app.services.artifact_lifecycle import delete_private_source, reconcile_artifacts
from app.services.catalog import FIXTURE_AUTHORIZATION
from app.services.operators import RoleChangeError, set_user_role
from app.services.sources.errors import SourceError, SourcePolicyError
from app.services.sources.pipeline import run_ingestion
from app.services.sources.registry import ProviderRegistry
from app.services.sources.worker import enqueue_ingestion
from app.services.storage import ArtifactStorage
from tests.conftest import TestingSessionLocal, app, csrf_headers
from tests.test_cv_engine import _create_cv_profile
from tests.test_phase4_discovery import ScriptedProvider, job_record

NOW = datetime.now(UTC)


def _storage() -> ArtifactStorage:
    return ArtifactStorage(get_settings())


@pytest.fixture()
def isolated_storage(tmp_path) -> ArtifactStorage:
    # Reconciliation scans the whole root, so it needs a root no other test writes to.
    settings = get_settings().model_copy(update={"artifact_storage_path": str(tmp_path)})
    return ArtifactStorage(settings)


def _age(storage: ArtifactStorage, object_key: str, hours: float) -> None:
    path = Path(storage.root) / object_key
    stamp = (NOW - timedelta(hours=hours)).timestamp()
    os.utime(path, (stamp, stamp))


def _connector(db, name: str, owner_user_id: str | None = None) -> SourceConnector:
    connector = SourceConnector(
        name=name,
        kind="fixture",
        tier="local",
        terms_version="fixture-v1",
        policy_status="approved",
        enabled=True,
        owner_user_id=owner_user_id,
        authorization_json=dict(FIXTURE_AUTHORIZATION),
        config_json={"allowed_url_hosts": ["example.invalid"]},
    )
    db.add(connector)
    db.commit()
    return connector


def _ingest(db, connector: SourceConnector, records, storage=None):
    registry = ProviderRegistry()
    registry.register("fixture", lambda: ScriptedProvider(records))
    return run_ingestion(
        db, storage or _storage(), connector_id=connector.id, registry=registry
    )


def _register(client: TestClient, email: str) -> str:
    response = client.post(
        "/api/v1/auth/register", json={"email": email, "password": "lifecycle-password-123"}
    )
    assert response.status_code == 201
    return response.json()["id"]


# --- orphan reconciliation --------------------------------------------------------------


def test_reconciliation_finds_and_removes_only_old_unreferenced_blobs(isolated_storage):
    storage = isolated_storage
    with TestingSessionLocal() as db:
        connector = _connector(db, "reconcile")
        _ingest(db, connector, [job_record("kept-1")], storage)
        referenced = db.scalars(select(StoredArtifact)).one()
        _age(storage, referenced.object_key, 5)

        old_orphan = str(storage.put_json({"x": 1}, kind="job-snapshot")["object_key"])
        _age(storage, old_orphan, 3)
        fresh_orphan = str(storage.put_json({"x": 2}, kind="job-snapshot")["object_key"])

        stale_row_data = storage.put_json({"x": 3}, kind="job-snapshot")
        stale_row = StoredArtifact(
            id=str(stale_row_data["id"]),
            kind="job-snapshot",
            object_key=str(stale_row_data["object_key"]),
            content_type="application/json",
            size_bytes=1,
            sha256="0" * 64,
            created_at=NOW - timedelta(hours=3),
        )
        missing = StoredArtifact(
            kind="cv-pdf",
            object_key="cv-pdf/00000000-0000-0000-0000-000000000000.bin",
            content_type="application/pdf",
            size_bytes=1,
            sha256="0" * 64,
        )
        db.add_all([stale_row, missing])
        db.commit()

        dry_run = reconcile_artifacts(db, storage, now=NOW)
        assert dry_run.orphan_files == [old_orphan]
        assert dry_run.unreferenced_snapshot_artifacts == [stale_row.object_key]
        assert dry_run.missing_files == [missing.object_key]
        assert storage.exists(old_orphan) and storage.exists(stale_row.object_key)

        applied = reconcile_artifacts(db, storage, apply=True, now=NOW)
        assert applied.deleted_files == 2 and applied.deleted_rows == 1
        assert not storage.exists(old_orphan)
        assert not storage.exists(stale_row.object_key)
        assert db.get(StoredArtifact, stale_row.id) is None
        # In-grace blobs, referenced evidence, and integrity problems are left alone.
        assert storage.exists(fresh_orphan)
        assert storage.exists(referenced.object_key)
        assert db.get(StoredArtifact, missing.id) is not None
        assert db.scalar(select(AuditEvent).where(AuditEvent.action == "artifacts.reconciled"))
        storage.delete(fresh_orphan)

    with pytest.raises(ValueError), TestingSessionLocal() as db:
        reconcile_artifacts(db, storage, grace=timedelta(minutes=5))


def test_blobs_left_by_an_interrupted_ingestion_are_recovered_by_reconciliation(
    isolated_storage,
):
    real = isolated_storage
    written: list[str] = []

    class CrashingStorage(ArtifactStorage):
        """Writes one blob, then fails; cleanup also fails, as in a hard crash."""

        def __init__(self) -> None:
            self.__dict__.update(real.__dict__)

        def put_json(self, value, *, kind):
            if written:
                raise OSError("disk full")
            data = super().put_json(value, kind=kind)
            written.append(str(data["object_key"]))
            return data

        def delete(self, object_key: str) -> None:
            raise OSError("storage unavailable")

    with TestingSessionLocal() as db:
        connector = _connector(db, "interrupted")
        with pytest.raises(SourceError):
            _ingest(db, connector, [job_record("i-1"), job_record("i-2")], CrashingStorage())
        assert db.query(StoredArtifact).count() == 0
        assert real.exists(written[0])  # The orphan blob survived the failed cleanup.
        _age(real, written[0], 2)

        report = reconcile_artifacts(db, real, apply=True, now=NOW)
        assert report.orphan_files == [written[0]]
        assert not real.exists(written[0])


# --- private source deletion ------------------------------------------------------------


def test_owner_can_delete_private_source_and_all_derived_data(client):
    owner_id = _register(client, "private-owner@example.test")
    other = TestClient(app)
    _register(other, "private-other@example.test")
    storage = _storage()
    onsite = job_record(
        "phase2-onsite",
        title="Example Data Analyst",
        description="Sanitized fixture. SQL is required and Python is preferred.",
        sponsorship="unknown",
        requirements=[
            {"skill": "SQL", "kind": "required", "evidence": "SQL is required"},
            {"skill": "Python", "kind": "preferred", "evidence": "Python is preferred"},
        ],
    )
    with TestingSessionLocal() as db:
        shared = _connector(db, "shared")
        private = _connector(db, "private", owner_user_id=owner_id)
        mirror = _connector(db, "private-mirror", owner_user_id=owner_id)
        _ingest(db, shared, [job_record("shared-1")])
        _ingest(db, private, [onsite])
        _ingest(db, mirror, [dict(onsite, source_job_id="mirror-1")])
        private_job = db.scalars(
            select(JobPosting).where(JobPosting.connector_id == private.id)
        ).one()
        mirror_job = db.scalars(
            select(JobPosting).where(JobPosting.connector_id == mirror.id)
        ).one()
        assert mirror_job.duplicate_of_job_id == private_job.id
        enqueue_ingestion(
            db, connector_id=private.id, idempotency_key="queued-1", requested_by_user_id=owner_id
        )
        db.commit()
        private_id, private_job_id, mirror_job_id = private.id, private_job.id, mirror_job.id
        blob_keys = list(db.scalars(select(StoredArtifact.object_key)))

    _create_cv_profile(client)
    match = client.post(
        f"/api/v1/jobs/{private_job_id}/match", headers=csrf_headers(client)
    ).json()
    assert match["eligibility"] == "eligible"
    cv = client.post(
        f"/api/v1/jobs/{private_job_id}/cv-drafts",
        headers=csrf_headers(client),
        json={"match_run_id": match["id"]},
    )
    assert cv.status_code == 201

    assert other.delete(
        f"/api/v1/connectors/{private_id}", headers=csrf_headers(other)
    ).status_code == 404
    with TestingSessionLocal() as db:
        shared_id = db.scalars(
            select(SourceConnector.id).where(SourceConnector.name == "shared")
        ).one()
    assert client.delete(
        f"/api/v1/connectors/{shared_id}", headers=csrf_headers(client)
    ).status_code == 403

    response = client.delete(f"/api/v1/connectors/{private_id}", headers=csrf_headers(client))
    assert response.status_code == 200
    body = response.json()
    assert body["job_postings"] == 1 and body["cv_versions"] == 1
    assert body["tasks_cancelled"] == 1 and body["blobs_deleted"] == body["artifacts"] == 3

    with TestingSessionLocal() as db:
        assert db.get(SourceConnector, private_id) is None
        assert db.get(JobPosting, private_job_id) is None
        snapshots = select(JobSnapshot).where(JobSnapshot.job_id == private_job_id)
        assert db.scalars(snapshots).all() == []
        assert db.scalars(select(CVVersion)).all() == []
        assert db.scalars(select(MatchRun)).all() == []
        assert db.scalars(select(SourceRun).where(SourceRun.connector_id == private_id)).all() == []
        assert db.scalars(select(Task.state)).all() == ["cancelled"]
        mirror_after = db.get(JobPosting, mirror_job_id)
        assert mirror_after is not None and mirror_after.duplicate_of_job_id is None
        assert db.query(JobPosting).count() == 2  # shared + mirror untouched
        assert db.scalar(select(AuditEvent).where(AuditEvent.action == "source.deleted"))
    remaining = {key for key in blob_keys if storage.exists(key)}
    assert len(remaining) == 2  # Shared and mirror snapshots only.
    assert client.get(f"/api/v1/jobs/{private_job_id}").status_code == 404


def test_private_source_deletion_is_refused_for_non_owners_and_active_runs():
    storage = _storage()
    with TestingSessionLocal() as db:
        owner = User(email="o@example.test", password_hash="x")
        db.add(owner)
        db.commit()
        connector = _connector(db, "busy", owner_user_id=owner.id)
        with pytest.raises(SourcePolicyError):
            delete_private_source(
                db, storage, connector_id=connector.id, requested_by_user_id="someone-else"
            )
        run = SourceRun(connector_id=connector.id, status="running")
        db.add(run)
        db.flush()
        connector.active_run_id = run.id
        db.commit()
        with pytest.raises(SourceError):
            delete_private_source(
                db, storage, connector_id=connector.id, requested_by_user_id=owner.id
            )
        assert db.get(SourceConnector, connector.id) is not None


# --- operator role ----------------------------------------------------------------------


def test_operator_promotion_is_audited_and_enables_shared_ingestion(client, monkeypatch, capsys):
    user_id = _register(client, "Ops@Example.test")
    with TestingSessionLocal() as db:
        connector = _connector(db, "shared-ops")
        connector_id = connector.id
    path = f"/api/v1/connectors/{connector_id}/ingest"
    assert client.post(path, headers=csrf_headers(client)).status_code == 403

    monkeypatch.setattr(admin_cli, "SessionLocal", TestingSessionLocal)
    assert admin_cli.main(
        ["promote-operator", "--email", "ops@example.test", "--reason", "OPS-1 on-call"]
    ) == 0
    assert '"role": "operator"' in capsys.readouterr().out
    assert client.post(path, headers=csrf_headers(client)).status_code == 202

    with TestingSessionLocal() as db:
        event = db.scalar(select(AuditEvent).where(AuditEvent.action == "user.role_changed"))
        assert event is not None and event.user_id == user_id
        assert event.metadata_json == {
            "from": "candidate",
            "to": "operator",
            "reason": "OPS-1 on-call",
        }
        assert event.actor.startswith("cli:")

    # The last operator cannot be removed; unknown accounts and roles are rejected.
    assert admin_cli.main(
        ["demote-operator", "--email", "ops@example.test", "--reason", "leaving"]
    ) == 1
    with TestingSessionLocal() as db:
        with pytest.raises(RoleChangeError):
            set_user_role(db, email="nobody@example.test", role="operator", actor="t", reason="r")
        with pytest.raises(RoleChangeError):
            set_user_role(db, email="ops@example.test", role="admin", actor="t", reason="r")
        with pytest.raises(RoleChangeError):
            set_user_role(db, email="ops@example.test", role="candidate", actor="t", reason=" ")


def test_no_http_endpoint_changes_roles(client):
    _register(client, "self-escalate@example.test")
    routes = {getattr(route, "path", "") for route in app.routes}
    assert not any("role" in path or "operator" in path for path in routes)
    me = client.get("/api/v1/auth/me").json()
    assert me["role"] == "candidate"
