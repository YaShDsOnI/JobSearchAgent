"""Job discovery pipeline.

Provider → Fetch → Validate → Normalize → Deduplicate → Snapshot → Persist → Audit.

`run_ingestion` owns its transaction boundaries (it commits), so call it with a dedicated
session. Record persistence for one run is a single transaction: it either fully commits
or rolls back, and the encrypted artifacts written during a failed attempt are deleted.
"""

import logging
import re
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.models import (
    JobPosting,
    JobSnapshot,
    SourceConnector,
    SourceRun,
    StoredArtifact,
    now_utc,
)
from app.services.audit import record_audit
from app.services.cv_invalidation import (
    changed_material_fields,
    comparable_job_content,
    supersede_cv_versions,
)
from app.services.sources.base import (
    FetchRequest,
    ProviderDescriptor,
    SourceProvider,
    freeze,
)
from app.services.sources.credentials import resolve_credential
from app.services.sources.dedup import (
    content_fingerprint,
    reconcile_duplicate_link,
    url_fingerprint,
)
from app.services.sources.egress import EgressGuard, EgressPolicy, Resolver, system_resolver
from app.services.sources.errors import (
    ConcurrentRunError,
    InvalidProviderResponseError,
    RetryableSourceError,
    SourceError,
    SourceErrorCode,
    SourcePolicyError,
)
from app.services.sources.http_client import ConnectorHttpClient, HttpLimits
from app.services.sources.normalize import (
    NORMALIZER_VERSION,
    NormalizedRecord,
    RecordRejected,
    normalize_record,
)
from app.services.sources.policy import (
    AuthorizationDecision,
    as_aware,
    authorize_connector,
    check_rate_limit,
    check_tenant_access,
    connector_fetch_hosts,
    url_host_permitted,
)
from app.services.sources.registry import ProviderRegistry, default_registry
from app.services.sources.validation import (
    extract_source_job_id,
    validate_page,
    validate_record,
)
from app.services.storage import ArtifactStorage

logger = logging.getLogger(__name__)

HttpClientFactory = Callable[[SourceConnector, Settings, tuple[str, ...]], ConnectorHttpClient]

STALE_RUN_AFTER = timedelta(minutes=15)
FAILING_THRESHOLD = 3
CIRCUIT_BREAKER_THRESHOLD = 5
MAX_RECORDED_REJECTIONS = 50
SUCCESS_STATUSES = ("succeeded", "partial")


@dataclass
class IngestionResult:
    run_id: str
    connector_id: str
    status: str
    stats: dict[str, Any]
    replayed: bool = False

    def count(self, key: str) -> int:
        return int(self.stats.get(key, 0))


@dataclass
class _Fetched:
    records: list[tuple[int, Any]]
    page_urls: dict[int, str | None]
    listing_complete: bool
    pages: int
    page_api_versions: dict[int, str | None] = field(default_factory=dict)


@dataclass
class _RunContext:
    connector: SourceConnector
    run: SourceRun
    descriptor: ProviderDescriptor
    decision: AuthorizationDecision
    storage: ArtifactStorage
    now: datetime
    actor: str
    written_object_keys: list[str] = field(default_factory=list)
    http: ConnectorHttpClient | None = None
    page_api_versions: dict[int, str | None] = field(default_factory=dict)


def build_connector_http_client(
    connector: SourceConnector,
    settings: Settings,
    egress_allowlist: tuple[str, ...],
    *,
    resolver: Resolver = system_resolver,
    transport: httpx.BaseTransport | None = None,
) -> ConnectorHttpClient:
    """The only way a provider gets network access: bound to this connector's policy."""
    config = connector.config_json or {}
    policy = EgressPolicy(
        deployment_allowlist=tuple(egress_allowlist),
        connector_hosts=connector_fetch_hosts(connector),
        path_prefixes=tuple(str(item) for item in config.get("fetch_path_prefixes", []) or []),
    )
    proxy = settings.source_egress_proxy
    return ConnectorHttpClient(
        # Behind the egress proxy, the proxy resolves DNS and refuses non-public addresses.
        guard=EgressGuard(policy, resolver, resolve_dns=proxy is None),
        limits=HttpLimits.from_settings(settings),
        credential=resolve_credential(config, settings),
        # Tests only; production uses the proxy tunnel or the DNS-pinning transport.
        transport=transport,
        proxy=proxy,
    )


def run_ingestion(
    db: Session,
    storage: ArtifactStorage,
    *,
    connector_id: str,
    registry: ProviderRegistry | None = None,
    idempotency_key: str | None = None,
    requested_by_user_id: str | None = None,
    actor: str = "system",
    now: datetime | None = None,
    network_access_enabled: bool | None = None,
    egress_allowlist: tuple[str, ...] | None = None,
    http_client_factory: HttpClientFactory | None = None,
) -> IngestionResult:
    now = now or now_utc()
    registry = registry or default_registry()
    settings = get_settings()
    if network_access_enabled is None:
        network_access_enabled = settings.source_network_access_enabled
    if egress_allowlist is None:
        egress_allowlist = settings.source_egress_allowlist_entries

    connector = db.get(SourceConnector, connector_id)
    if connector is None:
        raise SourcePolicyError(SourceErrorCode.CONNECTOR_NOT_FOUND, "Connector not found")
    # Checked before any write so another tenant cannot affect this connector's state.
    check_tenant_access(connector, requested_by_user_id)

    if idempotency_key:
        replay = _replay(db, connector, idempotency_key)
        if replay is not None:
            return replay

    try:
        provider = registry.create(connector.kind)
        decision = authorize_connector(
            connector,
            provider.descriptor,
            now=now,
            network_access_enabled=network_access_enabled,
            egress_allowlist=egress_allowlist,
        )
        http: ConnectorHttpClient | None = None
        if provider.descriptor.requires_network:
            # Only after every policy check passed; credential resolution also fails closed.
            http = (http_client_factory or build_connector_http_client)(
                connector, settings, egress_allowlist
            )
    except SourcePolicyError as exc:
        _record_blocked(db, connector, exc, idempotency_key, requested_by_user_id, actor, now)
        raise
    try:
        check_rate_limit(connector, provider.descriptor, now=now)
        run = _start_run(
            db, connector, provider.descriptor, decision, idempotency_key, requested_by_user_id,
            actor, now,
        )
    except SourceError:
        if http is not None:
            http.close()
        raise
    context = _RunContext(
        connector=connector,
        run=run,
        descriptor=provider.descriptor,
        decision=decision,
        storage=storage,
        now=now,
        actor=actor,
        http=http,
    )
    try:
        fetched = _fetch_all(provider, connector, http)
        context.page_api_versions = fetched.page_api_versions
        stats = _persist(db, context, fetched)
        if http is not None:
            stats["network"] = _record_network_events(db, context)
        status = "partial" if stats["rejected"] else "succeeded"
        run.status = status
        run.stats_json = stats
        run.finished_at = now
        previous_health = connector.health_status
        _update_health(connector, status, "records_rejected" if stats["rejected"] else None, now)
        _audit_health_change(db, connector, previous_health, actor)
        connector.active_run_id = None
        record_audit(
            db,
            action="source.ingestion_completed",
            actor=actor,
            user_id=connector.owner_user_id,
            object_type="source_run",
            object_id=run.id,
            metadata=_run_audit_metadata(connector, run, stats),
        )
        db.commit()
        return IngestionResult(run.id, connector.id, status, stats)
    except SourceError as exc:
        _abort(db, context, exc)
        raise
    except IntegrityError as exc:
        error = RetryableSourceError(
            SourceErrorCode.PERSISTENCE_CONFLICT, "Concurrent write conflict during ingestion"
        )
        _abort(db, context, error)
        raise error from exc
    except Exception as exc:
        # Storage/IO faults are usually transient; retries stay bounded by the task policy.
        internal = RetryableSourceError(
            SourceErrorCode.INTERNAL_ERROR, f"Unexpected {type(exc).__name__}"
        )
        _abort(db, context, internal)
        raise internal from exc
    finally:
        if http is not None:
            http.close()


def reset_connector_circuit(
    db: Session, *, connector: SourceConnector, actor: str, user_id: str | None
) -> None:
    previous = connector.health_status
    connector.health_status = "unknown"
    connector.consecutive_failures = 0
    record_audit(
        db,
        action="source.circuit_reset",
        actor=actor,
        user_id=user_id,
        object_type="source_connector",
        object_id=connector.id,
        metadata={"previous_health_status": previous},
    )
    db.flush()


# --- run bookkeeping -------------------------------------------------------------------


def _replay(db: Session, connector: SourceConnector, key: str) -> IngestionResult | None:
    run = db.scalar(
        select(SourceRun)
        .where(
            SourceRun.connector_id == connector.id,
            SourceRun.idempotency_key == key,
            SourceRun.status.in_(SUCCESS_STATUSES),
        )
        .order_by(SourceRun.attempt.desc())
    )
    if run is None:
        return None
    return IngestionResult(run.id, connector.id, run.status, dict(run.stats_json), replayed=True)


def _next_attempt(db: Session, connector_id: str, key: str | None) -> int:
    if not key:
        return 1
    current = db.scalar(
        select(func.max(SourceRun.attempt)).where(
            SourceRun.connector_id == connector_id, SourceRun.idempotency_key == key
        )
    )
    return int(current or 0) + 1


def _record_blocked(
    db: Session,
    connector: SourceConnector,
    exc: SourcePolicyError,
    key: str | None,
    requested_by_user_id: str | None,
    actor: str,
    now: datetime,
) -> None:
    run = SourceRun(
        connector_id=connector.id,
        status="blocked",
        stats_json={},
        idempotency_key=key,
        attempt=_next_attempt(db, connector.id, key),
        requested_by_user_id=requested_by_user_id,
        error_code=exc.code.value,
        error_detail=exc.detail,
        started_at=now,
        finished_at=now,
    )
    db.add(run)
    previous_health = connector.health_status
    if exc.code != SourceErrorCode.CIRCUIT_OPEN:
        connector.health_status = "blocked"
    connector.last_error_code = exc.code.value
    connector.last_failure_at = now
    db.flush()
    _audit_health_change(db, connector, previous_health, actor)
    record_audit(
        db,
        action="source.ingestion_blocked",
        actor=actor,
        user_id=connector.owner_user_id,
        object_type="source_run",
        object_id=run.id,
        metadata={"connector_id": connector.id, "error_code": exc.code.value},
    )
    db.commit()


def _start_run(
    db: Session,
    connector: SourceConnector,
    descriptor: ProviderDescriptor,
    decision: AuthorizationDecision,
    key: str | None,
    requested_by_user_id: str | None,
    actor: str,
    now: datetime,
) -> SourceRun:
    previous_run_id = connector.active_run_id
    if previous_run_id:
        previous = db.get(SourceRun, previous_run_id)
        if previous is not None and previous.status == "running":
            if now - as_aware(previous.started_at) < STALE_RUN_AFTER:
                raise ConcurrentRunError("Another ingestion run is active for this connector")
            previous.status = "abandoned"
            previous.error_code = "abandoned"
            previous.finished_at = now
    run = SourceRun(
        connector_id=connector.id,
        status="running",
        stats_json={},
        idempotency_key=key,
        attempt=_next_attempt(db, connector.id, key),
        requested_by_user_id=requested_by_user_id,
        provider_version=descriptor.version,
        authorization_basis=decision.basis,
        started_at=now,
    )
    db.add(run)
    db.flush()
    # Compare-and-set lock: exactly one active run per connector across workers.
    lock_condition = (
        SourceConnector.active_run_id.is_(None)
        if previous_run_id is None
        else SourceConnector.active_run_id == previous_run_id
    )
    result = db.execute(
        update(SourceConnector)
        .where(SourceConnector.id == connector.id, lock_condition)
        .values(active_run_id=run.id, last_run_at=now)
        .execution_options(synchronize_session=False)
    )
    if getattr(result, "rowcount", 0) != 1:
        db.rollback()
        raise ConcurrentRunError("Another ingestion run is active for this connector")
    connector.active_run_id = run.id
    connector.last_run_at = now
    record_audit(
        db,
        action="source.ingestion_started",
        actor=actor,
        user_id=connector.owner_user_id,
        object_type="source_run",
        object_id=run.id,
        metadata={
            "connector_id": connector.id,
            "attempt": run.attempt,
            "provider_version": descriptor.version,
            "authorization_basis": decision.basis,
        },
    )
    db.commit()
    return run


def _abort(db: Session, context: _RunContext, exc: SourceError) -> None:
    db.rollback()
    for object_key in context.written_object_keys:
        # Best effort: an orphaned encrypted blob is unreferenced and harmless.
        with suppress(OSError, ValueError):
            context.storage.delete(object_key)
    run = db.get(SourceRun, context.run.id)
    connector = db.get(SourceConnector, context.connector.id)
    if run is None or connector is None:
        return
    previous_health = connector.health_status
    blocked = isinstance(exc, SourcePolicyError)
    run.status = "blocked" if blocked else "failed"
    run.error_code = exc.code.value
    run.error_detail = exc.detail
    run.finished_at = context.now
    if context.http is not None:
        run.stats_json = {"network": _record_network_events(db, context)}
    if blocked:
        # Egress/access-control refusals need a human decision, not the retry counter.
        connector.health_status = "blocked"
        connector.last_error_code = exc.code.value
        connector.last_failure_at = context.now
    else:
        _update_health(connector, "failed", exc.code.value, context.now)
    _audit_health_change(db, connector, previous_health, context.actor)
    if connector.active_run_id == run.id:
        connector.active_run_id = None
    record_audit(
        db,
        action="source.ingestion_blocked" if blocked else "source.ingestion_failed",
        actor=context.actor,
        user_id=connector.owner_user_id,
        object_type="source_run",
        object_id=run.id,
        metadata={
            "connector_id": connector.id,
            "error_code": exc.code.value,
            "retryable": exc.retryable,
            "attempt": run.attempt,
        },
    )
    db.commit()


def _update_health(
    connector: SourceConnector, outcome: str, error_code: str | None, now: datetime
) -> None:
    if outcome in SUCCESS_STATUSES:
        connector.consecutive_failures = 0
        connector.health_status = "healthy" if outcome == "succeeded" else "degraded"
        connector.last_success_at = now
        connector.last_error_code = error_code
        return
    connector.consecutive_failures = (connector.consecutive_failures or 0) + 1
    connector.last_failure_at = now
    connector.last_error_code = error_code
    if connector.consecutive_failures >= CIRCUIT_BREAKER_THRESHOLD:
        connector.health_status = "suspended"
    elif connector.consecutive_failures >= FAILING_THRESHOLD:
        connector.health_status = "failing"
    else:
        connector.health_status = "degraded"


def _run_audit_metadata(
    connector: SourceConnector, run: SourceRun, stats: Mapping[str, Any]
) -> dict[str, Any]:
    keys = (
        "fetched", "created", "updated", "unchanged", "closed", "reopened", "stale",
        "rejected", "duplicates_linked", "duplicates_unlinked",
    )
    return {
        "connector_id": connector.id,
        "attempt": run.attempt,
        **{key: stats.get(key, 0) for key in keys},
    }


HEALTH_ALERT_STATES = frozenset({"failing", "suspended", "blocked"})


def _audit_health_change(
    db: Session, connector: SourceConnector, previous: str | None, actor: str
) -> None:
    current = connector.health_status
    if previous == current:
        return
    record_audit(
        db,
        action="source.health_changed",
        actor=actor,
        user_id=connector.owner_user_id,
        object_type="source_connector",
        object_id=connector.id,
        metadata={
            "from": previous,
            "to": current,
            "error_code": connector.last_error_code,
            "consecutive_failures": connector.consecutive_failures,
        },
    )
    if current in HEALTH_ALERT_STATES:
        logger.warning(
            "Source connector health degraded: %s -> %s (%s)",
            previous,
            current,
            connector.last_error_code,
            extra={"error_type": f"source_health_{current}"},
        )


def _record_network_events(db: Session, context: _RunContext) -> dict[str, Any]:
    assert context.http is not None
    events = list(context.http.events)
    context.http.events.clear()
    for event in events:
        record_audit(
            db,
            action=(
                "source.egress_denied" if event.outcome == "denied" else "source.network_request"
            ),
            actor=context.actor,
            user_id=context.connector.owner_user_id,
            object_type="source_run",
            object_id=context.run.id,
            metadata={"connector_id": context.connector.id, **asdict(event)},
        )
    return {
        "requests": sum(1 for event in events if event.outcome != "denied"),
        "denied": sum(1 for event in events if event.outcome == "denied"),
        "errors": sum(1 for event in events if event.outcome == "error"),
        "bytes": sum(event.bytes_read for event in events),
        "hosts": sorted({event.host for event in events if event.host}),
    }


# --- fetch ------------------------------------------------------------------------------


def _fetch_all(
    provider: SourceProvider, connector: SourceConnector, http: ConnectorHttpClient | None
) -> _Fetched:
    descriptor = provider.descriptor
    records: list[tuple[int, Any]] = []
    page_urls: dict[int, str | None] = {}
    page_api_versions: dict[int, str | None] = {}
    seen_cursors: set[str] = set()
    cursor: str | None = None
    complete = True
    pages = 0
    for page_index in range(descriptor.max_pages):
        request = FetchRequest(
            connector_id=connector.id,
            # A deep, read-only copy: provider code cannot alter the connector's
            # authorization or security configuration through its request.
            config=freeze(connector.config_json or {}),
            cursor=cursor,
            page_index=page_index,
            http=http,
        )
        try:
            page = validate_page(provider.fetch_page(request))
        except SourceError:
            raise
        except Exception as exc:  # Provider bugs/transport errors: classified, redacted.
            raise RetryableSourceError(
                SourceErrorCode.PROVIDER_ERROR, f"Provider raised {type(exc).__name__}"
            ) from exc
        pages += 1
        complete = complete and page.listing_complete
        page_urls[page_index] = page.source_url
        page_api_versions[page_index] = _safe_api_version(page.metadata)
        records.extend((page_index, record) for record in page.records)
        if len(records) > descriptor.max_records_per_run:
            raise InvalidProviderResponseError("Provider exceeded the per-run record limit")
        if page.next_cursor is None:
            break
        if page.next_cursor in seen_cursors:
            raise InvalidProviderResponseError("Provider pagination cursor repeated")
        seen_cursors.add(page.next_cursor)
        cursor = page.next_cursor
    else:
        complete = False  # Page limit reached; the listing is not known to be complete.
    return _Fetched(
        records=records,
        page_urls=page_urls,
        listing_complete=complete,
        pages=pages,
        page_api_versions=page_api_versions,
    )


_API_VERSION = re.compile(r"^[A-Za-z0-9._-]{1,32}$")


def _safe_api_version(metadata: Mapping[str, Any]) -> str | None:
    """The only provider metadata that is persisted: a short, validated version label."""
    value = metadata.get("api_version") if isinstance(metadata, Mapping) else None
    return value if isinstance(value, str) and _API_VERSION.match(value) else None


# --- persist ----------------------------------------------------------------------------


def _persist(db: Session, context: _RunContext, fetched: _Fetched) -> dict[str, Any]:
    stats: dict[str, Any] = {
        "fetched": len(fetched.records),
        "pages": fetched.pages,
        "listing_complete": fetched.listing_complete,
        "created": 0,
        "updated": 0,
        "unchanged": 0,
        "closed": 0,
        "reopened": 0,
        "stale": 0,
        "rejected": 0,
        "duplicates_linked": 0,
        "duplicates_unlinked": 0,
        "warnings": {},
        "rejections": [],
        "notices": [],
    }
    seen_ids: set[str] = set()
    accepted: dict[str, tuple[int, Mapping[str, Any]]] = {}

    def reject(source_job_id: str | None, codes: list[str]) -> None:
        stats["rejected"] += 1
        if len(stats["rejections"]) < MAX_RECORDED_REJECTIONS:
            stats["rejections"].append({"source_job_id": source_job_id, "codes": codes})

    for page_index, raw in fetched.records:
        source_job_id = extract_source_job_id(raw)
        if source_job_id:
            seen_ids.add(source_job_id)
        errors = validate_record(raw)
        if errors:
            reject(source_job_id, errors)
        elif source_job_id in accepted:
            reject(source_job_id, ["duplicate_in_response"])
        else:
            assert source_job_id is not None
            accepted[source_job_id] = (page_index, raw)

    # Deterministic processing order regardless of provider ordering.
    for source_job_id in sorted(accepted):
        page_index, raw = accepted[source_job_id]
        try:
            normalized = normalize_record(raw)
        except RecordRejected as exc:
            reject(source_job_id, exc.codes)
            continue
        if not url_host_permitted(normalized.content["canonical_url"], context.connector):
            reject(source_job_id, ["canonical_url_host_not_permitted"])
            continue
        for warning in normalized.warnings:
            stats["warnings"][warning] = stats["warnings"].get(warning, 0) + 1
        _upsert(db, context, normalized, raw, page_index, fetched.page_urls.get(page_index), stats)

    if fetched.listing_complete:
        _mark_absent_stale(db, context, seen_ids, stats)
    db.flush()
    return stats


def _mark_absent_stale(
    db: Session, context: _RunContext, seen_ids: set[str], stats: dict[str, Any]
) -> None:
    active = db.scalars(
        select(JobPosting).where(
            JobPosting.connector_id == context.connector.id, JobPosting.status == "active"
        )
    ).all()
    if not seen_ids and active:
        # An empty "complete" listing is far more likely a provider fault than every job
        # closing at once; do not stale the catalog on it.
        stats["notices"].append("empty_complete_listing_ignored")
        return
    for job in sorted(active, key=lambda item: item.source_job_id):
        if job.source_job_id not in seen_ids:
            _transition(db, context, job, "stale", "absent_from_complete_listing")
            stats["stale"] += 1


def _latest_snapshot(db: Session, job_id: str) -> JobSnapshot | None:
    return db.scalar(
        select(JobSnapshot)
        .where(JobSnapshot.job_id == job_id)
        .order_by(JobSnapshot.version.desc(), JobSnapshot.retrieved_at.desc())
    )


def _upsert(
    db: Session,
    context: _RunContext,
    normalized: NormalizedRecord,
    raw: Mapping[str, Any],
    page_index: int,
    page_url: str | None,
    stats: dict[str, Any],
) -> None:
    connector = context.connector
    content = normalized.content
    now = context.now
    job = db.scalar(
        select(JobPosting).where(
            JobPosting.connector_id == connector.id,
            JobPosting.source_job_id == normalized.source_job_id,
        )
    )
    if job is None:
        closed = content["status"] == "closed"
        job = JobPosting(
            connector_id=connector.id,
            owner_user_id=connector.owner_user_id,
            source_job_id=normalized.source_job_id,
            canonical_hash=normalized.content_hash,
            first_seen_at=now,
            last_seen_at=now,
            status="closed" if closed else "active",
            status_reason="source_declared_closed" if closed else None,
            status_changed_at=now,
            closed_at=now if closed else None,
        )
        _apply_content(job, content)
        db.add(job)
        db.flush()
        snapshot = _create_snapshot(db, context, job, normalized, raw, 1, page_index, page_url)
        stats["created"] += 1
        if closed:
            stats["closed"] += 1
        _audit_job(db, context, job, "job.created", snapshot)
        _reconcile(db, context, job, stats)
        return

    job.last_seen_at = now
    latest = _latest_snapshot(db, job.id)
    if latest is not None and latest.content_hash == normalized.content_hash:
        stats["unchanged"] += 1
        if job.status == "stale":
            _transition(db, context, job, "active", "reappeared_in_listing")
            stats["reopened"] += 1
        if job.url_fingerprint is None:
            # Postings created before fingerprints existed join cross-source dedup lazily.
            _apply_content(job, content)
            _reconcile(db, context, job, stats)
        return

    changed_fields = changed_material_fields(
        comparable_job_content(latest) if latest is not None else None, content
    )
    version = (latest.version if latest is not None else 0) + 1
    job.canonical_hash = normalized.content_hash
    _apply_content(job, content)
    snapshot = _create_snapshot(db, context, job, normalized, raw, version, page_index, page_url)
    stats["updated"] += 1
    _audit_job(
        db,
        context,
        job,
        "job.updated",
        snapshot,
        {"material_change": bool(changed_fields), "changed_material_fields": changed_fields},
    )
    if changed_fields:
        # Only a material change can make a CV drafted for the older snapshot wrong.
        superseded = supersede_cv_versions(
            db,
            reason="job_material_change",
            detail={
                "job_id": job.id,
                "previous_job_snapshot_id": latest.id if latest is not None else None,
                "job_snapshot_id": snapshot.id,
                "changed_material_fields": changed_fields,
            },
            actor=context.actor,
            job_id=job.id,
            now=now,
        )
        stats["cv_versions_superseded"] = stats.get("cv_versions_superseded", 0) + len(
            superseded
        )
    if content["status"] == "closed" and job.status != "closed":
        _transition(db, context, job, "closed", "source_declared_closed")
        stats["closed"] += 1
    elif content["status"] == "active" and job.status in {"closed", "stale"}:
        _transition(db, context, job, "active", "reopened_by_source")
        stats["reopened"] += 1
    _reconcile(db, context, job, stats)


def _apply_content(job: JobPosting, content: Mapping[str, Any]) -> None:
    job.title = content["title"]
    job.employer = content["employer"]
    job.canonical_url = content["canonical_url"]
    job.location_text = content["location_text"]
    job.work_mode = content["work_mode"]
    job.url_fingerprint = url_fingerprint(content["canonical_url"])
    job.content_fingerprint = content_fingerprint(dict(content))


def _create_snapshot(
    db: Session,
    context: _RunContext,
    job: JobPosting,
    normalized: NormalizedRecord,
    raw: Mapping[str, Any],
    version: int,
    page_index: int,
    page_url: str | None,
) -> JobSnapshot:
    connector = context.connector
    artifact_data = context.storage.put_json(dict(raw), kind="job-snapshot")
    context.written_object_keys.append(str(artifact_data["object_key"]))
    artifact = StoredArtifact(
        id=str(artifact_data["id"]),
        # Tenant scope is enforced through the owning job posting. Keeping the raw source
        # artifact unowned stops profile deletion from orphaning job evidence; deleting a
        # private source's data belongs to source/account deletion.
        owner_user_id=None,
        kind="job-snapshot",
        object_key=str(artifact_data["object_key"]),
        content_type="application/json",
        size_bytes=int(artifact_data["size_bytes"]),
        sha256=str(artifact_data["sha256"]),
    )
    db.add(artifact)
    # No ORM relationship links these rows, so flush the artifact first; otherwise the
    # unit of work may insert the snapshot first and violate the FK on PostgreSQL.
    db.flush()
    snapshot = JobSnapshot(
        job_id=job.id,
        version=version,
        content_hash=normalized.content_hash,
        raw_content_hash=normalized.raw_content_hash,
        raw_artifact_id=artifact.id,
        normalized_json=normalized.content,
        normalizer_version=NORMALIZER_VERSION,
        normalization_warnings=list(normalized.warnings),
        connector_id=connector.id,
        source_run_id=context.run.id,
        provenance_json={
            "connector_id": connector.id,
            "connector_name": connector.name,
            "provider_kind": context.descriptor.kind,
            "provider_version": context.descriptor.version,
            "normalizer_version": NORMALIZER_VERSION,
            "authorization_basis": context.decision.basis,
            "terms_version": context.decision.terms_version,
            "source_run_id": context.run.id,
            "page_index": page_index,
            "listing_url": page_url,
            "provider_api_version": context.page_api_versions.get(page_index),
            "source_job_id": normalized.source_job_id,
            "raw_content_hash": normalized.raw_content_hash,
            "fetched_at": context.now.isoformat(),
        },
        retrieved_at=context.now,
    )
    db.add(snapshot)
    db.flush()
    return snapshot


def _transition(
    db: Session, context: _RunContext, job: JobPosting, status: str, reason: str
) -> None:
    previous = job.status
    job.status = status
    job.status_reason = reason
    job.status_changed_at = context.now
    job.closed_at = context.now if status == "closed" else None
    record_audit(
        db,
        action="job.lifecycle_changed",
        actor=context.actor,
        user_id=job.owner_user_id,
        object_type="job_posting",
        object_id=job.id,
        metadata={
            "from": previous,
            "to": status,
            "reason": reason,
            "source_run_id": context.run.id,
        },
    )


def _audit_job(
    db: Session,
    context: _RunContext,
    job: JobPosting,
    action: str,
    snapshot: JobSnapshot,
    extra: dict[str, Any] | None = None,
) -> None:
    record_audit(
        db,
        action=action,
        actor=context.actor,
        user_id=job.owner_user_id,
        object_type="job_posting",
        object_id=job.id,
        metadata={
            "connector_id": context.connector.id,
            "source_job_id": job.source_job_id,
            "snapshot_id": snapshot.id,
            "snapshot_version": snapshot.version,
            "content_hash": snapshot.content_hash,
            "source_run_id": context.run.id,
            "normalization_warnings": list(snapshot.normalization_warnings),
            **(extra or {}),
        },
    )


def _reconcile(db: Session, context: _RunContext, job: JobPosting, stats: dict[str, Any]) -> None:
    for posting, change, reason in reconcile_duplicate_link(db, job):
        stats["duplicates_linked" if change == "linked" else "duplicates_unlinked"] += 1
        record_audit(
            db,
            action=f"job.duplicate_{change}",
            actor=context.actor,
            user_id=posting.owner_user_id,
            object_type="job_posting",
            object_id=posting.id,
            metadata={
                "duplicate_of_job_id": posting.duplicate_of_job_id,
                "reason": reason,
                "source_run_id": context.run.id,
            },
        )
