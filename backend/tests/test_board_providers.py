"""Greenhouse Job Board API and Lever Postings API providers (read-only discovery)."""

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

import app.admin as admin_cli
from app.core.config import get_settings
from app.db.models import JobPosting, JobRequirement, JobSnapshot, SourceConnector
from app.services.requirements import analyze_job_snapshot
from app.services.sources.base import FetchRequest, freeze
from app.services.sources.connectors import (
    ConnectorRegistrationError,
    register_greenhouse_connector,
    register_lever_connector,
)
from app.services.sources.errors import SourceErrorCode, SourcePolicyError
from app.services.sources.greenhouse import (
    GREENHOUSE_API_HOST,
    GreenhouseJobBoardProvider,
    greenhouse_board_url,
    map_greenhouse_job,
)
from app.services.sources.lever import (
    LEVER_API_HOSTS,
    LeverPostingsProvider,
    lever_postings_url,
    map_lever_posting,
)
from app.services.sources.pipeline import run_ingestion
from app.services.sources.registry import default_registry
from app.services.storage import ArtifactStorage
from tests.conftest import TestingSessionLocal
from tests.test_phase5b_ashby import AshbyServer as MockServer
from tests.test_phase5b_ashby import factory

FIXTURES = Path(__file__).parent / "fixtures"
GREENHOUSE = json.loads((FIXTURES / "greenhouse" / "board_v1.json").read_text(encoding="utf-8"))
LEVER = json.loads((FIXTURES / "lever" / "site_v1.json").read_text(encoding="utf-8"))
T0 = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)
SRE = "aaaaaaaa-1111-4111-8111-111111111111"
FRONTEND = "bbbbbbbb-2222-4222-8222-222222222222"
DESIGNER = "cccccccc-3333-4333-8333-333333333333"


@pytest.fixture()
def db():
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


def greenhouse_connector(db, *, careers_host: str | None = None, name: str = "gh-example"):
    connector = register_greenhouse_connector(
        db,
        name=name,
        board_token="exampleco",
        employer_name="Example Co",
        basis="official_api",
        evidence_ref="https://developers.greenhouse.io/job-board.html",
        reviewed_by="test-reviewer",
        terms_version="greenhouse-job-board-api-2026-11-16",
        careers_host=careers_host,
        approve=True,
        actor="test",
    )
    connector.enabled = True
    db.commit()
    return connector


def lever_connector(db, *, name: str = "lever-example"):
    connector = register_lever_connector(
        db,
        name=name,
        site="example-site",
        employer_name="Example Site Ltd",
        basis="official_api",
        evidence_ref="https://github.com/lever/postings-api",
        reviewed_by="test-reviewer",
        terms_version="lever-postings-api-2026-11-16",
        approve=True,
        actor="test",
    )
    connector.enabled = True
    db.commit()
    return connector


def run(db, connector, server, host: str, *, minutes: int = 0):
    return run_ingestion(
        db,
        ArtifactStorage(get_settings()),
        connector_id=connector.id,
        registry=default_registry(),
        network_access_enabled=True,
        egress_allowlist=(host,),
        http_client_factory=factory(server),
        now=T0 + timedelta(minutes=minutes),
    )


def postings(db, connector) -> dict[str, JobPosting]:
    return {
        item.source_job_id: item
        for item in db.scalars(select(JobPosting).where(JobPosting.connector_id == connector.id))
    }


# --- mapping ------------------------------------------------------------------------------


def test_greenhouse_mapping_is_explicit_and_text_only():
    record = map_greenhouse_job(GREENHOUSE["jobs"][0], employer="Example Co")
    assert record["source_job_id"] == "4100001"
    assert record["work_mode"] == "remote"
    # "Remote - India" names a work mode, not a permitted geography.
    assert record["location_country"] is None and "permitted_countries" not in record
    assert "steal()" not in record["description"] and "evil.test" not in record["description"]
    assert "Requirements" in record["description"] and "<" not in record["description"]
    assert record["posted_at"].startswith("2026-10-01")
    assert map_greenhouse_job(GREENHOUSE["jobs"][1], employer="E")["work_mode"] == "hybrid"
    assert map_greenhouse_job(GREENHOUSE["jobs"][2], employer="E")["work_mode"] is None
    assert map_greenhouse_job(GREENHOUSE["jobs"][4], employer="E")["source_job_id"] is None
    assert map_greenhouse_job({"id": True}, employer="E")["source_job_id"] is None


def test_lever_mapping_uses_structured_fields_only():
    site, region, employer = "example-site", "global", "Example Site Ltd"
    sre = map_lever_posting(LEVER[0], site=site, region=region, employer=employer)
    assert (sre["work_mode"], sre["location_country"]) == ("onsite", "IN")
    assert sre["employment_type"] == "full_time"
    assert sre["compensation"] == {"min_amount": 2400000, "max_amount": 3600000,
                                   "currency": "INR", "period": "year"}
    assert sre["secondary_locations"] == ["Ahmedabad, India"]
    assert sre["description"].splitlines()[2] == "Requirements"
    assert sre["posted_at"].startswith("2026-09")
    remote = map_lever_posting(LEVER[1], site=site, region=region, employer=employer)
    # A remote posting's country is where it is listed, not where work is permitted.
    assert (remote["work_mode"], remote["location_country"]) == ("remote", None)
    assert remote["compensation"] is None  # No interval: incomplete, so unknown.
    designer = map_lever_posting(LEVER[2], site=site, region=region, employer=employer)
    assert (designer["work_mode"], designer["location_country"]) == (None, None)
    assert map_lever_posting(LEVER[3], site=site, region=region, employer=employer)[
        "source_job_id"
    ] is None
    assert map_lever_posting(LEVER[4], site=site, region=region, employer=employer)[
        "source_job_id"
    ] is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c.update(fetch_hosts=["evil.test"]),
        lambda c: c.update(fetch_path_prefixes=["/v1/boards/"]),
        lambda c: c.update(allowed_url_hosts=["evil.test"]),
        lambda c: c.update(board_name="../x"),
        lambda c: c.update(credential="file:token"),
    ],
)
def test_greenhouse_connector_must_be_bound_to_one_board(db, mutate):
    connector = greenhouse_connector(db)
    config = copy.deepcopy(connector.config_json)
    mutate(config)
    with pytest.raises(SourcePolicyError) as raised:
        GreenhouseJobBoardProvider().fetch_page(
            FetchRequest(connector_id=connector.id, config=freeze(config), http=object())
        )
    assert raised.value.code == SourceErrorCode.CONNECTOR_MISCONFIGURED


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c.update(fetch_hosts=[LEVER_API_HOSTS["eu"]]),
        lambda c: c.update(region="mars"),
        lambda c: c.update(allowed_url_hosts=["jobs.lever.co", "evil.test"]),
        lambda c: c.update(site="Bad Site"),
    ],
)
def test_lever_connector_must_be_bound_to_one_site(db, mutate):
    connector = lever_connector(db)
    config = copy.deepcopy(connector.config_json)
    mutate(config)
    with pytest.raises(SourcePolicyError) as raised:
        LeverPostingsProvider().fetch_page(
            FetchRequest(connector_id=connector.id, config=freeze(config), http=object())
        )
    assert raised.value.code == SourceErrorCode.CONNECTOR_MISCONFIGURED


def test_registration_is_validated_and_starts_disabled(db):
    with pytest.raises(ConnectorRegistrationError, match="Basis"):
        register_greenhouse_connector(
            db, name="x", board_token="exampleco", employer_name="E", basis="scraping",
            evidence_ref="r", reviewed_by="w", terms_version="v", actor="t",
        )
    with pytest.raises(ConnectorRegistrationError, match="careers host"):
        register_greenhouse_connector(
            db, name="x", board_token="exampleco", employer_name="E", basis="official_api",
            evidence_ref="r", reviewed_by="w", terms_version="v", actor="t",
            careers_host="http://bad/",
        )
    with pytest.raises(ConnectorRegistrationError, match="region"):
        register_lever_connector(
            db, name="y", site="example-site", employer_name="E", basis="official_api",
            evidence_ref="r", reviewed_by="w", terms_version="v", actor="t", region="mars",
        )
    connector = register_lever_connector(
        db, name="eu-site", site="example-site", employer_name="E", basis="official_api",
        evidence_ref="r", reviewed_by="w", terms_version="v", actor="t", region="eu",
    )
    assert connector.enabled is False and connector.policy_status == "review_required"
    assert connector.base_domain == "api.eu.lever.co"
    assert connector.config_json["allowed_url_hosts"] == ["jobs.eu.lever.co"]


def test_both_providers_are_blocked_under_default_settings(db):
    for connector in (greenhouse_connector(db), lever_connector(db)):
        server = MockServer([])
        with pytest.raises(SourcePolicyError) as raised:
            run_ingestion(
                db, ArtifactStorage(get_settings()), connector_id=connector.id,
                registry=default_registry(), http_client_factory=factory(server),
            )
        assert raised.value.code == SourceErrorCode.NETWORK_ACCESS_DISABLED
        assert server.requests == []


# --- pipeline -----------------------------------------------------------------------------


def test_greenhouse_run_ingests_valid_jobs_and_rejects_the_rest(db):
    connector = greenhouse_connector(db)
    server = MockServer(GREENHOUSE)
    result = run(db, connector, server, GREENHOUSE_API_HOST)
    assert result.status == "partial"
    jobs = postings(db, connector)
    assert set(jobs) == {"4100001", "4100002"}
    assert result.count("created") == 2
    # Careers-domain URL without a reviewed careers host, missing title, invalid id, wrong
    # host, in-response duplicate, and a non-object.
    assert result.count("rejected") == 6
    [request] = server.requests
    assert request.method == "GET" and request.content == b""
    assert str(request.url) == f"{greenhouse_board_url('exampleco')}?content=true"
    assert "authorization" not in request.headers and "cookie" not in request.headers
    assert jobs["4100001"].work_mode == "remote"

    # Text requirements come from the converted description.
    snapshot = db.scalars(
        select(JobSnapshot).where(JobSnapshot.job_id == jobs["4100001"].id)
    ).one()
    analysis = analyze_job_snapshot(db, snapshot)
    db.commit()
    assert analysis.structured_json["requirements_source"] == "text"
    assert analysis.structured_json["permitted_countries"] == []


def test_greenhouse_careers_host_is_accepted_only_when_reviewed(db):
    connector = greenhouse_connector(db, careers_host="careers.example-co.test")
    result = run(db, connector, MockServer(GREENHOUSE), GREENHOUSE_API_HOST)
    assert "4100003" in postings(db, connector)
    assert result.count("created") == 3


def test_greenhouse_partial_listing_never_marks_jobs_stale(db):
    connector = greenhouse_connector(db)
    run(db, connector, MockServer(GREENHOUSE), GREENHOUSE_API_HOST)
    shrunk = copy.deepcopy(GREENHOUSE)
    shrunk["jobs"] = [shrunk["jobs"][1]]
    shrunk["meta"] = {"total": 8}  # The board claims more jobs than it returned.
    run(db, connector, MockServer(shrunk), GREENHOUSE_API_HOST, minutes=10)
    db.expire_all()
    assert postings(db, connector)["4100001"].status == "active"
    shrunk["meta"] = {"total": 1}
    run(db, connector, MockServer(shrunk), GREENHOUSE_API_HOST, minutes=20)
    db.expire_all()
    assert postings(db, connector)["4100001"].status == "stale"


def test_lever_run_ingests_and_extracts_list_sections(db):
    connector = lever_connector(db)
    server = MockServer(LEVER)
    result = run(db, connector, server, LEVER_API_HOSTS["global"])
    jobs = postings(db, connector)
    assert set(jobs) == {SRE, FRONTEND, DESIGNER}
    assert result.count("rejected") == 3
    [request] = server.requests
    assert str(request.url) == f"{lever_postings_url('example-site', 'global')}?mode=json"
    snapshot = db.scalars(select(JobSnapshot).where(JobSnapshot.job_id == jobs[SRE].id)).one()
    assert snapshot.normalized_json["location_country"] == "IN"
    assert snapshot.normalized_json["compensation"]["currency"] == "INR"
    analysis = analyze_job_snapshot(db, snapshot)
    db.commit()
    kinds = {
        item.skill_text: item.requirement_kind
        for item in db.scalars(
            select(JobRequirement).where(JobRequirement.analysis_id == analysis.id)
        )
    }
    assert kinds["Terraform"] == "required" and kinds["Linux"] == "required"
    assert kinds["Prometheus"] == "preferred" and kinds["AWS"] == "informational"


def test_lever_rerun_is_idempotent(db):
    connector = lever_connector(db)
    run(db, connector, MockServer(LEVER), LEVER_API_HOSTS["global"])
    again = run(db, connector, MockServer(LEVER), LEVER_API_HOSTS["global"], minutes=10)
    assert again.count("created") == 0 and again.count("unchanged") == 3


def test_admin_cli_registers_greenhouse_and_lever(monkeypatch, capsys):
    monkeypatch.setattr(admin_cli, "SessionLocal", TestingSessionLocal)
    common = ["--employer", "Example Co", "--basis", "official_api", "--evidence-ref", "ref",
              "--reviewed-by", "reviewer", "--terms-version", "v1"]
    assert admin_cli.main(["register-greenhouse", "--name", "gh", "--board", "exampleco",
                           *common, "--approve"]) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["kind"] == "greenhouse_job_board" and created["enabled"] is False
    assert admin_cli.main(["register-lever", "--name", "lv", "--site", "example-site",
                           *common, "--region", "eu"]) == 0
    assert json.loads(capsys.readouterr().out)["policy_status"] == "review_required"
    assert admin_cli.main(["register-lever", "--name", "lv", "--site", "example-site",
                           *common]) == 1
    assert "already exists" in capsys.readouterr().err
    with TestingSessionLocal() as db:
        assert {c.kind for c in db.scalars(select(SourceConnector))} >= {
            "greenhouse_job_board", "lever_postings"
        }
