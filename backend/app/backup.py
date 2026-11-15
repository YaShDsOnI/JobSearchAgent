"""Backup CLI. Needs the deployment environment plus BACKUP_ENCRYPTION_KEY.

    python -m app.backup create --out-root DIR --ledger FILE
    python -m app.backup verify --backup DIR [--no-decrypt-artifacts]
    python -m app.backup restore --backup DIR --ledger FILE
    python -m app.backup restore --backup DIR --skip-deletion-replay --reason "..."
    python -m app.backup update-ledger --ledger FILE
    python -m app.backup replay-deletions --ledger FILE
    python -m app.backup prune --out-root DIR --keep-days N

`restore` and `replay-deletions` act on the database in DATABASE_URL and the store in
ARTIFACT_STORAGE_PATH. Point them at the target, never at a live deployment you did not mean
to replace. Runbook: docs/runbooks.md, "Backup and restore". Output is JSON with no personal
data. Exit codes: 0 success, 1 refused or failed.
"""

import argparse
import getpass
import json
import sys
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.db.session import SessionLocal, engine
from app.services.audit import record_audit
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
from app.services.storage import ArtifactStorage, decode_artifact_key


def _actor() -> str:
    try:
        return f"cli:{getpass.getuser()}"[:64]
    except (KeyError, OSError):
        return "cli:unknown"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.backup")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="Write a new encrypted backup")
    create.add_argument("--out-root", type=Path, required=True)
    create.add_argument(
        "--ledger", type=Path, help="Also merge completed deletions into this ledger"
    )
    verify = commands.add_parser("verify", help="Check a backup without restoring it")
    verify.add_argument("--backup", type=Path, required=True)
    verify.add_argument(
        "--no-decrypt-artifacts",
        action="store_true",
        help="Check file hashes only (when ARTIFACT_ENCRYPTION_KEY is not available here)",
    )
    restore = commands.add_parser("restore", help="Restore into an empty, migrated target")
    restore.add_argument("--backup", type=Path, required=True)
    replay = restore.add_mutually_exclusive_group(required=True)
    replay.add_argument("--ledger", type=Path, help="Deletion ledger to replay after restore")
    replay.add_argument("--skip-deletion-replay", action="store_true")
    restore.add_argument("--reason", help="Required with --skip-deletion-replay")
    ledger = commands.add_parser("update-ledger", help="Merge completed deletions into a ledger")
    ledger.add_argument("--ledger", type=Path, required=True)
    replay_command = commands.add_parser(
        "replay-deletions", help="Re-apply ledger deletions to this database"
    )
    replay_command.add_argument("--ledger", type=Path, required=True)
    prune = commands.add_parser("prune", help="Delete backups past the retention period")
    prune.add_argument("--out-root", type=Path, required=True)
    prune.add_argument("--keep-days", type=int, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "restore" and args.skip_deletion_replay and not (args.reason or "").strip():
        print("error: --skip-deletion-replay needs --reason", file=sys.stderr)
        return 1
    try:
        print(json.dumps(_run(args), default=str, sort_keys=True))
    except BackupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def _run(args: argparse.Namespace) -> dict[str, Any]:
    settings = get_settings()
    if args.command == "prune":
        removed = prune_backups(args.out_root, keep_days=args.keep_days)
        with SessionLocal() as db:
            record_audit(
                db,
                action="backup.pruned",
                actor=_actor(),
                metadata={"removed": len(removed), "keep_days": args.keep_days},
            )
            db.commit()
        return {"removed": removed}

    backup_key = decode_backup_key(
        settings.backup_encryption_key, artifact_key_value=settings.artifact_encryption_key
    )
    storage_root = Path(settings.artifact_storage_path)

    if args.command == "create":
        result = create_backup(engine, storage_root, args.out_root, backup_key)
        summary = result.summary()
        with SessionLocal() as db:
            if args.ledger is not None:
                summary["ledger"] = update_deletion_ledger(db, args.ledger, backup_key)
            record_audit(
                db,
                action="backup.created",
                actor=_actor(),
                object_type="backup",
                metadata={
                    "backup_id": result.backup_id,
                    "rows": summary["rows"],
                    "artifacts": result.artifact_count,
                },
            )
            db.commit()
        return summary

    if args.command == "verify":
        artifact_key = (
            None
            if args.no_decrypt_artifacts
            else decode_artifact_key(settings.artifact_encryption_key)
        )
        return load_backup(args.backup, backup_key, artifact_key=artifact_key).summary()

    if args.command == "update-ledger":
        with SessionLocal() as db:
            return update_deletion_ledger(db, args.ledger, backup_key)

    if args.command == "replay-deletions":
        entries = read_ledger(args.ledger, backup_key)
        with SessionLocal() as db:
            return replay_deletions(
                db, ArtifactStorage(settings), entries, actor=_actor()
            ).summary()

    # restore
    ledger_entries = read_ledger(args.ledger, backup_key) if args.ledger is not None else None
    restored = restore_backup(
        engine,
        storage_root,
        args.backup,
        backup_key,
        decode_artifact_key(settings.artifact_encryption_key),
    )
    summary = restored.summary()
    with SessionLocal() as db:
        metadata: dict[str, Any] = {"backup_id": restored.backup_id, "rows": summary["rows"]}
        if ledger_entries is not None:
            replay = replay_deletions(db, ArtifactStorage(settings), ledger_entries, actor=_actor())
            summary["deletion_replay"] = replay.summary()
            metadata["deletion_replay"] = replay.summary()
        else:
            summary["deletion_replay"] = "skipped"
            metadata["deletion_replay_skipped_reason"] = args.reason.strip()[:500]
        record_audit(
            db, action="backup.restored", actor=_actor(), object_type="backup", metadata=metadata
        )
        db.commit()
    return summary


if __name__ == "__main__":
    raise SystemExit(main())
