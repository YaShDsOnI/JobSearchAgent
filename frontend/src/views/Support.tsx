import { FormEvent, useState } from "react";
import { api, jsonBody, message } from "../api";
import { Empty, Panel, useLoad, when } from "../ui";

type Grant = {
  id: string; support_email: string; scope: string; reason: string; state: "active" | "expired" | "revoked";
  granted_at: string; expires_at: string; revoked_at: string | null; last_viewed_at: string | null; view_count: number;
};
type Case = { grant_id: string; candidate: string; reason: string; expires_at: string };

/** Candidate side: grant a support account time-boxed, redacted, read-only access. */
export function SupportGrants({ setError }: { setError: (value: string) => void }) {
  const grants = useLoad(() => api<Grant[]>("/api/v1/support/grants"), setError);
  const [email, setEmail] = useState("");
  const [hours, setHours] = useState("24");
  const [reason, setReason] = useState("");

  async function grant(event: FormEvent) {
    event.preventDefault();
    try {
      await api("/api/v1/support/grants", jsonBody({ support_email: email, hours: Number(hours), reason }));
      setEmail("");
      setReason("");
      await grants.reload();
      setError("");
    } catch (reason) {
      setError(message(reason, "Unable to grant access"));
    }
  }

  async function revoke(id: string) {
    try {
      await api(`/api/v1/support/grants/${id}/revoke`, { method: "POST" });
      await grants.reload();
    } catch (reason) {
      setError(message(reason, "Unable to revoke"));
    }
  }

  return (
    <Panel eyebrow="Help from support" title="Support access">
      <p className="muted">If you ask support for help, you can let one support account see a summary of your case for a limited time. They see counts and statuses only: never your CV, fact values, answers, or contact details. Every view is recorded here, and you can revoke access at any time.</p>
      <form className="form-grid" onSubmit={grant}>
        <label>Support account email<input type="email" required value={email} onChange={(event) => setEmail(event.target.value)} /></label>
        <label>For how long<select value={hours} onChange={(event) => setHours(event.target.value)}><option value="1">1 hour</option><option value="24">24 hours</option><option value="72">72 hours</option></select></label>
        <label>Why<input required maxLength={500} value={reason} onChange={(event) => setReason(event.target.value)} placeholder="e.g. My matches look wrong" /></label>
        <div className="button-row"><button type="submit">Grant access</button></div>
      </form>
      {grants.data && grants.data.length > 0 && (
        <div className="table-wrap">
          <table className="data-table">
            <thead><tr><th>Support account</th><th>Status</th><th>Until</th><th>Views</th><th /></tr></thead>
            <tbody>
              {grants.data.map((item) => (
                <tr key={item.id}>
                  <td data-label="Support account">{item.support_email}<div className="muted small">{item.reason}</div></td>
                  <td data-label="Status"><span className={`badge ${item.state === "active" ? "badge-needs_review" : "badge-unmatched"}`}>{item.state}</span></td>
                  <td data-label="Until">{when(item.revoked_at ?? item.expires_at)}</td>
                  <td data-label="Views">{item.view_count}{item.last_viewed_at ? ` · last ${when(item.last_viewed_at)}` : ""}</td>
                  <td>{item.state === "active" && <button className="secondary" type="button" onClick={() => revoke(item.id)}>Revoke</button>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}

/** Support side: open cases and their redacted summaries. */
export function SupportCases({ id, setError }: { id: string | null; setError: (value: string) => void }) {
  const cases = useLoad(() => api<Case[]>("/api/v1/support/cases"), setError);
  const summary = useLoad(() => (id ? api<Record<string, unknown>>(`/api/v1/support/cases/${id}`) : Promise.resolve(null)), setError, [id]);
  return (
    <div className="view-grid">
      <Panel eyebrow="Granted by candidates" title="Open cases" aside={<span className="count">{cases.data?.length ?? 0}</span>}>
        <p className="muted small">You can see a case only while its candidate's grant is active. Every view is recorded and shown to the candidate.</p>
        {!cases.data?.length ? <Empty title="No open cases" /> : (
          <div className="job-list">
            {cases.data.map((item) => (
              <a key={item.grant_id} className="job-row" href={`#/support/${item.grant_id}`}>
                <span className="job-row-main"><strong>{item.candidate}</strong><span className="muted small">{item.reason}</span></span>
                <span className="muted small">until {when(item.expires_at)}</span>
              </a>
            ))}
          </div>
        )}
      </Panel>
      {id && summary.data && <Panel eyebrow="Redacted summary" title="Case summary"><Summary value={summary.data} /></Panel>}
    </div>
  );
}

export function Metrics({ setError }: { setError: (value: string) => void }) {
  const metrics = useLoad(() => api<Record<string, unknown>>("/api/v1/ops/metrics"), setError);
  return (
    <Panel eyebrow="Pilot" title="Aggregate pilot metrics">
      <p className="muted small">Counts across all candidates. Counts from 1 to 4 are shown as “&lt;5” so that no number points at one person.</p>
      {metrics.data ? <Summary value={metrics.data} /> : <p className="muted">Loading…</p>}
    </Panel>
  );
}

function Summary({ value }: { value: Record<string, unknown> }) {
  return (
    <dl className="summary">
      {Object.entries(value).map(([key, item]) => (
        <div key={key} className="summary-row">
          <dt>{key.replace(/_/g, " ")}</dt>
          <dd>{item !== null && typeof item === "object" && !Array.isArray(item) ? <Summary value={item as Record<string, unknown>} />
            : Array.isArray(item) ? (item.length === 0 ? "none" : <ul>{item.slice(0, 30).map((entry, index) => <li key={index}>{typeof entry === "object" ? Object.values(entry as object).join(" · ") : String(entry)}</li>)}</ul>)
            : String(item ?? "—")}</dd>
        </div>
      ))}
    </dl>
  );
}
