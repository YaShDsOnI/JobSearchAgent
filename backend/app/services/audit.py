from typing import Any

from sqlalchemy.orm import Session

from app.core.redaction import redact_value
from app.db.models import AuditEvent


def record_audit(
    db: Session,
    *,
    action: str,
    actor: str,
    user_id: str | None = None,
    object_type: str | None = None,
    object_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> AuditEvent:
    event = AuditEvent(
        user_id=user_id,
        actor=actor,
        action=action,
        object_type=object_type,
        object_id=object_id,
        metadata_json=redact_value(metadata or {}),
        request_id=request_id,
    )
    db.add(event)
    db.flush()
    return event
