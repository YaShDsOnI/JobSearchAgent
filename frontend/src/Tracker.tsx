import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { ApiError, TRACKER_STAGES, TrackerEntry, TrackerList, TrackerStage, api } from "./api";

const STAGE_LABELS: Record<TrackerStage, string> = {
  applied: "Applied",
  interviewing: "Interviewing",
  offer: "Offer",
  accepted: "Accepted",
  rejected: "Rejected",
  withdrawn: "Withdrawn",
};

const CHANNEL_LABELS: Record<TrackerEntry["channel"], string> = {
  link_out: "Prepared here · you applied on the employer's site",
  external: "Added by you",
  sandbox_adapter: "Sandbox submission",
};

function today(): string {
  const now = new Date();
  return new Date(now.getTime() - now.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
}

function formatDate(value: string): string {
  const [year, month, day] = value.split("-").map(Number);
  return new Date(year, month - 1, day).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

function json(body: unknown, method = "POST"): RequestInit {
  return { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}

type Draft = { company: string; role_title: string; location: string; job_url: string; applied_on: string; stage: TrackerStage; notes: string };
const emptyDraft = (): Draft => ({ company: "", role_title: "", location: "", job_url: "", applied_on: today(), stage: "applied", notes: "" });

export function TrackerPanel({ refreshKey, setError }: { refreshKey: number; setError: (value: string) => void }) {
  const [data, setData] = useState<TrackerList | null>(null);
  const [stage, setStage] = useState<TrackerStage | "">("");
  const [query, setQuery] = useState("");
  const [adding, setAdding] = useState(false);
  const [draft, setDraft] = useState<Draft>(emptyDraft);
  const [duplicateOf, setDuplicateOf] = useState<string | null>(null);

  const load = useCallback(async () => {
    const params = new URLSearchParams();
    if (stage) params.set("stage", stage);
    if (query.trim()) params.set("q", query.trim());
    try {
      setData(await api<TrackerList>(`/api/v1/tracker${params.size ? `?${params}` : ""}`));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to load your applications");
    }
  }, [stage, query, setError]);

  useEffect(() => { void load(); }, [load, refreshKey]);

  const duplicate = useMemo(() => data?.entries.find((entry) => entry.id === duplicateOf) ?? null, [data, duplicateOf]);

  async function save(event: FormEvent, allowDuplicate = false) {
    event.preventDefault();
    const body = {
      company: draft.company,
      role_title: draft.role_title,
      location: draft.location || null,
      job_url: draft.job_url || null,
      applied_on: draft.applied_on || null,
      stage: draft.stage,
      notes: draft.notes || null,
      allow_duplicate: allowDuplicate,
    };
    try {
      await api<TrackerEntry>("/api/v1/tracker", json(body));
      setDraft(emptyDraft());
      setDuplicateOf(null);
      setAdding(false);
      setError("");
      await load();
    } catch (reason) {
      if (reason instanceof ApiError && reason.code === "possible_duplicate") {
        setDuplicateOf(reason.existingId ?? null);
        return;
      }
      setError(reason instanceof Error ? reason.message : "Unable to add the application");
    }
  }

  async function update(entry: TrackerEntry, changes: Partial<Pick<TrackerEntry, "stage" | "notes">>) {
    try {
      await api<TrackerEntry>(`/api/v1/tracker/${entry.id}`, json(changes, "PATCH"));
      setError("");
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to update the application");
    }
  }

  async function remove(entry: TrackerEntry) {
    const warning = entry.channel === "link_out"
      ? `Remove "${entry.role_title}" at ${entry.company} from your tracker? The prepared package becomes editable again.`
      : `Remove "${entry.role_title}" at ${entry.company} from your tracker?`;
    if (!window.confirm(warning)) return;
    try {
      await api<void>(`/api/v1/tracker/${entry.id}`, { method: "DELETE" });
      setError("");
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to remove the application");
    }
  }

  const summary = data?.summary;
  const filtered = Boolean(stage || query.trim());

  return (
    <section className="panel tracker-panel" aria-labelledby="tracker-heading">
      <div className="section-heading">
        <div><p className="eyebrow">Application tracker</p><h2 id="tracker-heading">Where you have applied</h2></div>
        <button className="secondary" type="button" onClick={() => { setAdding(!adding); setDuplicateOf(null); setDraft(emptyDraft()); }}>{adding ? "Cancel" : "Add an application"}</button>
      </div>

      {summary && (
        <div className="tracker-summary" aria-label="Summary">
          <div className="stat"><strong>{summary.total}</strong><span>applications</span></div>
          <div className="stat"><strong>{summary.companies}</strong><span>{summary.companies === 1 ? "company" : "companies"}</span></div>
          {TRACKER_STAGES.filter((item) => summary.by_stage[item] > 0).map((item) => (
            <button key={item} type="button" className={`stage-chip stage-${item}${stage === item ? " active" : ""}`} aria-pressed={stage === item} onClick={() => setStage(stage === item ? "" : item)}>
              {STAGE_LABELS[item]} · {summary.by_stage[item]}
            </button>
          ))}
        </div>
      )}

      {adding && (
        <form className="tracker-form" onSubmit={(event) => save(event)}>
          <p className="muted small">For applications you made outside JobFinderAgent. Applications you prepare here are added with "Mark as applied" on the review package.</p>
          <div className="tracker-form-grid">
            <label>Company<input required maxLength={255} value={draft.company} onChange={(event) => setDraft({ ...draft, company: event.target.value })} /></label>
            <label>Role<input required maxLength={255} value={draft.role_title} onChange={(event) => setDraft({ ...draft, role_title: event.target.value })} /></label>
            <label><span>Location <span className="optional">(optional)</span></span><input maxLength={255} value={draft.location} onChange={(event) => setDraft({ ...draft, location: event.target.value })} /></label>
            <label><span>Job link <span className="optional">(optional)</span></span><input type="url" maxLength={2048} placeholder="https://" value={draft.job_url} onChange={(event) => setDraft({ ...draft, job_url: event.target.value })} /></label>
            <label>Applied on<input type="date" required max={today()} min="2000-01-01" value={draft.applied_on} onChange={(event) => setDraft({ ...draft, applied_on: event.target.value })} /></label>
            <label>Stage<select value={draft.stage} onChange={(event) => setDraft({ ...draft, stage: event.target.value as TrackerStage })}>{TRACKER_STAGES.map((item) => <option key={item} value={item}>{STAGE_LABELS[item]}</option>)}</select></label>
          </div>
          <label><span>Notes <span className="optional">(optional)</span></span><textarea rows={2} maxLength={2000} value={draft.notes} onChange={(event) => setDraft({ ...draft, notes: event.target.value })} /></label>
          {duplicateOf && (
            <div className="notice error" role="alert">
              <strong>This looks like one you already tracked</strong>
              {duplicate && <p className="small">{duplicate.role_title} at {duplicate.company}, applied {formatDate(duplicate.applied_on)}</p>}
              <div className="button-row"><button type="button" onClick={(event) => save(event, true)}>Add it anyway</button><button className="secondary" type="button" onClick={() => setDuplicateOf(null)}>Go back</button></div>
            </div>
          )}
          {!duplicateOf && <div className="button-row"><button type="submit">Save application</button></div>}
        </form>
      )}

      <div className="tracker-filters">
        <label className="grow">Search company or role<input type="search" maxLength={100} value={query} onChange={(event) => setQuery(event.target.value)} placeholder="e.g. Globex or engineer" /></label>
        <label>Stage<select value={stage} onChange={(event) => setStage(event.target.value as TrackerStage | "")}><option value="">All stages</option>{TRACKER_STAGES.map((item) => <option key={item} value={item}>{STAGE_LABELS[item]}</option>)}</select></label>
      </div>

      {!data ? <p className="muted">Loading your applications…</p>
        : data.entries.length === 0 ? (
          <div className="empty">
            {filtered ? <><h3>No applications match these filters</h3><button className="text-button" type="button" onClick={() => { setStage(""); setQuery(""); }}>Clear filters</button></>
              : <><h3>No applications tracked yet</h3><p className="muted">When you apply for a job you prepared here, choose "Mark as applied" on its review package. You can also add applications you made elsewhere.</p></>}
          </div>
        ) : (
          <div className="tracker-table-wrap">
            <table className="tracker-table">
              <colgroup><col className="col-company" /><col className="col-role" /><col className="col-date" /><col className="col-stage" /><col className="col-actions" /></colgroup>
              <thead><tr><th scope="col">Company</th><th scope="col">Role</th><th scope="col">Applied</th><th scope="col">Stage</th><th scope="col"><span className="visually-hidden">Actions</span></th></tr></thead>
              <tbody>
                {data.entries.map((entry) => (
                  <tr key={entry.id}>
                    <td data-label="Company"><strong>{entry.company}</strong>{entry.location && <div className="muted small">{entry.location}</div>}</td>
                    <td data-label="Role">{entry.job_url ? <a href={entry.job_url} target="_blank" rel="noopener noreferrer nofollow">{entry.role_title}</a> : entry.role_title}<div className="muted small">{CHANNEL_LABELS[entry.channel]}</div>{entry.notes && <div className="tracker-notes small">{entry.notes}</div>}</td>
                    <td data-label="Applied"><time dateTime={entry.applied_on}>{formatDate(entry.applied_on)}</time></td>
                    <td data-label="Stage"><select aria-label={`Stage for ${entry.role_title} at ${entry.company}`} className={`stage-select stage-${entry.stage}`} value={entry.stage} onChange={(event) => update(entry, { stage: event.target.value as TrackerStage })}>{TRACKER_STAGES.map((item) => <option key={item} value={item}>{STAGE_LABELS[item]}</option>)}</select></td>
                    <td className="tracker-actions"><button className="text-button danger-text" type="button" onClick={() => remove(entry)} aria-label={`Remove ${entry.role_title} at ${entry.company}`}>Remove</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
    </section>
  );
}

export function MarkAppliedControl({ onMark }: { onMark: (appliedOn: string) => Promise<void> }) {
  const [open, setOpen] = useState(false);
  const [appliedOn, setAppliedOn] = useState(today());
  if (!open) return <button className="secondary" type="button" onClick={() => setOpen(true)}>Mark as applied</button>;
  return (
    <div className="mark-applied">
      <label>Date you applied<input type="date" required max={today()} min="2000-01-01" value={appliedOn} onChange={(event) => setAppliedOn(event.target.value)} /></label>
      <button type="button" onClick={() => onMark(appliedOn).then(() => setOpen(false))}>Save to tracker</button>
      <button className="secondary" type="button" onClick={() => setOpen(false)}>Cancel</button>
    </div>
  );
}
