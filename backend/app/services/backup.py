"""Encrypted backup, verification, restore, retention, and deletion replay (Phase 7).

One backup is a directory named `jfa-backup-<YYYYMMDDTHHMMSSZ>-<8 hex>`:

    manifest.json          Plaintext, no personal data: format, backup ID, time, schema
                           revision, and the SHA-256 of every file. MAC'd with a key
                           derived from BACKUP_ENCRYPTION_KEY.
    database.enc           nonce || AES-GCM(gzip(canonical JSON of every application
                           table)), with per-table row digests inside the ciphertext.
    artifacts/<key>.bin    Each stored blob copied byte for byte. Blobs are already
                           AES-GCM encrypted under ARTIFACT_ENCRYPTION_KEY.

A directory is written as `<name>.partial` and renamed only when complete, so a crash never
leaves something that looks like a backup. Restore goes only into an empty, migrated
database at the same schema revision and an empty artifact store. It never merges or
overwrites. Every restore is followed by deletion replay from the deletion ledger, because
the backup can predate deletions the candidate already requested. Design:
`docs/runbooks.md`, "Backup and restore", and `docs/security.md`, "Backups".
"""

import base64
import gzip
import hashlib
import hmac
import json
import os
import re
import shutil
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import Column, Date, DateTime, Engine, Table, func, inspect, null, or_, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session
from sqlalchemy.types import JSON

from app.db.models import Base, DataRequest, User
from app.services.privacy import delete_candidate_data
from app.services.storage import ArtifactStorage, decode_artifact_key, decrypt_blob

FORMAT = "jobfinderagent-backup"
FORMAT_VERSION = 1
LEDGER_FORMAT = "jobfinderagent-deletion-ledger"
LEDGER_VERSION = 1
DIR_PREFIX = "jfa-backup-"
PARTIAL_SUFFIX = ".partial"
MANIFEST = "manifest.json"
DATABASE_FILE = "database.enc"
ARTIFACT_DIR = "artifacts"
_DIR_NAME = re.compile(r"^jfa-backup-\d{8}T\d{6}Z-[0-9a-f]{8}$")


class BackupError(Exception):
    """A backup, verification, or restore step refused or failed. Messages hold no data."""


# --- keys ------------------------------------------------------------------------------


def decode_backup_key(value: str, *, artifact_key_value: str) -> bytes:
    if not value:
        raise BackupError("BACKUP_ENCRYPTION_KEY is required")
    try:
        key = base64.urlsafe_b64decode(value.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise BackupError("BACKUP_ENCRYPTION_KEY must be URL-safe base64") from exc
    if len(key) != 32:
        raise BackupError("BACKUP_ENCRYPTION_KEY must decode to 32 bytes")
    try:
        artifact_key = decode_artifact_key(artifact_key_value)
    except RuntimeError:
        artifact_key = b""
    if hmac.compare_digest(key, artifact_key):
        raise BackupError("BACKUP_ENCRYPTION_KEY must differ from ARTIFACT_ENCRYPTION_KEY")
    return key


def _subkey(key: bytes, purpose: str) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=f"jobfinderagent-backup-v1:{purpose}".encode("ascii"),
    ).derive(key)


def _mac(key: bytes, purpose: str, document: dict[str, Any]) -> str:
    return hmac.new(_subkey(key, purpose), _canonical(document), hashlib.sha256).hexdigest()


def _check_mac(key: bytes, purpose: str, document: dict[str, Any], what: str) -> None:
    claimed = document.pop("mac", None)
    if not isinstance(claimed, str) or not hmac.compare_digest(
        claimed, _mac(key, purpose, document)
    ):
        raise BackupError(f"{what} MAC does not match: wrong BACKUP_ENCRYPTION_KEY or tampered")


# --- serialization ---------------------------------------------------------------------


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _app_tables() -> list[Table]:
    return list(Base.metadata.sorted_tables)


def _encode(value: Any) -> Any:
    # SQLite returns naive datetimes for timezone-aware columns; the app writes them in UTC.
    if isinstance(value, datetime):
        value = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _decode(column: Column[Any], value: Any) -> Any:
    if value is None:
        return None
    if isinstance(column.type, DateTime):
        return datetime.fromisoformat(value)
    if isinstance(column.type, Date):
        return date.fromisoformat(value)
    return value


def _read_table(connection: Connection, table: Table) -> dict[str, Any]:
    columns = list(table.columns)
    pk_index = [columns.index(column) for column in table.primary_key.columns]
    rows = [
        [_encode(row[column]) for column in columns]
        for row in connection.execute(select(table)).mappings()
    ]
    # Sorted in Python so the digest does not depend on the database's collation.
    rows.sort(key=lambda row: _canonical([row[index] for index in pk_index]))
    names = [column.name for column in columns]
    return {"columns": names, "rows": rows, "digest": _table_digest(names, rows)}


def _table_digest(columns: list[str], rows: Iterable[list[Any]]) -> str:
    digest = hashlib.sha256(_canonical(columns))
    for row in rows:
        digest.update(b"\n")
        digest.update(_canonical(row))
    return digest.hexdigest()


def _alembic_revision(connection: Connection) -> str | None:
    if not inspect(connection).has_table("alembic_version"):
        return None
    revisions: list[str] = list(
        connection.execute(text("SELECT version_num FROM alembic_version")).scalars()
    )
    if len(revisions) != 1:
        raise BackupError("alembic_version must hold exactly one revision")
    return str(revisions[0])


def _inside(root: Path, relative: str) -> Path:
    """`root/relative`, refusing anything that is not a `.bin` blob under `root`."""
    resolved_root = root.resolve()
    path = (root / relative).resolve()
    if resolved_root not in path.parents or path.suffix != ".bin":
        raise BackupError("artifact object key escapes the storage root")
    return path


# --- create ----------------------------------------------------------------------------


@dataclass(frozen=True)
class BackupResult:
    backup_id: str
    path: Path
    created_at: datetime
    table_counts: dict[str, int]
    artifact_count: int

    def summary(self) -> dict[str, Any]:
        return {
            "backup_id": self.backup_id,
            "path": str(self.path),
            "created_at": self.created_at.isoformat(),
            "rows": sum(self.table_counts.values()),
            "artifacts": self.artifact_count,
        }


def create_backup(
    engine: Engine,
    storage_root: Path,
    out_root: Path,
    backup_key: bytes,
    *,
    now: datetime | None = None,
) -> BackupResult:
    created_at = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    backup_id = uuid4().hex
    name = f"{DIR_PREFIX}{created_at:%Y%m%dT%H%M%SZ}-{backup_id[:8]}"
    out_root.mkdir(parents=True, exist_ok=True)
    final = out_root / name
    partial = out_root / f"{name}{PARTIAL_SUFFIX}"
    partial.mkdir()
    try:
        with engine.connect() as connection:
            if connection.dialect.name == "postgresql":
                # One consistent snapshot across every table.
                connection.execution_options(isolation_level="REPEATABLE READ")
            with connection.begin():
                dialect = connection.dialect.name
                revision = _alembic_revision(connection)
                tables = {table.name: _read_table(connection, table) for table in _app_tables()}

        artifacts = _copy_artifacts(tables["stored_artifacts"], storage_root, partial)
        payload = {
            "format": FORMAT,
            "format_version": FORMAT_VERSION,
            "backup_id": backup_id,
            "created_at": created_at.isoformat(),
            "alembic_revision": revision,
            "dialect": dialect,
            "tables": tables,
        }
        nonce = os.urandom(12)
        ciphertext = AESGCM(_subkey(backup_key, "database")).encrypt(
            nonce, gzip.compress(_canonical(payload), mtime=0), _aad(backup_id)
        )
        database_path = partial / DATABASE_FILE
        database_path.write_bytes(nonce + ciphertext)
        manifest: dict[str, Any] = {
            "format": FORMAT,
            "format_version": FORMAT_VERSION,
            "backup_id": backup_id,
            "created_at": created_at.isoformat(),
            "alembic_revision": revision,
            "database": {
                "file": DATABASE_FILE,
                "sha256": _sha256_file(database_path),
                "size_bytes": database_path.stat().st_size,
            },
            "artifacts": artifacts,
        }
        manifest["mac"] = _mac(backup_key, "manifest", manifest)
        (partial / MANIFEST).write_text(
            json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
        )
        partial.rename(final)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    return BackupResult(
        backup_id=backup_id,
        path=final,
        created_at=created_at,
        table_counts={name: len(data["rows"]) for name, data in tables.items()},
        artifact_count=len(artifacts),
    )


def _aad(backup_id: str) -> bytes:
    return f"{FORMAT}:{FORMAT_VERSION}:{backup_id}".encode("ascii")


def _copy_artifacts(
    stored_artifacts: dict[str, Any], storage_root: Path, destination: Path
) -> list[dict[str, Any]]:
    key_index = stored_artifacts["columns"].index("object_key")
    copied: list[dict[str, Any]] = []
    missing = 0
    for row in stored_artifacts["rows"]:
        object_key = row[key_index]
        source = _inside(storage_root, object_key)
        if not source.is_file():
            missing += 1
            continue
        target = _inside(destination / ARTIFACT_DIR, object_key)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        copied.append(
            {
                "object_key": object_key,
                "sha256": _sha256_file(target),
                "size_bytes": target.stat().st_size,
            }
        )
    if missing:
        raise BackupError(
            f"{missing} artifact file(s) referenced by the database are missing; "
            "run `python -m app.admin reconcile-artifacts` and investigate before backing up"
        )
    copied.sort(key=lambda item: item["object_key"])
    return copied


# --- verify ----------------------------------------------------------------------------


@dataclass
class VerifiedBackup:
    path: Path
    backup_id: str
    created_at: datetime
    alembic_revision: str | None
    tables: dict[str, dict[str, Any]]
    artifacts: list[dict[str, Any]]
    artifacts_decrypted: bool

    def summary(self) -> dict[str, Any]:
        return {
            "backup_id": self.backup_id,
            "created_at": self.created_at.isoformat(),
            "alembic_revision": self.alembic_revision,
            "rows": sum(len(data["rows"]) for data in self.tables.values()),
            "artifacts": len(self.artifacts),
            "artifacts_decrypted": self.artifacts_decrypted,
        }


def load_backup(path: Path, backup_key: bytes, *, artifact_key: bytes | None) -> VerifiedBackup:
    """Read and fully verify a backup. With `artifact_key`, every blob is also decrypted and
    checked against its row's plaintext SHA-256 and size."""
    manifest_path = path / MANIFEST
    if not manifest_path.is_file():
        raise BackupError("not a backup directory: manifest.json is missing")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise BackupError("manifest.json is not valid JSON") from exc
    if manifest.get("format") != FORMAT or manifest.get("format_version") != FORMAT_VERSION:
        raise BackupError("unsupported backup format")
    _check_mac(backup_key, "manifest", manifest, "manifest")

    database_path = path / DATABASE_FILE
    if not database_path.is_file() or _sha256_file(database_path) != manifest["database"]["sha256"]:
        raise BackupError("database.enc is missing or does not match the manifest")
    encrypted = database_path.read_bytes()
    try:
        plaintext = AESGCM(_subkey(backup_key, "database")).decrypt(
            encrypted[:12], encrypted[12:], _aad(manifest["backup_id"])
        )
    except (InvalidTag, ValueError) as exc:
        raise BackupError("database.enc does not decrypt with this BACKUP_ENCRYPTION_KEY") from exc
    payload = json.loads(gzip.decompress(plaintext))
    for name in ("backup_id", "created_at", "alembic_revision"):
        if payload.get(name) != manifest.get(name):
            raise BackupError(f"database.enc does not belong to this manifest ({name})")

    tables: dict[str, dict[str, Any]] = payload["tables"]
    expected = {table.name: [column.name for column in table.columns] for table in _app_tables()}
    if set(tables) != set(expected) or any(
        tables[name]["columns"] != columns for name, columns in expected.items()
    ):
        raise BackupError(
            "backup tables do not match this code's schema; restore with the release that "
            "matches the backup's alembic revision"
        )
    for name, data in tables.items():
        if _table_digest(data["columns"], data["rows"]) != data["digest"]:
            raise BackupError(f"table {name} does not match its digest")

    _verify_artifacts(path, manifest["artifacts"], tables["stored_artifacts"], artifact_key)
    return VerifiedBackup(
        path=path,
        backup_id=manifest["backup_id"],
        created_at=datetime.fromisoformat(manifest["created_at"]),
        alembic_revision=manifest["alembic_revision"],
        tables=tables,
        artifacts=manifest["artifacts"],
        artifacts_decrypted=artifact_key is not None,
    )


def _verify_artifacts(
    path: Path,
    listed: list[dict[str, Any]],
    stored_artifacts: dict[str, Any],
    artifact_key: bytes | None,
) -> None:
    columns = stored_artifacts["columns"]
    rows = {row[columns.index("object_key")]: row for row in stored_artifacts["rows"]}
    if {item["object_key"] for item in listed} != set(rows) or len(listed) != len(rows):
        raise BackupError("artifact files do not match the stored_artifacts rows")
    for item in listed:
        blob_path = _inside(path / ARTIFACT_DIR, item["object_key"])
        if not blob_path.is_file() or _sha256_file(blob_path) != item["sha256"]:
            raise BackupError("an artifact file is missing or does not match the manifest")
        if artifact_key is None:
            continue
        row = rows[item["object_key"]]
        try:
            content = decrypt_blob(artifact_key, item["object_key"], blob_path.read_bytes())
        except (InvalidTag, ValueError) as exc:
            raise BackupError(
                "an artifact does not decrypt with this ARTIFACT_ENCRYPTION_KEY"
            ) from exc
        if (
            hashlib.sha256(content).hexdigest() != row[columns.index("sha256")]
            or len(content) != row[columns.index("size_bytes")]
        ):
            raise BackupError("a decrypted artifact does not match its recorded SHA-256 and size")


# --- restore ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RestoreResult:
    backup_id: str
    backup_created_at: datetime
    table_counts: dict[str, int]
    artifact_count: int

    def summary(self) -> dict[str, Any]:
        return {
            "backup_id": self.backup_id,
            "backup_created_at": self.backup_created_at.isoformat(),
            "rows": sum(self.table_counts.values()),
            "artifacts": self.artifact_count,
        }


def restore_backup(
    engine: Engine,
    storage_root: Path,
    path: Path,
    backup_key: bytes,
    artifact_key: bytes,
) -> RestoreResult:
    """Restore into an empty, migrated database and an empty artifact store.

    The caller must then replay deletions (`replay_deletions`) before anyone uses the
    restored data.
    """
    backup = load_backup(path, backup_key, artifact_key=artifact_key)
    tables = _app_tables()
    with engine.connect() as connection:
        if _alembic_revision(connection) != backup.alembic_revision:
            raise BackupError(
                "target database schema revision differs from the backup's; migrate the "
                "empty target to the backup's revision first"
            )
        occupied = [
            table.name
            for table in tables
            if connection.execute(select(func.count()).select_from(table)).scalar_one()
        ]
    if occupied:
        raise BackupError(
            f"target database is not empty ({len(occupied)} table(s) have rows); restore only "
            "into a freshly migrated, empty database"
        )
    storage_root.mkdir(parents=True, exist_ok=True)
    if any(storage_root.rglob("*.bin")):
        raise BackupError("target artifact storage is not empty")

    copied: list[Path] = []
    try:
        for item in backup.artifacts:
            target = _inside(storage_root, item["object_key"])
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(_inside(path / ARTIFACT_DIR, item["object_key"]), target)
            copied.append(target)
        with engine.begin() as connection:
            for table in tables:
                _insert_table(connection, table, backup.tables[table.name])
            # Read everything back inside the transaction: commit only an exact copy.
            for table in tables:
                if _read_table(connection, table)["digest"] != backup.tables[table.name]["digest"]:
                    raise BackupError(f"restored table {table.name} does not match the backup")
    except BaseException:
        for target in copied:
            target.unlink(missing_ok=True)
        raise
    return RestoreResult(
        backup_id=backup.backup_id,
        backup_created_at=backup.created_at,
        table_counts={name: len(data["rows"]) for name, data in backup.tables.items()},
        artifact_count=len(backup.artifacts),
    )


def _insert_table(connection: Connection, table: Table, data: dict[str, Any]) -> None:
    if not data["rows"]:
        return
    columns = [table.c[name] for name in data["columns"]]
    rows = [
        {column.key: _decode(column, value) for column, value in zip(columns, row, strict=True)}
        for row in data["rows"]
    ]
    # Rows may point at rows of the same table (job_postings.duplicate_of_job_id): insert
    # them unlinked, then set the links once every row exists.
    self_refs = [fk.parent for fk in table.foreign_keys if fk.column.table is table]
    json_columns = [column for column in columns if isinstance(column.type, JSON)]
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for row in rows:
        values = {**row, **{column.key: None for column in self_refs}}
        # None in a JSON column is SQL NULL, not the JSON literal `null`.
        nulls = tuple(column.key for column in json_columns if values[column.key] is None)
        for key in nulls:
            del values[key]
        groups.setdefault(nulls, []).append(values)
    for nulls, params in groups.items():
        statement = table.insert()
        if nulls:
            statement = statement.values({key: null() for key in nulls})
        connection.execute(statement, params)
    if self_refs:
        primary_key = list(table.primary_key.columns)
        for row in rows:
            links = {column.key: row[column.key] for column in self_refs}
            if any(value is not None for value in links.values()):
                condition = [column == row[column.key] for column in primary_key]
                connection.execute(table.update().where(*condition).values(links))


# --- deletion ledger and replay --------------------------------------------------------


@dataclass(frozen=True)
class LedgerEntry:
    data_request_id: str
    user_id: str
    completed_at: datetime

    def as_json(self) -> dict[str, str]:
        return {
            "data_request_id": self.data_request_id,
            "user_id": self.user_id,
            "completed_at": _encode(self.completed_at),
        }


def read_ledger(path: Path, backup_key: bytes) -> list[LedgerEntry]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise BackupError("deletion ledger is missing or not valid JSON") from exc
    if document.get("format") != LEDGER_FORMAT or document.get("version") != LEDGER_VERSION:
        raise BackupError("unsupported deletion ledger format")
    _check_mac(backup_key, "ledger", document, "deletion ledger")
    return [
        LedgerEntry(
            data_request_id=item["data_request_id"],
            user_id=item["user_id"],
            completed_at=datetime.fromisoformat(item["completed_at"]),
        )
        for item in document["entries"]
    ]


def update_deletion_ledger(db: Session, path: Path, backup_key: bytes) -> dict[str, int]:
    """Merge every completed profile deletion into the ledger. Entries are never removed:
    the ledger has to outlive every backup it may be replayed against."""
    entries: dict[str, LedgerEntry] = {}
    if path.exists():
        entries = {entry.data_request_id: entry for entry in read_ledger(path, backup_key)}
    before = len(entries)
    for request in db.scalars(
        select(DataRequest).where(
            DataRequest.request_type == "profile_delete",
            DataRequest.status == "completed",
            DataRequest.completed_at.is_not(None),
        )
    ):
        assert request.completed_at is not None
        completed_at = request.completed_at
        if completed_at.tzinfo is None:
            completed_at = completed_at.replace(tzinfo=UTC)
        entries.setdefault(
            request.id,
            LedgerEntry(
                data_request_id=request.id, user_id=request.user_id, completed_at=completed_at
            ),
        )
    ordered = sorted(
        entries.values(), key=lambda entry: (entry.completed_at, entry.data_request_id)
    )
    document: dict[str, Any] = {
        "format": LEDGER_FORMAT,
        "version": LEDGER_VERSION,
        "updated_at": datetime.now(UTC).isoformat(),
        "entries": [entry.as_json() for entry in ordered],
    }
    document["mac"] = _mac(backup_key, "ledger", document)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)
    return {"entries": len(ordered), "added": len(ordered) - before}


@dataclass
class ReplayResult:
    replayed: list[str] = field(default_factory=list)
    already_applied: int = 0
    unknown_user: int = 0

    def summary(self) -> dict[str, int]:
        return {
            "replayed": len(self.replayed),
            "already_applied": self.already_applied,
            "unknown_user": self.unknown_user,
        }


def replay_deletions(
    db: Session,
    storage: ArtifactStorage,
    entries: Iterable[LedgerEntry],
    *,
    actor: str,
) -> ReplayResult:
    """Re-apply ledger deletions the database does not reflect. Idempotent.

    A deletion counts as applied when the database holds that data request, or any completed
    profile deletion for the same user at or after it: everything the user created later is
    legitimately theirs and stays.
    """
    result = ReplayResult()
    for entry in entries:
        if db.get(User, entry.user_id) is None:
            result.unknown_user += 1
            continue
        applied = db.scalar(
            select(DataRequest.id)
            .where(
                DataRequest.user_id == entry.user_id,
                DataRequest.request_type == "profile_delete",
                DataRequest.status == "completed",
                or_(
                    DataRequest.id == entry.data_request_id,
                    DataRequest.completed_at >= entry.completed_at,
                ),
            )
            .limit(1)
        )
        if applied is not None:
            result.already_applied += 1
            continue
        delete_candidate_data(
            db, storage, entry.user_id, actor=actor, replay_of=entry.data_request_id
        )
        db.commit()
        result.replayed.append(entry.data_request_id)
    return result


# --- retention -------------------------------------------------------------------------


def prune_backups(out_root: Path, *, keep_days: int, now: datetime | None = None) -> list[str]:
    """Delete backups older than `keep_days`, always keeping the newest complete one.

    Only directories this tool names are touched. A complete backup's age comes from its
    manifest; one whose manifest cannot be read is reported by the caller, never deleted.
    Abandoned `.partial` directories older than the cutoff are deleted by modification time.
    """
    if keep_days < 1:
        raise BackupError("keep_days must be at least 1")
    cutoff = (now or datetime.now(UTC)) - timedelta(days=keep_days)
    if not out_root.is_dir():
        return []
    complete: list[tuple[datetime, Path]] = []
    removed: list[str] = []
    for entry in sorted(out_root.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.endswith(PARTIAL_SUFFIX) and _DIR_NAME.match(
            entry.name.removesuffix(PARTIAL_SUFFIX)
        ):
            if datetime.fromtimestamp(entry.stat().st_mtime, tz=UTC) < cutoff:
                shutil.rmtree(entry)
                removed.append(entry.name)
            continue
        if not _DIR_NAME.match(entry.name):
            continue
        try:
            manifest = json.loads((entry / MANIFEST).read_text(encoding="utf-8"))
            if manifest.get("format") != FORMAT:
                continue
            complete.append((datetime.fromisoformat(manifest["created_at"]), entry))
        except (OSError, ValueError, KeyError, UnicodeDecodeError):
            continue
    complete.sort()
    for created_at, entry in complete[:-1]:
        if created_at < cutoff:
            shutil.rmtree(entry)
            removed.append(entry.name)
    return removed
