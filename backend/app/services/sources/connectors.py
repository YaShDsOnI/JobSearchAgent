"""Operator-side connector registration and kill switch (CLI only; no HTTP endpoint)."""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import SourceConnector, User
from app.services.audit import record_audit
from app.services.sources.ashby import ASHBY_API_HOST, ASHBY_DESCRIPTOR, ashby_connector_config
from app.services.sources.policy import ALLOWED_BASES


class ConnectorRegistrationError(ValueError):
    pass


def register_ashby_connector(
    db: Session,
    *,
    name: str,
    board_name: str,
    employer_name: str,
    basis: str,
    evidence_ref: str,
    reviewed_by: str,
    terms_version: str,
    actor: str,
    include_compensation: bool = True,
    owner_email: str | None = None,
    approve: bool = False,
) -> SourceConnector:
    """Create an Ashby connector. It starts disabled; enabling is a separate audited step."""
    if basis not in ALLOWED_BASES or basis not in ASHBY_DESCRIPTOR.allowed_bases:
        raise ConnectorRegistrationError(f"Basis not permitted for Ashby: {basis}")
    for label, value in (
        ("evidence_ref", evidence_ref),
        ("reviewed_by", reviewed_by),
        ("terms_version", terms_version),
        ("name", name),
    ):
        if not value.strip() or len(value) > 255:
            raise ConnectorRegistrationError(f"{label} is required")
    if db.scalar(select(SourceConnector).where(SourceConnector.name == name)):
        raise ConnectorRegistrationError("A connector with that name already exists")
    owner_id = None
    if owner_email:
        owner = db.scalar(select(User).where(User.email == owner_email.strip().lower()))
        if owner is None:
            raise ConnectorRegistrationError("Owner account not found")
        owner_id = owner.id
    try:
        config = ashby_connector_config(
            board_name, employer_name, include_compensation=include_compensation
        )
    except ValueError as exc:
        raise ConnectorRegistrationError(str(exc)) from exc
    connector = SourceConnector(
        name=name.strip(),
        kind=ASHBY_DESCRIPTOR.kind,
        tier="A",
        base_domain=ASHBY_API_HOST,
        terms_version=terms_version.strip(),
        policy_status="approved" if approve else "review_required",
        enabled=False,
        owner_user_id=owner_id,
        authorization_json={
            "basis": basis,
            "evidence_ref": evidence_ref.strip(),
            "reviewed_by": reviewed_by.strip(),
            "reviewed_at": datetime.now(UTC).isoformat(),
            "terms_version": terms_version.strip(),
        },
        config_json=config,
        metadata_json={"provider": "ashby", "network_access": True, "read_only": True},
    )
    db.add(connector)
    db.flush()
    record_audit(
        db,
        action="source.connector_registered",
        actor=actor,
        user_id=owner_id,
        object_type="source_connector",
        object_id=connector.id,
        metadata={
            "kind": connector.kind,
            "board_name": board_name,
            "basis": basis,
            "policy_status": connector.policy_status,
            "reviewed_by": reviewed_by.strip(),
        },
    )
    return connector


def set_connector_enabled(
    db: Session, connector: SourceConnector, *, enabled: bool, actor: str, reason: str
) -> SourceConnector:
    if not reason.strip():
        raise ConnectorRegistrationError("A reason is required")
    previous = connector.enabled
    connector.enabled = enabled
    record_audit(
        db,
        action="source.connector_enabled" if enabled else "source.connector_disabled",
        actor=actor,
        user_id=connector.owner_user_id,
        object_type="source_connector",
        object_id=connector.id,
        metadata={"previous": previous, "reason": reason.strip()[:200]},
    )
    db.flush()
    return connector
