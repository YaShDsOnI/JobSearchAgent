"""Phase 5C application review package. Preparation only: nothing is ever sent.

A package is an immutable, hashed revision that binds the exact job snapshot, match run,
CV version, profile/preference versions, and source policy that the candidate reviewed.
The state shown to the candidate is derived on every read from a live binding check;
approval is a recorded review decision that nothing in Phase 5C consumes.

This module deliberately has no network, socket, or browser capability (enforced by a
static test). The link-out destination is displayed to the candidate and never fetched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import (
    Application,
    ApplicationEvent,
    ApplicationPackage,
    ApprovalRecord,
    CandidateProfile,
    CVVersion,
    JobPosting,
    JobSnapshot,
    MatchRun,
    PreferenceVersion,
    ProfileFact,
    SourceConnector,
    StoredArtifact,
    TrackedApplication,
    now_utc,
)
from app.services.audit import record_audit
from app.services.catalog import get_visible_job, latest_snapshot, visible_to_user
from app.services.cv_invalidation import changed_material_fields, comparable_job_content
from app.services.sources.normalize import stable_hash

PACKAGE_SCHEMA_VERSION = "review-package-v1"
QUESTION_SET_VERSION = "standard-questions-v1"
DATA_SHARING_NOTICE_VERSION = "data-sharing-notice-v1"
APPROVAL_TTL = timedelta(hours=24)
MAX_ANSWER_LENGTH = 500

AUTHORIZED_STATUSES = frozenset({"authorized", "citizen", "permanent_resident", "work_permit"})
NOT_AUTHORIZED_STATUSES = frozenset({"denied", "not_authorized", "unauthorized"})

_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass(frozen=True)
class Question:
    key: str
    text: str
    kind: str  # text | email | yes_no
    required: bool


QUESTIONS: tuple[Question, ...] = (
    Question("full_name", "Full name", "text", True),
    Question("email", "Email address", "email", True),
    Question("phone", "Phone number", "text", False),
    Question("current_location", "Current location", "text", False),
    Question(
        "work_authorization",
        "Are you legally authorized to work in {country}?",
        "yes_no",
        True,
    ),
    Question(
        "sponsorship_required",
        "Will you now or in the future require visa sponsorship to work in {country}?",
        "yes_no",
        True,
    ),
    Question("notice_period", "Notice period", "text", False),
    Question("relocation", "Are you willing to relocate for this role?", "yes_no", False),
    Question("salary_expectation", "Salary expectation", "text", False),
)
QUESTIONS_BY_KEY = {question.key: question for question in QUESTIONS}


class ApplicationError(ValueError):
    """A refused application operation. `code` is stable; `status_code` is the HTTP hint."""

    def __init__(self, code: str, message: str, *, status_code: int = 409):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


# --- helpers -----------------------------------------------------------------------------


def _aware(value: datetime | None) -> datetime | None:
    # SQLite returns naive datetimes for timezone-aware columns; they are stored as UTC.
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _iso(value: datetime | None) -> str | None:
    aware = _aware(value)
    return aware.astimezone(UTC).isoformat() if aware else None


def package_hash(package: dict[str, Any]) -> str:
    return stable_hash(package)


def _current_profile_version_id(db: Session, user_id: str) -> str | None:
    profile = db.get(CandidateProfile, user_id)
    return profile.current_version_id if profile else None


def _current_preference_version_id(db: Session, user_id: str) -> str | None:
    return db.scalar(
        select(PreferenceVersion.id)
        .where(PreferenceVersion.user_id == user_id, PreferenceVersion.status == "active")
        .order_by(PreferenceVersion.created_at.desc())
    )


def _record_event(
    db: Session,
    application: Application,
    event_type: str,
    *,
    actor: str,
    metadata: dict[str, Any],
    request_id: str | None,
) -> None:
    """Timeline event plus mirrored audit event. Metadata must never carry answer values."""
    db.add(
        ApplicationEvent(
            application_id=application.id,
            event_type=event_type,
            actor=actor,
            metadata_json=metadata,
        )
    )
    record_audit(
        db,
        action=f"application.{event_type}",
        actor=actor,
        user_id=application.user_id,
        object_type="application",
        object_id=application.id,
        metadata=metadata,
        request_id=request_id,
    )


# --- answers ------------------------------------------------------------------------------


def _job_country(normalized: dict[str, Any]) -> tuple[str | None, str | None]:
    """The single country the authorization questions ask about, or a reason it is unknown."""
    location_country = normalized.get("location_country")
    if isinstance(location_country, str) and location_country.strip():
        return location_country.strip().upper(), None
    permitted = sorted(
        {str(item).strip().upper() for item in normalized.get("permitted_countries") or []}
        - {""}
    )
    if len(permitted) == 1:
        return permitted[0], None
    if len(permitted) > 1:
        return None, "job_geography_ambiguous"
    return None, "job_geography_unknown"


def _first_value(fact: ProfileFact, *names: str) -> Any:
    for name in names:
        value = (fact.value_json or {}).get(name)
        if value is not None:
            return value
    return None


def _text_value(fact: ProfileFact, *names: str) -> str | None:
    value = _first_value(fact, *names)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _notice_value(fact: ProfileFact) -> str | None:
    days = _first_value(fact, "notice_period_days")
    if isinstance(days, int) and not isinstance(days, bool) and days >= 0:
        return f"{days} days"
    return _text_value(fact, "notice_period")


def _bool_value(fact: ProfileFact, *names: str) -> str | None:
    value = _first_value(fact, *names)
    if isinstance(value, bool):
        return "yes" if value else "no"
    return None


def _fact_countries(fact: ProfileFact) -> set[str]:
    raw = _first_value(fact, "countries", "country")
    if isinstance(raw, str):
        return {raw.strip().upper()}
    if isinstance(raw, list):
        return {str(item).strip().upper() for item in raw}
    return set()


def _authorization_value(fact: ProfileFact) -> str | None:
    status = _first_value(fact, "status")
    if not isinstance(status, str):
        return None
    status = status.strip().lower()
    if status in AUTHORIZED_STATUSES:
        return "yes"
    if status in NOT_AUTHORIZED_STATUSES:
        return "no"
    return None


def _answer_from_facts(
    question: Question,
    facts: list[ProfileFact],
    fact_type: str,
    extract: Any,
) -> dict[str, Any]:
    """Resolve one answer from facts of one type. Unknown stays unknown; conflicts stay open."""
    confirmed: dict[str, list[str]] = {}
    unconfirmed_ids: list[str] = []
    for fact in facts:
        if fact.fact_type != fact_type:
            continue
        value = extract(fact)
        if value is None:
            continue
        if fact.confirmation_status == "confirmed":
            confirmed.setdefault(value, []).append(fact.id)
        elif fact.confirmation_status == "unknown":
            unconfirmed_ids.append(fact.id)
    if len(confirmed) == 1:
        value, fact_ids = next(iter(confirmed.items()))
        return _answer(question, "answered", value, "profile_fact", sorted(fact_ids), None)
    if len(confirmed) > 1:
        fact_ids = sorted(fact_id for ids in confirmed.values() for fact_id in ids)
        return _answer(question, "unresolved", None, None, fact_ids, "conflicting_facts")
    if unconfirmed_ids:
        return _answer(
            question, "unresolved", None, None, sorted(unconfirmed_ids), "fact_unconfirmed"
        )
    return _answer(question, "unresolved", None, None, [], "no_confirmed_fact")


def _answer(
    question: Question,
    status: str,
    value: str | None,
    provenance: str | None,
    source_fact_ids: list[str],
    reason: str | None,
    *,
    text: str | None = None,
) -> dict[str, Any]:
    if status == "unresolved" and not question.required and reason == "no_confirmed_fact":
        status = "left_blank"
    return {
        "question_key": question.key,
        "question_text": text or question.text,
        "kind": question.kind,
        "required": question.required,
        "status": status,
        "value": value,
        "provenance": provenance,
        "source_fact_ids": source_fact_ids,
        "reason": reason,
    }


def derive_answers(
    *,
    facts: list[ProfileFact],
    normalized_job: dict[str, Any],
    candidate_answers: dict[str, Any],
) -> list[dict[str, Any]]:
    country, geography_reason = _job_country(normalized_job)
    country_label = country or "the job's country"
    usable = [fact for fact in facts if fact.confirmation_status != "withdrawn"]
    authorization_facts = [
        fact
        for fact in usable
        if fact.fact_type == "work_authorization" and country and country in _fact_countries(fact)
    ]
    answers: list[dict[str, Any]] = []
    for question in QUESTIONS:
        text = question.text.format(country=country_label)
        entered = candidate_answers.get(question.key)
        if isinstance(entered, dict) and isinstance(entered.get("value"), str):
            answers.append(
                _answer(
                    question, "answered", entered["value"], "candidate_entered", [], None, text=text
                )
            )
            continue
        if question.key in {"work_authorization", "sponsorship_required"}:
            if country is None:
                answer = _answer(question, "unresolved", None, None, [], geography_reason)
            elif question.key == "work_authorization":
                answer = _answer_from_facts(
                    question, authorization_facts, "work_authorization", _authorization_value
                )
            else:
                answer = _answer_from_facts(
                    question,
                    authorization_facts,
                    "work_authorization",
                    lambda fact: _bool_value(fact, "sponsorship_needed", "needs_sponsorship"),
                )
        elif question.key == "full_name":
            answer = _answer_from_facts(
                question, usable, "contact", lambda fact: _text_value(fact, "full_name", "name")
            )
        elif question.key in {"email", "phone"}:
            key = question.key
            answer = _answer_from_facts(
                question, usable, "contact", lambda fact, key=key: _text_value(fact, key)
            )
        elif question.key == "current_location":
            answer = _answer_from_facts(
                question, usable, "contact", lambda fact: _text_value(fact, "location")
            )
        elif question.key == "notice_period":
            answer = _answer_from_facts(question, usable, "availability", _notice_value)
        elif question.key == "relocation":
            answer = _answer_from_facts(
                question, usable, "relocation", lambda fact: _bool_value(fact, "willing")
            )
        else:
            # salary_expectation: candidate-entered only; preferences are never answers.
            answer = _answer(question, "unresolved", None, None, [], "no_confirmed_fact")
        answer["question_text"] = text
        answers.append(answer)
    return answers


def validate_candidate_answer(question_key: str, value: str | None) -> str | None:
    question = QUESTIONS_BY_KEY.get(question_key)
    if question is None:
        raise ApplicationError("unknown_question", "Unknown question", status_code=404)
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        raise ApplicationError(
            "invalid_answer", "An answer cannot be empty; send null to clear it", status_code=422
        )
    if len(cleaned) > MAX_ANSWER_LENGTH or _CONTROL_CHARACTERS.search(cleaned):
        raise ApplicationError(
            "invalid_answer", "The answer is too long or malformed", status_code=422
        )
    if question.kind == "yes_no":
        cleaned = cleaned.lower()
        if cleaned not in {"yes", "no"}:
            raise ApplicationError("invalid_answer", "Answer yes or no", status_code=422)
    if question.kind == "email" and not _EMAIL.match(cleaned):
        raise ApplicationError("invalid_answer", "Enter a valid email address", status_code=422)
    return cleaned


# --- package build ------------------------------------------------------------------------


def _destination(url: str | None) -> dict[str, Any] | None:
    if not url:
        return None
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        return None
    return {"kind": "link_out", "url": url, "host": parts.hostname}


def _source_policy(connector: SourceConnector, snapshot: JobSnapshot) -> dict[str, Any]:
    authorization = connector.authorization_json or {}
    return {
        "connector_id": connector.id,
        "name": connector.name,
        "kind": connector.kind,
        "policy_status": connector.policy_status,
        "enabled": bool(connector.enabled),
        "authorization_basis": authorization.get("basis"),
        "terms_version": connector.terms_version,
        "provider_version": (snapshot.provenance_json or {}).get("provider_version"),
    }


def _artifact_sha(db: Session, artifact_id: str | None) -> str | None:
    if not artifact_id:
        return None
    artifact = db.get(StoredArtifact, artifact_id)
    return artifact.sha256 if artifact else None


def build_package(
    db: Session,
    *,
    user_id: str,
    job: JobPosting,
    cv_version_id: str,
    candidate_answers: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Assemble the canonical package for live inputs, or refuse with a specific reason.

    Returns (package, bindings) where bindings are the row-level foreign keys.
    """
    if job.status != "active":
        raise ApplicationError("job_not_active", "The job posting is not active")
    connector = db.get(SourceConnector, job.connector_id)
    if connector is None or not connector.enabled or connector.policy_status != "approved":
        raise ApplicationError(
            "source_not_authorized", "The job's source is not currently enabled and approved"
        )
    cv = db.scalar(
        select(CVVersion).where(CVVersion.id == cv_version_id, CVVersion.user_id == user_id)
    )
    if cv is None:
        raise ApplicationError("cv_not_found", "CV version not found", status_code=404)
    if cv.job_id != job.id:
        raise ApplicationError(
            "cv_job_mismatch", "The CV was drafted for a different job", status_code=400
        )
    if cv.status != "approved":
        raise ApplicationError(
            "cv_not_approved", "Only an approved CV can be packaged", status_code=400
        )
    match = (
        db.scalar(
            select(MatchRun).where(MatchRun.id == cv.match_run_id, MatchRun.user_id == user_id)
        )
        if cv.match_run_id
        else None
    )
    if match is None or match.eligibility_status != "eligible":
        raise ApplicationError(
            "match_not_eligible", "The CV's match is not eligible", status_code=400
        )
    current_profile = _current_profile_version_id(db, user_id)
    if current_profile is None or not (
        current_profile == cv.profile_version_id == match.profile_version_id
    ):
        raise ApplicationError(
            "profile_changed", "The profile changed since this CV was drafted; draft a new CV"
        )
    if _current_preference_version_id(db, user_id) != match.preference_version_id:
        raise ApplicationError(
            "preferences_changed",
            "Preferences changed since this match; re-run the match and draft a new CV",
        )
    snapshot = latest_snapshot(db, job.id)
    if snapshot is None:
        raise ApplicationError("job_snapshot_missing", "The job has no snapshot")
    if cv.job_snapshot_id != snapshot.id:
        cv_snapshot = db.get(JobSnapshot, cv.job_snapshot_id)
        previous = comparable_job_content(cv_snapshot) if cv_snapshot else None
        current = comparable_job_content(snapshot)
        if current is None or changed_material_fields(previous, current):
            raise ApplicationError(
                "cv_out_of_date", "The job changed materially since this CV was drafted"
            )

    normalized = dict(snapshot.normalized_json or {})
    facts = list(
        db.scalars(
            select(ProfileFact)
            .where(ProfileFact.profile_version_id == cv.profile_version_id)
            .order_by(ProfileFact.created_at.asc(), ProfileFact.id.asc())
        ).all()
    )
    answers = derive_answers(
        facts=facts, normalized_job=normalized, candidate_answers=candidate_answers
    )
    unresolved: list[dict[str, Any]] = [
        {"code": "answer_required", "question_key": item["question_key"], "reason": item["reason"]}
        for item in answers
        if item["required"] and item["status"] != "answered"
    ]
    warnings: list[dict[str, Any]] = [
        {
            "code": "optional_answer_unresolved",
            "question_key": item["question_key"],
            "reason": item["reason"],
        }
        for item in answers
        if not item["required"] and item["status"] == "unresolved"
    ]
    warnings.extend(
        {"code": "cv_unresolved", "message": str(item)}
        for item in (cv.source_manifest_json or {}).get("unresolved", [])
    )
    destination = _destination(normalized.get("canonical_url") or job.canonical_url)
    if destination is None:
        unresolved.append({"code": "destination_invalid", "question_key": None, "reason": None})
    scores = match.scores_json or {}
    employer = normalized.get("employer") or job.employer
    host = destination["host"] if destination else "the employer's site"
    package: dict[str, Any] = {
        "schema_version": PACKAGE_SCHEMA_VERSION,
        "question_set_version": QUESTION_SET_VERSION,
        "job": {
            "job_id": job.id,
            "connector_id": job.connector_id,
            "source_job_id": job.source_job_id,
            "snapshot_id": snapshot.id,
            "snapshot_version": snapshot.version,
            "content_hash": snapshot.content_hash,
            "raw_content_hash": snapshot.raw_content_hash,
            "retrieved_at": _iso(snapshot.retrieved_at),
            "title": normalized.get("title") or job.title,
            "employer": employer,
            "location_text": normalized.get("location_text"),
            "work_mode": normalized.get("work_mode", "unknown"),
            "status": job.status,
        },
        "source_policy": _source_policy(connector, snapshot),
        "profile_version_id": cv.profile_version_id,
        "preference_version_id": match.preference_version_id,
        "match": {
            "match_run_id": match.id,
            "policy_version": match.policy_version,
            "extractor_version": match.extractor_version,
            "eligibility": scores.get("eligibility", {}),
            "fit": scores.get("fit", {}),
            "preference": scores.get("preference", {}),
        },
        "cv": {
            "cv_version_id": cv.id,
            "content_hash": cv.content_hash,
            "generator_version": cv.generator_version,
            "template_version": cv.template_version,
            "pdf_sha256": _artifact_sha(db, cv.pdf_artifact_id),
            "docx_sha256": _artifact_sha(db, cv.docx_artifact_id),
        },
        "answers": answers,
        "unresolved": unresolved,
        "warnings": warnings,
        "destination": destination,
        "data_sharing_notice": {
            "version": DATA_SHARING_NOTICE_VERSION,
            "text": (
                "JobFinderAgent does not send this package anywhere. If you apply, you do so "
                f"yourself on {host}. Anything you enter or upload there, including your CV, "
                f"is shared with {employer} and the site operator under their terms."
            ),
        },
    }
    bindings = {
        "job_snapshot_id": snapshot.id,
        "match_run_id": match.id,
        "cv_version_id": cv.id,
        "profile_version_id": cv.profile_version_id,
        "preference_version_id": match.preference_version_id,
        "connector_id": connector.id,
    }
    return package, bindings


# --- binding check and derived state ------------------------------------------------------


def _linked_job_ids(db: Session, job: JobPosting, user_id: str) -> list[str]:
    primary_id = job.duplicate_of_job_id or job.id
    return list(
        db.scalars(
            select(JobPosting.id).where(
                or_(JobPosting.id == primary_id, JobPosting.duplicate_of_job_id == primary_id),
                JobPosting.id != job.id,
                visible_to_user(user_id),
            )
        ).all()
    )


def binding_drift(
    db: Session, application: Application, package: ApplicationPackage
) -> list[dict[str, str]]:
    """Every bound input that no longer matches live state. Empty means the package is current."""
    drift: list[dict[str, str]] = []

    def add(code: str, message: str) -> None:
        drift.append({"code": code, "message": message})

    if package_hash(package.package_json) != package.review_hash:
        add("package_integrity_failed", "The stored package no longer matches its review hash.")
    job = db.get(JobPosting, application.job_id)
    if job is None or job.status != "active":
        add("job_not_active", "The job posting is no longer active.")
    snapshot = latest_snapshot(db, application.job_id)
    if snapshot is None or snapshot.id != package.job_snapshot_id:
        add("job_snapshot_changed", "The job posting changed since this package was built.")
    connector = db.get(SourceConnector, package.connector_id)
    bound_policy = (package.package_json or {}).get("source_policy", {})
    if (
        connector is None
        or not connector.enabled
        or connector.policy_status != "approved"
        or (connector.authorization_json or {}).get("basis")
        != bound_policy.get("authorization_basis")
        or connector.terms_version != bound_policy.get("terms_version")
    ):
        add("source_policy_changed", "The job's source is no longer enabled under the same policy.")
    cv = db.get(CVVersion, package.cv_version_id)
    if cv is None or cv.status != "approved":
        add("cv_not_approved", "The packaged CV is no longer approved.")
    if _current_profile_version_id(db, application.user_id) != package.profile_version_id:
        add("profile_changed", "Your profile changed since this package was built.")
    if _current_preference_version_id(db, application.user_id) != package.preference_version_id:
        add("preferences_changed", "Your preferences changed since this package was built.")
    if job is not None:
        linked = _linked_job_ids(db, job, application.user_id)
        if linked and db.scalar(
            select(func.count())
            .select_from(Application)
            .where(
                Application.user_id == application.user_id,
                Application.job_id.in_(linked),
                Application.status == "open",
            )
        ):
            add(
                "duplicate_suspected",
                "You have another open application for a linked posting of this job. "
                "Skip one of them before approving.",
            )
    return drift


def approval_state(
    approval: ApprovalRecord,
    *,
    current_package_id: str | None,
    drift: list[dict[str, str]],
    now: datetime,
) -> str:
    if approval.revoked_at is not None or approval.package_id != current_package_id:
        return "revoked"
    if drift:
        return "invalidated"
    expires_at = _aware(approval.expires_at)
    if expires_at is None or now >= expires_at:
        return "expired"
    return "active"


@dataclass(frozen=True)
class ApplicationView:
    application: Application
    package: ApplicationPackage | None
    drift: list[dict[str, str]]
    approval: ApprovalRecord | None
    approval_state: str | None
    status: str
    events: list[ApplicationEvent]


def current_package(db: Session, application: Application) -> ApplicationPackage | None:
    if application.current_package_id is None:
        return None
    return db.get(ApplicationPackage, application.current_package_id)


def describe(
    db: Session, application: Application, *, now: datetime | None = None
) -> ApplicationView:
    now = now or now_utc()
    package = current_package(db, application)
    drift = binding_drift(db, application, package) if package is not None else [
        {"code": "package_missing", "message": "No package has been built."}
    ]
    approval = db.scalar(
        select(ApprovalRecord)
        .where(ApprovalRecord.application_id == application.id)
        .order_by(ApprovalRecord.approved_at.desc(), ApprovalRecord.id.desc())
    )
    state = (
        approval_state(
            approval, current_package_id=application.current_package_id, drift=drift, now=now
        )
        if approval is not None
        else None
    )
    if is_applied(db, application):
        status = "applied"
    elif application.status == "skipped":
        status = "skipped"
    elif drift:
        status = "review_package_stale"
    elif state == "active":
        status = "approved"
    else:
        status = "review_package_ready"
    events = list(
        db.scalars(
            select(ApplicationEvent)
            .where(ApplicationEvent.application_id == application.id)
            .order_by(ApplicationEvent.occurred_at.asc(), ApplicationEvent.id.asc())
        ).all()
    )
    return ApplicationView(application, package, drift, approval, state, status, events)


# --- operations ---------------------------------------------------------------------------


def owned_application(
    db: Session, application_id: str, user_id: str, *, for_update: bool = False
) -> Application:
    query = select(Application).where(
        Application.id == application_id, Application.user_id == user_id
    )
    if for_update:
        query = query.with_for_update()
    application = db.scalar(query)
    if application is None:
        raise ApplicationError("not_found", "Application not found", status_code=404)
    return application


def _revoke_unrevoked(
    db: Session,
    application: Application,
    *,
    reason: str,
    actor: str,
    request_id: str | None,
    now: datetime,
) -> int:
    revoked = 0
    for approval in db.scalars(
        select(ApprovalRecord).where(
            ApprovalRecord.application_id == application.id, ApprovalRecord.revoked_at.is_(None)
        )
    ).all():
        approval.revoked_at = now
        approval.revoked_reason = reason
        _record_event(
            db,
            application,
            "approval_revoked",
            actor=actor,
            metadata={"approval_id": approval.id, "reason": reason},
            request_id=request_id,
        )
        revoked += 1
    return revoked


def _build_for(
    db: Session, application: Application, *, cv_version_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    job = db.get(JobPosting, application.job_id)
    if job is None:
        raise ApplicationError("not_found", "Job not found", status_code=404)
    return build_package(
        db,
        user_id=application.user_id,
        job=job,
        cv_version_id=cv_version_id,
        candidate_answers=dict(application.candidate_answers_json or {}),
    )


def _store_package(
    db: Session,
    application: Application,
    package_json: dict[str, Any],
    bindings: dict[str, Any],
    *,
    actor: str,
    request_id: str | None,
) -> tuple[ApplicationPackage, bool]:
    """Persist a built package; reuse the current revision when nothing changed."""
    review_hash = package_hash(package_json)
    existing = current_package(db, application)
    if (
        existing is not None
        and existing.review_hash == review_hash
        and existing.superseded_at is None
    ):
        return existing, False
    now = now_utc()
    if existing is not None:
        existing.superseded_at = now
        existing.superseded_reason = "rebuilt"
    _revoke_unrevoked(
        db, application, reason="package_rebuilt", actor=actor, request_id=request_id, now=now
    )
    revision = (
        db.scalar(
            select(func.max(ApplicationPackage.revision)).where(
                ApplicationPackage.application_id == application.id
            )
        )
        or 0
    ) + 1
    package = ApplicationPackage(
        application_id=application.id,
        revision=revision,
        review_hash=review_hash,
        package_json=package_json,
        **bindings,
    )
    db.add(package)
    db.flush()
    application.current_package_id = package.id
    _record_event(
        db,
        application,
        "package_built",
        actor=actor,
        metadata={
            "package_id": package.id,
            "revision": revision,
            "review_hash": review_hash,
            "cv_version_id": bindings["cv_version_id"],
            "job_snapshot_id": bindings["job_snapshot_id"],
            "unresolved_count": len(package_json["unresolved"]),
        },
        request_id=request_id,
    )
    db.flush()
    return package, True


def prepare_application(
    db: Session,
    *,
    user_id: str,
    job_id: str,
    cv_version_id: str,
    request_id: str | None = None,
) -> tuple[Application, bool]:
    """Create, reopen, or rebuild the candidate's package for a job.

    Returns (application, created) where created is True for a new application.
    """
    job = get_visible_job(db, job_id, user_id)
    if job is None:
        raise ApplicationError("not_found", "Job not found", status_code=404)
    application = db.scalar(
        select(Application)
        .where(Application.user_id == user_id, Application.job_id == job.id)
        .with_for_update()
    )
    if application is not None:
        _ensure_not_applied(db, application)
    # Build (and so validate) before changing any state: a refused build leaves nothing behind.
    package_json, bindings = build_package(
        db,
        user_id=user_id,
        job=job,
        cv_version_id=cv_version_id,
        candidate_answers=dict(application.candidate_answers_json or {}) if application else {},
    )
    created = False
    if application is None:
        try:
            with db.begin_nested():
                application = Application(
                    user_id=user_id,
                    job_id=job.id,
                    connector_id=job.connector_id,
                    source_job_id=job.source_job_id,
                    status="open",
                    target_kind="link_out",
                    candidate_answers_json={},
                )
                db.add(application)
                db.flush()
            created = True
        except IntegrityError:
            # A concurrent request created it first; continue with that row.
            application = db.scalar(
                select(Application)
                .where(Application.user_id == user_id, Application.job_id == job.id)
                .with_for_update()
            )
            if application is None:
                raise
    if application.status == "skipped":
        application.status = "open"
        application.skip_reason = None
        _record_event(
            db, application, "reopened", actor="user", metadata={}, request_id=request_id
        )
    _store_package(db, application, package_json, bindings, actor="user", request_id=request_id)
    return application, created


def is_applied(db: Session, application: Application) -> bool:
    """True once the candidate recorded applying (a tracker entry is bound to it)."""
    return (
        db.scalar(
            select(TrackedApplication.id).where(
                TrackedApplication.application_id == application.id
            )
        )
        is not None
    )


def _ensure_not_applied(db: Session, application: Application) -> None:
    # The bound package revision is the record of what was prepared: it stays frozen.
    if is_applied(db, application):
        raise ApplicationError(
            "already_applied",
            "You marked this application as applied; remove that tracker entry to change it",
        )


def record_application_event(
    db: Session,
    application: Application,
    event_type: str,
    *,
    actor: str,
    metadata: dict[str, Any],
    request_id: str | None,
) -> None:
    _record_event(
        db, application, event_type, actor=actor, metadata=metadata, request_id=request_id
    )


def _require_open_current(db: Session, application: Application) -> ApplicationPackage:
    _ensure_not_applied(db, application)
    if application.status != "open":
        raise ApplicationError(
            "application_skipped", "The application was skipped; prepare it again"
        )
    package = current_package(db, application)
    if package is None:
        raise ApplicationError("package_missing", "No package has been built")
    drift = binding_drift(db, application, package)
    if drift:
        raise ApplicationError(
            "package_stale",
            "The package is out of date: " + ", ".join(item["code"] for item in drift),
        )
    return package


def set_candidate_answer(
    db: Session,
    *,
    user_id: str,
    application_id: str,
    question_key: str,
    value: str | None,
    request_id: str | None = None,
) -> Application:
    cleaned = validate_candidate_answer(question_key, value)
    application = owned_application(db, application_id, user_id, for_update=True)
    package = _require_open_current(db, application)
    answers = dict(application.candidate_answers_json or {})
    if cleaned is None:
        answers.pop(question_key, None)
    else:
        answers[question_key] = {"value": cleaned, "entered_at": _iso(now_utc())}
    application.candidate_answers_json = answers
    package_json, bindings = _build_for(db, application, cv_version_id=package.cv_version_id)
    _record_event(
        db,
        application,
        "answer_updated",
        actor="user",
        metadata={"question_key": question_key, "cleared": cleaned is None},
        request_id=request_id,
    )
    _store_package(db, application, package_json, bindings, actor="user", request_id=request_id)
    return application


def approve_package(
    db: Session,
    *,
    user_id: str,
    application_id: str,
    review_hash: str,
    request_id: str | None = None,
) -> ApprovalRecord:
    application = owned_application(db, application_id, user_id, for_update=True)
    package = _require_open_current(db, application)
    if review_hash != package.review_hash:
        raise ApplicationError(
            "hash_mismatch", "The package changed since you reviewed it; review it again"
        )
    if package.package_json.get("unresolved"):
        raise ApplicationError(
            "unresolved_items", "Resolve every required item before approving"
        )
    now = now_utc()
    for existing in db.scalars(
        select(ApprovalRecord).where(
            ApprovalRecord.package_id == package.id, ApprovalRecord.revoked_at.is_(None)
        )
    ).all():
        expires_at = _aware(existing.expires_at)
        if expires_at is not None and now < expires_at:
            return existing  # Idempotent: a double click does not create a second approval.
    approval = ApprovalRecord(
        application_id=application.id,
        package_id=package.id,
        reviewed_payload_hash=package.review_hash,
        approved_by_user_id=user_id,
        approved_at=now,
        expires_at=now + APPROVAL_TTL,
    )
    db.add(approval)
    db.flush()
    _record_event(
        db,
        application,
        "approved",
        actor="user",
        metadata={
            "approval_id": approval.id,
            "package_id": package.id,
            "revision": package.revision,
            "review_hash": package.review_hash,
            "expires_at": _iso(approval.expires_at),
        },
        request_id=request_id,
    )
    return approval


def revoke_approval(
    db: Session, *, user_id: str, application_id: str, request_id: str | None = None
) -> Application:
    application = owned_application(db, application_id, user_id, for_update=True)
    if not _revoke_unrevoked(
        db,
        application,
        reason="candidate_revoked",
        actor="user",
        request_id=request_id,
        now=now_utc(),
    ):
        raise ApplicationError("no_approval", "There is no approval to revoke")
    return application


def skip_application(
    db: Session,
    *,
    user_id: str,
    application_id: str,
    reason: str | None,
    request_id: str | None = None,
) -> Application:
    application = owned_application(db, application_id, user_id, for_update=True)
    _ensure_not_applied(db, application)
    if application.status == "skipped":
        return application
    if reason is not None and (
        len(reason) > MAX_ANSWER_LENGTH or _CONTROL_CHARACTERS.search(reason)
    ):
        raise ApplicationError(
            "invalid_reason", "The reason is too long or malformed", status_code=422
        )
    now = now_utc()
    _revoke_unrevoked(
        db, application, reason="skipped", actor="user", request_id=request_id, now=now
    )
    application.status = "skipped"
    application.skip_reason = reason.strip() if reason and reason.strip() else None
    _record_event(db, application, "skipped", actor="user", metadata={}, request_id=request_id)
    db.flush()
    return application


def delete_applications(db: Session, *, application_ids: list[str]) -> int:
    """Delete applications and everything bound to them (explicit order; SQLite has no FK)."""
    if not application_ids:
        return 0
    # Tracker entries are the candidate's own history: unlink, never delete, them here.
    db.execute(
        update(TrackedApplication)
        .where(TrackedApplication.application_id.in_(application_ids))
        .values(application_id=None)
    )
    db.execute(delete(ApprovalRecord).where(ApprovalRecord.application_id.in_(application_ids)))
    db.execute(
        delete(ApplicationEvent).where(ApplicationEvent.application_id.in_(application_ids))
    )
    db.execute(
        delete(ApplicationPackage).where(ApplicationPackage.application_id.in_(application_ids))
    )
    db.execute(delete(Application).where(Application.id.in_(application_ids)))
    return len(application_ids)


def export_applications(db: Session, user_id: str) -> list[dict[str, Any]]:
    """Every application with all package revisions, approvals, and timeline events."""
    result: list[dict[str, Any]] = []
    for application in db.scalars(
        select(Application)
        .where(Application.user_id == user_id)
        .order_by(Application.created_at.asc(), Application.id.asc())
    ).all():
        packages = db.scalars(
            select(ApplicationPackage)
            .where(ApplicationPackage.application_id == application.id)
            .order_by(ApplicationPackage.revision.asc())
        ).all()
        approvals = db.scalars(
            select(ApprovalRecord)
            .where(ApprovalRecord.application_id == application.id)
            .order_by(ApprovalRecord.approved_at.asc(), ApprovalRecord.id.asc())
        ).all()
        events = db.scalars(
            select(ApplicationEvent)
            .where(ApplicationEvent.application_id == application.id)
            .order_by(ApplicationEvent.occurred_at.asc(), ApplicationEvent.id.asc())
        ).all()
        result.append(
            {
                "id": application.id,
                "job_id": application.job_id,
                "connector_id": application.connector_id,
                "source_job_id": application.source_job_id,
                "status": application.status,
                "target_kind": application.target_kind,
                "skip_reason": application.skip_reason,
                "candidate_answers": application.candidate_answers_json or {},
                "current_package_id": application.current_package_id,
                "created_at": _iso(application.created_at),
                "packages": [
                    {
                        "id": package.id,
                        "revision": package.revision,
                        "review_hash": package.review_hash,
                        "content": package.package_json,
                        "superseded_at": _iso(package.superseded_at),
                        "superseded_reason": package.superseded_reason,
                        "created_at": _iso(package.created_at),
                    }
                    for package in packages
                ],
                "approvals": [
                    {
                        "id": approval.id,
                        "package_id": approval.package_id,
                        "reviewed_payload_hash": approval.reviewed_payload_hash,
                        "approved_at": _iso(approval.approved_at),
                        "expires_at": _iso(approval.expires_at),
                        "revoked_at": _iso(approval.revoked_at),
                        "revoked_reason": approval.revoked_reason,
                    }
                    for approval in approvals
                ],
                "events": [
                    {
                        "event_type": event.event_type,
                        "actor": event.actor,
                        "metadata": event.metadata_json or {},
                        "occurred_at": _iso(event.occurred_at),
                    }
                    for event in events
                ],
            }
        )
    return result
