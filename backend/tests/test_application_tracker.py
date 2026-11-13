"""Phase 6 application tracker: where the candidate has applied.

Design: docs/application-agent.md, "Application tracker". Nothing here sends anything.
"""

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import (
    Application,
    ApplicationEvent,
    AuditEvent,
    JobPosting,
    TrackedApplication,
)
from app.services.artifact_lifecycle import delete_private_source
from app.services.storage import ArtifactStorage
from tests.conftest import TestingSessionLocal, csrf_headers
from tests.test_lifecycle_and_operators import _connector, _ingest
from tests.test_phase2_matching import register
from tests.test_phase4_discovery import job_record
from tests.test_phase5c_applications import (
    _NETWORK_MODULES,
    _imports,
    answer,
    approve,
    create_profile,
    prepare,
    ready_application,
    ready_cv,
    standard_facts,
)

BACKEND = Path(__file__).resolve().parent.parent
TODAY = datetime.now(UTC).date()


def add(client: TestClient, **fields):
    body = {"company": "Globex", "role_title": "Data Engineer", **fields}
    return client.post("/api/v1/tracker", headers=csrf_headers(client), json=body)


def mark(client: TestClient, application_id: str, **fields):
    return client.post(
        f"/api/v1/applications/{application_id}/applied",
        headers=csrf_headers(client),
        json=fields,
    )


def tracker(client: TestClient, **params) -> dict:
    response = client.get("/api/v1/tracker", params=params)
    assert response.status_code == 200, response.text
    return response.json()


# --- in-app (link-out) applications ------------------------------------------------------


def test_marking_applied_binds_the_reviewed_package_and_freezes_it(client):
    job_id, cv_id, application = ready_application(client, "tracker-mark@example.test")
    approve(client, application)
    response = mark(client, application["id"], applied_on=str(TODAY), notes="Applied via site")
    assert response.status_code == 201, response.text
    entry = response.json()
    package = application["package"]
    assert entry["channel"] == "link_out" and entry["stage"] == "applied"
    assert (entry["application_id"], entry["package_id"], entry["review_hash"]) == (
        application["id"], package["id"], package["review_hash"],
    )
    # Company, role, and link are copied from the reviewed package, not typed.
    assert entry["company"] == package["content"]["job"]["employer"]
    assert entry["role_title"] == package["content"]["job"]["title"]
    assert entry["job_url"] == package["content"]["destination"]["url"]

    shown = client.get(f"/api/v1/applications/{application['id']}").json()
    assert shown["status"] == "applied"
    # The bound package revision is frozen while the entry exists.
    for refused in (
        prepare(client, job_id, cv_id),
        answer(client, application["id"], "salary_expectation", "100"),
        approve(client, application),
        client.post(f"/api/v1/applications/{application['id']}/skip",
                    headers=csrf_headers(client), json={}),
        mark(client, application["id"]),
    ):
        assert refused.status_code == 409, refused.text
        assert refused.json()["detail"]["code"] == "already_applied"
    with TestingSessionLocal() as db:
        events = [
            e.event_type
            for e in db.scalars(
                select(ApplicationEvent).where(ApplicationEvent.application_id == application["id"])
            )
        ]
    assert "marked_applied" in events


def test_unmarking_restores_the_application(client):
    job_id, cv_id, application = ready_application(client, "tracker-unmark@example.test")
    entry = mark(client, application["id"]).json()
    removed = client.delete(f"/api/v1/tracker/{entry['id']}", headers=csrf_headers(client))
    assert removed.status_code == 204
    shown = client.get(f"/api/v1/applications/{application['id']}").json()
    assert shown["status"] == "review_package_ready"
    assert prepare(client, job_id, cv_id).status_code == 200
    with TestingSessionLocal() as db:
        events = [
            e.event_type
            for e in db.scalars(
                select(ApplicationEvent).where(ApplicationEvent.application_id == application["id"])
            )
        ]
    assert events.index("marked_applied") < events.index("unmarked_applied")


def test_skipped_or_foreign_applications_cannot_be_marked(client):
    _, _, application = ready_application(client, "tracker-skip@example.test")
    client.post(f"/api/v1/applications/{application['id']}/skip",
                headers=csrf_headers(client), json={})
    skipped = mark(client, application["id"])
    assert skipped.status_code == 409 and skipped.json()["detail"]["code"] == "application_skipped"

    client.post("/api/v1/auth/logout", headers=csrf_headers(client))
    register(client, "tracker-other@example.test")
    foreign = mark(client, application["id"])
    assert foreign.status_code == 404
    assert tracker(client)["entries"] == []


# --- manual (external) entries ------------------------------------------------------------


def test_manual_entries_list_filter_and_summarize(client):
    register(client, "tracker-manual@example.test")
    first = add(client, company="Globex", role_title="Data Engineer",
                job_url="https://careers.globex.example/jobs/42",
                applied_on=str(TODAY - timedelta(days=3)))
    assert first.status_code == 201, first.text
    assert first.json()["channel"] == "external"
    add(client, company="Initech", role_title="Platform Engineer", stage="interviewing",
        applied_on=str(TODAY))
    add(client, company="globex ", role_title="Analytics Engineer", stage="rejected",
        applied_on=str(TODAY - timedelta(days=10)))

    listing = tracker(client)
    assert [e["company"] for e in listing["entries"]] == ["Initech", "Globex", "globex"]
    assert listing["summary"]["total"] == 3
    assert listing["summary"]["companies"] == 2  # Case/whitespace-insensitive.
    assert listing["summary"]["by_stage"]["interviewing"] == 1
    assert listing["summary"]["by_stage"]["rejected"] == 1
    assert [e["company"] for e in tracker(client, stage="interviewing")["entries"]] == ["Initech"]
    assert len(tracker(client, q="GLOBEX")["entries"]) == 2
    # Filters never hide the totals.
    assert tracker(client, q="initech")["summary"]["total"] == 3


def test_duplicates_are_refused_unless_confirmed(client):
    register(client, "tracker-dup@example.test")
    original = add(client, job_url="https://careers.globex.example/jobs/42").json()
    for duplicate in (
        add(client, company="  GLOBEX ", role_title="data   engineer"),
        add(client, company="Other", role_title="Other",
            job_url="https://CAREERS.globex.example/jobs/42/"),
    ):
        assert duplicate.status_code == 409
        detail = duplicate.json()["detail"]
        assert detail["code"] == "possible_duplicate" and detail["existing_id"] == original["id"]
    assert add(client, allow_duplicate=True).status_code == 201


def test_invalid_entries_are_rejected(client):
    register(client, "tracker-invalid@example.test")
    for fields in (
        {"company": "   "},
        {"role_title": "Data\x00Engineer"},
        {"job_url": "javascript:alert(1)"},
        {"job_url": "ftp://files.example/job"},
        {"job_url": "https://user:pw@careers.example/job"},
        {"applied_on": str(TODAY + timedelta(days=3))},
        {"applied_on": "1999-12-31"},
        {"stage": "hired"},
        {"notes": "x" * 2001},
        {"company": "x" * 256},
        {"unexpected": "field"},
    ):
        response = add(client, **fields)
        assert response.status_code == 422, (fields, response.text)
    assert tracker(client)["entries"] == []


def test_updates_respect_locked_fields_and_are_audited_without_content(client):
    _, _, application = ready_application(client, "tracker-update@example.test")
    linked = mark(client, application["id"]).json()
    manual = add(client, company="Initech", role_title="SRE", notes="Referral from a friend").json()

    staged = client.patch(f"/api/v1/tracker/{linked['id']}", headers=csrf_headers(client),
                          json={"stage": "interviewing", "notes": "Phone screen booked"})
    assert staged.status_code == 200 and staged.json()["stage"] == "interviewing"
    locked = client.patch(f"/api/v1/tracker/{linked['id']}", headers=csrf_headers(client),
                          json={"company": "Someone Else"})
    assert locked.status_code == 409 and locked.json()["detail"]["code"] == "field_locked"
    renamed = client.patch(f"/api/v1/tracker/{manual['id']}", headers=csrf_headers(client),
                           json={"company": "Initech Corp", "stage": "offer"})
    assert renamed.json()["company"] == "Initech Corp"

    with TestingSessionLocal() as db:
        audits = list(db.scalars(
            select(AuditEvent).where(AuditEvent.action.like("tracker.%"))
            .order_by(AuditEvent.occurred_at)
        ))
    assert [a.action for a in audits] == [
        "tracker.created", "tracker.created", "tracker.updated", "tracker.updated",
    ]
    assert audits[2].metadata_json["fields"] == ["notes", "stage"]
    stored = json.dumps([a.metadata_json for a in audits])
    for private in ("Initech", "SRE", "Referral", "Phone screen", linked["company"]):
        assert private not in stored


def test_entries_are_tenant_scoped(client):
    register(client, "tracker-owner@example.test")
    entry = add(client).json()
    client.post("/api/v1/auth/logout", headers=csrf_headers(client))
    register(client, "tracker-intruder@example.test")
    assert tracker(client)["entries"] == []
    for response in (
        client.patch(f"/api/v1/tracker/{entry['id']}", headers=csrf_headers(client),
                     json={"stage": "offer"}),
        client.delete(f"/api/v1/tracker/{entry['id']}", headers=csrf_headers(client)),
    ):
        assert response.status_code == 404


def test_write_routes_require_csrf(client):
    register(client, "tracker-csrf@example.test")
    response = client.post("/api/v1/tracker", json={"company": "A", "role_title": "B"})
    assert response.status_code == 403


# --- data lifecycle -------------------------------------------------------------------------


def test_export_includes_and_profile_deletion_removes_tracker_entries(client):
    _, _, application = ready_application(client, "tracker-export@example.test")
    mark(client, application["id"])
    add(client)
    exported = client.get("/api/v1/profile/export").json()
    assert {e["channel"] for e in exported["tracked_applications"]} == {"link_out", "external"}
    assert client.delete("/api/v1/profile", headers=csrf_headers(client)).status_code == 200
    with TestingSessionLocal() as db:
        assert db.scalars(select(TrackedApplication)).all() == []


def test_private_source_deletion_keeps_history_but_unlinks_it(client):
    owner_id = client.post(
        "/api/v1/auth/register",
        json={"email": "tracker-private@example.test", "password": "phase6-secure-password-1"},
    ).json()["id"]
    with TestingSessionLocal() as db:
        private = _connector(db, "private-tracker", owner_user_id=owner_id)
        _ingest(db, private, [job_record("private-t1", title="Example Data Analyst")])
        job_id = db.scalar(select(JobPosting.id).where(JobPosting.source_job_id == "private-t1"))
        private_id = private.id
    create_profile(client, standard_facts())
    application = prepare(client, job_id, ready_cv(client, job_id)).json()
    entry = mark(client, application["id"]).json()
    with TestingSessionLocal() as db:
        delete_private_source(db, ArtifactStorage(get_settings()), connector_id=private_id,
                              requested_by_user_id=owner_id)
        db.commit()
        assert db.scalars(select(Application)).all() == []
        kept = db.get(TrackedApplication, entry["id"])
        assert kept is not None and kept.application_id is None
        assert kept.role_title == "Example Data Analyst"


def test_tracker_code_has_no_network_capability():
    for relative in ("app/services/tracker.py", "app/api/routes/tracker.py"):
        imported = _imports(BACKEND / relative)
        for module in imported:
            assert not {module, module.split(".")[0]} & _NETWORK_MODULES, (relative, module)
        assert not any(
            module.startswith(("app.services.sources.http_client", "app.services.tasks"))
            for module in imported
        ), (relative, imported)


def test_default_applied_on_is_today():
    from app.services.tracker import clean_date

    assert clean_date(None) == TODAY
    assert clean_date(date(2001, 1, 1)) == date(2001, 1, 1)
