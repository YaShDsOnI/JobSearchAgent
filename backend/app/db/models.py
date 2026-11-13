from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON


def new_id() -> str:
    return str(uuid4())


def now_utc() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    # Explicit so the mapping matches the migrations regardless of SQLAlchemy version
    # (2.1 maps `float` to Double by default; migrations created Float).
    type_annotation_map = {dict[str, Any]: JSON, float: Float}


class User(Base):
    __tablename__ = "users"
    # Matches migration 0001: a named unique constraint plus a plain lookup index.
    __table_args__ = (UniqueConstraint("email", name="uq_users_email"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(320), index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(32), default="candidate")
    status: Mapped[str] = mapped_column(String(32), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    profile: Mapped["CandidateProfile"] = relationship(back_populates="user", uselist=False)


class CandidateProfile(Base):
    __tablename__ = "candidate_profiles"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    current_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("profile_versions.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    user: Mapped[User] = relationship(back_populates="profile", foreign_keys=[user_id])


class StoredArtifact(Base):
    __tablename__ = "stored_artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(64))
    object_key: Mapped[str] = mapped_column(String(512), unique=True)
    content_type: Mapped[str] = mapped_column(String(255))
    size_bytes: Mapped[int] = mapped_column(Integer)
    # Integrity hash only; nothing looks artifacts up by hash, so no index (see 0001).
    sha256: Mapped[str] = mapped_column(String(64))
    encryption_key_ref: Mapped[str] = mapped_column(String(255), default="local:aes-gcm:v1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class ProfileVersion(Base):
    __tablename__ = "profile_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    source_document_id: Mapped[str | None] = mapped_column(
        ForeignKey("stored_artifacts.id", ondelete="SET NULL"), nullable=True
    )
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    normalized_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    facts: Mapped[list["ProfileFact"]] = relationship(
        back_populates="profile_version", cascade="all, delete-orphan"
    )


class ProfileFact(Base):
    __tablename__ = "profile_facts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    profile_version_id: Mapped[str] = mapped_column(
        ForeignKey("profile_versions.id", ondelete="CASCADE"), index=True
    )
    fact_type: Mapped[str] = mapped_column(String(128))
    value_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    provenance: Mapped[str] = mapped_column(String(64))
    confirmation_status: Mapped[str] = mapped_column(String(32), default="unknown")
    source_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    evidence_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    profile_version: Mapped[ProfileVersion] = relationship(back_populates="facts")


class Skill(Base):
    __tablename__ = "skills"
    __table_args__ = (
        UniqueConstraint("canonical_name", "taxonomy_version", name="uq_skill_taxonomy"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    canonical_name: Mapped[str] = mapped_column(String(128))
    taxonomy_version: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class JobAnalysis(Base):
    __tablename__ = "job_analyses"
    __table_args__ = (
        UniqueConstraint(
            "job_snapshot_id", "extractor_version", name="uq_job_analysis_snapshot_version"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("job_snapshots.id", ondelete="CASCADE"), index=True
    )
    extractor_version: Mapped[str] = mapped_column(String(64))
    structured_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    confidence: Mapped[float] = mapped_column()
    status: Mapped[str] = mapped_column(String(32), default="validated")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class JobEvidence(Base):
    __tablename__ = "job_evidence"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    analysis_id: Mapped[str] = mapped_column(
        ForeignKey("job_analyses.id", ondelete="CASCADE"), index=True
    )
    field_path: Mapped[str] = mapped_column(String(255))
    quote: Mapped[str] = mapped_column(Text)
    start_offset: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_offset: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confidence: Mapped[float] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class JobRequirement(Base):
    __tablename__ = "job_requirements"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    analysis_id: Mapped[str] = mapped_column(
        ForeignKey("job_analyses.id", ondelete="CASCADE"), index=True
    )
    skill_id: Mapped[str | None] = mapped_column(
        ForeignKey("skills.id", ondelete="SET NULL"), nullable=True, index=True
    )
    skill_text: Mapped[str] = mapped_column(String(128))
    requirement_kind: Mapped[str] = mapped_column(String(32))
    importance: Mapped[float] = mapped_column(default=1.0)
    minimum_years: Mapped[float | None] = mapped_column(nullable=True)
    evidence_id: Mapped[str | None] = mapped_column(
        ForeignKey("job_evidence.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class MatchRun(Base):
    __tablename__ = "match_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    job_snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("job_snapshots.id", ondelete="CASCADE"), index=True
    )
    profile_version_id: Mapped[str] = mapped_column(
        ForeignKey("profile_versions.id", ondelete="CASCADE"), index=True
    )
    preference_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("preference_versions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    policy_version: Mapped[str] = mapped_column(String(64))
    extractor_version: Mapped[str] = mapped_column(String(64))
    eligibility_status: Mapped[str] = mapped_column(String(32))
    fit_score: Mapped[float] = mapped_column()
    preference_score: Mapped[float] = mapped_column()
    scores_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class MatchReview(Base):
    __tablename__ = "match_reviews"
    __table_args__ = (
        UniqueConstraint("match_run_id", name="uq_match_review_run"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    match_run_id: Mapped[str] = mapped_column(
        ForeignKey("match_runs.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(32), default="pending")
    reason_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    resolution_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    resolved_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rerun_match_id: Mapped[str | None] = mapped_column(
        ForeignKey("match_runs.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class CVVersion(Base):
    __tablename__ = "cv_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("job_postings.id", ondelete="RESTRICT"), index=True
    )
    job_snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("job_snapshots.id", ondelete="RESTRICT"), index=True
    )
    profile_version_id: Mapped[str] = mapped_column(
        ForeignKey("profile_versions.id", ondelete="CASCADE"), index=True
    )
    match_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("match_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    generator_version: Mapped[str] = mapped_column(String(64))
    template_version: Mapped[str] = mapped_column(String(64))
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    docx_artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("stored_artifacts.id", ondelete="SET NULL"), nullable=True
    )
    pdf_artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("stored_artifacts.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32), default="validated", index=True)
    source_manifest_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    approved_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejection_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    superseded_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    superseded_detail_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class CVClaim(Base):
    __tablename__ = "cv_claims"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    cv_version_id: Mapped[str] = mapped_column(
        ForeignKey("cv_versions.id", ondelete="CASCADE"), index=True
    )
    section: Mapped[str] = mapped_column(String(64))
    text: Mapped[str] = mapped_column(Text)
    source_fact_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    job_requirement_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    validation_status: Mapped[str] = mapped_column(String(32))
    validation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class PreferenceVersion(Base):
    __tablename__ = "preference_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    rules_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Consent(Base):
    __tablename__ = "consents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    purpose: Mapped[str] = mapped_column(String(128))
    scope: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    policy_version: Mapped[str] = mapped_column(String(64))
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SourceConnector(Base):
    __tablename__ = "source_connectors"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    kind: Mapped[str] = mapped_column(String(32))
    tier: Mapped[str] = mapped_column(String(32))
    base_domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    terms_version: Mapped[str] = mapped_column(String(128))
    policy_status: Mapped[str] = mapped_column(String(32), default="review_required")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # NULL owner = shared catalog source managed by operators; otherwise a source
    # explicitly authorized for (and visible to) a single tenant.
    owner_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    authorization_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    health_status: Mapped[str] = mapped_column(String(32), default="unknown")
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_failure_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    active_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # NULL interval = not scheduled (manual runs only).
    schedule_interval_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    next_run_due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )


class SourceRun(Base):
    __tablename__ = "source_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    connector_id: Mapped[str] = mapped_column(
        ForeignKey("source_connectors.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(32))
    stats_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    requested_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    provider_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    authorization_basis: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(String(512), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class JobPosting(Base):
    __tablename__ = "job_postings"
    __table_args__ = (
        UniqueConstraint("connector_id", "source_job_id", name="uq_job_source_identity"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    connector_id: Mapped[str] = mapped_column(
        ForeignKey("source_connectors.id", ondelete="RESTRICT"), index=True
    )
    source_job_id: Mapped[str] = mapped_column(String(255))
    canonical_hash: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(512))
    employer: Mapped[str] = mapped_column(String(512))
    canonical_url: Mapped[str] = mapped_column(String(2048))
    location_text: Mapped[str | None] = mapped_column(String(512), nullable=True)
    work_mode: Mapped[str] = mapped_column(String(32), default="unknown")
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    status_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    owner_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    url_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    content_fingerprint: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    duplicate_of_job_id: Mapped[str | None] = mapped_column(
        ForeignKey("job_postings.id", ondelete="SET NULL"), nullable=True, index=True
    )
    dedup_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class JobSnapshot(Base):
    __tablename__ = "job_snapshots"
    __table_args__ = (
        UniqueConstraint("job_id", "version", name="uq_job_snapshot_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("job_postings.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer, default=1)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    raw_content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    raw_artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("stored_artifacts.id", ondelete="SET NULL"), nullable=True
    )
    normalized_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    normalizer_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    normalization_warnings: Mapped[list[str]] = mapped_column(JSON, default=list)
    connector_id: Mapped[str | None] = mapped_column(
        ForeignKey("source_connectors.id", ondelete="SET NULL"), nullable=True
    )
    source_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("source_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    provenance_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_task_idempotency"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(128))
    idempotency_key: Mapped[str] = mapped_column(String(255))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    state: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )


class Application(Base):
    """A candidate's review of one job. Phase 5C: preparation only; nothing is sent.

    `status` records only the candidate's decision (open/skipped). The state shown to the
    candidate (stale, ready, approved) is derived on read from the current package, its live
    binding check, and its approval; see `app/services/applications.py`.
    """

    __tablename__ = "applications"
    __table_args__ = (
        UniqueConstraint("user_id", "job_id", name="uq_application_user_job"),
        CheckConstraint("status IN ('open', 'skipped')", name="ck_applications_status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("job_postings.id", ondelete="RESTRICT"), index=True
    )
    connector_id: Mapped[str] = mapped_column(String(36))
    source_job_id: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), default="open")
    target_kind: Mapped[str] = mapped_column(String(32), default="link_out")
    # No FK: packages reference the application, and the pointer is set after the package
    # row exists (avoids a circular foreign key).
    current_package_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    candidate_answers_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    skip_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )


TRACKER_CHANNELS = ("link_out", "external", "sandbox_adapter")
TRACKER_STAGES = ("applied", "interviewing", "offer", "accepted", "rejected", "withdrawn")


class TrackedApplication(Base):
    """The candidate's record of where they applied (Phase 6 application tracker).

    `link_out` entries are candidate-reported and bind the package revision that was current
    when marked; `external` entries are typed by the candidate. Personal data: exported and
    deleted with the profile, and never copied into audit metadata.
    """

    __tablename__ = "tracked_applications"
    __table_args__ = (
        CheckConstraint(
            "channel IN ('link_out', 'external', 'sandbox_adapter')",
            name="ck_tracked_applications_channel",
        ),
        CheckConstraint(
            "stage IN ('applied', 'interviewing', 'offer', 'accepted', 'rejected', 'withdrawn')",
            name="ck_tracked_applications_stage",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    application_id: Mapped[str | None] = mapped_column(
        ForeignKey("applications.id", ondelete="SET NULL"), nullable=True, unique=True
    )
    package_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    review_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    channel: Mapped[str] = mapped_column(String(32))
    company: Mapped[str] = mapped_column(String(255))
    role_title: Mapped[str] = mapped_column(String(255))
    location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    job_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    applied_on: Mapped[date] = mapped_column(Date)
    stage: Mapped[str] = mapped_column(String(32), default="applied")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )


class ApplicationPackage(Base):
    """One immutable, hashed revision of an application review package."""

    __tablename__ = "application_packages"
    __table_args__ = (
        UniqueConstraint("application_id", "revision", name="uq_application_package_revision"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    application_id: Mapped[str] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), index=True
    )
    revision: Mapped[int] = mapped_column(Integer)
    review_hash: Mapped[str] = mapped_column(String(64))
    package_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    job_snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("job_snapshots.id", ondelete="RESTRICT"), index=True
    )
    match_run_id: Mapped[str] = mapped_column(
        ForeignKey("match_runs.id", ondelete="RESTRICT"), index=True
    )
    cv_version_id: Mapped[str] = mapped_column(
        ForeignKey("cv_versions.id", ondelete="RESTRICT"), index=True
    )
    profile_version_id: Mapped[str] = mapped_column(String(36))
    preference_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    connector_id: Mapped[str] = mapped_column(String(36))
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    superseded_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class ApprovalRecord(Base):
    __tablename__ = "approval_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    application_id: Mapped[str] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), index=True
    )
    package_id: Mapped[str] = mapped_column(
        ForeignKey("application_packages.id", ondelete="CASCADE"), index=True
    )
    reviewed_payload_hash: Mapped[str] = mapped_column(String(64))
    approved_by_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ApplicationEvent(Base):
    """Append-only application timeline. Metadata never holds answer values."""

    __tablename__ = "application_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    application_id: Mapped[str] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(64))
    actor: Mapped[str] = mapped_column(String(64))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    actor: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(128))
    object_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    object_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class DataRequest(Base):
    __tablename__ = "data_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    request_type: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="requested")
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
