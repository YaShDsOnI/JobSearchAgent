"""Redaction of credentials and credential-shaped values from text and structured data.

Resolved secrets are registered here so they are scrubbed from every log line, error
detail, and audit event even if a provider or library echoes them back.
"""

import re
import threading
from collections.abc import Mapping
from typing import Any

REDACTED = "[REDACTED]"
_MIN_SECRET_LENGTH = 8
_lock = threading.Lock()
_registered: set[str] = set()

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(bearer|basic|token)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 " + REDACTED),
    (
        re.compile(
            r"(?i)\b(authorization|x-api-key|api[_-]?key|access[_-]?token|refresh[_-]?token|"
            r"client[_-]?secret|secret|password|passwd|token|signature|sig|cookie|session)"
            r"(\"?\s*[:=]\s*\"?)([^\s&\"',;}]+)"
        ),
        r"\1\2" + REDACTED,
    ),
)


def register_secret(value: str) -> None:
    if len(value) >= _MIN_SECRET_LENGTH:
        with _lock:
            _registered.add(value)


def redact(text: str) -> str:
    if not text:
        return text
    with _lock:
        secrets = sorted(_registered, key=len, reverse=True)
    for secret in secrets:
        if secret in text:
            text = text.replace(secret, REDACTED)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, Mapping):
        return {key: redact_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [redact_value(item) for item in value]
    return value
