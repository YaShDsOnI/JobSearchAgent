# Matching and eligibility engine

Status: Phase 2 deterministic baseline with a sanitized regression benchmark and human review workflow; scoring weights remain provisional until labeled candidate feedback exists.

Implementation note (2026-09-25): The fixture catalog is analyzed from explicit structured fields using `rules-v1`. Job evidence stores bounded quotes and offsets. Matching persists immutable `match_runs` with separate eligibility, fit, and preference breakdowns. No model output is treated as a verified fact.

Implementation note (2026-09-25): `backend/benchmarks/matching_v1.json` is the versioned internal regression benchmark. It covers strong/weak fit, skill and experience mismatch, eligibility and unknown-value gates, preference differences, sponsorship, and unconfirmed facts. It is not a production performance claim. `match_reviews` stores pending/resolved human decisions, evidence, audit metadata, and a deterministic rerun linked to the original snapshots. Review decisions affect only the rerun context and never create candidate facts.

## Contract

For each job snapshot and profile/preference snapshot, output:

- `eligibility`: `eligible`, `ineligible`, `needs_review`, or `unknown`;
- hard-gate reasons with evidence and source timestamps;
- fit sub-scores and missing/evidenced skills;
- preference score and explanation;
- rank among eligible jobs only;
- policy, taxonomy, extractor, and model versions.

The system must never report a single opaque number as the reason to apply.

## Pipeline

1. Normalize the job and candidate facts into canonical enums, units, and skill aliases.
2. Extract job claims with evidence spans: location, work mode, permitted countries, sponsorship, seniority, experience, education/certification, compensation, employment type, and application constraints.
3. Validate extraction. Conflicts, low confidence, missing decisive fields, and stale postings route to `needs_review`.
4. Apply hard eligibility rules.
5. Compute independent fit dimensions from confirmed facts.
6. Compute preference fit from user-controlled weights.
7. Rank only jobs not blocked by hard eligibility, while keeping `needs_review` visible but non-automatable.
8. Generate an explanation from deterministic facts; an LLM may summarize it but cannot add reasons.

## Eligibility rules

Hard rules are policy, not model output. Examples:

- Source does not permit machine access or application automation: no automated action.
- Job explicitly requires a country where the candidate lacks confirmed authorization and no allowed sponsorship path exists: ineligible.
- Job requires a credential/education/experience that the candidate has explicitly marked absent: ineligible if it is truly required.
- Job is closed, withdrawn, or application deadline passed: ineligible for new application.
- Required candidate answer is unknown or unconfirmed: needs review, not guessed.
- Remote job has no stated permitted geography: eligibility is unknown unless the candidate confirms an accepted interpretation through review.

“Remote,” “worldwide,” “remote-first,” “remote within X,” and “remote from India” are different facts. “No location shown” is not worldwide.

## Fit dimensions

Initial score is a transparent weighted vector, for example:

| Dimension | Inputs | Notes |
|---|---|---|
| Skill fit | Required/preferred skills mapped to confirmed facts | Required and preferred are not interchangeable |
| Experience fit | Verified years, scope, seniority, domain | Do not derive years from vague prose without confirmation |
| Role fit | Target role families, title normalization, responsibilities | Alias mapping is versioned |
| Work-mode/location preference | Candidate ranking and job's explicit mode/location | Preference does not override eligibility |
| Compensation preference | Comparable currency/period and confidence | Unknown salary is unknown, not a zero |
| Freshness/quality | Posting freshness, source completeness, dedup confidence | Never reward low-quality data merely for semantic similarity |

Use a fixed policy version for each run. Do not tune on application outcomes alone because outcomes reflect employer process and selection bias, not just match quality.

## Semantic retrieval

Embeddings are useful for candidate generation and synonym discovery, not authorization or hard gating. Store embedding model/version and input hash. Do not embed raw sensitive profile fields unless necessary; prefer a minimized skills/experience representation. Compare a simple lexical baseline and embedding model in evaluation; keep the simpler path if quality is equivalent.

## Evidence and explanations

For each positive or negative claim, retain:

- source quote and offsets;
- candidate fact IDs or explicit missing/unknown marker;
- rule ID and policy version;
- confidence and review state.

Example explanation: “Eligible pending confirmation of remote-from-India permission. Required Python and PostgreSQL are evidenced by facts F12/F27; Kubernetes is preferred and not evidenced. Salary is not published. Remote priority is 1 in the current preferences.”

Avoid explanations that imply causal hiring outcomes or use sensitive attributes.

## Fairness and safety

- Do not collect or use protected characteristics for ranking. Keep legally required voluntary demographic questions outside matching.
- Audit proxy features such as location, school, company prestige, name, photo, and employment gaps for unintended effects.
- Evaluate ranking quality across job families, seniority bands, language variants, and incomplete profiles.
- Provide a candidate correction/override path and record whether the issue was extraction, profile data, or preference policy.

## Acceptance criteria

- A test suite proves unknown authorization never becomes eligible.
- A job may be highly skilled-fit but preference-poor, or preference-rich but skill-poor; both dimensions remain visible.
- Changing preferences recomputes preference score without changing the historical fit or eligibility result.
- Replaying the same snapshots and policy version produces the same scores.
- Every displayed score has a machine-readable breakdown and evidence links.
- The versioned benchmark has deterministic expected outcomes, and `needs_review` matches can be opened, resolved with evidence, audited, and rerun without changing verified facts.
- Human labels on a held-out set meet a pre-agreed precision@k/recall target before automatic CV generation is enabled; target values are set after pilot labeling, not invented now.
