# Application agent and human-in-the-loop flow

Status: design baseline; application submission is not implemented or authorized in this phase.

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
