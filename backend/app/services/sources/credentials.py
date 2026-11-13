"""Connector credential references.

Connector configuration never holds a secret value. It may hold a reference
(`{"credential": {"ref": "file:acme_token", "header": "Authorization", "scheme": "Bearer"}}`)
that is resolved only when a run starts:

- `file:<name>` reads `<SECRETS_DIR>/<name>`: the interface secret managers provide by
  projecting secrets as files (Docker/Kubernetes secrets, Vault Agent, cloud CSI drivers).
  Allowed in every environment; read on every run, so rotation needs no restart.
- `env:JFA_SOURCE_SECRET_<NAME>` reads the process environment. Development/test only:
  environment variables leak through process listings and container inspection.

Resolved values are registered for redaction and are never logged, persisted, audited, or
exposed through the API.
"""

import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.config import Settings, get_settings
from app.core.redaction import register_secret
from app.services.sources.errors import SourceErrorCode, SourcePolicyError

_SECRET_KEY = re.compile(
    r"(secret|token|password|passwd|api[_-]?key|apikey|authorization|cookie|session|"
    r"credential|private[_-]?key|bearer)",
    re.IGNORECASE,
)
_REF = re.compile(r"^env:(JFA_SOURCE_SECRET_[A-Z0-9_]{1,64})$")
_FILE_REF = re.compile(r"^file:([a-z0-9][a-z0-9_.-]{0,127})$")
MAX_SECRET_FILE_BYTES = 8192
_HEADER = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,63}$")
FORBIDDEN_HEADERS = frozenset({"host", "cookie", "content-length", "transfer-encoding"})


class SecretValue:
    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "SecretValue([REDACTED])"

    __str__ = __repr__


@dataclass(frozen=True)
class ConnectorCredential:
    header: str
    value: SecretValue

    def header_value(self) -> str:
        return self.value.reveal()


def find_inline_secrets(config: Any, path: str = "") -> list[str]:
    """Paths of config entries that look like literal secrets (only references allowed)."""
    found: list[str] = []
    if isinstance(config, Mapping):
        for key, value in config.items():
            child = f"{path}.{key}" if path else str(key)
            if isinstance(value, str) and value and _SECRET_KEY.search(str(key)):
                found.append(child)
            else:
                found.extend(find_inline_secrets(value, child))
    elif isinstance(config, list | tuple):
        for index, item in enumerate(config):
            found.extend(find_inline_secrets(item, f"{path}[{index}]"))
    return found


def _unavailable(message: str) -> SourcePolicyError:
    return SourcePolicyError(SourceErrorCode.CREDENTIAL_UNAVAILABLE, message)


def _read_secret_file(name: str, settings: Settings) -> str:
    if not settings.secrets_dir.strip():
        raise _unavailable("Secret directory is not configured")
    try:
        root = Path(settings.secrets_dir).resolve(strict=True)
        # Kubernetes projected secrets are symlinks inside the mount: resolve, then require
        # the target to stay inside the secrets directory.
        target = (root / name).resolve(strict=True)
        if not target.is_relative_to(root) or target == root:
            raise _unavailable("Secret reference is malformed")
        info = target.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SECRET_FILE_BYTES:
            raise _unavailable("Referenced credential is not a valid secret file")
        value = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise _unavailable("Referenced credential is not configured") from exc
    # Exactly one trailing line ending is tolerated (editors and `echo` add one).
    for ending in ("\r\n", "\n"):
        if value.endswith(ending):
            return value[: -len(ending)]
    return value


def resolve_secret_ref(
    ref: str, settings: Settings | None = None, *, env_pattern: re.Pattern[str] = _REF
) -> SecretValue:
    """Resolve a `file:` or (development/test only) `env:` reference to a redacted value."""
    settings = settings or get_settings()
    ref = str(ref or "")
    file_match = _FILE_REF.match(ref)
    env_match = env_pattern.match(ref)
    if file_match is not None:
        value = _read_secret_file(file_match.group(1), settings)
    elif env_match is not None:
        if not settings.is_development_like:
            raise _unavailable("Environment secret references are not allowed in this environment")
        value = os.environ.get(env_match.group(1), "")
    else:
        raise _unavailable("Credential reference is malformed")
    if not value or "\n" in value or "\r" in value:
        raise _unavailable("Referenced credential is not configured")
    register_secret(value)
    return SecretValue(value)


def resolve_credential(
    config: Mapping[str, Any], settings: Settings | None = None
) -> ConnectorCredential | None:
    spec = config.get("credential")
    if spec is None:
        return None
    if not isinstance(spec, Mapping):
        raise SourcePolicyError(
            SourceErrorCode.CREDENTIAL_UNAVAILABLE, "Credential reference is malformed"
        )
    ref = str(spec.get("ref", ""))
    header = str(spec.get("header", "Authorization"))
    scheme = spec.get("scheme")
    if (
        not (_REF.match(ref) or _FILE_REF.match(ref))
        or not _HEADER.match(header)
        or header.lower() in FORBIDDEN_HEADERS
        or (scheme is not None and not re.fullmatch(r"[A-Za-z]{1,20}", str(scheme)))
    ):
        raise SourcePolicyError(
            SourceErrorCode.CREDENTIAL_UNAVAILABLE, "Credential reference is malformed"
        )
    value = resolve_secret_ref(ref, settings).reveal()
    full = f"{scheme} {value}" if scheme else value
    register_secret(full)
    return ConnectorCredential(header=header, value=SecretValue(full))
