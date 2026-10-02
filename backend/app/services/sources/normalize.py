"""Deterministic normalization to the `job-normalized-v2` schema.

Rules: never infer. Only explicit source statements populate authorization, remote
geography, sponsorship, relocation, compensation, and status. Anything absent or
unparseable stays unknown (`None`, `"unknown"`, or `[]` per field) and a warning code is
recorded. The output keeps every key the Phase 2 analyzer reads.
"""

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from app.services.sources.validation import extract_source_job_id

# v2 (Phase 5B): adds `secondary_locations` (verbatim location strings only).
NORMALIZER_VERSION = "job-normalizer-v2"
SCHEMA_VERSION = "job-normalized-v2"

MAX_TITLE_LENGTH = 512
MAX_SHORT_TEXT_LENGTH = 512
MAX_DESCRIPTION_LENGTH = 100_000
MAX_LIST_ITEMS = 50
MAX_LIST_ITEM_LENGTH = 1_000
MAX_REQUIREMENTS = 200

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_COUNTRY = re.compile(r"^[A-Z]{2}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")

WORK_MODES = {
    "remote": "remote",
    "hybrid": "hybrid",
    "onsite": "onsite",
    "on-site": "onsite",
    "on_site": "onsite",
    "in-office": "onsite",
    "in_office": "onsite",
}
ACTIVE_STATUSES = {"active", "open", "published", "live"}
CLOSED_STATUSES = {"closed", "filled", "expired", "archived", "removed", "cancelled", "canceled"}
TRI_STATE_AVAILABLE = {"available", "yes", "true", "offered"}
TRI_STATE_NOT_AVAILABLE = {"not_available", "unavailable", "no", "false", "not_offered"}
PERIODS = {
    "year": "year",
    "yearly": "year",
    "annual": "year",
    "annually": "year",
    "month": "month",
    "monthly": "month",
    "week": "week",
    "weekly": "week",
    "day": "day",
    "daily": "day",
    "hour": "hour",
    "hourly": "hour",
}
REQUIREMENT_KEYS = ("skill", "kind", "importance", "minimum_years", "evidence")


class RecordRejected(ValueError):
    def __init__(self, codes: list[str]) -> None:
        self.codes = codes
        super().__init__(",".join(codes))


@dataclass(frozen=True)
class NormalizedRecord:
    source_job_id: str
    content: dict[str, Any]
    warnings: tuple[str, ...]
    content_hash: str
    raw_content_hash: str


def stable_hash(value: Any) -> str:
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class _Normalizer:
    def __init__(self, raw: Mapping[str, Any]) -> None:
        self.raw = raw
        self.warnings: list[str] = []
        self.rejections: list[str] = []

    def warn(self, code: str) -> None:
        if code not in self.warnings:
            self.warnings.append(code)

    def text(self, field: str, *, multiline: bool = False, max_length: int) -> str | None:
        value = self.raw.get(field)
        if value is None:
            return None
        if not isinstance(value, str):
            self.warn(f"{field}_invalid")
            return None
        cleaned = _CONTROL.sub("", value)
        if multiline:
            cleaned = re.sub(r"[ \t]+", " ", cleaned.replace("\r\n", "\n").replace("\r", "\n"))
            cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
        else:
            cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if not cleaned:
            return None
        if len(cleaned) > max_length:
            self.warn(f"{field}_too_long")
            return None
        return cleaned

    def required_text(self, field: str, max_length: int) -> str:
        value = self.raw.get(field)
        cleaned = ""
        if isinstance(value, str):
            cleaned = re.sub(r"\s+", " ", _CONTROL.sub("", value)).strip()
        if not cleaned:
            self.rejections.append(f"{field}_missing")
        elif len(cleaned) > max_length:
            self.rejections.append(f"{field}_too_long")
        return cleaned

    def country(self, value: Any, field: str) -> str | None:
        if value is None or value == "":
            return None
        if isinstance(value, str) and _COUNTRY.match(value.strip().upper()):
            return value.strip().upper()
        self.warn(f"{field}_invalid")
        return None

    def countries(self, field: str) -> list[str] | None:
        value = self.raw.get(field)
        if value is None:
            return None
        if not isinstance(value, list):
            self.warn(f"{field}_invalid")
            return None
        result: list[str] = []
        for item in value:
            code = self.country(item, field)
            if code and code not in result:
                result.append(code)
        return sorted(result)

    def string_list(self, field: str) -> list[str]:
        value = self.raw.get(field)
        if value is None:
            return []
        items = [value] if isinstance(value, str) else value
        if not isinstance(items, list):
            self.warn(f"{field}_invalid")
            return []
        result: list[str] = []
        for item in items[:MAX_LIST_ITEMS]:
            if not isinstance(item, str):
                self.warn(f"{field}_invalid")
                continue
            cleaned = re.sub(r"\s+", " ", _CONTROL.sub("", item)).strip()
            if cleaned and len(cleaned) <= MAX_LIST_ITEM_LENGTH:
                result.append(cleaned)
            elif cleaned:
                self.warn(f"{field}_too_long")
        if len(items) > MAX_LIST_ITEMS:
            self.warn(f"{field}_truncated")
        return result

    def tri_state(self, field: str) -> str:
        value = self.raw.get(field)
        if value is None:
            return "unknown"
        if isinstance(value, bool):
            return "available" if value else "not_available"
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in TRI_STATE_AVAILABLE:
                return "available"
            if lowered in TRI_STATE_NOT_AVAILABLE:
                return "not_available"
            if lowered in {"", "unknown"}:
                return "unknown"
        self.warn(f"{field}_unrecognized")
        return "unknown"

    def number(self, value: Any, field: str, *, maximum: float | None = None) -> float | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int | float):
            self.warn(f"{field}_invalid")
            return None
        number = float(value)
        if not math.isfinite(number) or number < 0 or (maximum is not None and number > maximum):
            self.warn(f"{field}_invalid")
            return None
        return number

    def timestamp(self, field: str) -> str | None:
        value = self.raw.get(field)
        if value is None or value == "":
            return None
        if not isinstance(value, str):
            self.warn(f"{field}_invalid")
            return None
        text = value.strip()
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError:
            pass
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            self.warn(f"{field}_invalid")
            return None
        if parsed.tzinfo is None:
            # Keep the stated local time; do not guess a timezone.
            self.warn(f"{field}_timezone_unknown")
            return parsed.replace(microsecond=0).isoformat()
        return parsed.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def work_mode(self) -> str:
        value = self.raw.get("work_mode")
        if value is None:
            return "unknown"
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in WORK_MODES:
                return WORK_MODES[lowered]
            if lowered in {"", "unknown"}:
                return "unknown"
        self.warn("work_mode_unrecognized")
        return "unknown"

    def status(self) -> str:
        value = self.raw.get("status")
        if value is None:
            return "active"  # Presence in the provider listing is the only signal.
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in ACTIVE_STATUSES:
                return "active"
            if lowered in CLOSED_STATUSES:
                return "closed"
        self.rejections.append("status_invalid")
        return "active"

    def compensation(self) -> dict[str, Any] | None:
        value = self.raw.get("compensation")
        if value is None:
            return None
        if not isinstance(value, Mapping):
            self.warn("compensation_invalid")
            return None
        minimum = self.number(
            value.get("min_amount", value.get("min")), "compensation_min_amount"
        )
        maximum = self.number(
            value.get("max_amount", value.get("max")), "compensation_max_amount"
        )
        if minimum is None and maximum is None:
            self.warn("compensation_amount_missing")
            return None
        if minimum is not None and maximum is not None and minimum > maximum:
            self.warn("compensation_range_invalid")
            return None
        currency_raw = value.get("currency")
        currency = None
        if isinstance(currency_raw, str) and _CURRENCY.match(currency_raw.strip().upper()):
            currency = currency_raw.strip().upper()
        else:
            self.warn("compensation_currency_unknown")
        period_raw = value.get("period", value.get("interval"))
        period = PERIODS.get(period_raw.strip().lower()) if isinstance(period_raw, str) else None
        if period is None:
            self.warn("compensation_period_unknown")
        return {
            "min_amount": minimum,
            "max_amount": maximum,
            "currency": currency,
            "period": period,
        }

    def requirements(self) -> list[dict[str, Any]]:
        value = self.raw.get("requirements")
        if value is None:
            return []
        if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
            self.rejections.append("requirements_invalid")
            return []
        if len(value) > MAX_REQUIREMENTS:
            self.rejections.append("requirements_too_many")
            return []
        result: list[dict[str, Any]] = []
        for item in value:
            normalized: dict[str, Any] = {}
            for key in REQUIREMENT_KEYS:
                if key not in item or item[key] is None:
                    continue
                entry = item[key]
                if key in {"importance", "minimum_years"}:
                    number = self.number(
                        entry, f"requirement_{key}", maximum=1.0 if key == "importance" else 60
                    )
                    if number is not None:
                        normalized[key] = number
                elif isinstance(entry, str):
                    normalized[key] = re.sub(r"\s+", " ", _CONTROL.sub("", entry)).strip()
                else:
                    self.warn(f"requirement_{key}_invalid")
            result.append(normalized)
        return result


def normalize_record(raw: Mapping[str, Any]) -> NormalizedRecord:
    source_job_id = extract_source_job_id(raw)
    if source_job_id is None:
        raise RecordRejected(["source_job_id_invalid"])
    n = _Normalizer(raw)
    title = n.required_text("title", MAX_TITLE_LENGTH)
    employer = n.required_text("employer", MAX_SHORT_TEXT_LENGTH)
    description = n.text("description", multiline=True, max_length=MAX_DESCRIPTION_LENGTH)
    if "description_too_long" in n.warnings:
        n.rejections.append("description_too_long")
    content: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "source_job_id": source_job_id,
        "title": title,
        "employer": employer,
        "canonical_url": str(raw.get("canonical_url", "")).strip(),
        "description": description,
        "location_text": n.text("location_text", max_length=MAX_SHORT_TEXT_LENGTH),
        # Additional stated locations, kept verbatim; never parsed into countries/geography.
        "secondary_locations": n.string_list("secondary_locations"),
        "location_country": n.country(raw.get("location_country"), "location_country"),
        "work_mode": n.work_mode(),
        "remote_geography": n.countries("remote_geography") or None,
        "permitted_countries": n.countries("permitted_countries") or [],
        "work_authorization_requirements": n.string_list("work_authorization_requirements"),
        "sponsorship": n.tri_state("sponsorship"),
        "relocation_assistance": n.tri_state("relocation_assistance"),
        "compensation": n.compensation(),
        "benefits": n.string_list("benefits"),
        "education": n.string_list("education"),
        "requirements": n.requirements(),
        "minimum_experience_years": n.number(
            raw.get("minimum_experience_years"), "minimum_experience_years", maximum=60
        ),
        "employment_type": n.text("employment_type", max_length=64),
        "posted_at": n.timestamp("posted_at"),
        "updated_at": n.timestamp("updated_at"),
        "closing_at": n.timestamp("closing_at"),
        "status": n.status(),
    }
    if n.rejections:
        raise RecordRejected(sorted(set(n.rejections)))
    return NormalizedRecord(
        source_job_id=source_job_id,
        content=content,
        warnings=tuple(sorted(n.warnings)),
        content_hash=stable_hash(content),
        raw_content_hash=stable_hash(raw),
    )
