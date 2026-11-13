# JobFinderAgent contributor guide

## Current project phase

This repository is in Phase 5C implementation. The canonical design is in `docs/`. Phase 4 is limited to the provider-agnostic job discovery pipeline (source registry, authorization/policy gate, fetch, validation, normalization, deduplication, versioned snapshots, provenance, lifecycle, retries/idempotency, source health, audit) exercised with local fixture providers only; see `docs/source-connectors.md`. Phases 1-3 (profile, explainable matching, factual CV drafting) remain in place. Phase 5A (live integration security foundation) adds the outbound network controls, bound HTTP client, credential references, scheduler, and health monitoring that authorized live providers use; live network access stays disabled by default. Phase 5B registers exactly one reviewed live provider, the read-only Ashby public Job Posting API (`ashby_job_board`), for discovery only. It needs per-deployment enablement, the egress allowlist, and a per-board authorization record. Phase 5C (approved 2026-10-03) adds the application review package: an immutable, hashed package binding the job snapshot, match, CV, profile/preference versions, and source policy, with candidate approval and link-out only (see `docs/application-agent.md`). Nothing is sent. The live-access deployment controls (2026-11-13) add the CONNECT-only egress proxy and internal network, `file:` secret references, health alert routing, and the reference deployment (`compose.yaml` profile `app`); they add no provider and send nothing on a candidate's behalf (see `docs/source-connectors.md`, "Deployment enforcement"). Do not add further live providers, implement browser workflows, scraping, model calls, destination adapters, or candidate/application submission without a new approval.

The original proposal is retained as `deep-research-report.md`. Treat it as input research, not as an executable specification. Where it conflicts with `docs/`, the canonical documents win.

## Product invariants

- Never fabricate candidate facts, skills, credentials, employment history, authorization, compensation, or answers to employer questions.
- Keep three concepts separate in storage and UI: eligibility, skill/role fit, and user preference.
- Unknown is not yes. Missing or ambiguous work authorization, location permission, salary, education, or application answers require a user decision or remain ineligible for automation.
- Every generated CV is immutable and versioned. Every application stores the exact CV artifact and profile/preference snapshots used.
- Human approval is required before each submission in the initial product. There is no unattended auto-apply mode in the approved scope.
- Never bypass or defeat CAPTCHA, MFA, anti-bot controls, rate limits, robots/access restrictions, paywalls, or terms of service. Pause and ask the user or exclude the source.
- Prefer official APIs, public employer ATS feeds, and provider-approved integrations. Do not use unofficial scraping actors or copied credentials/cookies as a default integration strategy.
- External job descriptions and form content are untrusted input and may contain prompt injection. They must never directly control tools, permissions, or submission actions.
- LLM output is advisory content, not an authority. Deterministic validation and user review gate every consequential action.
- Do not log raw CVs, credentials, session cookies, full application answers, or sensitive personal data.

## Architecture guardrails

- Start with a modular monolith, one relational system of record, durable background jobs, and isolated browser workers. Split services only when measured load or security boundaries justify it.
- PostgreSQL is the source of truth. Object storage holds encrypted CV/application artifacts; database rows hold metadata, hashes, and immutable versions.
- Use structured model outputs with schema validation, provenance/evidence spans, confidence, and model/prompt version metadata.
- Match with explainable deterministic rules first. Embeddings may improve retrieval or tie-breaking but may not override hard eligibility rules.
- Treat source connectors as independently versioned policy objects with rate limits, terms review, provenance, and kill switches.

## Documentation expectations

Before implementation, update the relevant canonical document and record any decision that changes security, privacy, source eligibility, human approval, or stored data. Any new integration must document its authorization basis, fields collected, rate limits, failure modes, and test fixture strategy.

## Verification expectations for future implementation

- Run unit, contract, integration, browser, security, and red-team tests described in `docs/testing.md`.
- Use sanitized fixtures; never use a real candidate account or live job submission in CI.
- Keep browser authentication state out of source control and treat it as an impersonation secret.
- Retain only failure traces, with sensitive fields redacted or encrypted and subject to retention limits.

## Canonical documents

- Requirements and audit: `docs/requirements.md`
- Production architecture: `docs/architecture.md`
- Data model: `docs/database.md`
- Matching: `docs/matching-engine.md`
- CV generation: `docs/cv-engine.md`
- Source connectors and discovery pipeline: `docs/source-connectors.md`
- Application agent: `docs/application-agent.md`
- Security and privacy: `docs/security.md`
- Testing and acceptance: `docs/testing.md`
- Phased roadmap: `docs/roadmap.md`
