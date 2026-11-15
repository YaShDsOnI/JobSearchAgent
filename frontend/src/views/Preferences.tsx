import { FormEvent, useEffect, useState } from "react";
import { api, jsonBody, LOCATION_CATEGORIES, LOCATION_LABELS, LocationCategory, message, PreferenceRules, Preferences as PreferenceData } from "../api";
import { Panel, useLoad, when } from "../ui";

const DEFAULT_LADDER: LocationCategory[] = ["global_remote", "remote_from_home", "local_remote", "hybrid_home", "onsite_home"];

function list(value: string): string[] {
  return value.split(",").map((item) => item.trim()).filter(Boolean);
}

export function Preferences({ setError }: { setError: (value: string) => void }) {
  const current = useLoad(() => api<PreferenceData | null>("/api/v1/profile/preferences"), setError);
  const [roles, setRoles] = useState("");
  const [seniority, setSeniority] = useState("");
  const [homeCountry, setHomeCountry] = useState("IN");
  const [ladder, setLadder] = useState<LocationCategory[]>(DEFAULT_LADDER);
  const [cities, setCities] = useState<string[]>([]);
  const [newCity, setNewCity] = useState("");
  const [relocation, setRelocation] = useState<"" | "yes" | "no">("");
  const [minimum, setMinimum] = useState("");
  const [currency, setCurrency] = useState("INR");
  const [notice, setNotice] = useState("");
  const [saved, setSaved] = useState("");

  useEffect(() => {
    const rules = current.data?.rules;
    if (!rules) return;
    setRoles(rules.target_roles.join(", "));
    setSeniority(rules.seniority.join(", "));
    if (rules.location_policy) {
      setHomeCountry(rules.location_policy.home_country);
      setLadder(rules.location_policy.priorities);
      setCities(rules.location_policy.preferred_cities);
      setRelocation(rules.location_policy.open_to_relocation === null ? "" : rules.location_policy.open_to_relocation ? "yes" : "no");
    }
    setMinimum(rules.compensation.minimum !== undefined ? String(rules.compensation.minimum) : "");
    setCurrency(rules.compensation.currency ?? "INR");
    setNotice(rules.notice_period_days !== null ? String(rules.notice_period_days) : "");
  }, [current.data]);

  function move(index: number, offset: number) {
    const next = [...ladder];
    const [item] = next.splice(index, 1);
    next.splice(index + offset, 0, item);
    setLadder(next);
  }

  function moveCity(index: number, offset: number) {
    const next = [...cities];
    const [item] = next.splice(index, 1);
    next.splice(index + offset, 0, item);
    setCities(next);
  }

  async function save(event: FormEvent) {
    event.preventDefault();
    const rules: PreferenceRules = {
      target_roles: list(roles),
      seniority: list(seniority),
      employment_types: current.data?.rules.employment_types ?? [],
      compensation: minimum ? { minimum: Number(minimum), currency: currency.toUpperCase(), period: "year" } : {},
      work_modes: [],
      location_preferences: [],
      work_authorization: {},
      notice_period_days: notice ? Number(notice) : null,
      location_policy: {
        home_country: homeCountry.toUpperCase(),
        priorities: ladder,
        preferred_cities: cities,
        open_to_relocation: relocation === "" ? null : relocation === "yes",
      },
    };
    try {
      current.setData(await api<PreferenceData>("/api/v1/profile/preferences", jsonBody(rules, "PUT")));
      setSaved("Saved. Matches will be refreshed with your new preferences.");
      setError("");
    } catch (reason) {
      setError(message(reason, "Unable to save preferences"));
    }
  }

  const excluded = LOCATION_CATEGORIES.filter((item) => !ladder.includes(item));
  return (
    <form className="view-grid" onSubmit={save}>
      <Panel eyebrow="What you want" title="Roles and pay" aside={current.data && <span className="muted small">Version from {when(current.data.created_at)}</span>}>
        <div className="form-grid">
          <label>Target roles (comma-separated)<input value={roles} onChange={(event) => setRoles(event.target.value)} placeholder="Data Engineer, Analytics Engineer" /></label>
          <label>Seniority (comma-separated)<input value={seniority} onChange={(event) => setSeniority(event.target.value)} placeholder="senior, lead" /></label>
          <label>Minimum yearly pay<input type="number" min={0} value={minimum} onChange={(event) => setMinimum(event.target.value)} placeholder="e.g. 2400000" /></label>
          <label>Currency<input value={currency} maxLength={3} onChange={(event) => setCurrency(event.target.value)} /></label>
          <label>Notice period (days)<input type="number" min={0} value={notice} onChange={(event) => setNotice(event.target.value)} /></label>
        </div>
        <p className="muted small">Pay in another currency is never converted; it shows as not comparable.</p>
      </Panel>

      <Panel eyebrow="Where you want to work" title="Your location ladder">
        <p className="muted">Order the kinds of jobs you want, best first. Jobs in a higher step score higher on preference. A step you remove scores zero. This only ranks jobs: whether you may work somewhere still comes only from the work authorization you confirm in your profile.</p>
        <label className="narrow">Home country (ISO code)<input value={homeCountry} maxLength={2} onChange={(event) => setHomeCountry(event.target.value)} /></label>
        <ol className="ladder">
          {ladder.map((item, index) => (
            <li key={item}>
              <span className="ladder-step">{index + 1}</span>
              <span className="grow">{LOCATION_LABELS[item]}</span>
              <button className="secondary" type="button" aria-label={`Move ${LOCATION_LABELS[item]} up`} disabled={index === 0} onClick={() => move(index, -1)}>↑</button>
              <button className="secondary" type="button" aria-label={`Move ${LOCATION_LABELS[item]} down`} disabled={index === ladder.length - 1} onClick={() => move(index, 1)}>↓</button>
              <button className="secondary" type="button" onClick={() => setLadder(ladder.filter((entry) => entry !== item))}>Remove</button>
            </li>
          ))}
        </ol>
        {excluded.length > 0 && (
          <div className="button-row"><span className="muted small">Not wanted:</span>
            {excluded.map((item) => <button key={item} className="secondary" type="button" onClick={() => setLadder([...ladder, item])}>Add “{LOCATION_LABELS[item]}”</button>)}
          </div>
        )}
        <h3>Preferred cities for hybrid and on-site jobs</h3>
        <ol className="ladder">
          {cities.map((city, index) => (
            <li key={city}>
              <span className="ladder-step">{index + 1}</span><span className="grow">{city}</span>
              <button className="secondary" type="button" aria-label={`Move ${city} up`} disabled={index === 0} onClick={() => moveCity(index, -1)}>↑</button>
              <button className="secondary" type="button" aria-label={`Move ${city} down`} disabled={index === cities.length - 1} onClick={() => moveCity(index, 1)}>↓</button>
              <button className="secondary" type="button" onClick={() => setCities(cities.filter((entry) => entry !== city))}>Remove</button>
            </li>
          ))}
        </ol>
        <div className="inline-form">
          <label className="grow">Add a city<input value={newCity} onChange={(event) => setNewCity(event.target.value)} placeholder="e.g. Vadodara" /></label>
          <button className="secondary" type="button" disabled={!newCity.trim() || cities.includes(newCity.trim())} onClick={() => { setCities([...cities, newCity.trim()]); setNewCity(""); }}>Add city</button>
        </div>
        <label className="narrow">Open to relocating abroad?
          <select value={relocation} onChange={(event) => setRelocation(event.target.value as "" | "yes" | "no")}>
            <option value="">Not stated</option><option value="yes">Yes</option><option value="no">No</option>
          </select>
        </label>
      </Panel>
      <div className="button-row sticky-actions">
        <button type="submit">Save preferences</button>
        {saved && <span className="muted small" role="status">{saved}</span>}
      </div>
    </form>
  );
}
