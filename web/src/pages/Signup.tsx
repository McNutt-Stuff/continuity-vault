import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { Icon } from "../components/Icon";

interface Plan { id: string; name: string; price_per_tb_month: number; min_tb: number; }
interface SignupConfig { enabled: boolean; accepted_countries: string[]; plans: Plan[]; regions: { code: string; name: string }[]; }

const COUNTRIES: Record<string, string> = { US: "United States", CA: "Canada" };
const US_STATES = ["AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA","KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ","NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT","VA","WA","WV","WI","WY","DC"];
const CA_PROVINCES = ["AB","BC","MB","NB","NL","NS","NT","NU","ON","PE","QC","SK","YT"];

const PLAN_BLURB: Record<string, string> = {
  personal: "For one person. Pay only for what you protect.",
  family: "Protect the whole family with shared management.",
  business: "For teams and businesses that need continuity.",
  enterprise: "Scale, controls, and priority support.",
};
const DEDICATED = new Set(["family", "business", "enterprise"]);

export default function Signup() {
  const nav = useNavigate();
  const [cfg, setCfg] = useState<SignupConfig | null>(null);
  const [step, setStep] = useState<0 | 1 | 2 | 3>(0);
  const [plan, setPlan] = useState("");
  const [form, setForm] = useState({
    first_name: "", last_name: "", email: "", org_name: "",
    country: "US", subdivision: "", city: "", licensed_tb: "",
  });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [done, setDone] = useState<{ dev_code?: string } | null>(null);

  useEffect(() => { api.get<SignupConfig>("/signup/config").then(setCfg).catch(() => setCfg(null)); }, []);

  const selectedPlan = useMemo(() => cfg?.plans.find((p) => p.id === plan), [cfg, plan]);
  const isDedicated = DEDICATED.has(plan);
  const subs = form.country === "CA" ? CA_PROVINCES : US_STATES;

  function up(k: keyof typeof form, v: string) { setForm((f) => ({ ...f, [k]: v })); }

  function validAccount(): string {
    if (!form.first_name.trim()) return "Please enter your first name.";
    if (!form.email.includes("@")) return "Please enter a valid email.";
    if (!COUNTRIES[form.country]) return "Sign-up is only available in the US and Canada right now.";
    if (!form.subdivision) return "Please choose your state or province.";
    if (isDedicated && !form.org_name.trim()) return "Please name your organization.";
    return "";
  }

  async function submit() {
    setErr(""); setBusy(true);
    try {
      const res = await api.post<{ ok: boolean; dev_code?: string }>("/signup", {
        email: form.email.trim().toLowerCase(),
        first_name: form.first_name.trim(),
        last_name: form.last_name.trim(),
        org_name: form.org_name.trim(),
        country: form.country,
        subdivision: form.subdivision,
        city: form.city.trim(),
        plan,
        licensed_tb: parseFloat(form.licensed_tb) || 0,
      });
      setDone({ dev_code: res.dev_code });
      setStep(3);
    } catch (e) {
      setErr((e as { message?: string })?.message || "Sign-up failed. Please try again.");
    } finally { setBusy(false); }
  }

  if (cfg && !cfg.enabled) {
    return (
      <div className="auth-wrap"><div className="auth-card card">
        <div className="auth-logo"><img src="/logos/Logo-Full.png" alt="Arkive" /></div>
        <div className="auth-sub">Sign-up isn't open right now</div>
        <div className="faint" style={{ fontSize: 13, textAlign: "center" }}>Please check back soon.</div>
        <button className="btn ghost" style={{ width: "100%", marginTop: 16 }} onClick={() => nav("/")}>Back to sign in</button>
      </div></div>
    );
  }

  return (
    <div className="auth-wrap">
      <div className="auth-card card" style={{ maxWidth: 520 }}>
        <div className="auth-logo"><img src="/logos/Logo-Full.png" alt="Arkive" /></div>

        {step < 3 && (
          <div className="row" style={{ gap: 6, justifyContent: "center", marginBottom: 16 }}>
            {[0, 1, 2].map((s) => (
              <span key={s} style={{ width: 26, height: 4, borderRadius: 2, background: s <= step ? "var(--accent, #4f7cff)" : "var(--border-soft)" }} />
            ))}
          </div>
        )}

        {step === 0 && (
          <>
            <div className="auth-sub">Choose your plan</div>
            <div className="stack" style={{ gap: 10 }}>
              {(cfg?.plans || []).map((p) => (
                <button key={p.id} className="card" onClick={() => { setPlan(p.id); setStep(1); }}
                        style={{ textAlign: "left", cursor: "pointer", borderColor: plan === p.id ? "var(--accent, #4f7cff)" : undefined }}>
                  <div className="spread">
                    <div style={{ fontWeight: 700 }}>{p.name}</div>
                    <div className="faint" style={{ fontSize: 13 }}>${p.price_per_tb_month}/TB·mo{p.min_tb ? ` · ${p.min_tb} TB min` : ""}</div>
                  </div>
                  <div className="faint" style={{ fontSize: 12.5, marginTop: 4 }}>{PLAN_BLURB[p.id] || ""}</div>
                </button>
              ))}
              {!cfg && <div className="muted">Loading plans…</div>}
            </div>
            <button className="btn ghost sm" style={{ marginTop: 14 }} onClick={() => nav("/")}>Already have an account? Sign in</button>
          </>
        )}

        {step === 1 && (
          <>
            <div className="auth-sub">Your details</div>
            <div className="row" style={{ gap: 10 }}>
              <div className="field flex1"><label>First name</label>
                <input className="input" autoFocus value={form.first_name} onChange={(e) => up("first_name", e.target.value)} /></div>
              <div className="field flex1"><label>Last name</label>
                <input className="input" value={form.last_name} onChange={(e) => up("last_name", e.target.value)} /></div>
            </div>
            <div className="field"><label>Email</label>
              <input className="input" value={form.email} onChange={(e) => up("email", e.target.value)} placeholder="you@email.com" /></div>
            {isDedicated && (
              <div className="field"><label>Organization name</label>
                <input className="input" value={form.org_name} onChange={(e) => up("org_name", e.target.value)} placeholder="e.g. Smith Family, Acme Inc." /></div>
            )}
            <div className="row" style={{ gap: 10 }}>
              <div className="field flex1"><label>Country</label>
                <select className="input" value={form.country} onChange={(e) => { up("country", e.target.value); up("subdivision", ""); }}>
                  {Object.entries(COUNTRIES).map(([c, n]) => <option key={c} value={c}>{n}</option>)}
                </select></div>
              <div className="field flex1"><label>{form.country === "CA" ? "Province" : "State"}</label>
                <select className="input" value={form.subdivision} onChange={(e) => up("subdivision", e.target.value)}>
                  <option value="">—</option>
                  {subs.map((s) => <option key={s} value={s}>{s}</option>)}
                </select></div>
            </div>
            <div className="field"><label>City <span className="faint">(optional)</span></label>
              <input className="input" value={form.city} onChange={(e) => up("city", e.target.value)} /></div>
            {err && <div className="pill danger" style={{ marginBottom: 10 }}>{err}</div>}
            <div className="row" style={{ gap: 8 }}>
              <button className="btn ghost" onClick={() => setStep(0)}>Back</button>
              <button className="btn primary flex1" onClick={() => { const e = validAccount(); if (e) { setErr(e); return; } setErr(""); setStep(2); }}>Continue</button>
            </div>
          </>
        )}

        {step === 2 && (
          <>
            <div className="auth-sub">Review &amp; create</div>
            <div className="card" style={{ background: "var(--inset)" }}>
              <Row2 k="Plan" v={selectedPlan?.name || plan} />
              <Row2 k="Price" v={selectedPlan ? `$${selectedPlan.price_per_tb_month}/TB · month${selectedPlan.min_tb ? ` (min ${selectedPlan.min_tb} TB)` : ""}` : "—"} />
              <Row2 k="Name" v={`${form.first_name} ${form.last_name}`.trim()} />
              <Row2 k="Email" v={form.email} />
              {isDedicated && <Row2 k="Organization" v={form.org_name} />}
              <Row2 k="Location" v={`${form.city ? form.city + ", " : ""}${form.subdivision}, ${form.country}`} />
            </div>
            <div className="faint" style={{ fontSize: 12, margin: "12px 0" }}>
              <Icon name="info" size={12} /> We'll email you a one-time sign-in code. You'll add a payment method and
              finish setup after you sign in. By continuing you agree to our terms and privacy policy.
            </div>
            {err && <div className="pill danger" style={{ marginBottom: 10 }}>{err}</div>}
            <div className="row" style={{ gap: 8 }}>
              <button className="btn ghost" onClick={() => setStep(1)} disabled={busy}>Back</button>
              <button className="btn primary flex1" onClick={submit} disabled={busy}>{busy ? "Creating your account…" : "Create my account"}</button>
            </div>
          </>
        )}

        {step === 3 && done && (
          <>
            <div style={{ textAlign: "center", padding: "10px 0 4px" }}>
              <div style={{ display: "inline-flex", padding: 12, borderRadius: "50%", background: "var(--inset)", marginBottom: 10 }}>
                <Icon name="mail" size={22} />
              </div>
              <div className="auth-sub">Check your email</div>
              <div className="faint" style={{ fontSize: 13 }}>
                We sent a one-time sign-in code to <b>{form.email}</b>. Enter it on the next screen to finish setting up your account.
              </div>
              {done.dev_code && <div className="pill info" style={{ marginTop: 12 }}>Dev code: <span className="mono" style={{ marginLeft: 6 }}>{done.dev_code}</span></div>}
            </div>
            <button className="btn primary" style={{ width: "100%", marginTop: 16 }} onClick={() => nav("/")}>Continue to sign in</button>
          </>
        )}
      </div>
    </div>
  );
}

function Row2({ k, v }: { k: string; v: string }) {
  return (
    <div className="spread" style={{ padding: "4px 0", fontSize: 13 }}>
      <span className="faint">{k}</span>
      <span style={{ fontWeight: 600, textAlign: "right" }}>{v}</span>
    </div>
  );
}
