import { useState } from "react";
import { api, Application, CVVersion, jsonBody, message } from "../api";
import { Empty, go, Panel, useLoad, when } from "../ui";

const CV_STATUS: Record<CVVersion["status"], string> = {
  validated: "Waiting for your approval",
  approved: "Approved",
  rejected: "Rejected",
  superseded: "Out of date",
};

export function Cvs({ id, setError }: { id: string | null; setError: (value: string) => void }) {
  return id ? <CvDetail versionId={id} setError={setError} /> : <CvList setError={setError} />;
}

function CvList({ setError }: { setError: (value: string) => void }) {
  const versions = useLoad(() => api<CVVersion[]>("/api/v1/cv-versions"), setError);
  return (
    <Panel eyebrow="Tailored CVs" title="Your CV versions" aside={<span className="count">{versions.data?.length ?? 0}</span>}>
      <p className="muted small">Each CV is drafted for one job from your confirmed facts only. Versions never change; editing your profile creates new drafts.</p>
      {versions.loading ? <p className="muted">Loading…</p> : !versions.data?.length ? (
        <Empty title="No CVs yet">Open an eligible job and choose “Draft a factual CV”.</Empty>
      ) : (
        <div className="job-list">
          {versions.data.map((version) => (
            <button key={version.id} type="button" className="job-row" onClick={() => go("cvs", version.id)}>
              <span className="job-row-main"><strong>CV for job {version.job_id.slice(0, 8)}</strong><span className="muted small">Drafted {when(version.created_at)} · {version.claims.length} claims · {version.unresolved_items.length} unresolved</span></span>
              <span className={`badge badge-cv-${version.status}`}>{CV_STATUS[version.status]}</span>
            </button>
          ))}
        </div>
      )}
    </Panel>
  );
}

function CvDetail({ versionId, setError }: { versionId: string; setError: (value: string) => void }) {
  const version = useLoad(() => api<CVVersion>(`/api/v1/cv-versions/${versionId}`), setError, [versionId]);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);

  async function decide(decision: "approve" | "reject") {
    setBusy(true);
    try {
      version.setData(await api<CVVersion>(`/api/v1/cv-versions/${versionId}/${decision}`, decision === "reject" ? jsonBody({ note }) : { method: "POST" }));
      setError("");
    } catch (reason) {
      setError(message(reason, "Unable to update the CV"));
    } finally {
      setBusy(false);
    }
  }

  async function prepare(cv: CVVersion) {
    setBusy(true);
    try {
      const application = await api<Application>(`/api/v1/jobs/${cv.job_id}/applications`, jsonBody({ cv_version_id: cv.id }));
      go("applications", application.id);
    } catch (reason) {
      setError(message(reason, "Unable to prepare the application package"));
    } finally {
      setBusy(false);
    }
  }

  const cv = version.data;
  if (!cv) return <p className="muted">Loading…</p>;
  const sections = cv.claims.reduce<Record<string, CVVersion["claims"]>>((grouped, claim) => {
    grouped[claim.section] = [...(grouped[claim.section] ?? []), claim];
    return grouped;
  }, {});
  return (
    <div className="view-grid">
      <button className="text-button back" type="button" onClick={() => go("cvs")}>← All CVs</button>
      <Panel eyebrow="Factual CV preview" title="Check every claim before approving" aside={<span className={`badge badge-cv-${cv.status}`}>{CV_STATUS[cv.status]}</span>}>
        {cv.unresolved_items.length > 0 && <div className="notice error"><strong>Information the CV could not include</strong><ul>{cv.unresolved_items.map((item) => <li key={item}>{item}</li>)}</ul><p className="small">Add or confirm these facts in your profile if they are true, then draft again.</p></div>}
        <p className="muted small">Every claim comes from a confirmed fact. The job's wording only chooses what to show; it never adds anything about you.</p>
        {Object.entries(sections).map(([section, claims]) => (
          <div key={section} className="cv-section"><h3>{section}</h3><ul>{claims.map((claim) => <li key={claim.id}>{claim.text}<div className="muted small">Facts: {claim.source_fact_ids.join(", ")}</div></li>)}</ul></div>
        ))}
        <div className="button-row">
          <a className="secondary" href={`/api/v1/cv-versions/${cv.id}/artifact/pdf`}>Download PDF</a>
          <a className="secondary" href={`/api/v1/cv-versions/${cv.id}/artifact/docx`}>Download DOCX</a>
          <button className="secondary" type="button" onClick={() => go("jobs", cv.job_id)}>Open the job</button>
          {cv.status === "validated" && <button type="button" disabled={busy} onClick={() => decide("approve")}>Approve this CV</button>}
          {cv.status === "approved" && <button type="button" disabled={busy} onClick={() => prepare(cv)}>Prepare the application</button>}
        </div>
        {cv.status === "validated" && (
          <div className="inline-form">
            <label className="grow">Why reject it? (required)<input value={note} onChange={(event) => setNote(event.target.value)} placeholder="What should change" /></label>
            <button className="danger" type="button" disabled={busy || !note.trim()} onClick={() => decide("reject")}>Reject</button>
          </div>
        )}
        <p className="muted small">Generator {cv.generator_version} · template {cv.template_version} · content hash <code>{cv.content_hash}</code></p>
      </Panel>
    </div>
  );
}
