"""Fail-closed source authorization policy.

Every check raises `SourcePolicyError` (never retried) except the rate limit, which is a
retryable `RateLimitedError`. The provider is never called unless all checks pass.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from app.db.models import SourceConnector
from app.services.sources.base import ProviderDescriptor
from app.services.sources.credentials import find_inline_secrets
from app.services.sources.errors import RateLimitedError, SourceErrorCode, SourcePolicyError

ALLOWED_BASES = frozenset(
    {
        "official_api",
        "official_feed",
        "provider_approved_integration",
        "permitted_career_page",
        "explicit_authorization",
        "local_fixture",
    }
)
WORKER_TIERS = frozenset({"local", "A", "B"})
# Sources whose terms prohibit scraping/automation. Blocked for every basis in this phase.
BLOCKED_DOMAINS = ("linkedin.com", "naukri.com", "glassdoor.")
REQUIRED_AUTHORIZATION_FIELDS = ("basis", "evidence_ref", "reviewed_by", "reviewed_at")
_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def normalize_host(host: str) -> str | None:
    """Return a canonical ASCII hostname, or None if it is unsafe or ambiguous."""
    host = host.strip().lower().rstrip(".")
    if not host or len(host) > 253 or not host.isascii():
        return None  # Non-ASCII hosts are rejected outright (homoglyph/IDN confusion).
    labels = host.split(".")
    if len(labels) < 2 or not all(_LABEL.match(label) for label in labels):
        return None
    if labels[-1].isdigit() or labels[-1].startswith("0x"):
        return None  # Numeric TLDs are IP shorthand ("127.1", "2130706433", "0x7f.1").
    return host


def host_matches(host: str, entries: Iterable[str]) -> bool:
    for entry in entries:
        entry = entry.strip().lower().rstrip(".")
        if entry.startswith("*."):
            suffix = entry[1:]
            if host.endswith(suffix) and len(host) > len(suffix):
                return True
        elif entry and host == entry:
            return True
    return False


def connector_fetch_hosts(connector: SourceConnector) -> tuple[str, ...]:
    """Hosts a network connector may contact: reviewed `fetch_hosts`, else its base domain."""
    configured = (connector.config_json or {}).get("fetch_hosts")
    if configured:
        return tuple(str(item).strip().lower().rstrip(".") for item in configured)
    return (connector.base_domain.lower().rstrip("."),) if connector.base_domain else ()


def _entry_within(entry: str, deployment: tuple[str, ...]) -> bool:
    if entry.startswith("*."):
        suffix = entry[1:]
        return any(
            item == entry or (item.startswith("*.") and suffix.endswith(item[1:]))
            for item in deployment
        )
    host = normalize_host(entry)
    return host is not None and host_matches(host, deployment)


@dataclass(frozen=True)
class AuthorizationDecision:
    basis: str
    terms_version: str
    reviewed_at: str


def as_aware(value: datetime) -> datetime:
    # SQLite returns naive datetimes for timezone-aware columns; values are stored in UTC.
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return as_aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def is_blocked_host(host: str | None) -> bool:
    if not host:
        return False
    host = host.lower().rstrip(".")
    for blocked in BLOCKED_DOMAINS:
        if blocked.endswith("."):
            labels = host.split(".")
            if blocked.rstrip(".") in labels[:-1]:
                return True
        elif host == blocked or host.endswith("." + blocked):
            return True
    return False


def url_host_permitted(url: str, connector: SourceConnector) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    if not host or is_blocked_host(host):
        return False
    allowed = connector.config_json.get("allowed_url_hosts") if connector.config_json else None
    if not allowed:
        return True
    return any(host == item or host.endswith("." + item) for item in map(str, allowed))


def check_tenant_access(connector: SourceConnector, requested_by_user_id: str | None) -> None:
    """A private source can only run for its owner; None means the system acts for the owner."""
    if (
        connector.owner_user_id is not None
        and requested_by_user_id is not None
        and requested_by_user_id != connector.owner_user_id
    ):
        raise SourcePolicyError(SourceErrorCode.TENANT_MISMATCH, "Connector is not accessible")


def authorize_connector(
    connector: SourceConnector,
    descriptor: ProviderDescriptor,
    *,
    now: datetime,
    network_access_enabled: bool,
    egress_allowlist: tuple[str, ...] = (),
) -> AuthorizationDecision:
    if not connector.enabled:
        raise SourcePolicyError(SourceErrorCode.CONNECTOR_DISABLED, "Connector is disabled")
    if connector.policy_status != "approved":
        raise SourcePolicyError(
            SourceErrorCode.POLICY_NOT_APPROVED, "Connector policy is not approved"
        )
    if connector.tier not in WORKER_TIERS:
        raise SourcePolicyError(
            SourceErrorCode.TIER_NOT_PERMITTED, "Connector tier does not allow worker access"
        )
    if descriptor.kind != connector.kind:
        raise SourcePolicyError(
            SourceErrorCode.PROVIDER_MISMATCH, "Provider does not match connector kind"
        )
    if connector.health_status == "suspended":
        raise SourcePolicyError(
            SourceErrorCode.CIRCUIT_OPEN,
            "Connector circuit breaker is open; operator reset required",
        )

    authorization = connector.authorization_json or {}
    missing = [
        key for key in REQUIRED_AUTHORIZATION_FIELDS if not str(authorization.get(key) or "")
    ]
    reviewed_at = _parse_datetime(authorization.get("reviewed_at"))
    if missing or reviewed_at is None:
        fields = ",".join(missing or ["reviewed_at"])
        raise SourcePolicyError(
            SourceErrorCode.AUTHORIZATION_MISSING,
            f"Authorization record is missing or incomplete: {fields}",
        )
    if str(authorization.get("terms_version") or "") != connector.terms_version:
        raise SourcePolicyError(
            SourceErrorCode.TERMS_VERSION_MISMATCH,
            "Authorization was reviewed against a different terms version",
        )
    basis = str(authorization["basis"])
    if basis not in ALLOWED_BASES or basis not in descriptor.allowed_bases:
        raise SourcePolicyError(
            SourceErrorCode.AUTHORIZATION_BASIS_NOT_PERMITTED,
            "Authorization basis is not permitted for this provider",
        )
    if "expires_at" in authorization and authorization["expires_at"] is not None:
        expires_at = _parse_datetime(authorization["expires_at"])
        if expires_at is None or expires_at <= now:
            raise SourcePolicyError(
                SourceErrorCode.AUTHORIZATION_EXPIRED, "Source authorization has expired"
            )
    if (basis == "local_fixture") != (connector.tier == "local"):
        raise SourcePolicyError(
            SourceErrorCode.AUTHORIZATION_BASIS_NOT_PERMITTED,
            "Local fixture basis is only valid for local-tier connectors",
        )
    if basis == "local_fixture" and descriptor.requires_network:
        raise SourcePolicyError(
            SourceErrorCode.AUTHORIZATION_BASIS_NOT_PERMITTED,
            "Local fixture basis cannot authorize network access",
        )
    if is_blocked_host(connector.base_domain):
        raise SourcePolicyError(SourceErrorCode.DOMAIN_BLOCKED, "Source domain is blocked")
    for host in (connector.config_json or {}).get("allowed_url_hosts", []) or []:
        if is_blocked_host(str(host)):
            raise SourcePolicyError(SourceErrorCode.DOMAIN_BLOCKED, "Source domain is blocked")
    if find_inline_secrets(connector.config_json or {}):
        raise SourcePolicyError(
            SourceErrorCode.INLINE_SECRET_IN_CONFIG,
            "Connector configuration contains a literal secret; use a credential reference",
        )
    if descriptor.requires_network:
        if not connector.base_domain:
            raise SourcePolicyError(
                SourceErrorCode.AUTHORIZATION_MISSING, "Network source requires a base domain"
            )
        if not network_access_enabled:
            raise SourcePolicyError(
                SourceErrorCode.NETWORK_ACCESS_DISABLED, "Source network access is disabled"
            )
        hosts = connector_fetch_hosts(connector)
        if (
            not hosts
            or not egress_allowlist
            or any(is_blocked_host(host.lstrip("*.")) for host in hosts)
            or not all(_entry_within(host, egress_allowlist) for host in hosts)
        ):
            raise SourcePolicyError(
                SourceErrorCode.EGRESS_NOT_ALLOWLISTED,
                "Connector hosts are not in the deployment egress allowlist",
            )
    return AuthorizationDecision(
        basis=basis, terms_version=connector.terms_version, reviewed_at=reviewed_at.isoformat()
    )


def check_rate_limit(
    connector: SourceConnector, descriptor: ProviderDescriptor, *, now: datetime
) -> None:
    configured = int((connector.config_json or {}).get("min_interval_seconds", 0) or 0)
    interval = max(descriptor.min_interval_seconds, configured)
    if interval <= 0 or connector.last_run_at is None:
        return
    elapsed = (now - as_aware(connector.last_run_at)).total_seconds()
    if elapsed < interval:
        raise RateLimitedError(
            "Minimum interval between source runs has not elapsed",
            retry_after_seconds=max(1, int(interval - elapsed)),
        )
