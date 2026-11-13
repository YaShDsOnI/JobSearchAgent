"""Bounded, allowlisted HTTP client handed to network providers.

Providers never construct their own HTTP clients. The pipeline builds one per run from
the connector's reviewed configuration and deployment settings; it is bound to that
connector's egress policy and limits, which the provider cannot change.
"""

import hashlib
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from app.core.config import Settings
from app.services.sources.credentials import ConnectorCredential
from app.services.sources.egress import EgressDenied, EgressGuard, PinnedTransport
from app.services.sources.errors import (
    InvalidProviderResponseError,
    ProviderUnavailableError,
    RateLimitedError,
    RetryableSourceError,
    SourceError,
    SourceErrorCode,
    SourcePolicyError,
)

REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
JSON_CONTENT_TYPES = frozenset({"application/json", "application/ld+json"})
USER_AGENT = "JobFinderAgent/phase5a (authorized-source connector)"


@dataclass(frozen=True)
class HttpLimits:
    connect_timeout: float
    read_timeout: float
    total_timeout: float
    max_response_bytes: int
    max_redirects: int
    max_requests: int

    @classmethod
    def from_settings(cls, settings: Settings) -> "HttpLimits":
        return cls(
            connect_timeout=settings.source_http_connect_timeout_seconds,
            read_timeout=settings.source_http_read_timeout_seconds,
            total_timeout=settings.source_http_total_timeout_seconds,
            max_response_bytes=settings.source_http_max_response_bytes,
            max_redirects=settings.source_http_max_redirects,
            max_requests=settings.source_http_max_requests_per_run,
        )


@dataclass(frozen=True)
class NetworkEvent:
    """Audit-safe record of one outbound attempt: no query string, headers, or body."""

    outcome: str  # "response" | "denied" | "error"
    host: str
    path_digest: str
    status: int | None = None
    bytes_read: int = 0
    duration_ms: int = 0
    redirects: int = 0
    reason: str | None = None


def _path_digest(url: str) -> str:
    try:
        path = urlsplit(url).path
    except ValueError:
        path = ""
    return hashlib.sha256(path.encode("utf-8", "replace")).hexdigest()[:16]


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "")[:255]
    except ValueError:
        return ""


@dataclass
class ConnectorHttpClient:
    guard: EgressGuard
    limits: HttpLimits
    credential: ConnectorCredential | None = None
    transport: httpx.BaseTransport | None = None
    clock: Callable[[], float] = time.monotonic
    events: list[NetworkEvent] = field(default_factory=list)
    _requests: int = 0
    _client: httpx.Client | None = None

    def __repr__(self) -> str:
        return f"ConnectorHttpClient(hosts={self.guard.policy.connector_hosts!r})"

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                transport=self.transport or PinnedTransport(self.guard),
                follow_redirects=False,
                trust_env=False,  # Never route through environment-configured proxies.
                timeout=httpx.Timeout(
                    connect=self.limits.connect_timeout,
                    read=self.limits.read_timeout,
                    write=self.limits.read_timeout,
                    pool=self.limits.connect_timeout,
                ),
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def get_json(self, url: str, *, params: Mapping[str, str | int] | None = None) -> Any:
        body = self.get_bytes(url, params=params, accept=JSON_CONTENT_TYPES)
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise InvalidProviderResponseError("Provider returned malformed JSON") from exc

    def get_bytes(
        self,
        url: str,
        *,
        params: Mapping[str, str | int] | None = None,
        accept: frozenset[str] = JSON_CONTENT_TYPES,
    ) -> bytes:
        if self._requests >= self.limits.max_requests:
            raise SourceError(
                SourceErrorCode.REQUEST_BUDGET_EXCEEDED, "Per-run request budget exhausted"
            )
        self._requests += 1
        started = self.clock()
        deadline = started + self.limits.total_timeout
        current = str(httpx.URL(url, params=params)) if params else url
        first_host: str | None = None
        for hop in range(self.limits.max_redirects + 1):
            try:
                host = self.guard.check_url(current)
            except EgressDenied as exc:
                self._record("denied", current, started, redirects=hop, reason=exc.reason)
                raise
            first_host = first_host or host
            headers = {
                "Accept": ", ".join(sorted(accept)),
                "Accept-Encoding": "identity",
                "User-Agent": USER_AGENT,
            }
            # Credentials go only to the host the provider addressed, never to a redirect
            # target on another host, even an allowlisted one.
            if self.credential is not None and host == first_host:
                headers[self.credential.header] = self.credential.header_value()
            try:
                response = self._http().send(
                    httpx.Request("GET", current, headers=headers), stream=True
                )
            except EgressDenied as exc:
                self._record("denied", current, started, redirects=hop, reason=exc.reason)
                raise
            except httpx.TimeoutException as exc:
                self._record("error", current, started, redirects=hop, reason="timeout")
                raise RetryableSourceError(
                    SourceErrorCode.PROVIDER_TIMEOUT, "Provider request timed out"
                ) from exc
            except httpx.HTTPError as exc:
                self._record("error", current, started, redirects=hop, reason="transport")
                raise ProviderUnavailableError(
                    f"Provider transport error ({type(exc).__name__})"
                ) from exc
            try:
                if response.status_code in REDIRECT_STATUSES:
                    location = response.headers.get("location")
                    self._record(
                        "response", current, started, status=response.status_code,
                        redirects=hop,
                    )
                    if not location:
                        raise InvalidProviderResponseError("Redirect without a location")
                    current = urljoin(current, location)
                    continue
                # Every response is recorded, including rejected ones (429, 5xx, 401/403,
                # wrong type, oversized), so each outbound attempt reaches the audit log.
                try:
                    self._check_status(response)
                    self._check_content_type(response, accept)
                    body = self._read_limited(response, deadline)
                except SourceError as exc:
                    self._record(
                        "response", current, started, status=response.status_code,
                        redirects=hop, reason=exc.code.value,
                    )
                    raise
                except httpx.TimeoutException as exc:
                    # The body is streamed after `send()`, so read failures surface here.
                    self._record(
                        "error", current, started, status=response.status_code,
                        redirects=hop, reason="timeout",
                    )
                    raise RetryableSourceError(
                        SourceErrorCode.PROVIDER_TIMEOUT, "Provider response timed out"
                    ) from exc
                except httpx.HTTPError as exc:
                    self._record(
                        "error", current, started, status=response.status_code,
                        redirects=hop, reason="transport",
                    )
                    raise ProviderUnavailableError(
                        f"Provider transport error ({type(exc).__name__})"
                    ) from exc
                self._record(
                    "response", current, started, status=response.status_code,
                    bytes_read=len(body), redirects=hop,
                )
                return body
            finally:
                response.close()
        self._record("denied", current, started, reason="too_many_redirects")
        raise InvalidProviderResponseError("Provider exceeded the redirect limit")

    def _check_status(self, response: httpx.Response) -> None:
        status = response.status_code
        if status == 200:
            return
        if status == 429:
            raise RateLimitedError(
                "Provider rate limit", retry_after_seconds=_retry_after(response)
            )
        if status in {401, 403}:
            # Access control is respected, never retried around.
            raise SourcePolicyError(
                SourceErrorCode.PROVIDER_ACCESS_DENIED, f"Provider refused access ({status})"
            )
        if status in {500, 502, 503, 504}:
            raise ProviderUnavailableError(
                f"Provider unavailable ({status})", retry_after_seconds=_retry_after(response)
            )
        raise InvalidProviderResponseError(f"Unexpected provider status {status}")

    def _check_content_type(self, response: httpx.Response, accept: frozenset[str]) -> None:
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type not in accept:
            raise InvalidProviderResponseError("Provider returned an unexpected content type")
        declared = response.headers.get("content-length")
        if declared is not None:
            try:
                too_large = int(declared) > self.limits.max_response_bytes
            except ValueError as exc:
                raise InvalidProviderResponseError("Invalid content length") from exc
            if too_large:
                raise SourceError(
                    SourceErrorCode.RESPONSE_TOO_LARGE, "Provider response exceeds the size limit"
                )

    def _read_limited(self, response: httpx.Response, deadline: float) -> bytes:
        chunks: list[bytes] = []
        total = 0
        # Counts decoded bytes, so compressed payloads cannot bypass the limit. chunk_size=None
        # yields after every network read; re-chunking would buffer up to the chunk size
        # before the deadline is checked, letting a slow trickle outlive the total timeout.
        for chunk in response.iter_bytes(chunk_size=None):
            total += len(chunk)
            if total > self.limits.max_response_bytes:
                raise SourceError(
                    SourceErrorCode.RESPONSE_TOO_LARGE, "Provider response exceeds the size limit"
                )
            if self.clock() > deadline:
                raise RetryableSourceError(
                    SourceErrorCode.PROVIDER_TIMEOUT, "Provider response exceeded the time limit"
                )
            chunks.append(chunk)
        if not chunks:
            raise InvalidProviderResponseError("Provider returned an empty body")
        return b"".join(chunks)

    def _record(
        self,
        outcome: str,
        url: str,
        started: float,
        *,
        status: int | None = None,
        bytes_read: int = 0,
        redirects: int = 0,
        reason: str | None = None,
    ) -> None:
        self.events.append(
            NetworkEvent(
                outcome=outcome,
                host=_host(url),
                path_digest=_path_digest(url),
                status=status,
                bytes_read=bytes_read,
                duration_ms=int(max(self.clock() - started, 0) * 1000),
                redirects=redirects,
                reason=reason,
            )
        )


def _retry_after(response: httpx.Response) -> int | None:
    value = response.headers.get("retry-after")
    if value and value.strip().isdigit():
        return max(1, min(int(value.strip()), 3600))
    return None
