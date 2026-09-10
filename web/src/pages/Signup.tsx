import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { Icon, IconName } from "../components/Icon";

interface Plan { id: string; name: string; price_per_tb_month: number; min_tb: number; }
interface ApplianceTier { capacity_tb: number; monthly: number; setup: number; model: string; }
interface StorageTier { id: string; title: string; tagline: string; icon: string; color: string; benefits: string[]; }
interface Pricing {
  currency: string; protection_price_per_tb_month: number; cloud_price_per_tb_month: number;
  appliance_tiers: ApplianceTier[]; tiers: StorageTier[];
}
interface Config { enabled: boolean; trial_days: number; accepted_countries: string[]; plans: Plan[]; pricing: Pricing; }
interface PayConfig { configured: boolean; processor?: string | null; publishable_key?: string; currency?: string; }

let _stripeScript: Promise<void> | null = null;
function loadStripe(pk: string): Promise<any> {
  const w = window as any;
  if (!_stripeScript) {
    _stripeScript = new Promise<void>((resolve, reject) => {
      if (w.Stripe) { resolve(); return; }
      const s = document.createElement("script");
      s.src = "https://js.stripe.com/v3/"; s.async = true;
      s.onload = () => resolve();
      s.onerror = () => reject(new Error("Could not load Stripe.js"));
      document.head.appendChild(s);
    });
  }
  return _stripeScript.then(() => w.Stripe(pk));
}

const COUNTRIES: Record<string, string> = { US: "United States", CA: "Canada" };
const US_STATES = ["AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA","KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ","NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT","VA","WA","WV","WI","WY","DC"];
const CA_PROVINCES = ["AB","BC","MB","NB","NL","NS","NT","NU","ON","PE","QC","SK","YT"];
const PLAN_BLURB: Record<string, string> = {
  personal: "For one person. Pay only for what you protect.",
  family: "Protect the whole family with shared management.",
  business: "For teams and businesses that need continuity.",
  enterprise: "Scale, controls, and priority support.",
};
const RECOMMENDED = "business";
const DEDICATED = new Set(["family", "business", "enterprise"]);
const STEPS = ["Plan", "Account", "Address", "Protection", "Billing"];
const iconOf = (n: string): IconName => (["cloud","server","key","shield","check","database","file"].includes(n) ? n : "database") as IconName;
function money(n: number) { return "$" + Math.round(n).toLocaleString(); }

export default function Signup() {
  const nav = useNavigate();
  const [cfg, setCfg] = useState<Config | null>(null);
  const [pay, setPay] = useState<PayConfig | null>(null);
  const [step, setStep] = useState(0);
  const [plan, setPlan] = useState("");
  const [form, setForm] = useState({
    first_name: "", last_name: "", email: "", phone: "", org_name: "",
    line1: "", line2: "", city: "", subdivision: "", postal_code: "", country: "US",
  });
  const [options, setOptions] = useState<Set<string>>(new Set(["cv-cloud"]));
  const [licensedTb, setLicensedTb] = useState(1);
  const [qty, setQty] = useState<Record<number, number>>({});
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [done, setDone] = useState<{ dev_code?: string; trial?: boolean } | null>(null);

  const stripeRef = useRef<any>(null);
  const cardElRef = useRef<any>(null);
  const cardMountRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    api.get<Config>("/signup/config").then(setCfg).catch(() => setCfg(null));
    api.get<PayConfig>("/signup/payment-config").then(setPay).catch(() => setPay({ configured: false }));
  }, []);

  const stripeMode = pay?.processor === "stripe" && !!pay.publishable_key;
  const selectedPlan = useMemo(() => cfg?.plans.find((p) => p.id === plan), [cfg, plan]);
  const isDedicated = DEDICATED.has(plan);
  const subs = form.country === "CA" ? CA_PROVINCES : US_STATES;
  const pricing = cfg?.pricing;
  const trialDays = cfg?.trial_days || 7;

  // Live monthly estimate (mirrors the in-app onboarding math).
  const est = useMemo(() => {
    if (!pricing || !selectedPlan) return { monthly: 0, payg: false };
    const rate = selectedPlan.price_per_tb_month;
    const minTb = selectedPlan.min_tb || 0;
    const billable = isDedicated ? Math.max(licensedTb, minTb) : minTb;
    const protection = billable * rate;
    const appliance = (pricing.appliance_tiers || []).reduce((s, t) => s + (qty[t.capacity_tb] || 0) * t.monthly, 0);
    return { monthly: protection + appliance, payg: !isDedicated };
  }, [pricing, selectedPlan, isDedicated, licensedTb, qty]);

  // Mount the Stripe card field on the billing step.
  useEffect(() => {
    if (step !== 4 || !stripeMode || !pay?.publishable_key) return;
    let disposed = false;
    (async () => {
      try {
        const stripe = await loadStripe(pay.publishable_key!);
        if (disposed) return;
        stripeRef.current = stripe;
        const elements = stripe.elements();
        const color = getComputedStyle(document.documentElement).getPropertyValue("--text").trim() || "#e6ebff";
        const card = elements.create("card", { style: { base: { color, fontSize: "14px", "::placeholder": { color: "#8a93a6" } } } });
        setTimeout(() => { if (cardMountRef.current) card.mount(cardMountRef.current); }, 0);
        cardElRef.current = card;
      } catch (e) { setErr((e as Error).message || "Could not load the card form."); }
    })();
    return () => { disposed = true; try { cardElRef.current?.destroy(); } catch { /* ignore */ } cardElRef.current = null; };
  }, [step, stripeMode, pay?.publishable_key]);

  function up(k: keyof typeof form, v: string) { setForm((f) => ({ ...f, [k]: v })); }
  function toggleOpt(id: string) { setOptions((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n; }); }
  function bump(cap: number, d: number) { setQty((c) => ({ ...c, [cap]: Math.max(0, (c[cap] || 0) + d) })); }

  function validAccount(): string {
    if (!form.first_name.trim()) return "Please enter your first name.";
    if (!form.email.includes("@")) return "Please enter a valid email.";
    if (isDedicated && !form.org_name.trim()) return "Please name your organization.";
    return "";
  }
  function validAddress(): string {
    if (!COUNTRIES[form.country]) return "Sign-up is only available in the US and Canada right now.";
    if (!form.line1.trim()) return "Please enter your street address.";
    if (!form.city.trim()) return "Please enter your city.";
    if (!form.subdivision) return "Please choose your state or province.";
    if (!form.postal_code.trim()) return "Please enter your postal code.";
    return "";
  }

  async function submit(withCard: boolean) {
    setErr(""); setBusy(true);
    try {
      let token: string | null = null;
      if (withCard && stripeMode) {
        const stripe = stripeRef.current;
        if (!stripe || !cardElRef.current) { setErr("The card form isn't ready yet."); setBusy(false); return; }
        const { paymentMethod, error } = await stripe.createPaymentMethod({
          type: "card", card: cardElRef.current,
          billing_details: { name: `${form.first_name} ${form.last_name}`.trim(), email: form.email },
        });
        if (error) { setErr(error.message || "That card was declined."); setBusy(false); return; }
        token = paymentMethod.id;
      }
      const applianceArr = Object.entries(qty).filter(([, q]) => q > 0).map(([cap, q]) => ({ capacity_tb: Number(cap), qty: q }));
      const res = await api.post<{ ok: boolean; dev_code?: string; trial?: boolean }>("/signup", {
        email: form.email.trim().toLowerCase(),
        first_name: form.first_name.trim(), last_name: form.last_name.trim(),
        phone: form.phone.trim(), org_name: form.org_name.trim(), plan,
        address: { line1: form.line1.trim(), line2: form.line2.trim(), city: form.city.trim(),
          subdivision: form.subdivision, postal_code: form.postal_code.trim(), country: form.country },
        options: [...options], licensed_tb: licensedTb, appliance_plan: applianceArr,
        payment_method_token: token, trial: true,
      });
      setDone({ dev_code: res.dev_code, trial: res.trial });
      setStep(5);
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
      <div className="card" style={{ width: "min(680px, 100%)", padding: 0, overflow: "hidden" }}>
        {/* Header + progress */}
        <div style={{ padding: "20px 24px 0" }}>
          <div className="spread" style={{ alignItems: "center" }}>
            <img src="/logos/Logo-Full.png" alt="Arkive" style={{ height: 26 }} />
            <span className="pill info" style={{ fontSize: 11 }}><Icon name="sparkle" size={11} /> {trialDays}-day free trial</span>
          </div>
          {step < 5 && (
            <div className="row" style={{ gap: 6, marginTop: 16, marginBottom: 4 }}>
              {STEPS.map((s, i) => (
                <div key={s} style={{ flex: 1 }}>
                  <div style={{ height: 4, borderRadius: 2, background: i <= step ? "var(--accent, #4f7cff)" : "var(--border-soft)" }} />
                  <div className="faint" style={{ fontSize: 10, marginTop: 4, color: i === step ? "var(--text)" : undefined }}>{s}</div>
                </div>
              ))}
            </div>
          )}
        </div>

        <div style={{ padding: 24 }}>
          {/* STEP 0 — Plan */}
          {step === 0 && (
            <>
              <h2 style={{ margin: "0 0 4px" }}>Choose your plan</h2>
              <div className="faint" style={{ fontSize: 12.5, marginBottom: 16 }}>Start with a {trialDays}-day free trial. Cancel anytime before it ends.</div>
              <div className="stack" style={{ gap: 10 }}>
                {(cfg?.plans || []).map((p) => {
                  const on = plan === p.id;
                  return (
                    <button key={p.id} className="card" onClick={() => setPlan(p.id)}
                            style={{ textAlign: "left", cursor: "pointer", position: "relative",
                              borderColor: on ? "var(--accent, #4f7cff)" : undefined, borderWidth: on ? 2 : 1 }}>
                      {p.id === RECOMMENDED && <span className="pill info" style={{ position: "absolute", top: -10, right: 14, fontSize: 10 }}>Most popular</span>}
                      <div className="spread">
                        <div style={{ fontWeight: 700, fontSize: 15 }}>{p.name}</div>
                        <div style={{ fontWeight: 700 }}>${p.price_per_tb_month}<span className="faint" style={{ fontWeight: 400, fontSize: 12 }}>/TB·mo</span></div>
                      </div>
                      <div className="faint" style={{ fontSize: 12.5, marginTop: 4 }}>{PLAN_BLURB[p.id] || ""}{p.min_tb ? ` · ${p.min_tb} TB minimum` : ""}</div>
                    </button>
                  );
                })}
                {!cfg && <div className="muted">Loading plans…</div>}
              </div>
              <div className="spread" style={{ marginTop: 18 }}>
                <button className="btn ghost sm" onClick={() => nav("/")}>Already have an account?</button>
                <button className="btn primary" disabled={!plan} onClick={() => setStep(1)}>Continue</button>
              </div>
            </>
          )}

          {/* STEP 1 — Account */}
          {step === 1 && (
            <>
              <h2 style={{ margin: "0 0 16px" }}>Your details</h2>
              <div className="row" style={{ gap: 10 }}>
                <div className="field flex1"><label>First name</label><input className="input" autoFocus value={form.first_name} onChange={(e) => up("first_name", e.target.value)} /></div>
                <div className="field flex1"><label>Last name</label><input className="input" value={form.last_name} onChange={(e) => up("last_name", e.target.value)} /></div>
              </div>
              <div className="field"><label>Email</label><input className="input" value={form.email} onChange={(e) => up("email", e.target.value)} placeholder="you@email.com" /></div>
              <div className="field"><label>Phone</label><input className="input" value={form.phone} onChange={(e) => up("phone", e.target.value)} placeholder="+1 (555) 123-4567" /></div>
              {isDedicated && <div className="field"><label>Organization name</label><input className="input" value={form.org_name} onChange={(e) => up("org_name", e.target.value)} placeholder="e.g. Smith Family, Acme Inc." /></div>}
              {err && <div className="pill danger" style={{ marginBottom: 10 }}>{err}</div>}
              <div className="spread" style={{ marginTop: 8 }}>
                <button className="btn ghost" onClick={() => setStep(0)}>Back</button>
                <button className="btn primary" onClick={() => { const e = validAccount(); if (e) return setErr(e); setErr(""); setStep(2); }}>Continue</button>
              </div>
            </>
          )}

          {/* STEP 2 — Address */}
          {step === 2 && (
            <>
              <h2 style={{ margin: "0 0 4px" }}>Billing address</h2>
              <div className="faint" style={{ fontSize: 12.5, marginBottom: 16 }}>We use this to place your data in the right region and for billing.</div>
              <div className="field"><label>Street address</label><input className="input" autoFocus value={form.line1} onChange={(e) => up("line1", e.target.value)} placeholder="123 Main St" /></div>
              <div className="field"><label>Apt, suite, etc. <span className="faint">(optional)</span></label><input className="input" value={form.line2} onChange={(e) => up("line2", e.target.value)} /></div>
              <div className="row" style={{ gap: 10 }}>
                <div className="field flex1"><label>Country</label>
                  <select className="input" value={form.country} onChange={(e) => { up("country", e.target.value); up("subdivision", ""); }}>
                    {Object.entries(COUNTRIES).map(([c, n]) => <option key={c} value={c}>{n}</option>)}
                  </select></div>
                <div className="field flex1"><label>City</label><input className="input" value={form.city} onChange={(e) => up("city", e.target.value)} /></div>
              </div>
              <div className="row" style={{ gap: 10 }}>
                <div className="field flex1"><label>{form.country === "CA" ? "Province" : "State"}</label>
                  <select className="input" value={form.subdivision} onChange={(e) => up("subdivision", e.target.value)}>
                    <option value="">—</option>{subs.map((s) => <option key={s} value={s}>{s}</option>)}
                  </select></div>
                <div className="field flex1"><label>{form.country === "CA" ? "Postal code" : "ZIP code"}</label><input className="input" value={form.postal_code} onChange={(e) => up("postal_code", e.target.value)} /></div>
              </div>
              {err && <div className="pill danger" style={{ marginBottom: 10 }}>{err}</div>}
              <div className="spread" style={{ marginTop: 8 }}>
                <button className="btn ghost" onClick={() => setStep(1)}>Back</button>
                <button className="btn primary" onClick={() => { const e = validAddress(); if (e) return setErr(e); setErr(""); setStep(3); }}>Continue</button>
              </div>
            </>
          )}

          {/* STEP 3 — Protection setup (mirrors in-app onboarding) */}
          {step === 3 && (
            <>
              <h2 style={{ margin: "0 0 4px" }}>Set up your protection</h2>
              <div className="faint" style={{ fontSize: 12.5, marginBottom: 16 }}>Choose where your encrypted backups are kept. You can change this anytime.</div>
              <div className="stack" style={{ gap: 10 }}>
                {(pricing?.tiers || []).map((t) => {
                  const on = options.has(t.id);
                  return (
                    <button key={t.id} className="card" onClick={() => toggleOpt(t.id)}
                            style={{ textAlign: "left", cursor: "pointer", borderColor: on ? (t.color || "var(--accent, #4f7cff)") : undefined, borderWidth: on ? 2 : 1 }}>
                      <div className="row" style={{ gap: 10, alignItems: "flex-start" }}>
                        <div className="result-icon" style={{ width: 30, height: 30, background: "var(--inset)", color: t.color }}><Icon name={iconOf(t.icon)} size={16} /></div>
                        <div className="flex1">
                          <div className="spread"><span style={{ fontWeight: 700 }}>{t.title}</span>{on && <Icon name="check" size={15} />}</div>
                          <div className="faint" style={{ fontSize: 12 }}>{t.tagline}</div>
                        </div>
                      </div>
                    </button>
                  );
                })}
              </div>

              {isDedicated && (
                <div style={{ marginTop: 16 }}>
                  <label className="faint" style={{ fontSize: 12 }}>Licensed protected storage: <b style={{ color: "var(--text)" }}>{licensedTb} TB</b></label>
                  <input type="range" min={Math.max(1, selectedPlan?.min_tb || 1)} max={50} value={licensedTb}
                         onChange={(e) => setLicensedTb(parseInt(e.target.value, 10))} style={{ width: "100%" }} />
                </div>
              )}

              {options.has("appliance") && (pricing?.appliance_tiers || []).length > 0 && (
                <div style={{ marginTop: 16 }}>
                  <div className="faint" style={{ fontSize: 12, marginBottom: 8 }}>Add a secure appliance</div>
                  <div className="stack" style={{ gap: 6 }}>
                    {(pricing?.appliance_tiers || []).map((t) => (
                      <div key={t.capacity_tb} className="spread" style={{ fontSize: 12.5 }}>
                        <span>{t.model} · {t.capacity_tb} TB <span className="faint">· {money(t.monthly)}/mo + {money(t.setup)} setup</span></span>
                        <span className="row" style={{ gap: 8, alignItems: "center" }}>
                          <button className="btn ghost sm" onClick={() => bump(t.capacity_tb, -1)}>−</button>
                          <span style={{ minWidth: 16, textAlign: "center" }}>{qty[t.capacity_tb] || 0}</span>
                          <button className="btn ghost sm" onClick={() => bump(t.capacity_tb, 1)}>+</button>
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              <div className="card" style={{ background: "var(--inset)", marginTop: 16 }}>
                <div className="spread"><span className="faint">Estimated monthly</span>
                  <span style={{ fontWeight: 700 }}>{est.payg ? "Pay as you go" : `${money(est.monthly)}/mo`}</span></div>
                <div className="faint" style={{ fontSize: 11, marginTop: 4 }}>Free for {trialDays} days · then billed monthly{est.payg ? ` at $${selectedPlan?.price_per_tb_month}/TB` : ""}.</div>
              </div>

              <div className="spread" style={{ marginTop: 16 }}>
                <button className="btn ghost" onClick={() => setStep(2)}>Back</button>
                <button className="btn primary" disabled={options.size === 0} onClick={() => { setErr(""); setStep(4); }}>Continue</button>
              </div>
            </>
          )}

          {/* STEP 4 — Billing */}
          {step === 4 && (
            <>
              <h2 style={{ margin: "0 0 4px" }}>Start your free trial</h2>
              <div className="faint" style={{ fontSize: 12.5, marginBottom: 16 }}>
                Free for {trialDays} days. {est.payg ? "You'll be billed for what you protect" : `Then ${money(est.monthly)}/mo`} — cancel anytime before the trial ends and you won't be charged.
              </div>
              {stripeMode ? (
                <>
                  <div className="field"><label>Card details</label><div ref={cardMountRef} className="input" style={{ padding: "12px 12px" }} /></div>
                  <div className="faint" style={{ fontSize: 11, marginBottom: 12 }}><Icon name="lock" size={11} /> Secured by Stripe. Your card is charged {money(est.monthly)} only after your {trialDays}-day trial ends.</div>
                  {err && <div className="pill danger" style={{ marginBottom: 10 }}>{err}</div>}
                  <div className="spread">
                    <button className="btn ghost" onClick={() => setStep(3)} disabled={busy}>Back</button>
                    <button className="btn primary" onClick={() => submit(true)} disabled={busy}>{busy ? "Starting your trial…" : `Start ${trialDays}-day free trial`}</button>
                  </div>
                </>
              ) : (
                <>
                  <div className="card" style={{ background: "var(--inset)" }}>
                    <div className="row" style={{ gap: 8, alignItems: "flex-start" }}>
                      <Icon name="info" size={15} />
                      <div className="faint" style={{ fontSize: 12.5 }}>
                        Card entry isn't available right now — start your {trialDays}-day trial and add a payment method from your account before it ends.
                      </div>
                    </div>
                  </div>
                  {err && <div className="pill danger" style={{ margin: "10px 0" }}>{err}</div>}
                  <div className="spread" style={{ marginTop: 14 }}>
                    <button className="btn ghost" onClick={() => setStep(3)} disabled={busy}>Back</button>
                    <button className="btn primary" onClick={() => submit(false)} disabled={busy}>{busy ? "Creating your account…" : `Start ${trialDays}-day free trial`}</button>
                  </div>
                </>
              )}
            </>
          )}

          {/* STEP 5 — Done */}
          {step === 5 && done && (
            <div style={{ textAlign: "center", padding: "12px 0" }}>
              <div style={{ display: "inline-flex", padding: 14, borderRadius: "50%", background: "var(--inset)", marginBottom: 12 }}><Icon name="mail" size={24} /></div>
              <h2 style={{ margin: "0 0 6px" }}>Check your email</h2>
              <div className="faint" style={{ fontSize: 13, maxWidth: 380, margin: "0 auto" }}>
                {done.trial ? `Your ${trialDays}-day free trial has started. ` : ""}
                We sent a one-time sign-in code to <b>{form.email}</b>. Enter it on the next screen to finish setting up your account.
              </div>
              {done.dev_code && <div className="pill info" style={{ marginTop: 12 }}>Dev code: <span className="mono" style={{ marginLeft: 6 }}>{done.dev_code}</span></div>}
              <button className="btn primary" style={{ width: "100%", marginTop: 18 }} onClick={() => nav("/")}>Continue to sign in</button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
