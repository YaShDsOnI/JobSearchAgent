"""Submission state machine for the sandbox adapter (Phase 6).

API side: start, confirm, cancel, resolve. Runner side: process fill and submit tasks.
No browser or network imports: the runner is injected (`types.Runner`). Design:
docs/application-agent.md, "Phase 6: sandbox browser adapter".
"""

import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.models import (
    SUBMISSION_LIVE_STATES,
    Application,
    ApplicationPackage,
    ApplicationSubmission,
    CVVersion,
    StoredArtifact,
    Task,
    now_utc,
)
from app.services.applications import (
    ApplicationError,
    describe,
    is_applied,
    owned_application,
    record_application_event,
)
from app.services.storage import ArtifactStorage
from app.services.submission.destinations import SANDBOX_DESTINATION, destination_block_reason
from app.services.submission.types import AbortBeforeClick, Runner, RunRequest
from app.services.tasks import claim_next_task, complete_task, enqueue_task
from app.services.tracker import record_adapter_submission

TASK_FILL = "application_fill"
TASK_SUBMIT = "application_submit"
TASK_KINDS = (TASK_FILL, TASK_SUBMIT)
MAX_FILL_RUNS = 3
RUNNER_LEASE_SECONDS = 300
CANCELLABLE = frozenset({"queued", "filling", "final_review", "submit_queued", "paused"})


def _error(code: str, message: str, status_code: int = 409) -> ApplicationError:
    return ApplicationError(code, message, status_code=status_code)


def latest_submission(db: Session, application_id: str) -> ApplicationSubmission | None:
    return db.scalar(
        select(ApplicationSubmission)
        .where(ApplicationSubmission.application_id == application_id)
        .order_by(ApplicationSubmission.created_at.desc(), ApplicationSubmission.id.desc())
    )


def live_submission(db: Session, application_id: str) -> ApplicationSubmission | None:
    return db.scalar(
        select(ApplicationSubmission).where(
            ApplicationSubmission.application_id == application_id,
            ApplicationSubmission.state.in_(SUBMISSION_LIVE_STATES),
        )
    )


def _locked(db: Session, submission_id: str) -> ApplicationSubmission | None:
    db.expire_all()
    return db.scalar(
        select(ApplicationSubmission)
        .where(ApplicationSubmission.id == submission_id)
        .with_for_update()
    )


def _event(
    db: Session,
    submission: ApplicationSubmission,
    event_type: str,
    *,
    actor: str,
    metadata: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> None:
    application = db.get(Application, submission.application_id)
    if application is None:
        return
    record_application_event(
        db,
        application,
        event_type,
        actor=actor,
        metadata={"submission_id": submission.id, **(metadata or {})},
        request_id=request_id,
    )


def _finish(
    db: Session,
    submission: ApplicationSubmission,
    state: str,
    event_type: str,
    *,
    actor: str,
    reason: str | None = None,
) -> None:
    submission.state = state
    if state == "paused":
        submission.pause_reason = reason
    elif reason is not None:
        submission.error_code = reason
    if state not in {"final_review", "submit_queued", "submitting", "paused"}:
        submission.finished_at = now_utc()
    _event(db, submission, event_type, actor=actor,
           metadata={"state": state, "reason": reason} if reason else {"state": state})


def gate_reason(
    db: Session, submission: ApplicationSubmission, settings: Settings
) -> str | None:
    """Why the adapter may not act on this submission right now (fresh read), or None."""
    application = db.get(Application, submission.application_id)
    if application is None:
        return "application_missing"
    view = describe(db, application)
    package = view.package
    if package is None or package.id != submission.package_id:
        return "package_changed"
    if package.review_hash != submission.review_hash:
        return "package_changed"
    if view.drift:
        return "binding_stale"
    if view.approval is None or view.approval_state != "active":
        return "approval_not_active"
    if view.approval.package_id != submission.package_id:
        return "approval_not_active"
    return destination_block_reason(db, settings, (package.package_json or {}).get("destination"))


# --- API operations ---------------------------------------------------------------------


def start_submission(
    db: Session,
    *,
    user_id: str,
    application_id: str,
    review_hash: str,
    request_id: str | None = None,
    settings: Settings | None = None,
) -> ApplicationSubmission:
    settings = settings or get_settings()
    application = owned_application(db, application_id, user_id, for_update=True)
    if is_applied(db, application):
        raise _error("already_applied", "This application is already recorded as applied")
    view = describe(db, application)
    package = view.package
    if package is None:
        raise _error("package_missing", "No package has been built")
    if review_hash != package.review_hash:
        raise _error("hash_mismatch", "The package changed since you reviewed it")
    destination = (package.package_json or {}).get("destination") or {}
    if destination.get("kind") != SANDBOX_DESTINATION:
        raise _error(
            "destination_not_supported",
            "Only sandbox ATS jobs can be submitted; apply to this job yourself",
        )
    key = f"{application.id}:{package.id}"
    existing = db.scalar(
        select(ApplicationSubmission).where(ApplicationSubmission.idempotency_key == key)
    )
    if existing is not None:
        return existing  # Idempotent: a repeated start returns the same submission.
    other = live_submission(db, application.id)
    if other is not None:
        raise _error("submission_in_progress", "Another submission for this job is in progress")
    if view.status != "approved" or view.approval is None:
        raise _error("approval_required", "Approve this exact package before submitting")
    blocked = destination_block_reason(db, settings, destination)
    if blocked is not None:
        raise _error(blocked, "Submission to the sandbox is not enabled right now")
    submission = ApplicationSubmission(
        application_id=application.id,
        user_id=user_id,
        package_id=package.id,
        review_hash=package.review_hash,
        approval_id=view.approval.id,
        destination_name=SANDBOX_DESTINATION,
        destination_url=str(destination.get("url")),
        idempotency_key=key,
        state="queued",
        fill_runs=0,
    )
    try:
        with db.begin_nested():
            db.add(submission)
            db.flush()
    except IntegrityError:
        found = db.scalar(
            select(ApplicationSubmission).where(ApplicationSubmission.idempotency_key == key)
        )
        if found is None:
            raise
        return found
    enqueue_task(
        db,
        kind=TASK_FILL,
        idempotency_key=f"{TASK_FILL}:{submission.id}",
        payload={"submission_id": submission.id},
    )
    _event(db, submission, "submission_started", actor="user",
           metadata={"package_id": package.id, "review_hash": package.review_hash},
           request_id=request_id)
    return submission


def _owned_latest(db: Session, user_id: str, application_id: str) -> ApplicationSubmission:
    application = owned_application(db, application_id, user_id)
    submission = latest_submission(db, application.id)
    if submission is None:
        raise _error("submission_not_found", "No submission exists", status_code=404)
    locked = _locked(db, submission.id)
    assert locked is not None
    return locked


def confirm_submission(
    db: Session,
    *,
    user_id: str,
    application_id: str,
    fill_hash: str,
    request_id: str | None = None,
    settings: Settings | None = None,
) -> ApplicationSubmission:
    """The candidate's explicit action after reviewing the filled form."""
    settings = settings or get_settings()
    submission = _owned_latest(db, user_id, application_id)
    if submission.state == "submit_queued" and submission.confirmed_fill_hash == fill_hash:
        return submission  # Idempotent double click.
    if submission.state != "final_review":
        raise _error("not_ready_to_confirm", "This submission is not waiting for your review")
    if fill_hash != submission.fill_hash:
        raise _error("fill_hash_mismatch", "The filled form changed since you reviewed it")
    reason = gate_reason(db, submission, settings)
    if reason is not None:
        raise _error(reason, "The submission cannot proceed: " + reason)
    application = db.get(Application, submission.application_id)
    assert application is not None
    view = describe(db, application)
    assert view.approval is not None
    submission.approval_id = view.approval.id
    submission.confirmed_fill_hash = fill_hash
    submission.state = "submit_queued"
    enqueue_task(
        db,
        kind=TASK_SUBMIT,
        idempotency_key=f"{TASK_SUBMIT}:{submission.id}",
        payload={"submission_id": submission.id},
    )
    _event(db, submission, "submission_confirmed", actor="user",
           metadata={"fill_hash": fill_hash}, request_id=request_id)
    return submission


def cancel_submission(
    db: Session, *, user_id: str, application_id: str, request_id: str | None = None
) -> ApplicationSubmission:
    submission = _owned_latest(db, user_id, application_id)
    if submission.submit_clicked_at is not None or submission.state not in CANCELLABLE:
        raise _error("too_late_to_cancel", "This submission can no longer be cancelled")
    submission.state = "cancelled"
    submission.finished_at = now_utc()
    _event(db, submission, "submission_cancelled", actor="user", request_id=request_id)
    return submission


def resolve_submission(
    db: Session,
    *,
    user_id: str,
    application_id: str,
    received: bool,
    request_id: str | None = None,
) -> ApplicationSubmission:
    """The candidate says whether an unverified submission arrived (never auto-retried)."""
    submission = _owned_latest(db, user_id, application_id)
    if submission.state != "submitted_unverified":
        raise _error("not_unverified", "Only an unverified submission can be resolved")
    if received:
        submission.state = "submitted"
        submission.verification = "candidate_reported"
        _record_tracker(db, submission)
        _event(db, submission, "submission_verified", actor="user",
               metadata={"verification": "candidate_reported"}, request_id=request_id)
    else:
        submission.state = "not_received"
        _event(db, submission, "submission_not_received", actor="user", request_id=request_id)
    submission.finished_at = now_utc()
    return submission


def _record_tracker(db: Session, submission: ApplicationSubmission) -> None:
    application = db.get(Application, submission.application_id)
    package = db.get(ApplicationPackage, submission.package_id)
    if application is not None and package is not None and not is_applied(db, application):
        record_adapter_submission(db, application=application, package=package,
                                  destination_url=submission.destination_url)


# --- runner side ----------------------------------------------------------------------------


@dataclass
class _Prepared:
    request: RunRequest
    workdir: Path


def _prepare(
    db: Session, storage: ArtifactStorage, submission: ApplicationSubmission
) -> _Prepared:
    package = db.get(ApplicationPackage, submission.package_id)
    if package is None:
        raise _error("package_missing", "Package missing")
    content = package.package_json or {}
    answers = {
        str(item["question_key"]): {"value": item["value"], "provenance": item.get("provenance")}
        for item in content.get("answers", [])
        if item.get("status") == "answered" and item.get("value") not in (None, "")
    }
    cv = db.get(CVVersion, package.cv_version_id)
    artifact = db.get(StoredArtifact, cv.pdf_artifact_id) if cv and cv.pdf_artifact_id else None
    expected = (content.get("cv") or {}).get("pdf_sha256")
    if artifact is None or not expected or artifact.sha256 != expected:
        raise _error("cv_unavailable", "The packaged CV is unavailable")
    workdir = Path(tempfile.mkdtemp(prefix="jfa-runner-"))
    try:
        resume = workdir / "cv.pdf"
        resume.write_bytes(storage.read_bytes(artifact.object_key))
        resume.chmod(0o600)
        destination = content.get("destination") or {}
        request = RunRequest(
            url=str(destination["url"]),
            origin=str(destination["origin"]),
            answers=answers,
            resume_path=resume,
            resume_sha256=str(expected),
        )
    except Exception:
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    return _Prepared(request=request, workdir=workdir)


def process_fill(
    db: Session,
    storage: ArtifactStorage,
    submission_id: str,
    runner: Runner,
    settings: Settings,
) -> None:
    submission = _locked(db, submission_id)
    if submission is None or submission.state not in {"queued", "filling"}:
        db.commit()
        return
    reason = gate_reason(db, submission, settings)
    if reason is not None:
        _finish(db, submission, "blocked", "submission_blocked", actor="runner", reason=reason)
        db.commit()
        return
    if submission.fill_runs >= MAX_FILL_RUNS:
        _finish(db, submission, "failed", "submission_failed", actor="runner",
                reason="fill_attempts_exhausted")
        db.commit()
        return
    submission.state = "filling"
    submission.fill_runs += 1
    db.commit()
    prepared = _prepare(db, storage, submission)
    try:
        result = runner.fill(prepared.request)
    finally:
        shutil.rmtree(prepared.workdir, ignore_errors=True)
    submission = _locked(db, submission_id)
    if submission is None or submission.state != "filling":
        db.commit()  # Cancelled while the browser ran.
        return
    if result.pause_reason or result.manifest is None or result.fill_hash is None:
        _finish(db, submission, "paused", "submission_paused", actor="runner",
                reason=result.pause_reason or "form_contract_mismatch")
    else:
        submission.fill_manifest_json = result.manifest
        submission.fill_hash = result.fill_hash
        submission.cv_sha256 = prepared.request.resume_sha256
        _finish(db, submission, "final_review", "submission_filled", actor="runner")
    db.commit()


def process_submit(
    db: Session,
    storage: ArtifactStorage,
    submission_id: str,
    runner: Runner,
    settings: Settings,
) -> None:
    submission = _locked(db, submission_id)
    if submission is None:
        db.commit()
        return
    if submission.state == "submitting" and submission.submit_clicked_at is not None:
        # Redelivered after the click was recorded: never click again.
        _finish(db, submission, "submitted_unverified", "submission_unverified",
                actor="runner", reason="interrupted_after_click")
        db.commit()
        return
    if submission.state not in {"submit_queued", "submitting"}:
        db.commit()
        return
    reason = gate_reason(db, submission, settings)
    if reason is not None:
        _finish(db, submission, "blocked", "submission_blocked", actor="runner", reason=reason)
        db.commit()
        return
    submission.state = "submitting"
    db.commit()
    expected = submission.confirmed_fill_hash or ""

    def before_click() -> None:
        current = _locked(db, submission_id)
        if current is None or current.state != "submitting":
            db.commit()
            raise AbortBeforeClick("cancelled")
        blocked = gate_reason(db, current, settings)
        if blocked is not None:
            db.commit()
            raise AbortBeforeClick(blocked)
        current.submit_clicked_at = now_utc()
        _event(db, current, "submission_clicked", actor="runner")
        db.commit()  # Recorded before the click: a crash after this never re-clicks.

    prepared = _prepare(db, storage, submission)
    try:
        result = runner.submit(prepared.request, expected_fill_hash=expected,
                               before_click=before_click)
    finally:
        shutil.rmtree(prepared.workdir, ignore_errors=True)
    submission = _locked(db, submission_id)
    assert submission is not None
    if result.outcome == "aborted":
        if submission.state == "submitting":
            _finish(db, submission, "blocked", "submission_blocked", actor="runner",
                    reason=result.reason)
    elif result.outcome == "paused" and not result.clicked:
        _finish(db, submission, "paused", "submission_paused", actor="runner",
                reason=result.reason)
    elif result.outcome == "verified" and result.confirmation_id:
        submission.confirmation_id = result.confirmation_id
        submission.verification = "confirmation_page"
        _record_tracker(db, submission)
        _finish(db, submission, "submitted", "submission_verified", actor="runner")
    else:
        _finish(db, submission, "submitted_unverified", "submission_unverified",
                actor="runner", reason=result.reason or "no_confirmation")
    db.commit()


def run_next_submission_task(
    db: Session,
    storage: ArtifactStorage,
    *,
    worker_id: str,
    runner_factory: Callable[[], Runner],
    settings: Settings | None = None,
    now: datetime | None = None,
) -> Task | None:
    """Claim and run one fill or submit task. Tasks are never retried automatically."""
    settings = settings or get_settings()
    task = claim_next_task(
        db, worker_id=worker_id, lease_seconds=RUNNER_LEASE_SECONDS, kinds=TASK_KINDS
    )
    if task is None:
        db.rollback()
        return None
    db.commit()
    submission_id = str((task.payload_json or {}).get("submission_id", ""))
    try:
        if task.kind == TASK_FILL:
            process_fill(db, storage, submission_id, runner_factory(), settings)
        else:
            process_submit(db, storage, submission_id, runner_factory(), settings)
    except Exception as exc:  # noqa: BLE001 - classified below; never retried blindly.
        db.rollback()
        submission = _locked(db, submission_id)
        if submission is not None and submission.state in {"filling", "submitting"}:
            if submission.submit_clicked_at is not None:
                _finish(db, submission, "submitted_unverified", "submission_unverified",
                        actor="runner", reason="runner_error_after_click")
            else:
                code = exc.code if isinstance(exc, ApplicationError) else "runner_error"
                _finish(db, submission, "failed", "submission_failed", actor="runner",
                        reason=code)
        db.commit()
    task = db.get(Task, task.id) or task
    complete_task(db, task, worker_id=worker_id)
    db.commit()
    return task


def submission_dict(submission: ApplicationSubmission) -> dict[str, Any]:
    return {
        "id": submission.id,
        "package_id": submission.package_id,
        "review_hash": submission.review_hash,
        "destination": submission.destination_name,
        "destination_url": submission.destination_url,
        "state": submission.state,
        "pause_reason": submission.pause_reason,
        "error_code": submission.error_code,
        "fill_manifest": submission.fill_manifest_json,
        "fill_hash": submission.fill_hash,
        "confirmed_fill_hash": submission.confirmed_fill_hash,
        "cv_sha256": submission.cv_sha256,
        "submit_clicked_at": submission.submit_clicked_at.isoformat()
        if submission.submit_clicked_at else None,
        "confirmation_id": submission.confirmation_id,
        "verification": submission.verification,
        "created_at": submission.created_at.isoformat() if submission.created_at else None,
        "finished_at": submission.finished_at.isoformat() if submission.finished_at else None,
    }
