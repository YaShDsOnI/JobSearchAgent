import { useState } from "react";
import { api, Application, ApplicationSummary, jsonBody, message } from "../api";
import { SubmissionPanel } from "../Submission";
import { MarkAppliedControl } from "../Tracker";
import { Empty, go, Panel, useLoad, when } from "../ui";

const STATUS_LABELS: Record<Application["status"], string> = {
  review_package_ready: "Ready for your review",
  approved: "Approved (nothing sent)",
  review_package_stale: "Out of date",
  applied: "Applied",
  skipped: "Skipped",
};

const REASONS: Record<string, string> = {
  no_confirmed_fact: "no confirmed fact in your profile; add one or type an answer",
  job_geography_unknown: "the job does not state its country",
  conflicting_facts: "your confirmed facts disagree",
};

function reasonText(reason: string | null): string {
  return reason ? REASONS[reason] ?? reason.replace(/_/g, " ") : "no answer";
}

export function Applications({ id, setError }: { id: string | null; setError: (value: string) => void }) {
  return id ? <ApplicationDetail applicationId={id} setError={setError} /> : <ApplicationList setError={setError} />;
}

function ApplicationList({ setError }: { setError: (value: string) => void }) {
  const items = useLoad(() => api<ApplicationSummary[]>("/api/v1/applications"), setError);
  return (
    <Panel eyebrow="Review packages" title="Your applications" aside={<span className="count">{items.data?.length ?? 0}</span>}>
      <p className="muted small">A package binds the exact job, CV, and answers you approve. You apply yourself on the employer's site; only the local sandbox ATS can be submitted from here, after your final check.</p>
      {items.loading ? <p className="muted">Loading…</p> : !items.data?.length ? (
        <Empty title="No packages yet">Approve a CV, then choose “Prepare the application”.</Empty>
      ) : (
        <div className="job-list">
          {items.data.map((item) => (
            <button key={item.id} type="button" className="job-row" onClick={() => go("applications", item.id)}>
              <span className="job-row-main"><strong>{item.title ?? "Untitled job"}</strong><span className="muted small">{item.employer ?? "Unknown employer"} · updated {when(item.updated_at)}{item.unresolved_count ? ` · ${item.unresolved_count} unresolved` : ""}</span></span>
              <span className={`badge badge-app-${item.status}`}>{STATUS_LABELS[item.status]}</span>
            </button>
          ))}
        </div>
      )}
    </Panel>
  );
}

function ApplicationDetail({ applicationId, setError }: { applicationId: string; setError: (value: string) => void }) {
  const loaded = useLoad(() => api<Application>(`/api/v1/applications/${applicationId}`), setError, [applicationId]);
  const [drafts, setDrafts] = useState<Record<string, string>>({});

  async function action(path: string, init: RequestInit, failure: string) {
    try {
      loaded.setData(await api<Application>(path, init));
      setError("");
    } catch (reason) {
      setError(message(reason, failure));
    }
  }

  async function markApplied(appliedOn: string) {
    try {
      await api(`/api/v1/applications/${applicationId}/applied`, jsonBody({ applied_on: appliedOn }));
      await loaded.reload();
      setError("");
    } catch (reason) {
      setError(message(reason, "Unable to mark as applied"));
    }
  }

  const application = loaded.data;
  if (!application) return <p className="muted">Loading…</p>;
  const pkg = application.package;
  if (!pkg) return <Empty title="This application has no package" />;
  const job = pkg.content.job;
  const destination = pkg.destination && pkg.destination.url.startsWith("https://") ? pkg.destination : null;
  const applied = application.status === "applied";
  const editable = !applied && application.decision === "open" && application.binding.state === "current";
  const base = `/api/v1/applications/${application.id}`;

  return (
    <div className="view-grid">
      <button className="text-button back" type="button" onClick={() => go("applications")}>← All applications</button>
      <Panel eyebrow={`Application review package · revision ${pkg.revision}`} title={`${job.title} · ${job.employer}`} aside={<span className={`badge badge-app-${application.status}`}>{STATUS_LABELS[application.status]}</span>}>
        <div className="notice info"><strong>Nothing is sent by JobFinderAgent.</strong> {pkg.content.data_sharing_notice.text}</div>
        {applied && !application.submission_available && <div className="notice info"><strong>You marked this as applied.</strong> It is in your tracker, and this package revision is the record of what you prepared.</div>}
        {!applied && application.binding.state === "stale" && (
          <div className="notice error"><strong>This package is out of date.</strong><ul>{application.binding.drift.map((item) => <li key={item.code}>{item.message}</li>)}</ul>
            {application.decision === "open" && <button className="secondary" type="button" onClick={() => action(`/api/v1/jobs/${application.job_id}/applications`, jsonBody({ cv_version_id: pkg.cv_version_id }), "Unable to rebuild the package")}>Rebuild the package</button>}
          </div>
        )}
        <p className="muted small">Source: {pkg.content.source_policy.name} ({pkg.content.source_policy.authorization_basis ?? "unknown basis"}, terms {pkg.content.source_policy.terms_version}) · snapshot v{job.snapshot_version} · {job.location_text ?? "Location unknown"}</p>
        <div className="match-summary"><p><strong>Eligibility:</strong> {pkg.content.match.eligibility.status}</p><p><strong>Skill and experience fit:</strong> {pkg.content.match.fit.score}/100</p><p><strong>Preference fit:</strong> {pkg.content.match.preference.score}/100</p></div>

        <h3>CV</h3>
        <p className="muted small">Approved CV {pkg.content.cv.cv_version_id} · PDF SHA-256 <code>{pkg.content.cv.pdf_sha256 ?? "unavailable"}</code></p>
        <div className="button-row"><a className="secondary" href={`/api/v1/cv-versions/${pkg.cv_version_id}/artifact/pdf`}>Download the exact PDF</a><a className="secondary" href={`/api/v1/cv-versions/${pkg.cv_version_id}/artifact/docx`}>Download DOCX</a></div>

        <h3>Answers</h3>
        <p className="muted small">Answers come only from confirmed facts or from what you type here. Unknown stays unresolved. * required</p>
        <div className="answer-list">
          {pkg.answers.map((item) => (
            <div className={`answer-row ${item.status}`} key={item.question_key}>
              <div><strong>{item.question_text}{item.required ? " *" : ""}</strong>
                <div className="muted small">{item.status === "answered" ? `${item.value} · ${item.provenance === "candidate_entered" ? "entered by you" : `from confirmed fact ${item.source_fact_ids.join(", ")}`}` : item.status === "left_blank" ? "Will be left blank" : `Unresolved: ${reasonText(item.reason)}`}</div>
              </div>
              {editable && (
                <div className="answer-edit">
                  {item.kind === "yes_no" ? (
                    <select aria-label={`Answer: ${item.question_text}`} value={drafts[item.question_key] ?? ""} onChange={(event) => setDrafts({ ...drafts, [item.question_key]: event.target.value })}><option value="">Choose…</option><option value="yes">Yes</option><option value="no">No</option></select>
                  ) : (
                    <input aria-label={`Answer: ${item.question_text}`} maxLength={500} value={drafts[item.question_key] ?? ""} onChange={(event) => setDrafts({ ...drafts, [item.question_key]: event.target.value })} placeholder="Your answer" />
                  )}
                  <button className="secondary" type="button" disabled={!drafts[item.question_key]} onClick={() => action(`${base}/answers/${item.question_key}`, jsonBody({ value: drafts[item.question_key] }, "PUT"), "Unable to save the answer").then(() => setDrafts({ ...drafts, [item.question_key]: "" }))}>Save</button>
                  {item.provenance === "candidate_entered" && <button className="secondary" type="button" onClick={() => action(`${base}/answers/${item.question_key}`, jsonBody({ value: null }, "PUT"), "Unable to clear the answer")}>Clear</button>}
                </div>
              )}
            </div>
          ))}
        </div>
        {pkg.warnings.length > 0 && <details><summary>Warnings ({pkg.warnings.length})</summary><ul>{pkg.warnings.map((item, index) => <li key={`${item.code}-${index}`}>{item.message ?? `${item.question_key}: ${item.reason}`}</li>)}</ul></details>}
        {application.approval && <p className="muted small">Approval: {application.approval.state}{application.approval.state === "active" ? ` until ${new Date(application.approval.expires_at).toLocaleString()}` : ""}{application.approval.revoked_reason ? ` (${application.approval.revoked_reason})` : ""}</p>}
        <p className="muted small">Review hash <code>{pkg.review_hash}</code></p>
        <div className="button-row">
          {!applied && application.can_approve && <button type="button" onClick={() => action(`${base}/approve`, jsonBody({ review_hash: pkg.review_hash }), "Unable to approve")}>Approve this exact package</button>}
          {application.approval?.state === "active" && <button className="secondary" type="button" onClick={() => action(`${base}/approval/revoke`, { method: "POST" }, "Unable to revoke")}>Revoke approval</button>}
          {!applied && destination && application.decision === "open" && <a className="secondary" href={destination.url} target="_blank" rel="noopener noreferrer">Apply yourself on {destination.host}</a>}
          {!applied && !application.submission_available && application.decision === "open" && <MarkAppliedControl onMark={markApplied} />}
          {!applied && application.decision === "open" && <button className="danger" type="button" onClick={() => action(`${base}/skip`, jsonBody({}), "Unable to skip")}>Skip this job</button>}
        </div>
        {application.submission_available && <SubmissionPanel applicationId={application.id} reviewHash={pkg.review_hash} approved={application.approval?.state === "active" && application.binding.state === "current"} onChanged={loaded.reload} setError={setError} />}
      </Panel>
    </div>
  );
}
