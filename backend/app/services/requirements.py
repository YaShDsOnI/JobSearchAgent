import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    JobAnalysis,
    JobEvidence,
    JobRequirement,
    JobSnapshot,
    Skill,
)

EXTRACTOR_VERSION = "rules-v1"
TAXONOMY_VERSION = "skills-v1"

SKILL_ALIASES = {
    "postgres": "PostgreSQL",
    "postgresql": "PostgreSQL",
    "py": "Python",
    "python": "Python",
    "js": "JavaScript",
    "javascript": "JavaScript",
    "ts": "TypeScript",
    "typescript": "TypeScript",
    "k8s": "Kubernetes",
    "kubernetes": "Kubernetes",
    "aws": "AWS",
}


def canonical_skill(value: str) -> str:
    normalized = re.sub(r"\s+", " ", value.strip().lower())
    return SKILL_ALIASES.get(normalized, value.strip())


def _get_or_create_skill(db: Session, value: str) -> Skill:
    canonical_name = canonical_skill(value)
    skill = db.scalar(
        select(Skill).where(
            Skill.canonical_name == canonical_name,
            Skill.taxonomy_version == TAXONOMY_VERSION,
        )
    )
    if skill:
        return skill
    skill = Skill(canonical_name=canonical_name, taxonomy_version=TAXONOMY_VERSION)
    db.add(skill)
    db.flush()
    return skill


def _add_evidence(
    db: Session,
    analysis: JobAnalysis,
    *,
    field_path: str,
    quote: str,
    source_text: str,
    confidence: float,
) -> JobEvidence:
    start = source_text.find(quote) if quote else -1
    end = start + len(quote) if start >= 0 else None
    evidence = JobEvidence(
        analysis_id=analysis.id,
        field_path=field_path,
        quote=quote,
        start_offset=start if start >= 0 else None,
        end_offset=end,
        confidence=confidence,
    )
    db.add(evidence)
    db.flush()
    return evidence


def analyze_job_snapshot(
    db: Session, snapshot: JobSnapshot, *, extractor_version: str = EXTRACTOR_VERSION
) -> JobAnalysis:
    existing = db.scalar(
        select(JobAnalysis).where(
            JobAnalysis.job_snapshot_id == snapshot.id,
            JobAnalysis.extractor_version == extractor_version,
        )
    )
    if existing:
        return existing

    record = snapshot.normalized_json
    source_text = str(record.get("description", ""))
    requirements = record.get("requirements", [])
    status = "validated"
    structured: dict[str, Any] = {
        "title": record.get("title"),
        "work_mode": record.get("work_mode", "unknown"),
        "location_text": record.get("location_text"),
        "location_country": record.get("location_country"),
        "permitted_countries": record.get("permitted_countries", []),
        "sponsorship": record.get("sponsorship", "unknown"),
        "minimum_experience_years": record.get("minimum_experience_years"),
        "education": record.get("education", []),
        "compensation": record.get("compensation"),
        "job_status": record.get("status", "active"),
    }
    analysis = JobAnalysis(
        job_snapshot_id=snapshot.id,
        extractor_version=extractor_version,
        structured_json=structured,
        confidence=1.0,
        status=status,
    )
    db.add(analysis)
    db.flush()

    for field_path, value in (
        ("location_text", record.get("location_text")),
        ("location_country", record.get("location_country")),
        ("permitted_countries", record.get("permitted_countries")),
        ("sponsorship", record.get("sponsorship")),
        ("work_mode", record.get("work_mode", "unknown")),
    ):
        if value not in (None, "", []):
            _add_evidence(
                db,
                analysis,
                field_path=field_path,
                quote=str(value),
                source_text=source_text,
                confidence=1.0,
            )

    if not isinstance(requirements, list):
        analysis.status = "needs_review"
        db.flush()
        return analysis

    for index, item in enumerate(requirements):
        if not isinstance(item, dict):
            analysis.status = "needs_review"
            continue
        skill_text = str(item.get("skill", "")).strip()
        kind = str(item.get("kind", "informational"))
        quote = str(item.get("evidence", "")).strip()
        if not skill_text or kind not in {"required", "preferred", "informational"} or not quote:
            analysis.status = "needs_review"
            continue
        evidence = _add_evidence(
            db,
            analysis,
            field_path=f"requirements[{index}].evidence",
            quote=quote,
            source_text=source_text,
            confidence=1.0,
        )
        if evidence.start_offset is None:
            analysis.status = "needs_review"
            continue
        db.add(
            JobRequirement(
                analysis_id=analysis.id,
                skill_id=_get_or_create_skill(db, skill_text).id,
                skill_text=canonical_skill(skill_text),
                requirement_kind=kind,
                importance=float(item.get("importance", 1.0)),
                minimum_years=(
                    float(item["minimum_years"])
                    if item.get("minimum_years") is not None
                    else None
                ),
                evidence_id=evidence.id,
            )
        )
    db.flush()
    return analysis
