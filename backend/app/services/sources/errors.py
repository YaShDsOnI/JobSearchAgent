"""Structured, redacted source-integration errors.

Error details must never contain raw provider payloads, credentials, or candidate data:
they are persisted on source runs, returned to operators, and written to audit events.
"""

from enum import StrEnum

from app.core.redaction import redact


class SourceErrorCode(StrEnum):
    CONNECTOR_NOT_FOUND = "connector_not_found"
    CONNECTOR_DISABLED = "connector_disabled"
    POLICY_NOT_APPROVED = "policy_not_approved"
    TIER_NOT_PERMITTED = "tier_not_permitted"
    PROVIDER_UNKNOWN = "provider_unknown"
    PROVIDER_MISMATCH = "provider_mismatch"
    AUTHORIZATION_MISSING = "authorization_missing"
    AUTHORIZATION_BASIS_NOT_PERMITTED = "authorization_basis_not_permitted"
    AUTHORIZATION_EXPIRED = "authorization_expired"
    TERMS_VERSION_MISMATCH = "terms_version_mismatch"
    DOMAIN_BLOCKED = "domain_blocked"
    NETWORK_ACCESS_DISABLED = "network_access_disabled"
    TENANT_MISMATCH = "tenant_mismatch"
    CIRCUIT_OPEN = "circuit_open"
    RATE_LIMITED = "rate_limited"
    RUN_IN_PROGRESS = "run_in_progress"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_ERROR = "provider_error"
    INVALID_PROVIDER_RESPONSE = "invalid_provider_response"
    PERSISTENCE_CONFLICT = "persistence_conflict"
    INTERNAL_ERROR = "internal_error"
    PROVIDER_TIMEOUT = "provider_timeout"
    PROVIDER_ACCESS_DENIED = "provider_access_denied"
    EGRESS_DENIED = "egress_denied"
    EGRESS_NOT_ALLOWLISTED = "egress_not_allowlisted"
    RESPONSE_TOO_LARGE = "response_too_large"
    REQUEST_BUDGET_EXCEEDED = "request_budget_exceeded"
    INLINE_SECRET_IN_CONFIG = "inline_secret_in_config"
    CREDENTIAL_UNAVAILABLE = "credential_unavailable"
    CONNECTOR_MISCONFIGURED = "connector_misconfigured"


class SourceError(RuntimeError):
    """Base class. `retryable` drives task retry classification."""

    retryable = False

    def __init__(
        self,
        code: SourceErrorCode,
        detail: str = "",
        *,
        retry_after_seconds: int | None = None,
    ) -> None:
        self.code = code
        self.detail = redact(detail)[:512]
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"{code.value}: {self.detail}" if self.detail else code.value)


class SourcePolicyError(SourceError):
    """Authorization, terms, tier, tenant, or kill-switch failure. Never retried."""


class InvalidProviderResponseError(SourceError):
    """The provider response is structurally unusable. Retrying the same data will not help."""

    def __init__(self, detail: str = "") -> None:
        super().__init__(SourceErrorCode.INVALID_PROVIDER_RESPONSE, detail)


class RetryableSourceError(SourceError):
    retryable = True


class ProviderUnavailableError(RetryableSourceError):
    def __init__(self, detail: str = "", *, retry_after_seconds: int | None = None) -> None:
        super().__init__(
            SourceErrorCode.PROVIDER_UNAVAILABLE, detail, retry_after_seconds=retry_after_seconds
        )


class RateLimitedError(RetryableSourceError):
    def __init__(self, detail: str = "", *, retry_after_seconds: int | None = None) -> None:
        super().__init__(
            SourceErrorCode.RATE_LIMITED, detail, retry_after_seconds=retry_after_seconds
        )


class ConcurrentRunError(RetryableSourceError):
    def __init__(self, detail: str = "") -> None:
        super().__init__(SourceErrorCode.RUN_IN_PROGRESS, detail, retry_after_seconds=60)
