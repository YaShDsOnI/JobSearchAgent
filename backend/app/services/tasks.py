from collections.abc import Collection
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import Task


def enqueue_task(db: Session, *, kind: str, idempotency_key: str, payload: dict[str, Any]) -> Task:
    existing = db.scalar(select(Task).where(Task.idempotency_key == idempotency_key))
    if existing:
        return existing
    task = Task(kind=kind, idempotency_key=idempotency_key, payload_json=payload)
    try:
        # Keep the unique-key race inside a savepoint so a concurrent duplicate
        # does not poison the caller's outer transaction.
        with db.begin_nested():
            db.add(task)
            db.flush()
    except IntegrityError:
        existing = db.scalar(select(Task).where(Task.idempotency_key == idempotency_key))
        if existing is None:
            raise
        return existing
    return task


def mark_task_failed(db: Session, task: Task) -> None:
    task.state = "failed_manual"
    task.lease_owner = None
    task.lease_expires_at = None
    task.updated_at = datetime.now(UTC)
    db.flush()


def claim_next_task(
    db: Session,
    *,
    worker_id: str,
    lease_seconds: int = 60,
    kinds: Collection[str] | None = None,
) -> Task | None:
    """Claim one queued or expired task without losing it on worker failure."""
    if not worker_id or lease_seconds <= 0:
        raise ValueError("worker_id and a positive lease are required")
    now = datetime.now(UTC)
    conditions = [
        or_(
            (Task.state == "queued") & (Task.next_run_at <= now),
            (Task.state == "running") & (Task.lease_expires_at <= now),
        )
    ]
    if kinds is not None:
        conditions.append(Task.kind.in_(list(kinds)))
    task = db.scalar(
        select(Task)
        .where(*conditions)
        .order_by(Task.next_run_at, Task.created_at)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if task is None:
        return None
    task.state = "running"
    task.lease_owner = worker_id
    task.lease_expires_at = now + timedelta(seconds=lease_seconds)
    task.attempts += 1
    task.updated_at = now
    db.flush()
    return task


def heartbeat_task(
    db: Session, task: Task, *, worker_id: str, lease_seconds: int = 60
) -> None:
    if task.state != "running" or task.lease_owner != worker_id or lease_seconds <= 0:
        raise ValueError("Task is not leased by this worker")
    task.lease_expires_at = datetime.now(UTC) + timedelta(seconds=lease_seconds)
    task.updated_at = datetime.now(UTC)
    db.flush()


def complete_task(db: Session, task: Task, *, worker_id: str) -> None:
    if task.state != "running" or task.lease_owner != worker_id:
        raise ValueError("Task is not leased by this worker")
    task.state = "succeeded"
    task.lease_owner = None
    task.lease_expires_at = None
    task.updated_at = datetime.now(UTC)
    db.flush()


def reschedule_task(
    db: Session,
    task: Task,
    *,
    worker_id: str,
    delay_seconds: int,
    max_attempts: int = 3,
) -> None:
    if task.state != "running" or task.lease_owner != worker_id:
        raise ValueError("Task is not leased by this worker")
    if delay_seconds < 0 or max_attempts <= 0:
        raise ValueError("Invalid retry policy")
    task.state = "failed_manual" if task.attempts >= max_attempts else "queued"
    task.lease_owner = None
    task.lease_expires_at = None
    task.next_run_at = datetime.now(UTC) + timedelta(seconds=delay_seconds)
    task.updated_at = datetime.now(UTC)
    db.flush()
