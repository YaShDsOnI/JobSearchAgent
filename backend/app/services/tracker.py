"""Application tracker: where the candidate has applied (Phase 6).

Records what the candidate did; it never sends, uploads, or fetches anything. Design:
docs/application-agent.md, "Application tracker". Company, role, link, and notes are
personal data: they are exported and deleted with the profile and never put in audit
metadata (only IDs, channel, stage, and changed field names are audited).
"""

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db.models import (
    SUBMISSION_LIVE_STATES,
    TRACKER_STAGES,
    Application,
    ApplicationPackage,
    ApplicationSubmission,
    TrackedApplication,
)
from app.services.applications import (
    ApplicationError,
    current_package,
    owned_application,
    record_application_event,
)
from app.services.audit import record_audit

MAX_TEXT = 255
MAX_URL = 2048
MAX_NOTES = 2000
EARLIEST_DATE = date(2000, 1, 1)
# Dates are calendar dates in the candidate's time zone; allow up to UTC+14.
FUTURE_TOLERANCE = timedelta(days=1)
EXTERNAL_ONLY_FIELDS = frozenset({"company", "role_title", "location", "job_url"})
UPDATABLE_FIELDS = EXTERNAL_ONLY_FIELDS | {"stage", "notes", "applied_on"}

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_NOTES_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")  # Allows \t \n \r.
_SPACES = re.compile(r"\s+")


class TrackerError(ValueError):
    def __init__(
        self, code: str, message: str, *, status_code: int = 422, existing_id: str | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.existing_id = existing_id


# --- validation ---------------------------------------------------------------------------


def _today() -> date:
    return datetime.now(UTC).date()


def clean_text(value: str | None, field: str, *, required: bool) -> str | None:
    text = _SPACES.sub(" ", value or "").strip()
    if not text:
        if required:
            raise TrackerError("invalid_field", f"{field} is required")
        return None
    if len(text) > MAX_TEXT or _CONTROL.search(text):
        raise TrackerError("invalid_field", f"{field} is too long or malformed")
    return text


def clean_url(value: str | None) -> str | None:
    url = (value or "").strip()
    if not url:
        return None
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise TrackerError("invalid_field", "job_url is not a valid link") from exc
    if (
        len(url) > MAX_URL
        or parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or _CONTROL.search(url)
        or any(ch.isspace() for ch in url)
    ):
        raise TrackerError("invalid_field", "job_url must be an http(s) link without credentials")
    return url


def clean_date(value: date | None) -> date:
    applied_on = value or _today()
    if applied_on < EARLIEST_DATE or applied_on > _today() + FUTURE_TOLERANCE:
        raise TrackerError("invalid_field", "applied_on must be a past or current date")
    return applied_on


def clean_notes(value: str | None) -> str | None:
    notes = (value or "").strip()
    if not notes:
        return None
    if len(notes) > MAX_NOTES or _NOTES_CONTROL.search(notes):
        raise TrackerError("invalid_field", "notes are too long or malformed")
    return notes


def clean_stage(value: str | None) -> str:
    stage = value or "applied"
    if stage not in TRACKER_STAGES:
        raise TrackerError("invalid_field", "stage is not a known stage")
    return stage


def _key(text: str) -> str:
    return _SPACES.sub(" ", text).strip().casefold()


def _url_key(url: str | None) -> str | None:
    if not url:
        return None
    parts = urlsplit(url)
    return f"{(parts.hostname or '').lower()}{parts.path.rstrip('/')}?{parts.query}"


# --- queries -------------------------------------------------------------------------------


def owned_entry(db: Session, entry_id: str, user_id: str) -> TrackedApplication:
    entry = db.scalar(
        select(TrackedApplication).where(
            TrackedApplication.id == entry_id, TrackedApplication.user_id == user_id
        )
    )
    if entry is None:
        raise TrackerError("not_found", "Tracker entry not found", status_code=404)
    return entry


def list_entries(
    db: Session, user_id: str, *, stage: str | None = None, query: str | None = None
) -> list[TrackedApplication]:
    statement = select(TrackedApplication).where(TrackedApplication.user_id == user_id)
    if stage is not None:
        statement = statement.where(TrackedApplication.stage == clean_stage(stage))
    entries = list(
        db.scalars(
            statement.order_by(
                TrackedApplication.applied_on.desc(), TrackedApplication.created_at.desc()
            )
        ).all()
    )
    needle = _key(query or "")
    if needle:
        entries = [
            entry
            for entry in entries
            if needle in _key(entry.company) or needle in _key(entry.role_title)
        ]
    return entries


@dataclass(frozen=True)
class TrackerSummary:
    total: int
    companies: int
    by_stage: dict[str, int]


def summarize(entries: list[TrackedApplication]) -> TrackerSummary:
    by_stage = {stage: 0 for stage in TRACKER_STAGES}
    for entry in entries:
        by_stage[entry.stage] = by_stage.get(entry.stage, 0) + 1
    return TrackerSummary(
        total=len(entries),
        companies=len({_key(entry.company) for entry in entries}),
        by_stage=by_stage,
    )


def _find_duplicate(
    db: Session, user_id: str, company: str, role_title: str, job_url: str | None
) -> TrackedApplication | None:
    url_key = _url_key(job_url)
    for entry in db.scalars(
        select(TrackedApplication).where(TrackedApplication.user_id == user_id)
    ).all():
        same_role = _key(entry.company) == _key(company) and _key(entry.role_title) == _key(
            role_title
        )
        same_link = url_key is not None and _url_key(entry.job_url) == url_key
        if same_role or same_link:
            return entry
    return None


def _audit(
    db: Session,
    entry: TrackedApplication,
    action: str,
    *,
    request_id: str | None,
    fields: list[str] | None = None,
) -> None:
    metadata: dict[str, Any] = {
        "entry_id": entry.id,
        "channel": entry.channel,
        "stage": entry.stage,
        "application_id": entry.application_id,
    }
    if fields is not None:
        metadata["fields"] = sorted(fields)
    record_audit(
        db,
        action=action,
        actor="user",
        user_id=entry.user_id,
        object_type="tracked_application",
        object_id=entry.id,
        metadata=metadata,
        request_id=request_id,
    )


# --- operations ---------------------------------------------------------------------------


def create_external(
    db: Session,
    *,
    user_id: str,
    company: str,
    role_title: str,
    location: str | None = None,
    job_url: str | None = None,
    applied_on: date | None = None,
    stage: str | None = None,
    notes: str | None = None,
    allow_duplicate: bool = False,
    request_id: str | None = None,
) -> TrackedApplication:
    """A manual entry for an application made outside JobFinderAgent."""
    company_text = clean_text(company, "company", required=True)
    role_text = clean_text(role_title, "role_title", required=True)
    assert company_text is not None and role_text is not None
    url = clean_url(job_url)
    entry = TrackedApplication(
        user_id=user_id,
        channel="external",
        company=company_text,
        role_title=role_text,
        location=clean_text(location, "location", required=False),
        job_url=url,
        applied_on=clean_date(applied_on),
        stage=clean_stage(stage),
        notes=clean_notes(notes),
    )
    if not allow_duplicate:
        duplicate = _find_duplicate(db, user_id, company_text, role_text, url)
        if duplicate is not None:
            raise TrackerError(
                "possible_duplicate",
                "You already tracked an application for this company and role (or link)",
                status_code=409,
                existing_id=duplicate.id,
            )
    db.add(entry)
    db.flush()
    _audit(db, entry, "tracker.created", request_id=request_id)
    return entry


def mark_applied(
    db: Session,
    *,
    user_id: str,
    application_id: str,
    applied_on: date | None = None,
    notes: str | None = None,
    request_id: str | None = None,
) -> TrackedApplication:
    """The candidate applied on the employer's site after reviewing the package."""
    try:
        application: Application = owned_application(
            db, application_id, user_id, for_update=True
        )
    except ApplicationError as exc:
        raise TrackerError(exc.code, str(exc), status_code=exc.status_code) from exc
    existing = db.scalar(
        select(TrackedApplication).where(TrackedApplication.application_id == application.id)
    )
    if existing is not None:
        raise TrackerError(
            "already_applied",
            "This application is already marked as applied",
            status_code=409,
            existing_id=existing.id,
        )
    in_flight = db.scalar(
        select(ApplicationSubmission.id).where(
            ApplicationSubmission.application_id == application.id,
            ApplicationSubmission.state.in_(SUBMISSION_LIVE_STATES),
        )
    )
    if in_flight is not None:
        raise TrackerError(
            "submission_in_progress",
            "A sandbox submission for this job is in progress or finished; it is tracked "
            "automatically",
            status_code=409,
        )
    if application.status == "skipped":
        raise TrackerError(
            "application_skipped",
            "This job was skipped; prepare it again before marking it applied",
            status_code=409,
        )
    package = current_package(db, application)
    if package is None:
        raise TrackerError("package_missing", "No package has been built", status_code=409)
    content = package.package_json or {}
    job = content.get("job") or {}
    destination = content.get("destination") or {}
    entry = TrackedApplication(
        user_id=user_id,
        application_id=application.id,
        package_id=package.id,
        review_hash=package.review_hash,
        channel="link_out",
        company=clean_text(str(job.get("employer") or ""), "company", required=False)
        or "Employer not listed",
        role_title=clean_text(str(job.get("title") or ""), "role_title", required=False)
        or "Role not listed",
        location=clean_text(job.get("location_text"), "location", required=False),
        job_url=_safe_url(destination.get("url")),
        applied_on=clean_date(applied_on),
        stage="applied",
        notes=clean_notes(notes),
    )
    db.add(entry)
    db.flush()
    record_application_event(
        db,
        application,
        "marked_applied",
        actor="user",
        metadata={
            "tracker_entry_id": entry.id,
            "package_id": package.id,
            "revision": package.revision,
            "review_hash": package.review_hash,
        },
        request_id=request_id,
    )
    _audit(db, entry, "tracker.created", request_id=request_id)
    return entry


def record_adapter_submission(
    db: Session,
    *,
    application: Application,
    package: ApplicationPackage,
    destination_url: str,
) -> TrackedApplication:
    """A sandbox adapter submission was verified: record it (freezes the package)."""
    content = package.package_json or {}
    job = content.get("job") or {}
    entry = TrackedApplication(
        user_id=application.user_id,
        application_id=application.id,
        package_id=package.id,
        review_hash=package.review_hash,
        channel="sandbox_adapter",
        company=clean_text(str(job.get("employer") or ""), "company", required=False)
        or "Employer not listed",
        role_title=clean_text(str(job.get("title") or ""), "role_title", required=False)
        or "Role not listed",
        location=clean_text(job.get("location_text"), "location", required=False),
        job_url=destination_url[:MAX_URL],
        applied_on=_today(),
        stage="applied",
    )
    db.add(entry)
    db.flush()
    _audit(db, entry, "tracker.created", request_id=None)
    return entry


def _safe_url(value: Any) -> str | None:
    try:
        return clean_url(str(value)) if value else None
    except TrackerError:
        return None


def update_entry(
    db: Session,
    *,
    user_id: str,
    entry_id: str,
    changes: dict[str, Any],
    request_id: str | None = None,
) -> TrackedApplication:
    entry = owned_entry(db, entry_id, user_id)
    unknown = set(changes) - UPDATABLE_FIELDS
    if unknown:
        raise TrackerError("invalid_field", f"Cannot change {', '.join(sorted(unknown))}")
    if entry.channel != "external" and set(changes) & EXTERNAL_ONLY_FIELDS:
        raise TrackerError(
            "field_locked",
            "Company, role, location, and link mirror the reviewed package for this entry",
            status_code=409,
        )
    changed: list[str] = []
    for field, value in changes.items():
        if field == "company":
            new: Any = clean_text(value, "company", required=True)
        elif field == "role_title":
            new = clean_text(value, "role_title", required=True)
        elif field == "location":
            new = clean_text(value, "location", required=False)
        elif field == "job_url":
            new = clean_url(value)
        elif field == "applied_on":
            new = clean_date(value)
        elif field == "stage":
            new = clean_stage(value)
        else:
            new = clean_notes(value)
        if getattr(entry, field) != new:
            setattr(entry, field, new)
            changed.append(field)
    if changed:
        db.flush()
        _audit(db, entry, "tracker.updated", request_id=request_id, fields=changed)
    return entry


def delete_entry(
    db: Session, *, user_id: str, entry_id: str, request_id: str | None = None
) -> None:
    entry = owned_entry(db, entry_id, user_id)
    if entry.application_id is not None:
        application = db.get(Application, entry.application_id)
        if application is not None:
            record_application_event(
                db,
                application,
                "unmarked_applied",
                actor="user",
                metadata={"tracker_entry_id": entry.id},
                request_id=request_id,
            )
    _audit(db, entry, "tracker.deleted", request_id=request_id)
    db.delete(entry)
    db.flush()


def delete_all_entries(db: Session, user_id: str) -> int:
    entries = db.scalars(
        select(TrackedApplication.id).where(TrackedApplication.user_id == user_id)
    ).all()
    db.execute(delete(TrackedApplication).where(TrackedApplication.user_id == user_id))
    return len(entries)


def entry_dict(entry: TrackedApplication) -> dict[str, Any]:
    return {
        "id": entry.id,
        "application_id": entry.application_id,
        "package_id": entry.package_id,
        "review_hash": entry.review_hash,
        "channel": entry.channel,
        "company": entry.company,
        "role_title": entry.role_title,
        "location": entry.location,
        "job_url": entry.job_url,
        "applied_on": entry.applied_on.isoformat(),
        "stage": entry.stage,
        "notes": entry.notes,
        "created_at": entry.created_at.isoformat() if entry.created_at else None,
        "updated_at": entry.updated_at.isoformat() if entry.updated_at else None,
    }


def export_entries(db: Session, user_id: str) -> list[dict[str, Any]]:
    return [
        entry_dict(entry)
        for entry in db.scalars(
            select(TrackedApplication)
            .where(TrackedApplication.user_id == user_id)
            .order_by(TrackedApplication.created_at.asc(), TrackedApplication.id.asc())
        ).all()
    ]
