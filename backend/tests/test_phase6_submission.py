"""Phase 6 sandbox adapter: state machine, kill switches, crash safety, and real browser runs.

Design: docs/application-agent.md, "Phase 6: sandbox browser adapter". The only destination
is the loopback sandbox ATS; browser tests start it in-process on a random port.
"""

import ast
import hashlib
import json
import socket
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from sqlalchemy import select

import app.services.applications as applications_module
import app.services.submission.service as service_module
from app.core.config import get_settings
from app.db.models import (
    ApplicationEvent,
    ApplicationSubmission,
    AuditEvent,
    JobPosting,
    Task,
    TrackedApplication,
)
from app.services.storage import ArtifactStorage
from app.services.submission.destinations import get_destination, set_destination_enabled
from app.services.submission.sandbox_setup import register_sandbox
from app.services.submission.service import TASK_FILL, TASK_SUBMIT, run_next_submission_task
from app.services.submission.types import FillResult, RunRequest, SubmitResult
from sandbox_ats.app import create_app
from tests.conftest import TestingSessionLocal, csrf_headers
from tests.test_phase2_matching import register
from tests.test_phase5c_applications import (
    approve,
    create_profile,
    prepare,
    ready_application,
    ready_cv,
    standard_facts,
)

BACKEND = Path(__file__).resolve().parent.parent
DEFAULT_ORIGIN = "http://127.0.0.1:8100"


# --- fixtures and helpers --------------------------------------------------------------------


def configure(monkeypatch, *, enabled: bool = True, origin: str = DEFAULT_ORIGIN):
    settings = get_settings().model_copy(
        update={"application_submission_enabled": enabled, "sandbox_ats_origin": origin}
    )
    for module in (applications_module, service_module):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    return settings


def setup_sandbox(*, enable_destination: bool = True, origin: str = DEFAULT_ORIGIN) -> None:
    settings = get_settings().model_copy(update={"sandbox_ats_origin": origin})
    with TestingSessionLocal() as db:
        register_sandbox(db, ArtifactStorage(get_settings()), settings, actor="test")
        destination = get_destination(db)
        assert destination is not None
        if enable_destination:
            set_destination_enabled(db, destination, enabled=True, actor="test", reason="tests")
        db.commit()


def toggle_destination(enabled: bool) -> None:
    with TestingSessionLocal() as db:
        destination = get_destination(db)
        assert destination is not None
        set_destination_enabled(db, destination, enabled=enabled, actor="test", reason="test")
        db.commit()


def sandbox_job_id(key: str) -> str:
    with TestingSessionLocal() as db:
        job_id = db.scalar(select(JobPosting.id).where(JobPosting.source_job_id == key))
    assert job_id is not None
    return job_id


def approved_sandbox_application(client, key: str = "sbx-data-analyst", email: str | None = None):
    register(client, email or f"{key}@example.test")
    create_profile(client, standard_facts())
    job_id = sandbox_job_id(key)
    application = prepare(client, job_id, ready_cv(client, job_id)).json()
    assert application["submission_available"] is True
    assert application["package"]["destination"]["kind"] == "sandbox_ats"
    assert approve(client, application).status_code == 200
    return application


def start(client, application, review_hash: str | None = None):
    return client.post(
        f"/api/v1/applications/{application['id']}/submission",
        headers=csrf_headers(client),
        json={"review_hash": review_hash or application["package"]["review_hash"]},
    )


def confirm(client, application, fill_hash: str):
    return client.post(
        f"/api/v1/applications/{application['id']}/submission/confirm",
        headers=csrf_headers(client),
        json={"fill_hash": fill_hash},
    )


def action(client, application, name: str, body: dict | None = None):
    return client.post(
        f"/api/v1/applications/{application['id']}/submission/{name}",
        headers=csrf_headers(client),
        json=body or {},
    )


def current(client, application) -> dict:
    response = client.get(f"/api/v1/applications/{application['id']}/submission")
    assert response.status_code == 200, response.text
    return response.json()


def run_tasks(settings, runner_factory: Callable[[], Any], limit: int = 5) -> int:
    processed = 0
    with TestingSessionLocal() as db:
        storage = ArtifactStorage(get_settings())
        while processed < limit and run_next_submission_task(
            db, storage, worker_id="test-runner", runner_factory=runner_factory, settings=settings
        ):
            processed += 1
    return processed


class FakeRunner:
    """Records calls; `submit` calls the real before-click hook like the browser does."""

    def __init__(self, *, fill_pause: str | None = None, outcome: str = "verified",
                 raise_after_click: bool = False, raise_before_click: bool = False,
                 before_hook: Callable[[], None] | None = None) -> None:
        self.fill_pause = fill_pause
        self.outcome = outcome
        self.raise_after_click = raise_after_click
        self.raise_before_click = raise_before_click
        self.before_hook = before_hook
        self.fills: list[RunRequest] = []
        self.clicks = 0
        self.submits = 0

    def _manifest(self, request: RunRequest) -> dict[str, Any]:
        return {
            "fields": [
                {"question_key": key, "label": key.replace("_", " ").title(),
                 "display_value": str(answer["value"]), "provenance": answer["provenance"],
                 "value_sha256": hashlib.sha256(str(answer["value"]).encode()).hexdigest(),
                 "sent": True}
                for key, answer in sorted(request.answers.items())
            ],
            "resume": {"filename": "cv.pdf", "sha256": request.resume_sha256},
            "destination_url": request.url,
        }

    def fill(self, request: RunRequest) -> FillResult:
        self.fills.append(request)
        assert request.resume_path.read_bytes().startswith(b"%PDF")
        if self.fill_pause:
            return FillResult(pause_reason=self.fill_pause)
        from app.services.sources.normalize import stable_hash

        manifest = self._manifest(request)
        return FillResult(manifest=manifest, fill_hash=stable_hash(manifest))

    def submit(self, request, *, expected_fill_hash, before_click) -> SubmitResult:
        from app.services.submission.types import AbortBeforeClick

        self.submits += 1
        if self.raise_before_click:
            raise RuntimeError("browser crashed before click")
        if self.before_hook:
            self.before_hook()
        try:
            before_click()
        except AbortBeforeClick as abort:
            return SubmitResult(outcome="aborted", reason=abort.reason)
        self.clicks += 1
        if self.raise_after_click:
            raise RuntimeError("browser crashed after click")
        if self.outcome == "verified":
            return SubmitResult(outcome="verified", confirmation_id="SBX-FAKE01", clicked=True)
        return SubmitResult(outcome="unverified", reason="no_confirmation", clicked=True)


def filled(client, monkeypatch, runner: FakeRunner, **kwargs):
    settings = configure(monkeypatch)
    setup_sandbox()
    application = approved_sandbox_application(client, **kwargs)
    response = start(client, application)
    assert response.status_code == 202, response.text
    run_tasks(settings, lambda: runner)
    return settings, application, current(client, application)


# --- gates ------------------------------------------------------------------------------------


def test_only_sandbox_jobs_can_be_submitted(client, monkeypatch):
    configure(monkeypatch)
    setup_sandbox()
    _, _, application = ready_application(client, "linkout-only@example.test")
    approve(client, application)
    assert application["submission_available"] is False
    refused = start(client, application)
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "destination_not_supported"


def test_every_kill_switch_and_approval_gate_refuses_start(client, monkeypatch):
    settings = configure(monkeypatch, enabled=False)
    setup_sandbox(enable_destination=False)
    application = approved_sandbox_application(client)
    assert start(client, application).json()["detail"]["code"] == "submission_disabled"
    configure(monkeypatch, enabled=True)
    assert start(client, application).json()["detail"]["code"] == "destination_disabled"
    toggle_destination(True)
    wrong = start(client, application, review_hash="0" * 64)
    assert wrong.json()["detail"]["code"] == "hash_mismatch"
    client.post(f"/api/v1/applications/{application['id']}/approval/revoke",
                headers=csrf_headers(client))
    assert start(client, application).json()["detail"]["code"] == "approval_required"
    del settings
    with TestingSessionLocal() as db:
        assert db.scalars(select(ApplicationSubmission)).all() == []
        assert db.scalars(select(Task).where(Task.kind.in_([TASK_FILL, TASK_SUBMIT]))).all() == []


def test_sandbox_origin_must_be_loopback():
    from app.core.config import Settings

    base = {"SESSION_SECRET": "x" * 40, "ARTIFACT_ENCRYPTION_KEY": "k", "APP_ENV": "test"}
    for origin in ("https://127.0.0.1:8100", "http://example.com:8100", "http://10.0.0.5:8100",
                   "http://127.0.0.1", "http://user@127.0.0.1:8100", "http://127.0.0.1:8100/x"):
        with pytest.raises(RuntimeError, match="SANDBOX_ATS_ORIGIN"):
            Settings(**base, SANDBOX_ATS_ORIGIN=origin).validate_runtime_secrets()


# --- happy path (fake runner) ------------------------------------------------------------------


def test_fill_review_confirm_submit_verify_and_track(client, monkeypatch):
    runner = FakeRunner()
    settings, application, submission = filled(client, monkeypatch, runner)
    assert submission["state"] == "final_review" and submission["can_confirm"]
    assert {f["question_key"] for f in submission["fields"]} >= {"full_name", "email"}
    request = runner.fills[0]
    assert request.url == f"{DEFAULT_ORIGIN}/jobs/sbx-data-analyst/apply"
    assert request.answers["work_authorization"]["value"] == "yes"
    assert not request.resume_path.exists()  # The decrypted CV is deleted after the run.
    # Idempotent start: same submission, no second task.
    again = start(client, application).json()
    assert again["id"] == submission["id"]

    mismatch = confirm(client, application, "f" * 64)
    assert mismatch.json()["detail"]["code"] == "fill_hash_mismatch"
    assert confirm(client, application, submission["fill_hash"]).status_code == 200
    assert confirm(client, application, submission["fill_hash"]).status_code == 200  # double
    run_tasks(settings, lambda: runner)
    done = current(client, application)
    assert done["state"] == "submitted" and done["confirmation_id"] == "SBX-FAKE01"
    assert done["verification"] == "confirmation_page" and done["submit_clicked_at"]
    assert runner.clicks == 1

    shown = client.get(f"/api/v1/applications/{application['id']}").json()
    assert shown["status"] == "applied"
    entries = client.get("/api/v1/tracker").json()["entries"]
    assert [(e["channel"], e["application_id"]) for e in entries] == [
        ("sandbox_adapter", application["id"])
    ]
    with TestingSessionLocal() as db:
        kinds = [t.kind for t in db.scalars(select(Task).where(
            Task.kind.in_([TASK_FILL, TASK_SUBMIT])))]
        events = [e.event_type for e in db.scalars(select(ApplicationEvent).where(
            ApplicationEvent.application_id == application["id"]).order_by(
            ApplicationEvent.occurred_at))]
        audits = json.dumps([a.metadata_json for a in db.scalars(select(AuditEvent))])
    assert sorted(kinds) == [TASK_FILL, TASK_SUBMIT]
    for expected in ("submission_started", "submission_filled", "submission_confirmed",
                     "submission_clicked", "submission_verified"):
        assert expected in events
    assert "Asha Example" not in audits  # Sent values never reach the audit log.


# --- kill switch, crashes, duplicates --------------------------------------------------------


def test_kill_switch_closed_just_before_click_prevents_the_click(client, monkeypatch):
    runner = FakeRunner(before_hook=lambda: toggle_destination(False))
    settings, application, submission = filled(client, monkeypatch, runner)
    confirm(client, application, submission["fill_hash"])
    run_tasks(settings, lambda: runner)
    result = current(client, application)
    assert result["state"] == "blocked" and result["error_code"] == "destination_disabled"
    assert runner.clicks == 0 and result["submit_clicked_at"] is None


def test_global_switch_off_after_confirm_blocks_before_the_browser_starts(client, monkeypatch):
    runner = FakeRunner()
    _, application, submission = filled(client, monkeypatch, runner)
    confirm(client, application, submission["fill_hash"])
    off = configure(monkeypatch, enabled=False)
    run_tasks(off, lambda: runner)
    assert current(client, application)["state"] == "blocked"
    assert runner.submits == 0


def test_redelivered_task_after_the_click_never_clicks_again(client, monkeypatch):
    runner = FakeRunner()
    settings, application, submission = filled(client, monkeypatch, runner)
    confirm(client, application, submission["fill_hash"])
    with TestingSessionLocal() as db:
        row = db.get(ApplicationSubmission, submission["id"])
        row.state = "submitting"
        row.submit_clicked_at = row.created_at  # As if the process died right after clicking.
        db.commit()
    run_tasks(settings, lambda: runner)
    result = current(client, application)
    assert result["state"] == "submitted_unverified" and result["error_code"] == (
        "interrupted_after_click"
    )
    assert runner.submits == 0 and result["can_resolve"]


def test_crash_after_click_is_unverified_and_before_click_is_failed(client, monkeypatch):
    after = FakeRunner(raise_after_click=True)
    settings, application, submission = filled(client, monkeypatch, after,
                                               email="crash-after@example.test")
    confirm(client, application, submission["fill_hash"])
    run_tasks(settings, lambda: after)
    assert current(client, application)["state"] == "submitted_unverified"
    assert after.clicks == 1

    client.post("/api/v1/auth/logout", headers=csrf_headers(client))
    before = FakeRunner(raise_before_click=True)
    register(client, "crash-before@example.test")
    create_profile(client, standard_facts())
    job_id = sandbox_job_id("sbx-captcha")  # Any sandbox job; the fake runner ignores it.
    second = prepare(client, job_id, ready_cv(client, job_id)).json()
    approve(client, second)
    start(client, second)
    run_tasks(settings, lambda: before)
    confirm(client, second, current(client, second)["fill_hash"])
    run_tasks(settings, lambda: before)
    failed = current(client, second)
    assert failed["state"] == "failed" and failed["error_code"] == "runner_error"
    assert failed["submit_clicked_at"] is None


def test_unverified_outcome_is_resolved_by_the_candidate_never_retried(client, monkeypatch):
    runner = FakeRunner(outcome="unverified")
    settings, application, submission = filled(client, monkeypatch, runner)
    confirm(client, application, submission["fill_hash"])
    run_tasks(settings, lambda: runner)
    assert current(client, application)["state"] == "submitted_unverified"
    assert run_tasks(settings, lambda: runner) == 0 and runner.clicks == 1
    assert action(client, application, "cancel").json()["detail"]["code"] == "too_late_to_cancel"
    resolved = action(client, application, "resolve", {"received": True}).json()
    assert resolved["state"] == "submitted" and resolved["verification"] == "candidate_reported"
    assert client.get("/api/v1/tracker").json()["entries"][0]["channel"] == "sandbox_adapter"


def test_pause_and_cancel_paths(client, monkeypatch):
    runner = FakeRunner(fill_pause="captcha")
    _, application, submission = filled(client, monkeypatch, runner)
    assert submission["state"] == "paused" and submission["pause_reason"] == "captcha"
    assert not submission["can_confirm"] and submission["can_cancel"]
    cancelled = action(client, application, "cancel").json()
    assert cancelled["state"] == "cancelled"
    # The same package is not submitted again; rebuilding gives a new package.
    assert start(client, application).json()["state"] == "cancelled"


def test_manual_mark_applied_is_refused_during_a_live_submission(client, monkeypatch):
    runner = FakeRunner()
    _, application, _ = filled(client, monkeypatch, runner)
    refused = client.post(f"/api/v1/applications/{application['id']}/applied",
                          headers=csrf_headers(client), json={})
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "submission_in_progress"


def test_export_includes_and_profile_deletion_removes_submissions(client, monkeypatch):
    runner = FakeRunner()
    settings, application, submission = filled(client, monkeypatch, runner)
    confirm(client, application, submission["fill_hash"])
    run_tasks(settings, lambda: runner)
    exported = client.get("/api/v1/profile/export").json()
    [sent] = exported["applications"][0]["submissions"]
    assert sent["state"] == "submitted" and sent["fill_manifest"]["fields"]
    assert client.delete("/api/v1/profile", headers=csrf_headers(client)).status_code == 200
    with TestingSessionLocal() as db:
        assert db.scalars(select(ApplicationSubmission)).all() == []
        assert db.scalars(select(TrackedApplication)).all() == []


def test_only_the_browser_module_imports_a_browser_library():
    submission_dir = BACKEND / "app" / "services" / "submission"
    for path in [*submission_dir.glob("*.py"), BACKEND / "app/api/routes/submissions.py",
                 BACKEND / "app/services/applications.py", BACKEND / "app/main.py"]:
        imported: set[str] = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        uses_browser = any(name.split(".")[0] in {"playwright", "selenium"} for name in imported)
        assert uses_browser == (path.name == "browser.py"), path
        if path.name != "browser.py":
            assert not any(name.split(".")[0] in {"httpx", "requests", "socket", "urllib3"}
                           for name in imported), path


# --- real browser against the sandbox ATS -------------------------------------------------


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as playwright:
            playwright.chromium.launch().close()
        return True
    except Exception:  # noqa: BLE001 - any launch failure means "not installed here".
        return False


requires_browser = pytest.mark.skipif(
    not _chromium_available(),
    reason="needs Playwright Chromium (python -m playwright install chromium)",
)


@pytest.fixture(scope="module")
def sandbox_server() -> Iterator[str]:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=port,
                                           log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    origin = f"http://127.0.0.1:{port}"
    yield origin
    server.should_exit = True
    thread.join(timeout=5)


def browser_runner():
    from app.services.submission.browser import PlaywrightRunner

    return PlaywrightRunner(confirmation_timeout_ms=1500)


@requires_browser
def test_real_browser_submits_exactly_the_reviewed_package(client, monkeypatch, sandbox_server):
    httpx.delete(f"{sandbox_server}/__sandbox/submissions")
    settings = configure(monkeypatch, origin=sandbox_server)
    setup_sandbox(origin=sandbox_server)
    application = approved_sandbox_application(client, email="browser-happy@example.test")
    assert start(client, application).status_code == 202
    run_tasks(settings, browser_runner)
    review = current(client, application)
    assert review["state"] == "final_review", review
    labels = {f["question_key"]: (f["label"], f["display_value"]) for f in review["fields"]}
    assert labels["full_name"] == ("Full name", "Asha Example")
    assert labels["work_authorization"][1] == "Yes"
    assert httpx.get(f"{sandbox_server}/__sandbox/submissions").json() == []  # Nothing sent yet.

    confirm(client, application, review["fill_hash"])
    run_tasks(settings, browser_runner)
    done = current(client, application)
    assert done["state"] == "submitted" and done["confirmation_id"].startswith("SBX-")
    [received] = httpx.get(f"{sandbox_server}/__sandbox/submissions").json()
    assert received["confirmation_id"] == done["confirmation_id"]
    assert received["fields"]["full_name"] == "Asha Example"
    assert received["fields"]["work_authorization"] == "yes"
    assert received["fields"]["sponsorship_required"] == "no"
    assert received["resume_sha256"] == application["package"]["content"]["cv"]["pdf_sha256"]
    assert received["fields"].get("salary_expectation", "") == ""  # Unanswered stays blank.


@requires_browser
@pytest.mark.parametrize(
    ("key", "reason"),
    [
        ("sbx-captcha", "captcha"),
        ("sbx-mfa", "mfa_or_login"),
        ("sbx-unknown-field", "unknown_required_field"),
        ("sbx-attestation", "attestation_required"),
        ("sbx-offsite", "offsite_navigation"),
        ("sbx-dialog", "dialog"),
    ],
)
def test_real_browser_pauses_and_never_bypasses(client, monkeypatch, sandbox_server, key, reason):
    httpx.delete(f"{sandbox_server}/__sandbox/submissions")
    settings = configure(monkeypatch, origin=sandbox_server)
    setup_sandbox(origin=sandbox_server)
    application = approved_sandbox_application(client, key=key)
    assert start(client, application).status_code == 202
    run_tasks(settings, browser_runner)
    paused = current(client, application)
    assert (paused["state"], paused["pause_reason"]) == ("paused", reason)
    assert httpx.get(f"{sandbox_server}/__sandbox/submissions").json() == []


@requires_browser
def test_real_browser_form_drift_pauses_before_clicking(client, monkeypatch, sandbox_server):
    httpx.delete(f"{sandbox_server}/__sandbox/submissions")
    settings = configure(monkeypatch, origin=sandbox_server)
    setup_sandbox(origin=sandbox_server)
    application = approved_sandbox_application(client, key="sbx-form-drift")
    assert start(client, application).status_code == 202
    run_tasks(settings, browser_runner)
    review = current(client, application)
    assert review["state"] == "final_review"
    confirm(client, application, review["fill_hash"])
    run_tasks(settings, browser_runner)
    result = current(client, application)
    assert (result["state"], result["pause_reason"]) == ("paused", "form_changed")
    assert result["submit_clicked_at"] is None
    assert httpx.get(f"{sandbox_server}/__sandbox/submissions").json() == []


@requires_browser
def test_real_browser_missing_confirmation_is_unverified(client, monkeypatch, sandbox_server):
    httpx.delete(f"{sandbox_server}/__sandbox/submissions")
    settings = configure(monkeypatch, origin=sandbox_server)
    setup_sandbox(origin=sandbox_server)
    application = approved_sandbox_application(client, key="sbx-no-confirmation")
    assert start(client, application).status_code == 202
    run_tasks(settings, browser_runner)
    confirm(client, application, current(client, application)["fill_hash"])
    run_tasks(settings, browser_runner)
    result = current(client, application)
    assert result["state"] == "submitted_unverified" and result["submit_clicked_at"]
    assert len(httpx.get(f"{sandbox_server}/__sandbox/submissions").json()) == 1
