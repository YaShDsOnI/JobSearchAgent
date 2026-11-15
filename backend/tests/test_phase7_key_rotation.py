"""Phase 7 (second increment): artifact key rotation and re-encryption.

The suite's artifact key (`KEY_A`) plays the old key and `KEY_B` the new one. Storage with
the new key is built from a settings copy, so the rest of the suite is unaffected.
"""

import base64
import json
import os
import threading
from datetime import timedelta
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidTag
from sqlalchemy import select, update

from app import admin as admin_cli
from app.core.config import Settings, get_settings
from app.core.keyring import (
    LEGACY_KEY_REF,
    ArtifactKeyring,
    artifact_key_id,
    artifact_key_ref,
    decode_artifact_key,
    decrypt_blob,
)
from app.db.models import AuditEvent, StoredArtifact, now_utc
from app.services.backup import (
    BackupError,
    create_backup,
    decode_backup_key,
    load_backup,
    restore_backup,
)
from app.services.key_rotation import (
    RotationReport,
    _rotate_one,
    artifact_key_status,
    rotate_artifact_keys,
)
from app.services.privacy import delete_candidate_data
from app.services.storage import ROTATION_SUFFIX, ArtifactStorage
from tests.conftest import TestingSessionLocal, requires_postgres, test_engine
from tests.test_phase7_backup import (
    BACKUP_KEY,
    EMAIL,
    _seed_candidate,
    _storage_root,
    _wipe_database,
)

KEY_A_VALUE = get_settings().artifact_encryption_key
KEY_A = decode_artifact_key(KEY_A_VALUE)
KEY_B_VALUE = base64.urlsafe_b64encode(b"r" * 32).decode()
KEY_B = decode_artifact_key(KEY_B_VALUE)
KEY_C = b"c" * 32
ID_A = artifact_key_id(KEY_A)
ID_B = artifact_key_id(KEY_B)


def _storage(current: str, previous: str = "", root: Path | None = None) -> ArtifactStorage:
    update_values = {
        "artifact_encryption_key": current,
        "artifact_previous_encryption_keys": previous,
    }
    if root is not None:
        update_values["artifact_storage_path"] = str(root)
    return ArtifactStorage(get_settings().model_copy(update=update_values))


def _rows() -> list[StoredArtifact]:
    with TestingSessionLocal() as db:
        return list(db.scalars(select(StoredArtifact).order_by(StoredArtifact.id)))


def _blob(row: StoredArtifact, root: Path | None = None) -> bytes:
    return ((root or _storage_root()) / row.object_key).read_bytes()


def _rotate(storage: ArtifactStorage, **kwargs) -> RotationReport:
    with TestingSessionLocal() as db:
        return rotate_artifact_keys(
            db, storage, actor="test", reason="scheduled rotation", **kwargs
        )


def _status(storage: ArtifactStorage) -> dict:
    with TestingSessionLocal() as db:
        return artifact_key_status(db, storage)


# --- key ring and configuration ---------------------------------------------------------


def test_key_ids_are_stable_distinct_and_do_not_reveal_the_key():
    assert artifact_key_id(KEY_A) == artifact_key_id(bytes(KEY_A))
    assert ID_A != ID_B and len(ID_A) == 16
    assert KEY_A.hex()[:16] != ID_A and base64.urlsafe_b64encode(KEY_A).decode()[:16] != ID_A
    assert artifact_key_ref(KEY_A) == f"local:aes-gcm:{ID_A}" != LEGACY_KEY_REF


def test_keyring_tries_every_key_and_never_accepts_a_wrong_one():
    ring = ArtifactKeyring(KEY_B, (KEY_A,))
    old = _storage(KEY_A_VALUE).put_bytes(b"secret", kind="test", content_type="text/plain")
    blob = (_storage_root() / str(old["object_key"])).read_bytes()
    assert ring.decrypt(str(old["object_key"]), blob) == (b"secret", artifact_key_ref(KEY_A))
    with pytest.raises(InvalidTag):
        ArtifactKeyring(KEY_B).decrypt(str(old["object_key"]), blob)
    # The ciphertext is bound to its artifact ID: moving it to another key fails.
    with pytest.raises(InvalidTag):
        ring.decrypt("test/00000000-0000-0000-0000-000000000000.bin", blob)
    with pytest.raises(RuntimeError, match="repeat"):
        ArtifactKeyring(KEY_A, (KEY_A,))
    with pytest.raises(RuntimeError, match="repeat"):
        ArtifactKeyring(KEY_B, (KEY_A, KEY_A))


def test_settings_validate_previous_keys():
    base = {"_env_file": None, "SESSION_SECRET": "x" * 40, "APP_ENV": "test",
            "ARTIFACT_ENCRYPTION_KEY": KEY_B_VALUE}
    for previous, message in (
        ("not base64!", "ARTIFACT_PREVIOUS_ENCRYPTION_KEYS"),
        (base64.urlsafe_b64encode(b"short").decode(), "ARTIFACT_PREVIOUS_ENCRYPTION_KEYS"),
        (KEY_B_VALUE, "repeat"),
        (f"{KEY_A_VALUE},{KEY_A_VALUE}", "repeat"),
    ):
        with pytest.raises(RuntimeError, match=message):
            Settings(**base, ARTIFACT_PREVIOUS_ENCRYPTION_KEYS=previous).validate_runtime_secrets()
    Settings(
        **base, ARTIFACT_PREVIOUS_ENCRYPTION_KEYS=f" {KEY_A_VALUE} , "
    ).validate_runtime_secrets()


def test_backup_key_must_differ_from_previous_artifact_keys():
    backup_value = base64.urlsafe_b64encode(BACKUP_KEY).decode()
    with pytest.raises(BackupError, match="ARTIFACT_PREVIOUS_ENCRYPTION_KEYS"):
        decode_backup_key(
            backup_value,
            artifact_key_value=KEY_B_VALUE,
            previous_artifact_key_values=f"{KEY_A_VALUE},{backup_value}",
        )
    assert (
        decode_backup_key(
            backup_value, artifact_key_value=KEY_B_VALUE, previous_artifact_key_values=KEY_A_VALUE
        )
        == BACKUP_KEY
    )


# --- labeling and status ----------------------------------------------------------------


def test_new_artifacts_record_the_key_that_encrypted_them(client):
    _seed_candidate(client)
    rows = _rows()
    assert rows and {row.encryption_key_ref for row in rows} == {artifact_key_ref(KEY_A)}
    status = _status(_storage(KEY_A_VALUE))
    assert status["artifacts"]["current"] == len(rows)
    assert status["previous_keys_still_needed"] is False

    new = _storage(KEY_B_VALUE, KEY_A_VALUE).put_bytes(b"x", kind="test", content_type="text/plain")
    assert new["encryption_key_ref"] == artifact_key_ref(KEY_B)


def test_status_counts_previous_legacy_and_unknown_keys(client):
    _seed_candidate(client)
    rows = _rows()
    with TestingSessionLocal() as db:
        db.execute(
            update(StoredArtifact)
            .where(StoredArtifact.id == rows[0].id)
            .values(encryption_key_ref=LEGACY_KEY_REF)
        )
        db.execute(
            update(StoredArtifact)
            .where(StoredArtifact.id == rows[1].id)
            .values(encryption_key_ref=artifact_key_ref(KEY_C))
        )
        db.commit()
    status = _status(_storage(KEY_B_VALUE, KEY_A_VALUE))
    assert status["current_key_id"] == ID_B
    assert status["previous_key_ids"] == [ID_A]
    assert status["artifacts"] == {
        "total": len(rows),
        "current": 0,
        "previous": {ID_A: len(rows) - 2},
        "unlabeled_legacy": 1,
        "unknown_key": {artifact_key_id(KEY_C): 1},
    }
    assert status["previous_keys_still_needed"] is True
    assert EMAIL not in json.dumps(status)


# --- rotation ---------------------------------------------------------------------------


def test_rotation_re_encrypts_every_blob_without_changing_its_content(client):
    _seed_candidate(client)
    before = {row.id: (row.object_key, row.sha256, _blob(row)) for row in _rows()}
    old = _storage(KEY_A_VALUE)
    plaintexts = {aid: old.read_bytes(key) for aid, (key, _, _) in before.items()}

    rotating = _storage(KEY_B_VALUE, KEY_A_VALUE)
    # During rotation every blob stays readable.
    assert all(rotating.read_bytes(key) == plaintexts[aid] for aid, (key, _, _) in before.items())

    report = _rotate(rotating)
    assert report.summary() | {"stale_temp_files_removed": 0} == {
        "current_key_id": ID_B,
        "rotated": len(before),
        "relabeled": 0,
        "skipped_deleted": 0,
        "missing_blob": 0,
        "undecryptable": 0,
        "integrity_mismatch": 0,
        "write_failed": 0,
        "stale_temp_files_removed": 0,
        "from_key_ids": {ID_A: len(before)},
    }
    new_only = _storage(KEY_B_VALUE)
    for row in _rows():
        object_key, digest, old_blob = before[row.id]
        assert row.object_key == object_key and row.sha256 == digest
        assert row.encryption_key_ref == artifact_key_ref(KEY_B)
        blob = _blob(row)
        assert blob != old_blob
        assert decrypt_blob(KEY_B, object_key, blob) == plaintexts[row.id]
        with pytest.raises(InvalidTag):
            decrypt_blob(KEY_A, object_key, blob)
        # The previous key is no longer needed to read anything.
        assert new_only.read_bytes(object_key) == plaintexts[row.id]
    assert _status(rotating)["previous_keys_still_needed"] is False
    assert not list(_storage_root().rglob(f"*{ROTATION_SUFFIX}"))

    # Idempotent: a second run finds nothing to do.
    assert _rotate(rotating).summary()["rotated"] == 0
    with TestingSessionLocal() as db:
        events = db.scalars(
            select(AuditEvent).where(AuditEvent.action == "artifacts.keys_rotated")
        ).all()
    assert len(events) == 2
    assert events[0].metadata_json["rotated"] == len(before)
    assert events[0].metadata_json["reason"] == "scheduled rotation"
    assert EMAIL not in json.dumps([event.metadata_json for event in events])


def test_legacy_rows_under_the_current_key_are_relabeled_not_rewritten(client):
    _seed_candidate(client)
    with TestingSessionLocal() as db:
        db.execute(update(StoredArtifact).values(encryption_key_ref=LEGACY_KEY_REF))
        db.commit()
    blobs = {row.id: _blob(row) for row in _rows()}
    report = _rotate(_storage(KEY_A_VALUE))
    assert (report.rotated, report.relabeled) == (0, len(blobs))
    assert all(_blob(row) == blobs[row.id] for row in _rows())
    assert {row.encryption_key_ref for row in _rows()} == {artifact_key_ref(KEY_A)}


def test_an_interrupted_rotation_is_finished_by_the_next_run(client):
    """A crash after the blob was replaced but before the commit: new blob, old label."""
    _seed_candidate(client)
    rotating = _storage(KEY_B_VALUE, KEY_A_VALUE)
    row = _rows()[0]
    rotating.rewrite_under_current_key(row.object_key, rotating.read_bytes(row.object_key))
    # The label still says the previous key, so the status never calls it unneeded early.
    status = _status(rotating)
    assert status["previous_keys_still_needed"] is True
    assert status["artifacts"]["previous"][ID_A] == len(_rows())

    report = _rotate(rotating)
    assert report.relabeled == 1 and report.rotated == len(_rows()) - 1
    assert _status(rotating)["previous_keys_still_needed"] is False


def test_limit_rotates_in_batches(client):
    _seed_candidate(client)
    rotating = _storage(KEY_B_VALUE, KEY_A_VALUE)
    total = len(_rows())
    assert _rotate(rotating, limit=1).rotated == 1
    assert _status(rotating)["artifacts"]["previous"][ID_A] == total - 1
    assert _rotate(rotating).rotated == total - 1
    with pytest.raises(ValueError):
        _rotate(rotating, limit=0)


def test_failures_leave_the_artifact_untouched_and_are_counted(client):
    _seed_candidate(client)
    rows = _rows()
    assert len(rows) >= 3
    missing, foreign, mismatched = rows[0], rows[1], rows[2]
    (_storage_root() / missing.object_key).unlink()
    foreign_blob = _storage(base64.urlsafe_b64encode(KEY_C).decode())
    content = _storage(KEY_A_VALUE).read_bytes(foreign.object_key)
    foreign_blob.rewrite_under_current_key(foreign.object_key, content)
    with TestingSessionLocal() as db:
        db.execute(
            update(StoredArtifact)
            .where(StoredArtifact.id == mismatched.id)
            .values(sha256="0" * 64)
        )
        db.commit()
    blobs = {row.id: _blob(row) for row in (foreign, mismatched)}

    report = _rotate(_storage(KEY_B_VALUE, KEY_A_VALUE))
    assert (report.missing_blob, report.undecryptable, report.integrity_mismatch) == (1, 1, 1)
    assert report.failures == 3 and report.rotated == len(rows) - 3
    after = {row.id: row for row in _rows()}
    for row in (missing, foreign, mismatched):
        assert after[row.id].encryption_key_ref == artifact_key_ref(KEY_A)
    assert all(_blob(after[aid]) == blob for aid, blob in blobs.items())
    assert not (_storage_root() / missing.object_key).exists()


def test_a_deleted_artifact_is_skipped_and_its_blob_never_comes_back(client):
    stored = _storage(KEY_A_VALUE).put_bytes(b"gone", kind="test", content_type="text/plain")
    with TestingSessionLocal() as db:
        db.add(
            StoredArtifact(
                id=str(stored["id"]),
                kind="test",
                object_key=str(stored["object_key"]),
                content_type="text/plain",
                size_bytes=4,
                sha256=str(stored["sha256"]),
                encryption_key_ref=str(stored["encryption_key_ref"]),
            )
        )
        db.commit()
    storage = _storage(KEY_B_VALUE, KEY_A_VALUE)
    # Deleted after the rotation listed it: rows first, then the blob.
    with TestingSessionLocal() as db:
        db.execute(StoredArtifact.__table__.delete().where(StoredArtifact.id == stored["id"]))
        db.commit()
    storage.delete(str(stored["object_key"]))
    report = RotationReport(current_key_id=ID_B)
    with TestingSessionLocal() as db:
        _rotate_one(db, storage, str(stored["id"]), report)
    assert report.skipped_deleted == 1 and report.rotated == 0
    assert not (_storage_root() / str(stored["object_key"])).exists()


def test_stale_rotation_temp_files_are_removed_and_never_treated_as_blobs(client):
    _seed_candidate(client)
    row = _rows()[0]
    path = _storage_root() / row.object_key
    stale = path.with_name(f"{path.name}.deadbeef{ROTATION_SUFFIX}")
    fresh = path.with_name(f"{path.name}.cafef00d{ROTATION_SUFFIX}")
    stale.write_bytes(b"x")
    fresh.write_bytes(b"x")
    old = (now_utc() - timedelta(hours=2)).timestamp()
    os.utime(stale, (old, old))
    storage = _storage(KEY_B_VALUE, KEY_A_VALUE)
    assert all(not key.endswith(ROTATION_SUFFIX) for key, _ in storage.iter_objects())
    assert _rotate(storage).stale_temp_files_removed == 1
    assert not stale.exists() and fresh.exists()
    fresh.unlink()


# --- backups across a rotation ----------------------------------------------------------


def test_backups_taken_before_a_rotation_need_the_previous_key(client, tmp_path):
    _seed_candidate(client)
    total = len(_rows())
    before = create_backup(test_engine, _storage_root(), tmp_path / "b", BACKUP_KEY)
    _rotate(_storage(KEY_B_VALUE, KEY_A_VALUE))
    after = create_backup(test_engine, _storage_root(), tmp_path / "b", BACKUP_KEY)

    ring = ArtifactKeyring(KEY_B, (KEY_A,))
    assert load_backup(before.path, BACKUP_KEY, artifact_key=ring).artifact_key_ids == {
        ID_A: total
    }
    with pytest.raises(BackupError, match="predate a key rotation"):
        load_backup(before.path, BACKUP_KEY, artifact_key=ArtifactKeyring(KEY_B))
    # A backup taken after the rotation is restorable with the new key alone.
    verified = load_backup(after.path, BACKUP_KEY, artifact_key=ArtifactKeyring(KEY_B))
    assert verified.artifact_key_ids == {ID_B: total}
    assert verified.summary()["artifact_key_ids"] == {ID_B: total}

    # Restore the pre-rotation backup with both keys, then finish the rotation there.
    _wipe_database()
    target = tmp_path / "restored"
    restore_backup(test_engine, target, before.path, BACKUP_KEY, ring)
    restored = _storage(KEY_B_VALUE, KEY_A_VALUE, root=target)
    assert _status(restored)["artifacts"]["previous"] == {ID_A: total}
    assert _rotate(restored).rotated == total
    assert _status(restored)["previous_keys_still_needed"] is False


# --- CLI --------------------------------------------------------------------------------


def test_cli_status_and_rotate(client, monkeypatch, capsys):
    _seed_candidate(client)
    settings = get_settings().model_copy(
        update={
            "artifact_encryption_key": KEY_B_VALUE,
            "artifact_previous_encryption_keys": KEY_A_VALUE,
        }
    )
    monkeypatch.setattr(admin_cli, "get_settings", lambda: settings)
    monkeypatch.setattr(admin_cli, "SessionLocal", TestingSessionLocal)
    total = len(_rows())

    assert admin_cli.main(["artifact-key-status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["previous_keys_still_needed"] is True
    assert status["artifacts"]["previous"] == {ID_A: total}

    with pytest.raises(SystemExit):
        admin_cli.main(["rotate-artifact-keys"])  # --reason is required
    capsys.readouterr()
    assert admin_cli.main(["rotate-artifact-keys", "--reason", " "]) == 1
    assert "--reason" in capsys.readouterr().err

    assert admin_cli.main(["rotate-artifact-keys", "--reason", "OPS-7 rotation"]) == 0
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["rotated"] == total
    assert result["status"]["previous_keys_still_needed"] is False
    assert EMAIL not in output and KEY_A_VALUE not in output and KEY_B_VALUE not in output

    # A failure is reported with exit code 5.
    row = _rows()[0]
    with TestingSessionLocal() as db:
        db.execute(
            update(StoredArtifact)
            .where(StoredArtifact.id == row.id)
            .values(encryption_key_ref=LEGACY_KEY_REF)
        )
        db.commit()
    (_storage_root() / row.object_key).unlink()
    assert admin_cli.main(["rotate-artifact-keys", "--reason", "retry"]) == 5
    assert json.loads(capsys.readouterr().out)["missing_blob"] == 1


# --- concurrency (PostgreSQL) -----------------------------------------------------------


@requires_postgres
def test_profile_deletion_waits_for_an_in_flight_rotation_and_leaves_no_blob(client, monkeypatch):
    ids = _seed_candidate(client)
    user_id = ids["user_id"]
    storage = _storage(KEY_B_VALUE, KEY_A_VALUE)
    # Only the candidate's own artifacts are left to rotate.
    with TestingSessionLocal() as db:
        db.execute(
            update(StoredArtifact)
            .where(StoredArtifact.owner_user_id.is_distinct_from(user_id))
            .values(encryption_key_ref=storage.keyring.current_ref)
        )
        db.commit()
    owned = [row.object_key for row in _rows() if row.owner_user_id == user_id]
    assert owned

    in_rewrite = threading.Event()
    release = threading.Event()
    gated_keys: list[str] = []
    original = storage.rewrite_under_current_key

    def gated(object_key: str, content: bytes) -> None:
        # The row is locked and the old blob read; pause just before replacing it.
        if not in_rewrite.is_set():
            gated_keys.append(object_key)
            in_rewrite.set()
            release.wait(10)
        original(object_key, content)

    monkeypatch.setattr(storage, "rewrite_under_current_key", gated)
    errors: list[BaseException] = []

    def rotate() -> None:
        try:
            _rotate(storage)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    def delete() -> None:
        try:
            with TestingSessionLocal() as db:
                delete_candidate_data(db, storage, user_id, actor="test")
                db.commit()
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    rotator = threading.Thread(target=rotate)
    rotator.start()
    assert in_rewrite.wait(10)
    deleter = threading.Thread(target=delete)
    deleter.start()
    deleter.join(1.0)
    assert deleter.is_alive(), "deletion must wait for the rotation's row lock"
    # Deletion locks rows before unlinking blobs, so it has not unlinked this one yet.
    assert (_storage_root() / gated_keys[0]).exists()
    release.set()
    rotator.join(20)
    deleter.join(20)
    assert not errors
    assert not rotator.is_alive() and not deleter.is_alive()
    with TestingSessionLocal() as db:
        assert not db.scalars(
            select(StoredArtifact).where(StoredArtifact.owner_user_id == user_id)
        ).all()
    assert not any((_storage_root() / key).exists() for key in owned)
