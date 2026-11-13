"""Phase 5B: Ashby public job board provider through the full read-only pipeline.

All HTTP goes to an httpx.MockTransport serving sanitized fixtures; DNS is faked.
"""

import copy
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.admin as admin_cli
from app.core.config import get_settings
from app.core.redaction import register_secret
from app.db.models import AuditEvent, JobPosting, JobSnapshot, SourceConnector, SourceRun, Task
from app.services.sources.ashby import (
    ASHBY_API_HOST,
    ASHBY_DESCRIPTOR,
    AshbyJobBoardProvider,
    ashby_board_url,
    ashby_connector_config,
    html_to_text,
    map_ashby_job,
)
from app.services.sources.base import FetchRequest, freeze
from app.services.sources.connectors import (
    ConnectorRegistrationError,
    register_ashby_connector,
    set_connector_enabled,
)
from app.services.sources.egress import path_within
from app.services.sources.errors import SourceError, SourceErrorCode, SourcePolicyError
from app.services.sources.normalize import normalize_record
from app.services.sources.pipeline import build_connector_http_client, run_ingestion
from app.services.sources.registry import default_registry
from app.services.sources.scheduler import schedule_due_runs, set_connector_schedule
from app.services.sources.worker import run_next_ingestion_task
from app.services.storage import ArtifactStorage
from tests.conftest import TestingSessionLocal, app

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "ashby" / "board_v1.json").read_text(encoding="utf-8")
)
BOARD = "example-board"
EMPLOYER = "Example Robotics"
BOARD_URL = ashby_board_url(BOARD)
JOB1 = "11111111-1111-4111-8111-111111111111"
JOB2 = "22222222-2222-4222-8222-222222222222"
JOB3 = "33333333-3333-4333-8333-333333333333"
JOB10 = "99999999-9999-4999-8999-999999999999"
PUBLIC_IP = "172.66.40.214"
ALLOWLIST = (ASHBY_API_HOST,)
T0 = datetime(2026, 9, 30, 9, 0, tzinfo=UTC)


def at(minutes: int) -> datetime:
    """Consecutive runs are spaced past the provider's 5-minute minimum interval."""
    return T0 + timedelta(minutes=minutes)


def board(**changes: Any) -> dict[str, Any]:
    data = copy.deepcopy(FIXTURE)
    data.update(changes)
    return data


def job(payload: dict[str, Any], job_id: str) -> dict[str, Any]:
    return next(
        item for item in payload["jobs"]
        if isinstance(item, dict) and str(item.get("jobUrl", "")).endswith(job_id)
    )


class AshbyServer:
    """Mock Ashby endpoint that records every request it receives."""

    def __init__(self, payload: Any, status: int = 200, **headers: str) -> None:
        self.payload = payload
        self.status = status
        self.headers = {"content-type": "application/json; charset=utf-8", **headers}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        payload = self.payload
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return httpx.Response(self.status, headers=self.headers, content=body)


def factory(server: Callable[[httpx.Request], httpx.Response]):
    return partial(
        build_connector_http_client,
        resolver=lambda host, port: [PUBLIC_IP],
        transport=httpx.MockTransport(server),
    )


def ashby_connector(db, name: str = "example-ashby", **overrides: Any) -> SourceConnector:
    connector = register_ashby_connector(
        db,
        name=name,
        board_name=BOARD,
        employer_name=EMPLOYER,
        basis="official_api",
        evidence_ref="https://developers.ashbyhq.com/docs/public-job-posting-api",
        reviewed_by="test-reviewer",
        terms_version="ashby-posting-api-2026-09-30",
        approve=True,
        actor="test",
        **overrides,
    )
    connector.enabled = True
    db.commit()
    return connector


def run(db, connector, server, **kwargs: Any):
    return run_ingestion(
        db,
        ArtifactStorage(get_settings()),
        connector_id=connector.id,
        registry=default_registry(),
        network_access_enabled=True,
        egress_allowlist=ALLOWLIST,
        http_client_factory=factory(server),
        **kwargs,
    )


def postings(db, connector) -> dict[str, JobPosting]:
    return {
        item.source_job_id: item
        for item in db.scalars(select(JobPosting).where(JobPosting.connector_id == connector.id))
    }


@pytest.fixture()
def db():
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


# --- mapping ------------------------------------------------------------------------------


def test_mapping_copies_explicit_fields_and_leaves_eligibility_unknown():
    record = map_ashby_job(job(FIXTURE, JOB1), board=BOARD, employer=EMPLOYER)
    content = normalize_record(record).content
    assert content["source_job_id"] == JOB1
    assert content["title"] == "Robotics Software Engineer"
    assert content["employer"] == EMPLOYER
    assert content["canonical_url"] == f"https://jobs.ashbyhq.com/{BOARD}/{JOB1}"
    assert content["location_text"] == "Austin, TX"
    assert content["secondary_locations"] == ["Boston, MA", "Toronto, ON"]
    assert content["location_country"] == "US"  # Onsite role, explicit address country.
    assert content["work_mode"] == "onsite"
    assert content["employment_type"] == "full_time"
    assert content["posted_at"] == "2026-09-01T10:15:30Z"
    assert content["compensation"] == {
        "min_amount": 150000.0, "max_amount": 190000.0, "currency": "USD", "period": "year",
    }
    assert content["description"].startswith("Build robot software.")
    # Never inferred, even though the text mentions sponsorship and relocation.
    assert content["sponsorship"] == "unknown"
    assert content["relocation_assistance"] == "unknown"
    assert content["work_authorization_requirements"] == []
    assert content["remote_geography"] is None
    assert content["permitted_countries"] == []
    assert content["updated_at"] is None and content["closing_at"] is None
    assert content["status"] == "active"


def test_remote_address_is_not_treated_as_work_geography():
    content = normalize_record(
        map_ashby_job(job(FIXTURE, JOB2), board=BOARD, employer=EMPLOYER)
    ).content
    assert content["work_mode"] == "remote"
    assert content["location_country"] is None  # HQ address of a remote role.
    assert content["remote_geography"] is None
    assert content["compensation"] is None
    assert content["employment_type"] == "contract"


def test_html_only_description_is_text_only_and_ambiguous_pay_stays_unknown():
    content = normalize_record(
        map_ashby_job(job(FIXTURE, JOB3), board=BOARD, employer=EMPLOYER)
    ).content
    assert content["source_job_id"] == JOB3  # From the documented jobUrl (no id field).
    assert "Design products & systems." in content["description"]
    assert "fetch(" not in content["description"] and "<" not in content["description"]
    assert "Apply here" in content["description"] and "evil.test" not in content["description"]
    assert content["compensation"] is None  # Two salary components: ambiguous.
    assert content["location_country"] == "GB" and content["work_mode"] == "hybrid"
    assert html_to_text("<style>x{}</style>a<br>b") == "a\nb"


def test_contradictory_or_unmapped_values_stay_unknown():
    content = normalize_record(
        map_ashby_job(job(FIXTURE, JOB10), board=BOARD, employer=EMPLOYER)
    ).content
    assert content["work_mode"] == "unknown"  # OnSite + isRemote: contradictory.
    assert content["location_country"] is None
    assert content["compensation"] == {
        "min_amount": 30.0, "max_amount": 40.0, "currency": None, "period": "hour",
    }
    assert content["employment_type"] == "internship"


def test_identity_rules():
    wrong_board = map_ashby_job(
        {"jobUrl": f"https://jobs.ashbyhq.com/other-board/{JOB1}", "title": "x"},
        board=BOARD, employer=EMPLOYER,
    )
    conflict = map_ashby_job(
        {"id": JOB2, "jobUrl": f"https://jobs.ashbyhq.com/{BOARD}/{JOB1}"},
        board=BOARD, employer=EMPLOYER,
    )
    id_only = map_ashby_job({"id": JOB2.upper()}, board=BOARD, employer=EMPLOYER)
    no_identity = map_ashby_job({"title": "x"}, board=BOARD, employer=EMPLOYER)
    assert wrong_board["source_job_id"] is None
    assert conflict["source_job_id"] is None
    assert id_only["source_job_id"] == JOB2
    assert no_identity["source_job_id"] is None


# --- configuration binding ----------------------------------------------------------------


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c.update(board_name="../admin"),
        lambda c: c.update(board_name=""),
        lambda c: c.update(employer_name=""),
        lambda c: c.update(fetch_hosts=[ASHBY_API_HOST, "api.other.invalid"]),
        lambda c: c.update(fetch_path_prefixes=["/posting-api/job-board/"]),
        lambda c: c.update(fetch_path_prefixes=["/posting-api/job-board/other"]),
        lambda c: c.update(allowed_url_hosts=["jobs.ashbyhq.com", "evil.test"]),
        lambda c: c.update(include_compensation="yes"),
        lambda c: c.update(credential={"ref": "env:JFA_SOURCE_SECRET_X"}),
    ],
)
def test_connector_must_be_bound_to_exactly_one_board_endpoint(mutate):
    config = ashby_connector_config(BOARD, EMPLOYER)
    mutate(config)
    with pytest.raises(SourcePolicyError) as raised:
        AshbyJobBoardProvider().fetch_page(
            FetchRequest(connector_id="c", config=freeze(config), http=object())
        )
    assert raised.value.code in {
        SourceErrorCode.CONNECTOR_MISCONFIGURED, SourceErrorCode.CREDENTIAL_UNAVAILABLE,
    }


def test_exact_board_path_matching():
    prefix = "/posting-api/job-board/example-board"
    assert path_within(prefix, prefix)
    assert path_within(prefix + "/x", prefix)
    assert not path_within("/posting-api/job-board/example-boardEvil", prefix)
    assert not path_within("/posting-api/job-board/other", prefix)


def test_registration_is_disabled_by_default_and_validated(db):
    connector = register_ashby_connector(
        db, name="reg", board_name=BOARD, employer_name=EMPLOYER, basis="official_api",
        evidence_ref="docs", reviewed_by="reviewer", terms_version="t1", actor="t",
    )
    assert (connector.enabled, connector.policy_status) == (False, "review_required")
    assert connector.config_json["fetch_path_prefixes"] == [f"/posting-api/job-board/{BOARD}"]
    assert connector.authorization_json["reviewed_by"] == "reviewer"
    for kwargs in (
        {"basis": "unofficial_scraping"},
        {"basis": "local_fixture"},
        {"board_name": "bad board"},
        {"reviewed_by": " "},
        {"name": "reg"},  # duplicate
        {"owner_email": "missing@example.test"},
    ):
        values: dict[str, Any] = {
            "name": "reg-2", "board_name": BOARD, "employer_name": EMPLOYER,
            "basis": "official_api", "evidence_ref": "docs", "reviewed_by": "reviewer",
            "terms_version": "t1", "actor": "t",
        }
        values.update(kwargs)
        with pytest.raises(ConnectorRegistrationError):
            register_ashby_connector(db, **values)
    set_connector_enabled(db, connector, enabled=True, actor="t", reason="approved board")
    db.commit()
    actions = set(db.scalars(select(AuditEvent.action)).all())
    assert {"source.connector_registered", "source.connector_enabled"} <= actions


def test_ashby_is_blocked_under_default_settings(db):
    connector = ashby_connector(db)
    server = AshbyServer(FIXTURE)
    with pytest.raises(SourcePolicyError) as raised:
        run_ingestion(
            db, ArtifactStorage(get_settings()), connector_id=connector.id,
            registry=default_registry(), http_client_factory=factory(server),
        )
    assert raised.value.code == SourceErrorCode.NETWORK_ACCESS_DISABLED
    assert server.requests == []
    assert default_registry().kinds() == ["ashby_job_board", "fixture", "sandbox_ats_fixture"]


# --- full pipeline ------------------------------------------------------------------------


def test_first_run_ingests_valid_records_and_rejects_bad_ones(db):
    connector = ashby_connector(db)
    server = AshbyServer(FIXTURE)
    result = run(db, connector, server)

    assert result.status == "partial"
    assert (result.count("created"), result.count("rejected")) == (4, 5)
    codes = sorted(tuple(item["codes"]) for item in result.stats["rejections"])
    assert codes == sorted(
        [
            ("title_missing",),
            ("source_job_id_missing",),  # wrong host/board
            ("source_job_id_missing",),  # id/jobUrl conflict
            ("duplicate_in_response",),
            ("record_not_object",),
        ]
    )
    jobs = postings(db, connector)
    assert set(jobs) == {JOB1, JOB2, JOB3, JOB10}
    assert "44444444-4444-4444-8444-444444444444" not in jobs  # unlisted excluded
    assert jobs[JOB1].title == "Robotics Software Engineer"  # first occurrence kept

    # Exactly one outbound request: the configured board endpoint, read-only, anonymous.
    [request] = server.requests
    assert request.method == "GET" and request.content == b""
    assert str(request.url) == f"{BOARD_URL}?includeCompensation=true"
    assert "authorization" not in request.headers and "cookie" not in request.headers
    assert set(request.headers) <= {
        "host", "accept", "accept-encoding", "user-agent", "connection",
    }


def test_snapshots_carry_full_provenance_and_the_original_payload(db):
    connector = ashby_connector(db)
    result = run(db, connector, AshbyServer(FIXTURE))
    posting = postings(db, connector)[JOB1]
    [snapshot] = db.scalars(select(JobSnapshot).where(JobSnapshot.job_id == posting.id)).all()
    provenance = snapshot.provenance_json
    assert provenance["provider_kind"] == "ashby_job_board"
    assert provenance["provider_version"] == ASHBY_DESCRIPTOR.version
    assert provenance["provider_api_version"] == "1"
    assert provenance["listing_url"] == BOARD_URL
    assert provenance["authorization_basis"] == "official_api"
    assert provenance["terms_version"] == "ashby-posting-api-2026-09-30"
    assert provenance["source_run_id"] == result.run_id and provenance["fetched_at"]
    assert provenance["source_job_id"] == JOB1
    storage = ArtifactStorage(get_settings())
    from app.db.models import StoredArtifact

    artifact = db.get(StoredArtifact, snapshot.raw_artifact_id)
    assert artifact is not None
    raw = json.loads(storage.read_bytes(artifact.object_key))
    assert raw["source_payload"] == job(FIXTURE, JOB1)  # exactly what Ashby returned
    from app.services.sources.normalize import stable_hash

    assert snapshot.raw_content_hash == stable_hash(raw)
    events = db.scalars(
        select(AuditEvent).where(AuditEvent.action == "source.network_request")
    ).all()
    assert [event.metadata_json["host"] for event in events] == [ASHBY_API_HOST]


def test_rerun_is_idempotent(db):
    connector = ashby_connector(db)
    run(db, connector, AshbyServer(FIXTURE), now=at(0))
    snapshots_before = db.query(JobSnapshot).count()
    second = run(db, connector, AshbyServer(FIXTURE), now=at(10))
    assert (second.count("created"), second.count("updated"), second.count("unchanged")) == (
        0, 0, 4,
    )
    assert db.query(JobSnapshot).count() == snapshots_before
    assert db.query(JobPosting).count() == 4


def test_changed_removed_and_reappearing_postings(db):
    connector = ashby_connector(db)
    run(db, connector, AshbyServer(FIXTURE), now=at(0))

    changed = board()
    job(changed, JOB1)["title"] = "Senior Robotics Software Engineer"
    job(changed, JOB3)["publishedAt"] = "2026-09-05"  # non-material field only
    changed["jobs"] = [
        item for item in changed["jobs"]
        if not (isinstance(item, dict) and str(item.get("jobUrl", "")).endswith(JOB2))
    ]
    result = run(db, connector, AshbyServer(changed), now=at(10))
    jobs = postings(db, connector)
    assert result.count("updated") == 2 and result.count("stale") == 1
    assert jobs[JOB2].status == "stale"  # removed from listing: stale, not closed or deleted
    assert jobs[JOB2].status_reason == "absent_from_complete_listing"
    assert jobs[JOB1].title == "Senior Robotics Software Engineer"
    versions = db.scalars(
        select(JobSnapshot.version).where(JobSnapshot.job_id == jobs[JOB1].id)
    ).all()
    assert sorted(versions) == [1, 2]
    updates = {
        event.object_id: event.metadata_json
        for event in db.scalars(select(AuditEvent).where(AuditEvent.action == "job.updated"))
    }
    assert updates[jobs[JOB1].id]["changed_material_fields"] == ["title"]
    assert updates[jobs[JOB3].id]["material_change"] is False

    back = run(db, connector, AshbyServer(FIXTURE), now=at(20))
    assert back.count("reopened") == 1
    assert postings(db, connector)[JOB2].status == "active"

    # An empty listing is never treated as mass closure.
    empty = run(db, connector, AshbyServer({"apiVersion": "1", "jobs": []}), now=at(30))
    assert empty.count("stale") == 0 and "empty_complete_listing_ignored" in empty.stats["notices"]


@pytest.mark.parametrize(
    ("server", "code", "blocked"),
    [
        (AshbyServer({"apiVersion": "1"}), SourceErrorCode.INVALID_PROVIDER_RESPONSE, False),
        (AshbyServer([1, 2]), SourceErrorCode.INVALID_PROVIDER_RESPONSE, False),
        (AshbyServer(b"<html>"), SourceErrorCode.INVALID_PROVIDER_RESPONSE, False),
        (AshbyServer({}, status=503), SourceErrorCode.PROVIDER_UNAVAILABLE, False),
        (AshbyServer({}, status=403), SourceErrorCode.PROVIDER_ACCESS_DENIED, True),
        (
            AshbyServer(b"", status=302, location="/posting-api/job-board/other-board"),
            SourceErrorCode.EGRESS_DENIED,
            True,
        ),
        (
            AshbyServer(b"", status=302, location="https://jobs.ashbyhq.com/example-board"),
            SourceErrorCode.EGRESS_DENIED,
            True,
        ),
    ],
)
def test_failures_are_classified_and_persist_nothing(db, server, code, blocked):
    connector = ashby_connector(db)
    with pytest.raises(SourceError) as raised:
        run(db, connector, server)
    assert raised.value.code == code
    db.refresh(connector)
    assert (connector.health_status == "blocked") is blocked
    assert db.query(JobPosting).count() == 0
    run_row = db.scalars(select(SourceRun)).one()
    assert run_row.status == ("blocked" if blocked else "failed")


def test_oversized_board_is_rejected(db, monkeypatch):
    connector = ashby_connector(db)
    settings = get_settings()
    monkeypatch.setattr(settings, "source_http_max_response_bytes", 2_000)
    with pytest.raises(SourceError) as raised:
        run(db, connector, AshbyServer(FIXTURE))
    assert raised.value.code == SourceErrorCode.RESPONSE_TOO_LARGE


def test_same_board_via_second_shared_connector_is_linked_not_merged(db):
    first = ashby_connector(db, "ashby-a")
    second = ashby_connector(db, "ashby-b")
    run(db, first, AshbyServer(FIXTURE))
    run(db, second, AshbyServer(FIXTURE))
    primary = postings(db, first)[JOB1]
    duplicate = postings(db, second)[JOB1]
    assert duplicate.duplicate_of_job_id == primary.id
    assert duplicate.dedup_reason == "canonical_url"


def test_private_ashby_source_is_tenant_isolated_with_owner_audits(db):
    owner = TestClient(app)
    other = TestClient(app)
    owner.post("/api/v1/auth/register",
               json={"email": "ashby-owner@example.test", "password": "ashby-password-123"})
    other.post("/api/v1/auth/register",
               json={"email": "ashby-other@example.test", "password": "ashby-password-123"})
    owner_id = owner.get("/api/v1/auth/me").json()["id"]
    connector = ashby_connector(db, "private-ashby", owner_email="ashby-owner@example.test")
    shared = ashby_connector(db, "shared-ashby")
    run(db, connector, AshbyServer(FIXTURE))
    run(db, shared, AshbyServer(FIXTURE))

    private_jobs = postings(db, connector)
    assert all(item.owner_user_id == owner_id for item in private_jobs.values())
    # Never linked across tenant scopes, even with identical URLs.
    assert all(item.duplicate_of_job_id is None for item in private_jobs.values())
    events = db.scalars(select(AuditEvent).where(AuditEvent.object_id.in_(
        [item.id for item in private_jobs.values()]
    ))).all()
    assert events and all(event.user_id == owner_id for event in events)
    network = db.scalars(select(AuditEvent).where(AuditEvent.action == "source.network_request"))
    assert {event.user_id for event in network} == {owner_id, None}

    other_ids = {item["id"] for item in other.get("/api/v1/jobs").json()}
    assert not other_ids & {item.id for item in private_jobs.values()}
    assert {item["name"] for item in other.get("/api/v1/connectors").json()} == {"shared-ashby"}
    for posting in private_jobs.values():
        assert other.get(f"/api/v1/jobs/{posting.id}").status_code == 404


def test_no_secret_reaches_logs_audits_or_stored_records(db, caplog):
    settings = get_settings()
    deployment_secrets = [settings.session_secret, settings.artifact_encryption_key]
    for value in deployment_secrets:
        register_secret(value)
    connector = ashby_connector(db)
    with caplog.at_level(logging.DEBUG):
        run(db, connector, AshbyServer(FIXTURE), now=at(0))
        with pytest.raises(SourceError):
            run(db, connector, AshbyServer({}, status=500), now=at(10))
    storage = ArtifactStorage(get_settings())
    from app.db.models import StoredArtifact

    stored = [
        storage.read_bytes(item.object_key).decode()
        for item in db.scalars(select(StoredArtifact))
    ]
    persisted = json.dumps(
        [e.metadata_json for e in db.scalars(select(AuditEvent))]
        + [r.stats_json for r in db.scalars(select(SourceRun))]
        + [s.normalized_json for s in db.scalars(select(JobSnapshot))]
        + [s.provenance_json for s in db.scalars(select(JobSnapshot))]
        + [c.config_json for c in db.scalars(select(SourceConnector))]
    )
    haystack = "\n".join([caplog.text, persisted, *stored])
    for value in deployment_secrets:
        assert value not in haystack


def test_scheduler_and_worker_run_ashby_without_overlap(db):
    connector = ashby_connector(db)
    set_connector_schedule(db, connector, interval_seconds=3600, actor="t", user_id=None)
    db.commit()
    [decision] = schedule_due_runs(db)
    assert decision.action == "enqueued"
    assert schedule_due_runs(db) == []  # same slot: nothing new

    server = AshbyServer(FIXTURE)
    settings = get_settings()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(settings, "source_network_access_enabled", True)
        patch.setattr(settings, "source_egress_allowlist", ASHBY_API_HOST)
        run_next_ingestion_task(
            db, ArtifactStorage(settings), worker_id="w",
            registry=default_registry(), http_client_factory=factory(server),
        )
    task = db.get(Task, decision.task_id)
    assert task is not None
    db.refresh(task)
    assert task.state == "succeeded" and len(server.requests) == 1
    assert db.query(JobPosting).count() == 4

    # Provider minimum interval (5 min) is enforced for back-to-back manual runs.
    with pytest.raises(SourceError) as limited:
        run_ingestion(
            db, ArtifactStorage(settings), connector_id=connector.id,
            registry=default_registry(), network_access_enabled=True,
            egress_allowlist=ALLOWLIST, http_client_factory=factory(server),
        )
    assert limited.value.code == SourceErrorCode.RATE_LIMITED


def test_admin_cli_registers_and_toggles_ashby(monkeypatch, capsys):
    monkeypatch.setattr(admin_cli, "SessionLocal", TestingSessionLocal)
    args = [
        "register-ashby", "--name", "cli-ashby", "--board", BOARD, "--employer", EMPLOYER,
        "--basis", "official_api", "--evidence-ref", "docs-url", "--reviewed-by", "owner",
        "--terms-version", "t1", "--approve",
    ]
    assert admin_cli.main(args) == 0
    assert '"enabled": false' in capsys.readouterr().out
    assert admin_cli.main(args) == 1  # duplicate name
    assert admin_cli.main(["set-connector-enabled", "--connector", "cli-ashby", "--enable",
                           "--reason", "reviewed"]) == 0
    with TestingSessionLocal() as db:
        connector = db.scalars(select(SourceConnector).where(
            SourceConnector.name == "cli-ashby")).one()
        assert connector.enabled and connector.policy_status == "approved"


def test_http_library_loggers_never_emit_request_urls(capsys):
    """Found in the live smoke test: httpx logged full request URLs at INFO."""
    from app.core.logging import configure_logging

    configure_logging("DEBUG")
    try:
        logging.getLogger("httpx").info("HTTP Request: GET %s", f"{BOARD_URL}?token=abc")
        logging.getLogger("httpcore.connection").debug("connect_tcp.started host=%s", "x")
        logging.getLogger("app.test").info("application message")
        output = capsys.readouterr().out
    finally:
        configure_logging("INFO")
    assert "posting-api" not in output and "connect_tcp" not in output
    assert "application message" in output
