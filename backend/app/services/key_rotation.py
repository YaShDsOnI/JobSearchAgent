"""Artifact key rotation: report which key protects each blob, and re-encrypt blobs onto
the current key (Phase 7, second increment).

Re-encryption changes only the ciphertext envelope. The object key, the artifact ID bound
into the ciphertext, the plaintext, and its recorded SHA-256 stay the same, so CV versions,
review packages, and submissions that reference an artifact are unaffected.

Per artifact, inside one transaction:

1. `UPDATE stored_artifacts SET encryption_key_ref = <current>` takes the row's lock (the
   database write lock on SQLite) before the blob is read. A deletion that starts
   afterwards waits for the commit. A row that is already gone matches nothing, so a
   deleted artifact's blob is never rewritten and brought back.
2. The blob is decrypted with the key ring and checked against the row's SHA-256 and size.
3. If an older key decrypted it, the blob is atomically replaced by one encrypted under the
   current key. If the current key already decrypted it (an earlier run stopped before its
   commit), only the label changes.
4. Commit.

A crash or failed commit after step 3 leaves the new blob with the old label. That is safe:
every configured key is tried on read, and the next run relabels the row. The opposite
state, a current label on an old blob, is never written. The status report therefore never
calls a previous key unneeded while a blob still depends on it.

Design: `docs/security.md`, "Artifact key rotation"; runbook: `docs/runbooks.md`.
"""

from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta
from hashlib import sha256
from typing import Any

from cryptography.exceptions import InvalidTag
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.keyring import KEY_REF_PREFIX, LEGACY_KEY_REF
from app.db.models import StoredArtifact, now_utc
from app.services.audit import record_audit
from app.services.storage import ArtifactStorage

# A rotation temp file this old belongs to a run that crashed: an atomic replace takes
# milliseconds.
STALE_ROTATION_FILE_AGE = timedelta(hours=1)


def _key_id(ref: str) -> str:
    return ref.removeprefix(KEY_REF_PREFIX)


def artifact_key_status(db: Session, storage: ArtifactStorage) -> dict[str, Any]:
    """Which key each `stored_artifacts` row records. IDs and counts only.

    `previous_keys_still_needed` is false only when every row records the current key.
    Backups taken before the rotation still need the previous keys to restore, for as long
    as the backup retention period.
    """
    keyring = storage.keyring
    counts = dict(
        db.execute(
            select(StoredArtifact.encryption_key_ref, func.count()).group_by(
                StoredArtifact.encryption_key_ref
            )
        ).all()
    )
    previous = {_key_id(ref): counts.pop(ref, 0) for ref in keyring.previous_refs}
    current = counts.pop(keyring.current_ref, 0)
    legacy = counts.pop(LEGACY_KEY_REF, 0)
    # Labels naming a key this deployment does not hold: those blobs cannot be read here.
    unknown = {_key_id(ref): count for ref, count in sorted(counts.items())}
    return {
        "current_key_id": _key_id(keyring.current_ref),
        "previous_key_ids": [_key_id(ref) for ref in keyring.previous_refs],
        "artifacts": {
            "total": current + legacy + sum(previous.values()) + sum(unknown.values()),
            "current": current,
            "previous": previous,
            "unlabeled_legacy": legacy,
            "unknown_key": unknown,
        },
        "previous_keys_still_needed": bool(legacy or any(previous.values()) or unknown),
    }


@dataclass
class RotationReport:
    current_key_id: str
    rotated: int = 0
    relabeled: int = 0
    skipped_deleted: int = 0
    missing_blob: int = 0
    undecryptable: int = 0
    integrity_mismatch: int = 0
    write_failed: int = 0
    stale_temp_files_removed: int = 0
    from_key_ids: Counter[str] = field(default_factory=Counter)

    @property
    def failures(self) -> int:
        return self.missing_blob + self.undecryptable + self.integrity_mismatch + self.write_failed

    def summary(self) -> dict[str, Any]:
        return {
            "current_key_id": self.current_key_id,
            "rotated": self.rotated,
            "relabeled": self.relabeled,
            "skipped_deleted": self.skipped_deleted,
            "missing_blob": self.missing_blob,
            "undecryptable": self.undecryptable,
            "integrity_mismatch": self.integrity_mismatch,
            "write_failed": self.write_failed,
            "stale_temp_files_removed": self.stale_temp_files_removed,
            "from_key_ids": dict(sorted(self.from_key_ids.items())),
        }


def rotate_artifact_keys(
    db: Session,
    storage: ArtifactStorage,
    *,
    actor: str,
    reason: str,
    limit: int | None = None,
) -> RotationReport:
    """Re-encrypt every artifact whose row does not record the current key. Idempotent and
    safe to interrupt; run it again until the status report shows no previous key needed.

    Failures (a missing blob, a blob no configured key decrypts, a plaintext that does not
    match its row, a write error) leave that artifact untouched and are counted. They are
    integrity incidents to investigate, never repaired here.
    """
    if limit is not None and limit < 1:
        raise ValueError("limit must be at least 1")
    keyring = storage.keyring
    current_ref = keyring.current_ref
    report = RotationReport(current_key_id=_key_id(current_ref))
    report.stale_temp_files_removed = storage.remove_stale_rotation_files(
        older_than=now_utc() - STALE_ROTATION_FILE_AGE
    )
    query = (
        select(StoredArtifact.id)
        .where(StoredArtifact.encryption_key_ref != current_ref)
        .order_by(StoredArtifact.id)
    )
    if limit is not None:
        query = query.limit(limit)
    artifact_ids = list(db.scalars(query))
    db.rollback()  # End the read transaction; each artifact gets its own.

    try:
        for artifact_id in artifact_ids:
            _rotate_one(db, storage, artifact_id, report)
    finally:
        record_audit(
            db,
            action="artifacts.keys_rotated",
            actor=actor,
            object_type="artifact_store",
            metadata={**report.summary(), "limit": limit, "reason": reason[:500]},
        )
        db.commit()
    return report


def _rotate_one(
    db: Session, storage: ArtifactStorage, artifact_id: str, report: RotationReport
) -> None:
    current_ref = storage.keyring.current_ref
    try:
        locked = db.execute(
            update(StoredArtifact)
            .where(
                StoredArtifact.id == artifact_id,
                StoredArtifact.encryption_key_ref != current_ref,
            )
            .values(encryption_key_ref=current_ref)
            .execution_options(synchronize_session=False)
        )
        if locked.rowcount == 0:  # type: ignore[attr-defined]
            # Deleted, or rotated by a concurrent run, since the list was read.
            db.rollback()
            report.skipped_deleted += 1
            return
        row = db.execute(
            select(StoredArtifact.object_key, StoredArtifact.sha256, StoredArtifact.size_bytes)
            .where(StoredArtifact.id == artifact_id)
        ).one()
        try:
            content, from_ref = storage.read_with_key_ref(row.object_key)
        except FileNotFoundError:
            db.rollback()
            report.missing_blob += 1
            return
        except (InvalidTag, ValueError):
            db.rollback()
            report.undecryptable += 1
            return
        if sha256(content).hexdigest() != row.sha256 or len(content) != row.size_bytes:
            db.rollback()
            report.integrity_mismatch += 1
            return
        if from_ref != current_ref:
            storage.rewrite_under_current_key(row.object_key, content)
        db.commit()
        if from_ref == current_ref:
            report.relabeled += 1
        else:
            report.rotated += 1
            report.from_key_ids[_key_id(from_ref)] += 1
    except OSError:
        db.rollback()
        report.write_failed += 1
    except BaseException:
        db.rollback()
        raise
