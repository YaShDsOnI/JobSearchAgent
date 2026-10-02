"""Durable-task integration for ingestion with classified, bounded retries."""

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.db.models import Task
from app.services.sources.errors import SourceError
from app.services.sources.pipeline import HttpClientFactory, IngestionResult, run_ingestion
from app.services.sources.registry import ProviderRegistry
from app.services.storage import ArtifactStorage
from app.services.tasks import (
    claim_next_task,
    complete_task,
    enqueue_task,
    mark_task_failed,
    reschedule_task,
)

logger = logging.getLogger(__name__)

INGEST_TASK_KIND = "source_ingest"
MAX_ATTEMPTS = 5
BASE_BACKOFF_SECONDS = 30
MAX_BACKOFF_SECONDS = 3600
INGEST_LEASE_SECONDS = 300


def backoff_seconds(attempts: int, retry_after_seconds: int | None = None) -> int:
    exponential = min(BASE_BACKOFF_SECONDS * 2 ** max(attempts - 1, 0), MAX_BACKOFF_SECONDS)
    return max(exponential, min(retry_after_seconds or 0, MAX_BACKOFF_SECONDS))


def enqueue_ingestion(
    db: Session,
    *,
    connector_id: str,
    idempotency_key: str,
    requested_by_user_id: str | None,
) -> Task:
    return enqueue_task(
        db,
        kind=INGEST_TASK_KIND,
        idempotency_key=f"{INGEST_TASK_KIND}:{connector_id}:{idempotency_key}",
        payload={"connector_id": connector_id, "requested_by_user_id": requested_by_user_id},
    )


def process_ingestion_task(
    db: Session,
    storage: ArtifactStorage,
    task: Task,
    *,
    worker_id: str,
    registry: ProviderRegistry | None = None,
    now: datetime | None = None,
    network_access_enabled: bool | None = None,
    http_client_factory: HttpClientFactory | None = None,
) -> IngestionResult | None:
    """Run one claimed ingestion task. The claim must already be committed."""
    payload = task.payload_json or {}
    try:
        result = run_ingestion(
            db,
            storage,
            connector_id=str(payload.get("connector_id", "")),
            registry=registry,
            idempotency_key=task.idempotency_key,
            requested_by_user_id=payload.get("requested_by_user_id"),
            actor=f"worker:{worker_id}"[:64],
            now=now,
            network_access_enabled=network_access_enabled,
            http_client_factory=http_client_factory,
        )
    except SourceError as exc:
        if exc.retryable:
            reschedule_task(
                db,
                task,
                worker_id=worker_id,
                delay_seconds=backoff_seconds(task.attempts, exc.retry_after_seconds),
                max_attempts=MAX_ATTEMPTS,
            )
        else:
            # Policy/authorization failures and invalid responses need a human.
            mark_task_failed(db, task)
        db.commit()
        return None
    except Exception as exc:
        logger.error("Ingestion task failed", extra={"error_type": type(exc).__name__})
        db.rollback()
        reschedule_task(
            db,
            task,
            worker_id=worker_id,
            delay_seconds=backoff_seconds(task.attempts),
            max_attempts=MAX_ATTEMPTS,
        )
        db.commit()
        return None
    complete_task(db, task, worker_id=worker_id)
    db.commit()
    return result


def run_next_ingestion_task(
    db: Session,
    storage: ArtifactStorage,
    *,
    worker_id: str,
    registry: ProviderRegistry | None = None,
    http_client_factory: HttpClientFactory | None = None,
) -> Task | None:
    task = claim_next_task(
        db, worker_id=worker_id, lease_seconds=INGEST_LEASE_SECONDS, kinds=[INGEST_TASK_KIND]
    )
    if task is None:
        db.rollback()
        return None
    db.commit()
    process_ingestion_task(
        db,
        storage,
        task,
        worker_id=worker_id,
        registry=registry,
        http_client_factory=http_client_factory,
    )
    return task
