export type User = { id: string; email: string; role: string; status: string };

export type ProfileFact = {
  id: string;
  fact_type: string;
  value: Record<string, unknown>;
  provenance: string;
  confirmation_status: "confirmed" | "unknown" | "withdrawn";
  source_ref: string | null;
  evidence_text: string | null;
};

export type Profile = {
  id: string;
  summary: string | null;
  status: string;
  created_at: string;
  facts: ProfileFact[];
  source_document_id: string | null;
};

export type Artifact = {
  id: string;
  kind: string;
  content_type: string;
  size_bytes: number;
  sha256: string;
  created_at: string;
};

export type Job = {
  id: string;
  source_job_id: string;
  title: string;
  employer: string;
  canonical_url: string;
  location_text: string | null;
  work_mode: string;
  status: string;
  status_reason: string | null;
  connector: {
    name: string;
    kind: string;
    tier: string;
    policy_status: string;
    enabled: boolean;
    scope: "shared" | "private";
    authorization_basis: string | null;
    health_status: string;
  };
  first_seen_at: string;
  last_seen_at: string;
  duplicate_of_job_id: string | null;
  sources: JobSource[];
  snapshot_version: number | null;
  latest_snapshot: Record<string, unknown> | null;
};

export type JobSource = {
  job_id: string;
  connector_name: string;
  source_job_id: string;
  canonical_url: string;
  status: string;
  dedup_reason: string | null;
};

export type MatchReason = {
  code: string;
  message: string;
  source: "candidate" | "job" | "policy" | "preference";
  candidate_fact_ids: string[];
  job_evidence_ids: string[];
};

export type MatchBreakdown = {
  score: number;
  reasons: MatchReason[];
  details: Record<string, unknown>;
};

export type Match = {
  id: string;
  job_id: string;
  job_snapshot_id: string;
  profile_version_id: string;
  preference_version_id: string | null;
  policy_version: string;
  extractor_version: string;
  eligibility: "eligible" | "ineligible" | "needs_review" | "unknown";
  eligibility_reasons: MatchReason[];
  fit: MatchBreakdown;
  preference: MatchBreakdown;
  created_at: string;
};

export type MatchReview = {
  id: string;
  match_run_id: string;
  job_id: string;
  status: "pending" | "resolved";
  reasons: MatchReason[];
  job_evidence: Array<{ id: string; field_path: string; quote: string; start_offset: number | null; end_offset: number | null; confidence: number }>;
  candidate_facts: ProfileFact[];
  resolution: { decision: string; note: string } | null;
  resolved_by_user_id: string | null;
  resolved_at: string | null;
  rerun_match_id: string | null;
  created_at: string;
};

export type CVClaim = {
  id: string;
  section: string;
  text: string;
  source_fact_ids: string[];
  job_requirement_ids: string[];
  validation_status: "accepted" | "rejected" | "unresolved";
  validation_reason: string | null;
};

export type CVVersion = {
  id: string;
  job_id: string;
  job_snapshot_id: string;
  profile_version_id: string;
  match_run_id: string | null;
  generator_version: string;
  template_version: string;
  content_hash: string;
  status: "validated" | "approved" | "rejected" | "superseded";
  unresolved_items: string[];
  claims: CVClaim[];
  docx_artifact: Artifact | null;
  pdf_artifact: Artifact | null;
  approved_by_user_id: string | null;
  approved_at: string | null;
  rejected_at: string | null;
  rejection_note: string | null;
  superseded_at: string | null;
  superseded_reason: string | null;
  superseded_detail: Record<string, unknown>;
  created_at: string;
};

export type ApplicationAnswer = {
  question_key: string;
  question_text: string;
  kind: "text" | "email" | "yes_no";
  required: boolean;
  status: "answered" | "unresolved" | "left_blank";
  value: string | null;
  provenance: "profile_fact" | "candidate_entered" | null;
  source_fact_ids: string[];
  reason: string | null;
};

export type ApplicationPackage = {
  id: string;
  revision: number;
  review_hash: string;
  job_snapshot_id: string;
  match_run_id: string;
  cv_version_id: string;
  profile_version_id: string;
  preference_version_id: string | null;
  created_at: string;
  answers: ApplicationAnswer[];
  unresolved: Array<{ code: string; question_key: string | null; reason: string | null }>;
  warnings: Array<{ code: string; question_key?: string | null; reason?: string | null; message?: string }>;
  destination: { kind: "link_out"; url: string; host: string } | null;
  content: {
    job: { title: string; employer: string; location_text: string | null; snapshot_version: number; retrieved_at: string | null; content_hash: string };
    source_policy: { name: string; authorization_basis: string | null; terms_version: string };
    match: { eligibility: { status: string; reasons: MatchReason[] }; fit: MatchBreakdown; preference: MatchBreakdown };
    cv: { cv_version_id: string; content_hash: string; pdf_sha256: string | null };
    data_sharing_notice: { version: string; text: string };
  };
};

export type Application = {
  id: string;
  job_id: string;
  status: "review_package_ready" | "approved" | "review_package_stale" | "skipped" | "applied";
  decision: "open" | "skipped";
  target_kind: "link_out";
  skip_reason: string | null;
  can_approve: boolean;
  package: ApplicationPackage | null;
  binding: { state: "current" | "stale"; drift: Array<{ code: string; message: string }> };
  approval: { id: string; state: "active" | "expired" | "revoked" | "invalidated"; approved_at: string; expires_at: string; revoked_reason: string | null } | null;
  events: Array<{ id: string; event_type: string; occurred_at: string }>;
  submission_available: boolean;
  created_at: string;
  updated_at: string;
};

export const TRACKER_STAGES = ["applied", "interviewing", "offer", "accepted", "rejected", "withdrawn"] as const;
export type TrackerStage = (typeof TRACKER_STAGES)[number];

export type TrackerEntry = {
  id: string;
  application_id: string | null;
  package_id: string | null;
  review_hash: string | null;
  channel: "link_out" | "external" | "sandbox_adapter";
  company: string;
  role_title: string;
  location: string | null;
  job_url: string | null;
  applied_on: string;
  stage: TrackerStage;
  notes: string | null;
  created_at: string;
  updated_at: string;
};

export type TrackerList = {
  entries: TrackerEntry[];
  summary: { total: number; companies: number; by_stage: Record<TrackerStage, number> };
};

export class ApiError extends Error {
  constructor(message: string, readonly status: number, readonly code?: string, readonly existingId?: string) {
    super(message);
  }
}

function csrfToken(): string {
  return document.cookie
    .split("; ")
    .find((part) => part.startsWith("csrf_token="))
    ?.split("=")[1] ?? "";
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = init.method ?? "GET";
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  if (method !== "GET" && method !== "HEAD") {
    headers.set("X-CSRF-Token", decodeURIComponent(csrfToken()));
  }
  const response = await fetch(path, { ...init, headers, credentials: "include" });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const structured = typeof body.detail === "object" && body.detail !== null && !Array.isArray(body.detail);
    const detail = structured ? body.detail.message : Array.isArray(body.detail) ? "Please check the highlighted fields." : body.detail;
    throw new ApiError(detail ?? body.message ?? `Request failed (${response.status})`, response.status, structured ? body.detail.code : undefined, structured ? body.detail.existing_id : undefined);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export async function currentUser(): Promise<User | null> {
  const response = await fetch("/api/v1/auth/me", { credentials: "include" });
  if (response.status === 401) return null;
  if (!response.ok) throw new Error("Unable to load session");
  return response.json() as Promise<User>;
}

// --- agent, recommendations, preferences, suggestions, sources ------------------------------

export type NextAction = { code: string; message: string; count: number | null; target: string };

export type AgentStatus = {
  auto_matching: boolean;
  profile_ready: boolean;
  queued_refreshes: number;
  last_refresh_at: string | null;
  last_ingestion_at: string | null;
  counts: Record<"ready" | "review" | "unmatched" | "ineligible" | "stale" | "applied", number>;
  next_actions: NextAction[];
  policy_version: string;
  rank_formula: string;
};

export type RefreshResult = { matched: number; already_current: number; carried_reviews: number; errors: number; truncated: boolean };

export type Bucket = "ready" | "review" | "unmatched" | "ineligible";

export type Recommendation = {
  bucket: Bucket;
  rank: number | null;
  current: boolean;
  job: { id: string; title: string; employer: string; location_text: string | null; work_mode: string; canonical_url: string; first_seen_at: string; source: string };
  match: {
    id: string;
    eligibility_status: Match["eligibility"];
    eligibility_reasons: MatchReason[];
    fit_score: number;
    fit_details: Record<string, unknown>;
    preference_score: number;
    preference_reasons: MatchReason[];
    location_category: string | null;
    policy_version: string;
    created_at: string;
  } | null;
  application_id: string | null;
  applied: boolean;
  cv_status: CVVersion["status"] | null;
};

export const LOCATION_CATEGORIES = ["global_remote", "remote_from_home", "local_remote", "hybrid_home", "onsite_home", "abroad"] as const;
export type LocationCategory = (typeof LOCATION_CATEGORIES)[number];
export const LOCATION_LABELS: Record<LocationCategory, string> = {
  global_remote: "Global remote (open to several countries, employer abroad)",
  remote_from_home: "Remote from my country (employer abroad)",
  local_remote: "Remote with an employer in my country",
  hybrid_home: "Hybrid in my country",
  onsite_home: "On-site in my country",
  abroad: "Hybrid or on-site abroad (relocation)",
};

export type LocationPolicy = { home_country: string; priorities: LocationCategory[]; preferred_cities: string[]; open_to_relocation: boolean | null };

export type PreferenceRules = {
  target_roles: string[];
  seniority: string[];
  employment_types: string[];
  compensation: { minimum?: number; currency?: string; period?: string };
  work_modes: Array<"remote" | "hybrid" | "onsite" | "unknown">;
  location_preferences: string[];
  work_authorization: Record<string, unknown>;
  notice_period_days: number | null;
  location_policy: LocationPolicy | null;
};

export type Preferences = { id: string; rules: PreferenceRules; created_at: string; status: string };

export type FactSuggestion = {
  fact_type: string;
  value: Record<string, unknown>;
  provenance: string;
  confirmation_status: string;
  evidence_text: string;
  already_in_profile: boolean;
};

export type Suggestions = { source_document_id: string; suggester_version: string; warnings: string[]; suggestions: FactSuggestion[] };

export type Connector = {
  id: string; name: string; kind: string; tier: string; base_domain: string | null; terms_version: string;
  policy_status: string; enabled: boolean; scope: "shared" | "private"; authorization_basis: string | null;
  health_status: string; consecutive_failures: number; last_run_at: string | null; last_success_at: string | null;
  last_error_code: string | null; schedule_interval_seconds: number | null;
};

export type ApplicationSummary = {
  id: string; job_id: string; title: string | null; employer: string | null;
  status: Application["status"]; revision: number | null; unresolved_count: number; updated_at: string;
};

export function jsonBody(body: unknown, method = "POST"): RequestInit {
  return { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}

export function message(reason: unknown, fallback: string): string {
  return reason instanceof Error ? reason.message : fallback;
}
