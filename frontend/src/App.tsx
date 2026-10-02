import { ChangeEvent, FormEvent, useEffect, useState } from "react";
import { api, Artifact, currentUser, CVVersion, Job, Match, MatchReview, Profile, User } from "./api";

type AuthMode = "login" | "register";

export default function App() {
  const [user, setUser] = useState<User | null | undefined>(undefined);
  const [error, setError] = useState("");

  useEffect(() => {
    currentUser().then(setUser).catch((reason: Error) => {
      setError(reason.message);
      setUser(null);
    });
  }, []);

  if (user === undefined) return <main className="centered">Loading your workspace…</main>;
  if (user === null) {
    return <AuthPanel onAuthenticated={setUser} error={error} setError={setError} />;
  }
  return <Dashboard user={user} onSignedOut={() => setUser(null)} setError={setError} error={error} />;
}

type DraftFact = {
  fact_type: string;
  value: string;
  provenance: "candidate" | "uploaded_cv" | "approved_edit";
  confirmation_status: "confirmed" | "unknown" | "withdrawn";
  source_ref: string | null;
  evidence_text: string | null;
};

function ProfilePanel({ profile, onUpdated, setError }: { profile: Profile | null; onUpdated: () => Promise<void>; setError: (value: string) => void }) {
  const toDraftFacts = (current: Profile | null): DraftFact[] => (current?.facts ?? []).map((fact) => ({
    fact_type: fact.fact_type,
    value: JSON.stringify(fact.value),
    provenance: fact.provenance as DraftFact["provenance"],
    confirmation_status: fact.confirmation_status,
    source_ref: fact.source_ref,
    evidence_text: fact.evidence_text,
  }));
  const [summary, setSummary] = useState(profile?.summary ?? "");
  const [facts, setFacts] = useState<DraftFact[]>(() => toDraftFacts(profile));
  const [sourceDocumentId, setSourceDocumentId] = useState(profile?.source_document_id ?? undefined);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    setSummary(profile?.summary ?? "");
    setFacts(toDraftFacts(profile));
    setSourceDocumentId(profile?.source_document_id ?? undefined);
  }, [profile]);

  async function upload(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (!file) return;
    try {
      const body = new FormData();
      body.append("file", file);
      const artifact = await api<Artifact>("/api/v1/profile/source-document", { method: "POST", body });
      setSourceDocumentId(artifact.id);
      setError("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to upload document");
    }
  }

  async function save(event: FormEvent) {
    event.preventDefault();
    setSaving(true);
    try {
      const parsedFacts = facts.map((fact) => {
        const value = JSON.parse(fact.value || "{}");
        if (typeof value !== "object" || value === null || Array.isArray(value)) throw new SyntaxError();
        return { ...fact, value };
      });
      await api("/api/v1/profile/versions", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ summary: summary || null, facts: parsedFacts, source_document_id: sourceDocumentId ?? null }) });
      await onUpdated();
      setError("");
    } catch (reason) {
      setError(reason instanceof SyntaxError ? "Each fact value must be a JSON object" : reason instanceof Error ? reason.message : "Unable to save profile");
    } finally {
      setSaving(false);
    }
  }

  async function exportProfile() {
    try {
      const data = await api<Record<string, unknown>>("/api/v1/profile/export");
      const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }));
      const link = document.createElement("a");
      link.href = url;
      link.download = "jobfinder-profile-export.json";
      link.click();
      URL.revokeObjectURL(url);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to export profile");
    }
  }

  async function deleteProfile() {
    if (!window.confirm("Delete your stored profile data and uploaded documents?")) return;
    try {
      await api("/api/v1/profile", { method: "DELETE" });
      await onUpdated();
      setError("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to delete profile");
    }
  }

  const updateFact = (index: number, update: Partial<DraftFact>) => setFacts(facts.map((fact, factIndex) => factIndex === index ? { ...fact, ...update } : fact));
  return <section className="panel"><div className="section-heading"><div><p className="eyebrow">Profile data</p><h2>Review and control your facts</h2></div><div className="button-row"><button className="secondary" type="button" onClick={exportProfile}>Export</button><button className="danger" type="button" onClick={deleteProfile}>Delete</button></div></div><p className="muted small">Only information you provide is saved. Unknown facts stay unknown until you confirm or correct them.</p><form onSubmit={save} className="stack"><label>Source CV (PDF, DOCX, or text)<input type="file" accept="application/pdf,text/plain,.docx" onChange={upload} /></label><label>Candidate summary<textarea value={summary} onChange={(event) => setSummary(event.target.value)} rows={3} placeholder="Optional summary you provide" /></label>{facts.map((fact, index) => <div className="fact-row" key={`${fact.fact_type}-${index}`}><input aria-label={`Fact ${index + 1} type`} value={fact.fact_type} onChange={(event) => updateFact(index, { fact_type: event.target.value })} placeholder="Fact type (e.g. skill)" /><input aria-label={`Fact ${index + 1} value`} value={fact.value} onChange={(event) => updateFact(index, { value: event.target.value })} placeholder='Value JSON, e.g. {"name":"Python"}' /><select value={fact.confirmation_status} onChange={(event) => updateFact(index, { confirmation_status: event.target.value as DraftFact["confirmation_status"] })}><option value="confirmed">Confirmed</option><option value="unknown">Unknown</option><option value="withdrawn">Withdrawn</option></select><button className="secondary" type="button" onClick={() => setFacts(facts.filter((_, factIndex) => factIndex !== index))}>Remove</button></div>)}<div className="button-row"><button className="secondary" type="button" onClick={() => setFacts([...facts, { fact_type: "", value: "{}", provenance: "candidate", confirmation_status: "unknown", source_ref: null, evidence_text: null }])}>Add fact</button><button type="submit" disabled={saving}>{saving ? "Saving…" : "Save new version"}</button></div></form></section>;
}

function AuthPanel({
  onAuthenticated,
  error,
  setError,
}: {
  onAuthenticated: (user: User) => void;
  error: string;
  setError: (value: string) => void;
}) {
  const [mode, setMode] = useState<AuthMode>("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError("");
    try {
      const user = await api<User>(`/api/v1/auth/${mode}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password }),
      });
      onAuthenticated(user);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Authentication failed");
    }
  }

  return (
    <main className="auth-shell">
      <section className="auth-card" aria-labelledby="auth-title">
        <p className="eyebrow">JobFinderAgent · Foundation</p>
        <h1 id="auth-title">Your job search, under your control.</h1>
        <p className="muted">Jobs come only from authorized sources, matching is explainable, and CV drafts use confirmed facts and need your approval. No applications are submitted.</p>
        <form onSubmit={submit} className="stack">
          <label>Email<input type="email" required value={email} onChange={(event) => setEmail(event.target.value)} /></label>
          <label>Password<input type="password" required minLength={12} value={password} onChange={(event) => setPassword(event.target.value)} /></label>
          {error && <p className="error" role="alert">{error}</p>}
          <button type="submit">{mode === "login" ? "Sign in" : "Create account"}</button>
        </form>
        <button className="text-button" type="button" onClick={() => { setMode(mode === "login" ? "register" : "login"); setError(""); }}>
          {mode === "login" ? "Create a new account" : "I already have an account"}
        </button>
      </section>
    </main>
  );
}

function Dashboard({ user, onSignedOut, error, setError }: { user: User; onSignedOut: () => void; error: string; setError: (value: string) => void }) {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [matches, setMatches] = useState<Record<string, Match>>({});
  const [profile, setProfile] = useState<Profile | null>(null);
  const [loading, setLoading] = useState(true);
  const [profileStatus, setProfileStatus] = useState("No profile version saved yet.");
  const [role, setRole] = useState("");
  const [review, setReview] = useState<MatchReview | null>(null);
  const [cv, setCv] = useState<CVVersion | null>(null);

  useEffect(() => {
    Promise.all([api<Job[]>("/api/v1/jobs"), api<Profile | null>("/api/v1/profile")])
      .then(([jobData, currentProfile]) => {
        setJobs(jobData);
        setProfile(currentProfile);
        setProfileStatus(currentProfile ? "Profile version is active." : "No profile version saved yet.");
      })
      .catch((reason: Error) => setError(reason.message))
      .finally(() => setLoading(false));
  }, [setError]);

  async function refreshProfile() {
    const currentProfile = await api<Profile | null>("/api/v1/profile");
    setProfile(currentProfile);
    setProfileStatus(currentProfile ? "Profile version is active." : "No profile version saved yet.");
  }

  async function runMatch(jobId: string) {
    try {
      const result = await api<Match>(`/api/v1/jobs/${jobId}/match`, { method: "POST" });
      setMatches((current) => ({ ...current, [jobId]: result }));
      setError("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to calculate match");
    }
  }

  async function openReview(jobId: string, matchId: string) {
    try {
      const item = await api<MatchReview>(`/api/v1/jobs/${jobId}/match/${matchId}/review`, { method: "POST" });
      setReview(item);
      setError("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to open review");
    }
  }

  async function resolveReview(decision: string) {
    if (!review) return;
    try {
      await api<MatchReview>(`/api/v1/reviews/${review.id}/resolve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ decision, note: "Candidate explicitly reviewed this matching decision." }),
      });
      const latest = await api<Match>(`/api/v1/jobs/${review.job_id}/match`);
      setMatches((current) => ({ ...current, [review.job_id]: latest }));
      setReview(null);
      setError("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to resolve review");
    }
  }

  async function draftCv(jobId: string, matchId: string) {
    try {
      const version = await api<CVVersion>(`/api/v1/jobs/${jobId}/cv-drafts`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ match_run_id: matchId }),
      });
      setCv(version);
      setError("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to draft CV");
    }
  }

  async function decideCv(decision: "approve" | "reject", note: string) {
    if (!cv) return;
    try {
      const version = await api<CVVersion>(`/api/v1/cv-versions/${cv.id}/${decision}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: decision === "reject" ? JSON.stringify({ note }) : undefined,
      });
      setCv(version);
      setError("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to update CV review");
    }
  }

  async function signOut() {
    try {
      await api<void>("/api/v1/auth/logout", { method: "POST" });
      onSignedOut();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to sign out");
    }
  }

  async function savePreferences(event: FormEvent) {
    event.preventDefault();
    try {
      await api("/api/v1/profile/preferences", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target_roles: role ? [role] : [], work_modes: [], work_authorization: {} }),
      });
      setProfileStatus("Preferences saved as a new version.");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to save preferences");
    }
  }

  return (
    <div className="app-shell">
      <header className="topbar"><div><p className="eyebrow">JobFinderAgent</p><h1>Candidate workspace</h1></div><div className="account"><span>{user.email}</span><button className="secondary" onClick={signOut}>Sign out</button></div></header>
      {error && <div className="notice error" role="alert">{error}</div>}
      <main className="dashboard-grid">
        <ProfilePanel profile={profile} onUpdated={refreshProfile} setError={setError} />
        <section className="panel hero"><p className="eyebrow">Phase 3 · Factual CV drafting</p><h2>Read-only job discovery with truthful CV previews</h2><p className="muted">Jobs are provenance-labelled fixture records. Eligibility, skill/experience fit, and preferences are evaluated separately using confirmed facts. CVs are generated locally from verified facts and require your approval; application submission remains unavailable.</p><div className="status-row"><span className="status-dot" />{profileStatus}</div></section>
        {review && <ReviewPanel review={review} onResolve={resolveReview} />}
        {cv && <CVPanel version={cv} onDecision={decideCv} />}
        <section className="panel"><div className="section-heading"><div><p className="eyebrow">Profile preferences</p><h2>Tell us what you want</h2></div></div><form onSubmit={savePreferences} className="inline-form"><label className="grow">Target role<input value={role} onChange={(event) => setRole(event.target.value)} placeholder="e.g. Platform Engineer" /></label><button type="submit">Save version</button></form><p className="muted small">Work authorization remains explicit and is never inferred from location or profile data.</p></section>
        <section className="panel jobs-panel"><div className="section-heading"><div><p className="eyebrow">Catalog</p><h2>Available job records</h2></div><span className="count">{jobs.length}</span></div>{loading ? <p className="muted">Loading catalog…</p> : jobs.length === 0 ? <div className="empty"><h3>No approved job feed is connected</h3><p className="muted">Only authorized sources are ingested; the bundled source is a sanitized local fixture. No scraping or unauthorized integration is active.</p></div> : <div className="job-list">{jobs.map((job) => <JobCard key={job.id} job={job} match={matches[job.id]} onMatch={runMatch} onReview={openReview} onDraft={draftCv} />)}</div>}</section>
      </main>
    </div>
  );
}

function JobCard({ job, match, onMatch, onReview, onDraft }: { job: Job; match?: Match; onMatch: (jobId: string) => Promise<void>; onReview: (jobId: string, matchId: string) => Promise<void>; onDraft: (jobId: string, matchId: string) => Promise<void> }) {
  return <article className="job-card"><div><p className="eyebrow">{job.connector.name} · {job.connector.policy_status}{job.sources.length > 1 ? ` · listed on ${job.sources.length} sources` : ""}{job.status !== "active" ? ` · ${job.status}` : ""}</p><h3>{job.title}</h3><p>{job.employer} · {job.location_text ?? "Location unknown"}</p>{match && <div className="match-summary"><p><strong>Eligibility:</strong> {match.eligibility}</p><p><strong>Skill/experience fit:</strong> {match.fit.score}/100</p><p><strong>Preference fit:</strong> {match.preference.score}/100</p><ul>{match.eligibility_reasons.map((reason) => <li key={reason.code}>{reason.message}</li>)}{match.fit.reasons.slice(0, 3).map((reason) => <li key={reason.code}>{reason.message}</li>)}</ul></div>}</div><div className="job-meta"><span>{job.work_mode}</span><a href={job.canonical_url} target="_blank" rel="noreferrer">Open source</a><button className="secondary" type="button" onClick={() => onMatch(job.id)}>Analyze and match</button>{match?.eligibility === "needs_review" && <button className="secondary" type="button" onClick={() => onReview(job.id, match.id)}>Open human review</button>}{match?.eligibility === "eligible" && <button type="button" onClick={() => onDraft(job.id, match.id)}>Draft factual CV</button>}</div></article>;
}

function ReviewPanel({ review, onResolve }: { review: MatchReview; onResolve: (decision: string) => Promise<void> }) {
  const codes = new Set(review.reasons.map((reason) => reason.code));
  return <section className="panel"><div className="section-heading"><div><p className="eyebrow">Human review</p><h2>Resolve unknown eligibility evidence</h2></div><span className="count">{review.status}</span></div><ul>{review.reasons.map((reason) => <li key={reason.code}>{reason.message}</li>)}</ul><p className="muted small">Job evidence and candidate facts are shown for review. No unknown value is treated as a positive assumption.</p><details><summary>Evidence</summary><ul>{review.job_evidence.map((item) => <li key={item.id}><strong>{item.field_path}:</strong> {item.quote}</li>)}{review.candidate_facts.map((fact) => <li key={fact.id}><strong>{fact.fact_type}:</strong> {JSON.stringify(fact.value)} ({fact.confirmation_status})</li>)}</ul></details><div className="button-row">{codes.has("authorization_geography_unknown") && <button type="button" onClick={() => onResolve("accept_remote_geography")}>Accept remote geography</button>}{codes.has("authorization_unconfirmed") && <button type="button" onClick={() => onResolve("authorize_for_job_geography")}>Confirm authorization</button>}{codes.has("sponsorship_unknown") && <button type="button" onClick={() => onResolve("confirm_sponsorship_compatible")}>Confirm sponsorship compatibility</button>}<button className="danger" type="button" onClick={() => onResolve("mark_ineligible")}>Mark ineligible</button></div></section>;
}

function CVPanel({ version, onDecision }: { version: CVVersion; onDecision: (decision: "approve" | "reject", note: string) => Promise<void> }) {
  const [note, setNote] = useState("");
  const sections = version.claims.reduce<Record<string, typeof version.claims>>((grouped, claim) => {
    grouped[claim.section] = [...(grouped[claim.section] ?? []), claim];
    return grouped;
  }, {});
  return <section className="panel"><div className="section-heading"><div><p className="eyebrow">Factual CV preview</p><h2>Review verified claims before approval</h2></div><span className="count">{version.status}</span></div>{version.unresolved_items.length > 0 && <div className="notice error"><strong>Unresolved information</strong><ul>{version.unresolved_items.map((item) => <li key={item}>{item}</li>)}</ul></div>}<p className="muted small">Every displayed claim has confirmed fact provenance. Job wording only guides selection and does not add candidate facts.</p>{Object.entries(sections).map(([section, claims]) => <div key={section}><h3>{section}</h3><ul>{claims.map((claim) => <li key={claim.id}>{claim.text}<div className="muted small">Verified fact IDs: {claim.source_fact_ids.join(", ")}</div></li>)}</ul></div>)}<div className="button-row"><a className="secondary" href={`/api/v1/cv-versions/${version.id}/artifact/pdf`}>Download PDF</a><a className="secondary" href={`/api/v1/cv-versions/${version.id}/artifact/docx`}>Download DOCX</a>{version.status === "validated" && <><button type="button" onClick={() => onDecision("approve", "")}>Approve CV</button><button className="danger" type="button" onClick={() => onDecision("reject", note)}>Reject CV</button></>}</div>{version.status === "validated" && <label>Rejection note (required to reject)<input value={note} onChange={(event) => setNote(event.target.value)} placeholder="Explain what should be revised" /></label>}</section>;
}
