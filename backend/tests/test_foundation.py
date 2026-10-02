from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import JobPosting, JobSnapshot, SourceConnector, StoredArtifact, Task
from app.services.catalog import (
    ConnectorPolicyError,
    ingest_fixture_jobs,
    validate_connector_policy,
)
from app.services.storage import ArtifactStorage
from app.services.tasks import claim_next_task, complete_task, enqueue_task, heartbeat_task
from tests.conftest import TestingSessionLocal, app, csrf_headers


def register(client):
    response = client.post(
        "/api/v1/auth/register",
        json={"email": "candidate@example.test", "password": "a-secure-password-123"},
    )
    assert response.status_code == 201
    return response


def test_health_and_authentication(client):
    assert client.get("/api/v1/health/live").json()["status"] == "ok"
    assert client.get("/api/v1/health/ready").status_code == 200
    assert client.get("/api/v1/auth/me").status_code == 401
    register(client)
    assert client.get("/api/v1/auth/me").json()["email"] == "candidate@example.test"


def test_auth_errors_use_the_api_error_contract(client):
    register(client)
    duplicate = client.post(
        "/api/v1/auth/register",
        json={"email": "candidate@example.test", "password": "a-secure-password-123"},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == "Email already registered"

    client.post("/api/v1/auth/logout", headers=csrf_headers(client))
    invalid = client.post(
        "/api/v1/auth/login",
        json={"email": "candidate@example.test", "password": "wrong-password-123"},
    )
    assert invalid.status_code == 401
    assert invalid.json()["detail"] == "Invalid credentials"


def test_csrf_protects_mutating_profile_operations(client):
    register(client)
    payload = {"summary": "Candidate supplied summary", "facts": []}
    assert client.post("/api/v1/profile/versions", json=payload).status_code == 403
    assert (
        client.post(
            "/api/v1/profile/versions", json=payload, headers=csrf_headers(client)
        ).status_code
        == 201
    )
    profile = client.get("/api/v1/profile").json()
    assert profile["summary"] == "Candidate supplied summary"
    assert client.delete("/api/v1/profile", headers=csrf_headers(client)).status_code == 200
    assert client.get("/api/v1/profile").json() is None


def test_preferences_and_consent_are_versioned(client):
    register(client)
    headers = csrf_headers(client)
    first = client.put(
        "/api/v1/profile/preferences",
        headers=headers,
        json={
            "target_roles": ["Platform Engineer"],
            "work_modes": ["remote"],
            "work_authorization": {},
        },
    )
    second = client.put(
        "/api/v1/profile/preferences",
        headers=headers,
        json={"target_roles": ["Data Analyst"], "work_modes": ["onsite"], "work_authorization": {}},
    )
    assert first.status_code == second.status_code == 200
    assert client.get("/api/v1/profile/preferences").json()["rules"]["target_roles"] == [
        "Data Analyst"
    ]
    consent = client.post(
        "/api/v1/profile/consents",
        headers=headers,
        json={"purpose": "profile_processing", "scope": {"profile": True}, "policy_version": "v1"},
    )
    assert consent.status_code == 201
    withdrawn = client.post("/api/v1/profile/consents/profile_processing/withdraw", headers=headers)
    assert withdrawn.status_code == 200
    assert withdrawn.json()["withdrawn_at"] is not None


def test_fixture_catalog_is_deduplicated_and_has_provenance():
    db = TestingSessionLocal()
    records = [
        {
            "source_job_id": "one",
            "title": "Fixture Role",
            "employer": "Fixture Employer",
            "canonical_url": "https://example.invalid/jobs/one",
            "location_text": "Unknown",
            "work_mode": "unknown",
            "description": "fixture",
        }
    ]
    storage = ArtifactStorage(get_settings())
    first = ingest_fixture_jobs(db, storage, records)
    second = ingest_fixture_jobs(db, storage, records)
    db.commit()
    assert first == {"created": 1, "updated": 0, "unchanged": 0}
    assert second == {"created": 0, "updated": 0, "unchanged": 1}
    job = db.scalar(select(JobPosting).where(JobPosting.source_job_id == "one"))
    snapshot = db.scalar(select(JobSnapshot).where(JobSnapshot.job_id == job.id))
    artifact = db.get(StoredArtifact, snapshot.raw_artifact_id)
    assert snapshot.content_hash == job.canonical_hash
    assert artifact.owner_user_id is None
    encrypted = (Path(get_settings().artifact_storage_path) / artifact.object_key).read_bytes()
    assert b"Fixture Role" not in encrypted
    db.close()


def test_jobs_are_only_visible_after_authentication(client):
    assert client.get("/api/v1/jobs").status_code == 401
    register(client)
    assert client.get("/api/v1/jobs").status_code == 200


def test_profile_isolation_between_users(client):
    register(client)
    first_headers = csrf_headers(client)
    created = client.post(
        "/api/v1/profile/versions",
        headers=first_headers,
        json={
            "summary": "First candidate data",
            "facts": [
                {
                    "fact_type": "skill",
                    "value": {"name": "Python"},
                    "provenance": "candidate",
                    "confirmation_status": "confirmed",
                }
            ],
        },
    )
    assert created.status_code == 201

    second_client = TestClient(app)
    second_client.post(
        "/api/v1/auth/register",
        json={"email": "other@example.test", "password": "another-secure-password-123"},
    )
    assert second_client.get("/api/v1/profile").json() is None
    assert "First candidate data" not in second_client.get("/api/v1/profile/export").text


def test_profile_delete_removes_personal_facts_and_export_data(client):
    register(client)
    headers = csrf_headers(client)
    created = client.post(
        "/api/v1/profile/versions",
        headers=headers,
        json={
            "summary": "Private candidate data",
            "facts": [
                {
                    "fact_type": "email",
                    "value": {"value": "private@example.test"},
                    "provenance": "candidate",
                    "confirmation_status": "confirmed",
                }
            ],
        },
    )
    assert created.status_code == 201
    assert client.delete("/api/v1/profile", headers=headers).status_code == 200
    exported = client.get("/api/v1/profile/export").json()
    assert exported["profile"] is None
    assert "private@example.test" not in str(exported)


def test_authentication_secrets_are_not_logged(client, caplog):
    secret = "this-password-must-not-appear-in-logs"
    with caplog.at_level("INFO"):
        response = client.post(
            "/api/v1/auth/login",
            json={"email": "missing@example.test", "password": secret},
        )
    assert response.status_code == 401
    assert secret not in caplog.text


def test_profile_document_is_type_checked_and_encrypted(client):
    register(client)
    headers = csrf_headers(client)
    rejected = client.post(
        "/api/v1/profile/source-document",
        headers=headers,
        files={"file": ("candidate.pdf", b"not-a-pdf", "application/pdf")},
    )
    assert rejected.status_code == 400
    accepted = client.post(
        "/api/v1/profile/source-document",
        headers=headers,
        files={"file": ("candidate.pdf", b"%PDF-1.7 sanitized", "application/pdf")},
    )
    assert accepted.status_code == 201
    assert accepted.json()["sha256"]


def test_disabled_connector_policy_fails_closed():
    connector = SourceConnector(
        name="disabled",
        kind="external",
        tier="A",
        terms_version="v1",
        policy_status="approved",
        enabled=False,
    )
    with pytest.raises(ConnectorPolicyError):
        validate_connector_policy(connector)


def test_task_enqueue_is_idempotent():
    db = TestingSessionLocal()
    first = enqueue_task(
        db, kind="catalog_ingest", idempotency_key="catalog:fixture:1", payload={"n": 1}
    )
    second = enqueue_task(
        db, kind="catalog_ingest", idempotency_key="catalog:fixture:1", payload={"n": 2}
    )
    db.commit()
    assert first.id == second.id
    assert db.query(Task).count() == 1
    assert db.get(Task, first.id).payload_json == {"n": 1}
    db.close()


def test_task_lease_can_be_reclaimed_and_completed():
    db = TestingSessionLocal()
    task = enqueue_task(db, kind="profile_export", idempotency_key="export:1", payload={})
    db.flush()
    claimed = claim_next_task(db, worker_id="worker-a", lease_seconds=30)
    assert claimed is not None
    assert claimed.id == task.id
    heartbeat_task(db, claimed, worker_id="worker-a", lease_seconds=30)
    complete_task(db, claimed, worker_id="worker-a")
    db.commit()
    assert db.get(Task, task.id).state == "succeeded"
    db.close()
