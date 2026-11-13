# Phased implementation roadmap

Status: Phase 5C (application review package, no submission) implemented on top of Phase 5B (one authorized read-only live provider, Ashby), Phase 5A (live integration security foundation) and Phase 4 (job discovery and source integration foundation), which was implemented and readiness-hardened in the local/fixture scope, validated on PostgreSQL 17 and SQLite; dates are intentionally not fixed.

## Decision record: phase re-sequencing (2026-09-29)

The product owner re-sequenced the roadmap. Scope, acceptance criteria, and prohibitions of every moved phase are unchanged; only their order changed and one preparation-only phase was split out.

| Phase | Before | After |
|---|---|---|
| 4 | One permitted application destination | Job discovery and source integration foundation |
| 5 | Private beta hardening | First authorized live integration and application preparation (no submission) |
| 6 | Expansion by evidence | One permitted application destination (submission adapter) |
| 7 | — | Private beta hardening |
| 8 | — | Expansion only by evidence |

Rationale:

1. **Dependency order.** The architecture's main workflow runs connector → snapshot → analysis → match → CV → approval → submission. Before this change the roadmap put a submission adapter ahead of any real, authorized job source, so an application would have been bound to fixture jobs only. The discovery pipeline is the input to every later phase.
2. **Provenance binding.** An application must record the exact source connector, job snapshot, and policy versions (database design, "Versioning and reproducibility"). Those identities only exist once the discovery pipeline does, so it has to come first.
3. **Risk isolation.** Read-only discovery from one authorized source is the lowest-risk way to touch live data. It exercises the egress, authorization, rate-limit, and kill-switch controls before any write action (submission) exists. Submission is the highest-harm action and stays in its own later phase with its own approval.
4. **Preparation before submission.** Building the review package (job, eligibility, fit, CV, proposed answers, destination) for a live job is separable from sending it. It lets the candidate-review and approval-hash model be validated without any outbound write.

No live source connector is enabled. The network egress gate `SOURCE_NETWORK_ACCESS_ENABLED` defaults to `false`.

Implementation note (2026-09-25): Phase 1 foundation is implemented in `backend/` and `frontend/`. Because no live source authorization is recorded yet, the catalog path is limited to sanitized local fixtures; live connectors remain disabled pending the Phase 0 source decision.

Implementation note (2026-09-25): Phase 2 uses only the local fixture catalog and deterministic rule-based extraction/matching. Production-only hardening remains pending: KMS/object storage, malware and decompression scanning, MFA/step-up authentication, rate limiting, backup/vendor deletion exercises, live feed authorization, production identity-provider integration, and dedicated replay infrastructure.

## Phase 0 — decisions and controls (current; no application code)

Deliverables: this documentation set, source policy, threat model, data inventory, legal/privacy review brief, provider/model decision record, and pilot success metrics.

Acceptance criteria:

- Product owner approves human-approval-only scope and prohibited actions.
- At least one permitted discovery source and one permitted application destination are identified in writing.
- Data controller/fiduciary, hosting regions, model/data-processing terms, retention, deletion, and support-access policy are decided.
- Candidate fact schema, eligibility policy, match policy, CV claim policy, application state machine, and audit event taxonomy are approved.
- Test fixtures and a labeled matching benchmark are prepared without real candidate secrets.

## Phase 1 — safe foundation and read-only catalog

Build only identity/profile/preference versioning, consent, encrypted artifact storage, source connector registry, one or two approved read-only feeds, job normalization, deduplication, and dashboard review. No CV generation or submission.

Acceptance criteria:

- A candidate can upload, confirm, correct, export, and delete profile data.
- Each job has provenance, snapshot, lifecycle, and dedup behavior.
- Disabled/Tier C sources cannot be accessed by workers.
- Cross-tenant and secret/logging tests pass.
- Replay of a job/profile snapshot is deterministic.

## Phase 2 — explainable matching

Add schema-constrained extraction, evidence spans, eligibility policy, fit/preference scoring, review queues, and candidate corrections. Start with lexical/rule-based matching; evaluate embeddings before adding them.

Acceptance criteria:

- Unknown authorization and remote geography never silently pass hard gates.
- UI/API show independent eligibility, fit, and preference outputs.
- Golden extraction and held-out ranking evaluation meet approved thresholds.
- Every score has evidence and policy version.

## Phase 3 — factual CV drafting

Add fact-ledger assembly, structured CV outline generation, claim validation, rendering, immutable versions, and human preview/approval. CVs remain local artifacts; no application submission.

Acceptance criteria:

- Deliberate hallucination fixtures are rejected.
- Every accepted claim has fact provenance.
- Candidate edits create new versions and no approved artifact changes.
- Rendered PDF is selectable, accessible, and visually reviewed.

## Phase 4 — job discovery and source integration foundation

Build a provider-agnostic discovery pipeline (Provider → Fetch → Validate → Normalize → Deduplicate → Snapshot → Persist → Audit) with a source registry, fail-closed authorization/policy gate, structured errors, idempotent and retry-safe runs, source health, lifecycle, provenance, and tenant-scoped private sources. Only local fixture providers are registered; see `docs/source-connectors.md`.

Acceptance criteria:

- A connector without a complete, current, permitted authorization record (or disabled, not approved, Tier C/blocked, denylisted domain, unknown provider, network access disabled) fails closed before the provider is called, and the refusal is recorded and audited.
- Unknown job attributes stay unknown; work authorization, remote geography, salary, sponsorship, and relocation are never inferred.
- Re-ingesting identical data creates no new snapshot; changed data creates the next immutable snapshot version with provenance.
- The same job from multiple sources is linked conservatively without merging or losing provenance; tenant-private postings never join another scope.
- Explicitly closed and absent-from-complete-listing postings change lifecycle without deleting history.
- Invalid responses, provider failures, retries, duplicate task delivery, and concurrent runs are handled without duplicate writes; retry classification is bounded.
- Source health and recent runs are visible; a circuit breaker suspends repeatedly failing sources until reset.

## Phase 5 — first authorized live integration and application preparation

Phase 5 is delivered in steps:

- **5A (implemented).** Live-integration security foundation: egress allowlist, SSRF/DNS-rebinding protection, redirect and size/time limits, credential references and redaction, scheduler foundation, health monitoring, and live-activity audit. No live provider is connected.
- **5B (implemented).** One authorized read-only provider: Ashby's public Job Posting API, board `Ashby`, product-owner approval 2026-09-30. Validated with a live read-only smoke test.
- **5C (implemented).** Application review package (no submission). Product-owner approval 2026-10-03. Design: `docs/application-agent.md`, "Phase 5C: review package implementation".

### Decision record: Phase 5C scope (2026-10-03)

The product owner approved Phase 5C with this scope:

- **In scope:**
  - build an immutable, hashed review package for one job, binding the exact snapshot, match run, CV version, profile and preference versions, and source policy;
  - a fixed question set filled only from confirmed facts or candidate entry;
  - a live binding check on every read;
  - candidate approve, revoke, and skip actions with a 24-hour approval expiry;
  - link-out to the posting URL, which the candidate opens themselves;
  - an append-only event timeline;
  - export and deletion coverage.
- **Out of scope:** any submission, upload, browser runner, destination adapter, form scraping, use of Ashby `applyUrl`, model-generated answers, and EEO/demographic or attestation questions.
- **Stored-data change:** new tables `applications`, `application_packages`, `approval_records`, and `application_events` (migration `0009_application_review_package`). Packages contain contact and authorization answers. They are highly sensitive and are deleted with the profile.

Remaining prerequisites before `SOURCE_NETWORK_ACCESS_ENABLED` may be turned on in any deployed environment (the 2026-09-30 smoke test enabled it only in a single disposable process):

- a written source authorization record for each board (done for board `Ashby`; every other board needs its own);
- network-layer egress enforcement (firewall or egress proxy) matching the allowlist, as defense in depth;
- a production secret manager behind the credential-reference interface;
- routing of `health-report` alerts and `source.health_changed` warnings to on-call;
- a deployed scheduler timer and worker;
- sanitized contract fixtures for the chosen provider (done: `backend/tests/fixtures/ashby/board_v1.json`).

Add exactly one live, read-only discovery provider behind the Phase 4 registry, and build the application review package for jobs from it. Nothing is submitted.

Entry conditions:

- A written authorization record exists for one specific source (for example, one employer's public ATS job board feed), with terms version, rate limits, fields collected, and failure modes documented in `docs/source-connectors.md`.
- An outbound egress allowlist, SSRF/DNS-rebinding/redirect controls, and request timeouts/size limits are in place before `SOURCE_NETWORK_ACCESS_ENABLED` is turned on.
- A scheduler and alerting on source health exist.

Acceptance criteria:

- The provider passes sanitized contract fixtures (pagination, malformed payloads, rate limits, timeouts, closed jobs) and fails closed without authorization.
- Disabling the connector or the global egress gate stops fetching immediately.
- The review package binds the exact job snapshot, match run, CV version, and connector/policy versions, and shows unknown or unconfirmed answers as unresolved.
- No endpoint, worker, or adapter can send an application.

## Phase 6 — one permitted application destination

Implement one provider-approved API or narrowly scoped application adapter using a sandbox/dummy site, with per-application approval, idempotency, pause states, exact artifact binding, and verification. Link-out is the fallback.

Acceptance criteria:

- No submission can occur without a live approval matching the review hash.
- CAPTCHA/MFA/unknown fields pause and never trigger bypass behavior.
- Crashes/retries/double clicks do not duplicate an application.
- Sent fields, CV, answers, destination, and confirmation are reconstructible.
- A kill switch disables the adapter immediately.

## Phase 7 — private beta hardening

Pilot with a small number of consenting users and a limited source/destination set. Measure match quality, factual edits, user effort, source stability, adapter failure, and harmful events. Add alerting, runbooks, backup/restore, support access controls, and cost budgets.

Acceptance criteria:

- No critical security/privacy issue is open.
- Pilot meets approved quality/safety thresholds without optimizing for volume.
- Deletion/export and incident exercises complete successfully.
- Legal review confirms the intended launch jurisdictions and disclosures.

## Phase 8 — expansion only by evidence

Add connectors one at a time after policy review, fixtures, terms/authorization, rate limits, observability, and rollback. Consider additional models, vector retrieval, multilingual support, and more ATS adapters only when the simpler design fails a measured need.

Explicitly not on the roadmap without a new approval: unattended auto-apply, CAPTCHA/MFA solving, prohibited-board automation, recruiter messaging, proxy/stealth evasion, protected-attribute matching, and fabricated or guessed screening answers.

## Phase 2 release-gate evidence

The local release gate includes deterministic benchmark cases in `backend/benchmarks/matching_v1.json`, automated expected-outcome evaluation, tenant-isolated human review endpoints/UI, evidence display, resolution audit events, and snapshot-preserving reruns. The benchmark is an internal regression dataset only; no real-world ranking or precision claim is made. A labeled held-out evaluation and its approved thresholds remain a later quality exercise before any CV-generation or consequential automation work.

## Phase 3 implementation note

Phase 3 uses deterministic template generation and local encrypted artifacts. No LLM, live source, browser automation, or application submission is enabled. Model-assisted drafting remains an optional future adapter behind the same schema and provenance validator.

## Phase 4 implementation note

Phase 4 registers only network-free fixture providers. Validation evidence:

- `backend/tests/test_phase4_discovery.py`: contract, policy, lifecycle, dedup, retry, idempotency, tenant isolation.
- `backend/tests/test_concurrency.py`: parallel run starts on the compare-and-set lock, idempotent enqueue, `SKIP LOCKED` task claims, redelivered tasks, and uniqueness constraints (PostgreSQL).
- `backend/tests/test_cv_invalidation.py` and `backend/tests/test_lifecycle_and_operators.py`.
- The full suite passes on PostgreSQL 17 (schema built by Alembic migrations) and on SQLite.
- Alembic upgrade → downgrade base → upgrade passes on both databases, and `alembic check` reports no drift on either.

Pre-Phase-4 snapshots are versioned as `legacy-v0`. Their first Phase 4 ingestion creates one re-normalized snapshot version. Because only material job changes supersede CVs (see `docs/cv-engine.md`), that re-normalization does not invalidate existing CVs.

## Recommended next implementation phase

Phase 5B is implemented: the read-only Ashby public Job Posting API is registered as the only live provider, covered by sanitized contract fixtures, and validated by a live read-only smoke test (see `docs/testing.md`). Live access stays disabled by default. The remaining deployment prerequisites above must be met before network access is turned on in any deployed environment. Phase 5C is implemented: an immutable, hashed review package per job with a live binding check, candidate approval (24-hour expiry), and a link-out destination. Nothing is sent. Evidence is in `docs/testing.md`. The next step is Phase 6, one permitted application destination (submission adapter). It needs its own written authorization for a specific destination and a new product-owner approval before work starts. Do not add further live providers or implement application submission or browser automation without that approval.
