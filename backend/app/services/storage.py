import base64
import hashlib
import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import Settings


def decode_artifact_key(value: str) -> bytes:
    try:
        key = base64.urlsafe_b64decode(value.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise RuntimeError("ARTIFACT_ENCRYPTION_KEY must be URL-safe base64") from exc
    if len(key) not in {16, 24, 32}:
        raise RuntimeError("ARTIFACT_ENCRYPTION_KEY must decode to 16, 24, or 32 bytes")
    return key


def decrypt_blob(key: bytes, object_key: str, encrypted: bytes) -> bytes:
    """Decrypt a stored blob (`nonce || AES-GCM ciphertext`, bound to its artifact ID)."""
    if len(encrypted) < 13:
        raise ValueError("Stored artifact is truncated")
    artifact_id = Path(object_key).stem
    return AESGCM(key).decrypt(encrypted[:12], encrypted[12:], artifact_id.encode("utf-8"))


class ArtifactStorage:
    def __init__(self, settings: Settings):
        key = decode_artifact_key(settings.artifact_encryption_key)
        self.root = Path(settings.artifact_storage_path)
        self.root.mkdir(parents=True, exist_ok=True)
        self._root_resolved = self.root.resolve()
        self._key = key

    def put_bytes(self, content: bytes, *, kind: str, content_type: str) -> dict[str, str | int]:
        artifact_id = str(uuid4())
        nonce = os.urandom(12)
        ciphertext = AESGCM(self._key).encrypt(nonce, content, artifact_id.encode("utf-8"))
        object_key = f"{kind}/{artifact_id}.bin"
        destination = self.root / object_key
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(nonce + ciphertext)
        return {
            "id": artifact_id,
            "object_key": object_key,
            "content_type": content_type,
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
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
        path = (self.root / object_key).resolve()
        if self._root_resolved not in path.parents:
            raise ValueError("Artifact object key escapes configured storage root")
        if not path.is_file():
            raise FileNotFoundError(object_key)
        return decrypt_blob(self._key, object_key, path.read_bytes())

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
