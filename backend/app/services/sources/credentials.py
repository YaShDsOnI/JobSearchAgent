"""Connector credential references.

Connector configuration never holds a secret value. It may hold a reference
(`{"credential": {"ref": "env:JFA_SOURCE_SECRET_<NAME>", "header": "Authorization",
"scheme": "Bearer"}}`) that is resolved from the process environment (or, later, a secret
manager) only when a run starts. Resolved values are registered for redaction and are
never logged, persisted, audited, or exposed through the API.
"""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.core.redaction import register_secret
from app.services.sources.errors import SourceErrorCode, SourcePolicyError

_SECRET_KEY = re.compile(
    r"(secret|token|password|passwd|api[_-]?key|apikey|authorization|cookie|session|"
    r"credential|private[_-]?key|bearer)",
    re.IGNORECASE,
)
_REF = re.compile(r"^env:(JFA_SOURCE_SECRET_[A-Z0-9_]{1,64})$")
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


def resolve_credential(config: Mapping[str, Any]) -> ConnectorCredential | None:
    spec = config.get("credential")
    if spec is None:
        return None
    if not isinstance(spec, Mapping):
        raise SourcePolicyError(
            SourceErrorCode.CREDENTIAL_UNAVAILABLE, "Credential reference is malformed"
        )
    match = _REF.match(str(spec.get("ref", "")))
    header = str(spec.get("header", "Authorization"))
    scheme = spec.get("scheme")
    if (
        match is None
        or not _HEADER.match(header)
        or header.lower() in FORBIDDEN_HEADERS
        or (scheme is not None and not re.fullmatch(r"[A-Za-z]{1,20}", str(scheme)))
    ):
        raise SourcePolicyError(
            SourceErrorCode.CREDENTIAL_UNAVAILABLE, "Credential reference is malformed"
        )
    value = os.environ.get(match.group(1), "")
    if not value:
        raise SourcePolicyError(
            SourceErrorCode.CREDENTIAL_UNAVAILABLE, "Referenced credential is not configured"
        )
    register_secret(value)
    full = f"{scheme} {value}" if scheme else value
    register_secret(full)
    return ConnectorCredential(header=header, value=SecretValue(full))
