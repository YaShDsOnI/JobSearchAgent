"""The sandbox ATS web app. NOT a real employer: nothing here goes anywhere else.

Each job key selects a form variant so every pause and failure path of the adapter can be
exercised: CAPTCHA, MFA/login, unknown required field, attestation, off-site navigation,
JavaScript dialog, form drift, missing confirmation, and a server error after submit.
"""

import hashlib
import html
import secrets
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

BANNER = (
    '<div class="banner" role="note">SANDBOX ATS · not a real employer · '
    "submissions stay on this computer</div>"
)
STYLE = """<style>
body{font-family:system-ui,sans-serif;max-width:640px;margin:2rem auto;padding:0 1rem;color:#1d2a30}
.banner{background:#fff4d6;border:1px solid #e6c46b;border-radius:8px;padding:.5rem .75rem;margin-bottom:1rem;font-weight:600}
label{display:block;margin:.75rem 0 .25rem;font-weight:600}input,select{width:100%;padding:.45rem}
button{margin-top:1rem;padding:.6rem 1rem}</style>"""

JOBS: dict[str, dict[str, str]] = {
    "sbx-data-analyst": {"title": "Sandbox Data Analyst", "variant": "standard"},
    "sbx-captcha": {"title": "Sandbox Analyst (CAPTCHA)", "variant": "captcha"},
    "sbx-mfa": {"title": "Sandbox Analyst (login wall)", "variant": "mfa"},
    "sbx-unknown-field": {"title": "Sandbox Analyst (extra question)", "variant": "unknown_field"},
    "sbx-attestation": {"title": "Sandbox Analyst (attestation)", "variant": "attestation"},
    "sbx-offsite": {"title": "Sandbox Analyst (off-site redirect)", "variant": "offsite"},
    "sbx-dialog": {"title": "Sandbox Analyst (dialog)", "variant": "dialog"},
    "sbx-form-drift": {"title": "Sandbox Analyst (changing form)", "variant": "form_drift"},
    "sbx-no-confirmation": {"title": "Sandbox Analyst (no confirmation)", "variant": "no_confirmation"},
    "sbx-server-error": {"title": "Sandbox Analyst (server error)", "variant": "server_error"},
}

YES_NO = '<option value="">Choose…</option><option value="yes">Yes</option><option value="no">No</option>'
REQUIRED_FIELDS = ("full_name", "email", "work_authorization", "sponsorship_required")


@dataclass
class SandboxState:
    submissions: list[dict[str, Any]] = field(default_factory=list)
    renders: dict[str, int] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)


def create_app() -> FastAPI:
    app = FastAPI(title="Sandbox ATS (not a real employer)", docs_url=None, redoc_url=None)
    state = SandboxState()
    app.state.sandbox = state

    def job_or_404(key: str) -> dict[str, str]:
        job = JOBS.get(key)
        if job is None:
            raise HTTPException(status_code=404, detail="Unknown sandbox job")
        return job

    def page(title: str, body: str) -> HTMLResponse:
        return HTMLResponse(
            f"<!doctype html><html lang=en><head><meta charset=utf-8><title>{html.escape(title)}"
            f"</title>{STYLE}</head><body>{BANNER}{body}</body></html>"
        )

    @app.get("/jobs/{key}", response_class=HTMLResponse)
    def job_page(key: str) -> HTMLResponse:
        job = job_or_404(key)
        return page(job["title"], f'<h1>{html.escape(job["title"])}</h1>'
                    f'<p>Sandbox Employer (not real)</p><a href="/jobs/{key}/apply">Apply</a>')

    @app.get("/jobs/{key}/apply", response_class=HTMLResponse)
    def apply_form(key: str, request: Request):
        job = job_or_404(key)
        variant = job["variant"]
        if variant == "mfa" and request.cookies.get("sandbox_session") is None:
            return RedirectResponse(f"/login?next=/jobs/{key}/apply", status_code=302)
        with state.lock:
            renders = state.renders[key] = state.renders.get(key, 0) + 1
        name_label = "Full name"
        if variant == "form_drift" and renders % 2 == 0:
            name_label = "Legal full name (as on passport)"
        extra = ""
        head_script = ""
        if variant == "captcha":
            extra = '<div class="g-recaptcha" data-captcha data-sitekey="sandbox"></div>'
        elif variant == "unknown_field":
            extra = ('<label for="favourite_team">Which team would you join?</label>'
                     '<input id="favourite_team" name="favourite_team" required>')
        elif variant == "attestation":
            extra = ('<label><input type="checkbox" name="attest" data-attestation required> '
                     "I certify that everything above is true</label>")
        elif variant == "offsite":
            head_script = '<script>location.href="https://collector.invalid/steal"</script>'
        elif variant == "dialog":
            head_script = '<script>window.addEventListener("load",()=>alert("Session expiring"))</script>'
        form = f"""
<h1>Apply: {html.escape(job["title"])}</h1>
<form id="application-form" method="post" action="/jobs/{key}/apply" enctype="multipart/form-data">
<label for="full_name">{html.escape(name_label)}</label><input id="full_name" name="full_name" required>
<label for="email">Email address</label><input id="email" name="email" type="email" required>
<label for="phone">Phone number</label><input id="phone" name="phone" type="tel">
<label for="current_location">Current location</label><input id="current_location" name="current_location">
<label for="work_authorization">Are you legally authorized to work in this country?</label>
<select id="work_authorization" name="work_authorization" required>{YES_NO}</select>
<label for="sponsorship_required">Will you require visa sponsorship?</label>
<select id="sponsorship_required" name="sponsorship_required" required>{YES_NO}</select>
<label for="notice_period">Notice period</label><input id="notice_period" name="notice_period">
<label for="relocation">Willing to relocate?</label><select id="relocation" name="relocation">{YES_NO}</select>
<label for="salary_expectation">Salary expectation</label><input id="salary_expectation" name="salary_expectation">
<label for="resume">CV (PDF)</label><input id="resume" name="resume" type="file" accept="application/pdf" required>
{extra}
<button id="submit-application" type="submit">Submit application</button>
</form>"""
        response = page(job["title"], head_script + form)
        return response

    @app.get("/login", response_class=HTMLResponse)
    def login() -> HTMLResponse:
        return page("Sign in", '<h1>Sign in to continue</h1><form><label for="u">Email</label>'
                    '<input id="u" name="u"><label for="p">Password</label>'
                    '<input id="p" name="p" type="password"><label for="otp">One-time code</label>'
                    '<input id="otp" name="otp" autocomplete="one-time-code"></form>')

    @app.post("/jobs/{key}/apply", response_class=HTMLResponse)
    async def receive(key: str, request: Request):
        job = job_or_404(key)
        form = await request.form()
        missing = [name for name in REQUIRED_FIELDS if not str(form.get(name) or "").strip()]
        resume = form.get("resume")
        if missing or resume is None or not hasattr(resume, "read"):
            return page("Missing information", "<h1>Please complete every required field</h1>")
        content = await resume.read()
        fields = {
            name: str(value)
            for name, value in form.multi_items()
            if name != "resume" and not hasattr(value, "read")
        }
        confirmation = f"SBX-{secrets.token_hex(4).upper()}"
        record = {
            "confirmation_id": confirmation,
            "job_key": key,
            "fields": fields,
            "resume_filename": getattr(resume, "filename", None),
            "resume_sha256": hashlib.sha256(content).hexdigest(),
            "resume_bytes": len(content),
            "received_at": datetime.now(UTC).isoformat(),
        }
        with state.lock:
            state.submissions.append(record)
        if job["variant"] == "server_error":
            return HTMLResponse("<h1>Something went wrong</h1>", status_code=500)
        if job["variant"] == "no_confirmation":
            return page("Processing", "<h1>We are processing your application</h1>")
        return page("Application received",
                    f'<h1>Thank you, your application was received</h1>'
                    f'<p>Reference: <strong data-confirmation-id="{confirmation}">'
                    f"{confirmation}</strong></p>")

    @app.get("/__sandbox/submissions")
    def submissions() -> JSONResponse:
        with state.lock:
            return JSONResponse(list(state.submissions))

    @app.delete("/__sandbox/submissions")
    def reset() -> JSONResponse:
        with state.lock:
            state.submissions.clear()
            state.renders.clear()
        return JSONResponse({"cleared": True})

    return app


app = create_app()
