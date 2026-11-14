# Security, privacy, and responsible AI

Status: control baseline; legal counsel must confirm jurisdictional obligations before production.

Phase 3 implementation note: CV DOCX/PDF artifacts are encrypted through the existing application-layer artifact store, referenced by hashes and owner-scoped metadata, and served only after authenticated object-level authorization. The initial generator does not send candidate data to an external model; generated claims are deterministically validated before storage and human approval.

Phase 4 implementation note: job sources are policy objects. A connector is fetched only with a complete, current authorization record whose basis is permitted for the provider and whose terms version matches; everything else fails closed before provider code runs. LinkedIn, Naukri, and Glassdoor domains are denylisted for every basis. Providers needing network access are additionally gated by `SOURCE_NETWORK_ACCESS_ENABLED` (default `false`) until an egress allowlist and SSRF controls exist; only local fixture providers are registered. Provider output is untrusted: it is structurally validated, size-limited, normalized without inference, cannot alter connector configuration, and job URLs must be HTTPS without embedded credentials on permitted hosts. Error details and audit metadata are redacted (codes and exception types only). Tenant-private sources and their postings are only visible to, and only runnable by, their owner.

Phase 4 readiness note:

- **Operator role.** It is granted only through `python -m app.admin promote-operator`, which needs deployment-level database access and a recorded reason, and writes a `user.role_changed` audit event. There is no HTTP endpoint for role changes, and the last active operator cannot be demoted.
- **Private-source deletion.** A private source and all data derived from it (postings, snapshots, encrypted raw blobs, analyses, matches, reviews, CVs and their artifacts, run history, queued tasks) can be deleted by its owner via `DELETE /connectors/{id}`. Rows are committed before blobs are removed, and any blob left behind is removed by `python -m app.admin reconcile-artifacts --apply`.
- **Dependency audits.** pip-audit and npm audit are part of the release checks.

Phase 5C note (application review package, no submission):

- Packages and candidate-entered answers hold contact and work-authorization data. They are owner-scoped, excluded from logs and audit metadata (only question keys, hashes, and IDs are recorded), included in export, and deleted with the profile.
- The application code has no network, socket, or browser capability. A static test enforces this. The link-out destination is shown to the candidate and never fetched by the backend.
- An approval binds the exact package hash, expires after 24 hours, and is re-evaluated against live inputs on every read. Nothing consumes an approval in 5C.
- Job text is untrusted. It is displayed and hashed, never used to choose questions, answers, URLs, or actions.

Phase 6 note (application tracker and sandbox browser adapter, approved 2026-11-13):

- The only adapter destination is the local sandbox ATS on loopback. Startup refuses a `SANDBOX_ATS_ORIGIN` that is not `127.0.0.1` or `localhost`, and no real employer can be targeted.
- Three kill switches, all read fresh, must allow a run, and they are checked again immediately before the single submit click:
  - `APPLICATION_SUBMISSION_ENABLED` (default `false`);
  - the database-backed destination switch;
  - a bounded attempt budget.
- A human makes two decisions: approving the hashed package, then confirming the filled form (`fill_hash`) after seeing exactly what the runner read back.
- The browser runner is a separate process. Its context is ephemeral, requests to any other origin are aborted, and it has no stored state, downloads, traces, or screenshots. The decrypted CV goes into a private temporary directory and is deleted after each run.
- CAPTCHA, MFA, unknown required fields, attestations, dialogs, and off-site navigation pause the run. Nothing is bypassed.
- A static test confirms that only `submission/browser.py` imports a browser library. The API process never loads it.
- The fill manifest holds the values that were sent. It is personal data, exported and deleted with the profile and never placed in audit metadata.
- The application tracker stores where the candidate applied. Audit events hold IDs, channel, stage, and field names only.

Phase 7 note (backup, restore, and runbooks, 2026-11-14): see "Backups" below and `docs/runbooks.md`.

Phase 5B note: the first live provider is Ashby's anonymous, read-only public Job Posting API.

- It is bound to one exact board path on `api.ashbyhq.com`. Any redirect elsewhere blocks the source.
- It sends no credentials and no candidate or application data.
- Payload URLs are never fetched.
- The live smoke test found that httpx logs full request URLs at INFO. The `httpx` and `httpcore` loggers are now capped at WARNING (`app/core/logging.py`), so paths and query strings never reach logs.

Phase 5A note (live integration security foundation):

- Outbound access is fail-closed: disabled by default, and when enabled it needs a host in both the deployment allowlist and the connector's reviewed host list. HTTPS only.
- Each hop is validated for SSRF, and the connection is pinned to the validated address so a DNS rebinding answer cannot be used.
- Redirects are re-validated. Timeouts and decoded-size limits are enforced.
- Credentials are environment references only, sent only to the addressed host, and redacted everywhere.
- Every outbound attempt and every denial is audited without paths, queries, headers, or bodies.
- This is application-layer enforcement. Network-layer enforcement is added by the deployment controls below.

Live-access deployment controls (2026-11-13; design in `docs/source-connectors.md`, "Deployment enforcement"):

- The worker and scheduler run on an internal-only network with no route out and no external DNS. Their only path out is a CONNECT-only egress proxy that allows port 443 on allowlisted hosts and refuses non-public destination addresses. Outside development and test, the application refuses to start with network access enabled and no proxy.
- In proxy mode the host allowlist is enforced by both the application and the proxy. The destination-address check moves to the proxy, and the application's network backend can connect only to the proxy. TLS runs end to end, so the proxy never sees paths, queries, headers, or bodies.
- Connector and alert secrets are `file:` references to secret-manager-projected files, with traversal and symlink escapes refused. Environment references are refused outside development and test.
- Health alerts go to one pinned webhook host, carry no candidate data or private-source names, and never log the webhook URL.
- Residual: the API container keeps a route out (it publishes the port) but never runs providers. The session secret and artifact key are still environment variables; moving them to files is a follow-up.

## Security objectives

Protect candidate identity, CVs, job-search history, authorization answers, application content, external account access, and browser sessions from unauthorized access, disclosure, alteration, duplicate submission, and unsafe automation.

## Data protection

- Maintain a data inventory and classification for every field and artifact.
- Collect only what is needed for discovery, matching, CV preparation, and a candidate-approved application.
- Obtain clear, purpose-specific consent/notice for profile ingestion, model processing, source access, application submission, analytics, and optional feedback.
- Record provider, region, retention, subprocessors, training-use settings, and deletion path for every external model/storage service.
- Use jurisdiction-aware privacy notices and legal review for India's DPDP Act/Rules, GDPR/UK GDPR where applicable, cross-border transfers, and employment-related AI obligations. Do not market the system as “GDPR compliant” based on vendor capabilities.
- Provide export, correction, withdrawal, deletion, and support/complaint paths appropriate to the applicable law and contract.

### Backups

Implemented in Phase 7 (`python -m app.backup`; operations in `docs/runbooks.md`, "Backup and restore").

- A backup is a full copy of personal data, so it gets the same protection as the primary stores.
  - The database dump is AES-GCM encrypted under a key derived from `BACKUP_ENCRYPTION_KEY`.
  - Blobs stay encrypted under `ARTIFACT_ENCRYPTION_KEY`.
  - The manifest holds no personal data and is MAC'd.
  - Restore refuses tampered or mismatched files.
- The backup key is separate from the artifact key, which the CLI enforces. Only the backup CLI and the compose `backup` service receive it. Both keys are stored apart from the backups.
- Backups are retained for 35 days, and the newest backup is always kept. Off-site copies follow the same retention. Off-site replication is not implemented yet, and its target needs product-owner approval for personal data.
- Deletion survives restore. Every restore is followed by replay of the deletion ledger, so a backup never brings back data the candidate deleted. The ledger holds only data request IDs, user IDs, and completion times. It is MAC'd with the backup key, because a forged entry would cause a deletion.
- Restore never merges or overwrites. It accepts only an empty, migrated target, commits only an exact read-back copy, and records a `backup.restored` audit event.
- CLI output and audit metadata hold IDs, counts, and hashes only.
- Known gap: there is no artifact key rotation or re-encryption tool yet (`docs/runbooks.md`, "Leaked secret or token").

## Identity and access

- Use an established identity provider with secure session cookies, CSRF protection, rate limiting, and step-up authentication for exporting/deleting data and approving applications.
- Enforce object-level authorization on every user-owned resource; test for IDOR/cross-tenant access.
- Separate candidate, support, developer, and operations roles. Support access is time-bound, audited, and redacted by default.
- Do not store third-party passwords. Prefer OAuth/provider authorization. If a permitted integration requires a token, store only a vault reference, narrow scopes, rotate/revoke, and display connection status without revealing the token.
- Treat Playwright storage state/cookies as credentials. Keep them ephemeral, encrypted, access-controlled, and out of source control.

## Application and infrastructure security

- TLS in transit; KMS-backed encryption at rest; envelope encryption for high-sensitivity fields.
- Private database/object storage network paths; deny public buckets; short-lived signed URLs.
- Strict outbound egress allowlist for source and model domains; protect against SSRF, DNS rebinding, open redirects, and arbitrary file downloads.
- Validate MIME type, size, decompression ratio, and malware scan uploads. Do not execute document content.
- Pin and scan dependencies/images; generate SBOM; apply least-privilege container users and filesystem permissions.
- Redact secrets, CV text, contact details, tokens, cookies, and full form answers from logs. Use correlation IDs and hashes where possible.
- Back up encrypted data, test restore, and define incident response, revocation, notification, and recovery procedures.

## LLM and agent security

Treat external job descriptions, web pages, form labels, uploaded documents, and model outputs as untrusted. The key risks map to OWASP's LLM guidance: prompt injection, sensitive information disclosure, insecure output handling, excessive agency, vector/embedding weaknesses, misinformation, supply-chain risk, and unbounded consumption: <https://genai.owasp.org/llm-top-10/>.

Controls:

- Data-only delimiters and structured input; never follow instructions found in a job description.
- Narrow tools with explicit schemas, destination allowlists, permission checks, budgets, timeouts, and human approval.
- Schema-validate and sanitize model output before any database write, document generation, or browser action.
- Deterministic policy engine remains authoritative for eligibility, authorization, and submission.
- Minimize profile data sent to models, use provider privacy controls, and provide a no-external-model option if feasible.
- Track prompt/model versions, evidence, output hashes, cost, latency, and validation failures.
- Test direct/indirect prompt injection, data exfiltration, malicious attachments, tool argument tampering, model denial of service, and overreliance.
- Add circuit breakers for abnormal token spend, task volume, browser actions, destination changes, and repeated failures.

## Responsible ranking and employment risk

The product is candidate-directed and must not make decisions about who an employer hires. Still, ranking and CV tailoring can materially affect opportunity. NIST's AI RMF recommends continuous governance, mapping, measurement, management, and context-appropriate human oversight; apply those practices to the candidate-facing system: <https://www.nist.gov/itl/ai-risk-management-framework>.

Do not use sensitive/protected attributes or proxies for fit. Evaluate disparate failure patterns across roles, locations, language, incomplete profiles, and seniority. Explain and allow correction of any score. Maintain human review for eligibility ambiguity and every submission.

EU legal review is required before offering the service into relevant EU contexts. The EU AI Act identifies certain recruitment/selection uses as high risk and includes human-oversight and documentation obligations; GDPR Article 22 addresses solely automated decisions with significant effects. Applicability depends on the concrete service and role: <https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32024R1689> and <https://eur-lex.europa.eu/legal-content/EN/ALL/?uri=uriserv:OJ.L_.2016.119.01.0001.01.POL>.

## Threat model priorities

| Threat | Impact | Primary controls |
|---|---|---|
| Account takeover | Profile/application compromise | Strong sessions, MFA/step-up, rate limits, anomaly detection |
| Cross-tenant access | Privacy breach | Object authorization, RLS, negative tests, support audit |
| Credential/session theft | External account takeover | OAuth, vault references, ephemeral browser contexts, no logs |
| Prompt injection | Data exfiltration or unsafe action | Untrusted-data boundary, tool allowlist, schema/policy gates |
| Fabricated CV/application answer | Reputational/legal harm | Fact ledger, provenance, validators, human approval |
| Duplicate/incorrect submission | Candidate harm | Idempotency, approval hash, verification, stop on ambiguity |
| Malicious source/file | Code/SSRF compromise | Egress allowlist, sandboxing, file validation, malware scanning |
| Source terms violation | Account/legal harm | Connector registry, terms review, kill switch, link-out fallback |
| Biased ranking | Opportunity harm | No sensitive features, evaluation, correction and review |

## Security acceptance criteria

- Threat model and data-flow diagram are reviewed before implementation.
- Secret scanning, dependency scanning, SAST, DAST, and authorization tests pass in CI.
- A simulated prompt injection cannot cause data access, tool escalation, arbitrary navigation, or submission.
- An audit proves a deleted user's personal artifacts are removed from primary stores and vendor deletion requests are tracked.
- Incident runbooks exist for leaked token, unintended submission, source-policy violation, model provider incident, and data export failure. They are in `docs/runbooks.md` (Phase 7). No incident exercise has been run yet.
