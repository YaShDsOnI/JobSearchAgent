# JobFinderAgent requirements and specification audit

Status: canonical requirements; Phase 4 job discovery and source integration foundation is authorized within the documented scope (local fixture providers only). Reviewed 2026-09-29.

## Product intent

JobFinderAgent helps one candidate discover relevant jobs, understand why they fit, prepare a truthful tailored CV, and complete an application with the candidate in control. It is an assistant for search and preparation, not an autonomous employment decision-maker or a system that applies indiscriminately.

The initial delivery should be a private, single-candidate beta with a multi-tenant-safe data model. It should discover and normalize permitted public job postings, score them, generate reviewable CV drafts, and support a user-approved application handoff for a very small number of explicitly supported application surfaces.

## Requirement classes

| Class | Meaning | Example |
|---|---|---|
| Hard eligibility | A legal, factual, or source constraint that blocks automation | Candidate lacks confirmed work authorization for a location; source disallows automation |
| Fit | How well the role matches evidence in the candidate profile | Required skill coverage, seniority, role family |
| Preference | What the candidate prefers but may choose to override | Remote over hybrid; Ahmedabad over Pune |
| Workflow safety | A condition that requires a human or a stop | CAPTCHA, MFA, unknown required question, changed form |

The UI and API must expose these separately. A high fit score cannot make an ineligible job eligible, and a preferred job is not necessarily a better skill fit.

## Functional requirements

### Candidate profile and preferences

1. Accept a candidate-provided CV and structured facts with provenance.
2. Allow the candidate to confirm, correct, withdraw, or mark facts as unknown.
3. Store work authorization as explicit country/region status, sponsorship need, relocation willingness, and effective dates where known. Never infer it from nationality, address, education, or employer.
4. Store target roles, seniority, employment types, compensation expectations and currency, notice period, preferred work modes, and an ordered location preference list.
5. Version profile and preference changes. A historical match or application must be reproducible from its snapshots.
6. Support deletion/export requests and consent records.

### Job discovery and normalization

1. Ingest only from an approved connector whose authorization basis and terms review are recorded.
2. Prefer official/public ATS feeds and employer career pages. Store source URL, source job ID, retrieval timestamp, content hash, and raw snapshot reference.
3. Normalize title, employer, description, requirements, locations, work mode, compensation, posting/closing dates, application URL, and source provenance.
4. Preserve uncertainty: `unknown` is a valid value for remote scope, sponsorship, salary, and other fields.
5. Deduplicate by stable source identity first, then conservative content/link fingerprints.
6. Mark stale/closed postings without deleting historical evidence.
7. Fail closed when a connector's authorization record is missing, incomplete, expired, reviewed against different terms, or not permitted for its provider; record the refusal. The implemented policy, normalized schema, and dedup rules are in `docs/source-connectors.md`.

### Analysis and matching

1. Extract structured requirements with evidence spans from the source text.
2. Distinguish required, preferred, and informational requirements.
3. Produce eligibility status and reasons, fit sub-scores, preference score, overall rank, and an explanation backed by stored evidence.
4. Use deterministic rules for hard gates and bounded, versioned scoring for ranking. Embeddings are optional retrieval features, never the sole decision mechanism.
5. Route low-confidence or conflicting extraction to review; do not silently convert ambiguity into eligibility.

### CV generation

1. Generate from a canonical fact ledger and a job analysis snapshot only.
2. Permit reordering, shortening, and faithful paraphrase; do not add facts, metrics, dates, skills, titles, employers, credentials, or authorization claims.
3. Record source fact IDs for each generated claim where feasible and run a factuality validator before review.
4. Produce an ATS-friendly, accessible document without a requirement for a specific vendor's undocumented parser behavior.
5. Store immutable versions, file hashes, generator/model/prompt versions, and approval state.
6. Ask the candidate for missing facts; never let the model guess.

### Application assistance

1. The candidate reviews the job, eligibility, fit, preference result, questions, proposed answers, CV artifact, and destination before submission.
2. One explicit approval is required per application in the initial product and expires if job/profile/CV inputs change.
3. Fill only fields whose values are confirmed and whose semantics are known. Leave unknown questions for the candidate.
4. Pause on CAPTCHA, MFA, anti-bot signals, access restrictions, unexpected fields, or changed terms. The system must not solve, bypass, or retry around them.
5. Verify success using an allowed confirmation page, provider response, or candidate-provided evidence. “Clicked submit” is not proof of submission.
6. Record the exact CV, answers, snapshots, source connector, destination, timestamps, and event history.
7. Block duplicates using source/job identity and a conservative human-review path for suspected duplicates.

## Non-functional requirements

- Privacy by design: data minimization, purpose limitation, explicit consent where required, retention/deletion controls, and tenant isolation.
- Security: least privilege, encrypted sensitive artifacts, managed secrets, safe browser sessions, redacted logs, and auditable actions.
- Reliability: durable jobs, idempotency, resumable workflows, bounded retries, connector kill switches, and clear blocked states.
- Explainability: every score and gate must be attributable to rules, data, or evidence; LLM-written prose cannot be the only explanation.
- Accessibility: dashboard and review flow target WCAG 2.2 AA; keyboard review must be possible.
- Operability: structured events, metrics, cost accounting, alerting for connector failures and unsafe workflow states.
- Performance targets are measured in the beta; no “100 jobs/day” or “under 30 seconds per job” promise is accepted before workload and model-cost evidence.

## Audit of the supplied report

### Missing requirements

- No explicit data classification, tenant isolation model, retention schedule, deletion/export process, or key-management design.
- No canonical fact/provenance model for preventing CV and application-answer fabrication.
- No state machine, idempotency model, approval expiry rule, or exact definition of successful submission.
- No source-by-source authorization/terms register, connector kill switch, or change-management process.
- No treatment of malicious job descriptions and application fields as prompt-injection input.
- No fairness/bias evaluation plan for ranking, proxy features, or protected/sensitive attributes.
- No accessibility requirement, incident response process, dependency/secret supply-chain controls, or disaster recovery objective.
- No exact schemas for requirements, evidence, match runs, CV claims, application answers, or audit events.

### Conflicting requirements

- The report says approval is required by default but also proposes unattended auto-apply. This design resolves the conflict by excluding unattended submission from the initial product and requiring per-application approval.
- It says never assume authorization, but its ordered “global remote” rule treats no country restriction as eligible. Absence of a restriction is not proof that a candidate may work there; the design uses `unknown` and asks the candidate.
- It proposes storing authentication credentials and scraping logged-in boards while also promising security and compliance. This is unsafe and often contractually disallowed; the source policy below prohibits it unless a provider-approved integration exists.
- It mixes eligibility, skill fit, and preference into a weighted overall score. The design retains independent statuses and only ranks among eligible candidates.
- It calls third-party unofficial APIs a way to avoid HTML parsing. A third party does not grant permission to use a source's data; every connector needs an authorization basis and terms review.

### Outdated or unreliable assumptions

- LinkedIn automation and scraping are not a general integration option. LinkedIn's current User Agreement and help guidance prohibit scraping and unauthorized automated activity: <https://www.linkedin.com/legal/user-agreement> and <https://www.linkedin.com/help/linkedin/answer/a1341543>.
- “Pinecone is GDPR-compliant” is not a sufficient system-level conclusion. Compliance depends on the deployment, contracts, data flows, region, retention, and controls; the provider's marketing claim is not a design decision.
- “ATS scanners rely on keywords” is too broad to be a quality requirement. Use accessible conventional formatting and truthful job relevance, but validate rendered CVs and claims rather than optimizing for an undocumented parser.
- The report treats LLM prompting as a factuality control. Prompts help but cannot enforce truth; provenance, deterministic validation, model-output schemas, and human review are required.
- Kubernetes, multiple vector databases, Airflow/Temporal, and many job boards are not justified for the first beta. They increase operational and security surface without proving product value.
- The report's fixed dates and throughput targets are speculative. The roadmap below is milestone/acceptance based and time-boxed only after capacity is known.

### Requirements that should not be implemented

- CAPTCHA solving, MFA interception, anti-bot evasion, stealth plugins, proxy rotation to evade controls, or access-control circumvention.
- Automated LinkedIn/Naukri/Glassdoor account scraping or applying without written provider authorization and a reviewed integration.
- Unattended bulk submission or model-generated answers to unknown legal, demographic, salary, authorization, or screening questions.
- “Global remote” eligibility inferred from a job saying only “remote.”
- Ranking or generation based on protected characteristics, inferred health/disability, age, race/ethnicity, religion, gender, or other sensitive proxies.
- Sending the full profile/CV/JD to multiple model vendors by default without a recorded data-processing choice.

## Source policy

Connector tiers:

1. **Tier A — approved:** official public job feed/API or employer ATS endpoint with documented permitted use; read-only discovery first.
2. **Tier B — review required:** employer career page accessed at normal rate with permission/terms review, or provider-approved application integration.
3. **Tier C — link-out only:** source has no approved machine access, requires login, presents anti-bot controls, or terms prohibit automation. Show the link; no scrape, form filling, or submission.
4. **Blocked:** source is accessed by bypassing controls, unofficial credentials/cookies, or unclear legal/contractual basis.

Initial candidates for validation are public job feeds from Greenhouse, Lever, and Ashby where the employer/source terms permit access. Their official documentation describes public job-board feeds or application APIs, but availability and authorization still require connector-specific review: <https://docs.greenhouse.io/job-board.html>, <https://hire.lever.co/developer/documentation>, and <https://developers.ashbyhq.com/docs/public-job-posting-api>.

## Research sources and implications

- Playwright documents auto-waiting, isolated browser contexts, storage-state sensitivity, and failure traces. Use those capabilities for reliability and testing, but never treat them as permission to automate a site: <https://playwright.dev/docs/actionability>, <https://playwright.dev/docs/auth>, <https://playwright.dev/docs/trace-viewer>.
- OWASP's 2025 LLM Top 10 highlights prompt injection, sensitive information disclosure, improper output handling, excessive agency, vector/embedding weaknesses, misinformation, and unbounded consumption: <https://genai.owasp.org/llm-top-10/>.
- NIST AI RMF and its Generative AI Profile support governance, mapping, measurement, management, provenance, testing, incident disclosure, and context-appropriate human oversight: <https://www.nist.gov/itl/ai-risk-management-framework> and <https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.600-1.pdf>.
- India's Digital Personal Data Protection Act, 2023 and the 2025 Rules require jurisdiction-specific legal review of notice, consent, security, rights, retention, and transfers; do not claim compliance merely from encryption: <https://www.indiacode.nic.in/indiacode/handle/123456789/22037?view_type=browse> and <https://www.meity.gov.in/documents/act-and-policies/digital-personal-dataprotection-rules-2025gDOxUjMtQWa?pageTitle=Digital-Personal-Data-ProtectionRules-2025>.
- EU recruitment/selection AI can fall within high-risk employment use cases, and GDPR Article 22 constrains solely automated decisions with significant effects. Legal applicability depends on how the product is offered and used; the architecture therefore keeps the system candidate-directed, advisory, explainable, and human-approved: <https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32024R1689> and <https://eur-lex.europa.eu/legal-content/EN/ALL/?uri=uriserv:OJ.L_.2016.119.01.0001.01.POL>.

## Open questions requiring decisions

- Which countries will host the service and which candidate/user jurisdictions are in scope?
- Is this a personal tool for one candidate or a product for multiple candidates? Who is the data fiduciary/controller and who are processors?
- Which exact sources have permission for machine access and, separately, application submission?
- Which model providers may receive CVs and job text, in which regions, with what retention/training settings?
- What candidate-level retention, deletion, export, and backup policy is acceptable? (Phase 7 implements 35-day backup retention with deletion replay after restore; legal review must still confirm it.)
- Will applications be link-out only, API-based, or browser-assisted for the beta? Which ATS test environments are available?
- Does the product need cover letters, recruiter messages, demographic questions, or only CV and basic application fields? The default is to exclude these until separately approved.
