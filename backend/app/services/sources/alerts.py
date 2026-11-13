"""Health alert routing: firing/resolved notifications to one operator-configured webhook.

Runs from the scheduler loop and `python -m app.admin send-health-alerts`. Off unless both
`ALERT_WEBHOOK_REF` and `ALERT_WEBHOOK_HOST` are set. The last notification per connector
is read back from the audit log, so restarts never re-send. Payloads carry operational
state only: no candidate, profile, job, or application data, and no private-source names.
"""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.models import AuditEvent, SourceConnector, now_utc
from app.services.audit import record_audit
from app.services.sources.credentials import SecretValue, resolve_secret_ref
from app.services.sources.egress import EgressGuard, EgressPolicy, Resolver, system_resolver
from app.services.sources.errors import SourcePolicyError
from app.services.sources.health import ALERT_STATES, connector_health_report
from app.services.sources.http_client import WebhookDeliveryError, WebhookSender
from app.services.sources.policy import as_aware

logger = logging.getLogger(__name__)

NOTIFIED_ACTION = "source.alert_notified"
FAILED_ACTION = "source.alert_failed"
REMINDER_INTERVAL = timedelta(hours=6)
ALERT_ACTOR = "alerts"


@dataclass(frozen=True)
class AlertOutcome:
    connector_id: str
    kind: str  # "firing" | "resolved"
    state: str
    delivered: bool
    reason: str | None = None


@dataclass(frozen=True)
class _LastNotice:
    kind: str
    state: str
    at: datetime


def build_webhook_sender(
    settings: Settings,
    *,
    resolver: Resolver = system_resolver,
    transport: Any = None,
) -> WebhookSender:
    host = settings.alert_webhook_host.strip().lower()
    proxy = settings.source_egress_proxy
    policy = EgressPolicy(deployment_allowlist=(host,), connector_hosts=(host,))
    return WebhookSender(
        guard=EgressGuard(policy, resolver, resolve_dns=proxy is None),
        transport=transport,
        proxy=proxy,
    )


def _alert_state(row: dict[str, Any]) -> str | None:
    """The state to alert on, or None when the connector does not need attention."""
    if not row["enabled"] or not row["alert"]:
        return None
    return str(row["state"]) if row["state"] in ALERT_STATES else "overdue"


def _last_notices(db: Session, connector_ids: Sequence[str]) -> dict[str, _LastNotice]:
    last: dict[str, _LastNotice] = {}
    if not connector_ids:
        return last
    events = db.scalars(
        select(AuditEvent)
        .where(
            AuditEvent.action == NOTIFIED_ACTION,
            AuditEvent.object_type == "source_connector",
            AuditEvent.object_id.in_(list(connector_ids)),
        )
        .order_by(AuditEvent.occurred_at.desc())
    )
    for event in events:
        if event.object_id and event.object_id not in last:
            metadata = event.metadata_json or {}
            last[event.object_id] = _LastNotice(
                kind=str(metadata.get("kind", "")),
                state=str(metadata.get("state", "")),
                at=as_aware(event.occurred_at),
            )
    return last


def _payload(
    connector: SourceConnector, row: dict[str, Any], kind: str, state: str, now: datetime
) -> dict[str, Any]:
    shared = connector.owner_user_id is None
    return {
        "source": "jobfinderagent",
        "kind": kind,
        "connector_id": connector.id,
        # A private source's name was typed by its owner; it is withheld.
        "connector_name": connector.name if shared else None,
        "scope": "shared" if shared else "private",
        "state": state,
        "overdue": bool(row["overdue"]),
        "consecutive_failures": int(row["consecutive_failures"]),
        "last_error_code": row["last_error_code"],
        "at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def plan_notifications(
    db: Session, *, now: datetime
) -> list[tuple[SourceConnector, dict[str, Any], str, str]]:
    """(connector, health row, kind, state) for every notification that is due."""
    connectors = list(db.scalars(select(SourceConnector).order_by(SourceConnector.name)))
    report = {row["connector_id"]: row for row in connector_health_report(db, connectors, now=now)}
    last = _last_notices(db, [connector.id for connector in connectors])
    due: list[tuple[SourceConnector, dict[str, Any], str, str]] = []
    for connector in connectors:
        row = report[connector.id]
        state = _alert_state(row)
        previous = last.get(connector.id)
        if state is not None:
            if (
                previous is None
                or previous.kind != "firing"
                or previous.state != state
                or now - previous.at >= REMINDER_INTERVAL
            ):
                due.append((connector, row, "firing", state))
        elif previous is not None and previous.kind == "firing":
            due.append((connector, row, "resolved", str(row["state"])))
    return due


def send_health_alerts(
    db: Session,
    settings: Settings | None = None,
    *,
    now: datetime | None = None,
    sender_factory: Callable[[Settings], WebhookSender] = build_webhook_sender,
) -> list[AlertOutcome]:
    """Send every due notification and commit the audit trail. No-op when not configured."""
    settings = settings or get_settings()
    if not settings.alerting_enabled:
        return []
    now = now or now_utc()
    due = plan_notifications(db, now=now)
    if not due:
        return []
    url: SecretValue | None = None
    unavailable_reason = "webhook_unavailable"
    try:
        url = resolve_secret_ref(settings.alert_webhook_ref, settings)
    except SourcePolicyError as exc:
        unavailable_reason = f"webhook_unavailable:{exc.code.value}"
    sender = sender_factory(settings)
    outcomes: list[AlertOutcome] = []
    for connector, row, kind, state in due:
        try:
            if url is None:
                raise WebhookDeliveryError(unavailable_reason)
            sender.post_json(url, _payload(connector, row, kind, state, now))
        except WebhookDeliveryError as exc:
            failed = record_audit(
                db,
                action=FAILED_ACTION,
                actor=ALERT_ACTOR,
                user_id=connector.owner_user_id,
                object_type="source_connector",
                object_id=connector.id,
                metadata={"kind": kind, "state": state, "reason": exc.reason},
            )
            failed.occurred_at = now
            logger.warning(
                "Health alert delivery failed for connector %s: %s",
                connector.id,
                exc.reason,
                extra={"error_type": "alert_delivery_failed"},
            )
            outcomes.append(AlertOutcome(connector.id, kind, state, False, exc.reason))
            continue
        notified = record_audit(
            db,
            action=NOTIFIED_ACTION,
            actor=ALERT_ACTOR,
            user_id=connector.owner_user_id,
            object_type="source_connector",
            object_id=connector.id,
            metadata={"kind": kind, "state": state},
        )
        # The evaluation time, so reminder intervals are measured on one clock.
        notified.occurred_at = now
        outcomes.append(AlertOutcome(connector.id, kind, state, True))
    db.commit()
    return outcomes
