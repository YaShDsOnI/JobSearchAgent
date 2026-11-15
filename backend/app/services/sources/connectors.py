"""Operator-side connector registration and kill switch (CLI only; no HTTP endpoint)."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import SourceConnector, User
from app.services.audit import record_audit
from app.services.sources.ashby import ASHBY_API_HOST, ASHBY_DESCRIPTOR, ashby_connector_config
from app.services.sources.base import ProviderDescriptor
from app.services.sources.greenhouse import (
    GREENHOUSE_API_HOST,
    GREENHOUSE_DESCRIPTOR,
    greenhouse_connector_config,
)
from app.services.sources.lever import LEVER_API_HOSTS, LEVER_DESCRIPTOR, lever_connector_config
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
    try:
        config = ashby_connector_config(
            board_name, employer_name, include_compensation=include_compensation
        )
    except ValueError as exc:
        raise ConnectorRegistrationError(str(exc)) from exc
    return _register_board_connector(
        db,
        descriptor=ASHBY_DESCRIPTOR,
        provider="ashby",
        base_domain=ASHBY_API_HOST,
        board_label=board_name,
        config=config,
        name=name,
        basis=basis,
        evidence_ref=evidence_ref,
        reviewed_by=reviewed_by,
        terms_version=terms_version,
        actor=actor,
        owner_email=owner_email,
        approve=approve,
    )


def register_greenhouse_connector(
    db: Session,
    *,
    name: str,
    board_token: str,
    employer_name: str,
    basis: str,
    evidence_ref: str,
    reviewed_by: str,
    terms_version: str,
    actor: str,
    careers_host: str | None = None,
    owner_email: str | None = None,
    approve: bool = False,
) -> SourceConnector:
    """Create a Greenhouse Job Board connector (read-only listing). It starts disabled."""
    try:
        config = greenhouse_connector_config(
            board_token, employer_name, careers_host=careers_host
        )
    except ValueError as exc:
        raise ConnectorRegistrationError(str(exc)) from exc
    return _register_board_connector(
        db,
        descriptor=GREENHOUSE_DESCRIPTOR,
        provider="greenhouse",
        base_domain=GREENHOUSE_API_HOST,
        board_label=board_token,
        config=config,
        name=name,
        basis=basis,
        evidence_ref=evidence_ref,
        reviewed_by=reviewed_by,
        terms_version=terms_version,
        actor=actor,
        owner_email=owner_email,
        approve=approve,
    )


def register_lever_connector(
    db: Session,
    *,
    name: str,
    site: str,
    employer_name: str,
    basis: str,
    evidence_ref: str,
    reviewed_by: str,
    terms_version: str,
    actor: str,
    region: str = "global",
    owner_email: str | None = None,
    approve: bool = False,
) -> SourceConnector:
    """Create a Lever Postings connector (read-only listing). It starts disabled."""
    try:
        config = lever_connector_config(site, employer_name, region=region)
    except ValueError as exc:
        raise ConnectorRegistrationError(str(exc)) from exc
    return _register_board_connector(
        db,
        descriptor=LEVER_DESCRIPTOR,
        provider="lever",
        base_domain=LEVER_API_HOSTS[region],
        board_label=site,
        config=config,
        name=name,
        basis=basis,
        evidence_ref=evidence_ref,
        reviewed_by=reviewed_by,
        terms_version=terms_version,
        actor=actor,
        owner_email=owner_email,
        approve=approve,
    )


def _register_board_connector(
    db: Session,
    *,
    descriptor: ProviderDescriptor,
    provider: str,
    base_domain: str,
    board_label: str,
    config: dict[str, Any],
    name: str,
    basis: str,
    evidence_ref: str,
    reviewed_by: str,
    terms_version: str,
    actor: str,
    owner_email: str | None,
    approve: bool,
) -> SourceConnector:
    """Shared registration for public, read-only ATS job boards. Every board gets its own
    authorization record; the connector starts disabled."""
    if basis not in ALLOWED_BASES or basis not in descriptor.allowed_bases:
        raise ConnectorRegistrationError(f"Basis not permitted for {provider}: {basis}")
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
    connector = SourceConnector(
        name=name.strip(),
        kind=descriptor.kind,
        tier="A",
        base_domain=base_domain,
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
        metadata_json={"provider": provider, "network_access": True, "read_only": True},
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
            "board_name": board_label,
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
