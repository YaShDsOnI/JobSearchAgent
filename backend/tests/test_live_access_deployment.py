"""Live-access deployment controls: egress proxy tunnel, file secrets, alerts, run loops.

Design: docs/source-connectors.md, "Deployment enforcement". The real Squid proxy is
verified separately with deploy/egress-proxy/verify.py (see docs/testing.md); these tests
cover the application side without any network access.
"""

import json
import logging
import os
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpcore
import httpx
import pytest
from sqlalchemy import select

import app.scheduler as scheduler_cli
import app.worker as worker_cli
from app.core.config import Settings, get_settings
from app.core.shutdown import StopSignal
from app.db.models import AuditEvent, SourceConnector
from app.services.sources import alerts
from app.services.sources.credentials import (
    MAX_SECRET_FILE_BYTES,
    resolve_credential,
    resolve_secret_ref,
)
from app.services.sources.egress import (
    EgressDenied,
    EgressGuard,
    EgressPolicy,
    PinnedTransport,
    ProxyTunnelBackend,
    ProxyTunnelTransport,
    build_transport,
)
from app.services.sources.errors import SourceErrorCode, SourcePolicyError
from app.services.sources.http_client import ConnectorHttpClient
from app.services.sources.pipeline import build_connector_http_client
from app.services.storage import ArtifactStorage
from tests.conftest import TestingSessionLocal
from tests.test_phase4_discovery import register
from tests.test_phase5a_network import FEED_HOST, LIMITS, PUBLIC_IP
from tests.test_phase5a_scheduler import _connector

PROXY = ("egress-proxy", 3128)
HOOK_HOST = "hooks.example-oncall.com"
HOOK_URL = f"https://{HOOK_HOST}/v1/events/tok-6f1e2d3c4b5a"
T0 = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)


def _settings(**values: Any) -> Settings:
    """A Settings object built from aliases, validated like process start-up."""
    base: dict[str, Any] = {
        "SESSION_SECRET": "x" * 40,
        "ARTIFACT_ENCRYPTION_KEY": os.environ["ARTIFACT_ENCRYPTION_KEY"],
        "APP_ENV": "test",
    }
    base.update(values)
    settings = Settings(**base)
    settings.validate_runtime_secrets()
    return settings


def _no_dns(host: str, port: int) -> list[str]:
    raise AssertionError(f"the application must not resolve {host} in proxy mode")


# --- configuration -------------------------------------------------------------------------


def test_network_access_outside_development_requires_the_egress_proxy():
    production = {
        "APP_ENV": "production",
        "COOKIE_SECURE": "true",
        "SOURCE_NETWORK_ACCESS_ENABLED": "true",
        "SOURCE_EGRESS_ALLOWLIST": "api.ashbyhq.com",
    }
    with pytest.raises(RuntimeError, match="SOURCE_EGRESS_PROXY_URL is required"):
        _settings(**production)
    settings = _settings(**production, SOURCE_EGRESS_PROXY_URL="http://egress-proxy:3128")
    assert settings.source_egress_proxy == PROXY
    # Development keeps the direct DNS-pinning transport; network-off needs no proxy.
    assert _settings(
        SOURCE_NETWORK_ACCESS_ENABLED="true", SOURCE_EGRESS_ALLOWLIST="api.ashbyhq.com"
    ).source_egress_proxy is None
    assert _settings(APP_ENV="production", COOKIE_SECURE="true").source_egress_proxy is None


@pytest.mark.parametrize(
    "url",
    [
        "https://egress-proxy:3128",
        "http://egress-proxy",
        "http://user:pw@egress-proxy:3128",
        "http://egress-proxy:3128/path",
        "http://egress-proxy:3128?x=1",
        "socks5://egress-proxy:1080",
        "http://:3128",
    ],
)
def test_malformed_proxy_urls_fail_startup(url):
    with pytest.raises(RuntimeError, match="SOURCE_EGRESS_PROXY_URL"):
        _settings(SOURCE_EGRESS_PROXY_URL=url)


def test_alert_settings_must_be_complete_and_pin_one_exact_host():
    with pytest.raises(RuntimeError, match="set together"):
        _settings(ALERT_WEBHOOK_REF="file:alert_webhook")
    with pytest.raises(RuntimeError, match="set together"):
        _settings(ALERT_WEBHOOK_HOST=HOOK_HOST)
    for bad in ("*.example-oncall.com", "localhost", "10.0.0.1", "hooks example.com"):
        with pytest.raises(RuntimeError, match="ALERT_WEBHOOK_HOST"):
            _settings(ALERT_WEBHOOK_REF="file:alert_webhook", ALERT_WEBHOOK_HOST=bad)
    assert _settings(
        ALERT_WEBHOOK_REF="file:alert_webhook", ALERT_WEBHOOK_HOST=HOOK_HOST
    ).alerting_enabled
    assert not _settings().alerting_enabled


# --- proxy tunnel ---------------------------------------------------------------------------


class RecordingStream(httpcore.MockStream):
    def __init__(self, buffer: list[bytes], written: list[bytes]) -> None:
        super().__init__(buffer)
        self._written = written

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self._written.append(buffer)

    def start_tls(self, ssl_context: Any, server_hostname: str | None = None,
                  timeout: float | None = None) -> "RecordingStream":
        self._written.append(f"<TLS sni={server_hostname}>".encode())
        return self


class RecordingProxyBackend(httpcore.NetworkBackend):
    def __init__(self, responses: list[bytes]) -> None:
        self.connections: list[tuple[str, int]] = []
        self.written: list[bytes] = []
        self._responses = responses

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.connections.append((host, port))
        return RecordingStream(list(self._responses), self.written)


def _proxied_responses(body: bytes) -> list[bytes]:
    return [
        b"HTTP/1.1 200 Connection established\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
        + str(len(body)).encode() + b"\r\n\r\n" + body,
    ]


def _proxy_guard(hosts: tuple[str, ...] = (FEED_HOST,)) -> EgressGuard:
    return EgressGuard(
        EgressPolicy(deployment_allowlist=hosts, connector_hosts=hosts), _no_dns,
        resolve_dns=False,
    )


def test_requests_are_tunneled_through_the_proxy_with_tls_to_the_target():
    egress = _proxy_guard()
    inner = RecordingProxyBackend(_proxied_responses(b'{"jobs": []}'))
    transport = ProxyTunnelTransport(egress, PROXY, inner_backend=inner)
    # Guards against an httpx/httpcore upgrade silently dropping the proxy-only backend.
    assert isinstance(transport._pool, httpcore.HTTPProxy)
    assert isinstance(transport._pool._network_backend, ProxyTunnelBackend)
    http = ConnectorHttpClient(guard=egress, limits=LIMITS, transport=transport)
    assert http.get_json(f"https://{FEED_HOST}/jobs?page=2") == {"jobs": []}
    http.close()
    # The only TCP connection is to the proxy; the target name is only in CONNECT and SNI.
    assert inner.connections == [PROXY]
    sent = b"".join(inner.written).decode("latin-1")
    assert sent.startswith(f"CONNECT {FEED_HOST}:443 HTTP/1.1\r\n")
    assert f"<TLS sni={FEED_HOST}>" in sent
    # Path and query only travel inside the tunnel, after TLS starts.
    assert sent.index(f"<TLS sni={FEED_HOST}>") < sent.index("GET /jobs?page=2 HTTP/1.1")


def test_proxy_backend_connects_to_the_proxy_and_nothing_else():
    inner = RecordingProxyBackend([])
    backend = ProxyTunnelBackend("Egress-Proxy.", 3128, inner)
    backend.connect_tcp("egress-proxy", 3128)
    for host, port in (
        (FEED_HOST, 443), (PUBLIC_IP, 443), ("egress-proxy", 443), ("127.0.0.1", 3128),
        ("169.254.169.254", 80),
    ):
        with pytest.raises(EgressDenied) as denied:
            backend.connect_tcp(host, port)
        assert denied.value.reason == "proxy_only"
    with pytest.raises(EgressDenied):
        backend.connect_unix_socket("/var/run/docker.sock")
    assert inner.connections == [PROXY]


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        (f"http://{FEED_HOST}/jobs", "scheme_not_https"),
        (f"https://{FEED_HOST}:8443/jobs", "port_not_allowed"),
        ("https://evil.example.net/jobs", "host_not_allowlisted"),
        ("https://169.254.169.254/latest/meta-data", "host_not_allowlisted"),
    ],
)
def test_proxy_transport_rechecks_requests_even_if_the_guard_is_bypassed(url, reason):
    inner = RecordingProxyBackend([])
    transport = ProxyTunnelTransport(_proxy_guard(), PROXY, inner_backend=inner)
    with pytest.raises(EgressDenied) as denied:
        transport.handle_request(httpx.Request("GET", url))
    assert denied.value.reason == reason
    assert inner.connections == []


def test_guard_checks_still_apply_in_proxy_mode_without_local_dns():
    egress = _proxy_guard()
    assert egress.check_url(f"https://{FEED_HOST}/jobs") == FEED_HOST  # _no_dns not called
    for url in (
        "https://evil.example.net/", f"https://user@{FEED_HOST}/", "https://93.184.216.34/",
        f"http://{FEED_HOST}/", f"https://{FEED_HOST}:444/",
    ):
        with pytest.raises(EgressDenied):
            egress.check_url(url)


def test_pipeline_selects_the_proxy_transport_from_settings():
    connector = SourceConnector(
        name="proxied", kind="fixture", base_domain=FEED_HOST, config_json={}
    )
    proxied = build_connector_http_client(
        connector, _settings(SOURCE_EGRESS_PROXY_URL="http://egress-proxy:3128"), (FEED_HOST,),
        resolver=_no_dns,
    )
    assert proxied.proxy == PROXY and proxied.guard.resolve_dns is False
    assert isinstance(proxied._http()._transport, ProxyTunnelTransport)
    proxied.close()
    direct = build_connector_http_client(connector, _settings(), (FEED_HOST,))
    assert direct.proxy is None and direct.guard.resolve_dns is True
    assert isinstance(direct._http()._transport, PinnedTransport)
    direct.close()
    assert isinstance(build_transport(_proxy_guard(), None), PinnedTransport)


# --- file secret references -------------------------------------------------------------


def test_file_references_resolve_from_the_secrets_directory(tmp_path):
    settings = _settings(SECRETS_DIR=str(tmp_path))
    (tmp_path / "acme_token").write_text("tok-file-123\n", encoding="utf-8")
    assert resolve_secret_ref("file:acme_token", settings).reveal() == "tok-file-123"
    (tmp_path / "acme_token").write_bytes(b"tok-rotated-456\r\n")
    # Read on every resolution: rotation needs no restart.
    assert resolve_secret_ref("file:acme_token", settings).reveal() == "tok-rotated-456"
    credential = resolve_credential(
        {"credential": {"ref": "file:acme_token", "header": "X-Api-Key"}}, settings
    )
    assert credential is not None and credential.header == "X-Api-Key"
    assert "tok-rotated-456" not in repr(credential)


def test_file_references_cannot_escape_or_read_unsafe_files(tmp_path):
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("not-a-secret-for-you", encoding="utf-8")
    settings = _settings(SECRETS_DIR=str(secrets_dir))
    (secrets_dir / "empty").write_text("", encoding="utf-8")
    (secrets_dir / "huge").write_text("x" * (MAX_SECRET_FILE_BYTES + 1), encoding="utf-8")
    (secrets_dir / "multi").write_text("line1\nline2\n", encoding="utf-8")
    (secrets_dir / "subdir").mkdir()
    refs = [
        "file:../outside.txt", "file:/etc/passwd", "file:..", "file:.hidden", "file:UPPER",
        "file:a/b", "file:", "file:missing", "file:empty", "file:huge", "file:multi",
        "file:subdir", "vault:acme", "acme_token",
    ]
    try:
        (secrets_dir / "escape").symlink_to(outside)
        refs.append("file:escape")
    except OSError:
        pass  # Symlinks need privileges on some Windows hosts.
    for ref in refs:
        with pytest.raises(SourcePolicyError) as raised:
            resolve_secret_ref(ref, settings)
        assert raised.value.code == SourceErrorCode.CREDENTIAL_UNAVAILABLE, ref
        assert "not-a-secret-for-you" not in str(raised.value)
    with pytest.raises(SourcePolicyError):
        resolve_secret_ref("file:acme_token", _settings())  # SECRETS_DIR unset


def test_environment_references_are_refused_outside_development(monkeypatch):
    monkeypatch.setenv("JFA_SOURCE_SECRET_ACME", "env-secret-value")
    assert resolve_secret_ref("env:JFA_SOURCE_SECRET_ACME", _settings()).reveal() == (
        "env-secret-value"
    )
    production = _settings(APP_ENV="production", COOKIE_SECURE="true")
    with pytest.raises(SourcePolicyError, match="not allowed in this environment"):
        resolve_secret_ref("env:JFA_SOURCE_SECRET_ACME", production)
    with pytest.raises(SourcePolicyError):
        resolve_credential({"credential": {"ref": "env:JFA_SOURCE_SECRET_ACME"}}, production)


# --- worker and scheduler loops ----------------------------------------------------------


def test_stop_signal_interrupts_waits():
    stop = StopSignal()
    assert stop.wait(0) is False and not stop.stopped
    threading.Timer(0.05, stop.stop).start()
    assert stop.wait(5) is True and stop.stopped


def test_worker_loop_polls_until_stopped_and_batch_mode_drains(monkeypatch):
    monkeypatch.setattr(worker_cli, "SessionLocal", TestingSessionLocal)
    queue = ["task-a", "task-b"]
    calls: list[str] = []

    class FakeTask:
        def __init__(self, task_id: str) -> None:
            self.id, self.state = task_id, "succeeded"

    def fake_next(db, storage, *, worker_id):
        calls.append(worker_id)
        return FakeTask(queue.pop(0)) if queue else None

    monkeypatch.setattr(worker_cli, "run_next_ingestion_task", fake_next)
    storage = ArtifactStorage(get_settings())
    # Batch mode: drains the queue, then exits when it is empty.
    assert worker_cli.run_worker(
        storage, worker_id="w", stop=StopSignal(), limit=10, loop=False, poll_seconds=0
    ) == 2
    # Loop mode: keeps polling an empty queue until a stop is requested.
    queue.extend(["task-c"])
    stop = StopSignal()
    timer = threading.Timer(0.3, stop.stop)
    timer.start()
    processed = worker_cli.run_worker(
        storage, worker_id="w", stop=stop, limit=None, loop=True, poll_seconds=0.01
    )
    timer.join()
    assert processed == 1 and stop.stopped and len(calls) >= 4
    # A stop requested before start processes nothing.
    queue.extend(["task-d"])
    assert worker_cli.run_worker(
        storage, worker_id="w", stop=stop, limit=None, loop=True, poll_seconds=0.01
    ) == 0
    assert queue == ["task-d"]


def test_scheduler_tick_isolates_alert_failures(monkeypatch, capsys, caplog):
    monkeypatch.setattr(scheduler_cli, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(scheduler_cli, "schedule_due_runs", lambda db: [])
    alerting = get_settings().model_copy(
        update={"alert_webhook_ref": "file:alert_webhook", "alert_webhook_host": HOOK_HOST}
    )
    monkeypatch.setattr(scheduler_cli, "get_settings", lambda: alerting)

    def broken(db):
        raise ValueError(f"boom {HOOK_URL}")

    monkeypatch.setattr(scheduler_cli, "send_health_alerts", broken)
    with caplog.at_level(logging.ERROR):
        scheduler_cli.run_tick()  # Must not raise: scheduling continues.
    assert capsys.readouterr().out.strip() == "[]"
    assert "ValueError" in caplog.text and HOOK_URL not in caplog.text


# --- health alert routing -------------------------------------------------------------------


@pytest.fixture()
def db():
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def alert_settings(tmp_path) -> Settings:
    (tmp_path / "alert_webhook").write_text(HOOK_URL + "\n", encoding="utf-8")
    return get_settings().model_copy(
        update={
            "alert_webhook_ref": "file:alert_webhook",
            "alert_webhook_host": HOOK_HOST,
            "secrets_dir": str(tmp_path),
        }
    )


class Hook:
    def __init__(self, status: int = 202) -> None:
        self.status = status
        self.received: list[dict[str, Any]] = []
        self.urls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.urls.append(str(request.url))
        assert request.method == "POST"
        assert request.headers["content-type"] == "application/json"
        self.received.append(json.loads(request.content))
        return httpx.Response(self.status)

    def factory(self) -> Callable[[Settings], Any]:
        return lambda settings: alerts.build_webhook_sender(
            settings,
            resolver=lambda host, port: [PUBLIC_IP],
            transport=httpx.MockTransport(self.handler),
        )


def _audits(db, action: str) -> list[AuditEvent]:
    return list(
        db.scalars(
            select(AuditEvent).where(AuditEvent.action == action).order_by(AuditEvent.occurred_at)
        )
    )


def _set_health(db, connector: SourceConnector, status: str, failures: int = 0) -> None:
    connector.health_status = status
    connector.consecutive_failures = failures
    connector.last_error_code = "provider_unavailable" if failures else None
    db.commit()


def test_alerts_fire_once_remind_change_and_resolve(db, alert_settings):
    connector = _connector(db, "ashby-acme")
    _set_health(db, connector, "suspended", failures=5)
    hook = Hook()
    send = lambda when: alerts.send_health_alerts(  # noqa: E731
        db, alert_settings, now=when, sender_factory=hook.factory()
    )

    first = send(T0)
    assert [(o.kind, o.state, o.delivered) for o in first] == [("firing", "suspended", True)]
    assert hook.urls == [HOOK_URL]
    assert hook.received[0] == {
        "source": "jobfinderagent", "kind": "firing", "connector_id": connector.id,
        "connector_name": "ashby-acme", "scope": "shared", "state": "suspended",
        "overdue": False, "consecutive_failures": 5,
        "last_error_code": "provider_unavailable", "at": "2026-11-13T12:00:00Z",
    }
    assert send(T0 + timedelta(minutes=5)) == []  # Deduplicated through the audit log.
    assert send(T0 + alerts.REMINDER_INTERVAL - timedelta(seconds=1)) == []
    reminder = send(T0 + alerts.REMINDER_INTERVAL)
    assert [(o.kind, o.state) for o in reminder] == [("firing", "suspended")]

    _set_health(db, connector, "blocked", failures=5)
    changed = send(T0 + alerts.REMINDER_INTERVAL + timedelta(minutes=1))
    assert [(o.kind, o.state) for o in changed] == [("firing", "blocked")]

    _set_health(db, connector, "healthy")
    resolved = send(T0 + alerts.REMINDER_INTERVAL + timedelta(minutes=2))
    assert [(o.kind, o.state) for o in resolved] == [("resolved", "success")]
    assert send(T0 + timedelta(days=1)) == []
    assert [e.metadata_json["kind"] for e in _audits(db, alerts.NOTIFIED_ACTION)] == [
        "firing", "firing", "firing", "resolved",
    ]


def test_disabling_an_alerting_connector_resolves_it(db, alert_settings):
    connector = _connector(db, "kill-switched")
    _set_health(db, connector, "failing", failures=3)
    hook = Hook()
    assert alerts.send_health_alerts(db, alert_settings, now=T0, sender_factory=hook.factory())
    connector.enabled = False
    db.commit()
    outcomes = alerts.send_health_alerts(
        db, alert_settings, now=T0 + timedelta(minutes=1), sender_factory=hook.factory()
    )
    assert [o.kind for o in outcomes] == ["resolved"]


def test_private_source_names_are_withheld(db, alert_settings, client):
    owner_id = register(client, "private-alerts@example.test")
    connector = _connector(db, "my-secret-job-hunt", owner_user_id=owner_id)
    _set_health(db, connector, "suspended", failures=5)
    hook = Hook()
    alerts.send_health_alerts(db, alert_settings, now=T0, sender_factory=hook.factory())
    assert hook.received[0]["connector_name"] is None
    assert hook.received[0]["scope"] == "private"
    assert "my-secret-job-hunt" not in json.dumps(hook.received)


def test_failed_deliveries_are_audited_and_retried_without_leaking_the_url(
    db, alert_settings, caplog
):
    connector = _connector(db, "flaky")
    _set_health(db, connector, "suspended", failures=5)
    hook = Hook(status=500)
    with caplog.at_level(logging.WARNING):
        failed = alerts.send_health_alerts(
            db, alert_settings, now=T0, sender_factory=hook.factory()
        )
    assert [(o.delivered, o.reason) for o in failed] == [(False, "status_500")]
    assert _audits(db, alerts.NOTIFIED_ACTION) == []
    hook.status = 202
    retried = alerts.send_health_alerts(
        db, alert_settings, now=T0 + timedelta(minutes=1), sender_factory=hook.factory()
    )
    assert [o.delivered for o in retried] == [True]
    stored = json.dumps([e.metadata_json for e in db.scalars(select(AuditEvent))])
    for leaked in (HOOK_URL, "tok-6f1e2d3c4b5a"):
        assert leaked not in stored and leaked not in caplog.text


def test_webhook_host_is_pinned_and_redirects_are_not_followed(db, alert_settings, tmp_path):
    connector = _connector(db, "pinned")
    _set_health(db, connector, "suspended", failures=5)
    (tmp_path / "alert_webhook").write_text("https://attacker.example.net/collect", "utf-8")
    hook = Hook()
    outcome = alerts.send_health_alerts(
        db, alert_settings, now=T0, sender_factory=hook.factory()
    )
    assert outcome[0].reason == "egress_denied:host_not_allowlisted" and hook.urls == []

    (tmp_path / "alert_webhook").write_text(HOOK_URL, "utf-8")
    redirect = Hook(status=307)
    outcome = alerts.send_health_alerts(
        db, alert_settings, now=T0 + timedelta(minutes=1), sender_factory=redirect.factory()
    )
    assert outcome[0].reason == "status_307" and len(redirect.urls) == 1


def test_alerting_is_off_unless_configured_and_missing_secret_fails_closed(db, tmp_path):
    connector = _connector(db, "quiet")
    _set_health(db, connector, "suspended", failures=5)
    hook = Hook()
    assert alerts.send_health_alerts(
        db, get_settings(), now=T0, sender_factory=hook.factory()
    ) == []
    missing = get_settings().model_copy(
        update={
            "alert_webhook_ref": "file:alert_webhook",
            "alert_webhook_host": HOOK_HOST,
            "secrets_dir": str(tmp_path),
        }
    )
    outcome = alerts.send_health_alerts(db, missing, now=T0, sender_factory=hook.factory())
    assert [o.reason for o in outcome] == ["webhook_unavailable:credential_unavailable"]
    assert hook.urls == []


def test_alert_code_has_no_direct_network_imports():
    source = Path(alerts.__file__).read_text(encoding="utf-8")
    for module in ("import httpx", "import httpcore", "import socket", "import ssl"):
        assert module not in source
