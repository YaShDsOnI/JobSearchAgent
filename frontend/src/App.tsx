import { FormEvent, useEffect, useState } from "react";
import { api, currentUser, message, User } from "./api";
import { TrackerPanel } from "./Tracker";
import { go, useRoute } from "./ui";
import { Applications } from "./views/Applications";
import { Cvs } from "./views/Cvs";
import { Jobs } from "./views/Jobs";
import { Overview } from "./views/Overview";
import { Preferences } from "./views/Preferences";
import { Profile } from "./views/Profile";
import { Sources } from "./views/Sources";
import { Metrics, SupportCases } from "./views/Support";

type AuthMode = "login" | "register";

const NAV: Array<{ view: string; label: string; hint: string }> = [
  { view: "overview", label: "Overview", hint: "Agent status and next steps" },
  { view: "jobs", label: "Jobs", hint: "Ranked jobs and eligibility" },
  { view: "profile", label: "Profile", hint: "Your CV and confirmed facts" },
  { view: "preferences", label: "Preferences", hint: "Roles, pay, location ladder" },
  { view: "cvs", label: "CVs", hint: "Tailored, factual CV versions" },
  { view: "applications", label: "Applications", hint: "Review packages" },
  { view: "tracker", label: "Tracker", hint: "Where you applied" },
  { view: "sources", label: "Sources", hint: "Approved job sources" },
];

export default function App() {
  const [user, setUser] = useState<User | null | undefined>(undefined);
  const [error, setError] = useState("");

  useEffect(() => {
    currentUser().then(setUser).catch((reason: Error) => {
      setError(reason.message);
      setUser(null);
    });
  }, []);

  if (user === undefined) return <main className="centered">Loading your workspace…</main>;
  if (user === null) return <AuthPanel onAuthenticated={setUser} error={error} setError={setError} />;
  return <Workspace user={user} onSignedOut={() => setUser(null)} error={error} setError={setError} />;
}

function Workspace({ user, onSignedOut, error, setError }: { user: User; onSignedOut: () => void; error: string; setError: (value: string) => void }) {
  const route = useRoute();
  useEffect(() => setError(""), [route.view, route.id, setError]);

  async function signOut() {
    try {
      await api<void>("/api/v1/auth/logout", { method: "POST" });
      onSignedOut();
    } catch (reason) {
      setError(message(reason, "Unable to sign out"));
    }
  }

  const nav = [
    ...NAV,
    ...(user.role === "support" ? [{ view: "support", label: "Support cases", hint: "Cases candidates shared with you" }] : []),
    ...(user.role === "operator" ? [{ view: "metrics", label: "Pilot metrics", hint: "Aggregate, small counts hidden" }] : []),
  ];
  const active = nav.find((item) => item.view === route.view) ?? nav[0];
  return (
    <div className="workspace">
      <a className="skip-link" href="#main">Skip to content</a>
      <aside className="sidebar">
        <div className="brand"><p className="eyebrow">JobFinderAgent</p><strong>Your job search, under your control</strong></div>
        <nav aria-label="Workspace">
          <ul>
            {nav.map((item) => (
              <li key={item.view}>
                <a href={`#/${item.view}`} aria-current={item.view === active.view ? "page" : undefined} className={item.view === active.view ? "active" : ""}>
                  <span>{item.label}</span><small>{item.hint}</small>
                </a>
              </li>
            ))}
          </ul>
        </nav>
        <div className="account"><span>{user.email}</span><button className="secondary" type="button" onClick={signOut}>Sign out</button></div>
      </aside>
      <main id="main" className="content" tabIndex={-1}>
        <header className="content-header"><h1>{active.label}</h1><p className="muted">{active.hint}</p></header>
        {error && <div className="notice error" role="alert">{error}<button className="text-button" type="button" onClick={() => setError("")}>Dismiss</button></div>}
        <View view={active.view} id={route.id} setError={setError} />
      </main>
    </div>
  );
}

function View({ view, id, setError }: { view: string; id: string | null; setError: (value: string) => void }) {
  switch (view) {
    case "jobs": return <Jobs id={id} setError={setError} />;
    case "profile": return <Profile setError={setError} />;
    case "preferences": return <Preferences setError={setError} />;
    case "cvs": return <Cvs id={id} setError={setError} />;
    case "applications": return <Applications id={id} setError={setError} />;
    case "tracker": return <TrackerPanel refreshKey={0} setError={setError} />;
    case "sources": return <Sources setError={setError} />;
    case "support": return <SupportCases id={id} setError={setError} />;
    case "metrics": return <Metrics setError={setError} />;
    default: return <Overview setError={setError} />;
  }
}

function AuthPanel({ onAuthenticated, error, setError }: { onAuthenticated: (user: User) => void; error: string; setError: (value: string) => void }) {
  const [mode, setMode] = useState<AuthMode>("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError("");
    try {
      const user = await api<User>(`/api/v1/auth/${mode}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password }),
      });
      go("overview");
      onAuthenticated(user);
    } catch (reason) {
      setError(message(reason, "Authentication failed"));
    }
  }

  return (
    <main className="auth-shell">
      <section className="auth-card" aria-labelledby="auth-title">
        <p className="eyebrow">JobFinderAgent</p>
        <h1 id="auth-title">Your job search, under your control.</h1>
        <p className="muted">The agent finds jobs from approved sources, explains eligibility, skill fit, and preference fit separately, and drafts CVs only from facts you confirm. You approve every application, and nothing is sent to an employer for you.</p>
        <form onSubmit={submit} className="stack">
          <label>Email<input type="email" required autoComplete="email" value={email} onChange={(event) => setEmail(event.target.value)} /></label>
          <label>Password<input type="password" required minLength={12} autoComplete={mode === "login" ? "current-password" : "new-password"} value={password} onChange={(event) => setPassword(event.target.value)} /></label>
          {error && <p className="error" role="alert">{error}</p>}
          <button type="submit">{mode === "login" ? "Sign in" : "Create account"}</button>
        </form>
        <button className="text-button" type="button" onClick={() => { setMode(mode === "login" ? "register" : "login"); setError(""); }}>
          {mode === "login" ? "Create a new account" : "I already have an account"}
        </button>
      </section>
    </main>
  );
}
