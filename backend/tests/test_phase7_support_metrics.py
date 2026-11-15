"""Phase 7 remainder: support access grants, pilot metrics, and off-site backup copies."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app import admin as admin_cli
from app import backup as backup_cli
from app.core.config import get_settings
from app.db.models import AuditEvent, SupportAccessGrant, User
from app.services.backup import (
    BackupError,
    create_backup,
    load_backup,
    merge_ledger,
    read_ledger,
    replicate_backup,
    update_deletion_ledger,
)
from app.services.metrics import pilot_metrics, suppress
from app.services.operators import set_user_role
from tests.conftest import TestingSessionLocal, csrf_headers, test_engine
from tests.test_phase2_matching import register
from tests.test_phase7_backup import (
    BACKUP_KEY,
    BACKUP_KEY_VALUE,
    _artifact_key,
    _seed_candidate,
    _storage_root,
)

SECRET_FACT = "Kubernetes-secret-skill"
CANDIDATE = "support-candidate@example.test"
SUPPORT = "support-agent@example.test"


def _logout(client) -> None:
    client.post("/api/v1/auth/logout", headers=csrf_headers(client))


def _login(client, email: str) -> None:
    response = client.post("/api/v1/auth/login",
                           json={"email": email, "password": "phase2-secure-password-123"})
    assert response.status_code == 200, response.text


def _promote(email: str, role: str) -> None:
    with TestingSessionLocal() as db:
        set_user_role(db, email=email, role=role, actor="test", reason="test setup")
        db.commit()


def _setup(client) -> None:
    register(client, SUPPORT)
    _logout(client)
    _promote(SUPPORT, "support")
    register(client, CANDIDATE)
    client.post("/api/v1/profile/versions", headers=csrf_headers(client), json={
        "facts": [
            {"fact_type": "skill", "value": {"name": SECRET_FACT}, "provenance": "candidate",
             "confirmation_status": "confirmed"},
            {"fact_type": "contact", "value": {"phone": "+91 90000 11111"},
             "provenance": "candidate", "confirmation_status": "unknown"},
        ]})


def _grant(client, **overrides):
    body = {"support_email": SUPPORT, "hours": 24, "reason": "Cannot see my matches", **overrides}
    return client.post("/api/v1/support/grants", headers=csrf_headers(client), json=body)


# --- support access ----------------------------------------------------------------------


def test_candidate_grant_lets_support_see_only_a_redacted_audited_summary(client):
    _setup(client)
    grant = _grant(client)
    assert grant.status_code == 201, grant.text
    grant_id = grant.json()["id"]
    _logout(client)

    _login(client, SUPPORT)
    cases = client.get("/api/v1/support/cases").json()
    assert [case["grant_id"] for case in cases] == [grant_id]
    assert cases[0]["candidate"] == "s***@example.test"
    response = client.get(f"/api/v1/support/cases/{grant_id}")
    assert response.status_code == 200
    summary = response.json()
    assert summary["profile"]["facts_by_type"] == {"skill": {"confirmed": 1},
                                                   "contact": {"unknown": 1}}
    text = json.dumps(summary)
    for forbidden in (CANDIDATE, SECRET_FACT, "90000"):
        assert forbidden not in text
    _logout(client)

    _login(client, CANDIDATE)
    [listed] = client.get("/api/v1/support/grants").json()
    assert listed["view_count"] == 1 and listed["state"] == "active"
    with TestingSessionLocal() as db:
        event = db.scalars(
            select(AuditEvent).where(AuditEvent.action == "support.case_viewed")
        ).one()
        candidate_id = db.scalar(select(User.id).where(User.email == CANDIDATE))
        assert event.user_id == candidate_id and SECRET_FACT not in json.dumps(event.metadata_json)


def test_expired_revoked_and_foreign_grants_show_nothing(client):
    _setup(client)
    grant_id = _grant(client).json()["id"]
    _logout(client)
    register(client, "not-support@example.test")
    assert client.get("/api/v1/support/cases").status_code == 403
    assert client.get(f"/api/v1/support/cases/{grant_id}").status_code == 403
    _logout(client)

    register(client, "other-support@example.test")
    _logout(client)
    _promote("other-support@example.test", "support")
    _login(client, "other-support@example.test")
    assert client.get(f"/api/v1/support/cases/{grant_id}").status_code == 404
    _logout(client)

    with TestingSessionLocal() as db:
        db.get(SupportAccessGrant, grant_id).expires_at = datetime.now(UTC) - timedelta(minutes=1)
        db.commit()
    _login(client, SUPPORT)
    assert client.get("/api/v1/support/cases").json() == []
    assert client.get(f"/api/v1/support/cases/{grant_id}").status_code == 404
    _logout(client)

    _login(client, CANDIDATE)
    fresh = _grant(client).json()["id"]
    revoked = client.post(f"/api/v1/support/grants/{fresh}/revoke", headers=csrf_headers(client))
    assert revoked.json()["state"] == "revoked"
    _logout(client)
    _login(client, SUPPORT)
    assert client.get(f"/api/v1/support/cases/{fresh}").status_code == 404


def test_grant_validation(client):
    _setup(client)
    assert _grant(client, hours=0).status_code == 422
    assert _grant(client, hours=73).status_code == 422
    assert _grant(client, support_email="nobody@example.test").status_code == 400
    assert _grant(client, support_email=CANDIDATE).status_code == 400
    first = _grant(client).json()["id"]
    _grant(client)
    states = {item["id"]: item["state"] for item in client.get("/api/v1/support/grants").json()}
    assert states[first] == "revoked"  # A new grant replaces the old one.


def test_grants_are_exported_and_deleted_with_the_profile(client):
    _setup(client)
    _grant(client)
    exported = client.get("/api/v1/profile/export").json()
    assert len(exported["support_access_grants"]) == 1
    assert client.delete("/api/v1/profile", headers=csrf_headers(client)).status_code == 200
    with TestingSessionLocal() as db:
        assert db.scalars(select(SupportAccessGrant)).all() == []


def test_roles_change_only_through_the_cli(client, monkeypatch, capsys):
    register(client, "cli-support@example.test")
    monkeypatch.setattr(admin_cli, "SessionLocal", TestingSessionLocal)
    assert admin_cli.main(["promote-support", "--email", "cli-support@example.test",
                           "--reason", "OPS-1"]) == 0
    assert json.loads(capsys.readouterr().out)["role"] == "support"
    assert admin_cli.main(["demote-support", "--email", "cli-support@example.test",
                           "--reason", "OPS-2"]) == 0
    assert json.loads(capsys.readouterr().out)["role"] == "candidate"


# --- pilot metrics -----------------------------------------------------------------------


def test_pilot_metrics_are_operator_only_aggregate_and_suppress_small_counts(client, monkeypatch,
                                                                              capsys):
    _seed_candidate(client)
    assert client.get("/api/v1/ops/metrics").status_code == 403
    _logout(client)
    register(client, "metrics-operator@example.test")
    _promote("metrics-operator@example.test", "operator")
    metrics = client.get("/api/v1/ops/metrics").json()
    assert metrics["candidates"]["active"] == "<5"
    assert metrics["factual_edits"]["cv_versions_by_status"] == {"validated": "<5"}
    text = json.dumps(metrics)
    assert "@example.test" not in text
    assert suppress(0) == 0 and suppress(4) == "<5" and suppress(5) == 5

    monkeypatch.setattr(admin_cli, "SessionLocal", TestingSessionLocal)
    assert admin_cli.main(["pilot-metrics"]) == 0
    assert json.loads(capsys.readouterr().out)["metrics_version"] == "pilot-metrics-v1"
    with TestingSessionLocal() as db:
        assert pilot_metrics(db)["harmful_events"]["deletion_replays"] == 0


# --- off-site copies ---------------------------------------------------------------------


def test_replicate_verifies_copies_and_merges_ledgers(client, tmp_path):
    _seed_candidate(client)
    backups = tmp_path / "local"
    result = create_backup(test_engine, _storage_root(), backups, BACKUP_KEY)
    ledger = tmp_path / "ledger.json"
    with TestingSessionLocal() as db:
        update_deletion_ledger(db, ledger, BACKUP_KEY)
    target = tmp_path / "offsite"

    first = replicate_backup(result.path, target, BACKUP_KEY, artifact_key=_artifact_key(),
                             ledger=ledger)
    assert first["already_present"] is False
    copy = load_backup(target / result.path.name, BACKUP_KEY, artifact_key=_artifact_key())
    assert copy.backup_id == result.backup_id
    assert not list(target.glob("*.partial"))
    again = replicate_backup(result.path, target, BACKUP_KEY, artifact_key=_artifact_key())
    assert again["already_present"] is True

    # A ledger merge never drops the target's entries.
    client.delete("/api/v1/profile", headers=csrf_headers(client))
    other = tmp_path / "newer-ledger.json"
    with TestingSessionLocal() as db:
        update_deletion_ledger(db, other, BACKUP_KEY)
    merge_ledger(other, target / "deletion-ledger.json", BACKUP_KEY)
    merge_ledger(ledger, target / "deletion-ledger.json", BACKUP_KEY)
    assert len(read_ledger(target / "deletion-ledger.json", BACKUP_KEY)) == 1


def test_replicate_refuses_tampered_sources_and_unsafe_targets(client, tmp_path):
    _seed_candidate(client)
    result = create_backup(test_engine, _storage_root(), tmp_path / "local", BACKUP_KEY)
    with pytest.raises(BackupError, match="target must differ"):
        replicate_backup(result.path, result.path.parent, BACKUP_KEY, artifact_key=None)
    manifest = result.path / "manifest.json"
    manifest.write_text(manifest.read_text(encoding="utf-8").replace('"artifacts"', '"artifactz"'),
                        encoding="utf-8")
    target = tmp_path / "offsite"
    with pytest.raises(BackupError):
        replicate_backup(result.path, target, BACKUP_KEY, artifact_key=None)
    assert not target.exists() or not any(target.iterdir())


def test_replicate_cli_requires_an_approval_reference_and_audits(client, tmp_path, monkeypatch,
                                                                 capsys):
    _seed_candidate(client)
    result = create_backup(test_engine, _storage_root(), tmp_path / "local", BACKUP_KEY)
    settings = get_settings().model_copy(update={"backup_encryption_key": BACKUP_KEY_VALUE})
    monkeypatch.setattr(backup_cli, "get_settings", lambda: settings)
    monkeypatch.setattr(backup_cli, "SessionLocal", TestingSessionLocal)
    target = tmp_path / "offsite"
    base = ["replicate", "--backup", str(result.path), "--target-root", str(target)]
    with pytest.raises(SystemExit):
        backup_cli.main(base)
    capsys.readouterr()
    assert backup_cli.main([*base, "--approval-ref", " "]) == 1
    assert backup_cli.main([*base, "--approval-ref", "DPA-2026-11 storage target"]) == 0
    assert json.loads(capsys.readouterr().out)["backup_id"] == result.backup_id
    with TestingSessionLocal() as db:
        event = db.scalars(select(AuditEvent).where(AuditEvent.action == "backup.replicated")).one()
        assert event.metadata_json["approval_ref"] == "DPA-2026-11 storage target"
