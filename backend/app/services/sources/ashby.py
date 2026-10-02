"""Ashby public job board provider (read-only discovery).

Uses only Ashby's documented public Job Posting API:

    GET https://api.ashbyhq.com/posting-api/job-board/{JOB_BOARD_NAME}[?includeCompensation=true]

(https://developers.ashbyhq.com/docs/public-job-posting-api). No authentication, no private
Ashby APIs, no page scraping, and no candidate/application endpoints. All network access
goes through the connector-bound client in `FetchRequest.http` (Phase 5A egress controls).

Everything returned is untrusted data. The mapping below copies only explicit fields and
leaves every eligibility fact the API does not state (sponsorship, relocation, work
authorization, remote geography, salary currency) unknown. URLs in the payload are stored
as data and never fetched.
"""

import html
import re
from collections.abc import Mapping, Sequence
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

from app.services.sources.base import FetchPage, FetchRequest, ProviderDescriptor
from app.services.sources.errors import (
    InvalidProviderResponseError,
    SourceErrorCode,
    SourcePolicyError,
)

ASHBY_API_HOST = "api.ashbyhq.com"
ASHBY_JOBS_HOST = "jobs.ashbyhq.com"
ASHBY_BOARD_PATH = "/posting-api/job-board/"
ASHBY_DOCS_URL = "https://developers.ashbyhq.com/docs/public-job-posting-api"

_BOARD_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_INTERVAL = re.compile(r"^1[ _-]?(YEAR|MONTH|WEEK|DAY|HOUR)S?$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")

ASHBY_DESCRIPTOR = ProviderDescriptor(
    kind="ashby_job_board",
    version="ashby-posting-api-provider-v1",
    display_name="Ashby public job board (Job Posting API)",
    allowed_bases=frozenset({"official_api", "explicit_authorization"}),
    requires_network=True,
    min_interval_seconds=300,
    max_pages=1,  # The documented endpoint returns the whole board; it is not paginated.
    max_records_per_run=2000,
)

WORKPLACE_TYPES = {"OnSite": "onsite", "Remote": "remote", "Hybrid": "hybrid"}
EMPLOYMENT_TYPES = {
    "FullTime": "full_time",
    "PartTime": "part_time",
    "Intern": "internship",
    "Contract": "contract",
    "Temporary": "temporary",
}
# Exact, explicit country statements only (ISO code or exact English name). Anything else
# stays unknown; no fuzzy matching, no geocoding, no inference from cities or regions.
COUNTRY_CODES = {
    "united states": "US", "united states of america": "US", "usa": "US", "us": "US",
    "united kingdom": "GB", "gb": "GB", "canada": "CA", "ca": "CA", "india": "IN", "in": "IN",
    "germany": "DE", "de": "DE", "france": "FR", "fr": "FR", "ireland": "IE", "ie": "IE",
    "netherlands": "NL", "nl": "NL", "spain": "ES", "es": "ES", "australia": "AU", "au": "AU",
    "singapore": "SG", "sg": "SG", "japan": "JP", "jp": "JP", "brazil": "BR", "br": "BR",
    "mexico": "MX", "mx": "MX", "poland": "PL", "pl": "PL", "portugal": "PT", "pt": "PT",
    "sweden": "SE", "se": "SE", "switzerland": "CH", "ch": "CH", "israel": "IL", "il": "IL",
    "new zealand": "NZ", "nz": "NZ", "italy": "IT", "it": "IT", "denmark": "DK", "dk": "DK",
    "norway": "NO", "no": "NO", "finland": "FI", "fi": "FI", "austria": "AT", "at": "AT",
    "belgium": "BE", "be": "BE",
}


def ashby_board_path(board_name: str) -> str:
    return f"{ASHBY_BOARD_PATH}{board_name}"


def ashby_board_url(board_name: str) -> str:
    return f"https://{ASHBY_API_HOST}{ashby_board_path(board_name)}"


def ashby_connector_config(
    board_name: str, employer_name: str, *, include_compensation: bool = True
) -> dict[str, Any]:
    """The reviewed configuration for one board. Binds egress to that exact endpoint."""
    if not _BOARD_NAME.match(board_name):
        raise ValueError("Invalid Ashby job board name")
    if not employer_name.strip() or len(employer_name) > 200:
        raise ValueError("Employer name is required")
    return {
        "board_name": board_name,
        "employer_name": employer_name.strip(),
        "include_compensation": include_compensation,
        "fetch_hosts": [ASHBY_API_HOST],
        "fetch_path_prefixes": [ashby_board_path(board_name)],
        "allowed_url_hosts": [ASHBY_JOBS_HOST],
    }


def _misconfigured(detail: str) -> SourcePolicyError:
    return SourcePolicyError(SourceErrorCode.CONNECTOR_MISCONFIGURED, detail)


def _validated_config(config: Mapping[str, Any]) -> tuple[str, str, bool]:
    board = config.get("board_name")
    employer = config.get("employer_name")
    include_compensation = config.get("include_compensation", False)
    if not isinstance(board, str) or not _BOARD_NAME.match(board):
        raise _misconfigured("Ashby board_name is missing or invalid")
    if not isinstance(employer, str) or not employer.strip() or len(employer) > 200:
        raise _misconfigured("Ashby employer_name is missing or invalid")
    if not isinstance(include_compensation, bool):
        raise _misconfigured("Ashby include_compensation must be a boolean")
    # The connector must be bound to exactly this board's endpoint, nothing wider.
    if tuple(config.get("fetch_hosts") or ()) != (ASHBY_API_HOST,):
        raise _misconfigured("Ashby fetch_hosts must be exactly the posting API host")
    if tuple(config.get("fetch_path_prefixes") or ()) != (ashby_board_path(board),):
        raise _misconfigured("Ashby fetch_path_prefixes must be exactly the board path")
    if tuple(config.get("allowed_url_hosts") or ()) != (ASHBY_JOBS_HOST,):
        raise _misconfigured("Ashby allowed_url_hosts must be exactly the jobs host")
    if "credential" in config:
        raise _misconfigured("The public Ashby posting API takes no credentials")
    return board, employer.strip(), include_compensation


class AshbyJobBoardProvider:
    descriptor = ASHBY_DESCRIPTOR

    def fetch_page(self, request: FetchRequest) -> FetchPage:
        board, employer, include_compensation = _validated_config(request.config)
        if request.http is None:
            raise SourcePolicyError(
                SourceErrorCode.NETWORK_ACCESS_DISABLED, "No bound network client"
            )
        url = ashby_board_url(board)
        params = {"includeCompensation": "true"} if include_compensation else None
        payload = request.http.get_json(url, params=params)
        if not isinstance(payload, Mapping) or not isinstance(payload.get("jobs"), list):
            raise InvalidProviderResponseError("Ashby response has no jobs list")
        records: list[Any] = []
        unlisted = 0
        for job in payload["jobs"]:
            if isinstance(job, Mapping) and job.get("isListed") is False:
                # "Only available via direct link": respect the employer's intent.
                unlisted += 1
                continue
            records.append(
                map_ashby_job(job, board=board, employer=employer)
                if isinstance(job, Mapping)
                else job  # Left for the pipeline to reject as record_not_object.
            )
        api_version = payload.get("apiVersion")
        return FetchPage(
            records=records,
            # The documented endpoint returns the board's full published listing in one
            # response, so absence marks a posting `stale` (reversible). The API has no
            # closed state, so nothing is ever marked closed or deleted from absence.
            listing_complete=True,
            source_url=url,
            metadata={
                "api_version": str(api_version) if api_version is not None else None,
                "unlisted_excluded": unlisted,
            },
        )


def _identity(job: Mapping[str, Any], board: str) -> str | None:
    """Posting UUID from the documented jobUrl; an undocumented `id` must agree."""
    from_url = None
    job_url = job.get("jobUrl")
    if isinstance(job_url, str):
        try:
            parts = urlsplit(job_url)
        except ValueError:
            parts = None
        if parts is not None and (parts.hostname or "").lower() == ASHBY_JOBS_HOST:
            segments = [segment for segment in parts.path.split("/") if segment]
            if (
                len(segments) >= 2
                and segments[0].lower() == board.lower()
                and _UUID.match(segments[1].lower())
            ):
                from_url = segments[1].lower()
    raw_id = job.get("id")
    from_id = raw_id.lower() if isinstance(raw_id, str) and _UUID.match(raw_id.lower()) else None
    if from_url and from_id and from_url != from_id:
        return None  # Conflicting identities: reject rather than guess.
    return from_url or from_id


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _work_mode(job: Mapping[str, Any]) -> str | None:
    workplace = WORKPLACE_TYPES.get(str(job.get("workplaceType")))
    is_remote = job.get("isRemote")
    if workplace is None:
        return "remote" if is_remote is True else None
    if is_remote is True and workplace != "remote":
        return None  # Contradictory statements stay unknown.
    return workplace


def _country(job: Mapping[str, Any], work_mode: str | None) -> str | None:
    # A remote role's address (often HQ) says nothing about where work may happen.
    if work_mode not in {"onsite", "hybrid"}:
        return None
    address = job.get("address")
    postal = address.get("postalAddress") if isinstance(address, Mapping) else None
    country = postal.get("addressCountry") if isinstance(postal, Mapping) else None
    if not isinstance(country, str):
        return None
    return COUNTRY_CODES.get(country.strip().lower())


def _secondary_locations(job: Mapping[str, Any]) -> list[str]:
    items = job.get("secondaryLocations")
    if not isinstance(items, Sequence) or isinstance(items, str):
        return []
    return [
        item["location"]
        for item in items
        if isinstance(item, Mapping) and isinstance(item.get("location"), str)
    ]


def _compensation(value: Any) -> dict[str, Any] | None:
    """Only an unambiguous single salary component; never converted or completed."""
    if not isinstance(value, Mapping):
        return None
    components = value.get("summaryComponents")
    if not isinstance(components, Sequence) or isinstance(components, str):
        return None
    salaries = [
        item
        for item in components
        if isinstance(item, Mapping) and item.get("compensationType") == "Salary"
    ]
    if len(salaries) != 1:
        return None
    salary = salaries[0]
    currency = salary.get("currencyCode")
    interval = salary.get("interval")
    period_match = _INTERVAL.match(interval.strip().upper()) if isinstance(interval, str) else None
    return {
        "min_amount": salary.get("minValue"),
        "max_amount": salary.get("maxValue"),
        "currency": currency if isinstance(currency, str) and _CURRENCY.match(currency) else None,
        "period": period_match.group(1).lower() if period_match else None,
    }


class _TextExtractor(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in {"script", "style"}:
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._skip:
            self._skip -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    """Text only: tags, scripts, styles, and link targets are dropped, never followed."""
    parser = _TextExtractor()
    parser.feed(markup)
    parser.close()
    return html.unescape("".join(parser.parts))


def _description(job: Mapping[str, Any]) -> str | None:
    plain = _text(job.get("descriptionPlain"))
    if plain:
        return plain
    markup = _text(job.get("descriptionHtml"))
    return _text(html_to_text(markup)) if markup else None


def map_ashby_job(job: Mapping[str, Any], *, board: str, employer: str) -> dict[str, Any]:
    """Map one Ashby job object to the pipeline's raw record format (explicit fields only).

    `source_payload` keeps the original object so the encrypted raw snapshot and its hash
    preserve exactly what Ashby returned.
    """
    work_mode = _work_mode(job)
    return {
        "source_job_id": _identity(job, board),
        "title": job.get("title"),
        "employer": employer,
        "canonical_url": job.get("jobUrl"),
        "location_text": job.get("location") if isinstance(job.get("location"), str) else None,
        "secondary_locations": _secondary_locations(job),
        "location_country": _country(job, work_mode),
        "work_mode": work_mode,
        "description": _description(job),
        "employment_type": EMPLOYMENT_TYPES.get(str(job.get("employmentType"))),
        "compensation": _compensation(job.get("compensation")),
        "posted_at": job.get("publishedAt") if isinstance(job.get("publishedAt"), str) else None,
        # Not provided by the API and therefore unknown: updated_at, closing_at, remote
        # geography, permitted countries, sponsorship, relocation, work authorization.
        "source_payload": dict(job),
    }
