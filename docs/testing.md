# Testing and evaluation strategy

Status: Phase 4 validation and readiness hardening complete in the local/fixture scope, on PostgreSQL 17 and SQLite. Phase 3 validation also remains complete. PDF and DOCX PNG visual QA was completed with `pypdfium2` 4.30.0; DOCX conversion used LibreOffice 26.8.0 `soffice`.

## Quality gates by phase

### Data and profile

- Schema and migration tests for every table and constraint.
- Upload tests for file type/size/malware handling and document parsing failures.
- Authorization tests for every resource, including cross-user IDs and support-role boundaries.
- Versioning tests prove current edits do not alter historical snapshots.

### Sources and ingestion

- Connector contract tests against sanitized fixtures for Greenhouse/Lever/Ashby or other approved sources.
- Pagination, rate-limit, timeout, malformed payload, stale/closed job, and duplicate-ingestion tests.
- Terms/policy test: disabled or Tier C connectors cannot fetch or submit.
- Snapshot hash and provenance tests make replay deterministic.
- Phase 4 automated coverage (`backend/tests/test_phase4_discovery.py`): new, duplicate, in-response duplicate, updated (including revert), explicitly closed, stale/reappearing, and empty-listing jobs; missing/ambiguous fields staying unknown; required-field rejection; URL host and denylist rejection; invalid provider responses and pagination loops; provider failure health transitions and circuit breaker; redacted unexpected errors; persistence rollback with artifact cleanup; task retry with backoff and non-retry of policy failures; idempotency-key replay; single active run and abandoned-run takeover; rate limits; every authorization failure mode (disabled, unapproved, Tier C/blocked, missing/incomplete/expired authorization, non-permitted basis, terms change, denylisted domains, unknown provider, network egress disabled); provenance; encrypted raw snapshots; cross-source dedup and unlinking; untrusted job text and provider config tampering; operator/owner ingestion permissions; and tenant isolation of private sources across list/detail/snapshot/analysis/match/connector/run/ingest endpoints.

### Job analysis and matching

Phase 2 automated coverage includes fixture evidence bounds, deterministic extraction replay, the versioned sanitized benchmark in `backend/benchmarks/matching_v1.json`, unknown-authorization fail-closed behavior, confirmed-fact-only skill fit, separate preference recomputation, persisted match records, human review evidence/resolution/rerun behavior, and cross-user match isolation.

- Review regression tests prove that a human resolution cannot create a verified fact, an eligibility gate cannot be overridden by preference score, and a resolved rerun retains the original profile and job snapshots.

- Golden extraction set labeled by humans; test required/preferred/unknown, remote geography, sponsorship, compensation units, seniority, and conflicting statements.
- Property tests: unknown authorization never becomes eligible; preference changes do not change eligibility; score bounds are stable; same inputs/version replay identically.
- Held-out ranking evaluation using precision@k, recall@k, NDCG, calibration, and candidate correction rate. Thresholds are set after labeling.
- Fairness/robustness checks across language, sparse profiles, job families, location variants, and seniority.
- Prompt-injection fixtures in titles, descriptions, HTML, PDFs, and custom questions.

### CV engine

- Phase 3 automated coverage includes confirmed-fact-only ledger assembly, job-relevant evidence selection, deliberate hallucination rejection, unsupported-claim rejection, immutable artifact hashing, tenant isolation, approval/rejection transitions, and DOCX/PDF text extraction.
- Fact-grounded generation tests with omissions, conflicting dates, unsupported metrics, synonymous skills, and adversarial job text.
- Claim validator tests reject unsupported claims and require confirmation for ambiguous claims.
- Render tests extract text, inspect PDF and DOCX visual samples, verify encoding and page boundaries, and structurally validate DOCX packages. The prescribed DOCX accessibility audit reports zero findings on representative compact and multi-page samples.
- Snapshot/artifact hash tests prove immutable versioning and exact application binding.

### Application adapters

- Use local dummy ATS pages and provider sandboxes only; never submit to a live employer in CI.
- Contract tests for field mapping, file upload, requiredness, radio/select/checkbox semantics, redirects, and confirmation signals.
- Browser tests for slow pages, SPA transitions, duplicate clicks, crashes, retries, unexpected fields, expired approval, CAPTCHA/MFA, anti-bot, and outside-origin navigation.
- Verify no adapter can access arbitrary URLs, secrets, or files outside its job context.
- Human approval tests require a distinct action and fail closed on missing/changed review hash.

### Security and privacy

- SAST, dependency/SBOM/license scanning, secret scanning, container scanning, DAST, API fuzzing, SSRF/open-redirect tests, and rate-limit tests.
- Red-team LLM tests based on OWASP risks: direct/indirect prompt injection, sensitive-data disclosure, insecure output handling, excessive agency, vector poisoning, misinformation, supply chain, and unbounded consumption.
- Retention, export, deletion, backup, restore, and vendor deletion-request tests.
- Logging tests assert that CV text, tokens, cookies, passwords, and full answers do not appear.

### Phase 5A network and scheduler tests

`tests/test_phase5a_network.py` covers:

- allowed and denied hosts, and the two-list allowlist intersection;
- 23 malicious URL and hostname forms;
- 27 non-public address classes, and mixed DNS answers;
- DNS rebinding between check and connect, pinned connections, and transport wiring;
- proxy environment isolation;
- allowed, unsafe, private-target, and looping redirects;
- connect, read, and pool timeouts, the total deadline on slow streams, and DNS timeout;
- declared, streamed, and gzip-bomb oversize responses;
- invalid content, status classification, and the request budget;
- credential references, cross-host credential stripping, and redaction in logs, errors, and audits;
- over-broad allowlist rejection and the static "no provider sockets" guard;
- pipeline runs that are disabled, not allowlisted, carry inline secrets, or lack credentials;
- successful audited runs that never fetch job URLs;
- blocking, non-retried egress denial during a task;
- degrade and recover health transitions, and tenant isolation.

`tests/test_phase5a_scheduler.py` covers:

- schedule validation and audit, one task per slot, and overlap skipping;
- suspended and blocked sources never being re-enabled automatically;
- disabled or unapproved connectors, and retry without stacking;
- health states, overdue alerts, and tenant-scoped health API and CLI;
- circuit-breaker transitions, and concurrent ticks on PostgreSQL.

No test performs real network I/O or DNS.

### Phase 5B Ashby tests and live smoke test

`tests/test_phase5b_ashby.py` (sanitized fixtures, MockTransport, no network) covers:

- field mapping and unknowns;
- remote-address handling, HTML-to-text, ambiguous compensation, contradictory flags, and identity rules;
- exact endpoint binding (9 misconfigurations) and exact path matching;
- registration and the kill switch, and default-disabled behavior;
- the full pipeline with malformed, incomplete, duplicate, off-board, identity-conflict, non-object, and unlisted records;
- provenance, the original payload, and hashes;
- idempotent re-runs; changed, removed (stale), and reappearing postings with material-change classification; empty listings;
- failure classification (invalid JSON, non-object, HTML, 503, 403, off-board redirects);
- oversized responses and cross-connector dedup;
- tenant isolation and owner-attributed audits;
- secrets absent from logs, audits, and stored records;
- scheduler and worker execution with rate-limit enforcement, the admin CLI, and suppression of HTTP-library URL logging.

**Live read-only smoke test (2026-09-30), Ashby board `Ashby`, approved by the product owner under the `official_api` basis.**

Setup:

- disposable PostgreSQL 17 database and scratch artifact store;
- `SOURCE_NETWORK_ACCESS_ENABLED=true` and `SOURCE_EGRESS_ALLOWLIST=api.ashbyhq.com`, set in that process only;
- a connector registered through the CLI, with only one connector present;
- real DNS, the DNS-pinning transport, and TLS verification.

| Run | Path | Outcome |
|---|---|---|
| 1 | scheduler tick → worker (production path) | HTTP 200, 2.17 MB, 67 postings created, 0 rejected, 67 v1 snapshots |
| — | manual run 18 s before the 5-minute interval | refused `rate_limited` before any request |
| 2 | recorded transport around the real pinned transport | 67 unchanged, no new snapshots (idempotent) |
| 3 | real fetch with one title altered in memory | 1 updated to v2, classified as a material change (`title`), 66 unchanged |
| — | after `set-connector-enabled --disable` | refused `connector_disabled` before any request |

Verified:

- 68/68 snapshots re-verified their content hash and raw hash from the decrypted artifacts.
- Provenance recorded API version `1`, the listing URL, basis, terms version, run, and fetch time.
- Ashby's undocumented `id` was present and equal to the `jobUrl` UUID for 67/67 postings.
- All 67 postings were remote: none received a location country or remote geography, and sponsorship, relocation, and permitted countries stayed unknown. All carried an explicit currency.
- The only outbound request was an anonymous `GET` with an empty body and the headers `host`, `accept`, `accept-encoding: identity`, `user-agent`; no credentials and no candidate or application data.
- Audits: 3 network events (host and path digest only), run start/completion, `job.created` ×67, and one `job.updated`.
- No deployment secret appeared in audit metadata.

### Phase 5C application review package tests

`tests/test_phase5c_applications.py` uses sanitized fixtures only and makes no network calls. It covers:

- **Package contents.** Exact bindings: snapshot, content hash, match, CV with PDF/DOCX SHA-256, profile, preferences, source policy, and provider version. Eligibility, fit, and preference stay separate. Also covered: the link-out destination and the data-sharing notice.
- **Answers.** Answers come only from confirmed facts. Unconfirmed facts (`fact_unconfirmed`), conflicting facts (`conflicting_facts`), and missing sponsorship stay unresolved and block approval. A fact about another country never answers `no`. Unknown or ambiguous job geography leaves both authorization questions unresolved even for an eligible match. Optional questions with no fact are `left_blank`. Salary is never filled from preferences.
- **Candidate entry.** Values are validated: yes/no, email, length, control characters, unknown question keys. Each change creates a new revision and revokes the earlier approval. Entered values never appear in timeline or audit metadata.
- **Approval.** It requires the exact displayed hash (`hash_mismatch` otherwise). Approval is idempotent under double clicks and repeated prepares. It expires after 24 hours, can be revoked, and is revoked by skipping. Preparing again reopens a skipped application.
- **Binding drift.** Each of these marks the package stale and invalidates the approval: a material job change (the CV is also superseded), a non-material snapshot change (rebuilt against the new snapshot with the same CV), preference change, profile change, connector disabled, posting closed, a tampered package (`package_integrity_failed`), and a linked cross-source posting with another open application (`duplicate_suspected`, which clears once one application is skipped).
- **Preconditions.** Building refuses an unapproved CV, a missing CV or job, changed preferences, or an inactive posting, and nothing is persisted.
- **Prompt injection.** Job text cannot change answers or the destination.
- **Tenant isolation.** Covered for every endpoint.
- **Export and deletion.** Export includes all revisions, approvals, and events. Profile deletion and private-source deletion remove applications.
- **Ashby path.** A package for an Ashby job (mock transport) resolves authorization for the explicit US address country and links out to `jobs.ashbyhq.com`. Building, reading, and approving make no outbound request.
- **Nothing can be sent.**
  - A static AST check confirms the application modules import no HTTP, socket, TLS, SMTP, or browser library, and nothing from the outbound client, task queue, or worker.
  - The OpenAPI surface has no submit, send, or apply route, and the application routes are exactly the seven reviewed ones.
  - The database check constraint rejects a `submitted` status.
  - No task is ever enqueued.
- **Concurrency (PostgreSQL).** Six simultaneous prepares create one application and one revision. Six simultaneous approvals create one approval.

Evidence (2026-10-03):

- 267 tests pass on PostgreSQL 17 (disposable container, schema built by migrations, none skipped). The SQLite suite passes with the PostgreSQL-only tests skipped.
- Alembic upgrade → downgrade base → upgrade, and `alembic check` (no drift), pass on both databases, including `0009_application_review_package`.
- `ruff`, `mypy`, and `compileall` are clean.
- The frontend passes `npm run build` (type-check and bundle), and `npm audit` reports 0 vulnerabilities.
- `pip-audit` was not run, because it is not installed in this environment and no Python dependency changed.

### Live-access deployment controls

`tests/test_live_access_deployment.py` makes no network calls. It covers:

- **Configuration.** Outside development and test, enabling network access without `SOURCE_EGRESS_PROXY_URL` fails startup. Malformed proxy URLs are rejected (wrong scheme, no port, credentials, path, query). Alert settings must come as a pair and pin one exact host.
- **Proxy tunnel.** The only TCP connection goes to the proxy. The target appears only in `CONNECT host:443` and TLS SNI. Path and query travel only inside TLS. The backend refuses any other host or port. The transport re-checks scheme, port, and host even if the guard is bypassed. Guard checks still apply without local DNS. The pipeline picks the proxy transport from settings. A wiring assertion catches an httpx/httpcore upgrade that drops the proxy-only backend.
- **File secrets.** Resolution, trailing-newline handling, and rotation without restart. Traversal, absolute paths, hidden, uppercase, nested, missing, empty, oversized, multi-line, directory, and symlink-escape references are refused (the symlink case needs a host that permits symlinks). `env:` references are refused outside development and test.
- **Run loops.** The stop signal interrupts waits. The worker drains in batch mode, polls in loop mode until stopped, and processes nothing after a stop. A scheduler tick survives an alerting failure without logging the webhook URL.
- **Alerts.** Fire once, deduplicate, remind after 6 hours, re-fire on a state change, resolve once on recovery or disable. Private-source names are withheld. Failed deliveries are audited and retried, and the webhook URL and token never reach the audit log or logs. Host pinning holds, redirects are not followed, alerting stays off unless configured, and a missing secret fails closed.

**Egress proxy verification (manual, Docker).** `deploy/egress-proxy/verify.py` runs inside a container attached only to the internal network. It checks that there is no direct route and no external DNS. It checks that the proxy denies non-allowlisted hosts, port 80, IP literals, the proxy itself as a target, plain HTTP forwarding, the apex of a wildcard entry, and an allowlisted host that resolves to a non-public address. Finally it checks that TLS through the tunnel verifies end to end. Run it against every deployed environment before turning on network access. Results on 2026-11-13 (Squid 6.12, Alpine 3.22, Docker 29.3):

- Isolated test network: 12/12 checks passed. Proxy logs confirmed each denial (`TCP_DENIED/403`) and the allowed tunnel (`TCP_TUNNEL/200`). Wildcard subdomains, case variants, and trailing dots were allowed. Look-alike hosts (`evil-example.com`, `example.com.evil.net`) and clients outside `EGRESS_CLIENT_CIDR` were refused. Invalid allowlist entries (`*`, `*.com`, IPs, spaces, a leading dot) stop the proxy at startup with exit code 64. An empty allowlist denies everything.
- Full reference deployment (`compose.yaml` profile `app`, `APP_ENV=production`, isolated project and volumes): migrations ran to head and the API reported ready. A scheduled fixture connector was enqueued by the scheduler and run by the worker. From inside the worker, the application's own client tunneled through the proxy (`TCP_TUNNEL/200` from the worker address). Non-allowlisted hosts and plain HTTP were refused by the application before reaching the proxy, and direct connections failed. `verify.py` from the worker passed 10/10. `docker compose stop` ended the worker and scheduler in under a second with exit code 0.
- File secrets on Linux (backend image): a Kubernetes-style projected symlink (`name -> ..data/name`) resolved, and a symlink escaping `SECRETS_DIR` was refused.

### Phase 6 application tracker tests

`tests/test_application_tracker.py` makes no network calls. It covers:

- marking applied binds the current package revision and hash, copies company, role, and link from the package, and records `marked_applied`;
- once applied, rebuild, answer edits, approval, skip, and a second mark are refused with `already_applied`; removing the entry restores them (`unmarked_applied`);
- skipped and other candidates' applications cannot be marked;
- manual entries: listing (newest first), stage and text filters, and a summary that ignores filters;
- duplicates (case- and whitespace-insensitive company and role, or the same link) are refused with the existing ID unless confirmed;
- validation (blank, control characters, non-http(s) or credentialed links, future or pre-2000 dates, unknown stages, oversized notes, unknown fields);
- locked fields on `link_out` entries, and audit events that carry field names but never company, role, or notes;
- tenant isolation and CSRF;
- export and deletion with the profile; private-source deletion keeps the entry but unlinks it;
- a static check that the tracker imports no network library.

The route inventory in `tests/test_phase5c_applications.py` lists `POST /applications/{id}/applied` explicitly and still forbids any submit or send route.

Manual browser check (2026-11-13, Chrome on Windows, local throwaway database): the tracker summary, stage chips, search, inline stage change, adding an entry, the duplicate warning ("Add it anyway" or "Go back"), and "Mark as applied" on a review package (with the tracker refreshing and the package showing as frozen) all worked with no console errors. This check found and fixed two layout issues (label alignment, column widths shifting under filters) and a form that kept a cancelled draft.

### Running against PostgreSQL

The suite defaults to in-memory SQLite. Set `TEST_DATABASE_URL` to a **disposable** PostgreSQL database, for example `postgresql+psycopg://<user>:<password>@127.0.0.1:<port>/<db>`. In that mode the test run:

- drops and recreates the `public` schema;
- builds the schema with the Alembic migrations, not `create_all`;
- runs every test, including the thread-based concurrency tests in `tests/test_concurrency.py`, which are skipped on SQLite.

Never point it at a database with data you need.

Release checks for backend changes:

- `pytest` on SQLite and on PostgreSQL;
- `ruff check app migrations tests`, `mypy app`, and `compileall`;
- Alembic upgrade → downgrade base → upgrade, and `alembic check`, on both databases;
- `pip-audit` on the installed dependency set.

Frontend checks: `npm run lint`, `npm run build`, and `npm audit`.

## Production observability tests

Track, with privacy-safe aggregation:

- source freshness, connector success/blocked/terms-change rate;
- parse confidence and review rate;
- eligibility unknown/ineligible rates by reason;
- match precision@k and candidate correction rate;
- CV validation rejection and human edit rate;
- approval-to-submission conversion, adapter failure, duplicate prevention, and verified confirmation rate;
- model cost/latency/token usage and circuit-breaker events;
- data-access anomalies and security incidents.

Do not optimize for application volume alone. False positives, fabricated claims, duplicate submissions, and policy violations are higher-severity failures than low throughput.

## Release gates

No phase advances unless:

- acceptance criteria in the roadmap pass with evidence;
- security/privacy review is signed off for the new data flow;
- source and destination authorization is documented;
- unresolved critical/high findings have owners and a mitigation decision;
- rollback/kill switch and incident runbook are tested;
- candidate pilot feedback is incorporated into the next policy version.
