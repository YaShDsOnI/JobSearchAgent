"""Connector health reporting for dashboards, the admin CLI, and alerting hooks."""

from collections import Counter
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import SourceConnector, SourceRun, now_utc
from app.services.sources.policy import as_aware

# Internal health_status -> reported state.
STATE_BY_HEALTH = {
    "healthy": "success",
    "failing": "failure",
    "degraded": "degraded",
    "blocked": "blocked",
    "suspended": "suspended",
    "unknown": "unknown",
}
ALERT_STATES = frozenset({"failure", "blocked", "suspended"})


def connector_health_report(
    db: Session, connectors: Sequence[SourceConnector], *, now: datetime | None = None
) -> list[dict[str, Any]]:
    now = now or now_utc()
    since = now - timedelta(hours=24)
    ids = [connector.id for connector in connectors]
    counts: dict[str, Counter[str]] = {connector_id: Counter() for connector_id in ids}
    for connector_id, status, started_at in db.execute(
        select(SourceRun.connector_id, SourceRun.status, SourceRun.started_at).where(
            SourceRun.connector_id.in_(ids)
        )
    ):
        if as_aware(started_at) >= since:
            counts[connector_id][status] += 1

    report: list[dict[str, Any]] = []
    for connector in connectors:
        state = STATE_BY_HEALTH.get(connector.health_status or "unknown", "unknown")
        interval = connector.schedule_interval_seconds
        overdue = False
        if interval and connector.enabled:
            reference = connector.last_success_at or connector.created_at
            overdue = reference is not None and now - as_aware(reference) > timedelta(
                seconds=2 * interval
            )
        report.append(
            {
                "connector_id": connector.id,
                "name": connector.name,
                "scope": "private" if connector.owner_user_id else "shared",
                "enabled": connector.enabled,
                "state": state,
                "health_status": connector.health_status or "unknown",
                "alert": state in ALERT_STATES or overdue,
                "overdue": overdue,
                "consecutive_failures": connector.consecutive_failures or 0,
                "last_error_code": connector.last_error_code,
                "last_run_at": connector.last_run_at,
                "last_success_at": connector.last_success_at,
                "last_failure_at": connector.last_failure_at,
                "schedule_interval_seconds": interval,
                "next_run_due_at": connector.next_run_due_at,
                "runs_24h": dict(counts[connector.id]),
            }
        )
    return report
