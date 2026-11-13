"""Destination policy for the application adapter: the local sandbox ATS only.

A destination is a policy object with its own kill switch (`application_destinations`).
Everything here is pure policy and database work: no network or browser imports.
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, canonical_sandbox_origin
from app.db.models import ApplicationDestination, JobPosting, SourceConnector
from app.services.audit import record_audit

SANDBOX_DESTINATION = "sandbox_ats"
SANDBOX_CONNECTOR_KIND = "sandbox_ats_fixture"
SANDBOX_CONNECTOR_NAME = "sandbox-ats"
ADAPTER_VERSION = "sandbox-ats-adapter-v1"
SANDBOX_AUTHORIZATION = {
    "basis": "sandbox_self_owned",
    "evidence_ref": "docs/roadmap.md#decision-record-phase-6-approval-2026-11-13",
    "reviewed_by": "product-owner",
    "reviewed_at": "2026-11-13T00:00:00+00:00",
    "scope": "local loopback sandbox ATS owned by this project; no real employer",
}


def sandbox_origin(settings: Settings) -> str:
    origin = canonical_sandbox_origin(settings.sandbox_ats_origin)
    if origin is None:  # Unreachable: settings validation refuses non-loopback origins.
        raise RuntimeError("SANDBOX_ATS_ORIGIN is not a loopback origin")
    return origin


def is_sandbox_job(connector: SourceConnector | None) -> bool:
    return connector is not None and connector.kind == SANDBOX_CONNECTOR_KIND


def sandbox_destination(job: JobPosting, settings: Settings) -> dict[str, Any]:
    """The hashed package destination for a sandbox job (never a real employer)."""
    origin = sandbox_origin(settings)
    return {
        "kind": SANDBOX_DESTINATION,
        "url": f"{origin}/jobs/{job.source_job_id}/apply",
        "host": origin.split("://", 1)[1],
        "origin": origin,
        "adapter_version": ADAPTER_VERSION,
    }


def sandbox_notice() -> str:
    return (
        "This job comes from the local sandbox ATS, a test site on this computer that is not "
        "a real employer. If you start a submission and confirm the filled form, "
        "JobFinderAgent's browser runner sends this package, including your CV, to that "
        "sandbox only. Nothing is sent to any real employer."
    )


def get_destination(db: Session, name: str = SANDBOX_DESTINATION) -> ApplicationDestination | None:
    return db.scalar(select(ApplicationDestination).where(ApplicationDestination.name == name))


def ensure_sandbox_destination(
    db: Session, settings: Settings, *, actor: str
) -> ApplicationDestination:
    """Create the sandbox destination row (disabled) if it does not exist."""
    destination = get_destination(db)
    if destination is not None:
        return destination
    destination = ApplicationDestination(
        name=SANDBOX_DESTINATION,
        kind=SANDBOX_DESTINATION,
        origin=sandbox_origin(settings),
        enabled=False,
        policy_status="approved",
        adapter_version=ADAPTER_VERSION,
        authorization_json=dict(SANDBOX_AUTHORIZATION),
    )
    db.add(destination)
    db.flush()
    record_audit(
        db,
        action="destination.registered",
        actor=actor,
        object_type="application_destination",
        object_id=destination.id,
        metadata={"name": destination.name, "enabled": False},
    )
    return destination


def set_destination_enabled(
    db: Session, destination: ApplicationDestination, *, enabled: bool, actor: str, reason: str
) -> None:
    if not reason.strip():
        raise ValueError("A reason is required")
    previous = destination.enabled
    destination.enabled = enabled
    record_audit(
        db,
        action="destination.enabled_changed",
        actor=actor,
        object_type="application_destination",
        object_id=destination.id,
        metadata={"name": destination.name, "from": previous, "to": enabled,
                  "reason": reason.strip()[:200]},
    )


def destination_block_reason(
    db: Session, settings: Settings, destination_payload: dict[str, Any] | None
) -> str | None:
    """Why the adapter may not act now, or None. Reads the kill switch fresh every call."""
    if not settings.application_submission_enabled:
        return "submission_disabled"
    if not destination_payload or destination_payload.get("kind") != SANDBOX_DESTINATION:
        return "destination_not_supported"
    if destination_payload.get("origin") != sandbox_origin(settings):
        return "destination_changed"
    if destination_payload.get("adapter_version") != ADAPTER_VERSION:
        return "destination_changed"
    db.expire_all()  # Never trust a cached kill switch.
    destination = get_destination(db)
    if destination is None or not destination.enabled:
        return "destination_disabled"
    if destination.policy_status != "approved" or destination.origin != sandbox_origin(settings):
        return "destination_not_approved"
    return None
