# Source connectors and job discovery pipeline

Status: Phase 5B (2026-09-30). Registered providers: the bundled fixture and one reviewed live provider, `ashby_job_board` (Ashby's public posting API, read-only). Live access stays disabled by default and requires a per-deployment allowlist plus a per-connector authorization record. No live fetch happens unless a deployment explicitly enables it. Scraping, browser automation, and application submission are not implemented.

## Purpose

Phase 4 builds a provider-agnostic discovery pipeline so approved job sources can be added later without changing catalog, matching, or CV code. Every source is a versioned policy object: it declares how it is authorized, what it may fetch, how fast, and how it fails. A missing, incomplete, expired, or non-permitted authorization fails closed before any provider code runs.

## Pipeline

```
Provider registry → Policy/authorization gate → Fetch (bounded pages)
  → Validate (response + record structure) → Normalize (unknown stays unknown)
  → Deduplicate (source identity, then conservative URL/content fingerprints)
  → Snapshot (immutable, versioned, hashed, encrypted raw payload)
  → Persist (posting lifecycle + provenance) → Audit + source health
```

Implementation: `backend/app/services/sources/`.

| Module | Responsibility |
|---|---|
| `base.py` | `SourceProvider` protocol, `ProviderDescriptor`, `FetchRequest`, `FetchPage` |
| `registry.py` | `ProviderRegistry`; unknown provider kinds fail closed |
| `policy.py` | Authorization/terms/tier/domain/network/tenant/circuit/rate-limit gate |
| `fixture.py` | Bundled and static fixture providers (no network) |
| `validation.py` | Page and record structural validation, size limits |
| `normalize.py` | Deterministic normalization to `job-normalized-v1` |
| `dedup.py` | URL normalization and fingerprints; cross-source duplicate linking |
| `pipeline.py` | Orchestration, idempotency, run lock, snapshots, lifecycle, health, audit |
| `worker.py` | Durable-task integration with bounded, classified retries |
| `errors.py` | Structured, redacted error taxonomy with retry classification |

## Authorization and policy gate

A connector may be fetched only when **all** of the following hold. Each failure raises `SourcePolicyError` with a stable code, records a `blocked` run and audit event, and never calls the provider.

1. `enabled` is true (per-connector kill switch, effective without deployment).
2. `policy_status == "approved"`.
3. `tier` is `local`, `A`, or `B`. Tier `C` (link-out only) and `blocked` never permit worker access.
4. The registered provider kind equals `connector.kind`; unknown kinds fail closed.
5. `authorization_json` contains `basis`, `evidence_ref`, `reviewed_by`, `reviewed_at`, and a `terms_version` equal to `connector.terms_version`. A terms change therefore requires a new review.
6. `basis` is one of the permitted bases below **and** is accepted by that provider's descriptor.
7. `expires_at`, when present, is in the future.
8. `local_fixture` basis is only valid on tier `local` with a provider that needs no network, and tier `local` only accepts that basis.
9. Network providers require a `base_domain`, the global `SOURCE_NETWORK_ACCESS_ENABLED` setting (default `false`), and must not target a denylisted domain.
10. Denylisted domains (`linkedin.com`, `naukri.com`, `glassdoor.*`) are rejected for every basis in this phase, both as `base_domain` and as job URL hosts. Lifting this requires a written provider authorization and a new documented decision.
11. A tenant-owned connector may only be run for its owner (or by the system on the owner's behalf). A mismatch is rejected without touching the connector's health or run history.
12. A connector whose circuit breaker is open (`health_status == "suspended"`) is not fetched until an operator resets it.
13. Provider-declared minimum interval between runs (a connector may raise but not lower it) is enforced; violations raise a retryable `rate_limited` error.

Permitted authorization bases:

| Basis | Meaning |
|---|---|
| `official_api` | Provider's documented public/partner API with permitted use |
| `official_feed` | Provider's documented public job feed (e.g. employer ATS job board feed) |
| `provider_approved_integration` | Written approval from the provider for this integration |
| `permitted_career_page` | Employer career page with recorded permission/terms review (tier B) |
| `explicit_authorization` | Written authorization from the data owner for this specific source |
| `local_fixture` | Sanitized, bundled local data; no network |

Prohibited in all phases unless a new decision is recorded: scraping without an authorization basis, LinkedIn/Naukri/Glassdoor automation, CAPTCHA/anti-bot/access-control bypass, copied credentials or cookies, proxy rotation, and circumventing robots/terms restrictions.

## Normalized job schema (`job-normalized-v2`)

Normalization is deterministic and never infers. Absent or unparseable values stay unknown and a warning code is recorded on the snapshot.

| Field | Values | Unknown representation |
|---|---|---|
| `source_job_id`, `title`, `employer`, `canonical_url` | Required; record rejected if missing/invalid | — |
| `description` | Text (≤100k chars) | `null` |
| `location_text` | Verbatim text | `null` |
| `secondary_locations` (v2) | Verbatim additional location strings; never parsed | `[]` |
| `location_country` | ISO 3166-1 alpha-2 | `null` |
| `work_mode` | `remote`, `hybrid`, `onsite` | `unknown` (never derived from location text) |
| `remote_geography` | Explicit list of ISO country codes | `null` (never derived from "remote") |
| `permitted_countries` | Explicit list of ISO country codes | `[]` (matching treats as unknown) |
| `work_authorization_requirements` | Verbatim statements | `[]` |
| `sponsorship`, `relocation_assistance` | `available`, `not_available` | `unknown` |
| `compensation` | `min_amount`, `max_amount`, `currency` (ISO 4217), `period` | `null`; currency/period `null` when not stated; no conversions |
| `benefits`, `education` | Lists of strings | `[]` |
| `requirements` | Structured items consumed by the Phase 2 analyzer | `[]` |
| `minimum_experience_years` | Number ≥ 0 | `null` |
| `posted_at`, `updated_at`, `closing_at` | ISO date/datetime | `null` |
| `status` | `active`, `closed` (explicit source statement only) | Unrecognized status rejects the record |

Provenance is stored beside, not inside, the hashed content: connector, provider kind/version, normalizer version, authorization basis, terms version, run ID, page, listing URL, fetch time, and raw payload hash.

## Deduplication

1. **Source identity**: `(connector_id, source_job_id)` is the stable identity. Re-ingesting identical content creates no snapshot.
2. **Cross-source**: a posting from a different connector in the same tenant scope is linked (`duplicate_of_job_id`) to the earliest primary when the normalized canonical URL matches (tracking parameters, fragment, default port, and trailing slash removed) or when the casefolded employer + title + location + non-empty description match exactly.
3. Postings are never merged or deleted; each source keeps its own snapshots and provenance. Different `source_job_id`s from the same connector are never auto-linked. Links are re-evaluated when either side changes.
4. Tenant-private postings are only compared with postings of the same owner, so dedup cannot reveal another tenant's data.

## Snapshots and lifecycle

- Snapshots are immutable with a per-job monotonically increasing `version` (unique `(job_id, version)`). A content change (including a revert to earlier content) creates the next version. The raw record is stored encrypted as an artifact.
- Posting status: `active`, `closed` (source explicitly says closed), `stale` (absent from a *complete* listing). Stale/closed postings are never deleted. A reappearing or re-opened posting becomes `active`. An empty complete listing does not stale existing postings (treated as a probable provider fault).
- Existing Phase 3 behavior is preserved: a new snapshot supersedes validated/approved CV versions for that job.

## Idempotency, retries, and concurrency

- Item level: identical content hash → no write. Unique constraints back every identity.
- Run level: an `idempotency_key` whose run already succeeded/partially succeeded is replayed without fetching. Failed runs may be retried under the same key as a new attempt.
- One active run per connector, enforced with a compare-and-set on `source_connectors.active_run_id`. Runs abandoned for more than 15 minutes can be taken over. This is verified on PostgreSQL with overlapping runs and with six simultaneous run starts: exactly one writer, no duplicate postings or snapshots, and the lock released afterwards.
- Task enqueue is idempotent under concurrent callers (unique key plus savepoint), and competing workers claim distinct tasks (`FOR UPDATE SKIP LOCKED`). A redelivered ingestion task runs once.
- Record persistence is one transaction. On failure it rolls back, and the encrypted artifacts written during the attempt are deleted on a best-effort basis; blobs left by a hard crash are handled by reconciliation (below).
- Task retries (`source_ingest` tasks): retryable errors (`provider_unavailable`, `provider_error`, `rate_limited`, `run_in_progress`, `persistence_conflict`) are rescheduled with bounded exponential backoff (max 5 attempts); policy/authorization failures and invalid provider responses are never retried automatically.

## Source health

Each run updates `health_status`: `healthy` (success), `degraded` (partial or < 3 consecutive failures), `failing` (≥ 3), `suspended` (≥ 5; circuit open, operator reset required), `blocked` (policy/authorization failure). `last_success_at`, `last_failure_at`, `last_error_code`, and `consecutive_failures` are exposed through `GET /connectors`.

## Artifact lifecycle, retention, and deletion

Raw source records are stored as encrypted blobs (`job-snapshot` artifacts) referenced by `job_snapshots.raw_artifact_id`. A blob is written before the transaction that references it commits, so an interrupted run can leave an unreferenced blob behind.

**Reconciliation.** Run `python -m app.admin reconcile-artifacts` (dry run) or add `--apply`; this should be scheduled daily in production. It reports:

- **Orphan files**: blobs with no `stored_artifacts` row, older than the grace period (default 60 minutes, minimum 30, longer than the 15-minute abandoned-run threshold). Deleted with `--apply`.
- **Unreferenced snapshot artifacts**: `job-snapshot` rows that no snapshot references, older than the grace period. Deleted with `--apply`. CV and profile artifacts are never touched by this path.
- **Missing files**: rows whose blob is gone. Reported only (exit code 2) and logged as an integrity incident, never auto-deleted.

Applied runs write an `artifacts.reconciled` audit event with counts.

**Deletion ordering.** Every deletion commits database rows first and removes blobs afterwards. A crash in between leaves only unreferenced ciphertext, never a row pointing at missing evidence.

**Retention and deletion policy.**

| Data | Retention | Deletion trigger |
|---|---|---|
| Shared-catalog postings, snapshots, raw blobs | Kept, including stale/closed postings, as historical evidence for matches and CVs | Legal retention schedule (pending decision); no automatic purge yet |
| Private-source postings, snapshots, raw blobs, and derived analyses, matches, reviews, CVs, CV artifacts, run history, queued tasks | While the owner keeps the source | Owner deletes the source: `DELETE /connectors/{id}`. Account deletion must call the same routine first (the account-deletion flow itself is not yet implemented) |
| Profile data | While the profile exists | `DELETE /profile`. This does not delete private sources, because profile deletion is not source deletion |
| Audit events | Retained | They contain identifiers, codes, and hashes, not source or CV content |

## Tenancy

`source_connectors.owner_user_id` / `job_postings.owner_user_id` are `NULL` for the shared catalog (operator-managed sources) and set for sources a single candidate is explicitly authorized to use. Private postings are only visible, matchable, and dedup-comparable within their owner's scope. Global sources can be triggered only by the `operator` role; private sources only by their owner. Operators are designated with `python -m app.admin promote-operator --email <registered email> --reason <ticket>` (see `backend/README.md`); there is no web endpoint for role changes.

## API

- `GET /connectors`, `GET /connectors/{id}`, `GET /connectors/{id}/runs` — visible sources, authorization basis summary, health, recent runs.
- `DELETE /connectors/{id}` — owner-only deletion of a private source and all derived data.
- `POST /connectors/{id}/ingest` — enqueues an idempotent `source_ingest` task (optional `Idempotency-Key` header, otherwise a one-minute bucket). The request never carries URLs or provider parameters.
- `GET /jobs?status=active|closed|stale|all&include_duplicates=false`, `GET /jobs/{id}`, `GET /jobs/{id}/snapshots`.
- `python -m app.worker --once` processes queued ingestion tasks.

No endpoint creates connectors or accepts arbitrary URLs. Connectors are registered by operators through reviewed code/configuration.

## Registered providers

### `fixture` (bundled sanitized fixture catalog)

- Authorization basis: `local_fixture`; tier `local`; no network access.
- Fields collected: the sanitized fields in `backend/app/fixtures/jobs.json` (URLs use `example.invalid`). No personal data.
- Rate limits: none (local file); max 1,000 records per run.
- Failure modes: missing/malformed bundled file → `invalid_provider_response`; dataset names outside the bundled allowlist are rejected.
- Listing semantics: complete listing, so omitted fixture jobs become `stale`.
- Test strategy: static in-memory fixture providers and deliberately failing fake providers in `backend/tests/test_phase4_discovery.py`.

### `ashby_job_board` (Ashby public job board, Phase 5B)

Implementation: `backend/app/services/sources/ashby.py`.

**Endpoint.** Ashby's documented public Job Posting API only:

```
GET https://api.ashbyhq.com/posting-api/job-board/{JOB_BOARD_NAME}?includeCompensation=true
```

Reference: <https://developers.ashbyhq.com/docs/public-job-posting-api>. It is anonymous and read-only. No authenticated or private Ashby API, candidate/application endpoint, or page scraping is used. The provider rejects any credential configuration.

**Authorization basis.**

- `official_api` (Ashby's documented public API) or `explicit_authorization` (written permission from the board owner).
- Ashby documents the API's intended use as populating an employer's own careers page. Every board therefore needs its own recorded product-owner decision. The first board, `Ashby` (Ashby's own careers board, the documented example), was approved by the product owner on 2026-09-30 for read-only discovery: evidence = documentation URL, terms version `ashby-posting-api-docs-2026-09-30`.
- Connectors are created with `python -m app.admin register-ashby ...`. They start **disabled**, and `review_required` unless `--approve` is given. Enabling is a separate audited step: `set-connector-enabled`.

**Endpoint binding.** The reviewed config must be exactly:

- `fetch_hosts = ["api.ashbyhq.com"]`
- `fetch_path_prefixes = ["/posting-api/job-board/<board>"]` (an exact path, so `<board>Evil` does not match)
- `allowed_url_hosts = ["jobs.ashbyhq.com"]`

Anything else fails the run with `connector_misconfigured`. The deployment allowlist must also contain `api.ashbyhq.com`. A redirect to another board, to `jobs.ashbyhq.com`, or to any other host is denied, and the source is blocked.

**Field mapping.** Explicit fields only.

| Normalized field | Ashby source | Rule |
|---|---|---|
| `source_job_id` | posting UUID from documented `jobUrl` (`https://jobs.ashbyhq.com/<board>/<uuid>`) | The undocumented `id`, when present, must equal it, or the record is rejected. A missing identity rejects the record. In the live test `id` was present and equal for 67/67 postings |
| `title` | `title` | required |
| `employer` | connector `employer_name` | The API does not return an organization name |
| `canonical_url` | `jobUrl` | stored, never fetched; `applyUrl` is not used |
| `location_text` | `location` | verbatim |
| `secondary_locations` | `secondaryLocations[].location` | verbatim, never parsed |
| `work_mode` | `workplaceType` (`OnSite`/`Remote`/`Hybrid`), else `isRemote=true` → remote | contradictory flags → unknown |
| `location_country` | `address.postalAddress.addressCountry` | only for onsite/hybrid roles and only an exact ISO code or exact country name from a fixed table; remote roles never get a country (a remote role's address is usually HQ) |
| `description` | `descriptionPlain`, else `descriptionHtml` converted to text | scripts, styles, and link targets dropped; never rendered or fetched |
| `employment_type` | `employmentType` | `FullTime`→`full_time`, `PartTime`→`part_time`, `Intern`→`internship`, `Contract`→`contract`, `Temporary`→`temporary`; else unknown |
| `compensation` | `compensation.summaryComponents` | only when exactly one `Salary` component exists; `currencyCode` null → currency unknown; interval `1 YEAR/MONTH/WEEK/DAY/HOUR` → period, else unknown; no conversion |
| `posted_at` | `publishedAt` | normalized to UTC |
| `status` | — | listed postings are active; the API has no closed state |

- **Always unknown:** `updated_at`, `closing_at`, remote geography, permitted countries, sponsorship, relocation, and work authorization requirements.
- **Unlisted postings:** `isListed=false` postings ("only available via direct link") are excluded, respecting the employer's intent.
- **Raw payload:** the original Ashby job object is kept as `source_payload` in the encrypted raw snapshot, so the raw hash covers exactly what Ashby returned.

**Listing semantics.** The endpoint returns the full published board in one unpaginated response (`max_pages = 1`). A posting absent from a later response becomes `stale` (reversible, reopened if it reappears). It is **never** marked closed or deleted, because the API has no closure signal. An empty response is treated as a provider fault, not a mass closure.

**Provenance.** Each snapshot records:

- provider kind and version (`ashby-posting-api-provider-v1`) and the Ashby `apiVersion` (validated short string);
- the listing URL (no query string), fetch time, run, and page;
- authorization basis and terms version;
- normalizer version (`job-normalizer-v2`), content hash, and raw hash.

**Limits.** At most one request per run, and a provider minimum of 5 minutes between runs (the scheduler minimum is 15 minutes). The Phase 5A limits also apply: 5 MB decoded response, 30 s deadline, 2,000 postings per run, and 256 KB per posting record. The live `Ashby` board returned 67 postings in about 2.2 MB.

**Failure modes.**

| Condition | Result |
|---|---|
| Non-JSON, no `jobs` list, or not an object | `invalid_provider_response` (not retried) |
| 5xx or timeout | retried with backoff |
| 429 | `rate_limited` (retried after `Retry-After`) |
| 401/403 or any off-endpoint redirect | blocked, never retried |
| Oversized response | `response_too_large` |
| Individual malformed, duplicate, off-board, or identity-conflicting postings | rejected per record; the rest are stored |

**Fixtures and tests.**

- Fixtures: `backend/tests/fixtures/ashby/board_v1.json` (sanitized, fabricated) in the documented shape.
- Tests: `backend/tests/test_phase5b_ashby.py`, run through `httpx.MockTransport` with no network.
- The live smoke test is recorded in `docs/testing.md`.

## Live network controls (Phase 5A)

Implementation: `backend/app/services/sources/egress.py`, `http_client.py`, `credentials.py`, `scheduler.py`, `health.py`. The only live provider using these controls is `ashby_job_board` (above). `SOURCE_NETWORK_ACCESS_ENABLED=false` and an empty `SOURCE_EGRESS_ALLOWLIST` are the defaults. Startup fails if network access is enabled without an allowlist, or if the allowlist contains an invalid or over-broad entry (`*`, `*.com`, IPs).

### Egress allowlist

A host is reachable only if it is in **both** of these lists:

1. the deployment allowlist `SOURCE_EGRESS_ALLOWLIST`: an environment setting that the database, API, and providers cannot change;
2. the connector's reviewed `config_json.fetch_hosts` (default: `base_domain`).

Entries are exact hosts or `*.domain` (subdomains only, never the apex). At authorization time every connector host must fall within the deployment list, or the run is blocked with `egress_not_allowlisted`. An optional `fetch_path_prefixes` narrows allowed paths.

### SSRF, URL, and DNS/IP safety

Each request and each redirect hop is checked before it is sent. The check rejects:

- non-HTTPS schemes and ports other than 443;
- userinfo (`user@host`), percent-encoded hosts, backslashes, whitespace, and control characters;
- IP-literal hosts, including numeric-TLD shorthand such as `127.1`, `2130706433`, and `0x7f000001`;
- non-ASCII (homoglyph) hostnames;
- denylisted domains and hosts that are not allowlisted;
- any host with **any** resolved address that is not globally routable unicast. This covers loopback, private, link-local and cloud metadata (`169.254.169.254`, `fd00:ec2::254`), CGNAT, reserved, documentation, multicast, and unspecified addresses, plus IPv4-mapped, NAT64, 6to4, and Teredo encodings of them.

DNS lookups have a 5-second timeout.

**DNS rebinding.** The authoritative check runs inside a custom httpcore network backend (`PinnedNetworkBackend`) *at connect time*. It resolves once, validates every answer, and connects to the validated IP, while TLS still verifies the certificate and SNI against the hostname. A second, different answer cannot be used. Environment proxies are ignored (`trust_env=False`). A test asserts the transport wiring so an httpx upgrade cannot silently remove pinning.

### Redirects

Redirects are followed manually, at most `SOURCE_HTTP_MAX_REDIRECTS` (default 3). Every hop is re-validated as a new request, so downgrade, off-allowlist, IP, or metadata redirects are denied before any request is sent to them. Credentials are sent only to the host the provider addressed and are dropped on any cross-host redirect.

### Limits

| Setting | Default |
|---|---|
| Connect timeout | 5 s |
| Read timeout | 15 s |
| Total per-request deadline | 30 s, enforced after every network read so slow trickles cannot outlive it |
| Maximum response size | 5 MB of *decoded* bytes (compressed payloads cannot bypass it); an oversized `Content-Length` is rejected before reading |
| Requests per run | 60 |
| Accepted content types | `application/json` only |

`Accept-Encoding: identity` is requested.

### Response classification

| Response | Result |
|---|---|
| 429 | `rate_limited` (retryable, bounded `Retry-After`) |
| 5xx | `provider_unavailable` (retryable) |
| Timeout (connecting, waiting for headers, or while streaming the body) | `provider_timeout` (retryable) |
| 401 / 403 | `provider_access_denied`, blocked. Access control is respected, never retried around |
| Other status, wrong content type, malformed/empty JSON | `invalid_provider_response` |
| Egress denial | `egress_denied`, blocked; the task is never retried |

### Provider isolation

- Providers get network access only through `FetchRequest.http`: a `ConnectorHttpClient` bound to the connector's immutable egress policy and limits.
- Their view of connector config is a deep read-only copy, so provider code or provider data cannot alter authorization or security configuration.
- A static test fails if any source module other than the egress layer imports a network library.
- URLs inside job records or descriptions are stored as data and are never fetched.

### Credentials

- Connector config may hold only a reference: `{"credential": {"ref": "file:<name>", "header": "Authorization", "scheme": "Bearer"}}`. The value is read when a run starts, from a secret-manager-projected file or (development and test only) from `env:JFA_SOURCE_SECRET_<NAME>`. See "Secret references from files" below.
- Literal secret-looking keys in config (`api_key`, `token`, `password`, ...) block the connector (`inline_secret_in_config`). Malformed or unset references block it (`credential_unavailable`). `Host`/`Cookie` headers cannot be set.
- Resolved values are registered with the redactor (`app/core/redaction.py`), which scrubs them and credential-shaped patterns from log lines, error details, and audit metadata. `repr()` of credentials and clients never reveals them.

### Audit of live activity

- Each outbound attempt writes `source.network_request` or `source.egress_denied`. The event records the host, a path digest (no path, query, headers, or body), status, bytes, duration, redirect count, and denial reason, and is attached to the run and owner. Responses that are received and then refused (429, 5xx, 401/403, unexpected status or content type, oversized) are recorded too, with the error code as `reason`. A connection lost while streaming the body is recorded as an `error` event and classified as `provider_timeout` or `provider_unavailable` (retryable).
- Run stats include a `network` summary.
- Health transitions write `source.health_changed` and log a warning when a source enters `failing`, `suspended`, or `blocked`.

## Scheduler

`python -m app.scheduler --once` (or `--loop`) runs a tick that enqueues durable `source_ingest` tasks for due connectors. `python -m app.worker` executes them through the normal pipeline. No separate scheduler service is required: ticks are idempotent and can run from cron or a systemd timer.

- **Configuration.** `python -m app.admin set-schedule --connector NAME --interval-minutes N` (15 minutes to 7 days) or `--disable`. Changes are audited. Unscheduled connectors run on demand only.
- **Overlap prevention.** Three layers: one task per connector per time slot (idempotency key `schedule:<slot>`); skip while a queued/running ingestion task or an active run exists; and the pipeline's compare-and-set run lock. Concurrent ticks on PostgreSQL create exactly one task.
- **Health.** `suspended` and `blocked` connectors are skipped on every tick and are never re-enabled by time passing. An operator or owner reset (`POST /connectors/{id}/reset-circuit`), or a fixed authorization plus a manual run, is required.
- **Retries.** Temporary failures are retried by the task layer (exponential backoff, max 5 attempts). While a retry is pending, later slots are skipped rather than stacked.
- **Tenancy.** Scheduled runs act for the connector's owner (`requested_by_user_id = NULL`), and their audits are attributed to the owner.

## Health monitoring

`GET /connectors/health` (visible connectors only) and `python -m app.admin health-report` (exit code 3 when any source needs attention) report the following for each connector:

- `state`: `success`, `failure`, `degraded`, `blocked`, `suspended`, or `unknown`;
- the internal `health_status`;
- consecutive failures and last error code;
- last run, success, and failure times;
- schedule and next due time;
- run counts for the last 24 hours;
- `overdue` (scheduled but no success within twice the interval) and `alert`.

| Internal `health_status` | Reported `state` |
|---|---|
| `healthy` | `success` |
| `failing` (3+ consecutive failures) | `failure` |
| `degraded` | `degraded` |
| `blocked` | `blocked` |
| `suspended` (5+ consecutive failures, circuit open) | `suspended` |

## Deployment enforcement

Decision record: `docs/roadmap.md`, "Decision record: live-access deployment controls (2026-11-13)". The reference deployment is `compose.yaml` with profile `app`. Production platforms must provide the same properties with their own primitives, for example Kubernetes `NetworkPolicy` instead of an internal Docker network.

### Topology

| Service | Networks | Role |
|---|---|---|
| `postgres` | `default`, `backend` | System of record |
| `migrate` | `backend` | One-shot `alembic upgrade head`; the app services wait for it |
| `api` | `backend`, `edge` | HTTP API. `edge` exists only to publish the port; the API never runs providers |
| `worker` | `backend` | `python -m app.worker --loop`: executes queued ingestion tasks |
| `scheduler` | `backend` | `python -m app.scheduler --loop`: enqueues due runs and sends health alerts |
| `egress-proxy` | `backend`, `egress` | The only container with a route to the internet |

`backend` is `internal: true`: containers on it can reach each other but have no route out and no external DNS. The worker and scheduler are attached to nothing else.

### Network-layer egress enforcement

The egress proxy (`deploy/egress-proxy/`) is Squid 6.12 on Alpine 3.22. It is configured to:

- allow `CONNECT` only, to port 443 only. Every other method, and every other port, is refused;
- allow only hosts from `EGRESS_ALLOWLIST` (the union of `SOURCE_EGRESS_ALLOWLIST` and `ALERT_WEBHOOK_HOST`). Entries have the application's semantics: an exact host, or `*.domain` for subdomains only, never the apex. They are compiled to anchored regular expressions with reverse DNS disabled, so an IP-literal `CONNECT` never matches;
- refuse any destination that resolves to a loopback, private, link-local (including cloud metadata), CGNAT, reserved, multicast, documentation, or unique-local address;
- keep no cache, add no forwarding headers, and log only the time, client, status, method, and `host:443`. TLS is end to end, so paths, queries, headers, and bodies never reach the proxy.

The application side (`SOURCE_EGRESS_PROXY_URL=http://egress-proxy:3128`):

- `ProxyTunnelTransport` tunnels each request with `CONNECT host:443` and then performs TLS to the target. The certificate and SNI are verified against the target hostname, so the proxy cannot read or alter the traffic.
- Its network backend can connect only to the configured proxy host and port. A connection attempt to anything else is denied (`EgressDenied: proxy_only`).
- Before every request and every redirect hop, the guard still applies all URL checks: HTTPS, port 443, no userinfo or IP literals, denylist, both allowlists, and path prefixes. The proxy is never a way around them.
- In proxy mode the application does not resolve DNS itself. The proxy resolves and refuses non-public answers, because a resolution in the worker would fail by design (no external DNS). The host allowlist is enforced by both layers; the destination-address check is enforced by the proxy, and the worker cannot open any connection that skips the proxy.
- The proxy URL must be `http://host:port` with an explicit port and no credentials, path, or query. The hop to the proxy is plain HTTP inside the private network; the tunneled traffic is TLS.
- Outside `development` and `test`, `SOURCE_NETWORK_ACCESS_ENABLED=true` without `SOURCE_EGRESS_PROXY_URL` fails startup.

Without a proxy URL (development and tests), the direct DNS-pinning transport described under "Live network controls" is used unchanged.

### Secret references from files

Credential references accept two forms:

| Reference | Source | Allowed in |
|---|---|---|
| `env:JFA_SOURCE_SECRET_<NAME>` | Process environment | `development`, `test` only |
| `file:<name>` | `<SECRETS_DIR>/<name>` | All environments |

`<name>` is 1 to 128 characters of `a-z`, `0-9`, `_`, `.`, `-`. It starts with a letter or digit and contains no path separators. The resolved real path must stay inside `SECRETS_DIR` (Kubernetes projected secrets are symlinks inside the mount and are accepted; links that leave it are refused). The file must be a regular file of at most 8 KiB, and one trailing newline is removed. An empty or missing file, or an unset `SECRETS_DIR`, blocks the run with `credential_unavailable`. Files are read at the start of each run, so rotation needs no restart. Resolved values are registered with the redactor as before.

Docker and Kubernetes secrets, Vault Agent templates, and the AWS, GCP, and Azure secret-store CSI drivers all deliver secrets as files, so any of them can sit behind this interface without code changes.

### Health alert routing

When `ALERT_WEBHOOK_REF` and `ALERT_WEBHOOK_HOST` are both set, each scheduler tick (and `python -m app.admin send-health-alerts`) evaluates the health report and sends:

- `firing` when a connector enters an alert state (`failure`, `blocked`, `suspended`, or `overdue`), when its state changes while alerting, and as a reminder every 6 hours while it stays in alert;
- `resolved` once, when a connector that was alerting recovers or is disabled.

The last notification per connector is read from the audit log (`source.alert_notified`), so a restart does not re-send. Run one scheduler: two schedulers ticking at the same moment could both send the same notification (a duplicate, never a missed alert). A failed delivery writes `source.alert_failed` and is retried on the next tick. An alerting error never stops scheduling.

Payload (JSON `POST`):

```json
{"source": "jobfinderagent", "kind": "firing", "connector_id": "...", "connector_name": "ashby-acme",
 "scope": "shared", "state": "suspended", "overdue": false, "consecutive_failures": 5,
 "last_error_code": "provider_unavailable", "at": "2026-11-13T10:00:00Z"}
```

`connector_name` is sent for shared sources only. A private source's name was typed by its owner and is replaced by `null`. Payloads carry no candidate, profile, job, or application data. Any relay that accepts a JSON webhook (Alertmanager, PagerDuty or Opsgenie integrations, a chat webhook) can route it to on-call.

Delivery controls:

- The webhook URL comes from a secret reference (it usually embeds a token). It is never logged or audited.
- Its host must equal `ALERT_WEBHOOK_HOST` exactly. The URL alone cannot redirect alerts elsewhere.
- Delivery uses the egress guard (HTTPS, port 443, public address) and the proxy when one is configured. Redirects are not followed, and the timeout is 10 seconds. Any status other than 2xx is a failure.
- Alerting is off unless both settings are present, and it is independent of `SOURCE_NETWORK_ACCESS_ENABLED`.

### Continuous worker and scheduler

- `python -m app.worker --loop [--poll-seconds 5]` claims and runs tasks until stopped. It uses a fresh database session per task.
- `python -m app.scheduler --loop [--tick-seconds 60]` ticks until stopped.
- Both stop on `SIGTERM` or `SIGINT` after finishing the current task or tick, so a deploy never abandons a claimed task mid-run. Task leases still cover crashes.

## Adding further live connectors

The Phase 5A controls above are in place. A new provider still needs, before merge: authorization basis and evidence, terms version and review, fields collected, rate limits and backoff, failure modes, domain allowlist, sanitized contract fixtures (pagination, malformed payloads, rate limits, timeouts, closed jobs), a kill-switch test, and a security review of the new data flow. Enabling it in production also requires the deployment controls under "Deployment enforcement" to be running in that environment (see the prerequisites in `docs/roadmap.md`), and its host added to both `SOURCE_EGRESS_ALLOWLIST` and the egress proxy allowlist.
