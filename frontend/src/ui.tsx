import { ReactNode, useCallback, useEffect, useState } from "react";
import { MatchReason } from "./api";

export type Route = { view: string; id: string | null };

export function parseRoute(hash: string): Route {
  const [view, id] = hash.replace(/^#\/?/, "").split("/");
  return { view: view || "overview", id: id ? decodeURIComponent(id) : null };
}

export function go(view: string, id?: string) {
  window.location.hash = id ? `#/${view}/${encodeURIComponent(id)}` : `#/${view}`;
}

export function useRoute(): Route {
  const [route, setRoute] = useState(() => parseRoute(window.location.hash));
  useEffect(() => {
    const update = () => setRoute(parseRoute(window.location.hash));
    window.addEventListener("hashchange", update);
    return () => window.removeEventListener("hashchange", update);
  }, []);
  return route;
}

/** Load data, exposing a reload function. Errors go to the shared error banner. */
export function useLoad<T>(load: () => Promise<T>, setError: (value: string) => void, deps: unknown[] = []) {
  const [data, setData] = useState<T | undefined>(undefined);
  const [loading, setLoading] = useState(true);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const run = useCallback(load, deps);
  const reload = useCallback(async () => {
    setLoading(true);
    try {
      setData(await run());
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to load");
    } finally {
      setLoading(false);
    }
  }, [run, setError]);
  useEffect(() => { void reload(); }, [reload]);
  return { data, loading, reload, setData };
}

export function Panel({ eyebrow, title, aside, children, id }: { eyebrow?: string; title: string; aside?: ReactNode; children: ReactNode; id?: string }) {
  const headingId = id ?? `panel-${title.toLowerCase().replace(/[^a-z0-9]+/g, "-")}`;
  return (
    <section className="panel" aria-labelledby={headingId}>
      <div className="section-heading">
        <div>{eyebrow && <p className="eyebrow">{eyebrow}</p>}<h2 id={headingId}>{title}</h2></div>
        {aside}
      </div>
      {children}
    </section>
  );
}

export function Score({ label, value, hint }: { label: string; value: number | null; hint?: string }) {
  const known = value !== null && value !== undefined;
  return (
    <div className="score" title={hint}>
      <div className="score-label"><span>{label}</span><strong>{known ? Math.round(value) : "—"}</strong></div>
      <div className="score-track" aria-hidden="true"><div className="score-fill" style={{ width: `${known ? Math.max(0, Math.min(100, value)) : 0}%` }} /></div>
    </div>
  );
}

const ELIGIBILITY_LABELS: Record<string, string> = {
  eligible: "Eligible",
  needs_review: "Needs your review",
  unknown: "Unknown",
  ineligible: "Not eligible",
};

export function EligibilityBadge({ status }: { status: string | null | undefined }) {
  const value = status ?? "unmatched";
  return <span className={`badge badge-${value}`}>{ELIGIBILITY_LABELS[value] ?? "Not matched yet"}</span>;
}

export function Reasons({ reasons, limit }: { reasons: MatchReason[]; limit?: number }) {
  const shown = limit ? reasons.slice(0, limit) : reasons;
  if (shown.length === 0) return null;
  return <ul className="reasons">{shown.map((reason, index) => <li key={`${reason.code}-${index}`} className={`reason-${reason.source}`}>{reason.message}</li>)}</ul>;
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return <div className="empty"><h3>{title}</h3>{children && <div className="muted">{children}</div>}</div>;
}

export function when(value: string | null | undefined): string {
  return value ? new Date(value).toLocaleString() : "never";
}
