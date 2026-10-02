"""Durable scheduler foundation for connector runs.

A scheduler tick only *enqueues* `source_ingest` tasks; workers run them through the
normal pipeline (policy gate, run lock, retries). Overlap is prevented at three layers:

1. per-slot idempotency key: repeated or concurrent ticks for the same slot create one task;
2. a connector with a queued/running ingestion task or an active run is skipped;
3. the pipeline's compare-and-set run lock.

Health is respected: `suspended` (circuit open) and `blocked` connectors are skipped and are
never re-enabled by the scheduler. Only an operator/owner reset or a successful manual run
after the cause is fixed changes that state.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import SourceConnector, SourceRun, Task, now_utc
from app.services.audit import record_audit
from app.services.sources.policy import as_aware
from app.services.sources.worker import INGEST_TASK_KIND, enqueue_ingestion

MIN_INTERVAL_SECONDS = 15 * 60
MAX_INTERVAL_SECONDS = 7 * 24 * 3600
SKIPPED_HEALTH_STATES = frozenset({"suspended", "blocked"})


class ScheduleError(ValueError):
    pass


@dataclass(frozen=True)
class ScheduleDecision:
    connector_id: str
    action: str  # "enqueued" | "skipped"
    reason: str
    task_id: str | None = None


def slot_start(now: datetime, interval_seconds: int) -> datetime:
    epoch = int(now.timestamp())
    return datetime.fromtimestamp(epoch - epoch % interval_seconds, tz=UTC)


def set_connector_schedule(
    db: Session,
    connector: SourceConnector,
    *,
    interval_seconds: int | None,
    actor: str,
    user_id: str | None,
    now: datetime | None = None,
) -> SourceConnector:
    if interval_seconds is not None and not (
        MIN_INTERVAL_SECONDS <= interval_seconds <= MAX_INTERVAL_SECONDS
    ):
        raise ScheduleError(
            f"Interval must be between {MIN_INTERVAL_SECONDS} and {MAX_INTERVAL_SECONDS} seconds"
        )
    previous = connector.schedule_interval_seconds
    connector.schedule_interval_seconds = interval_seconds
    connector.next_run_due_at = (now or now_utc()) if interval_seconds else None
    record_audit(
        db,
        action="source.schedule_changed",
        actor=actor,
        user_id=user_id,
        object_type="source_connector",
        object_id=connector.id,
        metadata={"from_seconds": previous, "to_seconds": interval_seconds},
    )
    db.flush()
    return connector


def schedule_due_runs(
    db: Session, *, now: datetime | None = None, actor: str = "scheduler"
) -> list[ScheduleDecision]:
    """Enqueue one ingestion task per due connector slot, then commit."""
    now = now or now_utc()
    connectors = db.scalars(
        select(SourceConnector)
        .where(
            SourceConnector.schedule_interval_seconds.is_not(None),
            SourceConnector.enabled.is_(True),
            SourceConnector.policy_status == "approved",
        )
        .order_by(SourceConnector.id)
    ).all()
    pending_connectors = {
        str((task.payload_json or {}).get("connector_id"))
        for task in db.scalars(
            select(Task).where(
                Task.kind == INGEST_TASK_KIND, Task.state.in_(("queued", "running"))
            )
        )
    }
    decisions: list[ScheduleDecision] = []
    for connector in connectors:
        interval = int(connector.schedule_interval_seconds or 0)
        due_at = connector.next_run_due_at
        if interval <= 0 or (due_at is not None and as_aware(due_at) > now):
            continue
        reason = _skip_reason(db, connector, pending_connectors)
        if reason is not None:
            decisions.append(ScheduleDecision(connector.id, "skipped", reason))
            continue
        slot = slot_start(now, interval)
        task = enqueue_ingestion(
            db,
            connector_id=connector.id,
            idempotency_key=f"schedule:{slot.strftime('%Y%m%dT%H%M%SZ')}",
            requested_by_user_id=None,  # System acts on behalf of the owner/operator.
        )
        connector.next_run_due_at = slot + timedelta(seconds=interval)
        record_audit(
            db,
            action="source.run_scheduled",
            actor=actor,
            user_id=connector.owner_user_id,
            object_type="source_connector",
            object_id=connector.id,
            metadata={"task_id": task.id, "slot": slot.isoformat()},
        )
        pending_connectors.add(connector.id)
        decisions.append(ScheduleDecision(connector.id, "enqueued", "due", task.id))
    db.commit()
    return decisions


def _skip_reason(
    db: Session, connector: SourceConnector, pending_connectors: set[str]
) -> str | None:
    if connector.health_status in SKIPPED_HEALTH_STATES:
        return f"health_{connector.health_status}"
    if connector.id in pending_connectors:
        return "run_pending"
    if connector.active_run_id:
        active = db.get(SourceRun, connector.active_run_id)
        if active is not None and active.status == "running":
            return "run_in_progress"
    return None
