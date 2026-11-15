"""Greenhouse public Job Board API provider (read-only discovery).

Uses only Greenhouse's documented public Job Board API:

    GET https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true

(https://developers.greenhouse.io/job-board.html). Listing jobs needs no authentication.
The application-submission endpoint of the same API needs the employer's own API key and is
never used. There is no page scraping and no private API, and all network access goes
through the connector-bound client (Phase 5A egress controls).

The API has no structured workplace type, permitted geography, sponsorship, or salary
field. Work mode is set only from an explicit leading "Remote" or a "Hybrid" word in the
location name. Everything else, including the country, stays unknown. Descriptions arrive
as HTML-escaped HTML and are reduced to text; links in them are never followed.
"""

import html
import re
from collections.abc import Mapping, Sequence
from typing import Any

from app.services.sources.ashby import html_to_text
from app.services.sources.base import FetchPage, FetchRequest, ProviderDescriptor
from app.services.sources.errors import (
    InvalidProviderResponseError,
    SourceErrorCode,
    SourcePolicyError,
)

GREENHOUSE_API_HOST = "boards-api.greenhouse.io"
GREENHOUSE_JOB_HOSTS = ("boards.greenhouse.io", "job-boards.greenhouse.io")
GREENHOUSE_DOCS_URL = "https://developers.greenhouse.io/job-board.html"

_BOARD_TOKEN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,99}$")
_HOST = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_REMOTE = re.compile(r"^\s*remote\b", re.IGNORECASE)
_HYBRID = re.compile(r"\bhybrid\b", re.IGNORECASE)

GREENHOUSE_DESCRIPTOR = ProviderDescriptor(
    kind="greenhouse_job_board",
    version="greenhouse-job-board-api-provider-v1",
    display_name="Greenhouse public job board (Job Board API)",
    allowed_bases=frozenset({"official_api", "explicit_authorization"}),
    requires_network=True,
    min_interval_seconds=300,
    max_pages=1,  # The jobs endpoint returns the whole board; it is not paginated.
    max_records_per_run=2000,
)


def greenhouse_board_path(board_token: str) -> str:
    return f"/v1/boards/{board_token}/jobs"


def greenhouse_board_url(board_token: str) -> str:
    return f"https://{GREENHOUSE_API_HOST}{greenhouse_board_path(board_token)}"


def greenhouse_connector_config(
    board_token: str, employer_name: str, *, careers_host: str | None = None
) -> dict[str, Any]:
    """The reviewed configuration for one board. `careers_host` is the employer's own
    careers domain when its postings link there instead of greenhouse.io."""
    if not _BOARD_TOKEN.match(board_token):
        raise ValueError("Invalid Greenhouse board token")
    if not employer_name.strip() or len(employer_name) > 200:
        raise ValueError("Employer name is required")
    hosts = list(GREENHOUSE_JOB_HOSTS)
    if careers_host:
        host = careers_host.strip().lower()
        if not _HOST.match(host):
            raise ValueError("Invalid careers host")
        hosts.append(host)
    return {
        "board_name": board_token,
        "employer_name": employer_name.strip(),
        "fetch_hosts": [GREENHOUSE_API_HOST],
        "fetch_path_prefixes": [greenhouse_board_path(board_token)],
        "allowed_url_hosts": hosts,
    }


def _misconfigured(detail: str) -> SourcePolicyError:
    return SourcePolicyError(SourceErrorCode.CONNECTOR_MISCONFIGURED, detail)


def _validated_config(config: Mapping[str, Any]) -> tuple[str, str]:
    token = config.get("board_name")
    employer = config.get("employer_name")
    if not isinstance(token, str) or not _BOARD_TOKEN.match(token):
        raise _misconfigured("Greenhouse board_name is missing or invalid")
    if not isinstance(employer, str) or not employer.strip() or len(employer) > 200:
        raise _misconfigured("Greenhouse employer_name is missing or invalid")
    if tuple(config.get("fetch_hosts") or ()) != (GREENHOUSE_API_HOST,):
        raise _misconfigured("Greenhouse fetch_hosts must be exactly the Job Board API host")
    if tuple(config.get("fetch_path_prefixes") or ()) != (greenhouse_board_path(token),):
        raise _misconfigured("Greenhouse fetch_path_prefixes must be exactly the board path")
    hosts = tuple(config.get("allowed_url_hosts") or ())
    if hosts[: len(GREENHOUSE_JOB_HOSTS)] != GREENHOUSE_JOB_HOSTS or len(hosts) > 3:
        raise _misconfigured("Greenhouse allowed_url_hosts must be the board hosts (+1 host)")
    if "credential" in config:
        raise _misconfigured("Listing a public Greenhouse board takes no credentials")
    return token, employer.strip()


class GreenhouseJobBoardProvider:
    descriptor = GREENHOUSE_DESCRIPTOR

    def fetch_page(self, request: FetchRequest) -> FetchPage:
        token, employer = _validated_config(request.config)
        if request.http is None:
            raise SourcePolicyError(
                SourceErrorCode.NETWORK_ACCESS_DISABLED, "No bound network client"
            )
        url = greenhouse_board_url(token)
        payload = request.http.get_json(url, params={"content": "true"})
        if not isinstance(payload, Mapping) or not isinstance(payload.get("jobs"), list):
            raise InvalidProviderResponseError("Greenhouse response has no jobs list")
        records = [
            map_greenhouse_job(job, employer=employer) if isinstance(job, Mapping) else job
            for job in payload["jobs"]
        ]
        meta = payload.get("meta")
        total = meta.get("total") if isinstance(meta, Mapping) else None
        if isinstance(total, int) and total != len(records):
            # The board says it has more (or fewer) jobs than it returned: the listing is
            # not complete, so absence must not mark anything stale.
            return FetchPage(records=records, listing_complete=False, source_url=url,
                             metadata={"reported_total": total})
        return FetchPage(
            records=records,
            listing_complete=True,
            source_url=url,
            metadata={"reported_total": total},
        )


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _identity(job: Mapping[str, Any]) -> str | None:
    value = job.get("id")
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return str(value)
    if isinstance(value, str) and value.isdigit():
        return value
    return None


def _location_name(job: Mapping[str, Any]) -> str | None:
    location = job.get("location")
    return _text(location.get("name")) if isinstance(location, Mapping) else None


def _work_mode(location_name: str | None) -> str | None:
    """Only an explicit statement in the location name; never a guess from the city."""
    if not location_name:
        return None
    if _HYBRID.search(location_name):
        return "hybrid"
    if _REMOTE.match(location_name):
        return "remote"
    return None


def _offices(job: Mapping[str, Any]) -> list[str]:
    offices = job.get("offices")
    if not isinstance(offices, Sequence) or isinstance(offices, str):
        return []
    names: list[str] = []
    for office in offices:
        if isinstance(office, Mapping):
            label = _text(office.get("location")) or _text(office.get("name"))
            if label:
                names.append(label)
    return names


def _description(job: Mapping[str, Any]) -> str | None:
    content = _text(job.get("content"))
    if content is None:
        return None
    # `content` is HTML that is itself HTML-escaped: unescape once, then strip the tags.
    return _text(html_to_text(html.unescape(content)))


def map_greenhouse_job(job: Mapping[str, Any], *, employer: str) -> dict[str, Any]:
    """Map one Greenhouse job object to the pipeline's raw record (explicit fields only)."""
    location_name = _location_name(job)
    first_published = job.get("first_published")
    updated_at = job.get("updated_at")
    return {
        "source_job_id": _identity(job),
        "title": job.get("title"),
        "employer": employer,
        "canonical_url": job.get("absolute_url"),
        "location_text": location_name,
        "secondary_locations": _offices(job),
        "location_country": None,
        "work_mode": _work_mode(location_name),
        "description": _description(job),
        "posted_at": first_published if isinstance(first_published, str) else None,
        "updated_at": updated_at if isinstance(updated_at, str) else None,
        # Not provided: permitted countries, sponsorship, relocation, work authorization,
        # salary, employment type, closing date.
        "source_payload": dict(job),
    }
