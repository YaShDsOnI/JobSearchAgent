"""Agent loop: text requirement extraction, the location ladder (policy deterministic-v2),
automatic re-matching, recommendations, and next actions."""

from typing import Any

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import (
    AuditEvent,
    Consent,
    JobAnalysis,
    JobPosting,
    MatchRun,
    SourceConnector,
    Task,
)
from app.services import agent as agent_service
from app.services.agent import (
    AGENT_TASK_KIND,
    AUTO_MATCH_PURPOSE,
    enqueue_for_catalog_change,
    rank_score,
    refresh_matches,
    run_next_agent_task,
)
from app.services.catalog import ingest_fixture_jobs
from app.services.jd_text import extract_text_requirements
from app.services.matching import POLICY_VERSION, match_job_for_user
from app.services.sources import worker as source_worker
from app.services.sources.pipeline import IngestionResult
from app.services.storage import ArtifactStorage
from tests.conftest import TestingSessionLocal, csrf_headers
from tests.test_phase2_matching import register

DESCRIPTION = """About the team
We build data products with Kafka and Snowflake.

Requirements:
- 4+ years of experience with Python.
- Strong SQL skills and PostgreSQL.

Nice to have
- Exposure to Kubernetes.
- Terraform is a plus.

Benefits
- Learning budget for AWS certifications.
"""


def _job(**overrides: Any) -> str:
    record: dict[str, Any] = {
        "source_job_id": overrides.pop("source_job_id", "agent-job"),
        "title": "Senior Data Engineer",
        "employer": "Sanitized Example Employer",
        "canonical_url": "https://example.invalid/jobs/agent",
        "location_text": "Vadodara, India",
        "work_mode": "onsite",
        "description": DESCRIPTION,
        "location_country": "IN",
        "permitted_countries": ["IN"],
        "sponsorship": "unknown",
        "requirements": [],
    }
    record.update(overrides)
    with TestingSessionLocal() as db:
        ingest_fixture_jobs(db, ArtifactStorage(get_settings()), [record])
        db.commit()
        return db.scalar(
            select(JobPosting.id).where(JobPosting.source_job_id == record["source_job_id"])
        )


def _profile(client, *, skills=("Python", "SQL"), authorization: bool = True) -> None:
    facts = [
        {"fact_type": "skill", "value": {"name": name}, "provenance": "candidate",
         "confirmation_status": "confirmed"}
        for name in skills
    ]
    facts.append({"fact_type": "experience", "value": {"skill": "Python", "years": 5},
                  "provenance": "candidate", "confirmation_status": "confirmed"})
    if authorization:
        facts.append({"fact_type": "work_authorization",
                      "value": {"countries": ["IN"], "status": "authorized"},
                      "provenance": "candidate", "confirmation_status": "confirmed"})
    response = client.post("/api/v1/profile/versions", headers=csrf_headers(client),
                           json={"summary": "Sanitized summary", "facts": facts})
    assert response.status_code == 201, response.text


def _preferences(client, **rules: Any) -> None:
    response = client.put("/api/v1/profile/preferences", headers=csrf_headers(client), json=rules)
    assert response.status_code == 200, response.text


def _user_id(client) -> str:
    return client.get("/api/v1/auth/me").json()["id"]


def _match(client, job_id: str) -> dict[str, Any]:
    response = client.post(f"/api/v1/jobs/{job_id}/match", headers=csrf_headers(client))
    assert response.status_code == 201, response.text
    return response.json()


def _drain(worker_id: str = "test-worker") -> int:
    processed = 0
    with TestingSessionLocal() as db:
        while run_next_agent_task(db, worker_id=worker_id) is not None:
            processed += 1
    return processed


LADDER = {"home_country": "IN", "preferred_cities": ["Vadodara", "Ahmedabad"],
          "open_to_relocation": False}


# --- text extraction ---------------------------------------------------------------------


def test_text_extractor_reads_sections_cues_and_years():
    found = {item.skill: item for item in extract_text_requirements(DESCRIPTION)}
    assert found["Python"].kind == "required" and found["Python"].minimum_years == 4.0
    assert found["SQL"].kind == "required" and found["PostgreSQL"].kind == "required"
    assert found["Kubernetes"].kind == "preferred" and found["Terraform"].kind == "preferred"
    # Company background and benefits never become requirements.
    assert found["Apache Kafka"].kind == "informational"
    assert found["AWS"].kind == "informational"
    for item in found.values():
        assert item.evidence in DESCRIPTION and item.confidence < 1


def test_text_extractor_without_headings_uses_sentence_wording():
    text = "You must have Django experience. Familiarity with Redis. We sometimes use Figma."
    found = {item.skill: item.kind for item in extract_text_requirements(text)}
    assert found == {"Django": "required", "Redis": "preferred", "Figma": "informational"}


def test_years_are_not_spread_across_many_skills():
    text = "Requirements:\n- 6+ years building services in Python, Java, Go and Rust."
    found = extract_text_requirements(text)
    assert {item.skill for item in found} == {"Python", "Java", "Go", "Rust"}
    assert all(item.minimum_years is None for item in found)


def test_analysis_uses_text_when_no_structured_requirements(client):
    register(client, "text-analysis@example.test")
    job_id = _job()
    analysis = client.post(f"/api/v1/jobs/{job_id}/analysis", headers=csrf_headers(client)).json()
    assert analysis["status"] == "validated"
    kinds = {item["skill_text"]: item["requirement_kind"] for item in analysis["requirements"]}
    assert kinds["Python"] == "required" and kinds["Kubernetes"] == "preferred"
    assert all(item["evidence"]["start_offset"] is not None for item in analysis["requirements"])
    with TestingSessionLocal() as db:
        stored = db.scalars(select(JobAnalysis)).one()
        assert stored.structured_json["requirements_source"] == "text"
        # Geography still comes only from structured fields.
        assert stored.structured_json["permitted_countries"] == ["IN"]


def test_fit_is_unknown_not_full_when_no_requirement_is_found(client):
    register(client, "unknown-fit@example.test")
    _profile(client)
    job_id = _job(description="A sanitized role description with no named tools.")
    match = _match(client, job_id)
    assert match["fit"]["score"] == 50.0
    codes = {reason["code"] for reason in match["fit"]["reasons"]}
    assert "requirements_unknown" in codes
    assert match["fit"]["details"]["requirements_known"] is False


def test_text_requirements_score_fit_from_confirmed_facts(client):
    register(client, "text-fit@example.test")
    _profile(client, skills=("Python", "SQL", "PostgreSQL"))
    match = _match(client, _job())
    details = match["fit"]["details"]
    assert details["requirements_known"] is True and details["requirements_source"] == "text"
    assert details["required_matched"] == details["required_total"] == 3
    assert match["policy_version"] == POLICY_VERSION == "deterministic-v2"


# --- location ladder ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("job", "category", "score"),
    [
        ({"work_mode": "remote", "location_country": "US", "permitted_countries": ["US", "IN"]},
         "global_remote", 100.0),
        ({"work_mode": "remote", "location_country": "GB", "permitted_countries": ["IN"]},
         "remote_from_home", 80.0),
        ({"work_mode": "remote", "location_country": "IN", "permitted_countries": ["IN"]},
         "local_remote", 60.0),
        ({"work_mode": "hybrid", "location_text": "Ahmedabad, India"}, "hybrid_home", 30.0),
        ({"work_mode": "onsite", "location_text": "Vadodara, India"}, "onsite_home", 20.0),
        ({"work_mode": "onsite", "location_text": "Pune, India"}, "onsite_home", 10.0),
        ({"work_mode": "onsite", "location_text": "Berlin", "location_country": "DE",
          "permitted_countries": ["DE"]}, "abroad", 0.0),
    ],
)
def test_location_ladder_orders_remote_hybrid_onsite_and_cities(client, job, category, score):
    register(client, f"ladder-{category}-{score}@example.test")
    _profile(client)
    _preferences(client, location_policy=LADDER)
    match = _match(client, _job(**job))
    details = match["preference"]["details"]
    assert details["location_category"] == category
    assert details["location_policy"] == score


def test_ladder_never_changes_eligibility(client):
    register(client, "ladder-eligibility@example.test")
    _profile(client, authorization=False)
    job_id = _job(work_mode="remote", location_country="US", permitted_countries=["US", "IN"])
    before = _match(client, job_id)
    _preferences(client, location_policy=LADDER)
    after = _match(client, job_id)
    assert before["eligibility"] == after["eligibility"] == "needs_review"
    assert before["fit"]["score"] == after["fit"]["score"]


def test_excluded_category_and_unknown_geography(client):
    register(client, "ladder-excluded@example.test")
    _profile(client)
    _preferences(client, location_policy={**LADDER, "priorities": ["global_remote"]})
    onsite = _match(client, _job(source_job_id="onsite"))
    codes = {item["code"] for item in onsite["preference"]["reasons"]}
    assert "location_category_excluded" in codes
    unknown = _match(client, _job(source_job_id="remote-unknown", work_mode="remote",
                                  location_country=None, permitted_countries=[]))
    assert unknown["preference"]["details"]["location_policy"] == 50.0
    assert unknown["eligibility"] == "needs_review"


def test_title_tokens_and_seniority(client):
    register(client, "title-tokens@example.test")
    _profile(client)
    _preferences(client, target_roles=["Data Engineer"], seniority=["senior"],
                 location_policy=LADDER)
    match = _match(client, _job(title="Sr. Engineer, Data Platform"))
    codes = {item["code"] for item in match["preference"]["reasons"]}
    assert {"target_role_match", "seniority_match"} <= codes


def test_preferences_reject_repeated_ladder_categories(client):
    register(client, "ladder-invalid@example.test")
    response = client.put("/api/v1/profile/preferences", headers=csrf_headers(client),
                          json={"location_policy": {"home_country": "IN",
                                                    "priorities": ["onsite_home", "onsite_home"]}})
    assert response.status_code == 422


# --- agent loop --------------------------------------------------------------------------


def test_agent_is_off_until_enabled_and_then_matches_in_the_background(client):
    register(client, "agent-loop@example.test")
    _profile(client)
    _job(source_job_id="loop-1")
    _job(source_job_id="loop-2", title="Example Product Designer",
         description="Requirements:\n- Figma required.")
    with TestingSessionLocal() as db:
        assert not db.scalars(select(Task).where(Task.kind == AGENT_TASK_KIND)).all()

    status = client.post("/api/v1/agent/enable", headers=csrf_headers(client)).json()
    assert status["auto_matching"] is True and status["queued_refreshes"] == 1
    assert _drain() == 1
    with TestingSessionLocal() as db:
        assert len(db.scalars(select(MatchRun)).all()) == 2
        event = db.scalars(
            select(AuditEvent).where(AuditEvent.action == "agent.matches_refreshed")
        ).one()
        assert event.metadata_json["matched"] == 2

    items = client.get("/api/v1/recommendations").json()
    assert [item["bucket"] for item in items] == ["ready", "ready"]
    assert items[0]["rank"] >= items[1]["rank"]
    assert all(item["current"] for item in items)
    top = items[0]
    assert top["rank"] == rank_score(top["match"]["fit_score"], top["match"]["preference_score"])

    # The same inputs queue nothing new; a profile change queues one refresh.
    client.post("/api/v1/agent/enable", headers=csrf_headers(client))
    assert _drain() == 0
    _profile(client, skills=("Python", "SQL", "Figma"))
    assert all(not item["current"] for item in client.get("/api/v1/recommendations").json())
    assert _drain() == 1
    assert all(item["current"] for item in client.get("/api/v1/recommendations").json())


def test_disabling_the_agent_stops_queued_work(client):
    register(client, "agent-off@example.test")
    _profile(client)
    _job()
    client.post("/api/v1/agent/enable", headers=csrf_headers(client))
    status = client.post("/api/v1/agent/disable", headers=csrf_headers(client)).json()
    assert status["auto_matching"] is False
    assert _drain() == 1  # The queued task runs but does nothing.
    with TestingSessionLocal() as db:
        assert not db.scalars(select(MatchRun)).all()
        consent = db.scalars(select(Consent).where(Consent.purpose == AUTO_MATCH_PURPOSE)).one()
        assert consent.withdrawn_at is not None
    _preferences(client, target_roles=["Data Engineer"])
    with TestingSessionLocal() as db:
        assert db.scalars(select(Task).where(Task.kind == AGENT_TASK_KIND)).all()[-1].state \
            == "succeeded"


def test_manual_refresh_works_without_the_agent_and_is_idempotent(client):
    register(client, "agent-manual@example.test")
    assert client.post("/api/v1/agent/refresh", headers=csrf_headers(client)).status_code == 400
    _profile(client)
    _job()
    first = client.post("/api/v1/agent/refresh", headers=csrf_headers(client)).json()
    assert first["matched"] == 1
    second = client.post("/api/v1/agent/refresh", headers=csrf_headers(client)).json()
    assert (second["matched"], second["already_current"]) == (0, 1)


def test_recommendations_bucket_and_hide_ineligible_by_default(client):
    register(client, "agent-buckets@example.test")
    _profile(client)
    _job(source_job_id="ok")
    _job(source_job_id="review", work_mode="remote", location_country=None,
         permitted_countries=[])
    _job(source_job_id="closed-geo", location_country="DE", permitted_countries=["DE"],
         location_text="Berlin")
    client.post("/api/v1/agent/refresh", headers=csrf_headers(client))
    buckets = [item["bucket"] for item in client.get("/api/v1/recommendations").json()]
    assert buckets == ["ready", "review"]
    every = client.get("/api/v1/recommendations?include_ineligible=true").json()
    assert [item["bucket"] for item in every] == ["ready", "review", "ineligible"]
    assert every[-1]["rank"] is None
    assert [i["bucket"] for i in client.get("/api/v1/recommendations?bucket=review").json()] \
        == ["review"]


def test_resolved_review_is_carried_forward_only_for_the_same_inputs(client):
    register(client, "agent-review@example.test")
    _profile(client)
    job_id = _job(work_mode="remote", location_country=None, permitted_countries=[])
    match = _match(client, job_id)
    review = client.post(f"/api/v1/jobs/{job_id}/match/{match['id']}/review",
                         headers=csrf_headers(client))
    assert review.status_code in {200, 201}, review.text
    resolved = client.post(
        f"/api/v1/reviews/{review.json()['id']}/resolve", headers=csrf_headers(client),
        json={"decision": "accept_remote_geography", "note": "Confirmed with recruiter"},
    )
    assert resolved.status_code == 200, resolved.text

    # A preference change keeps the candidate's decision (same snapshot and profile).
    _preferences(client, target_roles=["Data Engineer"])
    result = client.post("/api/v1/agent/refresh", headers=csrf_headers(client)).json()
    assert result["carried_reviews"] == 1
    item = client.get("/api/v1/recommendations").json()[0]
    assert item["match"]["eligibility_status"] == "eligible"

    # A new profile version needs a new decision.
    _profile(client, skills=("Python",))
    client.post("/api/v1/agent/refresh", headers=csrf_headers(client))
    item = client.get("/api/v1/recommendations").json()[0]
    assert item["match"]["eligibility_status"] == "needs_review"


def test_ingestion_queues_refreshes_only_for_consenting_viewers(client, monkeypatch):
    register(client, "agent-ingest@example.test")
    _profile(client)
    job_id = _job()
    client.post("/api/v1/agent/enable", headers=csrf_headers(client))
    _drain()
    owner = _user_id(client)
    with TestingSessionLocal() as db:
        connector = db.get(SourceConnector, db.get(JobPosting, job_id).connector_id)
        connector_id = connector.id
        # A private source of another user queues nothing for this candidate.
        connector.owner_user_id = None
        db.commit()

    _job(source_job_id="agent-ingest-new")

    def fake_run_ingestion(*args, **kwargs):
        return IngestionResult(run_id="r", connector_id=connector_id, status="succeeded",
                               stats={"created": 1})

    monkeypatch.setattr(source_worker, "run_ingestion", fake_run_ingestion)
    with TestingSessionLocal() as db:
        task = source_worker.enqueue_ingestion(db, connector_id=connector_id,
                                               idempotency_key="agent-hook-1",
                                               requested_by_user_id=None)
        db.commit()
        source_worker.run_next_ingestion_task(db, ArtifactStorage(get_settings()),
                                              worker_id="w")
        assert db.get(Task, task.id).state == "succeeded"
        queued = db.scalars(
            select(Task).where(Task.kind == AGENT_TASK_KIND, Task.state == "queued")
        ).all()
        assert [item.payload_json["user_id"] for item in queued] == [owner]

        connector = db.get(SourceConnector, connector_id)
        connector.owner_user_id = "someone-else"
        assert enqueue_for_catalog_change(db, connector) == 0


def test_next_actions_guide_a_new_candidate(client):
    register(client, "agent-actions@example.test")
    codes = [a["code"] for a in client.get("/api/v1/agent/status").json()["next_actions"]]
    assert codes[:3] == ["add_profile", "set_location_ladder", "enable_agent"]
    _profile(client, skills=(), authorization=False)
    _job()
    codes = [a["code"] for a in client.get("/api/v1/agent/status").json()["next_actions"]]
    assert {"confirm_authorization", "add_skills", "refresh_matches"} <= set(codes)
    _profile(client)
    _preferences(client, location_policy=LADDER)
    client.post("/api/v1/agent/enable", headers=csrf_headers(client))
    _drain()
    status = client.get("/api/v1/agent/status").json()
    codes = [a["code"] for a in status["next_actions"]]
    assert codes == ["review_ready"] and status["counts"]["ready"] == 1
    assert "fit" in status["rank_formula"]


def test_refresh_respects_its_limit_and_tenant_scope(client):
    register(client, "agent-limit@example.test")
    _profile(client)
    for index in range(3):
        _job(source_job_id=f"limit-{index}")
    user_id = _user_id(client)
    with TestingSessionLocal() as db:
        result = refresh_matches(db, user_id, limit=2)
        assert (result.matched, result.truncated) == (2, True)
        assert refresh_matches(db, user_id).matched == 1
        # Another candidate with no profile gets nothing.
        assert refresh_matches(db, "no-such-user").matched == 0


def test_match_for_user_records_v2_versions(client):
    register(client, "agent-versions@example.test")
    _profile(client)
    job_id = _job()
    with TestingSessionLocal() as db:
        run = match_job_for_user(db, user_id=_user_id(client), job_id=job_id)
        assert run.policy_version == "deterministic-v2"
        assert run.extractor_version == "rules-v2"
    assert agent_service.MAX_JOBS_PER_REFRESH >= 100
