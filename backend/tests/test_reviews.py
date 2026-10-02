from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import AuditEvent, MatchRun, ProfileFact
from tests.conftest import TestingSessionLocal, app, csrf_headers
from tests.test_phase2_matching import create_profile, register, seed_job


def test_review_shows_evidence_resolves_and_reruns_without_creating_facts(client):
    register(client, "review-owner@example.test")
    job_id = seed_job(remote=True)
    create_profile(client, authorization=True, skill="Python")
    match = client.post(
        f"/api/v1/jobs/{job_id}/match", headers=csrf_headers(client)
    ).json()
    assert match["eligibility"] == "needs_review"

    opened = client.post(
        f"/api/v1/jobs/{job_id}/match/{match['id']}/review",
        headers=csrf_headers(client),
    )
    assert opened.status_code == 201
    review = opened.json()
    assert review["status"] == "pending"
    assert any(reason["code"] == "authorization_geography_unknown" for reason in review["reasons"])
    assert review["job_evidence"]
    assert review["candidate_facts"]

    resolved = client.post(
        f"/api/v1/reviews/{review['id']}/resolve",
        headers=csrf_headers(client),
        json={
            "decision": "accept_remote_geography",
            "note": "Candidate reviewed the sanitized remote geography evidence.",
        },
    )
    assert resolved.status_code == 200
    resolved_payload = resolved.json()
    assert resolved_payload["status"] == "resolved"
    assert resolved_payload["rerun_match_id"]
    assert resolved_payload["resolved_by_user_id"] == client.get("/api/v1/auth/me").json()["id"]

    db = TestingSessionLocal()
    try:
        rerun = db.get(MatchRun, resolved_payload["rerun_match_id"])
        assert rerun is not None
        assert rerun.eligibility_status == "eligible"
        assert rerun.scores_json["review"]["review_id"] == review["id"]
        facts = db.scalars(select(ProfileFact)).all()
        assert len(facts) == 2
        audit = db.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "match.review_resolved",
                AuditEvent.object_id == review["id"],
            )
        )
        assert audit is not None
    finally:
        db.close()


def test_review_authorization_resolution_does_not_create_verified_fact(client):
    register(client, "review-auth@example.test")
    job_id = seed_job()
    create_profile(client, authorization=False)
    match = client.post(
        f"/api/v1/jobs/{job_id}/match", headers=csrf_headers(client)
    ).json()
    assert match["eligibility"] == "needs_review"
    review = client.post(
        f"/api/v1/jobs/{job_id}/match/{match['id']}/review",
        headers=csrf_headers(client),
    ).json()

    resolved = client.post(
        f"/api/v1/reviews/{review['id']}/resolve",
        headers=csrf_headers(client),
        json={
            "decision": "authorize_for_job_geography",
            "note": "User explicitly resolved the review for this fixture job.",
        },
    )
    assert resolved.status_code == 200
    db = TestingSessionLocal()
    try:
        rerun = db.get(MatchRun, resolved.json()["rerun_match_id"])
        assert rerun is not None
        assert rerun.eligibility_status == "eligible"
        assert not db.scalars(
            select(ProfileFact).where(ProfileFact.fact_type == "work_authorization")
        ).all()
    finally:
        db.close()


def test_review_is_tenant_isolated_and_only_needs_review_matches_are_reviewable(client):
    register(client, "review-isolation-owner@example.test")
    job_id = seed_job(remote=True)
    create_profile(client, authorization=True, skill="Python")
    match = client.post(
        f"/api/v1/jobs/{job_id}/match", headers=csrf_headers(client)
    ).json()
    review = client.post(
        f"/api/v1/jobs/{job_id}/match/{match['id']}/review",
        headers=csrf_headers(client),
    ).json()

    other = TestClient(app)
    register(other, "review-isolation-other@example.test")
    assert other.get(f"/api/v1/reviews/{review['id']}").status_code == 404
    assert other.get("/api/v1/reviews").json() == []

    onsite_job_id = seed_job()
    create_profile(client, authorization=True)
    eligible_match = client.post(
        f"/api/v1/jobs/{onsite_job_id}/match", headers=csrf_headers(client)
    ).json()
    assert eligible_match["eligibility"] == "eligible"
    cannot_review = client.post(
        f"/api/v1/jobs/{onsite_job_id}/match/{eligible_match['id']}/review",
        headers=csrf_headers(client),
    )
    assert cannot_review.status_code == 400


def test_preference_score_cannot_override_unknown_eligibility(client):
    register(client, "review-preference-gate@example.test")
    job_id = seed_job()
    create_profile(client, authorization=False)
    preference = client.put(
        "/api/v1/profile/preferences",
        headers=csrf_headers(client),
        json={
            "target_roles": ["Data Analyst"],
            "work_modes": ["onsite"],
            "location_preferences": ["Vadodara"],
        },
    )
    assert preference.status_code == 200
    result = client.post(
        f"/api/v1/jobs/{job_id}/match", headers=csrf_headers(client)
    ).json()
    assert result["eligibility"] == "needs_review"
    assert result["preference"]["score"] == 100.0
