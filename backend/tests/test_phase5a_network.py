"""Phase 5A outbound network controls. No test opens a real network connection or DNS lookup."""

import contextlib
import gzip
import ipaddress
import json
import logging
from collections.abc import Callable, Iterator
from functools import partial
from typing import Any

import httpcore
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.core.logging import JsonFormatter
from app.core.redaction import redact
from app.db.models import AuditEvent, JobPosting, SourceConnector, SourceRun, Task
from app.services.sources.base import FetchPage, FetchRequest, ProviderDescriptor
from app.services.sources.credentials import resolve_credential
from app.services.sources.egress import (
    EgressDenied,
    EgressGuard,
    EgressPolicy,
    PinnedNetworkBackend,
    PinnedTransport,
    is_public_address,
)
from app.services.sources.errors import SourceError, SourceErrorCode, SourcePolicyError
from app.services.sources.http_client import ConnectorHttpClient, HttpLimits
from app.services.sources.pipeline import build_connector_http_client, run_ingestion
from app.services.sources.registry import ProviderRegistry
from app.services.sources.worker import enqueue_ingestion, run_next_ingestion_task
from app.services.storage import ArtifactStorage
from tests.conftest import TestingSessionLocal, app

PUBLIC_IP = "93.184.216.34"
OTHER_PUBLIC_IP = "151.101.1.69"
FEED_HOST = "boards.example-ats.com"
ALLOWLIST = (FEED_HOST, "*.cdn.example-ats.com")
SECRET = "s3cr3t-live-token-value-9f8e7d"
LIMITS = HttpLimits(
    connect_timeout=1, read_timeout=1, total_timeout=5, max_response_bytes=10_000,
    max_redirects=2, max_requests=10,
)
FEED_DESCRIPTOR = ProviderDescriptor(
    kind="official_feed_test",
    version="official-feed-test-v1",
    display_name="Mocked official feed (no real network)",
    allowed_bases=frozenset({"official_feed"}),
    requires_network=True,
)


def resolver_for(mapping: dict[str, list[str]]) -> Callable[[str, int], list[str]]:
    def resolve(host: str, port: int) -> list[str]:
        return mapping.get(host, [PUBLIC_IP])

    return resolve


def guard(
    connector_hosts: tuple[str, ...] = ALLOWLIST,
    deployment: tuple[str, ...] = ALLOWLIST,
    resolver: Callable[[str, int], list[str]] | None = None,
) -> EgressGuard:
    return EgressGuard(
        EgressPolicy(deployment_allowlist=deployment, connector_hosts=connector_hosts),
        resolver or resolver_for({}),
    )


def client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    egress: EgressGuard | None = None,
    limits: HttpLimits = LIMITS,
    clock: Callable[[], float] | None = None,
    credential: Any = None,
) -> ConnectorHttpClient:
    kwargs: dict[str, Any] = {}
    if clock is not None:
        kwargs["clock"] = clock
    return ConnectorHttpClient(
        guard=egress or guard(),
        limits=limits,
        credential=credential,
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def json_response(payload: Any, status: int = 200, **headers: str) -> httpx.Response:
    return httpx.Response(
        status, headers={"content-type": "application/json", **headers},
        content=json.dumps(payload).encode(),
    )


# --- allowlist / URL / hostname -----------------------------------------------------------


def test_allowed_hosts_exact_and_wildcard_subdomain():
    egress = guard()
    assert egress.check_url(f"https://{FEED_HOST}/v1/boards/acme/jobs") == FEED_HOST
    assert egress.check_url("https://eu.cdn.example-ats.com/feed.json") == "eu.cdn.example-ats.com"
    assert egress.check_url(f"https://{FEED_HOST.upper()}./jobs") == FEED_HOST


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("https://unknown.example.org/jobs", "host_not_allowlisted"),
        ("https://cdn.example-ats.com/x", "host_not_allowlisted"),  # wildcard excludes apex
        (f"https://{FEED_HOST}.evil.test/jobs", "host_not_allowlisted"),
        ("https://evil-boards.example-ats.com/jobs", "host_not_allowlisted"),
        (f"http://{FEED_HOST}/jobs", "scheme_not_https"),
        (f"ftp://{FEED_HOST}/jobs", "scheme_not_https"),
        ("file:///etc/passwd", "scheme_not_https"),
        (f"https://{FEED_HOST}:8443/jobs", "port_not_allowed"),
        (f"https://user@{FEED_HOST}/jobs", "userinfo_not_allowed"),
        (f"https://{FEED_HOST}@evil.test/jobs", "userinfo_not_allowed"),
        (f"https://evil.test#@{FEED_HOST}/jobs", "host_not_allowlisted"),
        (f"https://{FEED_HOST}\\@evil.test/jobs", "url_invalid"),
        (f"https://{FEED_HOST}/jobs\r\nHost: evil.test", "url_invalid"),
        ("https://%62oards.example-ats.com/jobs", "encoded_host_not_allowed"),
        ("https://bоards.example-ats.com/jobs", "hostname_invalid"),  # Cyrillic "о"
        ("https://localhost/jobs", "hostname_invalid"),
        ("https://127.0.0.1/jobs", "ip_literal_not_allowed"),
        ("https://[::1]/jobs", "ip_literal_not_allowed"),
        ("https://2130706433/jobs", "hostname_invalid"),
        ("https://0x7f000001/jobs", "hostname_invalid"),
        ("https://127.1/jobs", "hostname_invalid"),
        ("https://www.linkedin.com/jobs", "domain_blocked"),
        ("https://" + "a" * 2100 + ".com/", "url_invalid"),
    ],
)
def test_denied_urls_and_malicious_hostnames(url, reason):
    with pytest.raises(EgressDenied) as denied:
        guard().check_url(url)
    assert denied.value.reason == reason
    assert denied.value.code == SourceErrorCode.EGRESS_DENIED
    assert not denied.value.retryable


def test_host_must_be_in_both_deployment_and_connector_allowlists():
    only_deployment = guard(connector_hosts=("other.example-ats.com",))
    with pytest.raises(EgressDenied):
        only_deployment.check_url(f"https://{FEED_HOST}/jobs")
    only_connector = guard(deployment=("other.example-ats.com",))
    with pytest.raises(EgressDenied):
        only_connector.check_url(f"https://{FEED_HOST}/jobs")
    for empty in (guard(deployment=()), guard(connector_hosts=())):
        with pytest.raises(EgressDenied):
            empty.check_url(f"https://{FEED_HOST}/jobs")


def test_path_prefix_allowlist_is_enforced_when_configured():
    egress = EgressGuard(
        EgressPolicy(ALLOWLIST, ALLOWLIST, path_prefixes=("/v1/boards/acme/",)),
        resolver_for({}),
    )
    egress.check_url(f"https://{FEED_HOST}/v1/boards/acme/jobs")
    with pytest.raises(EgressDenied) as denied:
        egress.check_url(f"https://{FEED_HOST}/v1/boards/other/jobs")
    assert denied.value.reason == "path_not_allowlisted"


# --- IP safety ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1", "127.0.0.2", "::1",  # loopback
        "10.0.0.5", "172.16.3.4", "192.168.1.1", "fd00::1", "fc00::1",  # private
        "169.254.169.254", "fe80::1", "fd00:ec2::254",  # link-local / cloud metadata
        "100.64.0.1",  # carrier-grade NAT
        "0.0.0.0", "::", "240.0.0.1", "255.255.255.255", "198.18.0.1",  # reserved
        "192.0.2.10", "2001:db8::1",  # documentation
        "224.0.0.1", "ff02::1",  # multicast
        "::ffff:127.0.0.1", "::ffff:10.0.0.1",  # IPv4-mapped
        "64:ff9b::7f00:1", "64:ff9b::a00:1",  # NAT64-embedded private
        "2002:7f00:1::1", "2002:a00:1::1",  # 6to4-embedded private
        "2001:0:4136:e378:8000:63bf:3fff:fdd2",  # Teredo
    ],
)
def test_non_public_addresses_are_rejected(address):
    assert not is_public_address(ipaddress.ip_address(address))
    egress = guard(resolver=resolver_for({FEED_HOST: [address]}))
    with pytest.raises(EgressDenied) as denied:
        egress.check_url(f"https://{FEED_HOST}/jobs")
    assert denied.value.reason == "resolved_to_non_public_address"


def test_public_addresses_pass_and_one_bad_answer_rejects_the_host():
    for address in (PUBLIC_IP, "2606:4700:4700::1111", "::ffff:93.184.216.34"):
        assert is_public_address(ipaddress.ip_address(address))
    mixed = guard(resolver=resolver_for({FEED_HOST: [PUBLIC_IP, "10.0.0.7"]}))
    with pytest.raises(EgressDenied):
        mixed.check_url(f"https://{FEED_HOST}/jobs")
    with pytest.raises(EgressDenied) as empty:
        guard(resolver=resolver_for({FEED_HOST: []})).check_url(f"https://{FEED_HOST}/jobs")
    assert empty.value.reason == "dns_no_answer"


# --- DNS pinning / rebinding ----------------------------------------------------------------


class RecordingBackend(httpcore.NetworkBackend):
    def __init__(self, response: bytes = b"") -> None:
        self.connections: list[tuple[str, int]] = []
        self.response = response

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.connections.append((host, port))
        return httpcore.MockStream([self.response])


def rebinding_resolver(*answers: list[str]) -> Callable[[str, int], list[str]]:
    sequence: Iterator[list[str]] = iter(answers)
    last: list[list[str]] = [answers[-1]]

    def resolve(host: str, port: int) -> list[str]:
        with contextlib.suppress(StopIteration):
            last[0] = next(sequence)
        return last[0]

    return resolve


def test_connection_is_pinned_to_the_validated_address():
    inner = RecordingBackend()
    backend = PinnedNetworkBackend(guard(), inner)
    backend.connect_tcp(FEED_HOST, 443)
    assert inner.connections == [(PUBLIC_IP, 443)]  # IP, not hostname: no second lookup.
    for host, port in ((("evil.test", 443)), (FEED_HOST, 80), ("127.0.0.1", 443)):
        with pytest.raises(EgressDenied):
            backend.connect_tcp(host, port)
    with pytest.raises(EgressDenied):
        backend.connect_unix_socket("/var/run/docker.sock")
    assert len(inner.connections) == 1


def test_dns_rebinding_between_check_and_connect_is_blocked():
    # First answer (URL check) is public; the answer at connect time points inside.
    egress = guard(resolver=rebinding_resolver([PUBLIC_IP], ["127.0.0.1"]))
    inner = RecordingBackend()
    http = ConnectorHttpClient(
        guard=egress, limits=LIMITS, transport=PinnedTransport(egress, inner_backend=inner)
    )
    with pytest.raises(EgressDenied) as denied:
        http.get_json(f"https://{FEED_HOST}/jobs")
    assert denied.value.reason == "resolved_to_non_public_address"
    assert inner.connections == []  # No connection was attempted.
    assert http.events[-1].outcome == "denied"


def test_pinned_transport_end_to_end_and_wiring():
    body = b'{"jobs": []}'
    raw = (
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
    )
    egress = guard(resolver=resolver_for({FEED_HOST: [OTHER_PUBLIC_IP]}))
    inner = RecordingBackend(raw)
    transport = PinnedTransport(egress, inner_backend=inner)
    # Guards against an httpx/httpcore upgrade silently bypassing the pinned backend.
    assert isinstance(transport._pool._network_backend, PinnedNetworkBackend)
    http = ConnectorHttpClient(guard=egress, limits=LIMITS, transport=transport)
    assert http.get_json(f"https://{FEED_HOST}/jobs") == {"jobs": []}
    assert inner.connections == [(OTHER_PUBLIC_IP, 443)]
    http.close()


def test_client_ignores_environment_proxies(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:3128")
    http = client(lambda request: json_response({}))
    assert http._http()._trust_env is False
    assert http.get_json(f"https://{FEED_HOST}/x") == {}


# --- redirects ------------------------------------------------------------------------------


def test_allowed_redirects_are_followed_and_revalidated():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/moved"})
        if request.url.path == "/moved":
            return httpx.Response(
                301, headers={"location": "https://eu.cdn.example-ats.com/final.json"}
            )
        return json_response({"ok": True})

    http = client(handler)
    assert http.get_json(f"https://{FEED_HOST}/start") == {"ok": True}
    assert seen == [
        f"https://{FEED_HOST}/start",
        f"https://{FEED_HOST}/moved",
        "https://eu.cdn.example-ats.com/final.json",
    ]


@pytest.mark.parametrize(
    "location",
    [
        "https://evil.test/steal",
        f"http://{FEED_HOST}/downgrade",
        "https://169.254.169.254/latest/meta-data/",
        "https://localhost/admin",
        "https://internal.example-ats.com/",
        f"https://{FEED_HOST}:22/",
    ],
)
def test_unsafe_redirects_are_denied_before_any_request(location):
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(302, headers={"location": location})

    http = client(handler)
    with pytest.raises(EgressDenied):
        http.get_json(f"https://{FEED_HOST}/start")
    assert requested == [f"https://{FEED_HOST}/start"]
    assert http.events[-1].outcome == "denied"


def test_redirect_to_private_address_on_allowlisted_host_is_denied():
    egress = guard(resolver=resolver_for({"eu.cdn.example-ats.com": ["10.1.2.3"]}))
    http = client(
        lambda request: httpx.Response(
            302, headers={"location": "https://eu.cdn.example-ats.com/x"}
        ),
        egress=egress,
    )
    with pytest.raises(EgressDenied) as denied:
        http.get_json(f"https://{FEED_HOST}/start")
    assert denied.value.reason == "resolved_to_non_public_address"


def test_redirect_loops_are_bounded():
    http = client(lambda request: httpx.Response(302, headers={"location": "/again"}))
    with pytest.raises(SourceError) as raised:
        http.get_json(f"https://{FEED_HOST}/start")
    assert raised.value.code == SourceErrorCode.INVALID_PROVIDER_RESPONSE


# --- timeouts, sizes, content ---------------------------------------------------------------


@pytest.mark.parametrize("error", [httpx.ConnectTimeout, httpx.ReadTimeout, httpx.PoolTimeout])
def test_timeouts_are_retryable_provider_timeouts(error):
    def handler(request: httpx.Request) -> httpx.Response:
        raise error("timed out", request=request)

    with pytest.raises(SourceError) as raised:
        client(handler).get_json(f"https://{FEED_HOST}/jobs")
    assert raised.value.code == SourceErrorCode.PROVIDER_TIMEOUT and raised.value.retryable


def test_total_deadline_applies_to_slow_streams():
    ticks = iter(range(0, 100, 3))

    def clock() -> float:
        return float(next(ticks))

    def slow_body() -> Iterator[bytes]:
        for _ in range(10):
            yield b" " * 100

    http = client(
        lambda request: httpx.Response(
            200, headers={"content-type": "application/json"}, content=slow_body()
        ),
        clock=clock,
    )
    with pytest.raises(SourceError) as raised:
        http.get_json(f"https://{FEED_HOST}/jobs")
    assert raised.value.code == SourceErrorCode.PROVIDER_TIMEOUT


def test_declared_oversized_response_is_rejected_before_reading():
    consumed: list[int] = []

    def body() -> Iterator[bytes]:
        consumed.append(1)
        yield b"{}"

    http = client(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "application/json", "content-length": "999999999"},
            content=body(),
        )
    )
    with pytest.raises(SourceError) as raised:
        http.get_json(f"https://{FEED_HOST}/jobs")
    assert raised.value.code == SourceErrorCode.RESPONSE_TOO_LARGE
    assert consumed == []


def test_streamed_and_compressed_oversized_responses_are_cut_off():
    def chunks() -> Iterator[bytes]:
        while True:
            yield b"x" * 4096

    streamed = client(
        lambda request: httpx.Response(
            200, headers={"content-type": "application/json"}, content=chunks()
        )
    )
    with pytest.raises(SourceError) as too_big:
        streamed.get_json(f"https://{FEED_HOST}/jobs")
    assert too_big.value.code == SourceErrorCode.RESPONSE_TOO_LARGE

    bomb = gzip.compress(b"0" * 1_000_000)  # ~1 KB compressed, 1 MB decoded
    compressed = client(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "application/json", "content-encoding": "gzip"},
            content=bomb,
        )
    )
    with pytest.raises(SourceError) as decoded_too_big:
        compressed.get_json(f"https://{FEED_HOST}/jobs")
    assert decoded_too_big.value.code == SourceErrorCode.RESPONSE_TOO_LARGE


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html></html>"),
        httpx.Response(200, headers={"content-type": "application/json"}, content=b"{not json"),
        httpx.Response(200, headers={"content-type": "application/json"}, content=b""),
        httpx.Response(200, headers={"content-type": "application/json"}, content=b"\xff\xfe"),
        httpx.Response(404, headers={"content-type": "application/json"}, content=b"{}"),
        httpx.Response(302),  # Redirect without a location.
    ],
)
def test_invalid_content_is_rejected(response):
    with pytest.raises(SourceError) as raised:
        client(lambda request: response).get_json(f"https://{FEED_HOST}/jobs")
    assert raised.value.code == SourceErrorCode.INVALID_PROVIDER_RESPONSE
    assert not raised.value.retryable


def _constant(response: httpx.Response) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        return response

    return handler


def test_provider_status_classification():
    cases = [
        (json_response({}, 429, **{"retry-after": "120"}), SourceErrorCode.RATE_LIMITED, True),
        (json_response({}, 503), SourceErrorCode.PROVIDER_UNAVAILABLE, True),
        (json_response({}, 403), SourceErrorCode.PROVIDER_ACCESS_DENIED, False),
        (json_response({}, 401), SourceErrorCode.PROVIDER_ACCESS_DENIED, False),
    ]
    for response, code, retryable in cases:
        with pytest.raises(SourceError) as raised:
            client(_constant(response)).get_json(f"https://{FEED_HOST}/jobs")
        assert (raised.value.code, raised.value.retryable) == (code, retryable)
    with pytest.raises(SourceError) as limited:
        client(lambda request: json_response({}, 429, **{"retry-after": "120"})).get_json(
            f"https://{FEED_HOST}/jobs"
        )
    assert limited.value.retry_after_seconds == 120


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (json_response({}, 429), SourceErrorCode.RATE_LIMITED),
        (json_response({}, 503), SourceErrorCode.PROVIDER_UNAVAILABLE),
        (json_response({}, 403), SourceErrorCode.PROVIDER_ACCESS_DENIED),
        (json_response({}, 404), SourceErrorCode.INVALID_PROVIDER_RESPONSE),
        (
            httpx.Response(200, headers={"content-type": "text/html"}, content=b"<p>"),
            SourceErrorCode.INVALID_PROVIDER_RESPONSE,
        ),
        (
            httpx.Response(
                200, headers={"content-type": "application/json", "content-length": "99999"},
                content=b"{}",
            ),
            SourceErrorCode.RESPONSE_TOO_LARGE,
        ),
    ],
)
def test_rejected_responses_are_still_recorded_as_network_events(response, code):
    """Every outbound attempt must reach the audit log, including refused responses."""
    http = client(_constant(response))
    with pytest.raises(SourceError) as raised:
        http.get_json(f"https://{FEED_HOST}/jobs")
    assert raised.value.code == code
    [event] = http.events
    assert (event.outcome, event.status, event.reason) == (
        "response", response.status_code, code.value,
    )
    assert event.host == FEED_HOST


class _FailingBody(httpx.SyncByteStream):
    def __init__(self, error: Exception) -> None:
        self._error = error

    def __iter__(self):
        yield b'{"jobs": ['
        raise self._error


@pytest.mark.parametrize(
    ("error", "code", "reason"),
    [
        (httpx.ReadTimeout("read timed out"), SourceErrorCode.PROVIDER_TIMEOUT, "timeout"),
        (httpx.RemoteProtocolError("peer closed"), SourceErrorCode.PROVIDER_UNAVAILABLE,
         "transport"),
        (httpx.ReadError("connection reset"), SourceErrorCode.PROVIDER_UNAVAILABLE, "transport"),
    ],
)
def test_failures_while_streaming_the_body_are_classified_and_recorded(error, code, reason):
    # The body is streamed after send(); a failure mid-body used to escape as a raw httpx
    # exception (misfiled by the pipeline as `provider_error`) with no network event.
    http = client(
        lambda request: httpx.Response(
            200, headers={"content-type": "application/json"}, stream=_FailingBody(error)
        )
    )
    with pytest.raises(SourceError) as raised:
        http.get_json(f"https://{FEED_HOST}/jobs")
    assert (raised.value.code, raised.value.retryable) == (code, True)
    [event] = http.events
    assert (event.outcome, event.status, event.reason) == ("error", 200, reason)


def test_rejected_response_is_written_to_the_run_audit(db):
    connector = feed_connector(db)
    with pytest.raises(SourceError):
        run_feed(db, connector, lambda request: json_response({}, 503))
    events = db.scalars(
        select(AuditEvent).where(AuditEvent.action == "source.network_request")
    ).all()
    assert [(e.metadata_json["status"], e.metadata_json["reason"]) for e in events] == [
        (503, "provider_unavailable")
    ]
    [run] = db.scalars(select(SourceRun).where(SourceRun.connector_id == connector.id)).all()
    assert run.stats_json["network"]["requests"] == 1


def test_request_budget_is_enforced():
    limits = HttpLimits(1, 1, 5, 10_000, 2, max_requests=2)
    http = client(lambda request: json_response({}), limits=limits)
    http.get_json(f"https://{FEED_HOST}/a")
    http.get_json(f"https://{FEED_HOST}/b")
    with pytest.raises(SourceError) as raised:
        http.get_json(f"https://{FEED_HOST}/c")
    assert raised.value.code == SourceErrorCode.REQUEST_BUDGET_EXCEEDED


def test_network_events_never_contain_query_strings_or_paths():
    http = client(lambda request: json_response({"ok": 1}))
    http.get_json(f"https://{FEED_HOST}/v1/{SECRET}/jobs", params={"api_key": SECRET})
    [event] = http.events
    assert event.host == FEED_HOST and event.status == 200
    assert SECRET not in json.dumps(event.__dict__)


# --- credentials and redaction --------------------------------------------------------------


def test_credentials_are_references_only_and_redacted(monkeypatch, caplog):
    monkeypatch.setenv("JFA_SOURCE_SECRET_ACME", SECRET)
    credential = resolve_credential(
        {"credential": {"ref": "env:JFA_SOURCE_SECRET_ACME", "header": "Authorization",
                        "scheme": "Bearer"}}
    )
    assert credential is not None
    assert SECRET not in repr(credential) and SECRET not in str(credential.value)
    for bad in (
        {"credential": {"ref": "env:PATH"}},
        {"credential": {"ref": "file:/etc/shadow"}},
        {"credential": {"ref": "env:JFA_SOURCE_SECRET_ACME", "header": "Host"}},
        {"credential": {"ref": "env:JFA_SOURCE_SECRET_MISSING"}},
        {"credential": "env:JFA_SOURCE_SECRET_ACME"},
    ):
        with pytest.raises(SourcePolicyError) as raised:
            resolve_credential(bad)
        assert raised.value.code == SourceErrorCode.CREDENTIAL_UNAVAILABLE

    sent_to: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent_to.append((request.url.host, request.headers.get("authorization")))
        if request.url.host == FEED_HOST:
            return httpx.Response(302, headers={"location": "https://eu.cdn.example-ats.com/x"})
        return json_response({"echo": f"Bearer {SECRET}"}, 500)

    http = client(handler, credential=credential)
    with pytest.raises(SourceError) as failed:
        http.get_json(f"https://{FEED_HOST}/jobs")
    # Sent to the addressed host only; stripped when redirected to another host.
    assert sent_to == [(FEED_HOST, f"Bearer {SECRET}"), ("eu.cdn.example-ats.com", None)]
    assert SECRET not in str(failed.value) and SECRET not in repr(http)

    formatter = JsonFormatter()
    record = logging.LogRecord("t", logging.ERROR, __file__, 1, f"auth=Bearer {SECRET}", None, None)
    assert SECRET not in formatter.format(record)
    assert SECRET not in redact(f"GET /jobs?api_key={SECRET}&page=2")
    assert SourceError(SourceErrorCode.PROVIDER_ERROR, f"leaked {SECRET}").detail.count(SECRET) == 0


# --- pipeline integration -----------------------------------------------------------------


class FeedProvider:
    """Stand-in for a future official-feed provider; all traffic goes to a MockTransport."""

    descriptor = FEED_DESCRIPTOR

    def fetch_page(self, request: FetchRequest) -> FetchPage:
        payload = request.http.get_json(request.config["feed_url"])
        return FetchPage(records=payload["jobs"], listing_complete=True)


def feed_record(source_job_id: str) -> dict[str, Any]:
    return {
        "source_job_id": source_job_id,
        "title": f"Engineer {source_job_id}",
        "employer": "Acme",
        "canonical_url": f"https://{FEED_HOST}/acme/jobs/{source_job_id}",
        # Untrusted text full of URLs that must never be fetched.
        "description": "Apply at https://evil.test/collect or http://169.254.169.254/ now.",
    }


def feed_connector(db, name: str = "acme-feed", **overrides: Any) -> SourceConnector:
    fields: dict[str, Any] = {
        "name": name,
        "kind": FEED_DESCRIPTOR.kind,
        "tier": "A",
        "base_domain": FEED_HOST,
        "terms_version": "acme-board-terms-2026-09",
        "policy_status": "approved",
        "enabled": True,
        "authorization_json": {
            "basis": "official_feed",
            "evidence_ref": "docs/source-connectors.md#test-only",
            "reviewed_by": "security-review",
            "reviewed_at": "2026-09-29T00:00:00+00:00",
            "terms_version": "acme-board-terms-2026-09",
        },
        "config_json": {
            "feed_url": f"https://{FEED_HOST}/v1/boards/acme/jobs",
            "fetch_hosts": [FEED_HOST],
            "allowed_url_hosts": [FEED_HOST],
        },
    }
    fields.update(overrides)
    connector = SourceConnector(**fields)
    db.add(connector)
    db.commit()
    return connector


def run_feed(db, connector, handler, *, allowlist=ALLOWLIST, network=True, resolver=None, **kw):
    registry = ProviderRegistry()
    registry.register(FEED_DESCRIPTOR.kind, FeedProvider)
    return run_ingestion(
        db,
        ArtifactStorage(get_settings()),
        connector_id=connector.id,
        registry=registry,
        network_access_enabled=network,
        egress_allowlist=allowlist,
        http_client_factory=partial(
            build_connector_http_client,
            resolver=resolver or resolver_for({}),
            transport=httpx.MockTransport(handler),
        ),
        **kw,
    )


@pytest.fixture()
def db():
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


def test_live_network_is_disabled_by_default_and_requires_an_allowlist():
    base: dict[str, Any] = {
        "_env_file": None, "SESSION_SECRET": "x" * 40, "ARTIFACT_ENCRYPTION_KEY": "k",
    }
    defaults = Settings(**base)
    assert defaults.source_network_access_enabled is False
    assert defaults.source_egress_allowlist_entries == ()
    enabled_without_allowlist = Settings(**{**base, "SOURCE_NETWORK_ACCESS_ENABLED": True})
    with pytest.raises(RuntimeError, match="SOURCE_EGRESS_ALLOWLIST"):
        enabled_without_allowlist.validate_runtime_secrets()
    assert get_settings().source_network_access_enabled is False


def test_network_connector_fails_closed_without_enablement_or_allowlist(db):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return json_response({"jobs": []})

    cases: list[tuple[SourceConnector, dict[str, Any], SourceErrorCode]] = [
        (feed_connector(db, "off"), {"network": False}, SourceErrorCode.NETWORK_ACCESS_DISABLED),
        (feed_connector(db, "no-list"), {"allowlist": ()}, SourceErrorCode.EGRESS_NOT_ALLOWLISTED),
        (
            feed_connector(db, "outside", config_json={"feed_url": "https://x.other.com/j",
                                                     "fetch_hosts": ["x.other.com"]}),
            {},
            SourceErrorCode.EGRESS_NOT_ALLOWLISTED,
        ),
        (
            feed_connector(db, "wide-wildcard", config_json={"feed_url": f"https://{FEED_HOST}/j",
                                                           "fetch_hosts": ["*.com"]}),
            {},
            SourceErrorCode.EGRESS_NOT_ALLOWLISTED,
        ),
        (
            feed_connector(db, "inline", config_json={"feed_url": f"https://{FEED_HOST}/j",
                                                    "fetch_hosts": [FEED_HOST],
                                                    "api_key": SECRET}),
            {},
            SourceErrorCode.INLINE_SECRET_IN_CONFIG,
        ),
        (
            feed_connector(db, "missing-cred", config_json={
                "feed_url": f"https://{FEED_HOST}/j", "fetch_hosts": [FEED_HOST],
                "credential": {"ref": "env:JFA_SOURCE_SECRET_NOT_SET"}}),
            {},
            SourceErrorCode.CREDENTIAL_UNAVAILABLE,
        ),
    ]
    for connector, options, code in cases:
        with pytest.raises(SourcePolicyError) as raised:
            run_feed(db, connector, handler, **options)
        assert raised.value.code == code, connector.name
        db.refresh(connector)
        assert connector.health_status == "blocked"
    assert calls == []
    assert SECRET not in json.dumps(
        [event.metadata_json for event in db.scalars(select(AuditEvent))]
    )


def test_live_style_run_ingests_audits_and_never_follows_job_urls(db):
    connector = feed_connector(db)
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return json_response({"jobs": [feed_record("a-1"), feed_record("a-2")]})

    result = run_feed(db, connector, handler)
    assert result.status == "succeeded" and result.count("created") == 2
    assert requested == [f"https://{FEED_HOST}/v1/boards/acme/jobs"]
    assert result.stats["network"] == {
        "requests": 1, "denied": 0, "errors": 0, "bytes": result.stats["network"]["bytes"],
        "hosts": [FEED_HOST],
    }
    event = db.scalars(
        select(AuditEvent).where(AuditEvent.action == "source.network_request")
    ).one()
    assert event.object_id == result.run_id
    assert event.metadata_json["host"] == FEED_HOST and event.metadata_json["status"] == 200
    assert "boards/acme" not in json.dumps(event.metadata_json)


def test_unsafe_redirect_during_a_run_blocks_the_source_without_retry(db):
    connector = feed_connector(db)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.test/exfil"})

    task = enqueue_ingestion(
        db, connector_id=connector.id, idempotency_key="k-redirect", requested_by_user_id=None
    )
    db.commit()
    registry = ProviderRegistry()
    registry.register(FEED_DESCRIPTOR.kind, FeedProvider)
    import app.services.sources.pipeline as pipeline_module

    original = pipeline_module.build_connector_http_client
    pipeline_module.build_connector_http_client = partial(
        original, resolver=resolver_for({}), transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.MonkeyPatch.context() as patch:
            settings = get_settings()
            patch.setattr(settings, "source_network_access_enabled", True)
            patch.setattr(settings, "source_egress_allowlist", ",".join(ALLOWLIST))
            run_next_ingestion_task(db, ArtifactStorage(settings), worker_id="w", registry=registry)
    finally:
        pipeline_module.build_connector_http_client = original
    db.refresh(task)
    db.refresh(connector)
    assert task.state == "failed_manual"  # Never retried around an egress denial.
    assert connector.health_status == "blocked"
    assert connector.last_error_code == "egress_denied"
    run = db.scalars(select(SourceRun).where(SourceRun.connector_id == connector.id)).one()
    assert (run.status, run.error_code) == ("blocked", "egress_denied")
    denied = db.scalars(select(AuditEvent).where(AuditEvent.action == "source.egress_denied")).one()
    assert denied.metadata_json["host"] == "evil.test"
    assert denied.metadata_json["reason"] == "host_not_allowlisted"
    assert db.query(JobPosting).count() == 0


def test_transient_provider_failure_degrades_then_recovers(db):
    connector = feed_connector(db)
    with pytest.raises(SourceError) as raised:
        run_feed(db, connector, lambda request: json_response({}, 503))
    assert raised.value.retryable
    db.refresh(connector)
    assert (connector.health_status, connector.consecutive_failures) == ("degraded", 1)
    run_feed(db, connector, lambda request: json_response({"jobs": [feed_record("r-1")]}))
    db.refresh(connector)
    assert connector.health_status == "healthy"
    changes = db.scalars(
        select(AuditEvent.metadata_json).where(AuditEvent.action == "source.health_changed")
    ).all()
    assert [(item["from"], item["to"]) for item in changes] == [
        ("unknown", "degraded"),
        ("degraded", "healthy"),
    ]


def test_private_network_source_is_tenant_isolated(db):
    owner = TestClient(app)
    other = TestClient(app)
    owner_id = owner.post(
        "/api/v1/auth/register", json={"email": "net-owner@example.test",
                                       "password": "network-password-123"},
    ).json()["id"]
    other.post(
        "/api/v1/auth/register",
        json={"email": "net-other@example.test", "password": "network-password-123"},
    )
    connector = feed_connector(db, "private-feed", owner_user_id=owner_id)
    run_feed(db, connector, lambda request: json_response({"jobs": [feed_record("p-1")]}))

    network_events = db.scalars(
        select(AuditEvent).where(AuditEvent.action == "source.network_request")
    ).all()
    assert network_events and all(event.user_id == owner_id for event in network_events)
    assert [item["name"] for item in owner.get("/api/v1/connectors/health").json()] == [
        "private-feed"
    ]
    assert other.get("/api/v1/connectors/health").json() == []
    other_id = other.get("/api/v1/auth/me").json()["id"]
    with pytest.raises(SourcePolicyError) as raised:
        run_feed(db, connector, lambda request: json_response({"jobs": []}),
                 requested_by_user_id=other_id)
    assert raised.value.code == SourceErrorCode.TENANT_MISMATCH
    # A tenant's source cannot reach beyond the deployment allowlist either.
    wider = feed_connector(
        db, "private-wide", owner_user_id=owner_id,
        config_json={"feed_url": "https://tenant-chosen.example.org/j",
                     "fetch_hosts": ["tenant-chosen.example.org"]},
    )
    with pytest.raises(SourcePolicyError) as outside:
        run_feed(db, wider, lambda request: json_response({"jobs": []}))
    assert outside.value.code == SourceErrorCode.EGRESS_NOT_ALLOWLISTED
    assert db.query(Task).count() == 0


# --- hardening found in security review -----------------------------------------------------


def test_dns_resolution_timeout_is_enforced(monkeypatch):
    import time as time_module

    import app.services.sources.egress as egress_module

    def hanging_getaddrinfo(*args: Any, **kwargs: Any) -> list[Any]:
        time_module.sleep(2)
        return []

    monkeypatch.setattr(egress_module, "DNS_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(egress_module.socket, "getaddrinfo", hanging_getaddrinfo)
    started = time_module.monotonic()
    with pytest.raises(SourceError) as raised:
        egress_module.system_resolver(FEED_HOST, 443)
    assert raised.value.code == SourceErrorCode.PROVIDER_TIMEOUT and raised.value.retryable
    assert time_module.monotonic() - started < 1.5


@pytest.mark.parametrize("entry", ["*", "*.com", "com", "127.0.0.1", "exa mple.com", "*.1"])
def test_overly_broad_or_invalid_deployment_allowlist_is_rejected(entry):
    base: dict[str, Any] = {
        "_env_file": None, "SESSION_SECRET": "x" * 40, "ARTIFACT_ENCRYPTION_KEY": "k",
        "SOURCE_NETWORK_ACCESS_ENABLED": True,
    }
    settings = Settings(**{**base, "SOURCE_EGRESS_ALLOWLIST": entry})
    with pytest.raises(RuntimeError):
        settings.validate_runtime_secrets()
    valid = Settings(
        **{**base, "SOURCE_EGRESS_ALLOWLIST": f"{FEED_HOST}, *.cdn.example-ats.com"}
    )
    valid.validate_runtime_secrets()


def test_only_the_egress_layer_may_open_network_connections():
    """Providers must use the bound client in FetchRequest.http, never their own sockets."""
    import pathlib
    import re

    sources = pathlib.Path(__file__).resolve().parent.parent / "app" / "services" / "sources"
    allowed = {"egress.py", "http_client.py"}
    network_import = re.compile(
        r"^\s*(import|from)\s+(httpx|httpcore|socket|ssl|requests|urllib3|aiohttp|"
        r"urllib\.request|http\.client|ftplib|smtplib)\b",
        re.MULTILINE,
    )
    offenders = [
        path.name
        for path in sources.rglob("*.py")
        if path.name not in allowed and network_import.search(path.read_text(encoding="utf-8"))
    ]
    # The pipeline only builds the bound client; it never sends requests itself.
    assert offenders == ["pipeline.py"]
    pipeline_source = (sources / "pipeline.py").read_text(encoding="utf-8")
    assert re.findall(r"^import httpx$", pipeline_source, re.MULTILINE) == ["import httpx"]
    assert "httpx.Client(" not in pipeline_source and ".send(" not in pipeline_source
