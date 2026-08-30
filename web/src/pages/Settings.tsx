import { useEffect, useRef, useState } from "react";
import { api, Me } from "../api";
import { useAuth } from "../auth";
import { Card, Pill, Loading } from "../components/ui";
import { Icon, IconName } from "../components/Icon";
import { formDialog, confirmDialog, notify } from "../components/dialog";
import { getTheme, applyTheme, Theme } from "../theme";

interface Tenant { name: string; plan: string; key_ownership_model: string; vaults: any[]; }
interface KeyInfo {
  vault_id: string; vault_name: string; provisioned: boolean; status: string;
  content_algorithm: string; signature_algorithm: string; recovery_kem: string | null;
  strength_bits: number; pq_hybrid: boolean; ownership_model: string | null; root_key_hash: string | null;
}

type SettingsTab = "personal" | "billing" | "look" | "notifications" | "features" | "security" | "data";
const SETTINGS_TABS: { key: SettingsTab; label: string; icon: IconName }[] = [
  { key: "personal", label: "Personal information", icon: "user" },
  { key: "billing", label: "Billing", icon: "credit-card" },
  { key: "look", label: "Look & feel", icon: "sun" },
  { key: "notifications", label: "Notification preferences", icon: "mail" },
  { key: "features", label: "Account features", icon: "grid" },
  { key: "security", label: "Security settings", icon: "lock" },
  { key: "data", label: "Data protection", icon: "shield" },
];

export default function Settings() {
  const { me, enrollPasskey, refresh } = useAuth();
  const [tenant, setTenant] = useState<Tenant | null>(null);
  const [keys, setKeys] = useState<KeyInfo[]>([]);
  const [toast, setToast] = useState("");
  const [theme, setThemeState] = useState<Theme>(getTheme());
  const [loaded, setLoaded] = useState(false);
  const [tab, setTab] = useState<SettingsTab>("personal");

  useEffect(() => {
    api.get<Tenant>("/tenant").then(setTenant).catch(() => {}).finally(() => setLoaded(true));
    api.get<KeyInfo[]>("/tenant/keys").then(setKeys).catch(() => {});
  }, []);

  async function loadVaultsAndKeys() {
    const [t, k] = await Promise.all([
      api.get<Tenant>("/tenant").catch(() => null),
      api.get<KeyInfo[]>("/tenant/keys").catch(() => [] as KeyInfo[]),
    ]);
    if (t) setTenant(t);
    setKeys(k);
  }

  async function createVault() {
    const r = await formDialog({
      title: "New vault",
      message: "A vault is your own encrypted store with its own root key. You can route different sources into different vaults.",
      confirmLabel: "Create vault",
      fields: [
        { name: "name", label: "Vault name", required: true, placeholder: "e.g. Personal, Photos, Work" },
        { name: "key_ownership_model", label: "Key ownership", defaultValue: tenant?.key_ownership_model || "customer-managed",
          options: [
            { label: "Customer-managed — you hold the keys", value: "customer-managed" },
            { label: "Zero-knowledge — only you can decrypt", value: "zero-knowledge" },
          ] },
      ],
    });
    if (!r) return;
    try {
      await api.post("/tenant/vaults", r);
      await loadVaultsAndKeys();
      setToast("Vault created");
      setTimeout(() => setToast(""), 2500);
    } catch (e) {
      notify({ message: (e as { message?: string }).message || "Could not create vault", tone: "warn" });
    }
  }

  function pickTheme(t: Theme) { applyTheme(t); setThemeState(t); }

  async function addPasskey(transport: string) {
    await enrollPasskey(transport === "internal" ? "This device" : "Security key");
    await refresh();
    setToast("Passkey enrolled");
    setTimeout(() => setToast(""), 2500);
  }

  if (!loaded && !tenant) return <Loading label="Loading settings…" />;

  return (
    <div className="settings-layout">
      <nav className="settings-nav">
        {SETTINGS_TABS.map((t) => (
          <button key={t.key} className={`settings-nav-item ${tab === t.key ? "active" : ""}`}
                  onClick={() => setTab(t.key)}>
            <Icon name={t.icon} size={16} /> <span>{t.label}</span>
          </button>
        ))}
      </nav>

      <div className="settings-content">
        {tab === "personal" && <PersonalInfo me={me} tenant={tenant} refresh={refresh} />}

        {tab === "billing" && <><PlanChangeCard /><BillingSettings /></>}

        {tab === "look" && (
          <Card>
            <h2 style={{ marginBottom: 4 }}>Look &amp; feel</h2>
            <div className="muted" style={{ fontSize: 12.5, marginBottom: 14 }}>
              Choose how Arkive looks on this device.
            </div>
            <div className="row" style={{ gap: 12 }}>
              {([
                { id: "dark" as Theme, label: "Dark", icon: "moon" as const },
                { id: "light" as Theme, label: "Light", icon: "sun" as const },
              ]).map((opt) => {
                const on = theme === opt.id;
                return (
                  <div key={opt.id} onClick={() => pickTheme(opt.id)}
                       style={{ cursor: "pointer", flex: "0 0 150px", border: `1.5px solid ${on ? "var(--brand)" : "var(--border-soft)"}`,
                                borderRadius: 12, padding: 14, background: on ? "rgba(79,124,255,.08)" : "var(--inset)", transition: "all .12s" }}>
                    <div className="spread" style={{ marginBottom: 10 }}>
                      <Icon name={opt.icon} size={16} />
                      {on && <Icon name="check" size={15} />}
                    </div>
                    <div style={{ display: "flex", gap: 6, marginBottom: 10 }}>
                      <span style={{ width: 30, height: 20, borderRadius: 4, background: opt.id === "light" ? "#f4f6fb" : "#0b0f17", border: "1px solid var(--border-soft)" }} />
                      <span style={{ width: 30, height: 20, borderRadius: 4, background: opt.id === "light" ? "#ffffff" : "#141b2b", border: "1px solid var(--border-soft)" }} />
                      <span style={{ width: 20, height: 20, borderRadius: 4, background: "#4f7cff" }} />
                    </div>
                    <div style={{ fontWeight: 600, fontSize: 13 }}>{opt.label}</div>
                  </div>
                );
              })}
            </div>
          </Card>
        )}

        {tab === "notifications" && <NotificationSettings />}

        {tab === "features" && <ContactLinkingSettings />}

        {tab === "security" && (
          <>
            <Card style={{ marginBottom: 16 }}>
              <h2 style={{ marginBottom: 12 }}>Identity &amp; unlock</h2>
              <div className="muted" style={{ fontSize: 12.5, marginBottom: 14 }}>
                Passkeys and hardware tokens unlock the interfaces where you access protected data.
              </div>
              {me?.passkeys.map((p) => (
                <div key={p.id} className="result-row">
                  <div className="result-icon" style={{ background: "var(--bg-elev-2)" }}><Icon name="key" size={16} /></div>
                  <div className="flex1">
                    <div style={{ fontWeight: 600 }}>{p.label}</div>
                    <div className="faint" style={{ fontSize: 12 }}>{p.transport}</div>
                  </div>
                  <Pill tone="ok" dot>active</Pill>
                </div>
              ))}
              <div className="row" style={{ marginTop: 12, gap: 8 }}>
                <button className="btn sm" onClick={() => addPasskey("internal")}>
                  <Icon name="key" size={14} /> Add device passkey
                </button>
                <button className="btn sm" onClick={() => addPasskey("usb")}>
                  <Icon name="lock" size={14} /> Add hardware token
                </button>
              </div>
            </Card>

            <Card>
              <h2 style={{ marginBottom: 4 }}>Your encryption keys</h2>
              <div className="muted" style={{ fontSize: 12.5, marginBottom: 14 }}>
                You hold the keys to your data. These live in your key broker record and are shown here so you can
                confirm their type, strength and status — the key material itself is never exposed.
              </div>
              {keys.map((k) => (
                <div key={k.vault_id} className="result-row">
                  <div className="result-icon" style={{ background: "var(--bg-elev-2)" }}><Icon name="key" size={16} /></div>
                  <div className="flex1">
                    <div style={{ fontWeight: 600 }}>{k.vault_name}</div>
                    <div className="faint" style={{ fontSize: 12 }}>
                      {k.content_algorithm} · {k.pq_hybrid ? "hybrid post-quantum" : "classical"} · {k.recovery_kem || "ML-KEM"} · {k.strength_bits}-bit
                      {k.root_key_hash ? ` · ${k.root_key_hash.slice(0, 16)}…` : ""}
                    </div>
                  </div>
                  <Pill tone={k.pq_hybrid ? "ok" : "info"} dot>{k.pq_hybrid ? "quantum-safe" : "classical"}</Pill>
                  <Pill tone={k.status === "active" ? "ok" : "warn"} dot>{k.status}</Pill>
                </div>
              ))}
              {keys.length === 0 && <div className="muted" style={{ fontSize: 12.5 }}>No keys provisioned yet.</div>}
              <div className="divider" />
              <h3 style={{ marginBottom: 8 }}>How your keys are used</h3>
              <ul className="faint" style={{ fontSize: 12.5, margin: 0, paddingLeft: 18, lineHeight: 1.7 }}>
                <li>A unique root key encrypts your vault's contents with <b>AES-256-GCM</b>. Every vault has its own key.</li>
                <li>Your key is <b>wrapped</b> (never stored in the clear) and only unwrapped for operations you authorize by unlocking with your passkey.</li>
                <li><b>ML-KEM</b> protects recovery key exchange and <b>ML-DSA</b> signs appliance receipts — a hybrid, quantum-safe design.</li>
                <li>Arkive can't read your data: content stays encrypted end-to-end. Only you — or, for continuity, an authorized organization-admin key recovery — can unwrap it.</li>
              </ul>
            </Card>
          </>
        )}

        {tab === "data" && (
          <>
            <Card>
              <div className="spread" style={{ marginBottom: 10 }}>
                <div>
                  <h2 style={{ margin: 0 }}>Your vaults</h2>
                  <div className="muted" style={{ fontSize: 12.5 }}>Each vault is an independently encrypted store with its own root key.</div>
                </div>
                <button className="btn primary sm" onClick={createVault}>
                  <Icon name="shield" size={14} /> New vault
                </button>
              </div>
              {tenant?.vaults.map((v) => (
                <div key={v.id} className="result-row">
                  <div className="result-icon" style={{ background: "linear-gradient(135deg,#4f7cff,#35d0a5)" }}>
                    <Icon name="shield" size={16} />
                  </div>
                  <div className="flex1">
                    <div style={{ fontWeight: 600 }}>{v.name}</div>
                    <div className="faint mono" style={{ fontSize: 11 }}>{v.crypto_profile_id}</div>
                  </div>
                  <Pill tone="info">{v.key_ownership_model}</Pill>
                </div>
              ))}
              {(!tenant?.vaults || tenant.vaults.length === 0) && (
                <div className="muted" style={{ fontSize: 12.5 }}>
                  You don't have a vault yet. Create one to start protecting data.
                </div>
              )}
            </Card>
          </>
        )}
      </div>
      {toast && <div className="toast"><Icon name="check" size={15} /> {toast}</div>}
    </div>
  );
}

function PersonalInfo({ me, tenant, refresh }: { me: Me | null; tenant: Tenant | null; refresh: () => Promise<void> }) {
  const [first, setFirst] = useState(me?.first_name || "");
  const [last, setLast] = useState(me?.last_name || "");
  const [phone, setPhone] = useState(me?.phone || "");
  const [saving, setSaving] = useState(false);
  const [savedMsg, setSavedMsg] = useState("");
  useEffect(() => {
    setFirst(me?.first_name || ""); setLast(me?.last_name || ""); setPhone(me?.phone || "");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [me?.user_id]);
  const dirty = first !== (me?.first_name || "") || last !== (me?.last_name || "") || phone !== (me?.phone || "");
  async function save() {
    setSaving(true);
    try {
      await api.put("/auth/me/profile", { first_name: first, last_name: last, phone });
      await refresh();
      setSavedMsg("Saved"); setTimeout(() => setSavedMsg(""), 2000);
    } catch (e: any) { notify({ message: e.message || "Could not save", tone: "danger" }); }
    finally { setSaving(false); }
  }
  return (
    <Card>
      <h2 style={{ marginBottom: 4 }}>Personal information</h2>
      <div className="muted" style={{ fontSize: 12.5, marginBottom: 16 }}>
        Your name and contact details. Your email is your sign-in and is changed through a separate verified flow.
      </div>
      <div className="grid grid-2" style={{ gap: 12, marginBottom: 4 }}>
        <SettingsField label="First name" value={first} onChange={setFirst} placeholder="First name" />
        <SettingsField label="Last name" value={last} onChange={setLast} placeholder="Last name" />
        <SettingsField label="Phone" value={phone} onChange={setPhone} placeholder="+1 555 123 4567" />
        <label className="stack" style={{ gap: 5 }}>
          <span className="faint" style={{ fontSize: 12 }}>Email (sign-in)</span>
          <input className="input" value={me?.email || ""} disabled readOnly />
        </label>
      </div>
      <div className="row" style={{ gap: 10, alignItems: "center", marginTop: 12 }}>
        <button className="btn primary" disabled={saving || !dirty} onClick={save}>
          {saving ? "Saving…" : "Save changes"}
        </button>
        {savedMsg && <span className="faint" style={{ fontSize: 12.5, color: "var(--ok, #35d0a5)" }}><Icon name="check" size={13} /> {savedMsg}</span>}
      </div>

      <div className="divider" />
      <h3 style={{ marginBottom: 8 }}>Account</h3>
      <Row label="Organization" value={tenant?.name} />
      <Row label="Plan" value={tenant?.plan} />
      <Row label="Your role" value={me?.role} />
      <Row label="Key ownership" value={tenant?.key_ownership_model} />

      <div className="divider" />
      <AddressBook />
    </Card>
  );
}

interface Address {
  id: string; kind: string; label: string; name: string; line1: string; line2: string;
  city: string; region: string; postal_code: string; country: string; phone: string; is_default: boolean;
}
const ADDRESS_KINDS: { key: string; label: string }[] = [
  { key: "billing", label: "Billing" },
  { key: "shipping", label: "Shipping" },
  { key: "alternate", label: "Alternate" },
];
const BLANK_ADDRESS: Omit<Address, "id"> = {
  kind: "shipping", label: "", name: "", line1: "", line2: "", city: "", region: "",
  postal_code: "", country: "US", phone: "", is_default: false,
};

function AddressBook() {
  const [addrs, setAddrs] = useState<Address[]>([]);
  const [editing, setEditing] = useState<Address | (Omit<Address, "id"> & { id?: string }) | null>(null);
  const [busy, setBusy] = useState(false);

  async function load() {
    try { setAddrs((await api.get<{ addresses: Address[] }>("/auth/me/addresses")).addresses); }
    catch { /* ignore */ }
  }
  useEffect(() => { void load(); }, []);

  async function save() {
    if (!editing) return;
    setBusy(true);
    try {
      if (editing.id) await api.put(`/auth/me/addresses/${editing.id}`, editing);
      else await api.post("/auth/me/addresses", editing);
      setEditing(null);
      await load();
    } catch (e: any) { notify({ message: e.message || "Could not save address", tone: "danger" }); }
    finally { setBusy(false); }
  }
  async function remove(a: Address) {
    if (!(await confirmDialog({ title: "Remove address?", message: `Delete this ${a.kind} address?`, tone: "danger", confirmLabel: "Delete" }))) return;
    try { await api.del(`/auth/me/addresses/${a.id}`); await load(); }
    catch (e: any) { notify({ message: e.message, tone: "danger" }); }
  }

  return (
    <div>
      <div className="spread" style={{ marginBottom: 10 }}>
        <div>
          <h3 style={{ margin: 0 }}>Addresses</h3>
          <div className="muted" style={{ fontSize: 12.5 }}>Billing, shipping and alternate addresses for orders and invoices.</div>
        </div>
        <button className="btn sm" onClick={() => setEditing({ ...BLANK_ADDRESS })}>
          <Icon name="edit" size={14} /> Add address
        </button>
      </div>
      {addrs.length === 0 && !editing && (
        <div className="muted" style={{ fontSize: 12.5 }}>No addresses saved yet.</div>
      )}
      {addrs.map((a) => (
        <div key={a.id} className="result-row">
          <div className="result-icon" style={{ background: "var(--inset)" }}><Icon name="note" size={16} /></div>
          <div className="flex1">
            <div className="row" style={{ gap: 8, alignItems: "center" }}>
              <span style={{ fontWeight: 600, textTransform: "capitalize" }}>{a.kind}</span>
              {a.label && <span className="faint" style={{ fontSize: 12 }}>· {a.label}</span>}
              {a.is_default && <Pill tone="ok">Default</Pill>}
            </div>
            <div className="faint" style={{ fontSize: 12 }}>
              {[a.name, a.line1, a.line2, [a.city, a.region, a.postal_code].filter(Boolean).join(", "), a.country]
                .filter(Boolean).join(" · ")}
            </div>
          </div>
          <button className="btn ghost sm" onClick={() => setEditing({ ...a })}><Icon name="edit" size={14} /></button>
          <button className="btn ghost sm" onClick={() => void remove(a)}><Icon name="trash" size={14} /></button>
        </div>
      ))}
      {editing && (
        <div className="card" style={{ marginTop: 12, background: "var(--inset)" }}>
          <div className="grid grid-2" style={{ gap: 10 }}>
            <label className="stack" style={{ gap: 5 }}>
              <span className="faint" style={{ fontSize: 12 }}>Type</span>
              <select className="input" value={editing.kind} onChange={(e) => setEditing({ ...editing, kind: e.target.value })}>
                {ADDRESS_KINDS.map((k) => <option key={k.key} value={k.key}>{k.label}</option>)}
              </select>
            </label>
            <SettingsField label="Label (optional)" value={editing.label} onChange={(v) => setEditing({ ...editing, label: v })} placeholder="Home, Office…" />
            <SettingsField label="Full name" value={editing.name} onChange={(v) => setEditing({ ...editing, name: v })} placeholder="Recipient name" />
            <SettingsField label="Phone" value={editing.phone} onChange={(v) => setEditing({ ...editing, phone: v })} placeholder="+1 555 123 4567" />
            <SettingsField label="Address line 1" value={editing.line1} onChange={(v) => setEditing({ ...editing, line1: v })} placeholder="Street address" />
            <SettingsField label="Address line 2" value={editing.line2} onChange={(v) => setEditing({ ...editing, line2: v })} placeholder="Apt, suite (optional)" />
            <SettingsField label="City" value={editing.city} onChange={(v) => setEditing({ ...editing, city: v })} placeholder="City" />
            <SettingsField label="State / region" value={editing.region} onChange={(v) => setEditing({ ...editing, region: v })} placeholder="State" />
            <SettingsField label="Postal code" value={editing.postal_code} onChange={(v) => setEditing({ ...editing, postal_code: v })} placeholder="ZIP" />
            <SettingsField label="Country" value={editing.country} onChange={(v) => setEditing({ ...editing, country: v })} placeholder="US" />
          </div>
          <label className="row" style={{ gap: 8, marginTop: 12, alignItems: "center", cursor: "pointer" }}>
            <input type="checkbox" checked={editing.is_default} onChange={(e) => setEditing({ ...editing, is_default: e.target.checked })} />
            <span style={{ fontSize: 13 }}>Set as default {editing.kind} address</span>
          </label>
          <div className="row" style={{ gap: 8, marginTop: 12 }}>
            <button className="btn primary sm" disabled={busy} onClick={save}>{busy ? "Saving…" : "Save address"}</button>
            <button className="btn ghost sm" onClick={() => setEditing(null)}>Cancel</button>
          </div>
        </div>
      )}
    </div>
  );
}

function SettingsField({ label, value, onChange, placeholder }: {
  label: string; value: string; onChange: (v: string) => void; placeholder?: string;
}) {
  return (
    <label className="stack" style={{ gap: 5 }}>
      <span className="faint" style={{ fontSize: 12 }}>{label}</span>
      <input className="input" value={value} placeholder={placeholder} onChange={(e) => onChange(e.target.value)} />
    </label>
  );
}

interface PaymentMethodView {
  id: string; processor: string; type: string; brand: string; last4: string;
  exp_month: number; exp_year: number; holder_name: string; is_default: boolean;
}
interface PaymentConfig {
  configured: boolean; processor: string | null; name?: string; currency?: string;
  publishable_key?: string; client_id?: string; environment?: string;
}
interface SubscriptionResp {
  profile: {
    amount_cents: number; currency: string; interval: string; status: string; active: boolean;
    plan_name: string; current_period_end: string | null; next_charge_at: string | null;
  } | null;
  quote?: { amount_cents: number; currency: string; plan_id: string; plan_name: string };
}
function fmtCents(cents: number, cur = "USD"): string {
  const sym = cur === "USD" ? "$" : "";
  return `${sym}${(cents / 100).toFixed(2)}${cur === "USD" ? "" : " " + cur}`;
}

// Load Stripe.js once and return a Stripe instance for the publishable key.
let _stripeScript: Promise<void> | null = null;
function loadStripe(pk: string): Promise<any> {
  const w = window as any;
  if (!_stripeScript) {
    _stripeScript = new Promise<void>((resolve, reject) => {
      if (w.Stripe) { resolve(); return; }
      const s = document.createElement("script");
      s.src = "https://js.stripe.com/v3/";
      s.async = true;
      s.onload = () => resolve();
      s.onerror = () => reject(new Error("Could not load Stripe.js"));
      document.head.appendChild(s);
    });
  }
  return _stripeScript.then(() => w.Stripe(pk));
}
const BRAND_LABEL: Record<string, string> = {
  visa: "Visa", mastercard: "Mastercard", amex: "American Express",
  discover: "Discover", diners: "Diners Club", jcb: "JCB", card: "Card",
};

interface LicensePlanOpt { id: string; name: string; price_per_tb_month: number; min_tb: number }
interface PlanChangePreview {
  current_plan: { id: string; name: string };
  target_plan: { id: string; name: string; price_per_tb_month: number; min_tb: number };
  current_monthly: number; target_monthly: number; currency: string;
  is_upgrade: boolean; is_downgrade: boolean; is_inplace?: boolean; requires_new_tenant: boolean;
  affected_members: number; grace_days: number; cooldown_days?: number; blocked?: boolean; warnings: string[];
}
function money(n: number, cur = "USD"): string {
  return `${cur === "USD" ? "$" : ""}${(n || 0).toFixed(2)}${cur === "USD" ? "" : " " + cur}`;
}

function PlanChangeCard() {
  const { logout } = useAuth();
  const [plans, setPlans] = useState<LicensePlanOpt[]>([]);
  const [current, setCurrent] = useState<{ id: string; name: string } | null>(null);
  const [open, setOpen] = useState(false);
  const [target, setTarget] = useState("");
  const [prev, setPrev] = useState<PlanChangePreview | null>(null);
  const [tenantName, setTenantName] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  useEffect(() => {
    api.get<{ license_plans: LicensePlanOpt[] }>("/billing/pricing").then((p) => setPlans(p.license_plans || [])).catch(() => {});
    api.get<any>("/billing/plan").then((v) => v.license_plan && setCurrent({ id: v.license_plan.id, name: v.license_plan.name })).catch(() => {});
  }, []);

  function openModal() { setOpen(true); setTarget(""); setPrev(null); setErr(""); setTenantName(""); }
  function closeModal() { setOpen(false); setTarget(""); setPrev(null); setErr(""); }

  async function choose(id: string) {
    setTarget(id); setPrev(null); setErr(""); setTenantName("");
    if (!id) return;
    try { setPrev(await api.get<PlanChangePreview>(`/billing/plan-change/preview?plan=${encodeURIComponent(id)}`)); }
    catch (e: any) { setErr(e.message || "Could not preview this plan"); }
  }
  async function apply() {
    if (!prev) return;
    setBusy(true); setErr("");
    try {
      const r = await api.post<any>("/billing/plan-change", { plan: target, tenant_name: tenantName || null });
      // Close this modal BEFORE any toast/sign-out so nothing stacks behind it.
      setOpen(false); setBusy(false);
      if (r?.requires_relogin) {
        await notify({ title: "Plan changed", message: "You'll be signed out to finish switching — sign back in to continue.", tone: "ok" });
        logout();
        return;
      }
      const extra = r?.prorated_cents ? ` A prorated ${money(r.prorated_cents / 100)} was charged for the rest of this period.` : "";
      await notify({ title: "Plan changed", message: `You're now on ${prev.target_plan.name}.${extra}`, tone: "ok" });
    } catch (e: any) { setErr(e.message || "Could not change plan"); setBusy(false); }
  }

  const options = plans.filter((p) => p.id !== current?.id);
  if (plans.length === 0) return null;
  return (
    <Card style={{ marginBottom: 16 }}>
      <div className="spread" style={{ alignItems: "flex-start" }}>
        <div>
          <h2 style={{ marginBottom: 4 }}>Plan &amp; account type</h2>
          <div className="muted" style={{ fontSize: 12.5 }}>
            Current plan: <b>{current?.name || "—"}</b>. Upgrading to a Family/Business plan creates your own
            organization; downgrading returns you to a personal account.
          </div>
        </div>
        <button className="btn primary sm" onClick={openModal} style={{ whiteSpace: "nowrap" }}>
          <Icon name="credit-card" size={14} /> Change Plan Type
        </button>
      </div>

      {open && (
        <div className="modal-backdrop" onClick={closeModal}>
          <div className="modal-panel" style={{ width: "min(560px, 100%)" }} onClick={(e) => e.stopPropagation()}>
            <div className="spread">
              <div>
                <h3 style={{ margin: 0 }}>Change plan type</h3>
                <div className="faint" style={{ fontSize: 12 }}>Currently on {current?.name || "—"}</div>
              </div>
              <button className="btn ghost sm" onClick={closeModal}><Icon name="logout" size={14} /></button>
            </div>
            <div className="modal-body">
              <label className="stack" style={{ gap: 5 }}>
                <span className="faint" style={{ fontSize: 12 }}>New plan</span>
                <select className="input" value={target} onChange={(e) => choose(e.target.value)}>
                  <option value="">— choose a plan —</option>
                  {options.map((p) => <option key={p.id} value={p.id}>{p.name} — ${p.price_per_tb_month}/TB·mo{p.min_tb ? `, min ${p.min_tb} TB` : ""}</option>)}
                </select>
              </label>

              {prev && (
                <div className="card" style={{ marginTop: 14, background: "var(--inset)" }}>
                  <div className="faint" style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: ".06em", marginBottom: 8 }}>Billing summary</div>
                  <div className="spread" style={{ marginBottom: 4 }}>
                    <span>{prev.current_plan.name}</span>
                    <span className="faint">{money(prev.current_monthly, prev.currency)}/mo</span>
                  </div>
                  <div className="spread" style={{ fontWeight: 700 }}>
                    <span>{prev.target_plan.name}{prev.target_plan.min_tb ? <span className="faint" style={{ fontWeight: 400, fontSize: 12 }}> · {prev.target_plan.min_tb} TB min</span> : null}</span>
                    <span>{money(prev.target_monthly, prev.currency)}/mo</span>
                  </div>
                  <div className="row" style={{ gap: 6, marginTop: 8 }}>
                    {prev.is_upgrade && <Pill tone="ok">Upgrade</Pill>}
                    {prev.is_downgrade && <Pill tone="warn">Downgrade</Pill>}
                    {prev.is_inplace && <Pill tone="info">Plan change</Pill>}
                  </div>
                  {prev.requires_new_tenant && (
                    <div style={{ marginTop: 12 }}>
                      <SettingsField label="Name your organization" value={tenantName} onChange={setTenantName} placeholder="e.g. Smith Family" />
                    </div>
                  )}
                  {prev.warnings.map((w, i) => (
                    <div key={i} className="row" style={{ gap: 8, marginTop: 10, alignItems: "flex-start", color: "var(--text-dim)", fontSize: 12.5 }}>
                      <Icon name="alert" size={13} /> <span>{w}</span>
                    </div>
                  ))}
                </div>
              )}
              {err && <div style={{ color: "var(--danger, #ff5d5d)", fontSize: 12.5, marginTop: 10 }}><Icon name="alert" size={13} /> {err}</div>}
            </div>
            <div className="modal-foot">
              <button className="btn ghost sm" onClick={closeModal}>Cancel</button>
              <button className="btn primary sm" disabled={busy || !prev || prev.blocked || (prev.requires_new_tenant && !tenantName.trim())} onClick={apply}>
                {busy ? "Applying…" : prev ? `Confirm — switch to ${prev.target_plan.name}` : "Confirm"}
              </button>
            </div>
          </div>
        </div>
      )}
    </Card>
  );
}

function BillingSettings() {
  const [cfg, setCfg] = useState<PaymentConfig | null>(null);
  const [methods, setMethods] = useState<PaymentMethodView[]>([]);
  const [addrs, setAddrs] = useState<Address[]>([]);
  const [sub, setSub] = useState<SubscriptionResp | null>(null);
  const [adding, setAdding] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [form, setForm] = useState({ number: "", exp: "", cvc: "", holder_name: "", billing_address_id: "" });
  const stripeRef = useRef<any>(null);
  const cardElRef = useRef<any>(null);
  const cardMountRef = useRef<HTMLDivElement | null>(null);

  const stripeMode = cfg?.processor === "stripe" && !!cfg.publishable_key;

  async function load() {
    const [c, m, a, s] = await Promise.all([
      api.get<PaymentConfig>("/billing/payment-config").catch(() => null),
      api.get<{ payment_methods: PaymentMethodView[] }>("/billing/payment-methods").catch(() => ({ payment_methods: [] })),
      api.get<{ addresses: Address[] }>("/auth/me/addresses").catch(() => ({ addresses: [] })),
      api.get<SubscriptionResp>("/billing/subscription").catch(() => null),
    ]);
    setCfg(c); setMethods(m.payment_methods); setAddrs(a.addresses); setSub(s);
  }
  useEffect(() => { void load(); }, []);

  // Mount a Stripe Elements card field when adding a card in Stripe mode, so the
  // PAN is entered in Stripe's iframe and never touches our servers.
  useEffect(() => {
    if (!adding || !stripeMode || !cfg?.publishable_key) return;
    let disposed = false;
    (async () => {
      try {
        const stripe = await loadStripe(cfg.publishable_key!);
        if (disposed) return;
        stripeRef.current = stripe;
        const elements = stripe.elements();
        const dark = getComputedStyle(document.documentElement).getPropertyValue("--text").trim() || "#e6ebff";
        const card = elements.create("card", {
          style: { base: { color: dark, fontSize: "14px", "::placeholder": { color: "#8a93a6" } } },
        });
        if (cardMountRef.current) card.mount(cardMountRef.current);
        cardElRef.current = card;
      } catch (e: any) { setErr(e.message || "Could not load the card form."); }
    })();
    return () => {
      disposed = true;
      try { cardElRef.current?.destroy(); } catch { /* ignore */ }
      cardElRef.current = null;
    };
  }, [adding, stripeMode, cfg?.publishable_key]);

  async function addCard() {
    setErr("");
    setBusy(true);
    try {
      if (stripeMode) {
        const stripe = stripeRef.current;
        if (!stripe || !cardElRef.current) { setErr("Card form isn't ready yet."); return; }
        const { paymentMethod, error } = await stripe.createPaymentMethod({
          type: "card", card: cardElRef.current,
          billing_details: { name: form.holder_name || undefined },
        });
        if (error) { setErr(error.message || "That card was declined."); return; }
        await api.post("/billing/payment-methods", {
          payment_method_token: paymentMethod.id, holder_name: form.holder_name,
          billing_address_id: form.billing_address_id || null, make_default: methods.length === 0,
        });
      } else {
        const [mm, yy] = form.exp.split("/").map((s) => s.trim());
        const exp_month = parseInt(mm || "", 10);
        let exp_year = parseInt(yy || "", 10);
        if (exp_year && exp_year < 100) exp_year += 2000;
        if (!exp_month || !exp_year) { setErr("Enter the expiry as MM/YY."); return; }
        await api.post("/billing/payment-methods", {
          number: form.number, exp_month, exp_year, cvc: form.cvc,
          holder_name: form.holder_name, billing_address_id: form.billing_address_id || null,
          make_default: methods.length === 0,
        });
      }
      setForm({ number: "", exp: "", cvc: "", holder_name: "", billing_address_id: "" });
      setAdding(false);
      await load();
    } catch (e: any) { setErr(e.message || "Could not add card"); }
    finally { setBusy(false); }
  }
  async function makeDefault(id: string) {
    try { await api.put(`/billing/payment-methods/${id}/default`, {}); await load(); }
    catch (e: any) { notify({ message: e.message, tone: "danger" }); }
  }
  async function removeCard(pm: PaymentMethodView) {
    if (!(await confirmDialog({ title: "Remove card?", message: `Delete the ${BRAND_LABEL[pm.brand] || "card"} ending ${pm.last4}?`, tone: "danger", confirmLabel: "Delete" }))) return;
    try { await api.del(`/billing/payment-methods/${pm.id}`); await load(); }
    catch (e: any) { notify({ message: e.message, tone: "danger" }); }
  }

  const billingAddrs = addrs.filter((a) => a.kind === "billing");

  return (
    <Card>
      <div className="spread" style={{ marginBottom: 10 }}>
        <div>
          <h2 style={{ margin: 0 }}>Billing</h2>
          <div className="muted" style={{ fontSize: 12.5 }}>
            {cfg?.configured
              ? `Payments are processed securely by ${cfg.name || (cfg.processor === "paypal" ? "PayPal" : "Stripe")}. Card numbers are never stored by Arkive.`
              : "Manage the cards used for your Arkive subscription and appliance orders."}
          </div>
        </div>
        {cfg?.processor && <Pill tone="info">{cfg.processor === "paypal" ? "PayPal" : "Stripe"}</Pill>}
      </div>

      {(sub?.profile || sub?.quote) && (() => {
        const amount = sub.profile?.amount_cents ?? sub.quote?.amount_cents ?? 0;
        const cur = sub.profile?.currency ?? sub.quote?.currency ?? "USD";
        const planName = sub.profile?.plan_name || sub.quote?.plan_name || "Plan";
        const interval = sub.profile?.interval || "month";
        return (
          <div className="result-row" style={{ background: "var(--inset)", borderRadius: 10, marginBottom: 6 }}>
            <div className="result-icon" style={{ background: "var(--inset)" }}><Icon name="repeat" size={16} /></div>
            <div className="flex1">
              <div style={{ fontWeight: 600 }}>{fmtCents(amount, cur)} <span className="faint" style={{ fontWeight: 400, fontSize: 12 }}>/ {interval}</span> · {planName}</div>
              <div className="faint" style={{ fontSize: 12 }}>
                {sub.profile
                  ? (sub.profile.active
                    ? `Billed monthly${sub.profile.next_charge_at ? ` — next charge ${new Date(sub.profile.next_charge_at + (/[zZ]|[+-]\d\d:?\d\d$/.test(sub.profile.next_charge_at) ? "" : "Z")).toLocaleDateString()}` : ""}`
                    : "Card on file — recurring billing will begin once activated")
                  : "Your plan's recurring amount. Add a card to set up billing."}
              </div>
            </div>
            {sub.profile && <Pill tone={sub.profile.active ? "ok" : "warn"}>{sub.profile.active ? "Active" : "Inactive"}</Pill>}
          </div>
        );
      })()}

      {methods.length === 0 && !adding && (
        <div className="muted" style={{ fontSize: 12.5, padding: "8px 0" }}>No payment methods on file.</div>
      )}
      {methods.map((pm) => (
        <div key={pm.id} className="result-row">
          <div className="result-icon" style={{ background: "var(--inset)" }}><Icon name="credit-card" size={16} /></div>
          <div className="flex1">
            <div className="row" style={{ gap: 8, alignItems: "center" }}>
              <span style={{ fontWeight: 600 }}>{BRAND_LABEL[pm.brand] || "Card"} •••• {pm.last4}</span>
              {pm.is_default && <Pill tone="ok">Default</Pill>}
            </div>
            <div className="faint" style={{ fontSize: 12 }}>
              Expires {String(pm.exp_month).padStart(2, "0")}/{pm.exp_year}{pm.holder_name ? ` · ${pm.holder_name}` : ""}
            </div>
          </div>
          {!pm.is_default && <button className="btn ghost sm" onClick={() => void makeDefault(pm.id)}>Make default</button>}
          <button className="btn ghost sm" onClick={() => void removeCard(pm)}><Icon name="trash" size={14} /></button>
        </div>
      ))}

      {!adding ? (
        <div style={{ marginTop: 12 }}>
          <button className="btn sm" onClick={() => setAdding(true)}><Icon name="credit-card" size={14} /> Add payment method</button>
        </div>
      ) : (
        <div className="card" style={{ marginTop: 12, background: "var(--inset)" }}>
          <h3 style={{ marginTop: 0, marginBottom: 10 }}>Add a card</h3>
          <div className="grid grid-2" style={{ gap: 10 }}>
            {stripeMode ? (
              <label className="stack" style={{ gap: 5, gridColumn: "1 / -1" }}>
                <span className="faint" style={{ fontSize: 12 }}>Card details</span>
                <div ref={cardMountRef} className="input" style={{ padding: "11px 12px" }} />
              </label>
            ) : (
              <>
                <label className="stack" style={{ gap: 5, gridColumn: "1 / -1" }}>
                  <span className="faint" style={{ fontSize: 12 }}>Card number</span>
                  <input className="input" inputMode="numeric" autoComplete="cc-number" placeholder="1234 1234 1234 1234"
                         value={form.number} onChange={(e) => setForm({ ...form, number: e.target.value })} />
                </label>
                <SettingsField label="Expiry (MM/YY)" value={form.exp} onChange={(v) => setForm({ ...form, exp: v })} placeholder="MM/YY" />
                <SettingsField label="CVC" value={form.cvc} onChange={(v) => setForm({ ...form, cvc: v })} placeholder="123" />
              </>
            )}
            <SettingsField label="Cardholder name" value={form.holder_name} onChange={(v) => setForm({ ...form, holder_name: v })} placeholder="Name on card" />
            <label className="stack" style={{ gap: 5 }}>
              <span className="faint" style={{ fontSize: 12 }}>Billing address</span>
              <select className="input" value={form.billing_address_id} onChange={(e) => setForm({ ...form, billing_address_id: e.target.value })}>
                <option value="">— none —</option>
                {billingAddrs.map((a) => (
                  <option key={a.id} value={a.id}>{[a.label || a.name, a.line1, a.city].filter(Boolean).join(", ")}</option>
                ))}
              </select>
            </label>
          </div>
          {err && <div style={{ color: "var(--danger, #ff5d5d)", fontSize: 12.5, marginTop: 10 }}><Icon name="alert" size={13} /> {err}</div>}
          <div className="row" style={{ gap: 8, marginTop: 12 }}>
            <button className="btn primary sm" disabled={busy} onClick={addCard}>{busy ? "Saving…" : "Save card"}</button>
            <button className="btn ghost sm" onClick={() => { setAdding(false); setErr(""); }}>Cancel</button>
          </div>
          <div className="faint" style={{ fontSize: 11, marginTop: 10 }}>
            <Icon name="lock" size={11} /> {stripeMode
              ? "Your card is entered directly into Stripe and tokenized in your browser — the number never reaches Arkive's servers. We store only the brand, last four digits and expiry."
              : "Card details are tokenized by your payment processor — Arkive stores only the brand, last four digits and expiry."}
          </div>
        </div>
      )}
    </Card>
  );
}

function Row({ label, value }: { label: string; value?: string }) {
  return (
    <div className="spread" style={{ padding: "9px 0", borderBottom: "1px solid var(--border-soft)" }}>
      <span className="faint" style={{ fontSize: 12.5 }}>{label}</span>
      <span style={{ fontWeight: 600 }}>{value ?? "—"}</span>
    </div>
  );
}

interface NotifType { key: string; label: string; icon: string; desc: string; }

function NotificationSettings() {
  const [types, setTypes] = useState<NotifType[]>([]);
  const [prefs, setPrefs] = useState<Record<string, boolean>>({});
  const [emails, setEmails] = useState<string[]>([]);
  const [maxEmails, setMaxEmails] = useState(5);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    api.get<{ types: NotifType[]; prefs: Record<string, boolean>; emails: string[]; max_emails: number }>("/me/notifications")
      .then((r) => { setTypes(r.types || []); setPrefs(r.prefs || {}); setEmails(r.emails || []); setMaxEmails(r.max_emails || 5); })
      .catch(() => {}).finally(() => setLoaded(true));
  }, []);

  async function toggle(key: string) {
    const prev = prefs;
    const next = { ...prefs, [key]: !prefs[key] };
    setPrefs(next);
    try { await api.put("/me/notifications", { prefs: { [key]: next[key] } }); }
    catch (e: any) { notify({ message: e.message || "Could not save", tone: "danger" }); setPrefs(prev); }
  }

  async function saveEmails(next: string[]) {
    const prev = emails;
    setEmails(next);
    try {
      const r = await api.put<{ emails: string[] }>("/me/notification-emails", { emails: next });
      setEmails(r.emails || []);
    } catch (e: any) {
      notify({ message: e.message || "Could not save", tone: "danger" });
      setEmails(prev);
    }
  }

  async function addEmail() {
    const r = await formDialog({
      title: "Add a notification email",
      message: "This address will also receive your Arkive email notifications. It's never used to sign in, and it can be the same as another account's login email.",
      confirmLabel: "Add email",
      fields: [{ name: "email", label: "Email address", required: true, placeholder: "you@example.com" }],
    });
    if (!r) return;
    const addr = String(r.email || "").trim().toLowerCase();
    const ok = /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(addr);
    if (!ok) { notify({ message: "Enter a valid email address.", tone: "warn" }); return; }
    if (emails.includes(addr)) { notify({ message: "That address is already on the list.", tone: "warn" }); return; }
    await saveEmails([...emails, addr]);
  }

  function removeEmail(addr: string) { void saveEmails(emails.filter((e) => e !== addr)); }

  if (!loaded || types.length === 0) return null;
  return (
    <Card style={{ marginBottom: 16 }}>
      <h2 style={{ marginBottom: 4 }}>Email notifications</h2>
      <div className="muted" style={{ fontSize: 12.5, marginBottom: 6 }}>
        Choose which emails Arkive sends you. You can change these any time.
      </div>
      <div className="stack" style={{ gap: 0 }}>
        {types.map((t) => (
          <div key={t.key} className="spread"
               style={{ padding: "13px 0", borderTop: "1px solid var(--border-soft)", alignItems: "center", gap: 14 }}>
            <div className="row" style={{ gap: 12, alignItems: "flex-start", minWidth: 0 }}>
              <div className="result-icon" style={{ width: 34, height: 34, background: "var(--inset)", color: "var(--brand)", flexShrink: 0 }}>
                <Icon name={t.icon as IconName} size={17} />
              </div>
              <div style={{ minWidth: 0 }}>
                <div style={{ fontWeight: 600, fontSize: 14 }}>{t.label}</div>
                <div className="faint" style={{ fontSize: 12.5 }}>{t.desc}</div>
              </div>
            </div>
            <Toggle on={!!prefs[t.key]} onClick={() => toggle(t.key)} />
          </div>
        ))}
      </div>

      <div style={{ borderTop: "1px solid var(--border-soft)", marginTop: 8, paddingTop: 14 }}>
        <div className="spread" style={{ alignItems: "center", marginBottom: 4 }}>
          <h3 style={{ margin: 0, fontSize: 15 }}>Additional notification emails</h3>
          <button className="btn sm" disabled={emails.length >= maxEmails} onClick={addEmail}>
            <Icon name="mail" size={13} /> Add email
          </button>
        </div>
        <div className="muted" style={{ fontSize: 12.5, marginBottom: 10 }}>
          Copy your notifications to another address (a backup or family email). These addresses only receive
          notifications — they're never used to sign in.
        </div>
        {emails.length === 0 ? (
          <div className="faint" style={{ fontSize: 12.5 }}>No additional emails. Your notifications go to your login email only.</div>
        ) : (
          <div className="stack" style={{ gap: 8 }}>
            {emails.map((addr) => (
              <div key={addr} className="spread" style={{ alignItems: "center", padding: "9px 12px", background: "var(--inset)", borderRadius: 10 }}>
                <span className="row" style={{ gap: 9, minWidth: 0, alignItems: "center" }}>
                  <Icon name="mail" size={15} />
                  <span style={{ fontWeight: 600, fontSize: 13.5, wordBreak: "break-all" }}>{addr}</span>
                </span>
                <button className="btn ghost sm" title="Remove" onClick={() => removeEmail(addr)}><Icon name="trash" size={13} /></button>
              </div>
            ))}
          </div>
        )}
        {emails.length >= maxEmails && (
          <div className="faint" style={{ fontSize: 11.5, marginTop: 8 }}>You've reached the maximum of {maxEmails} additional emails.</div>
        )}
      </div>
    </Card>
  );
}

function ContactLinkingSettings() {
  const [enabled, setEnabled] = useState<boolean | null>(null);
  useEffect(() => {
    api.get<{ enabled: boolean }>("/me/contact-linking").then((r) => setEnabled(!!r.enabled)).catch(() => setEnabled(false));
  }, []);
  async function toggle() {
    const next = !enabled;
    setEnabled(next);
    try { await api.put("/me/contact-linking", { enabled: next }); }
    catch (e: any) { notify({ message: e.message || "Could not save", tone: "danger" }); setEnabled(!next); }
  }
  if (enabled === null) return null;
  return (
    <Card style={{ marginBottom: 16 }}>
      <div className="spread" style={{ alignItems: "flex-start", gap: 16 }}>
        <div style={{ minWidth: 0 }}>
          <h2 style={{ marginBottom: 4 }}>Contact linking</h2>
          <div className="muted" style={{ fontSize: 12.5 }}>
            Match phone numbers and email addresses in your messages to the people in your contacts, so a text
            from a number shows the contact's name. Numbers are normalized (<span className="mono">+12015771404</span>,
            <span className="mono"> 2015771404</span> and <span className="mono">201-577-1404</span> all match). Built privately in
            the background from your own contact sources and shown as linked from your contacts.
          </div>
        </div>
        <Toggle on={enabled} onClick={toggle} />
      </div>
    </Card>
  );
}

export function Toggle({ on, onClick }: { on: boolean; onClick: () => void }) {
  return (
    <button onClick={onClick} aria-pressed={on} title={on ? "On" : "Off"}
      style={{ width: 42, height: 24, borderRadius: 999, border: "none", cursor: "pointer", flexShrink: 0,
               background: on ? "var(--brand)" : "var(--border)", position: "relative", transition: "background .15s" }}>
      <span style={{ position: "absolute", top: 3, left: on ? 21 : 3, width: 18, height: 18, borderRadius: "50%",
                     background: "#fff", transition: "left .15s", boxShadow: "0 1px 2px rgba(0,0,0,.3)" }} />
    </button>
  );
}
