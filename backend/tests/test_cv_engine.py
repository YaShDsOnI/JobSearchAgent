from io import BytesIO
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import AuditEvent, CVVersion, ProfileFact, StoredArtifact
from app.services.catalog import ingest_fixture_jobs
from app.services.cv import (
    CVDocument,
    CVValidationError,
    DraftClaim,
    DraftSection,
    extract_docx_text,
    extract_pdf_text,
    render_docx,
    render_pdf,
    validate_claims,
)
from app.services.storage import ArtifactStorage
from tests.conftest import TestingSessionLocal, app, csrf_headers
from tests.test_phase2_matching import register, seed_job


def _create_cv_profile(client: TestClient) -> None:
    response = client.post(
        "/api/v1/profile/versions",
        headers=csrf_headers(client),
        json={
            "summary": "Summary text is stored separately from verified CV claims.",
            "facts": [
                {
                    "fact_type": "summary",
                    "value": {"text": "Data analyst focused on SQL analysis"},
                    "provenance": "candidate",
                    "confirmation_status": "confirmed",
                },
                {
                    "fact_type": "skill",
                    "value": {"name": "SQL"},
                    "provenance": "candidate",
                    "confirmation_status": "confirmed",
                },
                {
                    "fact_type": "experience",
                    "value": {
                        "title": "Analyst",
                        "employer": "Example Co",
                        "start_date": "2022",
                        "end_date": "2024",
                        "skill": "SQL",
                        "years": 3,
                        "responsibilities": ["Built SQL reports"],
                        "metrics": ["20 reports"],
                    },
                    "provenance": "candidate",
                    "confirmation_status": "confirmed",
                },
                {
                    "fact_type": "education",
                    "value": {"degree": "BSc", "institution": "Example University"},
                    "provenance": "candidate",
                    "confirmation_status": "confirmed",
                },
                {
                    "fact_type": "work_authorization",
                    "value": {"countries": ["IN"], "status": "authorized"},
                    "provenance": "candidate",
                    "confirmation_status": "confirmed",
                },
                {
                    "fact_type": "skill",
                    "value": {"name": "Python"},
                    "provenance": "candidate",
                    "confirmation_status": "unknown",
                },
            ],
        },
    )
    assert response.status_code == 201


def test_renderers_are_deterministic_and_text_selectable():
    document = CVDocument(
        title="Factual CV",
        target_title="Example Analyst",
        target_employer="Sanitized Employer",
        target_location="Vadodara",
        sections=(
            DraftSection(
                "Skills",
                (DraftClaim("Skills", "SQL", ("fact-1",)),),
            ),
        ),
    )
    pdf = render_pdf(document)
    docx = render_docx(document)
    assert pdf == render_pdf(document)
    assert docx == render_docx(document)
    assert pdf.startswith(b"%PDF-1.4")
    assert "SQL" in extract_pdf_text(pdf)
    assert "SQL" in extract_docx_text(docx)
    assert "�" not in extract_pdf_text(pdf)
    assert "�" not in extract_docx_text(docx)
    with ZipFile(BytesIO(docx)) as archive:
        assert "word/styles.xml" in archive.namelist()
        assert "word/numbering.xml" in archive.namelist()
        styles = archive.read("word/styles.xml")
        numbering = archive.read("word/numbering.xml")
        assert b'w:styleId="Heading1"' in styles
        assert b'w:styleId="ListBullet"' in styles
        assert b'w:numId w:val="1"' in styles
        assert b'w:numId="1"' in numbering


def test_unsupported_or_unconfirmed_claims_are_rejected():
    confirmed = ProfileFact(
        id="confirmed-python",
        fact_type="skill",
        value_json={"name": "Python"},
        confirmation_status="confirmed",
    )
    unconfirmed = ProfileFact(
        id="unconfirmed-sql",
        fact_type="skill",
        value_json={"name": "SQL"},
        confirmation_status="unknown",
    )
    with pytest.raises(CVValidationError):
        validate_claims(
            [DraftClaim("Skills", "Led a team of 20 Python engineers", (confirmed.id,))],
            [confirmed],
        )
    with pytest.raises(CVValidationError):
        validate_claims(
            [DraftClaim("Skills", "Python engineer", (confirmed.id,))],
            [confirmed],
        )
    with pytest.raises(CVValidationError):
        validate_claims(
            [DraftClaim("Skills", "SQL", (unconfirmed.id,))],
            [unconfirmed],
        )


def test_cv_generation_persists_provenance_artifacts_and_approval_workflow(client):
    register(client, "cv-owner@example.test")
    job_id = seed_job()
    _create_cv_profile(client)
    match = client.post(
        f"/api/v1/jobs/{job_id}/match", headers=csrf_headers(client)
    ).json()
    assert match["eligibility"] == "eligible"

    created = client.post(
        f"/api/v1/jobs/{job_id}/cv-drafts",
        headers=csrf_headers(client),
        json={"match_run_id": match["id"]},
    )
    assert created.status_code == 201, created.text
    version = created.json()
    assert version["status"] == "validated"
    assert version["pdf_artifact"] is not None
    assert version["docx_artifact"] is not None
    assert version["claims"]
    assert all(claim["validation_status"] == "accepted" for claim in version["claims"])
    assert all(claim["source_fact_ids"] for claim in version["claims"])
    assert any("20 reports" in claim["text"] for claim in version["claims"])
    assert not any("Python" in claim["text"] for claim in version["claims"])

    second_created = client.post(
        f"/api/v1/jobs/{job_id}/cv-drafts",
        headers=csrf_headers(client),
        json={"match_run_id": match["id"]},
    )
    assert second_created.status_code == 201
    second_version = second_created.json()
    missing_note = client.post(
        f"/api/v1/cv-versions/{second_version['id']}/reject",
        headers=csrf_headers(client),
        json={"note": ""},
    )
    assert missing_note.status_code == 422
    rejected = client.post(
        f"/api/v1/cv-versions/{second_version['id']}/reject",
        headers=csrf_headers(client),
        json={"note": "Candidate rejected this draft for revision."},
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"

    pdf = client.get(f"/api/v1/cv-versions/{version['id']}/artifact/pdf")
    docx = client.get(f"/api/v1/cv-versions/{version['id']}/artifact/docx")
    assert pdf.status_code == 200
    assert pdf.headers["content-type"] == "application/pdf"
    assert pdf.content.startswith(b"%PDF-1.4")
    assert docx.status_code == 200
    assert docx.content.startswith(b"PK")

    other = TestClient(app)
    register(other, "cv-other@example.test")
    assert other.get(f"/api/v1/cv-versions/{version['id']}").status_code == 404
    assert other.get(f"/api/v1/cv-versions/{version['id']}/artifact/pdf").status_code == 404

    approved = client.post(
        f"/api/v1/cv-versions/{version['id']}/approve", headers=csrf_headers(client)
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    cannot_approve_again = client.post(
        f"/api/v1/cv-versions/{version['id']}/approve", headers=csrf_headers(client)
    )
    assert cannot_approve_again.status_code == 400

    db = TestingSessionLocal()
    try:
        audit = db.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "cv.approved", AuditEvent.object_id == version["id"]
            )
        )
        assert audit is not None
    finally:
        db.close()

    _create_cv_profile(client)
    superseded = client.get(f"/api/v1/cv-versions/{version['id']}")
    assert superseded.status_code == 200
    assert superseded.json()["status"] == "superseded"


def test_cv_requires_eligible_match_and_rejection_requires_note(client):
    register(client, "cv-gates@example.test")
    job_id = seed_job(remote=True)
    _create_cv_profile(client)
    match = client.post(
        f"/api/v1/jobs/{job_id}/match", headers=csrf_headers(client)
    ).json()
    assert match["eligibility"] == "needs_review"
    blocked = client.post(
        f"/api/v1/jobs/{job_id}/cv-drafts",
        headers=csrf_headers(client),
        json={"match_run_id": match["id"]},
    )
    assert blocked.status_code == 400

    db = TestingSessionLocal()
    try:
        assert db.scalars(select(CVVersion)).all() == []
    finally:
        db.close()


def test_job_snapshot_change_supersedes_existing_cv(client):
    register(client, "cv-job-change@example.test")
    job_id = seed_job()
    _create_cv_profile(client)
    match = client.post(
        f"/api/v1/jobs/{job_id}/match", headers=csrf_headers(client)
    ).json()
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

    db = TestingSessionLocal()
    try:
        result = ingest_fixture_jobs(
            db,
            ArtifactStorage(get_settings()),
            [
                {
                    "source_job_id": "phase2-onsite",
                    "title": "Changed Example Data Analyst",
                    "employer": "Sanitized Example Employer",
                    "canonical_url": "https://example.invalid/jobs/phase2-changed",
                    "location_text": "Vadodara, India",
                    "work_mode": "onsite",
                    "description": "Sanitized fixture changed. SQL and Python are required.",
                    "location_country": "IN",
                    "permitted_countries": ["IN"],
                    "sponsorship": "not_available",
                    "requirements": [
                        {"skill": "SQL", "kind": "required", "evidence": "SQL is required"},
                        {"skill": "Python", "kind": "required", "evidence": "Python is required"},
                    ],
                }
            ],
        )
        db.commit()
        assert result["updated"] == 1
    finally:
        db.close()
    superseded = client.get(f"/api/v1/cv-versions/{version_id}")
    assert superseded.status_code == 200
    assert superseded.json()["status"] == "superseded"


def test_profile_deletion_removes_cv_rows_and_owned_artifacts(client):
    register(client, "cv-delete@example.test")
    job_id = seed_job()
    _create_cv_profile(client)
    match = client.post(
        f"/api/v1/jobs/{job_id}/match", headers=csrf_headers(client)
    ).json()
    created = client.post(
        f"/api/v1/jobs/{job_id}/cv-drafts",
        headers=csrf_headers(client),
        json={"match_run_id": match["id"]},
    )
    assert created.status_code == 201
    version_id = created.json()["id"]
    user_id = client.get("/api/v1/auth/me").json()["id"]
    deleted = client.delete("/api/v1/profile", headers=csrf_headers(client))
    assert deleted.status_code == 200
    assert client.get(f"/api/v1/cv-versions/{version_id}").status_code == 404

    db = TestingSessionLocal()
    try:
        assert db.scalars(select(CVVersion).where(CVVersion.user_id == user_id)).all() == []
        assert (
            db.scalars(select(StoredArtifact).where(StoredArtifact.owner_user_id == user_id)).all()
            == []
        )
    finally:
        db.close()
