import hashlib
import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.core.config import Settings
from app.core.keyring import (
    ArtifactKeyring,
    decode_artifact_key,
    decrypt_blob,
    encrypt_blob,
)

__all__ = ["ArtifactStorage", "decode_artifact_key", "decrypt_blob", "keyring_from_settings"]

# Temporary files written while re-encrypting a blob. They never match `*.bin`, so the
# orphan sweep and backups ignore them; key rotation removes stale ones.
ROTATION_SUFFIX = ".rotating"


def keyring_from_settings(settings: Settings) -> ArtifactKeyring:
    return ArtifactKeyring.from_values(
        settings.artifact_encryption_key, settings.artifact_previous_encryption_keys
    )


class ArtifactStorage:
    def __init__(self, settings: Settings):
        self.keyring = keyring_from_settings(settings)
        self.root = Path(settings.artifact_storage_path)
        self.root.mkdir(parents=True, exist_ok=True)
        self._root_resolved = self.root.resolve()

    def put_bytes(self, content: bytes, *, kind: str, content_type: str) -> dict[str, str | int]:
        """Encrypt and store a new blob under the current key. The result's
        `encryption_key_ref` belongs on the artifact's `stored_artifacts` row."""
        artifact_id = str(uuid4())
        object_key = f"{kind}/{artifact_id}.bin"
        destination = self.root / object_key
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(
            encrypt_blob(self.keyring.current, object_key, os.urandom(12), content)
        )
        return {
            "id": artifact_id,
            "object_key": object_key,
            "content_type": content_type,
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "encryption_key_ref": self.keyring.current_ref,
        }

    def put_json(self, value: dict, *, kind: str) -> dict[str, str | int]:
        return self.put_bytes(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            ),
            kind=kind,
            content_type="application/json",
        )

    def read_bytes(self, object_key: str) -> bytes:
        return self.read_with_key_ref(object_key)[0]

    def read_with_key_ref(self, object_key: str) -> tuple[bytes, str]:
        """(plaintext, ref of the key that decrypted it), trying the current key first."""
        path = self._path(object_key)
        if not path.is_file():
            raise FileNotFoundError(object_key)
        return self.keyring.decrypt(object_key, path.read_bytes())

    def rewrite_under_current_key(self, object_key: str, content: bytes) -> None:
        """Replace a blob with `content` encrypted under the current key, atomically.

        The object key, and so the artifact ID bound into the ciphertext, stays the same, and
        so does the plaintext. Readers see either the old or the new file, never a partial
        one. The caller holds the artifact row's lock (see `app.services.key_rotation`).
        """
        path = self._path(object_key)
        if not path.is_file():
            raise FileNotFoundError(object_key)
        temporary = path.with_name(f"{path.name}.{uuid4().hex[:8]}{ROTATION_SUFFIX}")
        try:
            with temporary.open("xb") as handle:
                handle.write(
                    encrypt_blob(self.keyring.current, object_key, os.urandom(12), content)
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def remove_stale_rotation_files(self, *, older_than: datetime) -> int:
        removed = 0
        for path in self._root_resolved.rglob(f"*{ROTATION_SUFFIX}"):
            modified = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            if path.is_file() and modified < older_than:
                path.unlink(missing_ok=True)
                removed += 1
        return removed

    def _path(self, object_key: str) -> Path:
        path = (self.root / object_key).resolve()
        if self._root_resolved not in path.parents:
            raise ValueError("Artifact object key escapes configured storage root")
        return path

    def iter_objects(self) -> Iterator[tuple[str, datetime]]:
        """Yield (object_key, modified_at) for every stored blob under the root."""
        for path in sorted(self._root_resolved.rglob("*.bin")):
            if path.is_file():
                object_key = path.relative_to(self._root_resolved).as_posix()
                yield object_key, datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)

    def exists(self, object_key: str) -> bool:
        path = (self.root / object_key).resolve()
        return self._root_resolved in path.parents and path.is_file()

    def delete(self, object_key: str) -> None:
        path = (self.root / object_key).resolve()
        if self._root_resolved not in path.parents:
            raise ValueError("Artifact object key escapes configured storage root")
        if path.exists() and path.is_file():
            path.unlink()
