import { useMemo, useState } from "react";
import { api, Bucket, CVVersion, Job, jsonBody, Match, MatchReview, message, Recommendation } from "../api";
import { EligibilityBadge, Empty, go, Panel, Reasons, Score, useLoad, when } from "../ui";

const BUCKETS: Array<{ key: Bucket | "all"; label: string }> = [
  { key: "all", label: "All" },
  { key: "ready", label: "Ready" },
  { key: "review", label: "Needs review" },
  { key: "unmatched", label: "Not matched" },
  { key: "ineligible", label: "Not eligible" },
];

const CATEGORY_LABELS: Record<string, string> = {
  global_remote: "Global remote",
  remote_from_home: "Remote from your country",
  local_remote: "Local remote",
  hybrid_home: "Hybrid at home",
  onsite_home: "On-site at home",
  abroad: "Abroad",
};

export function Jobs({ id, setError }: { id: string | null; setError: (value: string) => void }) {
  return id ? <JobDetail jobId={id} setError={setError} /> : <JobList setError={setError} />;
}

function JobList({ setError }: { setError: (value: string) => void }) {
  const [bucket, setBucket] = useState<Bucket | "all">("all");
  const [query, setQuery] = useState("");
  const list = useLoad(() => api<Recommendation[]>("/api/v1/recommendations?include_ineligible=true"), setError);
  const items = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return (list.data ?? []).filter((item) =>
      (bucket === "all" ? item.bucket !== "ineligible" : item.bucket === bucket) &&
      (!needle || `${item.job.title} ${item.job.employer} ${item.job.location_text ?? ""}`.toLowerCase().includes(needle)));
  }, [list.data, bucket, query]);
  const counts = useMemo(() => {
    const result: Record<string, number> = { all: 0, ready: 0, review: 0, unmatched: 0, ineligible: 0 };
    for (const item of list.data ?? []) {
      result[item.bucket] += 1;
      if (item.bucket !== "ineligible") result.all += 1;
    }
    return result;
  }, [list.data]);

  return (
    <Panel eyebrow="Jobs from approved sources" title="Ranked jobs" aside={<span className="count">{items.length}</span>}>
      <div className="toolbar">
        <div className="tabs" role="tablist" aria-label="Filter jobs">
          {BUCKETS.map((entry) => (
            <button key={entry.key} role="tab" aria-selected={bucket === entry.key} type="button"
              className={`tab ${bucket === entry.key ? "active" : ""}`} onClick={() => setBucket(entry.key)}>
              {entry.label} <span className="tab-count">{counts[entry.key]}</span>
            </button>
          ))}
        </div>
        <label className="search"><span className="visually-hidden">Search jobs</span>
          <input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search jobs" />
        </label>
      </div>
      <p className="muted small">Eligibility, skill fit, and preference fit are separate. Only jobs not blocked by eligibility get a rank. Unknown is never treated as yes.</p>
      {list.loading ? <p className="muted">Loading…</p> : items.length === 0 ? (
        <Empty title="No jobs here">Jobs come only from approved sources. Turn the agent on or press “Match now” on the overview after adding your profile.</Empty>
      ) : (
        <div className="job-list">
          {items.map((item) => (
            <button key={item.job.id} type="button" className="job-row" onClick={() => go("jobs", item.job.id)}>
              <span className="job-row-main">
                <strong>{item.job.title}</strong>
                <span className="muted small">{item.job.employer} · {item.job.location_text ?? "Location unknown"} · {item.job.work_mode} · {item.job.source}</span>
                <span className="tags">
                  {item.match?.location_category && <span className="tag">{CATEGORY_LABELS[item.match.location_category] ?? item.match.location_category}</span>}
                  {item.match && !item.current && <span className="tag tag-warn">Out of date</span>}
                  {item.cv_status && <span className="tag">CV {item.cv_status}</span>}
                  {item.applied && <span className="tag tag-good">Applied</span>}
                  {item.match?.fit_details.requirements_known === false && <span className="tag">Requirements unknown</span>}
                </span>
              </span>
              <span className="job-row-scores">
                <EligibilityBadge status={item.match?.eligibility_status} />
                <Score label="Fit" value={item.match?.fit_score ?? null} />
                <Score label="Preference" value={item.match?.preference_score ?? null} />
                {item.rank !== null && <span className="rank" title="0.6 × fit + 0.4 × preference">Rank {Math.round(item.rank)}</span>}
              </span>
            </button>
          ))}
        </div>
      )}
    </Panel>
  );
}

function JobDetail({ jobId, setError }: { jobId: string; setError: (value: string) => void }) {
  const job = useLoad(() => api<Job>(`/api/v1/jobs/${jobId}`), setError, [jobId]);
  const match = useLoad(() => api<Match>(`/api/v1/jobs/${jobId}/match`).catch(() => null), setError, [jobId]);
  const [review, setReview] = useState<MatchReview | null>(null);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);

  async function act<T>(work: () => Promise<T>, failure: string): Promise<T | undefined> {
    setBusy(true);
    try {
      const result = await work();
      setError("");
      return result;
    } catch (reason) {
      setError(message(reason, failure));
      return undefined;
    } finally {
      setBusy(false);
    }
  }

  const runMatch = () => act(async () => match.setData(await api<Match>(`/api/v1/jobs/${jobId}/match`, { method: "POST" })), "Unable to match this job");
  const openReview = (matchId: string) => act(async () => setReview(await api<MatchReview>(`/api/v1/jobs/${jobId}/match/${matchId}/review`, { method: "POST" })), "Unable to open the review");
  const resolve = (decision: string) => act(async () => {
    if (!review) return;
    await api<MatchReview>(`/api/v1/reviews/${review.id}/resolve`, jsonBody({ decision, note: note.trim() || "Candidate reviewed this decision." }));
    setReview(null);
    setNote("");
    await match.reload();
  }, "Unable to resolve the review");
  const draftCv = (matchId: string) => act(async () => {
    const version = await api<CVVersion>(`/api/v1/jobs/${jobId}/cv-drafts`, jsonBody({ match_run_id: matchId }));
    go("cvs", version.id);
  }, "Unable to draft a CV");

  if (job.loading && !job.data) return <p className="muted">Loading…</p>;
  if (!job.data) return <Empty title="Job not found" />;
  const posting = job.data;
  const current = match.data ?? null;
  const description = typeof posting.latest_snapshot?.description === "string" ? posting.latest_snapshot.description : null;
  const codes = new Set(review?.reasons.map((reason) => reason.code) ?? []);
  const fitDetails = current?.fit.details ?? {};

  return (
    <div className="view-grid">
      <button className="text-button back" type="button" onClick={() => go("jobs")}>← All jobs</button>
      <Panel eyebrow={`${posting.connector.name} · ${posting.connector.authorization_basis ?? "unknown basis"}`} title={`${posting.title} · ${posting.employer}`}
        aside={<EligibilityBadge status={current?.eligibility} />}>
        <p className="muted">{posting.location_text ?? "Location unknown"} · {posting.work_mode} · first seen {when(posting.first_seen_at)}{posting.status !== "active" ? ` · ${posting.status}` : ""}</p>
        <div className="button-row">
          <a className="secondary" href={posting.canonical_url} target="_blank" rel="noopener noreferrer">Open the posting</a>
          <button className="secondary" type="button" onClick={runMatch} disabled={busy}>{current ? "Match again" : "Match now"}</button>
          {current?.eligibility === "needs_review" && <button type="button" onClick={() => openReview(current.id)} disabled={busy}>Review eligibility</button>}
          {current?.eligibility === "eligible" && <button type="button" onClick={() => draftCv(current.id)} disabled={busy}>Draft a factual CV</button>}
        </div>
        {description && <details className="description"><summary>Job description (untrusted text from the source)</summary><pre>{description}</pre></details>}
      </Panel>

      {review && (
        <Panel eyebrow="Your decision" title="Resolve unknown eligibility">
          <Reasons reasons={review.reasons} />
          <details><summary>Evidence</summary><ul>
            {review.job_evidence.map((item) => <li key={item.id}><strong>{item.field_path}:</strong> {item.quote}</li>)}
            {review.candidate_facts.map((fact) => <li key={fact.id}><strong>{fact.fact_type}:</strong> {JSON.stringify(fact.value)} ({fact.confirmation_status})</li>)}
          </ul></details>
          <label>Note (kept with your decision)<input value={note} maxLength={500} onChange={(event) => setNote(event.target.value)} placeholder="e.g. Recruiter confirmed remote from India is fine" /></label>
          <div className="button-row">
            {codes.has("authorization_geography_unknown") && <button type="button" onClick={() => resolve("accept_remote_geography")}>Accept the remote geography</button>}
            {codes.has("authorization_unconfirmed") && <button type="button" onClick={() => resolve("authorize_for_job_geography")}>I am authorized for this geography</button>}
            {codes.has("sponsorship_unknown") && <button type="button" onClick={() => resolve("confirm_sponsorship_compatible")}>Sponsorship is compatible</button>}
            <button className="danger" type="button" onClick={() => resolve("mark_ineligible")}>Mark not eligible</button>
            <button className="secondary" type="button" onClick={() => setReview(null)}>Cancel</button>
          </div>
          <p className="muted small">Your decision applies only to this version of the posting and your current profile. A changed posting or profile asks again.</p>
        </Panel>
      )}

      {!current ? (
        <Panel title="Not matched yet"><p className="muted">Press “Match now” to evaluate this job against your confirmed facts and preferences.</p></Panel>
      ) : (
        <div className="breakdown">
          <Panel eyebrow="Hard rules" title="Eligibility">
            <EligibilityBadge status={current.eligibility} />
            <Reasons reasons={current.eligibility_reasons} />
          </Panel>
          <Panel eyebrow="From confirmed facts" title="Skill and experience fit">
            <Score label="Fit" value={current.fit.score} />
            {fitDetails.requirements_known === false ? <p className="muted small">No requirements could be identified in this job, so fit is unknown.</p> : (
              <p className="muted small">Required {String(fitDetails.required_matched ?? 0)}/{String(fitDetails.required_total ?? 0)} · preferred {String(fitDetails.preferred_matched ?? 0)}/{String(fitDetails.preferred_total ?? 0)} · from {String(fitDetails.requirements_source ?? "structured")} requirements</p>
            )}
            <Reasons reasons={current.fit.reasons} />
          </Panel>
          <Panel eyebrow="Your choices" title="Preference fit">
            <Score label="Preference" value={current.preference.score} />
            <Reasons reasons={current.preference.reasons} />
            <p className="muted small">Preferences rank jobs; they never make a job eligible.</p>
          </Panel>
        </div>
      )}
      {current && <p className="muted small">Policy {current.policy_version} · extractor {current.extractor_version} · matched {when(current.created_at)}</p>}
    </div>
  );
}
