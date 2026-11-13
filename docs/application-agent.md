# Application agent and human-in-the-loop flow

Status: Phase 5C implements the application review package only (see "Phase 5C: review package implementation" below). Application submission, browser runners, and destination adapters are not implemented or authorized.

## Scope boundary

The agent can prepare and, after explicit candidate approval, submit through a permitted provider-approved API or a narrowly scoped browser adapter. It is not a general web agent. It cannot scrape or automate a source merely because Playwright can technically control it.

Initial default: candidate reviews each application and remains responsible for final submission. Batch approval, unattended mode, recruiter messaging, and demographic/EEO-question automation are out of scope until separately approved through legal, safety, and product review.

## Supported destination tiers

1. **API integration:** documented provider API, valid authorization, explicit application scope, idempotency/confirmation semantics, and test tenant or sandbox.
2. **Hosted/public application form:** source terms and permission reviewed; adapter is pinned to one domain and one known form contract; final submit remains approval-gated.
3. **Link-out:** user opens and completes the form; the agent may show the prepared CV and answers but does not operate the site.
4. **Blocked:** login scraping, unsupported source, prohibited automation, CAPTCHA/MFA challenge, access restriction, or unknown form semantics.

Official documentation demonstrates that some ATS vendors expose structured job/application interfaces, but those interfaces are generally intended for employer, partner, or custom-career-site integrations. The project must obtain the right authorization for the exact use case. Examples: Greenhouse Job Board API, Lever Postings API, and Ashby APIs: <https://docs.greenhouse.io/job-board.html>, <https://hire.lever.co/developer/documentation>, <https://developers.ashbyhq.com/docs/public-job-posting-api>.

## Application state machine

`candidate_selected` → `review_package_ready` → `cv_approved` → `approval_pending` → `approved` → `runner_started` → `form_filled` → `final_review` → `submitted` → `verified`

Alternate terminal/paused states: `skipped`, `expired`, `blocked_policy`, `blocked_unknown`, `paused_human`, `failed_retryable`, `failed_manual`, `duplicate_suspected`.

`approved` is an expiring capability bound to a hash of the job snapshot, profile version, preferences, CV version, answers, destination, and source policy. Any change invalidates it. A runner must re-check the binding immediately before final submission.

## Review package

The candidate sees, in one screen:

- employer, role, destination URL, source, and last-retrieved timestamp;
- eligibility status and reasons, separate fit and preference breakdowns;
- exact CV preview/download and its version/hash;
- every answer to be sent, with source fact or “candidate entered” provenance;
- unanswered/unknown required fields;
- data-sharing notice and destination scope;
- approval, skip, edit, and link-out actions.

The final submission step requires an explicit action after the form is filled. A model suggestion or a prior preference is not approval.

## Browser runner rules

- Use a fresh, ephemeral browser context per application; never share contexts across candidates.
- Allow navigation only to the approved destination origin and expected redirect origins.
- Use stable semantic locators and explicit assertions. Playwright's auto-waiting can improve reliability, and failure traces can help debug adapters, but browser storage state is sensitive and must be treated as an impersonation secret: <https://playwright.dev/docs/actionability>, <https://playwright.dev/docs/auth>, <https://playwright.dev/docs/trace-viewer>.
- Do not use stealth plugins, CAPTCHA services, proxy rotation for evasion, DOM obfuscation, or attempts to defeat rate limits.
- Do not let the model choose arbitrary URLs, selectors, scripts, uploads, or credentials. Adapters are reviewed code and policy-checked inputs.
- Stop on CAPTCHA, MFA, anti-bot interstitial, access denial, suspicious challenge, unexpected required field, changed consent/terms, or navigation outside the allowlist.
- Redact sensitive inputs from traces, screenshots, network logs, and error messages; retain a minimal encrypted failure artifact only when needed.

## Form handling

Use a destination-specific schema containing field label/semantic type, requiredness, allowed values, provenance, and safe answer policy. Unknown questions are not answerable by semantic similarity alone.

Safe defaults:

- Basic contact fields: fill from confirmed profile facts.
- Work authorization, sponsorship, salary, relocation, notice period: fill only from explicitly confirmed current values.
- Free-text screening: candidate-written or candidate-approved answer only.
- Demographic/EEO/voluntary self-identification: leave to the candidate; exclude from matching.
- Legal attestations and e-signatures: candidate must complete personally.

## Verification and duplicate protection

Submission is verified only by a trusted API response, an allowed confirmation page with a stable confirmation signal, or candidate-supplied evidence. Store status as `submitted_unverified` if the action occurred but evidence is insufficient.

Before starting, check source/job identity, application history, destination, and candidate's manual records. On ambiguous duplicate, stop and ask. Never retry a submission just because the browser timed out.

## Operational acceptance criteria

- An adapter cannot navigate to an unapproved origin in tests.
- CAPTCHA/MFA fixtures pause without attempting workarounds.
- Double-clicks, worker retries, and process crashes do not submit twice.
- Approval expires when any bound input changes.
- All sent fields and artifacts are reconstructible from an application event timeline.
- A candidate can cancel before final submission and revoke a pending approval.

## Phase 5C: review package implementation

Product-owner approval: 2026-10-03. Scope: build, show, and approve a review package for one job. Nothing is sent anywhere. Implementation: `backend/app/services/applications.py`, `backend/app/api/routes/applications.py`.

### What 5C does not do

- No endpoint, worker, task kind, or adapter sends, uploads, or submits anything to an employer or ATS.
- The application service imports no HTTP, socket, or browser library. Tests enforce this with a static check.
- The only destination tier is **link-out**. The package shows the posting's `canonical_url`. The candidate opens it in their own browser and completes any form themselves. The backend never fetches that URL. For Ashby, `applyUrl` is still not collected.
- A 5C approval is a recorded review decision. Nothing consumes it. Phase 6 will add the submission adapter, and it must re-verify the approval and the binding before acting.

### Stored state

- `applications.status` stores only what the candidate decided: `open` or `skipped`. There is no stored `submitted` state, and a database check constraint rejects any other value.
- The state shown to the candidate is **derived on every read** from the current package, its live binding check, and its approval. It is never cached:

| Shown state | Condition |
|---|---|
| `skipped` | The candidate skipped the job |
| `review_package_stale` | A bound input changed after the package was built (see "Binding check") |
| `review_package_ready` | The package is current; it may still have unresolved required answers |
| `approved` | The package is current, has no unresolved required answers, and has an active approval |

Approval states: `active`, `expired` (24 hours after approval), `revoked` (by the candidate, by skipping, or by a rebuild), and `invalidated` (the binding drifted). An approval is never treated as active unless all of these hold: its package is the current package, the stored review hash matches the recomputed hash, the live binding check passes, and it is neither expired nor revoked.

### Package contents and hash

Each package revision is immutable (`application_packages`, unique per application and revision number). Its canonical JSON (`review-package-v1`) contains:

- **Job:** job ID, connector ID, source job ID, snapshot ID, version, content and raw hashes, retrieval time, employer, title, location, work mode, and lifecycle status.
- **Source policy:** connector name, kind, policy status, enabled flag, authorization basis, terms version, and the provider version from snapshot provenance.
- **Inputs:** profile version and preference version.
- **Match:** match run ID, policy and extractor versions, and the eligibility, fit, and preference results. These are kept as three separate breakdowns.
- **CV:** CV version ID, content hash, generator and template versions, and the SHA-256 of the PDF and DOCX artifacts.
- **Answers:** one entry per question, with status, value, provenance, and source fact IDs.
- **Unresolved items and warnings.**
- **Destination:** `link_out`, URL, and host.
- **Data-sharing notice:** its version and text.

`review_hash` is the SHA-256 of that canonical JSON. Approval requires the client to send the hash it displayed, so a candidate cannot approve a package they did not see. Building again from identical inputs reuses the current revision, which makes the operation idempotent.

### Preconditions for building a package

- The job is visible to the candidate and its posting is `active`.
- Its source connector still exists, is enabled, and is `approved`.
- The candidate names a CV version that is theirs, belongs to this job, and is `approved`.
- The CV's match run is `eligible`.
- The CV's profile version is the candidate's current profile version.
- The match's preference version is the candidate's current preference version.
- If the CV was drafted from an older snapshot, no material job field may have changed since (same rule as CV supersession).

Any failure refuses the build with a specific error. Nothing is guessed or defaulted.

### Binding check (verify on every read and before approval)

The package is stale when any of the following differ from what was bound:

- the job's latest snapshot;
- the posting is no longer `active`;
- the connector is missing, disabled, not approved, or its authorization basis or terms version changed;
- the CV is no longer `approved`;
- the current profile version;
- the current preference version;
- another open application exists for a posting linked to this one as a cross-source duplicate (`duplicate_suspected`; stop and ask);
- the stored package no longer reproduces its review hash (integrity failure).

Approval is refused while the package is stale. A stale package is replaced by building again.

### Question set `standard-questions-v1`

There is no destination form schema in 5C, because the authorized source does not expose application forms. The package uses a fixed, reviewed, destination-agnostic question set. Answers come **only** from confirmed profile facts or from values the candidate types into this package (`candidate_entered`). Preferences are never used as answers: a matching preference such as a minimum salary is not an answer to an employer.

| Key | Required | Filled from |
|---|---|---|
| `full_name` | yes | confirmed `contact` fact: `full_name` (or `name`) |
| `email` | yes | confirmed `contact` fact: `email` |
| `phone` | no | confirmed `contact` fact: `phone` |
| `current_location` | no | confirmed `contact` fact: `location` |
| `work_authorization` | yes | confirmed `work_authorization` facts covering the job country (see below) |
| `sponsorship_required` | yes | the `sponsorship_needed` field of confirmed `work_authorization` facts covering the job country |
| `notice_period` | no | confirmed `availability` fact: `notice_period_days` or `notice_period` |
| `relocation` | no | confirmed `relocation` fact: `willing` (boolean) |
| `salary_expectation` | no | candidate-entered only |

Rules:

- **Job country.** It is the posting's `location_country`, else its single permitted country. No country, or several permitted countries, leaves both authorization questions unresolved (`job_geography_unknown` or `job_geography_ambiguous`). This holds even though the match is eligible.
- **Authorization statuses.** Only `authorized`, `citizen`, `permanent_resident`, and `work_permit` answer `yes`. `denied`, `not_authorized`, and `unauthorized` answer `no`. A fact whose countries do not include the job country says nothing about it, and unknown is never `no`.
- **No confirmed fact.** An answer is `unresolved` (`no_confirmed_fact`). When the fact exists but is unconfirmed, the reason is `fact_unconfirmed`. Confirmed facts that disagree give `conflicting_facts`.
- **Blocking.** A required question that is unanswered blocks approval. An optional question that is unanswered is shown as `left_blank` and does not block.
- **Candidate entry.** The candidate may type an answer for any question. A typed answer takes precedence and is labeled `candidate_entered`. Yes/no questions accept only `yes`/`no`; text is limited to 500 characters with no control characters. Every change builds a new package revision, which revokes any earlier approval.
- **Never asked.** Demographic/EEO questions, legal attestations, e-signatures, and free-text screening questions are not part of the set.

CV gaps recorded on the CV (`unresolved` in its manifest) are shown as non-blocking warnings. The candidate already reviewed them when approving the CV.

### Audit and timeline

`application_events` is an append-only timeline: `package_built`, `answer_updated`, `approved`, `approval_revoked`, `skipped`, `reopened`. Audit events mirror it. Neither ever contains answer values, CV text, or contact details. They hold question keys, hashes, revision numbers, and IDs only.

### Data lifecycle

- `DELETE /profile` deletes the candidate's applications, packages, approvals, and events, because packages embed profile-derived answers.
- Deleting a private source deletes applications for its postings.
- `GET /profile/export` includes applications with their package revisions.
