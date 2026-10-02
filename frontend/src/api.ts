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
    throw new Error(body.detail ?? body.message ?? `Request failed (${response.status})`);
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
