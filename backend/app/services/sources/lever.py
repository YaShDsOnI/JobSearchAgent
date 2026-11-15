"""Lever public Postings API provider (read-only discovery).

Uses only Lever's documented public Postings API:

    GET https://api.lever.co/v0/postings/{site}?mode=json        (global instance)
    GET https://api.eu.lever.co/v0/postings/{site}?mode=json     (EU instance)

(https://github.com/lever/postings-api). Listing published postings needs no
authentication. The API's apply endpoint needs the employer's API key and is never used.
There is no page scraping, and all network access goes through the connector-bound client.

Mapping rules:

- Work mode comes from the structured `workplaceType`. "unspecified" stays unknown.
- The structured `country` code is used only for on-site and hybrid roles: a remote
  posting's country says where the job is listed, not where work is permitted.
- Pay comes only from a complete `salaryRange` (currency and interval). It is never
  converted.
- The description is the plain description, then the closing text, then each titled
  list (often "Requirements" or "Nice to have", which the text extractor reads as sections).
"""

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from app.services.sources.ashby import html_to_text
from app.services.sources.base import FetchPage, FetchRequest, ProviderDescriptor
from app.services.sources.errors import (
    InvalidProviderResponseError,
    SourceErrorCode,
    SourcePolicyError,
)

LEVER_API_HOSTS = {"global": "api.lever.co", "eu": "api.eu.lever.co"}
LEVER_JOB_HOSTS = {"global": "jobs.lever.co", "eu": "jobs.eu.lever.co"}
LEVER_DOCS_URL = "https://github.com/lever/postings-api"

_SITE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,99}$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_COUNTRY = re.compile(r"^[A-Z]{2}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")

LEVER_DESCRIPTOR = ProviderDescriptor(
    kind="lever_postings",
    version="lever-postings-api-provider-v1",
    display_name="Lever public job site (Postings API)",
    allowed_bases=frozenset({"official_api", "explicit_authorization"}),
    requires_network=True,
    min_interval_seconds=300,
    max_pages=1,  # Without `limit` the endpoint returns every published posting.
    max_records_per_run=2000,
)

WORKPLACE_TYPES = {"remote": "remote", "hybrid": "hybrid", "on-site": "onsite", "onsite": "onsite"}
COMMITMENTS = {
    "full-time": "full_time",
    "full time": "full_time",
    "part-time": "part_time",
    "part time": "part_time",
    "intern": "internship",
    "internship": "internship",
    "contract": "contract",
    "contractor": "contract",
    "temporary": "temporary",
}
SALARY_INTERVALS = {
    "per-year-salary": "year",
    "per-month-salary": "month",
    "per-week-salary": "week",
    "per-day-wage": "day",
    "per-hour-wage": "hour",
}


def lever_postings_path(site: str) -> str:
    return f"/v0/postings/{site}"


def lever_postings_url(site: str, region: str) -> str:
    return f"https://{LEVER_API_HOSTS[region]}{lever_postings_path(site)}"


def lever_connector_config(
    site: str, employer_name: str, *, region: str = "global"
) -> dict[str, Any]:
    if region not in LEVER_API_HOSTS:
        raise ValueError("Lever region must be 'global' or 'eu'")
    if not _SITE.match(site):
        raise ValueError("Invalid Lever site name")
    if not employer_name.strip() or len(employer_name) > 200:
        raise ValueError("Employer name is required")
    return {
        "site": site,
        "region": region,
        "employer_name": employer_name.strip(),
        "fetch_hosts": [LEVER_API_HOSTS[region]],
        "fetch_path_prefixes": [lever_postings_path(site)],
        "allowed_url_hosts": [LEVER_JOB_HOSTS[region]],
    }


def _misconfigured(detail: str) -> SourcePolicyError:
    return SourcePolicyError(SourceErrorCode.CONNECTOR_MISCONFIGURED, detail)


def _validated_config(config: Mapping[str, Any]) -> tuple[str, str, str]:
    site = config.get("site")
    region = config.get("region", "global")
    employer = config.get("employer_name")
    if region not in LEVER_API_HOSTS:
        raise _misconfigured("Lever region is invalid")
    if not isinstance(site, str) or not _SITE.match(site):
        raise _misconfigured("Lever site is missing or invalid")
    if not isinstance(employer, str) or not employer.strip() or len(employer) > 200:
        raise _misconfigured("Lever employer_name is missing or invalid")
    if tuple(config.get("fetch_hosts") or ()) != (LEVER_API_HOSTS[region],):
        raise _misconfigured("Lever fetch_hosts must be exactly the Postings API host")
    if tuple(config.get("fetch_path_prefixes") or ()) != (lever_postings_path(site),):
        raise _misconfigured("Lever fetch_path_prefixes must be exactly the site path")
    if tuple(config.get("allowed_url_hosts") or ()) != (LEVER_JOB_HOSTS[region],):
        raise _misconfigured("Lever allowed_url_hosts must be exactly the jobs host")
    if "credential" in config:
        raise _misconfigured("Listing public Lever postings takes no credentials")
    return site, region, employer.strip()


class LeverPostingsProvider:
    descriptor = LEVER_DESCRIPTOR

    def fetch_page(self, request: FetchRequest) -> FetchPage:
        site, region, employer = _validated_config(request.config)
        if request.http is None:
            raise SourcePolicyError(
                SourceErrorCode.NETWORK_ACCESS_DISABLED, "No bound network client"
            )
        url = lever_postings_url(site, region)
        payload = request.http.get_json(url, params={"mode": "json"})
        if not isinstance(payload, list):
            raise InvalidProviderResponseError("Lever response is not a list of postings")
        records = [
            map_lever_posting(item, site=site, region=region, employer=employer)
            if isinstance(item, Mapping)
            else item
            for item in payload
        ]
        return FetchPage(records=records, listing_complete=True, source_url=url)


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _identity(posting: Mapping[str, Any], site: str, region: str) -> str | None:
    """Posting UUID; the hosted URL, when present, must name the same site and ID."""
    raw = posting.get("id")
    posting_id = raw.lower() if isinstance(raw, str) and _UUID.match(raw.lower()) else None
    hosted = posting.get("hostedUrl")
    if posting_id and isinstance(hosted, str):
        try:
            parts = urlsplit(hosted)
        except ValueError:
            return None
        segments = [segment for segment in parts.path.split("/") if segment]
        if (
            (parts.hostname or "").lower() != LEVER_JOB_HOSTS[region]
            or len(segments) < 2
            or segments[0].lower() != site
            or segments[1].lower() != posting_id
        ):
            return None  # The URL points elsewhere: reject rather than guess.
    return posting_id


def _categories(posting: Mapping[str, Any]) -> Mapping[str, Any]:
    value = posting.get("categories")
    return value if isinstance(value, Mapping) else {}


def _work_mode(posting: Mapping[str, Any]) -> str | None:
    value = posting.get("workplaceType")
    return WORKPLACE_TYPES.get(value.strip().lower()) if isinstance(value, str) else None


def _country(posting: Mapping[str, Any], work_mode: str | None) -> str | None:
    if work_mode not in {"onsite", "hybrid"}:
        return None
    value = posting.get("country")
    return value if isinstance(value, str) and _COUNTRY.match(value) else None


def _secondary_locations(categories: Mapping[str, Any]) -> list[str]:
    items = categories.get("allLocations")
    primary = categories.get("location")
    if not isinstance(items, Sequence) or isinstance(items, str):
        return []
    return [item for item in items if isinstance(item, str) and item.strip() and item != primary]


def _compensation(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    currency = value.get("currency")
    period = SALARY_INTERVALS.get(str(value.get("interval", "")).strip().lower())
    if not isinstance(currency, str) or not _CURRENCY.match(currency) or period is None:
        return None
    return {
        "min_amount": value.get("min"),
        "max_amount": value.get("max"),
        "currency": currency,
        "period": period,
    }


def _description(posting: Mapping[str, Any]) -> str | None:
    parts: list[str] = []
    plain = _text(posting.get("descriptionPlain"))
    if plain:
        parts.append(plain.strip())
    elif _text(posting.get("description")):
        parts.append(html_to_text(str(posting["description"])).strip())
    # Closing text (benefits, equal-opportunity statements) goes before the titled lists so
    # it never falls under a "Requirements" or "Nice to have" heading.
    closing = _text(posting.get("additionalPlain"))
    if closing:
        parts.append(closing.strip())
    lists = posting.get("lists")
    if isinstance(lists, Sequence) and not isinstance(lists, str):
        for item in lists:
            if not isinstance(item, Mapping):
                continue
            heading = _text(item.get("text"))
            body = _text(item.get("content"))
            if heading:
                parts.append(heading.strip())
            if body:
                parts.append(html_to_text(body).strip())
    text = "\n".join(part for part in parts if part)
    return text or None


def _created_at(posting: Mapping[str, Any]) -> str | None:
    value = posting.get("createdAt")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def map_lever_posting(
    posting: Mapping[str, Any], *, site: str, region: str, employer: str
) -> dict[str, Any]:
    """Map one Lever posting to the pipeline's raw record (explicit fields only)."""
    categories = _categories(posting)
    work_mode = _work_mode(posting)
    commitment = categories.get("commitment")
    return {
        "source_job_id": _identity(posting, site, region),
        "title": posting.get("text"),
        "employer": employer,
        "canonical_url": posting.get("hostedUrl"),
        "location_text": _text(categories.get("location")),
        "secondary_locations": _secondary_locations(categories),
        "location_country": _country(posting, work_mode),
        "work_mode": work_mode,
        "description": _description(posting),
        "employment_type": COMMITMENTS.get(commitment.strip().lower())
        if isinstance(commitment, str)
        else None,
        "compensation": _compensation(posting.get("salaryRange")),
        "posted_at": _created_at(posting),
        # Not provided: permitted countries, sponsorship, relocation, work authorization,
        # closing date.
        "source_payload": dict(posting),
    }
