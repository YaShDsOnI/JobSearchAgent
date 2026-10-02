"""Job catalog helpers and the fixture-ingestion compatibility entry point.

Ingestion itself lives in `app.services.sources.pipeline`; this module keeps the Phase 1-3
call sites working and centralizes tenant visibility and snapshot ordering rules.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db.models import JobPosting, JobSnapshot, SourceConnector
from app.services.sources.base import ProviderDescriptor
from app.services.sources.errors import SourcePolicyError
from app.services.sources.fixture import FIXTURE_DESCRIPTOR, StaticFixtureProvider
from app.services.sources.normalize import stable_hash
from app.services.sources.pipeline import IngestionResult, run_ingestion
from app.services.sources.policy import authorize_connector
from app.services.sources.registry import ProviderRegistry
from app.services.storage import ArtifactStorage

# Backwards-compatible name used by Phase 1 callers and tests.
ConnectorPolicyError = SourcePolicyError

FIXTURE_CONNECTOR_NAME = "phase1-fixture"
FIXTURE_AUTHORIZATION = {
    "basis": "local_fixture",
    "evidence_ref": "docs/source-connectors.md#fixture-bundled-sanitized-fixture-catalog",
    "reviewed_by": "phase4-source-policy",
    "reviewed_at": "2026-09-29T00:00:00+00:00",
    "terms_version": "fixture-v1",
}


def validate_connector_policy(
    connector: SourceConnector, descriptor: ProviderDescriptor = FIXTURE_DESCRIPTOR
) -> None:
    authorize_connector(
        connector, descriptor, now=datetime.now(UTC), network_access_enabled=False
    )


def canonical_hash(value: dict[str, Any]) -> str:
    return stable_hash(value)


def ensure_fixture_connector(db: Session) -> SourceConnector:
    connector = db.scalar(
        select(SourceConnector).where(SourceConnector.name == FIXTURE_CONNECTOR_NAME)
    )
    if connector:
        # Never silently (re)authorize an existing connector; the policy gate decides.
        return connector
    connector = SourceConnector(
        name=FIXTURE_CONNECTOR_NAME,
        kind="fixture",
        tier="local",
        base_domain=None,
        terms_version="fixture-v1",
        policy_status="approved",
        enabled=True,
        metadata_json={"network_access": False, "purpose": "sanitized local test catalog"},
        authorization_json=dict(FIXTURE_AUTHORIZATION),
        config_json={"dataset": "jobs", "allowed_url_hosts": ["example.invalid"]},
    )
    db.add(connector)
    db.flush()
    return connector


def ingest_fixture_records(
    db: Session,
    storage: ArtifactStorage,
    records: list[Any],
    *,
    listing_complete: bool = False,
) -> IngestionResult:
    """Ingest in-memory sanitized records through the full pipeline (commits)."""
    connector = ensure_fixture_connector(db)
    db.commit()
    registry = ProviderRegistry()
    registry.register(
        "fixture",
        lambda: StaticFixtureProvider(records, listing_complete=listing_complete),
    )
    return run_ingestion(db, storage, connector_id=connector.id, registry=registry)


def ingest_fixture_jobs(
    db: Session, storage: ArtifactStorage, records: list[dict[str, Any]]
) -> dict[str, int]:
    result = ingest_fixture_records(db, storage, records)
    return {key: result.count(key) for key in ("created", "updated", "unchanged")}


def visible_to_user(user_id: str):
    """SQL condition: shared-catalog postings plus the user's own private-source postings."""
    return or_(JobPosting.owner_user_id.is_(None), JobPosting.owner_user_id == user_id)


def connector_visible_to_user(user_id: str):
    return or_(SourceConnector.owner_user_id.is_(None), SourceConnector.owner_user_id == user_id)


def get_visible_job(db: Session, job_id: str, user_id: str) -> JobPosting | None:
    return db.scalar(select(JobPosting).where(JobPosting.id == job_id, visible_to_user(user_id)))


def latest_snapshot(db: Session, job_id: str) -> JobSnapshot | None:
    return db.scalar(
        select(JobSnapshot)
        .where(JobSnapshot.job_id == job_id)
        .order_by(JobSnapshot.version.desc(), JobSnapshot.retrieved_at.desc())
    )
