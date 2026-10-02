import re
from datetime import UTC, datetime
from typing import Literal, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, aliased

from app.api.dependencies import get_current_user, require_csrf
from app.core.config import get_settings
from app.db.models import (
    JobAnalysis,
    JobEvidence,
    JobPosting,
    JobRequirement,
    JobSnapshot,
    MatchRun,
    SourceConnector,
    SourceRun,
    User,
)
from app.db.session import get_db
from app.schemas import (
    ConnectorResponse,
    IngestionTaskResponse,
    JobAnalysisResponse,
    JobEvidenceResponse,
    JobRequirementResponse,
    JobResponse,
    JobSnapshotResponse,
    JobSourceResponse,
    MatchBreakdown,
    MatchResponse,
    SourceHealthResponse,
    SourceRunResponse,
)
from app.services.artifact_lifecycle import delete_private_source
from app.services.audit import record_audit
from app.services.catalog import (
    connector_visible_to_user,
    get_visible_job,
    latest_snapshot,
    visible_to_user,
)
from app.services.matching import MatchInputError, match_job_for_user
from app.services.requirements import analyze_job_snapshot
from app.services.sources.errors import ConcurrentRunError
from app.services.sources.health import connector_health_report
from app.services.sources.pipeline import reset_connector_circuit
from app.services.sources.worker import enqueue_ingestion
from app.services.storage import ArtifactStorage

router = APIRouter(tags=["catalog"])

_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")


def connector_response(connector: SourceConnector) -> ConnectorResponse:
    authorization = connector.authorization_json or {}
    expires_at = authorization.get("expires_at")
    return ConnectorResponse(
        id=connector.id,
        name=connector.name,
        kind=connector.kind,
        tier=connector.tier,
        base_domain=connector.base_domain,
        terms_version=connector.terms_version,
        policy_status=connector.policy_status,
        enabled=connector.enabled,
        scope="private" if connector.owner_user_id else "shared",
        authorization_basis=str(authorization["basis"]) if authorization.get("basis") else None,
        authorization_expires_at=str(expires_at) if expires_at else None,
        health_status=connector.health_status or "unknown",
        consecutive_failures=connector.consecutive_failures or 0,
        last_run_at=connector.last_run_at,
        last_success_at=connector.last_success_at,
        last_failure_at=connector.last_failure_at,
        last_error_code=connector.last_error_code,
        schedule_interval_seconds=connector.schedule_interval_seconds,
        next_run_due_at=connector.next_run_due_at,
    )


def _visible_connector(db: Session, connector_id: str, user: User) -> SourceConnector:
    connector = db.scalar(
        select(SourceConnector).where(
            SourceConnector.id == connector_id, connector_visible_to_user(user.id)
        )
    )
    if connector is None:
        raise HTTPException(status_code=404, detail="Connector not found")
    return connector


def _can_operate(connector: SourceConnector, user: User) -> bool:
    if connector.owner_user_id is not None:
        return connector.owner_user_id == user.id
    return user.role == "operator"


@router.get("/connectors", response_model=list[ConnectorResponse])
def list_connectors(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    connectors = db.scalars(
        select(SourceConnector)
        .where(connector_visible_to_user(user.id))
        .order_by(SourceConnector.name)
    ).all()
    return [connector_response(connector) for connector in connectors]


@router.get("/connectors/health", response_model=list[SourceHealthResponse])
def connectors_health(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    connectors = db.scalars(
        select(SourceConnector)
        .where(connector_visible_to_user(user.id))
        .order_by(SourceConnector.name)
    ).all()
    return [SourceHealthResponse(**item) for item in connector_health_report(db, connectors)]


@router.get("/connectors/{connector_id}", response_model=ConnectorResponse)
def get_connector(
    connector_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    return connector_response(_visible_connector(db, connector_id, user))


@router.get("/connectors/{connector_id}/runs", response_model=list[SourceRunResponse])
def list_connector_runs(
    connector_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    connector = _visible_connector(db, connector_id, user)
    runs = db.scalars(
        select(SourceRun)
        .where(SourceRun.connector_id == connector.id)
        .order_by(SourceRun.started_at.desc(), SourceRun.attempt.desc())
        .limit(limit)
    ).all()
    return [
        SourceRunResponse(
            id=run.id,
            connector_id=run.connector_id,
            status=run.status,
            attempt=run.attempt,
            provider_version=run.provider_version,
            authorization_basis=run.authorization_basis,
            error_code=run.error_code,
            error_detail=run.error_detail,
            stats=run.stats_json or {},
            started_at=run.started_at,
            finished_at=run.finished_at,
        )
        for run in runs
    ]


@router.post(
    "/connectors/{connector_id}/ingest",
    response_model=IngestionTaskResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def request_ingestion(
    connector_id: str,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    require_csrf(request)
    connector = _visible_connector(db, connector_id, user)
    if not _can_operate(connector, user):
        raise HTTPException(status_code=403, detail="Not permitted to run this source")
    if idempotency_key is not None and not _IDEMPOTENCY_KEY.match(idempotency_key):
        raise HTTPException(status_code=400, detail="Invalid Idempotency-Key")
    # Without a client key, repeated clicks within the same minute collapse to one task.
    key = idempotency_key or "manual:" + datetime.now(UTC).strftime("%Y%m%dT%H%M")
    task = enqueue_ingestion(
        db, connector_id=connector.id, idempotency_key=key, requested_by_user_id=user.id
    )
    record_audit(
        db,
        action="source.ingestion_requested",
        actor="user",
        user_id=user.id,
        object_type="source_connector",
        object_id=connector.id,
        metadata={"task_id": task.id},
        request_id=request.state.request_id,
    )
    db.commit()
    return IngestionTaskResponse(
        task_id=task.id, connector_id=connector.id, state=task.state, attempts=task.attempts
    )


@router.delete("/connectors/{connector_id}")
def delete_connector(
    connector_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete a private source and all data derived from it (owner only)."""
    require_csrf(request)
    connector = _visible_connector(db, connector_id, user)
    if connector.owner_user_id != user.id:
        raise HTTPException(status_code=403, detail="Shared catalog sources cannot be deleted")
    try:
        counts = delete_private_source(
            db,
            ArtifactStorage(get_settings()),
            connector_id=connector.id,
            requested_by_user_id=user.id,
            request_id=request.state.request_id,
        )
    except ConcurrentRunError as exc:
        raise HTTPException(status_code=409, detail="An ingestion run is active") from exc
    return {"deleted": True, **counts}


@router.post("/connectors/{connector_id}/reset-circuit", response_model=ConnectorResponse)
def reset_circuit(
    connector_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    require_csrf(request)
    connector = _visible_connector(db, connector_id, user)
    if not _can_operate(connector, user):
        raise HTTPException(status_code=403, detail="Not permitted to manage this source")
    reset_connector_circuit(db, connector=connector, actor="user", user_id=user.id)
    db.commit()
    return connector_response(connector)


@router.get("/jobs", response_model=list[JobResponse])
def list_jobs(
    status: Literal["active", "closed", "stale", "all"] = Query(default="active"),
    include_duplicates: bool = Query(default=False),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    primary = aliased(JobPosting)
    query = (
        select(JobPosting, SourceConnector)
        .join(SourceConnector, SourceConnector.id == JobPosting.connector_id)
        .outerjoin(primary, primary.id == JobPosting.duplicate_of_job_id)
        .where(visible_to_user(user.id))
        .order_by(JobPosting.last_seen_at.desc(), JobPosting.id.asc())
    )
    if status != "all":
        query = query.where(JobPosting.status == status)
    if not include_duplicates:
        # Collapse a duplicate into its primary only when the primary is itself listed;
        # an active listing on another source must not disappear behind a closed primary.
        is_primary = JobPosting.duplicate_of_job_id.is_(None)
        query = query.where(
            is_primary if status == "all" else or_(is_primary, primary.status != status)
        )
    rows = db.execute(query).all()
    return [_job_response(db, job, connector, user) for job, connector in rows]


@router.get("/jobs/{job_id}", response_model=JobResponse)
def get_job(job_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    job = _visible_job(db, job_id, user)
    connector = db.get(SourceConnector, job.connector_id)
    if connector is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return _job_response(db, job, connector, user)


@router.get("/jobs/{job_id}/snapshots", response_model=list[JobSnapshotResponse])
def list_job_snapshots(
    job_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    job = _visible_job(db, job_id, user)
    snapshots = db.scalars(
        select(JobSnapshot)
        .where(JobSnapshot.job_id == job.id)
        .order_by(JobSnapshot.version.desc())
    ).all()
    return [
        JobSnapshotResponse(
            id=item.id,
            version=item.version,
            content_hash=item.content_hash,
            raw_content_hash=item.raw_content_hash,
            normalizer_version=item.normalizer_version,
            normalization_warnings=list(item.normalization_warnings or []),
            provenance=item.provenance_json or {},
            normalized=item.normalized_json,
            retrieved_at=item.retrieved_at,
        )
        for item in snapshots
    ]


@router.post("/jobs/{job_id}/analysis", response_model=JobAnalysisResponse)
def analyze_job(
    job_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    require_csrf(request)
    snapshot = _latest_snapshot(db, job_id, user)
    analysis = analyze_job_snapshot(db, snapshot)
    db.commit()
    return _analysis_response(db, analysis)


@router.get("/jobs/{job_id}/analysis", response_model=JobAnalysisResponse)
def get_analysis(
    job_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    snapshot = _latest_snapshot(db, job_id, user)
    analysis = db.scalar(
        select(JobAnalysis)
        .where(JobAnalysis.job_snapshot_id == snapshot.id)
        .order_by(JobAnalysis.created_at.desc())
    )
    if analysis is None:
        raise HTTPException(status_code=404, detail="Job analysis not found")
    return _analysis_response(db, analysis)


@router.post(
    "/jobs/{job_id}/match", response_model=MatchResponse, status_code=status.HTTP_201_CREATED
)
def create_match(
    job_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    require_csrf(request)
    _visible_job(db, job_id, user)
    try:
        match = match_job_for_user(db, user_id=user.id, job_id=job_id)
    except MatchInputError as exc:
        detail = str(exc)
        raise HTTPException(
            status_code=404 if detail == "Job not found" else 400, detail=detail
        ) from exc
    db.commit()
    return _match_response(db, match, job_id)


@router.get("/jobs/{job_id}/match", response_model=MatchResponse)
def get_match(job_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _visible_job(db, job_id, user)
    match = db.scalar(
        select(MatchRun)
        .join(JobSnapshot, MatchRun.job_snapshot_id == JobSnapshot.id)
        .where(MatchRun.user_id == user.id, JobSnapshot.job_id == job_id)
        .order_by(MatchRun.created_at.desc())
    )
    if match is None:
        raise HTTPException(status_code=404, detail="Match not found")
    return _match_response(db, match, job_id)


def _visible_job(db: Session, job_id: str, user: User) -> JobPosting:
    job = get_visible_job(db, job_id, user.id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


def _job_response(
    db: Session, job: JobPosting, connector: SourceConnector, user: User
) -> JobResponse:
    snapshot = latest_snapshot(db, job.id)
    primary_id = job.duplicate_of_job_id or job.id
    linked = db.execute(
        select(JobPosting, SourceConnector)
        .join(SourceConnector, SourceConnector.id == JobPosting.connector_id)
        .where(
            or_(JobPosting.id == primary_id, JobPosting.duplicate_of_job_id == primary_id),
            visible_to_user(user.id),
        )
        .order_by(JobPosting.first_seen_at.asc(), JobPosting.id.asc())
    ).all()
    return JobResponse(
        id=job.id,
        source_job_id=job.source_job_id,
        title=job.title,
        employer=job.employer,
        canonical_url=job.canonical_url,
        location_text=job.location_text,
        work_mode=job.work_mode,
        status=job.status,
        status_reason=job.status_reason,
        closed_at=job.closed_at,
        connector=connector_response(connector),
        first_seen_at=job.first_seen_at,
        last_seen_at=job.last_seen_at,
        duplicate_of_job_id=job.duplicate_of_job_id,
        dedup_reason=job.dedup_reason,
        sources=[
            JobSourceResponse(
                job_id=item.id,
                connector_id=item.connector_id,
                connector_name=item_connector.name,
                source_job_id=item.source_job_id,
                canonical_url=item.canonical_url,
                status=item.status,
                dedup_reason=item.dedup_reason,
                first_seen_at=item.first_seen_at,
                last_seen_at=item.last_seen_at,
            )
            for item, item_connector in linked
        ],
        snapshot_version=snapshot.version if snapshot else None,
        latest_snapshot=snapshot.normalized_json if snapshot else None,
        latest_provenance=snapshot.provenance_json if snapshot else None,
    )


def _latest_snapshot(db: Session, job_id: str, user: User) -> JobSnapshot:
    _visible_job(db, job_id, user)
    snapshot = latest_snapshot(db, job_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Job snapshot not found")
    return snapshot


def _analysis_response(db: Session, analysis: JobAnalysis) -> JobAnalysisResponse:
    evidence_rows = list(
        db.scalars(select(JobEvidence).where(JobEvidence.analysis_id == analysis.id)).all()
    )
    evidence_by_id = {item.id: item for item in evidence_rows}
    requirements = list(
        db.scalars(select(JobRequirement).where(JobRequirement.analysis_id == analysis.id)).all()
    )
    return JobAnalysisResponse(
        id=analysis.id,
        job_snapshot_id=analysis.job_snapshot_id,
        extractor_version=analysis.extractor_version,
        structured=analysis.structured_json,
        confidence=analysis.confidence,
        status=analysis.status,
        evidence=[
            JobEvidenceResponse.model_validate(item, from_attributes=True)
            for item in evidence_rows
        ],
        requirements=[
            JobRequirementResponse(
                id=item.id,
                skill_text=item.skill_text,
                requirement_kind=cast(
                    Literal["required", "preferred", "informational"], item.requirement_kind
                ),
                importance=item.importance,
                minimum_years=item.minimum_years,
                evidence=(
                    JobEvidenceResponse.model_validate(
                        evidence_by_id[item.evidence_id], from_attributes=True
                    )
                    if item.evidence_id in evidence_by_id
                    else None
                ),
            )
            for item in requirements
        ],
        created_at=analysis.created_at,
    )


def _match_response(db: Session, match: MatchRun, job_id: str) -> MatchResponse:
    scores = match.scores_json
    return MatchResponse(
        id=match.id,
        job_id=job_id,
        job_snapshot_id=match.job_snapshot_id,
        profile_version_id=match.profile_version_id,
        preference_version_id=match.preference_version_id,
        policy_version=match.policy_version,
        extractor_version=match.extractor_version,
        eligibility=cast(
            Literal["eligible", "ineligible", "needs_review", "unknown"],
            match.eligibility_status,
        ),
        eligibility_reasons=[
            item for item in scores["eligibility"]["reasons"]
        ],
        fit=MatchBreakdown.model_validate(scores["fit"]),
        preference=MatchBreakdown.model_validate(scores["preference"]),
        created_at=match.created_at,
    )
