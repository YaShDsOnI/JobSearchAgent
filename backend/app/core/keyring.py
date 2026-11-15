"""Artifact encryption keys: the current key and the previous keys kept for rotation.

Every blob is `nonce || AES-GCM(plaintext)` with the artifact ID as associated data. New
blobs are always written under the current key (`ARTIFACT_ENCRYPTION_KEY`). Blobs written
under an earlier key stay readable while that key is listed in
`ARTIFACT_PREVIOUS_ENCRYPTION_KEYS`, until `python -m app.admin rotate-artifact-keys` has
re-encrypted them. AES-GCM authenticates, so trying the keys in turn can never return the
wrong plaintext: a wrong key fails with `InvalidTag`.

Each key is identified by a fingerprint (`artifact_key_id`). The fingerprint is a keyed hash
and does not reveal the key. `stored_artifacts.encryption_key_ref` records the key that
encrypted each blob as `local:aes-gcm:<fingerprint>`. Rows written before rotation existed
carry `local:aes-gcm:v1`, which names no particular key. Design: `docs/security.md`,
"Artifact key rotation".
"""

import base64
import hashlib
import hmac
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_REF_PREFIX = "local:aes-gcm:"
# Rows written before key rotation existed. They name no particular key.
LEGACY_KEY_REF = f"{KEY_REF_PREFIX}v1"
_KEY_ID_LABEL = b"jobfinderagent-artifact-key-id-v1"


def decode_artifact_key(value: str, *, name: str = "ARTIFACT_ENCRYPTION_KEY") -> bytes:
    try:
        key = base64.urlsafe_b64decode(value.strip().encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise RuntimeError(f"{name} must be URL-safe base64") from exc
    if len(key) not in {16, 24, 32}:
        raise RuntimeError(f"{name} must decode to 16, 24, or 32 bytes")
    return key


def decode_previous_artifact_keys(value: str) -> tuple[bytes, ...]:
    """The comma-separated `ARTIFACT_PREVIOUS_ENCRYPTION_KEYS` value, in order."""
    return tuple(
        decode_artifact_key(item, name="ARTIFACT_PREVIOUS_ENCRYPTION_KEYS")
        for item in value.split(",")
        if item.strip()
    )


def artifact_key_id(key: bytes) -> str:
    """A stable, non-reversible fingerprint of an artifact key (16 hex characters)."""
    return hmac.new(key, _KEY_ID_LABEL, hashlib.sha256).hexdigest()[:16]


def artifact_key_ref(key: bytes) -> str:
    return f"{KEY_REF_PREFIX}{artifact_key_id(key)}"


def _aad(object_key: str) -> bytes:
    # The artifact ID is the object key's file stem (`<kind>/<artifact id>.bin`).
    return Path(object_key).stem.encode("utf-8")


def encrypt_blob(key: bytes, object_key: str, nonce: bytes, content: bytes) -> bytes:
    return nonce + AESGCM(key).encrypt(nonce, content, _aad(object_key))


def decrypt_blob(key: bytes, object_key: str, encrypted: bytes) -> bytes:
    """Decrypt a stored blob (`nonce || AES-GCM ciphertext`, bound to its artifact ID)."""
    if len(encrypted) < 13:
        raise ValueError("Stored artifact is truncated")
    return AESGCM(key).decrypt(encrypted[:12], encrypted[12:], _aad(object_key))


@dataclass(frozen=True)
class ArtifactKeyring:
    current: bytes
    previous: tuple[bytes, ...] = ()

    def __post_init__(self) -> None:
        keys = [self.current, *self.previous]
        ids = [artifact_key_id(key) for key in keys]
        if len(set(ids)) != len(ids):
            raise RuntimeError(
                "ARTIFACT_PREVIOUS_ENCRYPTION_KEYS must not repeat a key or include "
                "ARTIFACT_ENCRYPTION_KEY"
            )

    @classmethod
    def from_values(cls, current: str, previous: str = "") -> "ArtifactKeyring":
        return cls(decode_artifact_key(current), decode_previous_artifact_keys(previous))

    @property
    def current_ref(self) -> str:
        return artifact_key_ref(self.current)

    @property
    def previous_refs(self) -> tuple[str, ...]:
        return tuple(artifact_key_ref(key) for key in self.previous)

    def keys(self) -> Iterator[tuple[str, bytes]]:
        """(key ref, key), current key first."""
        for key in (self.current, *self.previous):
            yield artifact_key_ref(key), key

    def contains(self, key: bytes) -> bool:
        return any(hmac.compare_digest(key, candidate) for _, candidate in self.keys())

    def decrypt(self, object_key: str, encrypted: bytes) -> tuple[bytes, str]:
        """(plaintext, ref of the key that decrypted it). Raises `InvalidTag` when no key
        in the ring decrypts the blob."""
        if len(encrypted) < 13:
            raise ValueError("Stored artifact is truncated")
        for ref, key in self.keys():
            try:
                return decrypt_blob(key, object_key, encrypted), ref
            except InvalidTag:
                continue
        raise InvalidTag()
