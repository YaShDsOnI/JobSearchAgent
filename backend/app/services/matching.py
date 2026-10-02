from __future__ import annotations

from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.db.models import (
    CandidateProfile,
    JobEvidence,
    JobPosting,
    JobRequirement,
    JobSnapshot,
    MatchRun,
    PreferenceVersion,
    ProfileFact,
    ProfileVersion,
)
from app.services.requirements import analyze_job_snapshot, canonical_skill

POLICY_VERSION = "deterministic-v1"


class MatchInputError(ValueError):
    pass


def _fact_value(fact: ProfileFact, *keys: str) -> Any:
    for key in keys:
        if key in fact.value_json:
            return fact.value_json[key]
    return None


def _confirmed_facts(db: Session, profile_version_id: str) -> list[ProfileFact]:
    return list(
        db.scalars(
            select(ProfileFact).where(
                ProfileFact.profile_version_id == profile_version_id,
                ProfileFact.confirmation_status == "confirmed",
            )
        ).all()
    )


def _reason(
    code: str,
    message: str,
    source: str,
    *,
    facts: list[str] | None = None,
    evidence: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "source": source,
        "candidate_fact_ids": facts or [],
        "job_evidence_ids": evidence or [],
    }


def _candidate_authorization(
    facts: list[ProfileFact],
) -> tuple[set[str], str | None, bool | None, list[str]]:
    countries: set[str] = set()
    status: str | None = None
    sponsorship_needed: bool | None = None
    fact_ids: list[str] = []
    for fact in facts:
        if fact.fact_type != "work_authorization":
            continue
        fact_ids.append(fact.id)
        raw_countries = _fact_value(fact, "countries", "country")
        if isinstance(raw_countries, str):
            countries.add(raw_countries.upper())
        elif isinstance(raw_countries, list):
            countries.update(str(item).upper() for item in raw_countries)
        raw_status = _fact_value(fact, "status")
        if raw_status is not None:
            status = str(raw_status).lower()
        raw_sponsorship = _fact_value(fact, "sponsorship_needed", "needs_sponsorship")
        if isinstance(raw_sponsorship, bool):
            sponsorship_needed = raw_sponsorship
    return countries, status, sponsorship_needed, fact_ids


def _eligibility(
    analysis_data: dict[str, Any],
    facts: list[ProfileFact],
    evidence_by_field: dict[str, list[str]],
    review_context: dict[str, Any] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    review_context = review_context or {}
    reasons: list[dict[str, Any]] = []
    if review_context.get("force_ineligible"):
        return "ineligible", [
            _reason(
                "human_review_ineligible",
                "The candidate explicitly resolved this job as ineligible.",
                "policy",
            )
        ]
    if analysis_data.get("job_status") in {"closed", "withdrawn", "expired"}:
        return "ineligible", [
            _reason("job_not_active", "The job is not active for matching.", "job")
        ]

    countries, auth_status, sponsorship_needed, auth_fact_ids = _candidate_authorization(facts)
    permitted = {str(item).upper() for item in analysis_data.get("permitted_countries", [])}
    location_country = analysis_data.get("location_country")
    if location_country:
        permitted.add(str(location_country).upper())
    if not permitted and not review_context.get("remote_geography_accepted"):
        reasons.append(
            _reason(
                "authorization_geography_unknown",
                "The job does not state a permitted work geography; "
                "authorization cannot be assumed.",
                "policy",
                facts=auth_fact_ids,
                evidence=evidence_by_field.get("work_mode", []),
            )
        )
    elif (
        not review_context.get("authorization_confirmed")
        and (not auth_fact_ids or auth_status in {None, "unknown", "pending"})
    ):
        reasons.append(
            _reason(
                "authorization_unconfirmed",
                "Work authorization for the job geography is not confirmed.",
                "candidate",
                facts=auth_fact_ids,
                evidence=evidence_by_field.get("permitted_countries", [])
                + evidence_by_field.get("location_country", []),
            )
        )
    elif (
        not review_context.get("authorization_confirmed")
        and (
            auth_status in {"denied", "not_authorized", "unauthorized"}
            or (
                not review_context.get("remote_geography_accepted")
                and not countries.intersection(permitted)
            )
        )
    ):
        return "ineligible", [
            _reason(
                "authorization_not_satisfied",
                "The confirmed work-authorization facts do not cover the job geography.",
                "candidate",
                facts=auth_fact_ids,
                evidence=evidence_by_field.get("permitted_countries", [])
                + evidence_by_field.get("location_country", []),
            )
        ]

    sponsorship = str(analysis_data.get("sponsorship", "unknown")).lower()
    if sponsorship_needed is True and sponsorship in {"not_available", "no"}:
        return "ineligible", [
            _reason(
                "sponsorship_unavailable",
                "The candidate confirmed sponsorship is needed, but the job does not offer it.",
                "job",
                facts=auth_fact_ids,
                evidence=evidence_by_field.get("sponsorship", []),
            )
        ]
    if (
        sponsorship_needed is True
        and sponsorship in {"unknown", ""}
        and not review_context.get("sponsorship_compatible")
    ):
        reasons.append(
            _reason(
                "sponsorship_unknown",
                "Sponsorship compatibility is unknown and needs candidate review.",
                "job",
                facts=auth_fact_ids,
                evidence=evidence_by_field.get("sponsorship", []),
            )
        )
    if reasons:
        return "needs_review", reasons
    if review_context.get("authorization_confirmed"):
        return "eligible", [
            _reason(
                "authorization_human_reviewed",
                "The candidate explicitly resolved authorization for this job geography.",
                "policy",
            )
        ]
    if review_context.get("remote_geography_accepted"):
        return "eligible", [
            _reason(
                "remote_geography_human_reviewed",
                "The candidate explicitly accepted the job's remote geography for this match.",
                "policy",
            )
        ]
    if review_context.get("sponsorship_compatible"):
        return "eligible", [
            _reason(
                "sponsorship_human_reviewed",
                "The candidate explicitly resolved sponsorship compatibility for this match.",
                "policy",
            )
        ]
    return "eligible", [
        _reason(
            "authorization_confirmed",
            "Confirmed authorization covers the explicit job geography.",
            "candidate",
            facts=auth_fact_ids,
            evidence=evidence_by_field.get("permitted_countries", [])
            + evidence_by_field.get("location_country", []),
        )
    ]


def _fit(
    requirements: list[JobRequirement],
    evidence: dict[str, JobEvidence],
    facts: list[ProfileFact],
    analysis_data: dict[str, Any],
) -> tuple[float, list[dict[str, Any]], dict[str, Any]]:
    candidate_skills: dict[str, list[ProfileFact]] = {}
    experience: dict[str, tuple[float, str]] = {}
    for fact in facts:
        if fact.fact_type == "skill":
            name = _fact_value(fact, "name", "skill")
            if name:
                candidate_skills.setdefault(canonical_skill(str(name)).lower(), []).append(fact)
        if fact.fact_type == "experience":
            name = _fact_value(fact, "skill", "name", "role")
            years = _fact_value(fact, "years", "years_experience")
            if name is not None and isinstance(years, (int, float)):
                experience[canonical_skill(str(name)).lower()] = (float(years), fact.id)

    required = [item for item in requirements if item.requirement_kind == "required"]
    preferred = [item for item in requirements if item.requirement_kind == "preferred"]
    reasons: list[dict[str, Any]] = []
    matched_required = matched_preferred = 0
    for item in required + preferred:
        key = canonical_skill(item.skill_text).lower()
        matching_facts = candidate_skills.get(key, [])
        years = experience.get(key)
        years_ok = item.minimum_years is None or (
            years is not None and years[0] >= item.minimum_years
        )
        matched = bool(matching_facts) and years_ok
        kind_label = "required" if item in required else "preferred"
        evidence_ids = [item.evidence_id] if item.evidence_id else []
        if matched:
            if kind_label == "required":
                matched_required += 1
            else:
                matched_preferred += 1
            fact_ids = [fact.id for fact in matching_facts]
            if years:
                fact_ids.append(years[1])
            reasons.append(
                _reason(
                    f"{kind_label}_skill_evidenced",
                    f"{kind_label.title()} skill {item.skill_text} is "
                    "supported by confirmed facts.",
                    "candidate",
                    facts=fact_ids,
                    evidence=evidence_ids,
                )
            )
        else:
            if matching_facts and item.minimum_years is not None and not years_ok:
                reasons.append(
                    _reason(
                        f"{kind_label}_experience_missing",
                        f"{kind_label.title()} experience for {item.skill_text} is not verified "
                        f"at the required {item.minimum_years:g} years.",
                        "candidate",
                        facts=[fact.id for fact in matching_facts],
                        evidence=evidence_ids,
                    )
                )
                continue
            reasons.append(
                _reason(
                    f"{kind_label}_skill_missing",
                    f"{kind_label.title()} skill {item.skill_text} is not fully "
                    "supported by confirmed facts.",
                    "candidate",
                    evidence=evidence_ids,
                )
            )
    education_requirements = [
        str(item) for item in analysis_data.get("education", []) if str(item).strip()
    ]
    education_facts = [fact for fact in facts if fact.fact_type == "education"]
    education_matched = 0
    for education in education_requirements:
        matched_facts = [
            fact
            for fact in education_facts
            if education.lower()
            in " ".join(str(value) for value in fact.value_json.values()).lower()
        ]
        if matched_facts:
            education_matched += 1
            reasons.append(
                _reason(
                    "education_evidenced",
                    f"Education requirement {education} is supported by a confirmed fact.",
                    "candidate",
                    facts=[fact.id for fact in matched_facts],
                )
            )
        else:
            reasons.append(
                _reason(
                    "education_missing",
                    f"Education requirement {education} is not supported by a confirmed fact.",
                    "candidate",
                )
            )
    required_score = matched_required / len(required) if required else 1.0
    preferred_score = matched_preferred / len(preferred) if preferred else 1.0
    if education_requirements:
        education_score = education_matched / len(education_requirements)
        score = round(
            (required_score * 65.0) + (preferred_score * 25.0) + (education_score * 10.0), 2
        )
    else:
        education_score = 1.0
        score = round((required_score * 70.0) + (preferred_score * 30.0), 2)
    return score, reasons, {
        "required_total": len(required),
        "required_matched": matched_required,
        "preferred_total": len(preferred),
        "preferred_matched": matched_preferred,
        "education_total": len(education_requirements),
        "education_matched": education_matched,
        "education_score": education_score,
    }


def _preference(
    job: JobPosting, analysis_data: dict[str, Any], preference: PreferenceVersion | None
) -> tuple[float, list[dict[str, Any]], dict[str, Any]]:
    if preference is None:
        return (
            50.0,
            [_reason("no_preferences", "No candidate preferences were provided.", "preference")],
            {},
        )
    rules = preference.rules_json
    components: dict[str, float] = {}
    reasons: list[dict[str, Any]] = []
    target_roles = [str(item).lower() for item in rules.get("target_roles", [])]
    title = job.title.lower()
    if target_roles:
        matched = any(role in title or title in role for role in target_roles)
        components["target_role"] = 100.0 if matched else 0.0
        reasons.append(
            _reason(
                "target_role_match" if matched else "target_role_mismatch",
                "The job title matches a target role."
                if matched
                else "The job title does not match a target role.",
                "preference",
            )
        )
    else:
        components["target_role"] = 50.0
    work_modes = [str(item).lower() for item in rules.get("work_modes", [])]
    if work_modes:
        mode = str(job.work_mode).lower()
        matched = mode in work_modes
        components["work_mode"] = 100.0 if matched else 0.0
        reasons.append(
            _reason(
                "work_mode_match" if matched else "work_mode_mismatch",
                "The work mode matches a stated preference."
                if matched
                else "The work mode does not match a stated preference.",
                "preference",
            )
        )
    else:
        components["work_mode"] = 50.0
    locations = [str(item).lower() for item in rules.get("location_preferences", [])]
    if locations:
        location = (job.location_text or "").lower()
        matched = any(item in location for item in locations)
        components["location"] = 100.0 if matched else 0.0
        reasons.append(
            _reason(
                "location_match" if matched else "location_mismatch",
                "The job location matches a stated preference."
                if matched
                else "The job location does not match a stated preference.",
                "preference",
            )
        )
    else:
        components["location"] = 50.0
    compensation = rules.get("compensation") or {}
    if compensation:
        job_compensation = analysis_data.get("compensation") or {}
        candidate_currency = str(compensation.get("currency", "")).upper()
        job_currency = str(job_compensation.get("currency", "")).upper()
        candidate_min = compensation.get("minimum")
        job_max = job_compensation.get("maximum")
        if not job_compensation:
            components["compensation"] = 50.0
            reasons.append(
                _reason(
                    "compensation_unknown",
                    "The job does not publish comparable compensation.",
                    "preference",
                )
            )
        elif candidate_currency and job_currency != candidate_currency:
            components["compensation"] = 50.0
            reasons.append(
                _reason(
                    "compensation_currency_unknown",
                    "Compensation currency is not comparable without an approved conversion.",
                    "preference",
                )
            )
        else:
            overlaps = (
                isinstance(candidate_min, (int, float))
                and isinstance(job_max, (int, float))
                and job_max >= candidate_min
            )
            components["compensation"] = 100.0 if overlaps else 0.0
            reasons.append(
                _reason(
                    "compensation_match" if overlaps else "compensation_mismatch",
                    "Published compensation overlaps the stated minimum."
                    if overlaps
                    else "Published compensation does not meet the stated minimum.",
                    "preference",
                )
            )
    score = round(sum(components.values()) / len(components), 2)
    return score, reasons, components


def match_job_for_user(
    db: Session,
    *,
    user_id: str,
    job_id: str,
    profile_version_id: str | None = None,
    preference_version_id: str | None = None,
    preserve_preference_snapshot: bool = False,
    snapshot_id: str | None = None,
    review_context: dict[str, Any] | None = None,
) -> MatchRun:
    profile = db.get(CandidateProfile, user_id)
    if profile is None or (profile.current_version_id is None and profile_version_id is None):
        raise MatchInputError("A profile version is required before matching")
    selected_profile_version_id = profile_version_id or profile.current_version_id
    profile_version = db.scalar(
        select(ProfileVersion).where(
            ProfileVersion.id == selected_profile_version_id,
            ProfileVersion.user_id == user_id,
            ProfileVersion.status != "deleted",
        )
    )
    if profile_version is None:
        raise MatchInputError("The current profile version is unavailable")
    job = db.get(JobPosting, job_id)
    # Private-source postings are only matchable by the tenant that owns the source.
    if job is None or job.owner_user_id not in (None, user_id):
        raise MatchInputError("Job not found")
    snapshot = (
        db.get(JobSnapshot, snapshot_id)
        if snapshot_id
        else db.scalar(
            select(JobSnapshot)
            .where(JobSnapshot.job_id == job.id)
            .order_by(desc(JobSnapshot.version), desc(JobSnapshot.retrieved_at))
        )
    )
    if snapshot is not None and snapshot.job_id != job.id:
        raise MatchInputError("Job snapshot does not belong to job")
    if snapshot is None:
        raise MatchInputError("Job snapshot is unavailable")
    analysis = analyze_job_snapshot(db, snapshot)
    requirements = list(
        db.scalars(select(JobRequirement).where(JobRequirement.analysis_id == analysis.id)).all()
    )
    evidence = {
        item.id: item
        for item in db.scalars(
            select(JobEvidence).where(JobEvidence.analysis_id == analysis.id)
        ).all()
    }
    evidence_by_field: dict[str, list[str]] = {}
    for item in evidence.values():
        evidence_by_field.setdefault(item.field_path, []).append(item.id)
    facts = _confirmed_facts(db, profile_version.id)
    eligibility, eligibility_reasons = _eligibility(
        analysis.structured_json, facts, evidence_by_field, review_context
    )
    if analysis.status != "validated" and eligibility == "eligible":
        eligibility = "needs_review"
        eligibility_reasons.append(
            _reason(
                "analysis_needs_review",
                "Job requirement extraction is incomplete or conflicting.",
                "policy",
            )
        )
    fit_score, fit_reasons, fit_details = _fit(
        requirements, evidence, facts, analysis.structured_json
    )
    preference = (
        db.get(PreferenceVersion, preference_version_id)
        if preserve_preference_snapshot and preference_version_id
        else None
    )
    if not preserve_preference_snapshot:
        preference = db.scalar(
            select(PreferenceVersion)
            .where(PreferenceVersion.user_id == user_id, PreferenceVersion.status == "active")
            .order_by(PreferenceVersion.created_at.desc())
        )
    if preference is not None and preference.user_id != user_id:
        raise MatchInputError("Preference version does not belong to user")
    preference_score, preference_reasons, preference_details = _preference(
        job, analysis.structured_json, preference
    )
    scores = {
        "eligibility": {"status": eligibility, "reasons": eligibility_reasons},
        "fit": {"score": fit_score, "reasons": fit_reasons, "details": fit_details},
        "preference": {
            "score": preference_score,
            "reasons": preference_reasons,
            "details": preference_details,
        },
        "review": review_context,
    }
    match = MatchRun(
        user_id=user_id,
        job_snapshot_id=snapshot.id,
        profile_version_id=profile_version.id,
        preference_version_id=preference.id if preference else None,
        policy_version=POLICY_VERSION,
        extractor_version=analysis.extractor_version,
        eligibility_status=eligibility,
        fit_score=fit_score,
        preference_score=preference_score,
        scores_json=scores,
    )
    db.add(match)
    db.flush()
    return match
