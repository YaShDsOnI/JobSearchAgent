from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RegisterRequest(StrictModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=12, max_length=128)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return value.strip().lower()


class LoginRequest(RegisterRequest):
    pass


class UserResponse(StrictModel):
    id: str
    email: str
    role: str
    status: str


class FactInput(StrictModel):
    fact_type: str = Field(min_length=1, max_length=128)
    value: dict[str, Any]
    provenance: Literal["candidate", "uploaded_cv", "approved_edit"]
    confirmation_status: Literal["confirmed", "unknown", "withdrawn"] = "unknown"
    source_ref: str | None = Field(default=None, max_length=512)
    evidence_text: str | None = Field(default=None, max_length=2_000)


class ProfileVersionCreate(StrictModel):
    summary: str | None = Field(default=None, max_length=10_000)
    facts: list[FactInput] = Field(default_factory=list, max_length=500)
    source_document_id: str | None = None


class FactResponse(StrictModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    fact_type: str
    value: dict[str, Any] = Field(validation_alias="value_json")
    provenance: str
    confirmation_status: str
    source_ref: str | None
    evidence_text: str | None


class ProfileVersionResponse(StrictModel):
    id: str
    summary: str | None
    status: str
    created_at: datetime
    facts: list[FactResponse]
    source_document_id: str | None


class Preferences(StrictModel):
    target_roles: list[str] = Field(default_factory=list, max_length=50)
    seniority: list[str] = Field(default_factory=list, max_length=20)
    employment_types: list[str] = Field(default_factory=list, max_length=20)
    compensation: dict[str, Any] = Field(default_factory=dict)
    work_modes: list[Literal["remote", "hybrid", "onsite", "unknown"]] = Field(
        default_factory=list, max_length=10
    )
    location_preferences: list[str] = Field(default_factory=list, max_length=50)
    work_authorization: dict[str, Any] = Field(default_factory=dict)
    notice_period_days: int | None = Field(default=None, ge=0, le=3650)


class PreferencesResponse(StrictModel):
    id: str
    rules: Preferences
    created_at: datetime
    status: str


class ConsentCreate(StrictModel):
    purpose: str = Field(min_length=1, max_length=128)
    scope: dict[str, Any] = Field(default_factory=dict)
    policy_version: str = Field(min_length=1, max_length=64)


class ConsentResponse(StrictModel):
    id: str
    purpose: str
    scope: dict[str, Any]
    policy_version: str
    granted_at: datetime
    withdrawn_at: datetime | None


class ArtifactResponse(StrictModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    kind: str
    content_type: str
    size_bytes: int
    sha256: str
    created_at: datetime


class ConnectorResponse(StrictModel):
    id: str
    name: str
    kind: str
    tier: str
    base_domain: str | None
    terms_version: str
    policy_status: str
    enabled: bool
    scope: Literal["shared", "private"] = "shared"
    authorization_basis: str | None = None
    authorization_expires_at: str | None = None
    health_status: str = "unknown"
    consecutive_failures: int = 0
    last_run_at: datetime | None = None
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_error_code: str | None = None
    schedule_interval_seconds: int | None = None
    next_run_due_at: datetime | None = None


class SourceHealthResponse(StrictModel):
    connector_id: str
    name: str
    scope: Literal["shared", "private"]
    enabled: bool
    state: Literal["success", "failure", "degraded", "blocked", "suspended", "unknown"]
    health_status: str
    alert: bool
    overdue: bool
    consecutive_failures: int
    last_error_code: str | None
    last_run_at: datetime | None
    last_success_at: datetime | None
    last_failure_at: datetime | None
    schedule_interval_seconds: int | None
    next_run_due_at: datetime | None
    runs_24h: dict[str, int]


class SourceRunResponse(StrictModel):
    id: str
    connector_id: str
    status: str
    attempt: int
    provider_version: str | None
    authorization_basis: str | None
    error_code: str | None
    error_detail: str | None
    stats: dict[str, Any]
    started_at: datetime
    finished_at: datetime | None


class IngestionTaskResponse(StrictModel):
    task_id: str
    connector_id: str
    state: str
    attempts: int


class JobSourceResponse(StrictModel):
    job_id: str
    connector_id: str
    connector_name: str
    source_job_id: str
    canonical_url: str
    status: str
    dedup_reason: str | None
    first_seen_at: datetime
    last_seen_at: datetime


class JobSnapshotResponse(StrictModel):
    id: str
    version: int
    content_hash: str
    raw_content_hash: str | None
    normalizer_version: str | None
    normalization_warnings: list[str]
    provenance: dict[str, Any]
    normalized: dict[str, Any]
    retrieved_at: datetime


class JobResponse(StrictModel):
    id: str
    source_job_id: str
    title: str
    employer: str
    canonical_url: str
    location_text: str | None
    work_mode: str
    status: str
    status_reason: str | None = None
    closed_at: datetime | None = None
    connector: ConnectorResponse
    first_seen_at: datetime
    last_seen_at: datetime
    duplicate_of_job_id: str | None = None
    dedup_reason: str | None = None
    sources: list[JobSourceResponse] = Field(default_factory=list)
    snapshot_version: int | None = None
    latest_snapshot: dict[str, Any] | None = None
    latest_provenance: dict[str, Any] | None = None


class JobEvidenceResponse(StrictModel):
    id: str
    field_path: str
    quote: str
    start_offset: int | None
    end_offset: int | None
    confidence: float


class JobRequirementResponse(StrictModel):
    id: str
    skill_text: str
    requirement_kind: Literal["required", "preferred", "informational"]
    importance: float
    minimum_years: float | None
    evidence: JobEvidenceResponse | None


class JobAnalysisResponse(StrictModel):
    id: str
    job_snapshot_id: str
    extractor_version: str
    structured: dict[str, Any]
    confidence: float
    status: str
    requirements: list[JobRequirementResponse]
    evidence: list[JobEvidenceResponse]
    created_at: datetime


class MatchReason(StrictModel):
    code: str
    message: str
    source: Literal["candidate", "job", "policy", "preference"]
    candidate_fact_ids: list[str] = Field(default_factory=list)
    job_evidence_ids: list[str] = Field(default_factory=list)


class MatchBreakdown(StrictModel):
    score: float
    reasons: list[MatchReason]
    details: dict[str, Any] = Field(default_factory=dict)


class MatchResponse(StrictModel):
    id: str
    job_id: str
    job_snapshot_id: str
    profile_version_id: str
    preference_version_id: str | None
    policy_version: str
    extractor_version: str
    eligibility: Literal["eligible", "ineligible", "needs_review", "unknown"]
    eligibility_reasons: list[MatchReason]
    fit: MatchBreakdown
    preference: MatchBreakdown
    created_at: datetime


class ReviewResolution(StrictModel):
    decision: Literal[
        "authorize_for_job_geography",
        "accept_remote_geography",
        "confirm_sponsorship_compatible",
        "mark_ineligible",
        "leave_unresolved",
    ]
    note: str = Field(min_length=1, max_length=2_000)


class MatchReviewResponse(StrictModel):
    id: str
    match_run_id: str
    job_id: str
    status: Literal["pending", "resolved"]
    reasons: list[MatchReason]
    job_evidence: list[JobEvidenceResponse]
    candidate_facts: list[FactResponse]
    resolution: ReviewResolution | None
    resolved_by_user_id: str | None
    resolved_at: datetime | None
    rerun_match_id: str | None
    created_at: datetime


class CVDraftCreate(StrictModel):
    match_run_id: str = Field(min_length=1, max_length=36)


class CVDecision(StrictModel):
    note: str = Field(default="", max_length=2_000)


class CVClaimResponse(StrictModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    section: str
    text: str
    source_fact_ids: list[str]
    job_requirement_ids: list[str]
    validation_status: Literal["accepted", "rejected", "unresolved"]
    validation_reason: str | None


class CVVersionResponse(StrictModel):
    id: str
    job_id: str
    job_snapshot_id: str
    profile_version_id: str
    match_run_id: str | None
    generator_version: str
    template_version: str
    content_hash: str
    status: Literal["validated", "approved", "rejected", "superseded"]
    unresolved_items: list[str]
    claims: list[CVClaimResponse]
    docx_artifact: ArtifactResponse | None
    pdf_artifact: ArtifactResponse | None
    approved_by_user_id: str | None
    approved_at: datetime | None
    rejected_at: datetime | None
    rejection_note: str | None
    superseded_at: datetime | None = None
    superseded_reason: str | None = None
    superseded_detail: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class DataRequestResponse(StrictModel):
    id: str
    request_type: str
    status: str
    requested_at: datetime
    completed_at: datetime | None
