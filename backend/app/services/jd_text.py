"""Deterministic requirement extraction from free job-description text (`rules-v2`).

Live providers (Ashby, Greenhouse, Lever) publish a description, not structured
requirements. This extractor finds known skills (`skills-v2` taxonomy) in the text and
classifies each one as required, preferred, or informational from the section it appears in
and the wording of its sentence. It also reads "N+ years" phrases next to a skill.

Rules:

- Every requirement carries its sentence as a verbatim evidence quote, so its offsets in
  the stored description can be checked. No sentence, no requirement.
- Only skills are extracted. Geography, work authorization, sponsorship, salary, and
  education are never read from prose here: those gate eligibility and stay `unknown`
  unless the provider states them in a structured field.
- A skill mentioned under responsibilities, benefits, or company background is
  informational and does not affect fit.
- Output is advisory input to fit scoring with confidence below 1. It never changes
  eligibility.
"""

import re
from dataclasses import dataclass

from app.services.skills_taxonomy import find_skills

TEXT_EXTRACTOR_CONFIDENCE = 0.7
MAX_QUOTE_LENGTH = 400
MAX_REQUIREMENTS = 60

_REQUIRED_HEADINGS = re.compile(
    r"^(?:requirements?|qualifications?|minimum qualifications?|basic qualifications?|"
    r"required(?: skills| qualifications| experience)?|must[- ]haves?|"
    r"what (?:you(?:'|’)ll|you will|you) (?:need|bring)|what we(?:'|’)re looking for|"
    r"who you are|you have|about you|skills(?: and experience)?|experience|"
    r"your (?:profile|background|skills)|key skills)\b",
    re.IGNORECASE,
)
_PREFERRED_HEADINGS = re.compile(
    r"^(?:preferred(?: qualifications| skills| experience)?|nice[- ]to[- ]haves?|"
    r"bonus(?: points)?|(?:it(?:'|’)s )?a plus|good to have|desired(?: skills)?|"
    r"additional qualifications|extra credit|even better if)\b",
    re.IGNORECASE,
)
_INFORMATIONAL_HEADINGS = re.compile(
    r"^(?:responsibilities|what you(?:'|’)ll do|what you will do|the role|about (?:the )?"
    r"(?:role|team|company|us)|who we are|benefits|perks|why join|our stack|tech stack|"
    r"compensation|location|how to apply|equal opportunity|day to day|in this role)\b",
    re.IGNORECASE,
)
_PREFERRED_CUES = re.compile(
    r"\b(?:preferred|nice to have|nice-to-have|a plus|is a plus|bonus|ideally|desirable|"
    r"good to have|familiarity with|exposure to)\b",
    re.IGNORECASE,
)
_REQUIRED_CUES = re.compile(
    r"\b(?:required|requires?|must|need(?:s|ed)? to|you have|you(?:'|’)ve|proficien(?:t|cy)|"
    r"strong (?:experience|knowledge|skills?)|experience (?:with|in|using|building)|"
    r"\d+\+? years?|solid|expert(?:ise)?|deep (?:knowledge|experience))\b",
    re.IGNORECASE,
)
_YEARS = re.compile(
    r"(\d{1,2})(?:\s*(?:\+|plus))?(?:\s*(?:-|–|to)\s*\d{1,2})?\s*\+?\s*(?:years?|yrs?)\b",
    re.IGNORECASE,
)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?;])\s+(?=[A-Z0-9(•\-*])|\n+")
_BULLET = re.compile(r"^\s*(?:[-*•·▪◦‣]|\d+[.)])\s*")


@dataclass(frozen=True)
class TextRequirement:
    skill: str
    kind: str  # required | preferred | informational
    evidence: str
    minimum_years: float | None
    confidence: float = TEXT_EXTRACTOR_CONFIDENCE


def _heading_kind(line: str) -> str | None:
    text = _BULLET.sub("", line).strip().strip(":").strip()
    # A heading is short and has no sentence-ending punctuation.
    if not text or len(text) > 60 or text.endswith((".", "!", "?")):
        return None
    if _PREFERRED_HEADINGS.match(text):
        return "preferred"
    if _INFORMATIONAL_HEADINGS.match(text):
        return "informational"
    if _REQUIRED_HEADINGS.match(text):
        return "required"
    return None


def _segments(description: str) -> list[tuple[str, str | None]]:
    """(verbatim sentence, section kind or None) in document order."""
    segments: list[tuple[str, str | None]] = []
    section: str | None = None
    for line in description.splitlines():
        if not line.strip():
            continue
        heading = _heading_kind(line)
        if heading is not None:
            section = heading
            # A heading line like "Requirements: Python, SQL" also carries content.
            if ":" in line and line.split(":", 1)[1].strip():
                segments.append((line.strip(), section))
            continue
        for sentence in _SENTENCE_SPLIT.split(line):
            sentence = sentence.strip()
            if sentence:
                segments.append((sentence, section))
    return segments


def _classify(sentence: str, section: str | None) -> str:
    if _PREFERRED_CUES.search(sentence):
        return "preferred"
    if section == "preferred":
        return "preferred"
    if section == "required":
        return "required"
    if section == "informational":
        return "informational"
    # No section heading: rely on the sentence's own wording.
    return "required" if _REQUIRED_CUES.search(sentence) else "informational"


def _years(sentence: str, skills: list[str]) -> float | None:
    # Years attach to a skill only when the sentence names few skills; "5+ years building
    # web apps with Python, Go, Java, and Rust" is not five years in each.
    if len(skills) > 2:
        return None
    match = _YEARS.search(sentence)
    if match is None:
        return None
    years = float(match.group(1))
    return years if 0 < years <= 40 else None


def extract_text_requirements(description: str) -> list[TextRequirement]:
    """Requirements found in free text, one per skill. When a skill appears more than once,
    the strongest kind wins (required > preferred > informational)."""
    rank = {"required": 3, "preferred": 2, "informational": 1}
    found: dict[str, TextRequirement] = {}
    for sentence, section in _segments(description):
        skills = find_skills(sentence)
        if not skills:
            continue
        kind = _classify(sentence, section)
        years = _years(sentence, skills) if kind != "informational" else None
        quote = sentence if len(sentence) <= MAX_QUOTE_LENGTH else None
        if quote is None:
            continue  # Too long to quote faithfully: skip rather than truncate evidence.
        for skill in skills:
            candidate = TextRequirement(
                skill=skill, kind=kind, evidence=quote, minimum_years=years
            )
            current = found.get(skill)
            if current is None or rank[kind] > rank[current.kind] or (
                rank[kind] == rank[current.kind]
                and years is not None
                and (current.minimum_years is None or years > current.minimum_years)
            ):
                found[skill] = candidate
    return list(found.values())[:MAX_REQUIREMENTS]
