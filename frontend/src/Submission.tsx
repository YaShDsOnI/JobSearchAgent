import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, api } from "./api";

export type Submission = {
  id: string;
  state: "queued" | "filling" | "final_review" | "submit_queued" | "submitting" | "submitted" | "submitted_unverified" | "not_received" | "paused" | "blocked" | "failed" | "cancelled";
  pause_reason: string | null;
  error_code: string | null;
  destination_url: string;
  fields: Array<{ question_key: string; label: string; display_value: string; provenance: string | null; sent: boolean }>;
  resume_sha256: string | null;
  fill_hash: string | null;
  confirmation_id: string | null;
  verification: "confirmation_page" | "candidate_reported" | null;
  submit_clicked_at: string | null;
  can_confirm: boolean;
  can_cancel: boolean;
  can_resolve: boolean;
};

const WORKING = new Set(["queued", "filling", "submit_queued", "submitting"]);

const PAUSE_TEXT: Record<string, string> = {
  captcha: "The form shows a CAPTCHA. JobFinderAgent never solves or bypasses CAPTCHAs.",
  mfa_or_login: "The site asked for a sign-in or one-time code. That is yours to do; the runner stopped.",
  unknown_required_field: "The form has a required question the adapter does not know. It will not guess an answer.",
  attestation_required: "The form asks you to certify or sign something. Only you can do that.",
  offsite_navigation: "The page tried to send the browser to another site, so the runner stopped.",
  dialog: "The page opened a pop-up dialog, so the runner stopped.",
  form_changed: "The form changed after you reviewed it. Nothing was sent.",
  form_contract_mismatch: "The form did not match what the adapter expects. Nothing was sent.",
  page_unavailable: "The sandbox page could not be loaded. Nothing was sent.",
  cv_hash_mismatch: "The CV file did not match the approved package. Nothing was sent.",
};

const BLOCK_TEXT: Record<string, string> = {
  submission_disabled: "Submission is switched off for this deployment.",
  destination_disabled: "The sandbox destination is switched off.",
  destination_not_approved: "The sandbox destination is not approved.",
  destination_changed: "The sandbox address changed since you approved the package.",
  approval_not_active: "Your approval expired or was revoked. Approve the package again.",
  binding_stale: "The package is out of date. Rebuild and approve it again.",
  package_changed: "The package changed. Approve the new package to submit it.",
  cancelled: "You cancelled before anything was sent.",
};

function json(body: unknown): RequestInit {
  return { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}

export function SubmissionPanel({ applicationId, reviewHash, approved, onChanged, setError }: {
  applicationId: string;
  reviewHash: string;
  approved: boolean;
  onChanged: () => Promise<void>;
  setError: (value: string) => void;
}) {
  const [submission, setSubmission] = useState<Submission | null | undefined>(undefined);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setSubmission(await api<Submission>(`/api/v1/applications/${applicationId}/submission`));
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 404) setSubmission(null);
      else setError(reason instanceof Error ? reason.message : "Unable to load the submission");
    }
  }, [applicationId, setError]);

  useEffect(() => { void load(); }, [load]);

  const working = submission ? WORKING.has(submission.state) : false;
  useEffect(() => {
    if (!working) return;
    const timer = window.setInterval(() => { void load(); }, 1500);
    return () => window.clearInterval(timer);
  }, [working, load]);

  // Refresh the application and tracker once when this submission is recorded as submitted.
  const onChangedRef = useRef(onChanged);
  onChangedRef.current = onChanged;
  const notified = useRef<string | null>(null);
  const submittedId = submission?.state === "submitted" ? submission.id : null;
  useEffect(() => {
    if (submittedId && notified.current !== submittedId) {
      notified.current = submittedId;
      void onChangedRef.current();
    }
  }, [submittedId]);

  async function act(path: string, init: RequestInit, failure: string) {
    setBusy(true);
    try {
      setSubmission(await api<Submission>(`/api/v1/applications/${applicationId}/submission${path}`, init));
      setError("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : failure);
    } finally {
      setBusy(false);
    }
  }

  if (submission === undefined) return null;

  return (
    <div className="submission-panel" aria-live="polite">
      <h3>Submit to the sandbox ATS</h3>
      <p className="muted small">This job comes from the local sandbox ATS, a test site on this computer. It is not a real employer. A browser runner fills the form, you review exactly what it filled, and only your confirmation sends it.</p>

      {submission === null && (
        approved
          ? <button type="button" disabled={busy} onClick={() => act("", json({ review_hash: reviewHash }), "Unable to start the submission")}>Fill the sandbox form for review</button>
          : <p className="muted small">Approve this exact package first.</p>
      )}

      {submission && WORKING.has(submission.state) && (
        <p className="status-row"><span className="status-dot working" />{submission.state === "queued" || submission.state === "filling" ? "Filling the form in an isolated browser… nothing is sent at this step." : "Submitting to the sandbox…"}</p>
      )}

      {submission?.state === "final_review" && (
        <>
          <div className="notice info"><strong>Review what will be sent.</strong> This is what the runner read back from the filled form. Nothing has been sent yet.</div>
          <table className="submission-fields">
            <thead><tr><th scope="col">Form field</th><th scope="col">Value</th></tr></thead>
            <tbody>
              {submission.fields.map((item) => (
                <tr key={item.question_key}><th scope="row">{item.label}</th><td>{item.sent ? item.display_value : <span className="muted">Left blank</span>}</td></tr>
              ))}
              <tr><th scope="row">CV (PDF)</th><td><code>{submission.resume_sha256?.slice(0, 16)}…</code> <span className="muted small">matches the approved package</span></td></tr>
            </tbody>
          </table>
          <p className="muted small">Destination: <code>{submission.destination_url}</code></p>
          <div className="button-row">
            <button type="button" disabled={busy} onClick={() => act("/confirm", json({ fill_hash: submission.fill_hash }), "Unable to confirm")}>Confirm and submit to sandbox</button>
            <button className="secondary" type="button" disabled={busy} onClick={() => act("/cancel", json({}), "Unable to cancel")}>Cancel</button>
          </div>
        </>
      )}

      {submission?.state === "submitted" && (
        <div className="notice info"><strong>Submitted to the sandbox.</strong> {submission.verification === "confirmation_page" ? <>Confirmation reference <code>{submission.confirmation_id}</code>.</> : "You confirmed the sandbox received it."} It is in your application tracker.</div>
      )}

      {submission?.state === "submitted_unverified" && (
        <div className="notice error">
          <strong>Sent, but not confirmed.</strong> The submit button was clicked, but no confirmation came back. It will not be retried automatically, because that could send it twice.
          <div className="button-row"><button type="button" disabled={busy} onClick={() => act("/resolve", json({ received: true }), "Unable to update")}>The sandbox received it</button><button className="secondary" type="button" disabled={busy} onClick={() => act("/resolve", json({ received: false }), "Unable to update")}>It was not received</button></div>
        </div>
      )}

      {submission?.state === "paused" && (
        <div className="notice error"><strong>Paused: nothing was sent.</strong> {PAUSE_TEXT[submission.pause_reason ?? ""] ?? "The runner stopped for your attention."} You can apply yourself instead.{submission.can_cancel && <div className="button-row"><button className="secondary" type="button" disabled={busy} onClick={() => act("/cancel", json({}), "Unable to cancel")}>Close this submission</button></div>}</div>
      )}

      {submission?.state === "blocked" && <div className="notice error"><strong>Stopped before sending.</strong> {BLOCK_TEXT[submission.error_code ?? ""] ?? submission.error_code}</div>}
      {submission?.state === "failed" && <div className="notice error"><strong>The runner failed before sending anything</strong> ({submission.error_code}).</div>}
      {submission?.state === "cancelled" && <p className="muted small">Cancelled. Nothing was sent. Rebuilding the package lets you start again.</p>}
      {submission?.state === "not_received" && <p className="muted small">Marked as not received. Rebuild the package to try again.</p>}
    </div>
  );
}
