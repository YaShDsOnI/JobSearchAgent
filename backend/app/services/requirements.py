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
from app.services.jd_text import TEXT_EXTRACTOR_CONFIDENCE, extract_text_requirements
from app.services.skills_taxonomy import TAXONOMY_VERSION, canonical_name

# rules-v2: structured requirements when the provider supplies them, otherwise skills read
# from the description text (`app.services.jd_text`). rules-v1 analyses stay as recorded.
EXTRACTOR_VERSION = "rules-v2"
# With MODEL_EXTRACTION_ENABLED, descriptions without structured requirements are first sent
# to the model (`app.services.model_extraction`); rules-v2 text extraction is the fallback.
MODEL_ASSISTED_EXTRACTOR_VERSION = "rules-v2+model-assisted-v1"
__all__ = [
    "EXTRACTOR_VERSION",
    "MODEL_ASSISTED_EXTRACTOR_VERSION",
    "TAXONOMY_VERSION",
    "active_extractor_version",
    "analyze_job_snapshot",
    "canonical_skill",
]


def active_extractor_version() -> str:
    from app.core.config import get_settings

    if get_settings().model_extraction_enabled:
        return MODEL_ASSISTED_EXTRACTOR_VERSION
    return EXTRACTOR_VERSION

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
    return canonical_name(normalized) or SKILL_ALIASES.get(normalized, value.strip())


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
    db: Session, snapshot: JobSnapshot, *, extractor_version: str | None = None
) -> JobAnalysis:
    extractor_version = extractor_version or active_extractor_version()
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
        "requirements_source": "structured" if requirements else "none",
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
    if not requirements:
        if extractor_version == MODEL_ASSISTED_EXTRACTOR_VERSION and _add_model_requirements(
            db, analysis, snapshot, source_text
        ):
            db.flush()
            return analysis
        _add_text_requirements(db, analysis, source_text)
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


def _add_text_requirements(db: Session, analysis: JobAnalysis, source_text: str) -> None:
    """Skills read from the description when the provider gives no structured list. Each
    requirement keeps its sentence as verbatim evidence; one that cannot be located in the
    text is dropped, never stored without evidence."""
    added = 0
    for index, item in enumerate(extract_text_requirements(source_text)):
        evidence = _add_evidence(
            db,
            analysis,
            field_path=f"requirements_text[{index}].evidence",
            quote=item.evidence,
            source_text=source_text,
            confidence=item.confidence,
        )
        if evidence.start_offset is None:
            db.delete(evidence)
            continue
        db.add(
            JobRequirement(
                analysis_id=analysis.id,
                skill_id=_get_or_create_skill(db, item.skill).id,
                skill_text=item.skill,
                requirement_kind=item.kind,
                importance=1.0 if item.kind == "required" else 0.5,
                minimum_years=item.minimum_years,
                evidence_id=evidence.id,
            )
        )
        added += 1
    if added:
        analysis.structured_json = {
            **analysis.structured_json,
            "requirements_source": "text",
        }
        analysis.confidence = TEXT_EXTRACTOR_CONFIDENCE


def _add_model_requirements(
    db: Session, analysis: JobAnalysis, snapshot: JobSnapshot, source_text: str
) -> bool:
    """Model-proposed requirements that passed validation; False means use the rules."""
    from app.core.config import get_settings
    from app.services.model_extraction import MODEL_CONFIDENCE, extract_requirements

    result = extract_requirements(db, get_settings(), source_text, snapshot_id=snapshot.id)
    analysis.structured_json = {
        **analysis.structured_json,
        "model_status": result.status,
        "model": result.model,
        "model_rejected": result.rejected,
    }
    if result.status != "ok" or not result.requirements:
        return False
    for index, item in enumerate(result.requirements):
        evidence = _add_evidence(
            db,
            analysis,
            field_path=f"requirements_model[{index}].evidence",
            quote=item.evidence,
            source_text=source_text,
            confidence=item.confidence,
        )
        if evidence.start_offset is None:  # Validation already checked; keep the invariant.
            db.delete(evidence)
            continue
        db.add(
            JobRequirement(
                analysis_id=analysis.id,
                skill_id=_get_or_create_skill(db, item.skill).id,
                skill_text=canonical_skill(item.skill),
                requirement_kind=item.kind,
                importance=1.0 if item.kind == "required" else 0.5,
                minimum_years=item.minimum_years,
                evidence_id=evidence.id,
            )
        )
    analysis.structured_json = {**analysis.structured_json, "requirements_source": "model"}
    analysis.confidence = MODEL_CONFIDENCE
    return True
