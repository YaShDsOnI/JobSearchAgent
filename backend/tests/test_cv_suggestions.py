"""CV fact suggestions: isolated text extraction and deterministic, unconfirmed proposals."""

import io
import json
import subprocess
import zipfile

import pytest
from sqlalchemy import select

from app.db.models import AuditEvent
from app.services import cv_suggestions
from app.services.cv import CVDocument, DraftClaim, DraftSection, render_docx, render_pdf
from app.services.cv_suggestions import (
    DocumentTextError,
    extract_document_text,
    suggest_facts,
)
from tests.conftest import TestingSessionLocal, csrf_headers
from tests.test_phase2_matching import register

CV_TEXT = """Asha Example
asha.example@example.test | +91 98765 43210 | linkedin.com/in/asha-example
Summary
Data engineer with 5 years of experience with Python.
Skills: SQL, PostgreSQL, Apache Airflow, Docker
Experience
Sanitized Example Employer, 2019 - 2024
Education
B.Tech in Computer Science, Example Institute of Technology
Certifications
AWS Certified Solutions Architect
Personal
Date of birth: 01/01/1995
Visa status: authorized to work in India, Python and SQL
Expected salary: 30 LPA
Gender: female
"""

DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _facts(text: str) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for item in suggest_facts(text):
        grouped.setdefault(item.fact_type, []).append(item.value)
    return grouped


def _document(*lines: str) -> CVDocument:
    claims = tuple(DraftClaim(section="Profile", text=line, source_fact_ids=()) for line in lines)
    return CVDocument(title="Sanitized CV", target_title="Example Role",
                      target_employer="Example Employer", target_location=None,
                      sections=(DraftSection(heading="Profile", claims=claims),))


def _upload(client, content: bytes, content_type: str, name: str) -> str:
    response = client.post(
        "/api/v1/profile/source-document",
        headers=csrf_headers(client),
        files={"file": (name, content, content_type)},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _suggestions(client, artifact_id: str):
    return client.get(f"/api/v1/profile/source-documents/{artifact_id}/suggestions")


# --- rules --------------------------------------------------------------------------------


def test_suggestions_cover_contact_skills_experience_education_and_links():
    facts = _facts(CV_TEXT)
    assert {"email": "asha.example@example.test"} in facts["contact"]
    assert {"phone": "+91 98765 43210"} in facts["contact"]
    assert facts["link"] == [{"url": "linkedin.com/in/asha-example", "kind": "linkedin"}]
    names = [item["name"] for item in facts["skill"]]
    assert names[:5] == ["Python", "SQL", "PostgreSQL", "Apache Airflow", "Docker"]
    assert facts["experience"] == [{"skill": "Python", "years": 5}]
    assert facts["education"][0]["level"] == "bachelor"
    assert facts["certification"] == [{"name": "AWS Certified Solutions Architect"}]


def test_sensitive_and_eligibility_lines_are_never_suggested():
    suggestions = suggest_facts(CV_TEXT)
    evidence = " ".join(item.evidence_text for item in suggestions)
    for forbidden in ("Date of birth", "Visa", "salary", "Gender"):
        assert forbidden not in evidence
    assert all(item.fact_type != "work_authorization" for item in suggestions)
    # A year range is not a phone number.
    assert all("2019 - 2024" not in value.get("phone", "")
               for item in suggestions for value in [item.value])


def test_every_suggestion_quotes_its_line():
    for item in suggest_facts(CV_TEXT):
        assert item.evidence_text in CV_TEXT


# --- extraction ---------------------------------------------------------------------------


def test_pdf_and_docx_are_read_in_an_isolated_process():
    document = _document("Skilled in Kubernetes and Terraform.", "Contact: pdf@example.test")
    pdf_text, _ = extract_document_text(render_pdf(document), "application/pdf")
    assert "Kubernetes" in pdf_text and "pdf@example.test" in pdf_text
    docx_text, _ = extract_document_text(render_docx(document), DOCX_TYPE)
    assert "Terraform" in docx_text


def test_extraction_environment_holds_no_secrets(monkeypatch):
    captured: dict = {}

    def fake_run(argv, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"text": "x"}).encode())

    monkeypatch.setenv("SESSION_SECRET", "must-not-reach-the-parser")
    monkeypatch.setattr(cv_suggestions.subprocess, "run", fake_run)
    extract_document_text(b"%PDF-1.4", "application/pdf")
    assert "SESSION_SECRET" not in captured["env"]
    assert "ARTIFACT_ENCRYPTION_KEY" not in captured["env"]
    assert captured["timeout"] == cv_suggestions.EXTRACT_TIMEOUT_SECONDS


def test_malformed_and_hostile_documents_fail_closed(monkeypatch):
    with pytest.raises(DocumentTextError):
        extract_document_text(b"%PDF-1.4 not really a pdf", "application/pdf")
    bomb = io.BytesIO()
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", "<a>" + "x" * 11_000_000 + "</a>")
    with pytest.raises(DocumentTextError, match="docx_document_too_large"):
        extract_document_text(bomb.getvalue(), DOCX_TYPE)

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="parser", timeout=1)

    monkeypatch.setattr(cv_suggestions.subprocess, "run", timeout)
    with pytest.raises(DocumentTextError, match="extraction_timeout"):
        extract_document_text(b"%PDF-1.4", "application/pdf")


# --- API ----------------------------------------------------------------------------------


def test_suggestions_endpoint_returns_unconfirmed_facts_and_flags_known_ones(client):
    register(client, "suggest@example.test")
    artifact_id = _upload(client, CV_TEXT.encode(), "text/plain", "cv.txt")
    listed = client.get("/api/v1/profile/source-documents").json()
    assert [item["id"] for item in listed] == [artifact_id]

    body = _suggestions(client, artifact_id).json()
    assert body["suggester_version"] == "cv-suggest-v1"
    assert all(item["confirmation_status"] == "unknown" for item in body["suggestions"])
    assert all(item["provenance"] == "uploaded_cv" for item in body["suggestions"])
    assert not any(item["already_in_profile"] for item in body["suggestions"])

    client.post("/api/v1/profile/versions", headers=csrf_headers(client), json={
        "facts": [{"fact_type": "skill", "value": {"name": "python"}, "provenance": "candidate",
                   "confirmation_status": "confirmed"}],
        "source_document_id": artifact_id,
    })
    flagged = {
        item["value"].get("name"): item["already_in_profile"]
        for item in _suggestions(client, artifact_id).json()["suggestions"]
        if item["fact_type"] == "skill"
    }
    assert flagged["Python"] is True and flagged["SQL"] is False

    with TestingSessionLocal() as db:
        events = db.scalars(
            select(AuditEvent).where(AuditEvent.action == "profile.suggestions_generated")
        ).all()
        assert events and "asha" not in json.dumps([e.metadata_json for e in events]).lower()


def test_suggestions_are_owner_only_and_report_unreadable_documents(client):
    register(client, "suggest-owner@example.test")
    artifact_id = _upload(client, b"%PDF-1.4 broken", "application/pdf", "cv.pdf")
    response = _suggestions(client, artifact_id)
    assert response.status_code == 422
    client.post("/api/v1/auth/logout", headers=csrf_headers(client))
    register(client, "suggest-other@example.test")
    assert _suggestions(client, artifact_id).status_code == 404
    assert client.get("/api/v1/profile/source-documents").json() == []


def test_pdf_upload_produces_suggestions(client):
    register(client, "suggest-pdf@example.test")
    document = _document("Seven years of experience with Kubernetes.", "Skills: Go, Rust, Redis")
    artifact_id = _upload(client, render_pdf(document), "application/pdf", "cv.pdf")
    names = {
        item["value"].get("name")
        for item in _suggestions(client, artifact_id).json()["suggestions"]
        if item["fact_type"] == "skill"
    }
    assert {"Kubernetes", "Redis"} <= names
