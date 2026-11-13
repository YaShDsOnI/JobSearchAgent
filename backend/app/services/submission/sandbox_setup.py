"""Register the local sandbox ATS: its job source and its (initially disabled) destination."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import SourceConnector
from app.services.catalog import FIXTURE_AUTHORIZATION
from app.services.sources.pipeline import run_ingestion
from app.services.storage import ArtifactStorage
from app.services.submission.destinations import (
    SANDBOX_CONNECTOR_KIND,
    SANDBOX_CONNECTOR_NAME,
    ensure_sandbox_destination,
)


def ensure_sandbox_connector(db: Session) -> SourceConnector:
    connector = db.scalar(
        select(SourceConnector).where(SourceConnector.name == SANDBOX_CONNECTOR_NAME)
    )
    if connector is not None:
        return connector  # Never silently re-authorize an existing connector.
    connector = SourceConnector(
        name=SANDBOX_CONNECTOR_NAME,
        kind=SANDBOX_CONNECTOR_KIND,
        tier="local",
        base_domain=None,
        terms_version="fixture-v1",
        policy_status="approved",
        enabled=True,
        metadata_json={"network_access": False,
                       "purpose": "Phase 6 sandbox ATS jobs; not real employers"},
        authorization_json={
            **FIXTURE_AUTHORIZATION,
            "evidence_ref": "docs/application-agent.md#phase-6-sandbox-browser-adapter",
        },
        config_json={"allowed_url_hosts": ["sandbox-ats.invalid"]},
    )
    db.add(connector)
    db.flush()
    return connector


def register_sandbox(
    db: Session, storage: ArtifactStorage, settings: Settings, *, actor: str
) -> dict[str, Any]:
    connector = ensure_sandbox_connector(db)
    destination = ensure_sandbox_destination(db, settings, actor=actor)
    db.commit()
    result = run_ingestion(db, storage, connector_id=connector.id, actor=actor)
    db.commit()
    return {
        "connector": connector.name,
        "destination": destination.name,
        "destination_enabled": destination.enabled,
        "origin": destination.origin,
        "ingestion": result.status,
        "jobs_created": result.stats.get("created", 0),
    }
