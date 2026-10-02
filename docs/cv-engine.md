# CV engine

Status: Phase 3 deterministic implementation baseline; no LLM is required for the initial pipeline. Local DOCX/PDF rendering uses deterministic standard-library renderers and encrypted artifacts.

## Source of truth

The canonical candidate profile is a fact ledger, not an unstructured prompt. Each fact has:

- stable ID and type;
- value and unit/date range;
- origin (candidate entry, uploaded CV, approved edit, public portfolio link if explicitly supplied);
- confidence and confirmation status;
- optional source span/document hash;
- visibility/use consent.

The generator may select and rewrite confirmed facts. It may not invent facts or use an unconfirmed claim as factual content.

## Generation pipeline

1. Freeze profile, job snapshot, match run, and template versions.
2. Select relevant confirmed facts using deterministic relevance rules and optionally semantic retrieval.
3. Ask a schema-constrained model for a structured CV outline and claim-to-fact references, not a free-form final document.
4. Validate every claim against the fact ledger: names, employers, dates, titles, skills, metrics, and credentials.
5. Reject unsupported, contradictory, or ambiguous claims. Route them to a candidate question.
6. Render a simple, accessible document (PDF plus editable source when supported).
7. Extract the rendered text and run a second validation pass to catch renderer/encoding loss.
8. Present the exact artifact and a change summary to the candidate.
9. On approval, make the version immutable and bind it to any later application.

### Phase 3 implementation boundary

The initial implementation uses deterministic fact-ledger assembly and template rendering. It does not call an LLM. A future structured model draft may be added only behind the existing claim schema and validator; untrusted model output can never bypass validation or approval.

## Invalidation (supersession)

An approved or validated CV version is superseded, never edited, when its inputs change materially. Implementation: `backend/app/services/cv_invalidation.py`.

- **Job change.** A new job snapshot supersedes the job's CVs only when a material field changed: title, employer, description, requirements, location text/country, work mode, remote geography, permitted countries, work authorization requirements, sponsorship, relocation, compensation, minimum experience, education, or employment type.
  - The previous snapshot is re-normalized with the current normalizer before comparison. A representation-only change, such as a `legacy-v0` snapshot re-normalized by `job-normalizer-v1`, therefore does not invalidate a CV.
  - Non-material edits do not invalidate a CV: posting/closing dates, benefits, canonical URL tracking parameters, and lifecycle status. Job closure is tracked on the posting; application steps must gate on the posting lifecycle, not on CV status.
- **Profile change.** A new profile version supersedes the user's active CVs.
- **What is recorded.** Every supersession records `superseded_at`, a `superseded_reason` (`job_material_change`, `profile_version_changed`, or `legacy_unrecorded` for rows superseded before migration `0007`), and a detail object with the triggering snapshot/profile IDs and changed fields. It also writes a `cv.superseded` audit event.
- **What is never changed.** Rejected CVs are left alone. Supersession changes only status fields: claims, rendered DOCX/PDF artifacts, content hashes, and approval metadata are never modified.

## Truthfulness controls

- No model instruction can override the fact ledger.
- Quantitative results may be included only when the source fact includes the metric and context.
- Synonyms are allowed only when they preserve meaning; “worked with” is not silently upgraded to “led.”
- Job keywords may guide ordering or wording but may not add an unverified skill.
- Missing information produces a question or omission, never a plausible completion.
- A human approval is required before a CV is used in an application.

## Document requirements

- Plain text flow, conventional headings, selectable text, clear dates, consistent job titles, and accessible contrast.
- No photos, decorative columns, hidden keywords, white text, misleading metadata, or parser-evasion techniques.
- The candidate can download the exact PDF and, where available, editable source.
- Template, font, renderer, locale, and artifact hash are recorded.

## Model interface

Input is a minimized, labeled bundle:

- `confirmed_facts[]` with IDs;
- `job_requirements[]` with evidence IDs;
- candidate-approved template/length constraints;
- explicit instruction that job text is untrusted data.

Output schema includes sections, bullet text, `source_fact_ids[]`, `job_requirement_ids[]`, and `needs_candidate_input[]`. Free-form content is not accepted until it passes the schema and claim validator.

## Acceptance criteria

- Golden tests with deliberately missing metrics, skills, and credentials produce no fabricated claim.
- Every final claim is traceable to one or more confirmed facts or is rejected.
- Re-rendering the same CV version produces a byte-identical artifact or a documented deterministic renderer difference.
- Candidate edits create a new version; approved versions cannot be mutated.
- A job change or profile change invalidates the approval binding and requires re-review.
- QA checks text extraction, visual layout, links, page count, accessibility, and artifact hash. PDF visual QA was performed by rendering a sanitized generated PDF with `pypdfium2` 4.30.0 at scale 2 to PNG and visually inspecting the resulting page. DOCX visual QA was performed with LibreOffice 26.8.0 `soffice` conversion to PDF, followed by PNG rendering with `pypdfium2` 4.30.0 and inspection of compact and two-page samples. Structural DOCX QA also passed: deterministic byte equality, ZIP/package validity, `word/document.xml` presence, extracted text, defined styles/numbering, explicit page margins, and preservation of validated claim tokens.

## Phase 3 acceptance evidence

- Deliberate hallucination and unconfirmed-fact fixtures are rejected or omitted.
- Every emitted claim stores confirmed source fact IDs and relevant job requirement IDs.
- DOCX and PDF artifacts are encrypted, hashed, immutable, and previewable locally.
- Approval and rejection are explicit, tenant-isolated, audited actions; approved versions cannot be mutated.
- Profile/job input changes require a new CV version and do not mutate an existing artifact.

## Phase 3 release-record clarification

- The PDF visual sample was generated from the deterministic `render_pdf` renderer, rasterized with `pypdfium2` 4.30.0, and inspected as a PNG. The inspection confirmed readable headings, selectable-text layout, stable spacing, and the corrected ASCII-safe job header separators.
- `render_docx` was called twice and produced byte-identical output; the package was validated as a ZIP, its document XML was read, extracted text was checked, and validated claim tokens were required to survive rendering. The package now includes explicit styles, bullet numbering, fonts, page size, and margins.
- DOCX visual QA is complete. LibreOffice 26.8.0 `soffice` rendered a compact one-page sample and a two-page sample. Every rendered page was inspected for layout, page breaks, margins, fonts, headings, bullets, clipping/overflow, blank pages, encoding, missing content, and readability.
- The LibreOffice/a11y audit reported zero high-, medium-, or low-severity findings for both samples. Direct OOXML checks confirmed heading styles, bullet numbering, page margins, content types, relationships, and complete text content.
- The prior mojibake/separator defect is fixed by using ASCII-safe separators in both renderers. Regression tests assert the rendered PDF/DOCX text contains no replacement character and that the expected claim text remains present. Additional regression coverage verifies the DOCX styles and numbering parts.
- The complete Phase 3 test suite currently passes: 29 tests passed.
