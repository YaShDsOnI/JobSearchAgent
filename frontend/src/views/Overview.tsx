import { useState } from "react";
import { AgentStatus, api, message, Recommendation, RefreshResult } from "../api";
import { EligibilityBadge, Empty, go, Panel, Score, useLoad, when } from "../ui";

const TARGET_VIEWS: Record<string, string> = {
  profile: "profile",
  preferences: "preferences",
  jobs: "jobs",
  cvs: "cvs",
  overview: "overview",
};

export function Overview({ setError }: { setError: (value: string) => void }) {
  const status = useLoad(() => api<AgentStatus>("/api/v1/agent/status"), setError);
  const top = useLoad(() => api<Recommendation[]>("/api/v1/recommendations?bucket=ready"), setError);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");

  async function toggle() {
    if (!status.data) return;
    setBusy(true);
    try {
      status.setData(await api<AgentStatus>(`/api/v1/agent/${status.data.auto_matching ? "disable" : "enable"}`, { method: "POST" }));
      setError("");
    } catch (reason) {
      setError(message(reason, "Unable to change the agent"));
    } finally {
      setBusy(false);
    }
  }

  async function refresh() {
    setBusy(true);
    try {
      const result = await api<RefreshResult>("/api/v1/agent/refresh", { method: "POST" });
      await Promise.all([status.reload(), top.reload()]);
      setNote(`Matched ${result.matched} job${result.matched === 1 ? "" : "s"}; ${result.already_current} already current${result.carried_reviews ? `; ${result.carried_reviews} review decision(s) kept` : ""}${result.truncated ? "; more remain, refresh again" : ""}.`);
      setError("");
    } catch (reason) {
      setError(message(reason, "Unable to refresh matches"));
    } finally {
      setBusy(false);
    }
  }

  const data = status.data;
  return (
    <div className="view-grid">
      <section className="panel hero" aria-labelledby="agent-title">
        <p className="eyebrow">Your job-search agent</p>
        <h2 id="agent-title">{data?.auto_matching ? "Watching for new jobs" : "Agent is off"}</h2>
        <p className="muted">
          When it is on, the agent re-matches every job from approved sources whenever new jobs arrive or you change your profile or preferences.
          It explains eligibility, skill fit, and preference fit separately. It never drafts, approves, or sends anything for you.
        </p>
        <div className="button-row">
          <button type="button" onClick={toggle} disabled={busy || !data} aria-pressed={data?.auto_matching ?? false}>
            {data?.auto_matching ? "Turn the agent off" : "Turn the agent on"}
          </button>
          <button className="secondary" type="button" onClick={refresh} disabled={busy || !data?.profile_ready}>Match now</button>
        </div>
        {note && <p className="muted small" role="status">{note}</p>}
        {data && (
          <p className="muted small">
            Last match refresh: {when(data.last_refresh_at)} · last new jobs: {when(data.last_ingestion_at)}
            {data.queued_refreshes > 0 ? ` · ${data.queued_refreshes} refresh queued` : ""} · policy {data.policy_version}
          </p>
        )}
      </section>

      {data && (
        <section className="stat-row" aria-label="Job counts">
          <Stat label="Ready for you" value={data.counts.ready} onClick={() => go("jobs")} />
          <Stat label="Need your review" value={data.counts.review} onClick={() => go("jobs")} />
          <Stat label="Not matched yet" value={data.counts.unmatched} />
          <Stat label="Out of date" value={data.counts.stale} />
          <Stat label="Applied" value={data.counts.applied} onClick={() => go("tracker")} />
        </section>
      )}

      <Panel eyebrow="Next steps" title="What to do next">
        {!data ? <p className="muted">Loading…</p> : data.next_actions.length === 0 ? (
          <p className="muted">Nothing needs your attention right now.</p>
        ) : (
          <ol className="action-list">
            {data.next_actions.map((action) => (
              <li key={action.code}>
                <span>{action.message}{action.count !== null ? <strong className="count inline">{action.count}</strong> : null}</span>
                {action.target !== "overview" && <button className="secondary" type="button" onClick={() => go(TARGET_VIEWS[action.target] ?? "overview")}>Open</button>}
              </li>
            ))}
          </ol>
        )}
      </Panel>

      <Panel eyebrow="Top matches" title="Best eligible jobs" aside={<button className="secondary" type="button" onClick={() => go("jobs")}>All jobs</button>}>
        {top.loading ? <p className="muted">Loading…</p> : !top.data || top.data.length === 0 ? (
          <Empty title="No eligible jobs yet">Add confirmed facts and your location ladder, then match. Jobs whose eligibility is unclear appear under Jobs as “Needs your review”.</Empty>
        ) : (
          <div className="job-list">
            {top.data.slice(0, 5).map((item) => (
              <button key={item.job.id} type="button" className="job-row" onClick={() => go("jobs", item.job.id)}>
                <span className="job-row-main"><strong>{item.job.title}</strong><span className="muted small">{item.job.employer} · {item.job.location_text ?? "Location unknown"} · {item.job.work_mode}</span></span>
                <span className="job-row-scores">
                  <EligibilityBadge status={item.match?.eligibility_status} />
                  <Score label="Fit" value={item.match?.fit_score ?? null} />
                  <Score label="Preference" value={item.match?.preference_score ?? null} />
                </span>
              </button>
            ))}
          </div>
        )}
        {data && <p className="muted small">{data.rank_formula}.</p>}
      </Panel>
    </div>
  );
}

function Stat({ label, value, onClick }: { label: string; value: number; onClick?: () => void }) {
  const body = <><strong>{value}</strong><span>{label}</span></>;
  return onClick ? <button type="button" className="stat stat-button" onClick={onClick}>{body}</button> : <div className="stat">{body}</div>;
}
