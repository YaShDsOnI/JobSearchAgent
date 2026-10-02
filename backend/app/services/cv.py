from __future__ import annotations

import hashlib
import json
import re
import textwrap
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from io import BytesIO
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    CVClaim,
    CVVersion,
    JobAnalysis,
    JobEvidence,
    JobPosting,
    JobRequirement,
    JobSnapshot,
    MatchRun,
    ProfileFact,
    ProfileVersion,
    StoredArtifact,
)
from app.services.requirements import analyze_job_snapshot, canonical_skill
from app.services.storage import ArtifactStorage

GENERATOR_VERSION = "deterministic-cv-v1"
TEMPLATE_VERSION = "classic-v1"
_RISK_WORDS = {
    "achieved",
    "architect",
    "architected",
    "certified",
    "expert",
    "improved",
    "increased",
    "led",
    "managed",
    "owned",
    "reduced",
    "senior",
}
_STOP_WORDS = {
    "a",
    "and",
    "at",
    "by",
    "for",
    "in",
    "of",
    "on",
    "the",
    "to",
    "with",
    "years",
}
_FORMAT_WORDS = {"experience", "present"}


class CVInputError(ValueError):
    pass


class CVValidationError(ValueError):
    def __init__(self, message: str, *, rejected_claims: list[str] | None = None):
        super().__init__(message)
        self.rejected_claims = rejected_claims or []


@dataclass(frozen=True)
class DraftClaim:
    section: str
    text: str
    source_fact_ids: tuple[str, ...]
    job_requirement_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class DraftSection:
    heading: str
    claims: tuple[DraftClaim, ...]


@dataclass(frozen=True)
class CVDocument:
    title: str
    target_title: str
    target_employer: str
    target_location: str | None
    sections: tuple[DraftSection, ...]


@dataclass(frozen=True)
class JobCVContext:
    job: JobPosting
    snapshot: JobSnapshot
    analysis: JobAnalysis
    requirements: tuple[JobRequirement, ...]
    evidence: tuple[JobEvidence, ...]


def _fact_values(value: Any) -> list[str]:
    if isinstance(value, dict):
        result: list[str] = []
        for nested in value.values():
            result.extend(_fact_values(nested))
        return result
    if isinstance(value, list):
        result = []
        for nested in value:
            result.extend(_fact_values(nested))
        return result
    if value is None or isinstance(value, bool):
        return []
    return [str(value).strip()]


def _fact_text(fact: ProfileFact) -> str:
    return " ".join(item for item in _fact_values(fact.value_json) if item)


def _tokens(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", value.lower()))


def _numeric_tokens(value: str) -> set[str]:
    return set(re.findall(r"\b\d+(?:\.\d+)?\b", value))


def _fact_field(fact: ProfileFact, *names: str) -> Any:
    for name in names:
        value = fact.value_json.get(name)
        if value is not None:
            return value
    return None


def _display(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        values = [_display(item) for item in value]
        return ", ".join(item for item in values if item) or None
    if isinstance(value, dict):
        return None
    text = str(value).strip()
    return text or None


def assemble_fact_ledger(
    db: Session, *, user_id: str, profile_version_id: str
) -> list[ProfileFact]:
    profile = db.scalar(
        select(ProfileVersion).where(
            ProfileVersion.id == profile_version_id,
            ProfileVersion.user_id == user_id,
            ProfileVersion.status != "deleted",
        )
    )
    if profile is None:
        raise CVInputError("Profile version is unavailable")
    return list(
        db.scalars(
            select(ProfileFact)
            .where(
                ProfileFact.profile_version_id == profile.id,
                ProfileFact.confirmation_status == "confirmed",
            )
            .order_by(ProfileFact.created_at.asc(), ProfileFact.id.asc())
        ).all()
    )


def retrieve_job_evidence(
    db: Session, *, job_id: str, snapshot_id: str
) -> JobCVContext:
    job = db.get(JobPosting, job_id)
    snapshot = db.get(JobSnapshot, snapshot_id)
    if job is None:
        raise CVInputError("Job not found")
    if snapshot is None or snapshot.job_id != job.id:
        raise CVInputError("Job snapshot does not belong to job")
    analysis = analyze_job_snapshot(db, snapshot)
    requirements = tuple(
        db.scalars(
            select(JobRequirement)
            .where(JobRequirement.analysis_id == analysis.id)
            .order_by(JobRequirement.requirement_kind.asc(), JobRequirement.id.asc())
        ).all()
    )
    evidence = tuple(
        db.scalars(
            select(JobEvidence)
            .where(JobEvidence.analysis_id == analysis.id)
            .order_by(JobEvidence.id.asc())
        ).all()
    )
    return JobCVContext(job, snapshot, analysis, requirements, evidence)


def _fact_matches_requirement(fact: ProfileFact, requirement: JobRequirement) -> bool:
    if fact.fact_type not in {"skill", "experience"}:
        return False
    value = _fact_field(fact, "name", "skill", "role")
    if value is None:
        return False
    return canonical_skill(str(value)).lower() == canonical_skill(requirement.skill_text).lower()


def _format_experience(fact: ProfileFact) -> list[str]:
    title = _display(_fact_field(fact, "title", "role"))
    employer = _display(_fact_field(fact, "employer", "company", "organization"))
    start = _display(_fact_field(fact, "start_date", "start"))
    end = _display(_fact_field(fact, "end_date", "end")) or (
        "Present" if _fact_field(fact, "current") is True else None
    )
    dates = " – ".join(item for item in (start, end) if item)
    heading = " — ".join(item for item in (title, employer) if item)
    if dates:
        heading = f"{heading} ({dates})" if heading else dates
    if not heading:
        skill = _display(_fact_field(fact, "skill", "name"))
        years = _display(_fact_field(fact, "years", "years_experience"))
        heading = f"{skill} experience" if skill else "Verified experience"
        if years:
            heading = f"{heading} — {years} years"
    claims = [heading]
    for field in ("responsibilities", "achievements", "metrics", "description", "details"):
        value = _fact_field(fact, field)
        values = value if isinstance(value, list) else [value]
        claims.extend(item for item in (_display(nested) for nested in values) if item)
    return claims


def _format_general_fact(fact: ProfileFact) -> str | None:
    if fact.fact_type == "skill":
        return _display(_fact_field(fact, "name", "skill"))
    if fact.fact_type == "summary":
        return _display(_fact_field(fact, "text", "summary"))
    if fact.fact_type in {"education", "certification", "project"}:
        values = _fact_values(fact.value_json)
        return " — ".join(item for item in values if item) or None
    return None


def build_cv_document(
    *, facts: list[ProfileFact], context: JobCVContext
) -> tuple[CVDocument, list[str]]:
    requirements = [
        item for item in context.requirements if item.requirement_kind in {"required", "preferred"}
    ]
    claims: list[DraftClaim] = []
    unresolved: list[str] = []

    by_requirement: dict[str, list[ProfileFact]] = {}
    for requirement in requirements:
        matched = [fact for fact in facts if _fact_matches_requirement(fact, requirement)]
        by_requirement[requirement.id] = matched
        if requirement.requirement_kind == "required" and not matched:
            unresolved.append(
                f"Required skill not evidenced by a confirmed fact: {requirement.skill_text}"
            )
        if (
            requirement.requirement_kind == "required"
            and requirement.minimum_years is not None
            and matched
            and not any(
                isinstance(_fact_field(fact, "years", "years_experience"), (int, float))
                and float(_fact_field(fact, "years", "years_experience"))
                >= requirement.minimum_years
                for fact in matched
            )
        ):
            unresolved.append(
                f"Required experience not verified for {requirement.skill_text}: "
                f"{requirement.minimum_years:g} years"
            )

    summary_facts = [fact for fact in facts if fact.fact_type == "summary"]
    summary_claims = [
        DraftClaim("Summary", text, (fact.id,))
        for fact in summary_facts
        if (text := _format_general_fact(fact))
    ]
    if summary_claims:
        claims.extend(summary_claims[:1])

    relevant_skill_facts: list[tuple[ProfileFact, list[str]]] = []
    for fact in facts:
        if fact.fact_type != "skill":
            continue
        requirement_ids = [
            requirement.id
            for requirement in requirements
            if fact in by_requirement.get(requirement.id, [])
        ]
        if requirement_ids or not requirements:
            relevant_skill_facts.append((fact, requirement_ids))
    for fact, requirement_ids in relevant_skill_facts:
        if text := _format_general_fact(fact):
            claims.append(DraftClaim("Skills", text, (fact.id,), tuple(requirement_ids)))

    for fact in facts:
        if fact.fact_type != "experience":
            continue
        experience_requirement_ids = tuple(
            requirement.id
            for requirement in requirements
            if fact in by_requirement.get(requirement.id, [])
        )
        for text in _format_experience(fact):
            claims.append(
                DraftClaim("Experience", text, (fact.id,), experience_requirement_ids)
            )

    for fact_type, heading in (
        ("education", "Education"),
        ("certification", "Certifications"),
        ("project", "Projects"),
    ):
        for fact in facts:
            if fact.fact_type == fact_type and (text := _format_general_fact(fact)):
                claims.append(DraftClaim(heading, text, (fact.id,)))

    grouped: dict[str, list[DraftClaim]] = {}
    for claim in claims:
        grouped.setdefault(claim.section, []).append(claim)
    sections = tuple(
        DraftSection(section, tuple(section_claims))
        for section, section_claims in grouped.items()
    )
    return (
        CVDocument(
            title="Factual CV",
            target_title=context.job.title,
            target_employer=context.job.employer,
            target_location=context.job.location_text,
            sections=sections,
        ),
        unresolved,
    )


def validate_claims(claims: list[DraftClaim], facts: list[ProfileFact]) -> list[DraftClaim]:
    facts_by_id = {fact.id: fact for fact in facts}
    rejected: list[str] = []
    for claim in claims:
        if not claim.text.strip() or not claim.source_fact_ids:
            rejected.append(claim.text or "<empty claim>")
            continue
        source_facts = [facts_by_id.get(fact_id) for fact_id in claim.source_fact_ids]
        if any(fact is None or fact.confirmation_status != "confirmed" for fact in source_facts):
            rejected.append(claim.text)
            continue
        source_text = " ".join(_fact_text(fact) for fact in source_facts if fact is not None)
        claim_tokens = _tokens(claim.text)
        source_tokens = _tokens(source_text)
        unsupported_tokens = claim_tokens - source_tokens - _STOP_WORDS - _FORMAT_WORDS
        if unsupported_tokens or not claim_tokens.intersection(source_tokens):
            rejected.append(claim.text)
            continue
        if not _numeric_tokens(claim.text).issubset(_numeric_tokens(source_text)):
            rejected.append(claim.text)
            continue
        if any(word in claim_tokens and word not in source_tokens for word in _RISK_WORDS):
            rejected.append(claim.text)
    if rejected:
        raise CVValidationError(
            "One or more CV claims are unsupported by confirmed candidate facts",
            rejected_claims=rejected,
        )
    return claims


def _xml_paragraph(text: str, style: str | None = None) -> str:
    style_xml = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return f'<w:p>{style_xml}<w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>'


def render_docx(document: CVDocument) -> bytes:
    paragraphs = [_xml_paragraph(document.title, "Title")]
    target = " - ".join(item for item in (document.target_title, document.target_employer) if item)
    if document.target_location:
        target = f"{target} | {document.target_location}"
    # Keep the artifact header ASCII-safe for deterministic local rendering.
    target = target.replace("\ufffd", " | ")
    paragraphs.append(_xml_paragraph(target))
    for section in document.sections:
        paragraphs.append(_xml_paragraph(section.heading, "Heading1"))
        paragraphs.extend(_xml_paragraph(claim.text, "ListBullet") for claim in section.claims)
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f'<w:body>{"".join(paragraphs)}'
        '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/>'
        '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" '
        'w:header="720" w:footer="720" w:gutter="0"/></w:sectPr>'
        '</w:body></w:document>'
    ).encode()
    content_types = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        b'<Default Extension="rels" '
        b'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        b'<Default Extension="xml" ContentType="application/xml"/>'
        b'<Override PartName="/word/document.xml" '
        b'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        b'<Override PartName="/word/styles.xml" '
        b'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
        b'<Override PartName="/word/numbering.xml" '
        b'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>'
        b'</Types>'
    )
    rels = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        b'<Relationship Id="rId1" '
        b'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        b'relationships/officeDocument" '
        b'Target="word/document.xml"/>'
        b'</Relationships>'
    )
    document_rels = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        b'<Relationship Id="rId1" '
        b'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        b'Target="styles.xml"/>'
        b'<Relationship Id="rId2" '
        b'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" '
        b'Target="numbering.xml"/>'
        b'</Relationships>'
    )
    styles = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        b'<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        b'<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Arial" w:hAnsi="Arial"/>'
        b'<w:sz w:val="20"/></w:rPr></w:rPrDefault></w:docDefaults>'
        b'<w:style w:type="paragraph" w:default="1" w:styleId="Normal">'
        b'<w:name w:val="Normal"/><w:rPr><w:rFonts w:ascii="Arial" w:hAnsi="Arial"/>'
        b'<w:sz w:val="20"/></w:rPr></w:style>'
        b'<w:style w:type="paragraph" w:styleId="Title">'
        b'<w:name w:val="Title"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/>'
        b'<w:pPr><w:spacing w:after="240"/></w:pPr>'
        b'<w:rPr><w:b/><w:sz w:val="32"/></w:rPr></w:style>'
        b'<w:style w:type="paragraph" w:styleId="Heading1">'
        b'<w:name w:val="heading 1"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/>'
        b'<w:pPr><w:keepNext/><w:spacing w:before="180" w:after="80"/></w:pPr>'
        b'<w:rPr><w:b/><w:sz w:val="26"/></w:rPr></w:style>'
        b'<w:style w:type="paragraph" w:styleId="ListBullet">'
        b'<w:name w:val="List Bullet"/><w:basedOn w:val="Normal"/><w:next w:val="ListBullet"/>'
        b'<w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr>'
        b'<w:ind w:left="720" w:hanging="360"/></w:pPr></w:style>'
        b'</w:styles>'
    )
    numbering = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        b'<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        b'<w:abstractNum w:abstractNumId="0"><w:multiLevelType w:val="singleLevel"/>'
        b'<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="bullet"/>'
        b'<w:lvlText w:val="&#8226;"/><w:lvlJc w:val="left"/>'
        b'<w:pPr><w:ind w:left="720" w:hanging="360"/></w:pPr>'
        b'<w:rPr><w:rFonts w:ascii="Symbol" w:hAnsi="Symbol"/></w:rPr></w:lvl>'
        b'</w:abstractNum><w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num></w:numbering>'
    )
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for name, content in (
        ("[Content_Types].xml", content_types),
        ("_rels/.rels", rels),
        ("word/document.xml", xml),
        ("word/_rels/document.xml.rels", document_rels),
        ("word/styles.xml", styles),
        ("word/numbering.xml", numbering),
        ):
            info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            archive.writestr(info, content)
    return output.getvalue()


def _pdf_escape(value: str) -> str:
    return value.encode("latin-1", "replace").decode("latin-1").replace("\\", "\\\\").replace(
        "(", "\\("
    ).replace(")", "\\)")


def _document_lines(document: CVDocument) -> list[tuple[str, str]]:
    lines: list[tuple[str, str]] = [("bold", document.title)]
    target = " - ".join(item for item in (document.target_title, document.target_employer) if item)
    if document.target_location:
        target = f"{target} | {document.target_location}"
    # Keep the artifact header ASCII-safe for deterministic local rendering.
    target = target.replace("\ufffd", " | ")
    lines.append(("normal", target))
    for section in document.sections:
        lines.append(("bold", section.heading))
        for claim in section.claims:
            lines.append(("normal", f"- {claim.text}"))
    wrapped: list[tuple[str, str]] = []
    for style, line in lines:
        chunks = textwrap.wrap(line, width=92, break_long_words=False) or [""]
        wrapped.extend((style, chunk) for chunk in chunks)
    return wrapped


def render_pdf(document: CVDocument) -> bytes:
    lines = _document_lines(document)
    pages = [lines[index : index + 38] for index in range(0, len(lines), 38)] or [[]]
    objects: list[bytes | None] = [None]
    objects.extend([None, None])
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>")
    page_refs: list[int] = []
    for page_lines in pages:
        content_parts = ["BT"]
        first = True
        for style, line in page_lines:
            font = "F2" if style == "bold" else "F1"
            size = "14" if style == "bold" else "10"
            content_parts.append(f"/{font} {size} Tf")
            if first:
                content_parts.append("50 780 Td")
                first = False
            else:
                content_parts.append("0 -18 Td")
            content_parts.append(f"({_pdf_escape(line)}) Tj")
        content_parts.append("ET")
        stream = "\n".join(content_parts).encode("latin-1", "replace")
        page_number = len(objects)
        content_number = page_number + 1
        objects.append(None)
        objects.append(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream")
        page_refs.append(page_number)
        objects[page_number] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents {content_number} 0 R >>"
        ).encode()
    objects[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[2] = (
        f"<< /Type /Pages /Kids [{' '.join(f'{ref} 0 R' for ref in page_refs)}] "
        f"/Count {len(page_refs)} >>"
    ).encode()
    output = BytesIO()
    output.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, value in enumerate(objects[1:], start=1):
        if value is None:
            raise RuntimeError(f"PDF object {number} was not rendered")
        offsets.append(output.tell())
        output.write(f"{number} 0 obj\n".encode())
        output.write(value)
        output.write(b"\nendobj\n")
    xref_offset = output.tell()
    output.write(f"xref\n0 {len(objects)}\n".encode())
    output.write(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.write(f"{offset:010d} 00000 n \n".encode())
    output.write(
        f"trailer\n<< /Size {len(objects)} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n".encode()
    )
    return output.getvalue()


def extract_pdf_text(content: bytes) -> str:
    matches = re.findall(rb"\(((?:\\.|[^()])*)\) Tj", content)
    values = []
    for match in matches:
        value = match.decode("latin-1")
        value = value.replace("\\(", "(").replace("\\)", ")").replace("\\\\", "\\")
        values.append(value)
    return "\n".join(values)


def extract_docx_text(content: bytes) -> str:
    with ZipFile(BytesIO(content)) as archive:
        xml = archive.read("word/document.xml").decode("utf-8")
    return "\n".join(re.findall(r"<w:t[^>]*>(.*?)</w:t>", xml))


def _add_artifact(
    db: Session,
    *,
    user_id: str,
    storage: ArtifactStorage,
    content: bytes,
    kind: str,
    content_type: str,
) -> StoredArtifact:
    stored = storage.put_bytes(content, kind=kind, content_type=content_type)
    artifact = StoredArtifact(
        id=str(stored["id"]),
        owner_user_id=user_id,
        kind=kind,
        object_key=str(stored["object_key"]),
        content_type=content_type,
        size_bytes=int(stored["size_bytes"]),
        sha256=str(stored["sha256"]),
    )
    db.add(artifact)
    # Flush before a CVVersion references it: there is no ORM relationship to order the
    # inserts, and PostgreSQL enforces the foreign key.
    db.flush()
    return artifact


def create_cv_version(
    db: Session,
    *,
    user_id: str,
    job_id: str,
    match_run_id: str,
    storage: ArtifactStorage,
) -> CVVersion:
    match = db.scalar(
        select(MatchRun).where(MatchRun.id == match_run_id, MatchRun.user_id == user_id)
    )
    if match is None:
        raise CVInputError("Match not found")
    if match.eligibility_status != "eligible":
        raise CVInputError("A CV requires an eligible match")
    snapshot = db.get(JobSnapshot, match.job_snapshot_id)
    if snapshot is None or snapshot.job_id != job_id:
        raise CVInputError("Match does not belong to job")
    facts = assemble_fact_ledger(db, user_id=user_id, profile_version_id=match.profile_version_id)
    context = retrieve_job_evidence(db, job_id=job_id, snapshot_id=snapshot.id)
    document, unresolved = build_cv_document(facts=facts, context=context)
    claims = [claim for section in document.sections for claim in section.claims]
    validate_claims(claims, facts)
    docx_content = render_docx(document)
    pdf_content = render_pdf(document)
    rendered_pdf_text = extract_pdf_text(pdf_content)
    rendered_docx_text = extract_docx_text(docx_content)
    for claim in claims:
        if not all(token in _tokens(rendered_pdf_text) for token in _tokens(claim.text)):
            raise CVValidationError(
                "PDF rendering lost a validated claim", rejected_claims=[claim.text]
            )
        if not all(token in _tokens(rendered_docx_text) for token in _tokens(claim.text)):
            raise CVValidationError(
                "DOCX rendering lost a validated claim", rejected_claims=[claim.text]
            )
    docx_artifact = _add_artifact(
        db,
        user_id=user_id,
        storage=storage,
        content=docx_content,
        kind="cv-docx",
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    pdf_artifact = _add_artifact(
        db,
        user_id=user_id,
        storage=storage,
        content=pdf_content,
        kind="cv-pdf",
        content_type="application/pdf",
    )
    manifest = {
        "profile_version_id": match.profile_version_id,
        "job_snapshot_id": snapshot.id,
        "match_run_id": match.id,
        "preference_version_id": match.preference_version_id,
        "match_policy_version": match.policy_version,
        "extractor_version": match.extractor_version,
        "analysis_id": context.analysis.id,
        "fact_ids": [fact.id for fact in facts],
        "job_requirement_ids": [requirement.id for requirement in context.requirements],
        "job_evidence_ids": [evidence.id for evidence in context.evidence],
        "generator_version": GENERATOR_VERSION,
        "template_version": TEMPLATE_VERSION,
        "unresolved": unresolved,
    }
    content_hash = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        + b"\0"
        + docx_content
        + b"\0"
        + pdf_content
    ).hexdigest()
    version = CVVersion(
        user_id=user_id,
        job_id=job_id,
        job_snapshot_id=snapshot.id,
        profile_version_id=match.profile_version_id,
        match_run_id=match.id,
        generator_version=GENERATOR_VERSION,
        template_version=TEMPLATE_VERSION,
        content_hash=content_hash,
        docx_artifact_id=docx_artifact.id,
        pdf_artifact_id=pdf_artifact.id,
        status="validated",
        source_manifest_json=manifest,
    )
    db.add(version)
    db.flush()
    for claim in claims:
        db.add(
            CVClaim(
                cv_version_id=version.id,
                section=claim.section,
                text=claim.text,
                source_fact_ids=list(claim.source_fact_ids),
                job_requirement_ids=list(claim.job_requirement_ids),
                validation_status="accepted",
                validation_reason="Supported by confirmed candidate facts.",
            )
        )
    db.flush()
    return version


def approve_cv(db: Session, *, version: CVVersion, user_id: str) -> CVVersion:
    if version.user_id != user_id:
        raise CVInputError("CV version not found")
    if version.status != "validated":
        raise CVInputError("Only a validated CV can be approved")
    version.status = "approved"
    version.approved_by_user_id = user_id
    version.approved_at = datetime.now(UTC)
    db.flush()
    return version


def reject_cv(db: Session, *, version: CVVersion, user_id: str, note: str) -> CVVersion:
    if version.user_id != user_id:
        raise CVInputError("CV version not found")
    if version.status != "validated":
        raise CVInputError("Only a validated CV can be rejected")
    version.status = "rejected"
    version.rejected_at = datetime.now(UTC)
    version.rejection_note = note
    db.flush()
    return version
