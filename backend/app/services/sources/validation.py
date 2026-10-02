"""Structural validation of untrusted provider output, before normalization."""

import json
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from app.services.sources.base import FetchPage
from app.services.sources.errors import InvalidProviderResponseError

MAX_RECORD_BYTES = 256_000
MAX_SOURCE_JOB_ID_LENGTH = 255
MAX_URL_LENGTH = 2048
MAX_CURSOR_LENGTH = 512
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


def validate_page(page: Any) -> FetchPage:
    if not isinstance(page, FetchPage):
        raise InvalidProviderResponseError("Provider did not return a page object")
    records = page.records
    if isinstance(records, str | bytes | Mapping) or not isinstance(records, list | tuple):
        raise InvalidProviderResponseError("Provider page records must be a list")
    cursor = page.next_cursor
    if cursor is not None and (
        not isinstance(cursor, str) or not cursor or len(cursor) > MAX_CURSOR_LENGTH
    ):
        raise InvalidProviderResponseError("Provider pagination cursor is invalid")
    if not isinstance(page.listing_complete, bool):
        raise InvalidProviderResponseError("Provider listing_complete flag is invalid")
    return page


def extract_source_job_id(raw: Any) -> str | None:
    if not isinstance(raw, Mapping):
        return None
    value = raw.get("source_job_id")
    if isinstance(value, bool) or not isinstance(value, str | int):
        return None
    text = str(value).strip()
    if (
        not text
        or len(text) > MAX_SOURCE_JOB_ID_LENGTH
        or _CONTROL_CHARACTERS.search(text)
    ):
        return None
    return text


def is_valid_job_url(value: Any) -> bool:
    if not isinstance(value, str) or not value or len(value) > MAX_URL_LENGTH:
        return False
    if _CONTROL_CHARACTERS.search(value) or any(ch.isspace() for ch in value):
        return False
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and bool(parts.hostname)
        and parts.username is None
        and parts.password is None
        and (port is None or 0 < port < 65536)
    )


def validate_record(raw: Any) -> list[str]:
    """Return rejection codes; an empty list means the record may be normalized."""
    if not isinstance(raw, Mapping):
        return ["record_not_object"]
    try:
        size = len(json.dumps(raw, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    except (TypeError, ValueError):
        return ["record_not_serializable"]
    if size > MAX_RECORD_BYTES:
        return ["record_too_large"]
    errors: list[str] = []
    if extract_source_job_id(raw) is None:
        missing = raw.get("source_job_id") in (None, "")
        errors.append("source_job_id_missing" if missing else "source_job_id_invalid")
    for field in ("title", "employer"):
        value = raw.get(field)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{field}_missing")
    url = raw.get("canonical_url")
    if url in (None, ""):
        errors.append("canonical_url_missing")
    elif not is_valid_job_url(url):
        errors.append("canonical_url_invalid")
    return errors
