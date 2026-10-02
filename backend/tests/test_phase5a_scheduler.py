"""Phase 5A scheduler foundation and connector health monitoring."""

import threading
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.admin as admin_cli
from app.core.config import get_settings
from app.db.models import AuditEvent, JobPosting, SourceConnector, SourceRun, Task
from app.services.catalog import FIXTURE_AUTHORIZATION
from app.services.sources.errors import ProviderUnavailableError
from app.services.sources.health import connector_health_report
from app.services.sources.pipeline import reset_connector_circuit, run_ingestion
from app.services.sources.registry import ProviderRegistry
from app.services.sources.scheduler import (
    ScheduleError,
    schedule_due_runs,
    set_connector_schedule,
)
from app.services.sources.worker import run_next_ingestion_task
from app.services.storage import ArtifactStorage
from tests.conftest import TestingSessionLocal, app, csrf_headers, requires_postgres
from tests.test_phase4_discovery import ScriptedProvider, job_record

T0 = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
HOUR = 3600


def _connector(db, name: str = "scheduled", **fields: Any) -> SourceConnector:
    values: dict[str, Any] = {
        "name": name,
        "kind": "fixture",
        "tier": "local",
        "terms_version": "fixture-v1",
        "policy_status": "approved",
        "enabled": True,
        "authorization_json": dict(FIXTURE_AUTHORIZATION),
        "config_json": {"allowed_url_hosts": ["example.invalid"]},
    }
    values.update(fields)
    connector = SourceConnector(**values)
    db.add(connector)
    db.commit()
    return connector


def _scheduled(db, name: str = "scheduled", interval: int = HOUR, **fields: Any):
    connector = _connector(db, name, **fields)
    set_connector_schedule(db, connector, interval_seconds=interval, actor="test", user_id=None,
                           now=T0)
    db.commit()
    return connector


def _tasks(db) -> list[Task]:
    return list(db.scalars(select(Task).order_by(Task.created_at, Task.id)).all())


@pytest.fixture()
def db():
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


def test_schedule_validation_and_audit(db):
    connector = _connector(db)
    for bad in (60, 8 * 24 * HOUR):
        with pytest.raises(ScheduleError):
            set_connector_schedule(db, connector, interval_seconds=bad, actor="t", user_id=None)
    set_connector_schedule(db, connector, interval_seconds=HOUR, actor="t", user_id=None, now=T0)
    db.commit()
    assert connector.schedule_interval_seconds == HOUR
    event = db.scalar(select(AuditEvent).where(AuditEvent.action == "source.schedule_changed"))
    assert event is not None and event.metadata_json == {"from_seconds": None, "to_seconds": HOUR}
    set_connector_schedule(db, connector, interval_seconds=None, actor="t", user_id=None)
    db.commit()
    assert schedule_due_runs(db, now=T0 + timedelta(days=3)) == []


def test_due_connector_is_queued_once_per_slot(db):
    connector = _scheduled(db)
    [first] = schedule_due_runs(db, now=T0 + timedelta(minutes=5))
    assert (first.action, first.reason) == ("enqueued", "due")
    db.refresh(connector)
    assert connector.next_run_due_at.replace(tzinfo=UTC) == T0 + timedelta(hours=1)
    # Not due again inside the slot.
    assert schedule_due_runs(db, now=T0 + timedelta(minutes=30)) == []
    # A replayed tick for the same slot (e.g. clock skew, double cron) reuses the task.
    connector.next_run_due_at = T0
    db.commit()
    task = db.get(Task, first.task_id)
    assert task is not None
    task.state = "succeeded"
    db.commit()
    [replay] = schedule_due_runs(db, now=T0 + timedelta(minutes=40))
    assert replay.task_id == first.task_id
    assert len(_tasks(db)) == 1
    # The next slot gets its own task.
    [following] = schedule_due_runs(db, now=T0 + timedelta(hours=1, minutes=1))
    assert following.task_id != first.task_id and len(_tasks(db)) == 2
    assert all(t.payload_json["requested_by_user_id"] is None for t in _tasks(db))


def test_overlapping_runs_are_not_scheduled(db):
    pending = _scheduled(db, "pending")
    running = _scheduled(db, "running")
    decisions = {d.connector_id: d for d in schedule_due_runs(db, now=T0)}
    assert decisions[pending.id].action == "enqueued"
    # Queued task still outstanding -> the next slot is skipped, not stacked.
    decisions = {d.connector_id: d for d in schedule_due_runs(db, now=T0 + timedelta(hours=2))}
    assert decisions[pending.id].reason == "run_pending"
    # A connector with a live run is skipped even without a queued task.
    for task in _tasks(db):
        task.state = "succeeded"
    active = SourceRun(connector_id=running.id, status="running", started_at=T0)
    db.add(active)
    db.flush()
    running.active_run_id = active.id
    running.next_run_due_at = T0
    db.commit()
    decisions = {d.connector_id: d for d in schedule_due_runs(db, now=T0 + timedelta(hours=3))}
    assert decisions[running.id].reason == "run_in_progress"


def test_suspended_and_blocked_sources_are_never_auto_reenabled(db):
    suspended = _scheduled(db, "suspended-src", health_status="suspended",
                           consecutive_failures=5)
    blocked = _scheduled(db, "blocked-src", health_status="blocked")
    for days in (0, 1, 30):
        tick = schedule_due_runs(db, now=T0 + timedelta(days=days))
        decisions = {d.connector_id: d for d in tick}
        assert decisions[suspended.id].reason == "health_suspended"
        assert decisions[blocked.id].reason == "health_blocked"
    db.refresh(suspended)
    assert suspended.health_status == "suspended" and _tasks(db) == []

    reset_connector_circuit(db, connector=suspended, actor="operator", user_id=None)
    db.commit()
    decisions = {d.connector_id: d for d in schedule_due_runs(db, now=T0 + timedelta(days=31))}
    assert decisions[suspended.id].action == "enqueued"


def test_disabled_or_unapproved_connectors_are_not_scheduled(db):
    _scheduled(db, "disabled", enabled=False)
    _scheduled(db, "unapproved", policy_status="review_required")
    _connector(db, "manual-only")
    assert schedule_due_runs(db, now=T0 + timedelta(days=1)) == []


def test_scheduled_run_retries_temporary_failures_without_overlap(db):
    connector = _scheduled(db)
    provider = ScriptedProvider(ProviderUnavailableError("503"), [job_record("sched-1")])
    registry = ProviderRegistry()
    registry.register("fixture", lambda: provider)
    storage = ArtifactStorage(get_settings())

    [decision] = schedule_due_runs(db, now=T0)
    run_next_ingestion_task(db, storage, worker_id="w1", registry=registry)
    task = db.get(Task, decision.task_id)
    assert task is not None
    db.refresh(task)
    assert (task.state, task.attempts) == ("queued", 1)
    db.refresh(connector)
    assert connector.health_status == "degraded"

    # The retry is still pending, so the next slot does not stack a second run.
    [skip] = schedule_due_runs(db, now=T0 + timedelta(hours=1, minutes=1))
    assert skip.reason == "run_pending"

    task.next_run_at = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()
    run_next_ingestion_task(db, storage, worker_id="w2", registry=registry)
    db.refresh(task)
    db.refresh(connector)
    assert (task.state, task.attempts) == ("succeeded", 2)
    assert connector.health_status == "healthy"
    assert db.query(JobPosting).count() == 1
    assert db.scalar(select(AuditEvent).where(AuditEvent.action == "source.run_scheduled"))


def test_health_report_states_overdue_and_alerts(db):
    now = T0 + timedelta(hours=5)
    states = {
        "ok": ("healthy", now - timedelta(minutes=10)),
        "bad": ("failing", None),
        "slow": ("degraded", now - timedelta(minutes=10)),
        "stop": ("blocked", None),
        "open": ("suspended", None),
        "late": ("healthy", now - timedelta(hours=4)),
    }
    for name, (health, success) in states.items():
        connector = _scheduled(db, name, health_status=health, last_success_at=success)
        connector.created_at = T0
    db.commit()
    db.add(SourceRun(connector_id=db.scalars(select(SourceConnector.id).where(
        SourceConnector.name == "bad")).one(), status="failed", started_at=now))
    db.commit()
    report = {
        item["name"]: item
        for item in connector_health_report(db, db.scalars(select(SourceConnector)).all(), now=now)
    }
    assert {name: item["state"] for name, item in report.items()} == {
        "ok": "success", "bad": "failure", "slow": "degraded", "stop": "blocked",
        "open": "suspended", "late": "success",
    }
    assert {name for name, item in report.items() if item["alert"]} == {
        "bad", "stop", "open", "late",
    }
    assert report["late"]["overdue"] and not report["ok"]["overdue"]
    assert report["bad"]["runs_24h"] == {"failed": 1}


def test_health_endpoint_and_cli_are_tenant_scoped_and_alerting(monkeypatch, capsys):
    owner = TestClient(app)
    other = TestClient(app)
    owner_id = owner.post(
        "/api/v1/auth/register",
        json={"email": "health-owner@example.test", "password": "health-password-123"},
    ).json()["id"]
    other.post(
        "/api/v1/auth/register",
        json={"email": "health-other@example.test", "password": "health-password-123"},
    )
    with TestingSessionLocal() as db:
        _connector(db, "shared-healthy", health_status="healthy")
        _connector(db, "private-suspended", owner_user_id=owner_id, health_status="suspended")
    owner_view = {i["name"]: i["state"] for i in owner.get("/api/v1/connectors/health").json()}
    other_view = {i["name"]: i["state"] for i in other.get("/api/v1/connectors/health").json()}
    assert owner_view == {"shared-healthy": "success", "private-suspended": "suspended"}
    assert other_view == {"shared-healthy": "success"}
    listing = owner.get("/api/v1/connectors").json()
    assert {"schedule_interval_seconds", "next_run_due_at"} <= set(listing[0])

    monkeypatch.setattr(admin_cli, "SessionLocal", TestingSessionLocal)
    assert admin_cli.main(["health-report"]) == 3
    assert "private-suspended" in capsys.readouterr().out
    assert admin_cli.main(["set-schedule", "--connector", "shared-healthy",
                           "--interval-minutes", "5"]) == 1
    assert admin_cli.main(["set-schedule", "--connector", "shared-healthy",
                           "--interval-minutes", "60"]) == 0
    assert admin_cli.main(["set-schedule", "--connector", "nope", "--disable"]) == 1
    # Owners can reset their own suspended private source; nothing resets it automatically.
    with TestingSessionLocal() as db:
        private_id = db.scalars(select(SourceConnector.id).where(
            SourceConnector.name == "private-suspended")).one()
    assert other.post(f"/api/v1/connectors/{private_id}/reset-circuit",
                      headers=csrf_headers(other)).status_code == 404
    reset = owner.post(f"/api/v1/connectors/{private_id}/reset-circuit",
                       headers=csrf_headers(owner))
    assert reset.status_code == 200 and reset.json()["health_status"] == "unknown"


def test_suspension_after_repeated_failures_blocks_runs_until_reset(db):
    connector = _connector(db, "breaker")
    provider = ScriptedProvider(ProviderUnavailableError("down"))
    registry = ProviderRegistry()
    registry.register("fixture", lambda: provider)
    storage = ArtifactStorage(get_settings())
    for _ in range(5):
        with pytest.raises(ProviderUnavailableError):
            run_ingestion(db, storage, connector_id=connector.id, registry=registry)
    db.refresh(connector)
    assert connector.health_status == "suspended"
    calls = provider.calls
    with pytest.raises(Exception):  # noqa: B017 - circuit open (SourcePolicyError)
        run_ingestion(db, storage, connector_id=connector.id, registry=registry)
    assert provider.calls == calls
    transitions = [
        (e["from"], e["to"]) for e in db.scalars(
            select(AuditEvent.metadata_json).where(AuditEvent.action == "source.health_changed")
        )
    ]
    assert transitions == [
        ("unknown", "degraded"), ("degraded", "failing"), ("failing", "suspended"),
    ]


@requires_postgres
def test_concurrent_scheduler_ticks_create_one_task_per_slot():
    with TestingSessionLocal() as db:
        _scheduled(db, "concurrent-tick")
    barrier = threading.Barrier(4)
    errors: list[BaseException] = []

    def tick() -> None:
        try:
            barrier.wait(timeout=20)
            with TestingSessionLocal() as db:
                schedule_due_runs(db, now=T0 + timedelta(minutes=1))
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=tick) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert errors == []
    with TestingSessionLocal() as db:
        assert db.query(Task).count() == 1
