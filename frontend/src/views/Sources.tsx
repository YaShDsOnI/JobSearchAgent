import { api, Connector } from "../api";
import { Empty, Panel, useLoad, when } from "../ui";

const KIND_LABELS: Record<string, string> = {
  fixture: "Bundled sample jobs",
  sandbox_ats_fixture: "Local sandbox ATS",
  ashby_job_board: "Ashby public job board",
  greenhouse_job_board: "Greenhouse public job board",
  lever_postings: "Lever public job site",
};

export function Sources({ setError }: { setError: (value: string) => void }) {
  const connectors = useLoad(() => api<Connector[]>("/api/v1/connectors"), setError);
  return (
    <Panel eyebrow="Where jobs come from" title="Approved sources" aside={<span className="count">{connectors.data?.length ?? 0}</span>}>
      <p className="muted">Jobs come only from sources with a recorded authorization: official public job-board APIs and local fixtures. Nothing is scraped, and no site that forbids automation is used. An operator approves and enables each source; live network access is off unless the deployment turns it on.</p>
      {connectors.loading ? <p className="muted">Loading…</p> : !connectors.data?.length ? <Empty title="No sources are registered" /> : (
        <div className="table-wrap">
          <table className="data-table">
            <thead><tr><th>Source</th><th>Type</th><th>Policy</th><th>Status</th><th>Last success</th></tr></thead>
            <tbody>
              {connectors.data.map((connector) => (
                <tr key={connector.id}>
                  <td data-label="Source"><strong>{connector.name}</strong><div className="muted small">{connector.scope === "private" ? "Private to you" : "Shared"} · tier {connector.tier}</div></td>
                  <td data-label="Type">{KIND_LABELS[connector.kind] ?? connector.kind}<div className="muted small">{connector.base_domain ?? "local"}</div></td>
                  <td data-label="Policy">{connector.policy_status}<div className="muted small">{connector.authorization_basis ?? "no basis recorded"} · terms {connector.terms_version}</div></td>
                  <td data-label="Status"><span className={`badge ${connector.enabled ? "badge-eligible" : "badge-unknown"}`}>{connector.enabled ? "Enabled" : "Disabled"}</span><div className="muted small">Health {connector.health_status}{connector.last_error_code ? ` · ${connector.last_error_code}` : ""}</div></td>
                  <td data-label="Last success">{when(connector.last_success_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}
