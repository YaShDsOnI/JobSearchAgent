"""Concurrency and constraint behavior that needs real parallel database connections.

Thread-based tests are skipped on the default in-memory SQLite database (one shared
connection); run with TEST_DATABASE_URL pointing at PostgreSQL to exercise them.
"""

import threading
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.db.models import JobPosting, JobSnapshot, SourceConnector, SourceRun, Task
from app.services.catalog import FIXTURE_AUTHORIZATION
from app.services.sources.base import FetchPage, FetchRequest
from app.services.sources.errors import SourceError, SourceErrorCode
from app.services.sources.fixture import FIXTURE_DESCRIPTOR
from app.services.sources.pipeline import run_ingestion
from app.services.sources.registry import ProviderRegistry
from app.services.sources.worker import enqueue_ingestion, run_next_ingestion_task
from app.services.storage import ArtifactStorage
from app.services.tasks import claim_next_task, enqueue_task
from tests.conftest import TestingSessionLocal, requires_postgres


def _connector(name: str = "concurrency") -> str:
    with TestingSessionLocal() as db:
        connector = SourceConnector(
            name=name,
            kind="fixture",
            tier="local",
            terms_version="fixture-v1",
            policy_status="approved",
            enabled=True,
            authorization_json=dict(FIXTURE_AUTHORIZATION),
            config_json={"allowed_url_hosts": ["example.invalid"]},
        )
        db.add(connector)
        db.commit()
        return connector.id


def _record(source_job_id: str) -> dict[str, Any]:
    return {
        "source_job_id": source_job_id,
        "title": f"Concurrent Role {source_job_id}",
        "employer": "Sanitized Employer",
        "canonical_url": f"https://example.invalid/jobs/{source_job_id}",
        "description": f"Sanitized fixture {source_job_id}.",
    }


class GatedProvider:
    """Blocks inside fetch until released, so runs overlap deterministically."""

    descriptor = FIXTURE_DESCRIPTOR

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def fetch_page(self, request: FetchRequest) -> FetchPage:
        self.calls += 1
        self.entered.set()
        assert self.release.wait(timeout=20)
        return FetchPage(records=[_record("gated-1")])


def _in_threads(count: int, target: Callable[[int], Any]) -> list[Any]:
    barrier = threading.Barrier(count)
    results: list[Any] = [None] * count

    def runner(index: int) -> None:
        barrier.wait(timeout=20)
        try:
            results[index] = target(index)
        except Exception as exc:  # Collected and asserted by the caller.
            results[index] = exc

    threads = [threading.Thread(target=runner, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return results


@requires_postgres
def test_overlapping_runs_for_one_connector_allow_exactly_one_writer():
    connector_id = _connector()
    provider = GatedProvider()
    registry = ProviderRegistry()
    registry.register("fixture", lambda: provider)
    storage = ArtifactStorage(get_settings())
    outcome: dict[str, Any] = {}

    def first() -> None:
        with TestingSessionLocal() as db:
            outcome["first"] = run_ingestion(
                db, storage, connector_id=connector_id, registry=registry
            )

    thread = threading.Thread(target=first)
    thread.start()
    assert provider.entered.wait(timeout=20)
    with TestingSessionLocal() as db, pytest.raises(SourceError) as second:
        run_ingestion(db, storage, connector_id=connector_id, registry=registry)
    provider.release.set()
    thread.join(timeout=60)

    assert second.value.code == SourceErrorCode.RUN_IN_PROGRESS
    assert outcome["first"].status == "succeeded"
    assert provider.calls == 1
    with TestingSessionLocal() as db:
        assert db.query(JobPosting).count() == 1
        assert db.query(SourceRun).count() == 1
        connector = db.get(SourceConnector, connector_id)
        assert connector is not None and connector.active_run_id is None


@requires_postgres
@pytest.mark.parametrize("round_number", range(5))
def test_simultaneous_run_starts_race_on_the_compare_and_set_lock(round_number):
    connector_id = _connector(f"race-{round_number}")
    storage = ArtifactStorage(get_settings())

    def attempt(index: int):
        provider = GatedProvider()
        provider.release.set()
        registry = ProviderRegistry()
        registry.register("fixture", lambda: provider)
        with TestingSessionLocal() as db:
            return run_ingestion(db, storage, connector_id=connector_id, registry=registry)

    results = _in_threads(6, attempt)
    errors = [item for item in results if isinstance(item, Exception)]
    successes = [item for item in results if not isinstance(item, Exception)]
    assert successes, results
    assert all(
        isinstance(item, SourceError) and item.code == SourceErrorCode.RUN_IN_PROGRESS
        for item in errors
    ), errors
    with TestingSessionLocal() as db:
        # However runs interleave, the job and its snapshot are written exactly once.
        assert db.query(JobPosting).count() == 1
        assert db.query(JobSnapshot).count() == 1
        runs = db.scalars(select(SourceRun)).all()
        assert all(run.status == "succeeded" for run in runs)
        connector = db.get(SourceConnector, connector_id)
        assert connector is not None and connector.active_run_id is None


@requires_postgres
def test_concurrent_enqueue_with_same_idempotency_key_creates_one_task():
    results = _in_threads(
        8,
        lambda index: _enqueue("catalog:concurrent:1", {"attempt": index}),
    )
    assert not [item for item in results if isinstance(item, Exception)], results
    assert len(set(results)) == 1
    with TestingSessionLocal() as db:
        assert db.query(Task).count() == 1


def _enqueue(key: str, payload: dict[str, Any]) -> str:
    with TestingSessionLocal() as db:
        task = enqueue_task(db, kind="source_ingest", idempotency_key=key, payload=payload)
        db.commit()
        return task.id


@requires_postgres
def test_competing_workers_claim_distinct_tasks():
    with TestingSessionLocal() as db:
        for index in range(6):
            enqueue_task(db, kind="source_ingest", idempotency_key=f"claim:{index}", payload={})
        db.commit()

    def claim(index: int) -> str | None:
        with TestingSessionLocal() as db:
            task = claim_next_task(db, worker_id=f"worker-{index}", lease_seconds=60)
            db.commit()
            return task.id if task else None

    claimed = [item for item in _in_threads(6, claim) if item is not None]
    assert len(claimed) == len(set(claimed)) == 6


@requires_postgres
def test_redelivered_ingestion_task_runs_once_across_workers():
    connector_id = _connector("redelivery")
    with TestingSessionLocal() as db:
        enqueue_ingestion(
            db, connector_id=connector_id, idempotency_key="nightly", requested_by_user_id=None
        )
        db.commit()
    registry = ProviderRegistry()
    registry.register(
        "fixture", lambda: _StaticProvider([_record("once-1"), _record("once-2")])
    )
    storage = ArtifactStorage(get_settings())

    def work(index: int):
        with TestingSessionLocal() as db:
            task = run_next_ingestion_task(
                db, storage, worker_id=f"w{index}", registry=registry
            )
            return task.id if task else None

    processed = [item for item in _in_threads(4, work) if item is not None]
    assert len(processed) == 1
    with TestingSessionLocal() as db:
        assert db.scalars(select(Task.state)).all() == ["succeeded"]
        assert db.query(JobPosting).count() == 2
        assert db.query(SourceRun).count() == 1


class _StaticProvider:
    descriptor = FIXTURE_DESCRIPTOR

    def __init__(self, records: list[dict[str, Any]]) -> None:
        self.records = records

    def fetch_page(self, request: FetchRequest) -> FetchPage:
        return FetchPage(records=self.records)


# Constraint checks run on every backend.


def test_snapshot_version_and_source_identity_are_unique_in_the_database():
    connector_id = _connector("constraints")
    with TestingSessionLocal() as db:
        job = JobPosting(
            connector_id=connector_id,
            source_job_id="c-1",
            canonical_hash="0" * 64,
            title="t",
            employer="e",
            canonical_url="https://example.invalid/jobs/c-1",
        )
        db.add(job)
        db.flush()
        db.add(JobSnapshot(job_id=job.id, version=1, content_hash="a" * 64))
        db.commit()
        job_id = job.id

    with TestingSessionLocal() as db, pytest.raises(IntegrityError):
        db.add(JobSnapshot(job_id=job_id, version=1, content_hash="b" * 64))
        db.commit()
    with TestingSessionLocal() as db, pytest.raises(IntegrityError):
        db.add(
            JobPosting(
                connector_id=connector_id,
                source_job_id="c-1",
                canonical_hash="1" * 64,
                title="t2",
                employer="e",
                canonical_url="https://example.invalid/jobs/c-1b",
            )
        )
        db.commit()
    # A revert to earlier content is a new version, never a hash-uniqueness violation.
    with TestingSessionLocal() as db:
        db.add(JobSnapshot(job_id=job_id, version=2, content_hash="a" * 64))
        db.commit()
