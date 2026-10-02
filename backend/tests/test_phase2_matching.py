from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import JobPosting
from app.services.catalog import ingest_fixture_jobs
from app.services.storage import ArtifactStorage
from tests.conftest import TestingSessionLocal, app, csrf_headers


def register(client: TestClient, email: str = "phase2@example.test"):
    response = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "phase2-secure-password-123"},
    )
    assert response.status_code == 201


def seed_job(*, remote: bool = False) -> str:
    record = {
        "source_job_id": "phase2-remote" if remote else "phase2-onsite",
        "title": "Example Platform Engineer" if remote else "Example Data Analyst",
        "employer": "Sanitized Example Employer",
        "canonical_url": "https://example.invalid/jobs/phase2",
        "location_text": "Remote - geography unspecified" if remote else "Vadodara, India",
        "work_mode": "remote" if remote else "onsite",
        "description": (
            "Sanitized fixture. Python is required and Kubernetes is preferred."
            if remote
            else "Sanitized fixture. SQL is required and Python is preferred."
        ),
        "location_country": None if remote else "IN",
        "permitted_countries": [] if remote else ["IN"],
        "sponsorship": "unknown",
        "requirements": (
            [
                {
                    "skill": "Python",
                    "kind": "required",
                    "evidence": "Python is required",
                },
                {
                    "skill": "Kubernetes",
                    "kind": "preferred",
                    "evidence": "Kubernetes is preferred",
                },
            ]
            if remote
            else [
                {"skill": "SQL", "kind": "required", "evidence": "SQL is required"},
                {"skill": "Python", "kind": "preferred", "evidence": "Python is preferred"},
            ]
        ),
    }
    db = TestingSessionLocal()
    ingest_fixture_jobs(db, ArtifactStorage(get_settings()), [record])
    db.commit()
    job = db.scalar(select(JobPosting).where(JobPosting.source_job_id == record["source_job_id"]))
    db.close()
    assert job is not None
    return job.id


def create_profile(client: TestClient, *, authorization: bool, skill: str = "SQL"):
    facts = [
        {
            "fact_type": "skill",
            "value": {"name": skill},
            "provenance": "candidate",
            "confirmation_status": "confirmed",
            "evidence_text": f"Candidate supplied {skill}.",
        }
    ]
    if authorization:
        facts.append(
            {
                "fact_type": "work_authorization",
                "value": {"countries": ["IN"], "status": "authorized"},
                "provenance": "candidate",
                "confirmation_status": "confirmed",
                "evidence_text": "Candidate explicitly confirmed authorization for IN.",
            }
        )
    response = client.post(
        "/api/v1/profile/versions",
        headers=csrf_headers(client),
        json={"summary": "Candidate supplied summary", "facts": facts},
    )
    assert response.status_code == 201


def test_analysis_persists_requirements_and_bounded_evidence(client):
    register(client)
    job_id = seed_job()
    response = client.post(f"/api/v1/jobs/{job_id}/analysis", headers=csrf_headers(client))
    assert response.status_code == 200
    analysis = response.json()
    assert analysis["status"] == "validated"
    assert {item["skill_text"] for item in analysis["requirements"]} == {"SQL", "Python"}
    assert all(item["evidence"]["start_offset"] is not None for item in analysis["requirements"])
    replay = client.get(f"/api/v1/jobs/{job_id}/analysis")
    assert replay.status_code == 200
    assert replay.json()["id"] == analysis["id"]


def test_unknown_authorization_never_becomes_eligible(client):
    register(client)
    job_id = seed_job()
    create_profile(client, authorization=False)
    response = client.post(f"/api/v1/jobs/{job_id}/match", headers=csrf_headers(client))
    assert response.status_code == 201
    result = response.json()
    assert result["eligibility"] == "needs_review"
    assert any(
        reason["code"] == "authorization_unconfirmed"
        for reason in result["eligibility_reasons"]
    )
    assert client.get(f"/api/v1/jobs/{job_id}/match").json()["eligibility"] == "needs_review"


def test_confirmed_facts_drive_fit_and_preferences_are_separate(client):
    register(client)
    job_id = seed_job()
    create_profile(client, authorization=True)
    headers = csrf_headers(client)
    first_preferences = client.put(
        "/api/v1/profile/preferences",
        headers=headers,
        json={
            "target_roles": ["Unrelated Role"],
            "work_modes": ["onsite"],
            "location_preferences": ["Vadodara"],
            "compensation": {"minimum": 100000, "currency": "INR"},
        },
    )
    assert first_preferences.status_code == 200
    first = client.post(f"/api/v1/jobs/{job_id}/match", headers=headers).json()
    assert first["eligibility"] == "eligible"
    assert first["fit"]["score"] == 70.0
    assert any(reason["code"] == "required_skill_evidenced" for reason in first["fit"]["reasons"])
    assert any(reason["code"] == "preferred_skill_missing" for reason in first["fit"]["reasons"])

    second_preferences = client.put(
        "/api/v1/profile/preferences",
        headers=headers,
        json={
            "target_roles": ["Data Analyst"],
            "work_modes": ["onsite"],
            "location_preferences": ["Vadodara"],
            "compensation": {"minimum": 100000, "currency": "INR"},
        },
    )
    assert second_preferences.status_code == 200
    second = client.post(f"/api/v1/jobs/{job_id}/match", headers=headers).json()
    assert second["eligibility"] == first["eligibility"]
    assert second["fit"]["score"] == first["fit"]["score"]
    assert second["preference"]["score"] > first["preference"]["score"]
    assert any(
        reason["code"] == "compensation_unknown"
        for reason in second["preference"]["reasons"]
    )
    assert second["profile_version_id"] == first["profile_version_id"]
    assert second["preference_version_id"] != first["preference_version_id"]


def test_remote_geography_without_authorization_is_needs_review(client):
    register(client)
    job_id = seed_job(remote=True)
    create_profile(client, authorization=True, skill="Python")
    result = client.post(
        f"/api/v1/jobs/{job_id}/match", headers=csrf_headers(client)
    ).json()
    assert result["eligibility"] == "needs_review"
    assert any(
        reason["code"] == "authorization_geography_unknown"
        for reason in result["eligibility_reasons"]
    )


def test_match_is_tenant_isolated(client):
    register(client, "first-phase2@example.test")
    job_id = seed_job()
    create_profile(client, authorization=True)
    created = client.post(f"/api/v1/jobs/{job_id}/match", headers=csrf_headers(client))
    assert created.status_code == 201

    other = TestClient(app)
    register(other, "second-phase2@example.test")
    assert other.get(f"/api/v1/jobs/{job_id}/match").status_code == 404
