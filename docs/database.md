# Database design

Status: Phase 5A/5B schema implemented through Alembic migration `0008_connector_schedule` (Phase 5B added no schema changes; Ashby connectors use the existing connector, authorization, and snapshot tables); validated on PostgreSQL 17 and SQLite with no model/migration drift. Production hardening remains pending.

## Principles

- PostgreSQL is authoritative for identity, workflow state, versions, and audit metadata.
- Binary documents and raw source snapshots live in encrypted object storage; rows contain object keys, hashes, MIME type, size, and retention metadata.
- Every user-owned row carries `tenant_id`/`user_id` and is protected by service-layer authorization plus database row-level security where practical.
- Mutable user data is versioned when it can affect a historical decision. Historical records reference immutable snapshots, not the current profile.
- `NULL`/`unknown` means unknown. Do not collapse unknown to false or true.

## Core tables

| Table | Important fields | Invariants |
|---|---|---|
| `users` | id, email, status, created_at | Email identity is unique; account status gates access |
| `candidate_profiles` | user_id, current_version_id | One current pointer; profile history is immutable |
| `profile_versions` | id, user_id, source_document_id, normalized_json, created_at | Hash and schema version make a snapshot reproducible |
| `profile_facts` | id, profile_version_id, fact_type, value_json, provenance, source_ref, evidence_text, confirmation_status | Only confirmed facts can be used for matching; provenance/evidence remain candidate-supplied or source-linked |
| `preferences` | user_id, current_version_id | Candidate controls these values |
| `preference_versions` | id, user_id, rules_json, created_at | Match runs point to this snapshot |
| `consents` | user_id, purpose, scope, policy_version, granted_at, withdrawn_at | No processing beyond recorded purpose/scope |
| `source_connectors` | id, name, kind, tier, base_domain, terms_version, policy_status, enabled, owner_user_id, authorization_json, config_json, health_status, consecutive_failures, last_run/success/failure_at, last_error_code, active_run_id | Kill switch and source authorization are explicit; missing/incomplete authorization fails closed; `owner_user_id` NULL = shared catalog, otherwise tenant-private; `active_run_id` is a compare-and-set run lock |
| `source_connectors` schedule | schedule_interval_seconds, next_run_due_at | NULL interval = manual runs only; ticks advance `next_run_due_at` per slot |
| `source_runs` | id, connector_id, status (running/succeeded/partial/failed/blocked/abandoned), idempotency_key, attempt, requested_by_user_id, provider_version, authorization_basis, error_code, error_detail, stats, started_at, finished_at | Run telemetry excludes raw personal data and raw payloads; a succeeded/partial run replays for its idempotency key |
| `job_postings` | id, connector_id, owner_user_id, source_job_id, canonical_hash, title, employer, canonical_url, status (active/closed/stale), status_reason, status_changed_at, closed_at, url_fingerprint, content_fingerprint, duplicate_of_job_id, dedup_reason | Unique `(connector_id, source_job_id)`; cross-source duplicates are linked, never merged, and only within one tenant scope |
| `job_snapshots` | id, job_id, version, retrieved_at, content_hash, raw_content_hash, raw_artifact_id, normalized_json, normalizer_version, normalization_warnings, connector_id, source_run_id, provenance_json | Immutable source evidence; unique `(job_id, version)`; provenance is stored beside, not inside, the hashed content |
| `job_analyses` | id, job_snapshot_id, extractor_version, structured_json, confidence, status | Only validated schema enters matching |
| `job_evidence` | id, analysis_id, field_path, quote, start/end offsets, confidence | Evidence quote is bounded and source-linked |
| `skills` | id, canonical_name, taxonomy_version | Canonical vocabulary is versioned |
| `job_requirements` | id, analysis_id, skill_id/text, requirement_kind, importance, evidence_id | Required/preferred/informational are separate |
| `match_runs` | id, user_id, job_snapshot_id, profile_version_id, preference_version_id, policy_version, extractor_version, eligibility_status, fit_score, preference_score, scores_json | Immutable calculation record with separate breakdowns and reasons |
| `match_reviews` | id, user_id, match_run_id, status, reason_json, resolution_json, resolved_by_user_id, resolved_at, rerun_match_id | One review per needs-review match; evidence is copied by reference through reason IDs; resolution is audited and rerun is linked |
| `cv_versions` | id, user_id, job_id, job_snapshot_id, profile_version_id, match_run_id, generator/template versions, content_hash, docx/pdf artifact IDs, status, approval metadata | Immutable; exact profile/job/match inputs and rendered artifacts are retained; approved artifacts are never overwritten |
| `cv_versions` supersession | superseded_at, superseded_reason, superseded_detail_json | Records why a CV stopped being current; see `docs/cv-engine.md` |
| `cv_claims` | id, cv_version_id, section, text, source_fact_ids, job_requirement_ids, validation_status, validation_reason | Every material claim is supported by confirmed facts or explicitly rejected/unresolved |
| `applications` | id, user_id, job_id, cv_version_id, status, target_kind, approval_id, submitted_at | Exact CV and input snapshot are mandatory before submit |
| `application_answers` | id, application_id, question_key, question_text, answer_value, source_fact_ids, confirmed_by_user | No guessed answer; unknown remains unresolved |
| `application_events` | id, application_id, event_type, actor, metadata_json, occurred_at | Append-only audit timeline |
| `approval_records` | id, application_id, reviewed_payload_hash, approved_by, approved_at, expires_at, revoked_at | Approval binds to exact review package |
| `external_accounts` | id, user_id, provider, authorization_type, secret_ref, scopes, status | Store only a vault reference; never a password/cookie in DB |
| `tasks` | id, kind, idempotency_key, payload_ref, state, lease, attempts, next_run_at | Unique idempotency key prevents duplicate work |
| `audit_events` | id, user_id, actor, action, object_type/id, metadata_json, request_id, occurred_at | Redacted and append-only |
| `data_requests` | id, user_id, type, status, requested_at, completed_at | Export/deletion is trackable and repeatable |

## Sensitive fields

Highly sensitive: contact details, CV source documents, profile facts, authorization/visa answers, application answers, external tokens, browser state, and rendered documents. Encrypt at the application layer for the most sensitive values, use KMS-backed envelope keys, and never put them in URLs or logs.

Moderately sensitive: job history tied to a candidate, match explanations, source snapshots, and audit metadata. Apply access control and retention even when the source job text is public.

## Keys and indexing

- Use UUID/ULID primary keys; use provider IDs as scoped external identities, never as global primary keys.
- Unique index on `(connector_id, source_job_id)` where source IDs are stable.
- Unique index on a normalized application identity such as `(user_id, connector_id, source_job_id)` for submitted/active applications, with a review path for re-posted roles.
- Index job lifecycle/status, retrieval time, normalized location, work mode, employer, and match eligibility.
- Full-text search can begin with PostgreSQL. Add vector columns/`pgvector` only with an evaluated query and retention plan; do not create a separate vector database for the beta.

## Versioning and reproducibility

When an application starts, persist references to:

1. profile version and confirmed fact set;
2. preference version;
3. job snapshot and analysis version;
4. match policy/extractor/model versions;
5. CV version and artifact hash;
6. answers and their confirmation events;
7. connector and destination policy versions.

Deleting a user must remove or irreversibly anonymize personal data according to the approved retention/legal policy while preserving only legally necessary non-identifying operational evidence.

## Migration notes

- Revision IDs longer than Alembic's default `VARCHAR(32)` version column already exist and are never renamed. `migrations/env.py` creates or widens `alembic_version.version_num` to `VARCHAR(255)` on non-SQLite databases before migrating. Before this fix, the chain could not be applied to PostgreSQL at all.
- Models declare exactly what the migrations created: the `users.email` named unique constraint plus a non-unique index, no index on `stored_artifacts.sha256` (it is an integrity hash, never a lookup key), and `Float` for float columns regardless of SQLAlchemy version. `alembic check` must report no drift on PostgreSQL and SQLite.
- Rows that reference each other without an ORM relationship (artifact → snapshot, artifact → CV version) are flushed in dependency order, because PostgreSQL enforces foreign keys that SQLite ignores.

## Database acceptance criteria

- A historical application can be rendered with exactly the stored CV artifact and input snapshot.
- An unconfirmed fact cannot satisfy an application answer or CV claim constraint.
- Cross-user reads fail in authorization tests.
- Duplicate ingestion and duplicate task delivery are idempotent.
- Deletion/export exercises cover DB rows, object storage, search/vector indexes, backups, and vendor retention.
