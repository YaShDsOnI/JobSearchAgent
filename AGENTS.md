# JobFinderAgent contributor guide

## Current project phase

This repository is in Phase 6 implementation (approved 2026-11-13). The canonical design is in `docs/`. Phases 1-3 (profile, explainable matching, factual CV drafting), Phase 4 (provider-agnostic discovery pipeline with local fixture providers; see `docs/source-connectors.md`), Phase 5A (live integration security foundation; live network access disabled by default), Phase 5B (one read-only live provider, the Ashby public Job Posting API, `ashby_job_board`), the live-access deployment controls (CONNECT-only egress proxy and internal network, `file:` secret references, health alert routing, reference deployment), and Phase 5C (immutable, hashed application review package with candidate approval) remain in place. Phase 6 adds the application tracker and a browser adapter (Playwright) whose **only destination is the local sandbox ATS** (`backend/sandbox_ats/`, loopback only, not a real employer), with a two-step fill-then-confirm flow, kill switches checked immediately before the single submit click, and pauses (never bypasses) on CAPTCHA, MFA, unknown fields, attestations, dialogs, and off-site navigation (see `docs/application-agent.md`). Do not add a real application destination, further live providers, scraping, model calls, or unattended or batch submission without a new approval and, for a destination, its written authorization.

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
