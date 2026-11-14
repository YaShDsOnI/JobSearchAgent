"""Phase 7: encrypted backup, verification, restore, retention, and deletion replay.

Restores go into the suite's own database after wiping it, so the same tests run on SQLite
and, with TEST_DATABASE_URL, on PostgreSQL built by the Alembic migrations.
"""

import base64
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, text

from app import backup as backup_cli
from app.core.config import get_settings
from app.db.models import (
    AuditEvent,
    Base,
    CVVersion,
    DataRequest,
    JobPosting,
    ProfileVersion,
    StoredArtifact,
    TrackedApplication,
)
from app.services import backup as backup_service
from app.services.backup import (
    BackupError,
    create_backup,
    decode_backup_key,
    load_backup,
    prune_backups,
    read_ledger,
    replay_deletions,
    restore_backup,
    update_deletion_ledger,
)
from app.services.storage import ArtifactStorage, decode_artifact_key, decrypt_blob
from tests.conftest import IS_SQLITE, TestingSessionLocal, csrf_headers, test_engine
from tests.test_cv_engine import _create_cv_profile
from tests.test_phase2_matching import register, seed_job

BACKUP_KEY_VALUE = base64.urlsafe_b64encode(b"b" * 32).decode()
BACKUP_KEY = base64.urlsafe_b64decode(BACKUP_KEY_VALUE)
EMAIL = "backup-owner@example.test"


def _artifact_key() -> bytes:
    return decode_artifact_key(get_settings().artifact_encryption_key)


def _storage_root() -> Path:
    return Path(get_settings().artifact_storage_path)


def _storage_at(root: Path) -> ArtifactStorage:
    return ArtifactStorage(get_settings().model_copy(update={"artifact_storage_path": str(root)}))


def _digests() -> dict[str, str]:
    with test_engine.connect() as connection:
        return {
            table.name: backup_service._read_table(connection, table)["digest"]
            for table in Base.metadata.sorted_tables
        }


def _wipe_database() -> None:
    if IS_SQLITE:
        for table in reversed(Base.metadata.sorted_tables):
            with test_engine.begin() as connection:
                connection.execute(table.delete())
    else:
        names = ", ".join(table.name for table in Base.metadata.sorted_tables)
        with test_engine.begin() as connection:
            connection.execute(text(f"TRUNCATE {names} CASCADE"))


def _seed_candidate(client) -> dict[str, str]:
    """A candidate with a CV (two encrypted artifacts), a tracker entry, and a job posting
    that points at another posting (a self-referencing foreign key)."""
    register(client, EMAIL)
    job_id = seed_job()
    other_job_id = seed_job(remote=True)
    _create_cv_profile(client)
    match = client.post(f"/api/v1/jobs/{job_id}/match", headers=csrf_headers(client)).json()
    created = client.post(
        f"/api/v1/jobs/{job_id}/cv-drafts",
        headers=csrf_headers(client),
        json={"match_run_id": match["id"]},
    )
    assert created.status_code == 201, created.text
    tracked = client.post(
        "/api/v1/tracker",
        headers=csrf_headers(client),
        json={
            "company": "Sanitized Example Employer",
            "role_title": "Example Data Analyst",
            "applied_on": "2026-11-01",
            "notes": "private note about the interview",
        },
    )
    assert tracked.status_code == 201, tracked.text
    with TestingSessionLocal() as db:
        posting = db.get(JobPosting, other_job_id)
        assert posting is not None
        posting.duplicate_of_job_id = job_id
        db.commit()
    return {
        "user_id": client.get("/api/v1/auth/me").json()["id"],
        "job_id": job_id,
        "cv_version_id": created.json()["id"],
    }


def _artifact_plaintexts(root: Path) -> dict[str, bytes]:
    with TestingSessionLocal() as db:
        rows = db.scalars(select(StoredArtifact)).all()
        return {
            row.object_key: decrypt_blob(
                _artifact_key(), row.object_key, (root / row.object_key).read_bytes()
            )
            for row in rows
        }


def test_backup_key_must_be_32_bytes_and_differ_from_the_artifact_key():
    artifact_value = get_settings().artifact_encryption_key
    with pytest.raises(BackupError, match="required"):
        decode_backup_key("", artifact_key_value=artifact_value)
    with pytest.raises(BackupError, match="32 bytes"):
        decode_backup_key(
            base64.urlsafe_b64encode(b"x" * 16).decode(), artifact_key_value=artifact_value
        )
    with pytest.raises(BackupError, match="differ"):
        decode_backup_key(artifact_value, artifact_key_value=artifact_value)
    assert decode_backup_key(BACKUP_KEY_VALUE, artifact_key_value=artifact_value) == BACKUP_KEY


def test_backup_restores_an_exact_copy_of_every_table_and_artifact(client, tmp_path):
    seeded = _seed_candidate(client)
    before = _digests()
    plaintexts = _artifact_plaintexts(_storage_root())
    assert len(plaintexts) >= 3  # job snapshots + the CV's PDF and DOCX

    result = create_backup(test_engine, _storage_root(), tmp_path / "backups", BACKUP_KEY)
    assert result.artifact_count == len(plaintexts)
    assert result.path.name.startswith("jfa-backup-")
    assert not list((tmp_path / "backups").glob("*.partial"))

    # Nothing readable about the candidate is left in plaintext on disk.
    manifest_text = (result.path / "manifest.json").read_text(encoding="utf-8")
    database_bytes = (result.path / "database.enc").read_bytes()
    for secret in (EMAIL, "private note about the interview", "Example Co", "20 reports"):
        assert secret not in manifest_text
        assert secret.encode() not in database_bytes

    verified = load_backup(result.path, BACKUP_KEY, artifact_key=_artifact_key())
    assert verified.artifacts_decrypted
    assert verified.summary()["rows"] == sum(result.table_counts.values())

    _wipe_database()
    target_root = tmp_path / "restored-artifacts"
    restored = restore_backup(test_engine, target_root, result.path, BACKUP_KEY, _artifact_key())
    assert restored.backup_id == result.backup_id
    assert _digests() == before
    assert _artifact_plaintexts(target_root) == plaintexts
    with TestingSessionLocal() as db:
        posting = db.scalar(select(JobPosting).where(JobPosting.duplicate_of_job_id.is_not(None)))
        assert posting is not None and posting.duplicate_of_job_id == seeded["job_id"]
        version = db.get(CVVersion, seeded["cv_version_id"])
        assert version is not None


def test_restore_refuses_any_target_that_is_not_empty(client, tmp_path):
    _seed_candidate(client)
    result = create_backup(test_engine, _storage_root(), tmp_path / "backups", BACKUP_KEY)
    before = _digests()

    with pytest.raises(BackupError, match="not empty"):
        restore_backup(test_engine, tmp_path / "fresh", result.path, BACKUP_KEY, _artifact_key())
    assert _digests() == before

    _wipe_database()
    occupied = tmp_path / "occupied"
    (occupied / "cv-pdf").mkdir(parents=True)
    (occupied / "cv-pdf" / "leftover.bin").write_bytes(b"x")
    with pytest.raises(BackupError, match="artifact storage is not empty"):
        restore_backup(test_engine, occupied, result.path, BACKUP_KEY, _artifact_key())
    with TestingSessionLocal() as db:
        assert db.scalars(select(StoredArtifact)).all() == []


def test_restore_refuses_a_different_schema_revision(client, tmp_path, monkeypatch):
    _seed_candidate(client)
    result = create_backup(test_engine, _storage_root(), tmp_path / "backups", BACKUP_KEY)
    _wipe_database()
    monkeypatch.setattr(backup_service, "_alembic_revision", lambda connection: "9999_future")
    with pytest.raises(BackupError, match="revision"):
        restore_backup(test_engine, tmp_path / "fresh", result.path, BACKUP_KEY, _artifact_key())


def test_failed_restore_leaves_nothing_behind(client, tmp_path, monkeypatch):
    _seed_candidate(client)
    result = create_backup(test_engine, _storage_root(), tmp_path / "backups", BACKUP_KEY)
    _wipe_database()
    original = backup_service._read_table

    def drifted(connection, table):
        data = original(connection, table)
        return {**data, "digest": "0" * 64} if table.name == "cv_versions" else data

    # Verification reads the backup, not the database, so only the read-back is affected.
    monkeypatch.setattr(backup_service, "_read_table", drifted)
    target_root = tmp_path / "fresh"
    with pytest.raises(BackupError, match="does not match the backup"):
        restore_backup(test_engine, target_root, result.path, BACKUP_KEY, _artifact_key())
    monkeypatch.setattr(backup_service, "_read_table", original)
    assert not list(target_root.rglob("*.bin"))
    with TestingSessionLocal() as db:
        assert db.scalars(select(StoredArtifact)).all() == []
        assert db.scalars(select(CVVersion)).all() == []


@pytest.mark.parametrize(
    "mutation, message",
    [
        ("manifest", "MAC"),
        ("database", "does not match the manifest"),
        ("artifact", "does not match the manifest"),
        ("missing_artifact", "missing"),
    ],
)
def test_verification_detects_tampering(client, tmp_path, mutation, message):
    _seed_candidate(client)
    result = create_backup(test_engine, _storage_root(), tmp_path / "backups", BACKUP_KEY)
    blobs = sorted((result.path / "artifacts").rglob("*.bin"))
    if mutation == "manifest":
        manifest = json.loads((result.path / "manifest.json").read_text(encoding="utf-8"))
        manifest["alembic_revision"] = "edited"
        (result.path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    elif mutation == "database":
        data = bytearray((result.path / "database.enc").read_bytes())
        data[-1] ^= 1
        (result.path / "database.enc").write_bytes(bytes(data))
    elif mutation == "artifact":
        data = bytearray(blobs[0].read_bytes())
        data[-1] ^= 1
        blobs[0].write_bytes(bytes(data))
    else:
        blobs[0].unlink()
    with pytest.raises(BackupError, match=message):
        load_backup(result.path, BACKUP_KEY, artifact_key=_artifact_key())


def test_wrong_keys_are_refused(client, tmp_path):
    _seed_candidate(client)
    result = create_backup(test_engine, _storage_root(), tmp_path / "backups", BACKUP_KEY)
    with pytest.raises(BackupError, match="MAC"):
        load_backup(result.path, b"c" * 32, artifact_key=_artifact_key())
    with pytest.raises(BackupError, match="ARTIFACT_ENCRYPTION_KEY"):
        load_backup(result.path, BACKUP_KEY, artifact_key=b"1" * 32)
    # Hashes alone still verify without the artifact key.
    assert not load_backup(result.path, BACKUP_KEY, artifact_key=None).artifacts_decrypted


def test_backup_with_a_missing_artifact_fails_and_leaves_no_directory(client, tmp_path):
    _seed_candidate(client)
    with TestingSessionLocal() as db:
        artifact = db.scalars(select(StoredArtifact)).first()
        assert artifact is not None
        object_key = artifact.object_key
    moved = tmp_path / "moved.bin"
    shutil.move(_storage_root() / object_key, moved)
    try:
        with pytest.raises(BackupError, match="missing"):
            create_backup(test_engine, _storage_root(), tmp_path / "backups", BACKUP_KEY)
    finally:
        shutil.move(moved, _storage_root() / object_key)
    assert list((tmp_path / "backups").iterdir()) == []


def test_restore_then_replay_removes_data_deleted_after_the_backup(client, tmp_path):
    seeded = _seed_candidate(client)
    result = create_backup(test_engine, _storage_root(), tmp_path / "backups", BACKUP_KEY)
    assert client.delete("/api/v1/profile", headers=csrf_headers(client)).status_code == 200
    ledger = tmp_path / "ledger" / "deletions.json"
    with TestingSessionLocal() as db:
        assert update_deletion_ledger(db, ledger, BACKUP_KEY) == {"entries": 1, "added": 1}

    # The whole deployment is lost and restored from the older backup.
    _wipe_database()
    target_root = tmp_path / "restored-artifacts"
    restore_backup(test_engine, target_root, result.path, BACKUP_KEY, _artifact_key())
    with TestingSessionLocal() as db:
        assert db.get(CVVersion, seeded["cv_version_id"]) is not None  # back, until replay

        replay = replay_deletions(
            db, _storage_at(target_root), read_ledger(ledger, BACKUP_KEY), actor="cli:test"
        )
        assert replay.summary() == {"replayed": 1, "already_applied": 0, "unknown_user": 0}
        user_id = seeded["user_id"]
        assert db.scalars(select(CVVersion).where(CVVersion.user_id == user_id)).all() == []
        assert (
            db.scalars(
                select(TrackedApplication).where(TrackedApplication.user_id == user_id)
            ).all()
            == []
        )
        owned = db.scalars(select(StoredArtifact).where(StoredArtifact.owner_user_id == user_id))
        assert owned.all() == []
        audit = db.scalars(
            select(AuditEvent).where(AuditEvent.action == "profile.deletion_replayed")
        ).one()
        assert (
            audit.metadata_json["replay_of"] == read_ledger(ledger, BACKUP_KEY)[0].data_request_id
        )

        again = replay_deletions(
            db, _storage_at(target_root), read_ledger(ledger, BACKUP_KEY), actor="cli:test"
        )
        assert again.summary() == {"replayed": 0, "already_applied": 1, "unknown_user": 0}
    # Owned blobs are gone from the restored store; shared job snapshots remain.
    remaining = {path.relative_to(target_root).as_posix() for path in target_root.rglob("*.bin")}
    assert all(key.startswith("job-snapshot/") for key in remaining)


def test_replay_keeps_data_created_after_the_deletion(client, tmp_path):
    seeded = _seed_candidate(client)
    assert client.delete("/api/v1/profile", headers=csrf_headers(client)).status_code == 200
    ledger = tmp_path / "deletions.json"
    with TestingSessionLocal() as db:
        update_deletion_ledger(db, ledger, BACKUP_KEY)
    _create_cv_profile(client)  # the candidate starts again after deleting
    user_id = seeded["user_id"]
    with TestingSessionLocal() as db:
        replay = replay_deletions(
            db, _storage_at(_storage_root()), read_ledger(ledger, BACKUP_KEY), actor="cli:test"
        )
        assert replay.summary()["already_applied"] == 1
        live_versions = db.scalars(
            select(ProfileVersion).where(
                ProfileVersion.user_id == user_id, ProfileVersion.deleted_at.is_(None)
            )
        ).all()
        assert len(live_versions) == 1


def test_ledger_is_authenticated_and_never_drops_entries(client, tmp_path):
    _seed_candidate(client)
    assert client.delete("/api/v1/profile", headers=csrf_headers(client)).status_code == 200
    ledger = tmp_path / "deletions.json"
    with TestingSessionLocal() as db:
        update_deletion_ledger(db, ledger, BACKUP_KEY)
    _wipe_database()
    with TestingSessionLocal() as db:
        # The database no longer remembers the deletion; the ledger still does.
        assert db.scalars(select(DataRequest)).all() == []
        assert update_deletion_ledger(db, ledger, BACKUP_KEY) == {"entries": 1, "added": 0}

    document = json.loads(ledger.read_text(encoding="utf-8"))
    document["entries"][0]["user_id"] = "00000000-0000-0000-0000-000000000000"
    ledger.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(BackupError, match="MAC"):
        read_ledger(ledger, BACKUP_KEY)


def test_prune_keeps_the_newest_backup_and_ignores_other_directories(client, tmp_path):
    out_root = tmp_path / "backups"
    now = datetime(2026, 11, 14, tzinfo=UTC)
    old = create_backup(
        test_engine, _storage_root(), out_root, BACKUP_KEY, now=now - timedelta(days=60)
    )
    older = create_backup(
        test_engine, _storage_root(), out_root, BACKUP_KEY, now=now - timedelta(days=90)
    )
    recent = create_backup(
        test_engine, _storage_root(), out_root, BACKUP_KEY, now=now - timedelta(days=2)
    )
    (out_root / "unrelated").mkdir()
    removed = prune_backups(out_root, keep_days=35, now=now)
    assert sorted(removed) == sorted([old.path.name, older.path.name])
    assert recent.path.exists() and (out_root / "unrelated").exists()

    # Even when every backup is past retention, the newest one stays.
    assert prune_backups(out_root, keep_days=1, now=now + timedelta(days=30)) == []
    with pytest.raises(BackupError):
        prune_backups(out_root, keep_days=0)


def test_cli_create_verify_restore_and_refusals(client, tmp_path, monkeypatch, capsys):
    _seed_candidate(client)
    settings = get_settings().model_copy(update={"backup_encryption_key": BACKUP_KEY_VALUE})
    monkeypatch.setattr(backup_cli, "get_settings", lambda: settings)
    monkeypatch.setattr(backup_cli, "engine", test_engine)
    monkeypatch.setattr(backup_cli, "SessionLocal", TestingSessionLocal)
    out_root = tmp_path / "backups"
    ledger = tmp_path / "deletions.json"

    assert backup_cli.main(["create", "--out-root", str(out_root), "--ledger", str(ledger)]) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["ledger"] == {"entries": 0, "added": 0}
    assert EMAIL not in json.dumps(created)
    path = created["path"]

    assert backup_cli.main(["verify", "--backup", path]) == 0
    assert json.loads(capsys.readouterr().out)["artifacts_decrypted"] is True

    # Into the live (non-empty) database: refused.
    assert backup_cli.main(["restore", "--backup", path, "--ledger", str(ledger)]) == 1
    assert "not empty" in capsys.readouterr().err
    # Skipping deletion replay needs a stated reason.
    assert backup_cli.main(["restore", "--backup", path, "--skip-deletion-replay"]) == 1
    assert "--reason" in capsys.readouterr().err

    with TestingSessionLocal() as db:
        assert (
            db.scalars(select(AuditEvent).where(AuditEvent.action == "backup.created"))
            .one()
            .metadata_json["backup_id"]
            == created["backup_id"]
        )

    _wipe_database()
    target_root = tmp_path / "restored"
    monkeypatch.setattr(
        backup_cli,
        "get_settings",
        lambda: settings.model_copy(update={"artifact_storage_path": str(target_root)}),
    )
    assert backup_cli.main(["restore", "--backup", path, "--ledger", str(ledger)]) == 0
    restored = json.loads(capsys.readouterr().out)
    assert restored["deletion_replay"] == {"replayed": 0, "already_applied": 0, "unknown_user": 0}
    with TestingSessionLocal() as db:
        assert (
            db.scalars(select(AuditEvent).where(AuditEvent.action == "backup.restored"))
            .one()
            .metadata_json["backup_id"]
            == created["backup_id"]
        )
