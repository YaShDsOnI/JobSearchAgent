import { ChangeEvent, FormEvent, useEffect, useState } from "react";
import { api, Artifact, FactSuggestion, jsonBody, message, Profile as ProfileData, Suggestions } from "../api";
import { Empty, Panel, useLoad, when } from "../ui";

type FieldKind = "text" | "number" | "countries" | "select" | "bool";
type Field = { key: string; label: string; kind: FieldKind; options?: Array<[string, string]>; placeholder?: string };
type Schema = { label: string; hint: string; fields: Field[] };

const FACT_SCHEMAS: Record<string, Schema> = {
  skill: { label: "Skill", hint: "A skill you can evidence.", fields: [{ key: "name", label: "Skill", kind: "text", placeholder: "e.g. Python" }] },
  experience: { label: "Experience with a skill", hint: "Years of hands-on experience; used for “N+ years” requirements.", fields: [
    { key: "skill", label: "Skill", kind: "text", placeholder: "e.g. SQL" }, { key: "years", label: "Years", kind: "number" }] },
  work_authorization: { label: "Work authorization", hint: "Only you can state this. It is never inferred from your CV, location, or nationality.", fields: [
    { key: "countries", label: "Countries (ISO codes, comma-separated)", kind: "countries", placeholder: "IN, AE" },
    { key: "status", label: "Authorization status", kind: "select", options: [["authorized", "Authorized to work"], ["citizen", "Citizen"], ["permanent_resident", "Permanent resident"], ["work_permit", "Work permit"], ["pending", "Pending"], ["not_authorized", "Not authorized"]] },
    { key: "sponsorship_needed", label: "Needs sponsorship", kind: "bool" }] },
  contact: { label: "Contact details", hint: "Used only to pre-fill application answers you review.", fields: [
    { key: "full_name", label: "Full name", kind: "text" }, { key: "email", label: "Email", kind: "text" },
    { key: "phone", label: "Phone", kind: "text" }, { key: "location", label: "Current location", kind: "text" }] },
  education: { label: "Education", hint: "Shown on CVs and checked against stated education requirements.", fields: [
    { key: "text", label: "Degree and institution", kind: "text", placeholder: "B.Tech Computer Science, Example Institute" },
    { key: "level", label: "Level", kind: "select", options: [["bachelor", "Bachelor"], ["master", "Master"], ["doctorate", "Doctorate"], ["diploma", "Diploma"]] }] },
  certification: { label: "Certification", hint: "", fields: [{ key: "name", label: "Certification", kind: "text" }] },
  project: { label: "Project", hint: "", fields: [{ key: "name", label: "Project", kind: "text" }, { key: "description", label: "What you did", kind: "text" }] },
  availability: { label: "Notice period", hint: "", fields: [{ key: "notice_period_days", label: "Notice period (days)", kind: "number" }] },
  relocation: { label: "Relocation", hint: "", fields: [{ key: "willing", label: "Willing to relocate", kind: "bool" }] },
  summary: { label: "Summary", hint: "A sentence about you, in your words.", fields: [{ key: "text", label: "Summary", kind: "text" }] },
  link: { label: "Link", hint: "", fields: [{ key: "url", label: "URL", kind: "text" }, { key: "kind", label: "Kind", kind: "text", placeholder: "linkedin, github, web" }] },
};

type Draft = {
  fact_type: string;
  value: Record<string, unknown>;
  provenance: "candidate" | "uploaded_cv" | "approved_edit";
  confirmation_status: "confirmed" | "unknown" | "withdrawn";
  source_ref: string | null;
  evidence_text: string | null;
};

function toDrafts(profile: ProfileData | null | undefined): Draft[] {
  return (profile?.facts ?? []).map((fact) => ({
    fact_type: fact.fact_type,
    value: fact.value,
    provenance: fact.provenance as Draft["provenance"],
    confirmation_status: fact.confirmation_status,
    source_ref: fact.source_ref,
    evidence_text: fact.evidence_text,
  }));
}

function describe(fact: { fact_type: string; value: Record<string, unknown> }): string {
  return Object.entries(fact.value).map(([key, value]) => `${key}: ${Array.isArray(value) ? value.join(", ") : String(value)}`).join(" · ");
}

export function Profile({ setError }: { setError: (value: string) => void }) {
  const profile = useLoad(() => api<ProfileData | null>("/api/v1/profile"), setError);
  const documents = useLoad(() => api<Artifact[]>("/api/v1/profile/source-documents"), setError);
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [summary, setSummary] = useState("");
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState("");
  const [suggestions, setSuggestions] = useState<Suggestions | null>(null);
  const [newType, setNewType] = useState("skill");
  const [deleteText, setDeleteText] = useState("");

  useEffect(() => {
    setDrafts(toDrafts(profile.data));
    setSummary(profile.data?.summary ?? "");
  }, [profile.data]);

  const latestDocument = documents.data?.[0] ?? null;

  async function upload(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (!file) return;
    try {
      const body = new FormData();
      body.append("file", file);
      const artifact = await api<Artifact>("/api/v1/profile/source-document", { method: "POST", body });
      await documents.reload();
      await loadSuggestions(artifact.id);
      setError("");
    } catch (reason) {
      setError(message(reason, "Unable to upload the document"));
    } finally {
      event.target.value = "";
    }
  }

  async function loadSuggestions(artifactId: string) {
    try {
      setSuggestions(await api<Suggestions>(`/api/v1/profile/source-documents/${artifactId}/suggestions`));
      setError("");
    } catch (reason) {
      setError(message(reason, "Unable to read the document"));
    }
  }

  function addSuggestion(item: FactSuggestion, confirmed: boolean) {
    setDrafts((current) => [...current, {
      fact_type: item.fact_type, value: item.value, provenance: "uploaded_cv",
      confirmation_status: confirmed ? "confirmed" : "unknown",
      source_ref: suggestions?.source_document_id ?? null, evidence_text: item.evidence_text,
    }]);
    setSuggestions((current) => current && { ...current, suggestions: current.suggestions.map((entry) => entry === item ? { ...entry, already_in_profile: true } : entry) });
  }

  function update(index: number, change: Partial<Draft>) {
    setDrafts((current) => current.map((draft, position) => position === index ? { ...draft, ...change } : draft));
  }

  function setField(index: number, field: Field, raw: string) {
    const draft = drafts[index];
    const value: Record<string, unknown> = { ...draft.value };
    if (raw === "") delete value[field.key];
    else if (field.kind === "number") value[field.key] = Number(raw);
    else if (field.kind === "countries") value[field.key] = raw.split(",").map((item) => item.trim().toUpperCase()).filter(Boolean);
    else if (field.kind === "bool") value[field.key] = raw === "yes";
    else value[field.key] = raw;
    update(index, { value, provenance: draft.provenance === "uploaded_cv" ? "approved_edit" : draft.provenance });
  }

  async function save(event: FormEvent) {
    event.preventDefault();
    setSaving(true);
    try {
      await api("/api/v1/profile/versions", jsonBody({
        summary: summary.trim() || null,
        facts: drafts.filter((draft) => Object.keys(draft.value).length > 0),
        source_document_id: profile.data?.source_document_id ?? latestDocument?.id ?? null,
      }));
      await profile.reload();
      setSaved(`Saved a new profile version at ${new Date().toLocaleTimeString()}.`);
      setError("");
    } catch (reason) {
      setError(message(reason, "Unable to save the profile"));
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
      setError(message(reason, "Unable to export"));
    }
  }

  async function deleteProfile() {
    try {
      await api("/api/v1/profile", { method: "DELETE" });
      setDeleteText("");
      setSuggestions(null);
      await Promise.all([profile.reload(), documents.reload()]);
      setSaved("Your profile data and uploaded documents were deleted.");
      setError("");
    } catch (reason) {
      setError(message(reason, "Unable to delete"));
    }
  }

  const confirmedCount = drafts.filter((draft) => draft.confirmation_status === "confirmed").length;
  return (
    <div className="view-grid">
      <Panel eyebrow="Your CV" title="Start from your CV" aside={latestDocument && <span className="muted small">Latest upload {when(latestDocument.created_at)}</span>}>
        <p className="muted">Upload your CV (PDF, DOCX, or text). It is stored encrypted. The agent proposes facts from it; nothing counts until you confirm it. Work authorization and sensitive details are never read from your CV.</p>
        <div className="button-row">
          <label className="file-button">Upload a CV<input type="file" accept="application/pdf,text/plain,.docx" onChange={upload} /></label>
          {latestDocument && <button className="secondary" type="button" onClick={() => loadSuggestions(latestDocument.id)}>Suggest facts from my latest CV</button>}
        </div>
        {suggestions && (
          <div className="suggestions">
            <h3>Suggested facts <span className="muted small">({suggestions.suggestions.length})</span></h3>
            {suggestions.warnings.length > 0 && <p className="muted small">Notes: {suggestions.warnings.join(", ")}</p>}
            {suggestions.suggestions.length === 0 ? <p className="muted">No facts could be found in this document.</p> : (
              <ul className="suggestion-list">
                {suggestions.suggestions.map((item, index) => (
                  <li key={`${item.fact_type}-${index}`} className={item.already_in_profile ? "done" : ""}>
                    <div><strong>{FACT_SCHEMAS[item.fact_type]?.label ?? item.fact_type}:</strong> {describe(item)}
                      <div className="muted small">From your CV: “{item.evidence_text}”</div></div>
                    {item.already_in_profile ? <span className="tag">In profile</span> : (
                      <div className="button-row">
                        <button type="button" onClick={() => addSuggestion(item, true)}>This is true</button>
                        <button className="secondary" type="button" onClick={() => addSuggestion(item, false)}>Add as unknown</button>
                      </div>
                    )}
                  </li>
                ))}
              </ul>
            )}
            <p className="muted small">Added facts appear below. Save to create a new profile version.</p>
          </div>
        )}
      </Panel>

      <Panel eyebrow="Profile facts" title="Your facts" aside={<span className="count">{confirmedCount} confirmed</span>}>
        <form className="stack" onSubmit={save}>
          <label>Summary<textarea rows={2} value={summary} onChange={(event) => setSummary(event.target.value)} placeholder="Optional summary in your own words" /></label>
          {drafts.length === 0 && <Empty title="No facts yet">Upload your CV above or add facts one by one.</Empty>}
          {drafts.map((draft, index) => {
            const schema = FACT_SCHEMAS[draft.fact_type];
            return (
              <fieldset key={index} className={`fact-card status-${draft.confirmation_status}`}>
                <legend>{schema?.label ?? draft.fact_type}</legend>
                {schema?.hint && <p className="muted small">{schema.hint}</p>}
                <div className="fact-fields">
                  {schema ? schema.fields.map((field) => <FieldInput key={field.key} field={field} value={draft.value[field.key]} onChange={(raw) => setField(index, field, raw)} />)
                    : <p className="muted small">{describe(draft)}</p>}
                </div>
                {draft.evidence_text && <p className="muted small">Evidence: “{draft.evidence_text}”</p>}
                <div className="fact-actions">
                  <label className="inline">Confirmation
                    <select value={draft.confirmation_status} onChange={(event) => update(index, { confirmation_status: event.target.value as Draft["confirmation_status"] })}>
                      <option value="confirmed">Confirmed</option><option value="unknown">Unknown</option><option value="withdrawn">Withdrawn</option>
                    </select>
                  </label>
                  <button className="secondary" type="button" onClick={() => setDrafts(drafts.filter((_, position) => position !== index))}>Remove</button>
                </div>
              </fieldset>
            );
          })}
          <div className="button-row">
            <select aria-label="Fact type to add" value={newType} onChange={(event) => setNewType(event.target.value)}>
              {Object.entries(FACT_SCHEMAS).map(([key, schema]) => <option key={key} value={key}>{schema.label}</option>)}
            </select>
            <button className="secondary" type="button" onClick={() => setDrafts([...drafts, { fact_type: newType, value: {}, provenance: "candidate", confirmation_status: "confirmed", source_ref: null, evidence_text: null }])}>Add fact</button>
            <button type="submit" disabled={saving}>{saving ? "Saving…" : "Save new version"}</button>
          </div>
          {saved && <p className="muted small" role="status">{saved}</p>}
          <p className="muted small">Only confirmed facts are used for matching, CVs, and answers. Unknown stays unknown. Saving creates a new version; earlier matches and CVs keep the version they used.</p>
        </form>
      </Panel>

      <Panel eyebrow="Privacy" title="Your data">
        <p className="muted">Export everything the app holds about you, or delete your profile, CVs, applications, and tracker entries. Deleted data is also removed from any later restore of an older backup.</p>
        <div className="button-row">
          <button className="secondary" type="button" onClick={exportProfile}>Export my data</button>
          <label className="inline">Type DELETE to confirm<input value={deleteText} onChange={(event) => setDeleteText(event.target.value)} /></label>
          <button className="danger" type="button" disabled={deleteText !== "DELETE"} onClick={deleteProfile}>Delete my profile data</button>
        </div>
      </Panel>
    </div>
  );
}

function FieldInput({ field, value, onChange }: { field: Field; value: unknown; onChange: (raw: string) => void }) {
  const text = Array.isArray(value) ? value.join(", ") : value === undefined || value === null ? "" : field.kind === "bool" ? (value ? "yes" : "no") : String(value);
  if (field.kind === "select" || field.kind === "bool") {
    const options = field.kind === "bool" ? [["yes", "Yes"], ["no", "No"]] : field.options ?? [];
    return <label>{field.label}<select value={text} onChange={(event) => onChange(event.target.value)}><option value="">Not stated</option>{options.map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>;
  }
  return <label>{field.label}<input type={field.kind === "number" ? "number" : "text"} min={field.kind === "number" ? 0 : undefined} value={text} placeholder={field.placeholder} onChange={(event) => onChange(event.target.value)} /></label>;
}
