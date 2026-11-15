"""Fact suggestions from the candidate's uploaded CV (`cv-suggest-v1`).

The candidate's own document is read into text and scanned with fixed, deterministic rules.
Every suggestion:

- is returned with `confirmation_status = "unknown"` and `provenance = "uploaded_cv"`, so
  it counts for nothing until the candidate confirms it in a new profile version;
- carries the line it came from as `evidence_text`;
- is derived again on each request and never stored.

Never suggested, whatever the document says: work authorization, visa or sponsorship
status, salary, date of birth, gender, nationality, religion, marital status, health, or
any other sensitive attribute. Those come only from the candidate's own explicit entry.

Text extraction runs in a separate interpreter (`app.services.document_text_worker`) with
a timeout and an environment holding no secrets.
"""

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.services.skills_taxonomy import find_skills

SUGGESTER_VERSION = "cv-suggest-v1"
EXTRACT_TIMEOUT_SECONDS = 20
MAX_EVIDENCE_CHARS = 300
MAX_SUGGESTIONS = 200
_BACKEND_ROOT = str(Path(__file__).resolve().parents[2])
_BOOTSTRAP = (
    "import sys; sys.path.insert(0, sys.argv.pop(1)); "
    "from app.services.document_text_worker import main; raise SystemExit(main())"
)

_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})+")
_PHONE = re.compile(r"(?<![\w+])(\+?\d[\d ()./-]{6,18}\d)(?!\w)")
_URL = re.compile(r"https?://[^\s<>()\"']{3,200}|(?:www\.)?(?:linkedin|github)\.com/[^\s<>()\"']{2,120}")
_YEAR_RANGE = re.compile(r"(?:19|20)\d{2}\D+(?:19|20)\d{2}")
_YEARS = re.compile(r"(\d{1,2})\+?\s*(?:years?|yrs?)\b", re.IGNORECASE)
_DEGREE_LEVELS = (
    ("doctorate", re.compile(r"\b(?:ph\.?\s?d|doctorate|doctor of)\b", re.IGNORECASE)),
    ("master", re.compile(
        r"\b(?:master(?:'s|s)?|m\.?\s?tech|m\.?\s?sc|m\.?\s?s\.?|mba|m\.?\s?e\.?|m\.?\s?a\.?|"
        r"mca|m\.?\s?com)\b", re.IGNORECASE)),
    ("bachelor", re.compile(
        r"\b(?:bachelor(?:'s|s)?|b\.?\s?tech|b\.?\s?sc|b\.?\s?e\.?|b\.?\s?a\.?|bca|b\.?\s?com|"
        r"b\.?\s?s\.?)\b", re.IGNORECASE)),
    ("diploma", re.compile(r"\bdiploma\b", re.IGNORECASE)),
)
_EDUCATION_CONTEXT = re.compile(
    r"\b(?:university|college|institute|school|iit|nit|degree|graduat|engineering|science|"
    r"commerce|arts|computer|technology)\w*", re.IGNORECASE
)
_CERTIFICATION = re.compile(r"\b(?:certified|certification|certificate)\b", re.IGNORECASE)
# Lines that state sensitive or eligibility facts are skipped entirely.
_SENSITIVE = re.compile(
    r"\b(?:visa|work permit|authori[sz]ed to work|sponsorship|citizenship|citizen|nationality|"
    r"passport|date of birth|d\.?o\.?b|born|gender|male|female|marital|married|religion|caste|"
    r"disabilit|salary|ctc|compensation|expected pay)\w*",
    re.IGNORECASE,
)


class DocumentTextError(Exception):
    """The document could not be read as text. The message is a short code only."""


@dataclass(frozen=True)
class Suggestion:
    fact_type: str
    value: dict[str, Any]
    evidence_text: str

    def key(self) -> str:
        return json.dumps([self.fact_type, self.value], sort_keys=True).lower()


def extract_document_text(content: bytes, content_type: str) -> tuple[str, list[str]]:
    """Plain text and warning codes for an uploaded CV."""
    if content_type == "text/plain":
        text = content.decode("utf-8", errors="replace")
        return text, (["text_decoding_replaced"] if "�" in text else [])
    environment = {
        name: os.environ[name]
        for name in ("PATH", "SYSTEMROOT", "TEMP", "TMP", "LANG")
        if name in os.environ
    }
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [sys.executable, "-I", "-c", _BOOTSTRAP, _BACKEND_ROOT, content_type],
            input=content,
            capture_output=True,
            timeout=EXTRACT_TIMEOUT_SECONDS,
            env=environment,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise DocumentTextError("extraction_timeout") from exc
    try:
        result = json.loads(completed.stdout.decode("utf-8") or "{}")
    except (UnicodeDecodeError, ValueError) as exc:
        raise DocumentTextError("extraction_failed") from exc
    if completed.returncode != 0 or "error" in result:
        raise DocumentTextError(str(result.get("error", "extraction_failed"))[:64])
    return str(result.get("text", "")), [str(item) for item in result.get("warnings", [])]


def _lines(text: str) -> list[str]:
    return [
        re.sub(r"[ \t]+", " ", line).strip(" \t•·▪◦‣-*|")
        for line in text.splitlines()
        if line.strip()
    ]


def _evidence(line: str) -> str:
    return line if len(line) <= MAX_EVIDENCE_CHARS else line[: MAX_EVIDENCE_CHARS - 1] + "…"


def suggest_facts(text: str) -> list[Suggestion]:
    """Deterministic fact suggestions from CV text, in document order without repeats."""
    found: list[Suggestion] = []
    seen: set[str] = set()

    def add(suggestion: Suggestion) -> None:
        key = suggestion.key()
        if key not in seen and len(found) < MAX_SUGGESTIONS:
            seen.add(key)
            found.append(suggestion)

    for line in _lines(text):
        if _SENSITIVE.search(line):
            continue
        evidence = _evidence(line)
        for email in _EMAIL.findall(line):
            add(Suggestion("contact", {"email": email.lower()}, evidence))
        for phone in _PHONE.findall(line):
            digits = re.sub(r"\D", "", phone)
            # A plausible phone number, not a year range or a date.
            if 8 <= len(digits) <= 15 and not _YEAR_RANGE.fullmatch(phone):
                add(Suggestion("contact", {"phone": phone.strip()}, evidence))
        for url in _URL.findall(line):
            url = url.rstrip(".,;")
            kind = next((name for name in ("linkedin", "github") if f"{name}.com" in url), "web")
            add(Suggestion("link", {"url": url, "kind": kind}, evidence))
        skills = find_skills(line)
        for skill in skills:
            add(Suggestion("skill", {"name": skill}, evidence))
        years = _YEARS.search(line)
        if years and 1 <= len(skills) <= 2 and "experience" in line.lower():
            for skill in skills:
                add(Suggestion("experience", {"skill": skill, "years": int(years.group(1))},
                               evidence))
        if _EDUCATION_CONTEXT.search(line):
            for level, pattern in _DEGREE_LEVELS:
                if pattern.search(line):
                    add(Suggestion("education", {"level": level, "text": evidence}, evidence))
                    break
        if _CERTIFICATION.search(line) and len(line) <= 160:
            add(Suggestion("certification", {"name": evidence}, evidence))
    return found


def suggestion_payload(
    suggestions: list[Suggestion], existing: list[tuple[str, dict[str, Any]]]
) -> list[dict[str, Any]]:
    """API shape: each suggestion as a draft fact, flagged when the profile already has it."""
    known = {
        json.dumps([fact_type, value], sort_keys=True).lower() for fact_type, value in existing
    }
    known_skills = {
        str(value.get("name", "")).lower() for fact_type, value in existing if fact_type == "skill"
    }
    payload = []
    for item in suggestions:
        already = item.key() in known or (
            item.fact_type == "skill" and str(item.value.get("name", "")).lower() in known_skills
        )
        payload.append(
            {
                "fact_type": item.fact_type,
                "value": item.value,
                "provenance": "uploaded_cv",
                "confirmation_status": "unknown",
                "evidence_text": item.evidence_text,
                "already_in_profile": already,
            }
        )
    return payload
