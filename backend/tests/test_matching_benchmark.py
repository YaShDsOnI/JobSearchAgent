import json
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import JobPosting
from app.services.catalog import ingest_fixture_jobs
from app.services.storage import ArtifactStorage
from tests.conftest import TestingSessionLocal, app, csrf_headers

BENCHMARK_PATH = Path(__file__).parents[1] / "benchmarks" / "matching_v1.json"


def _register(client: TestClient, email: str) -> None:
    response = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "benchmark-secure-password-123"},
    )
    assert response.status_code == 201


def _seed_fixture_jobs() -> dict[str, str]:
    records = json.loads(
        (Path(__file__).parents[1] / "app" / "fixtures" / "jobs.json").read_text(
            encoding="utf-8"
        )
    )
    db = TestingSessionLocal()
    try:
        ingest_fixture_jobs(db, ArtifactStorage(get_settings()), records)
        db.commit()
        jobs = db.scalars(select(JobPosting)).all()
        return {job.source_job_id: job.id for job in jobs}
    finally:
        db.close()


def _create_profile(client: TestClient, case: dict) -> None:
    facts = [
        {
            "fact_type": fact["fact_type"],
            "value": fact["value"],
            "provenance": "candidate",
            "confirmation_status": fact["confirmation_status"],
            "evidence_text": "Sanitized benchmark fact supplied by the test case.",
        }
        for fact in case["facts"]
    ]
    response = client.post(
        "/api/v1/profile/versions",
        headers=csrf_headers(client),
        json={"summary": "Sanitized benchmark profile", "facts": facts},
    )
    assert response.status_code == 201
    preferences = case.get("preferences", {})
    if preferences:
        response = client.put(
            "/api/v1/profile/preferences",
            headers=csrf_headers(client),
            json=preferences,
        )
        assert response.status_code == 200


def test_versioned_matching_benchmark_has_deterministic_expected_outcomes(client):
    benchmark = json.loads(BENCHMARK_PATH.read_text(encoding="utf-8"))
    assert benchmark["benchmark_version"] == "matching-benchmark-v1"
    job_ids = _seed_fixture_jobs()

    for index, case in enumerate(benchmark["cases"]):
        case_client = TestClient(app)
        _register(case_client, f"benchmark-{index}@example.test")
        _create_profile(case_client, case)
        result = case_client.post(
            f"/api/v1/jobs/{job_ids[case['job_source_id']]}/match",
            headers=csrf_headers(case_client),
        )
        assert result.status_code == 201, case["id"]
        payload = result.json()
        expected = case["expected"]
        assert payload["eligibility"] == expected["eligibility"], case["id"]
        if "fit_score" in expected:
            assert payload["fit"]["score"] == expected["fit_score"], case["id"]
        for code in expected.get("eligibility_reason_codes", []):
            assert code in {reason["code"] for reason in payload["eligibility_reasons"]}, case[
                "id"
            ]
        for code in expected.get("fit_reason_codes", []):
            assert code in {reason["code"] for reason in payload["fit"]["reasons"]}, case[
                "id"
            ]
        for code in expected.get("preference_reason_codes", []):
            assert code in {reason["code"] for reason in payload["preference"]["reasons"]}, case[
                "id"
            ]
