import { ReactNode, useEffect, useState } from "react";
import { api } from "../api";
import { Card, Pill, Stat, timeAgo } from "../components/ui";
import { Icon } from "../components/Icon";
import { promptDialog } from "../components/dialog";

type Tab = "overview" | "tenants" | "fleet" | "pricing" | "email" | "crypto" | "audit" | "updates";

export default function Admin() {
  const [tab, setTab] = useState<Tab>("overview");
  return (
    <>
      <div className="chips" style={{ marginBottom: 18 }}>
        {(["overview", "tenants", "fleet", "pricing", "email", "crypto", "audit", "updates"] as Tab[]).map((t) => (
          <span key={t} className={`chip ${tab === t ? "active" : ""}`} onClick={() => setTab(t)}>
            {t[0].toUpperCase() + t.slice(1)}
          </span>
        ))}
      </div>
      {tab === "overview" && <Overview />}
      {tab === "tenants" && <Tenants />}
      {tab === "fleet" && <Fleet />}
      {tab === "pricing" && <Pricing />}
      {tab === "email" && <EmailAdmin />}
      {tab === "crypto" && <Crypto />}
      {tab === "audit" && <Audit />}
      {tab === "updates" && <Updates />}
    </>
  );
}

function Overview() {
  const [o, setO] = useState<any>(null);
  useEffect(() => { api.get("/admin/overview").then(setO).catch(() => {}); }, []);
  if (!o) return <Card><div className="muted">Loading…</div></Card>;
  return (
    <>
      <div className="grid grid-4">
        <Stat label="Tenants" value={o.tenants} />
        <Stat label="Users" value={o.users} />
        <Stat label="Appliances" value={o.appliances} />
        <Stat label="Linked sources" value={o.connectors} />
      </div>
      <div className="grid grid-3" style={{ marginTop: 16 }}>
        <Stat label="Recovery points" value={o.snapshots} hint={`${o.recoverable_snapshots} recoverable`} />
        <Card className="stat">
          <div className="value">{o.pq_available ? "Active" : "Fallback"}</div>
          <div className="label">Post-quantum crypto</div>
          <Pill tone={o.pq_available ? "ok" : "warn"}>{o.pq_available ? "liboqs ML-KEM/ML-DSA" : "dev fallback"}</Pill>
        </Card>
        <Card className="stat">
          <div className="value">{o.audit_chain_valid ? "Valid" : "Broken"}</div>
          <div className="label">Audit ledger integrity</div>
          <Pill tone={o.audit_chain_valid ? "ok" : "danger"}>hash-chained</Pill>
        </Card>
      </div>
    </>
  );
}

function Tenants() {
  const [rows, setRows] = useState<any[]>([]);
  useEffect(() => { api.get<any[]>("/admin/tenants").then(setRows).catch(() => {}); }, []);
  return (
    <Card>
      <table className="table">
        <thead><tr><th>Tenant</th><th>Plan</th><th>Key model</th><th>Users</th><th>Appliances</th><th>Status</th></tr></thead>
        <tbody>
          {rows.map((t) => (
            <tr key={t.id}>
              <td style={{ fontWeight: 600 }}>{t.name}</td>
              <td><Pill tone="info">{t.plan}</Pill></td>
              <td className="faint">{t.key_ownership_model}</td>
              <td>{t.users}</td><td>{t.appliances}</td>
              <td><Pill tone="ok">{t.status}</Pill></td>
            </tr>
          ))}
        </tbody>
      </table>
    </Card>
  );
}

function Fleet() {
  const [rows, setRows] = useState<any[]>([]);
  useEffect(() => { api.get<any[]>("/admin/fleet").then(setRows).catch(() => {}); }, []);
  return (
    <Card>
      <table className="table">
        <thead><tr><th>Serial</th><th>Model</th><th>State</th><th>Isolation</th><th>Attestation</th><th>Version</th><th>Heartbeat</th></tr></thead>
        <tbody>
          {rows.map((a) => (
            <tr key={a.id}>
              <td className="mono">{a.serial}</td><td>{a.model}</td>
              <td><Pill tone="info">{a.state}</Pill></td>
              <td className="faint">{a.isolation_state}</td>
              <td>{a.attestation_ok ? <Pill tone="ok">ok</Pill> : <Pill tone="danger">failed</Pill>}</td>
              <td>{a.software_version}</td>
              <td className="faint">{timeAgo(a.last_heartbeat_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length === 0 && <div className="muted">No appliances in fleet.</div>}
    </Card>
  );
}

function Crypto() {
  const [d, setD] = useState<any>(null);
  useEffect(() => { api.get("/admin/crypto-profiles").then(setD).catch(() => {}); }, []);
  if (!d) return <Card><div className="muted">Loading…</div></Card>;
  return (
    <Card>
      <div className="spread" style={{ marginBottom: 12 }}>
        <h2>Cryptographic profile registry</h2>
        <Pill tone={d.pq_available ? "ok" : "warn"}>{d.pq_available ? "PQC active" : "fallback"}</Pill>
      </div>
      <table className="table">
        <thead><tr><th>Profile</th><th>Content</th><th>KEM</th><th>Signature</th><th>Hash</th><th>Status</th></tr></thead>
        <tbody>
          {d.profiles.map((p: any) => (
            <tr key={p.profile_id}>
              <td className="mono">{p.profile_id}</td>
              <td>{p.content_algo}</td>
              <td className="faint">{p.kem_classical} + {p.kem_pq}</td>
              <td className="faint">{p.sig_classical} + {p.sig_pq}</td>
              <td>{p.hash_algo}</td>
              <td><Pill tone={p.status === "preferred" ? "ok" : "info"}>{p.status}</Pill></td>
            </tr>
          ))}
        </tbody>
      </table>
    </Card>
  );
}

function Audit() {
  const [d, setD] = useState<any>(null);
  useEffect(() => { api.get("/admin/audit").then(setD).catch(() => {}); }, []);
  if (!d) return <Card><div className="muted">Loading…</div></Card>;
  return (
    <Card>
      <div className="spread" style={{ marginBottom: 12 }}>
        <h2>Tamper-evident audit ledger</h2>
        <Pill tone={d.chain_valid ? "ok" : "danger"}>{d.chain_valid ? "Chain valid" : "Chain broken"}</Pill>
      </div>
      <table className="table">
        <thead><tr><th>Time</th><th>Actor</th><th>Action</th><th>Resource</th><th>Hash</th></tr></thead>
        <tbody>
          {d.events.map((e: any, i: number) => (
            <tr key={i}>
              <td className="faint">{timeAgo(e.created_at)}</td>
              <td>{e.actor}</td>
              <td><Pill tone="info">{e.action}</Pill></td>
              <td className="mono faint">{(e.resource || "").slice(0, 10)}</td>
              <td className="mono faint">{e.entry_hash}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Card>
  );
}

function Updates() {
  const [releases, setReleases] = useState<any[]>([]);
  const [fleet, setFleet] = useState<any[]>([]);
  const [toast, setToast] = useState("");

  async function load() {
    setReleases(await api.get<any[]>("/updates/releases"));
    setFleet(await api.get<any[]>("/admin/fleet"));
  }
  useEffect(() => { void load(); }, []);

  async function publish(component: string) {
    const version = await promptDialog({
      title: `Publish ${component} release`,
      label: "New version",
      defaultValue: "1.0.1",
      placeholder: "1.0.1",
      confirmLabel: "Publish",
    });
    if (!version) return;
    await api.post("/updates/releases", {
      component, version,
      package_url: `https://vault.arkive.life/releases/${component}-${version}.tar.gz`,
      package_hash: `sha384:${version}-placeholder`,
      security_floor: "1.0.0",
    });
    setToast(`Published ${component} ${version}`);
    await load();
    setTimeout(() => setToast(""), 2500);
  }

  async function trigger(r: any, target_type: string, target_id?: string) {
    await api.post("/updates/trigger", { release_id: r.id, target_type, target_id });
    setToast(`Update ${r.version} triggered for ${target_type}`);
    setTimeout(() => setToast(""), 2500);
  }

  return (
    <>
      <Card style={{ marginBottom: 16 }}>
        <div className="spread" style={{ marginBottom: 12 }}>
          <h2>Software releases</h2>
          <div className="row">
            <button className="btn sm" onClick={() => publish("cloud")}>Publish cloud</button>
            <button className="btn sm" onClick={() => publish("appliance")}>Publish appliance</button>
          </div>
        </div>
        <table className="table">
          <thead><tr><th>Component</th><th>Version</th><th>Hash</th><th>Trigger</th></tr></thead>
          <tbody>
            {releases.map((r) => (
              <tr key={r.id}>
                <td><Pill tone="info">{r.component}</Pill></td>
                <td className="mono">{r.version}</td>
                <td className="mono faint">{r.package_hash.slice(0, 16)}…</td>
                <td>
                  {r.component === "cloud" ? (
                    <button className="btn sm" onClick={() => trigger(r, "cloud")}>Roll out to cloud</button>
                  ) : (
                    <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
                      {fleet.map((a) => (
                        <button key={a.id} className="btn sm" onClick={() => trigger(r, "appliance", a.id)}>
                          {a.serial.slice(0, 8)}
                        </button>
                      ))}
                    </div>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {releases.length === 0 && <div className="muted">No releases published.</div>}
      </Card>
      <div className="muted" style={{ fontSize: 12.5 }}>
        Cloud updates are picked up by the cloud updater script; appliance updates are delivered
        as signed STAGE_UPDATE commands over the management plane with rollback protection.
      </div>
      {toast && <div className="toast"><Icon name="check" size={15} /> {toast}</div>}
    </>
  );
}

interface PricingCfg {
  currency: string;
  protection_price_per_tb_month: number;
  cloud_price_per_tb_month: number;
  s3_price_per_tb_month: number;
  azure_price_per_tb_month: number;
  appliance_tiers: { capacity_tb: number; monthly: number; setup: number; model: string }[];
  data_value_per_type: Record<string, number>;
}

function Pricing() {
  const [p, setP] = useState<PricingCfg | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => { api.get<PricingCfg>("/admin/pricing").then(setP).catch(() => {}); }, []);
  function set<K extends keyof PricingCfg>(k: K, v: PricingCfg[K]) { setSaved(false); setP((c) => c ? { ...c, [k]: v } : c); }
  async function save() {
    if (!p) return;
    try { const r = await api.put<PricingCfg>("/admin/pricing", p); setP(r); setSaved(true); } catch { /* ignore */ }
  }
  if (!p) return <Card><div className="muted">Loading pricing…</div></Card>;

  const num = (v: string) => Number(v) || 0;
  const valueKeys = ["email", "credential", "document", "photo", "media", "file", "contact"];

  return (
    <>
      <Card style={{ marginBottom: 16 }}>
        <h3 style={{ marginTop: 0 }}>Recurring pricing (per TB · month)</h3>
        <div className="grid grid-4" style={{ gap: 12 }}>
          <PriceField label="Data protection" value={p.protection_price_per_tb_month} onChange={(v) => set("protection_price_per_tb_month", num(v))} />
          <PriceField label="Arkive Cloud storage" value={p.cloud_price_per_tb_month} onChange={(v) => set("cloud_price_per_tb_month", num(v))} />
          <PriceField label="AWS S3 estimate" value={p.s3_price_per_tb_month} onChange={(v) => set("s3_price_per_tb_month", num(v))} />
          <PriceField label="Azure estimate" value={p.azure_price_per_tb_month} onChange={(v) => set("azure_price_per_tb_month", num(v))} />
        </div>
      </Card>

      <Card style={{ marginBottom: 16 }}>
        <h3 style={{ marginTop: 0 }}>Appliance hardware (leased)</h3>
        <div className="muted" style={{ fontSize: 12.5, marginBottom: 12 }}>Monthly lease fee plus a one-time setup fee per appliance.</div>
        <table className="table">
          <thead><tr><th>Model</th><th>Capacity (TB)</th><th>Monthly ($)</th><th>Setup ($)</th></tr></thead>
          <tbody>
            {p.appliance_tiers.map((t, i) => (
              <tr key={i}>
                <td><input className="input sm" value={t.model} onChange={(e) => { const a = [...p.appliance_tiers]; a[i] = { ...t, model: e.target.value }; set("appliance_tiers", a); }} /></td>
                <td><input className="input sm" type="number" value={t.capacity_tb} onChange={(e) => { const a = [...p.appliance_tiers]; a[i] = { ...t, capacity_tb: num(e.target.value) }; set("appliance_tiers", a); }} style={{ width: 90 }} /></td>
                <td><input className="input sm" type="number" value={t.monthly} onChange={(e) => { const a = [...p.appliance_tiers]; a[i] = { ...t, monthly: num(e.target.value) }; set("appliance_tiers", a); }} style={{ width: 110 }} /></td>
                <td><input className="input sm" type="number" value={t.setup} onChange={(e) => { const a = [...p.appliance_tiers]; a[i] = { ...t, setup: num(e.target.value) }; set("appliance_tiers", a); }} style={{ width: 110 }} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>

      <Card style={{ marginBottom: 16 }}>
        <h3 style={{ marginTop: 0 }}>Estimated value per object ($)</h3>
        <div className="muted" style={{ fontSize: 12.5, marginBottom: 12 }}>Drives the customer's data-value / cost-benefit estimate.</div>
        <div className="grid grid-4" style={{ gap: 12 }}>
          {valueKeys.map((k) => (
            <PriceField key={k} label={k[0].toUpperCase() + k.slice(1)} value={p.data_value_per_type[k] ?? 0}
                        onChange={(v) => set("data_value_per_type", { ...p.data_value_per_type, [k]: num(v) })} />
          ))}
        </div>
      </Card>

      <div className="row">
        <button className="btn primary" onClick={save}>{saved ? "Saved ✓" : "Save pricing"}</button>
      </div>
    </>
  );
}

function PriceField({ label, value, onChange }: { label: string; value: number; onChange: (v: string) => void }) {
  return (
    <div className="stack" style={{ gap: 4 }}>
      <label className="faint" style={{ fontSize: 12 }}>{label}</label>
      <input className="input sm" type="number" step="0.01" value={value} onChange={(e) => onChange(e.target.value)} />
    </div>
  );
}

interface EmailCfg {
  provider: string; enabled: boolean; from_email: string; from_name: string;
  reply_to: string; region: string;
}
interface AdminUser { id: string; email: string; display_name: string; role: string; status: string; tenant_id: string; tenant_name: string; }

function EmailAdmin() {
  const [cfg, setCfg] = useState<EmailCfg | null>(null);
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [toast, setToast] = useState("");
  const [testTo, setTestTo] = useState("");

  // Broadcast composer
  const [audience, setAudience] = useState<"all" | "selected">("all");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [subject, setSubject] = useState("");
  const [message, setMessage] = useState("");
  const [ctaLabel, setCtaLabel] = useState("");
  const [ctaUrl, setCtaUrl] = useState("");
  const [sending, setSending] = useState(false);

  useEffect(() => {
    api.get<EmailCfg>("/admin/email-config").then(setCfg).catch(() => {});
    api.get<AdminUser[]>("/admin/users").then(setUsers).catch(() => {});
  }, []);
  function flash(m: string) { setToast(m); setTimeout(() => setToast(""), 3500); }
  function setC<K extends keyof EmailCfg>(k: K, v: EmailCfg[K]) { setCfg((c) => c ? { ...c, [k]: v } : c); }

  async function saveCfg() {
    if (!cfg) return;
    try { const r = await api.put<EmailCfg>("/admin/email-config", cfg); setCfg(r); flash("Email settings saved"); }
    catch { flash("Could not save settings"); }
  }
  async function sendTest() {
    if (!testTo.trim()) return;
    try { const r = await api.post<{ channel: string }>("/admin/email-test", { to: testTo.trim() }); flash(`Test sent via ${r.channel}`); }
    catch { flash("Test failed"); }
  }
  function toggleUser(id: string) {
    setSelected((cur) => { const n = new Set(cur); if (n.has(id)) n.delete(id); else n.add(id); return n; });
  }
  const recipientCount = audience === "all" ? users.filter((u) => u.status === "active").length : selected.size;
  async function broadcast() {
    if (!subject.trim() || !message.trim() || recipientCount === 0) return;
    setSending(true);
    try {
      const r = await api.post<{ sent: number; failed: number; recipients: number; channel: string }>("/admin/email-broadcast", {
        audience, user_ids: [...selected], subject, message,
        cta_label: ctaLabel || undefined, cta_url: ctaUrl || undefined,
      });
      flash(`Sent to ${r.sent} of ${r.recipients} (via ${r.channel}${r.failed ? `, ${r.failed} failed` : ""})`);
      setSubject(""); setMessage(""); setCtaLabel(""); setCtaUrl("");
    } catch { flash("Broadcast failed"); }
    setSending(false);
  }

  if (!cfg) return <Card><div className="muted">Loading email settings…</div></Card>;

  return (
    <>
      <Card style={{ marginBottom: 16 }}>
        <div className="spread" style={{ marginBottom: 12 }}>
          <h3 style={{ margin: 0 }}>Email delivery (AWS SES)</h3>
          <Pill tone={cfg.enabled ? "ok" : "warn"}>{cfg.enabled ? "Enabled" : "Disabled"}</Pill>
        </div>
        <div className="grid grid-3" style={{ gap: 12 }}>
          <Field label="Provider">
            <select className="input sm" value={cfg.provider} onChange={(e) => setC("provider", e.target.value)}>
              <option value="ses">AWS SES</option>
              <option value="smtp">SMTP</option>
              <option value="log">Log only (dev)</option>
            </select>
          </Field>
          <Field label="From name"><input className="input sm" value={cfg.from_name} onChange={(e) => setC("from_name", e.target.value)} /></Field>
          <Field label="From email"><input className="input sm" value={cfg.from_email} onChange={(e) => setC("from_email", e.target.value)} placeholder="notifications@arkive.life" /></Field>
          <Field label="Reply-to"><input className="input sm" value={cfg.reply_to} onChange={(e) => setC("reply_to", e.target.value)} placeholder="support@arkive.life" /></Field>
          <Field label="AWS region"><input className="input sm" value={cfg.region} onChange={(e) => setC("region", e.target.value)} placeholder="us-east-1" /></Field>
          <Field label="Enabled">
            <label className="row" style={{ gap: 8, fontSize: 13 }}>
              <input type="checkbox" checked={cfg.enabled} onChange={(e) => setC("enabled", e.target.checked)} /> Send live emails
            </label>
          </Field>
        </div>
        <div className="row" style={{ gap: 10, marginTop: 14, flexWrap: "wrap" }}>
          <button className="btn primary" onClick={saveCfg}>Save settings</button>
          <input className="input sm" placeholder="you@example.com" value={testTo} onChange={(e) => setTestTo(e.target.value)} style={{ width: 220 }} />
          <button className="btn" onClick={sendTest}>Send test</button>
        </div>
        <div className="muted" style={{ fontSize: 12, marginTop: 10 }}>
          Credentials come from the platform AWS identity (IAM role / env) — never stored here.
          Verify the <b>arkive.life</b> domain in SES and publish SPF, DKIM & DMARC records for deliverability.
        </div>
      </Card>

      <Card>
        <h3 style={{ marginTop: 0 }}>Send a message</h3>
        <div className="muted" style={{ fontSize: 12.5, marginBottom: 14 }}>Email all users or a selected group. Each recipient is sent an individual, branded message.</div>
        <div className="row" style={{ gap: 8, marginBottom: 12 }}>
          {(["all", "selected"] as const).map((a) => (
            <span key={a} className={`chip ${audience === a ? "active" : ""}`} onClick={() => setAudience(a)}>
              {a === "all" ? "All users" : "Selected users"}
            </span>
          ))}
          <span className="faint" style={{ fontSize: 12, alignSelf: "center" }}>{recipientCount} recipient{recipientCount === 1 ? "" : "s"}</span>
        </div>

        {audience === "selected" && (
          <div style={{ maxHeight: 200, overflow: "auto", border: "1px solid var(--border-soft)", borderRadius: 8, padding: 8, marginBottom: 12 }}>
            {users.map((u) => (
              <label key={u.id} className="row" style={{ gap: 8, padding: "4px 2px", fontSize: 12.5, cursor: "pointer" }}>
                <input type="checkbox" checked={selected.has(u.id)} onChange={() => toggleUser(u.id)} />
                <span style={{ fontWeight: 600 }}>{u.display_name || u.email}</span>
                <span className="faint">{u.email} · {u.tenant_name}</span>
              </label>
            ))}
            {users.length === 0 && <div className="muted">No users.</div>}
          </div>
        )}

        <div className="stack" style={{ gap: 10 }}>
          <Field label="Subject"><input className="input sm" value={subject} onChange={(e) => setSubject(e.target.value)} placeholder="Subject line" /></Field>
          <Field label="Message">
            <textarea className="input" value={message} onChange={(e) => setMessage(e.target.value)} rows={6}
                      placeholder="Write your message. Blank lines separate paragraphs." style={{ resize: "vertical" }} />
          </Field>
          <div className="grid grid-2" style={{ gap: 10 }}>
            <Field label="Button label (optional)"><input className="input sm" value={ctaLabel} onChange={(e) => setCtaLabel(e.target.value)} placeholder="Open Arkive" /></Field>
            <Field label="Button link (optional)"><input className="input sm" value={ctaUrl} onChange={(e) => setCtaUrl(e.target.value)} placeholder="https://vault.arkive.life" /></Field>
          </div>
        </div>
        <div className="row" style={{ marginTop: 14 }}>
          <button className="btn primary" onClick={broadcast} disabled={sending || !subject.trim() || !message.trim() || recipientCount === 0}>
            {sending ? "Sending…" : `Send to ${recipientCount} recipient${recipientCount === 1 ? "" : "s"}`}
          </button>
        </div>
      </Card>

      {toast && <div className="toast"><Icon name="check" size={15} /> {toast}</div>}
    </>
  );
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="stack" style={{ gap: 4 }}>
      <label className="faint" style={{ fontSize: 12 }}>{label}</label>
      {children}
    </div>
  );
}
