import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { Card, Pill, bytes, Loading } from "../components/ui";
import { Icon, IconName } from "../components/Icon";
import { SourceIcon } from "../components/SourceIcon";
import { notify, confirmDialog } from "../components/dialog";

interface ProviderField { name: string; label: string; placeholder?: string; required?: boolean; secret?: boolean; options?: { value: string; label: string }[]; }
export interface ProviderSpec {
  provider: string; display_name: string; icon: string; color: string; help?: string[];
  config: ProviderField[]; write: ProviderField[]; read: ProviderField[]; provision?: ProviderField[];
}
interface StorageInstance {
  id: string; name: string; provider: string; provider_display: string; icon: string; color: string;
  config: Record<string, string>; enabled: boolean; status: string;
  provision_mode: string; provision_state: string; provision_message?: string | null;
  has_read_credential: boolean; used_bytes: number; recovery_points: number; object_count: number;
  last_test_at: string | null; last_test_ok: boolean; last_test_error?: string | null; created_at: string | null;
}
interface ListResp { providers: ProviderSpec[]; instances: StorageInstance[]; }
interface StorageSource {
  collection_id: string; name: string; source_type: string; recovery_points: number;
  objects: number; bytes: number; recoverable: number; last_at: string | null;
}
interface DataResp { storage: StorageInstance; sources: StorageSource[]; }
interface ArkiveCloud {
  enabled: boolean; used_bytes: number; recovery_points: number; object_count: number;
  source_count: number; last_backup_at: string | null; sources: StorageSource[];
}

const HEALTH: Record<string, { tone: "ok" | "warn" | "info" | "danger"; label: string; dot: string }> = {
  healthy: { tone: "ok", label: "Healthy", dot: "#2dbe60" },
  error: { tone: "danger", label: "Error", dot: "#f2545b" },
  degraded: { tone: "warn", label: "Degraded", dot: "#f5a623" },
  unknown: { tone: "info", label: "Not tested", dot: "#8a94a6" },
};
const fmtAgo = (s: string | null): string => {
  if (!s) return "never";
  const d = new Date(s.endsWith("Z") ? s : `${s}Z`).getTime();
  const secs = Math.max(1, Math.round((Date.now() - d) / 1000));
  if (secs < 60) return `${secs}s ago`;
  if (secs < 3600) return `${Math.round(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.round(secs / 3600)}h ago`;
  return `${Math.round(secs / 86400)}d ago`;
};
const health = (s: StorageInstance) => HEALTH[s.enabled ? (s.status || "unknown") : "unknown"] || HEALTH.unknown;

// Arkive's own app mark, swapped by theme via CSS (icon-dark on dark, icon-light on light).
function ArkiveMark({ size = 46 }: { size?: number }) {
  return (
    <>
      <img className="arkive-mark arkive-mark--dark" src="/logos/icon-dark.png" width={size} height={size} alt="Arkive" />
      <img className="arkive-mark arkive-mark--light" src="/logos/icon-light.png" width={size} height={size} alt="Arkive" />
    </>
  );
}

// Pre-fill dropdown fields with their first option so required selects aren't "".
function selectDefaults(fields: ProviderField[]): Record<string, string> {
  const d: Record<string, string> = {};
  for (const f of fields) if (f.options && f.options.length) d[f.name] = f.options[0].value;
  return d;
}

function FieldInput({ f, value, onChange, placeholder, showRequired = true }: {
  f: ProviderField; value: string; onChange: (v: string) => void; placeholder?: string; showRequired?: boolean;
}) {
  return (
    <label className="stack" style={{ marginBottom: 12 }}>
      <span className="faint" style={{ fontSize: 11.5 }}>{f.label}{f.required && showRequired ? " *" : ""}</span>
      {f.options ? (
        <select className="input" value={value} onChange={(e) => onChange(e.target.value)}>
          {!f.required && <option value="">—</option>}
          {f.options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
        </select>
      ) : (
        <input className="input" type={f.secret ? "password" : "text"}
               placeholder={placeholder ?? f.placeholder ?? ""}
               value={value} onChange={(e) => onChange(e.target.value)} />
      )}
    </label>
  );
}

export default function CloudStorage() {
  const [list, setList] = useState<ListResp | null>(null);
  const [arkive, setArkive] = useState<ArkiveCloud | null>(null);
  const [loading, setLoading] = useState(true);
  const [showAdd, setShowAdd] = useState(false);
  const [detailId, setDetailId] = useState<string | null>(null);
  const [arkiveDetail, setArkiveDetail] = useState(false);

  async function load() {
    try {
      const [l, a] = await Promise.all([
        api.get<ListResp>("/storage"),
        api.get<ArkiveCloud>("/storage/arkive-cloud").catch(() => null),
      ]);
      setList(l);
      setArkive(a);
    } catch { /* ignore */ }
    finally { setLoading(false); }
  }
  useEffect(() => {
    void load();
    const t = setInterval(() => { void load(); }, 6000);
    return () => clearInterval(t);
  }, []);

  if (loading) return <Loading label="Loading cloud storage…" />;

  const detail = detailId ? list?.instances.find((i) => i.id === detailId) : null;
  if (detailId && detail) {
    return <StorageDetail inst={detail} providers={list?.providers || []}
                          onBack={() => { setDetailId(null); void load(); }} onChanged={load} />;
  }
  if (arkiveDetail && arkive) {
    return <ArkiveCloudDetail data={arkive} onBack={() => { setArkiveDetail(false); void load(); }} />;
  }

  return (
    <>
      <div className="spread" style={{ marginBottom: 18, alignItems: "center", gap: 12, flexWrap: "wrap" }}>
        <div className="stack">
          <h2 style={{ margin: 0 }}>Cloud Storage</h2>
          <div className="faint" style={{ fontSize: 12.5, maxWidth: 640 }}>
            Where your protected data lives in the cloud — Arkive's fully-managed hosted service,
            plus any of your own AWS, Azure or Google Cloud buckets. Everything is encrypted with our
            quantum-safe cipher before it leaves Arkive, so providers only ever hold ciphertext.
          </div>
        </div>
      </div>

      {/* Arkive Cloud — our hosted, fully-managed tier. Read-only: usage only. */}
      <ArkiveCloudRow data={arkive} onOpen={() => setArkiveDetail(true)} />

      {/* Bring your own storage — customer-owned buckets with full controls. */}
      <div className="spread" style={{ margin: "24px 0 12px", alignItems: "flex-end", gap: 12, flexWrap: "wrap" }}>
        <div className="stack">
          <h3 style={{ margin: 0 }}>Bring your own storage</h3>
          <div className="faint" style={{ fontSize: 12.5, maxWidth: 560 }}>
            Add your own cloud bucket as a backup destination and route sources to it in the{" "}
            <a href="/mappings">Data Map</a>.
          </div>
        </div>
        <button className="btn primary" onClick={() => setShowAdd(true)}>
          <Icon name="link" size={15} /> Add cloud storage
        </button>
      </div>

      {list && list.instances.length > 0 ? (
        <div className="insights-cards" style={{ marginBottom: 20 }}>
          {list.instances.map((s) => (
            <StorageCard key={s.id} inst={s} onOpen={() => setDetailId(s.id)} />
          ))}
        </div>
      ) : (
        <Card>
          <div className="stack" style={{ alignItems: "center", gap: 10, padding: "28px 12px", textAlign: "center" }}>
            <div className="insight-card-ic" style={{ background: "#0559c91e", color: "#0559c9", width: 48, height: 48 }}>
              <Icon name="cloud" size={22} />
            </div>
            <div style={{ fontWeight: 700, fontSize: 15 }}>No bring-your-own storage yet</div>
            <div className="faint" style={{ fontSize: 12.5, maxWidth: 440 }}>
              Connect your own AWS, Azure or Google Cloud storage so backups also land in infrastructure
              you control — protected end-to-end by Arkive.
            </div>
            <button className="btn primary sm" onClick={() => setShowAdd(true)}>
              <Icon name="link" size={13} /> Add your first storage
            </button>
          </div>
        </Card>
      )}

      {showAdd && (
        <AddStorageModal providers={list?.providers || []}
                         onClose={() => setShowAdd(false)}
                         onDone={() => { setShowAdd(false); void load(); }} />
      )}
    </>
  );
}

// Arkive Cloud is our fully-managed hosted tier — a distinct full-width row, not
// a customer card. Read-only: we surface usage + the sources landing here; there
// are no credentials, health tests or controls to manage (Arkive runs it).
function ArkiveCloudRow({ data, onOpen }: { data: ArkiveCloud | null; onOpen: () => void }) {
  const inUse = !!data && (data.enabled || data.used_bytes > 0 || data.source_count > 0);
  return (
    <Card style={{ borderLeft: "3px solid #0559c9" }}>
      <div className="row" style={{ gap: 14, alignItems: "center", flexWrap: "wrap" }}>
        <ArkiveMark size={46} />
        <div className="flex1" style={{ minWidth: 200 }}>
          <div className="row" style={{ gap: 8, alignItems: "center" }}>
            <div style={{ fontWeight: 700, fontSize: 15 }}>Arkive Cloud</div>
            <Pill tone="info">Managed by Arkive</Pill>
          </div>
          <div className="faint" style={{ fontSize: 12 }}>
            Our hosted, multi-region cloud — fully managed, nothing to configure.
          </div>
        </div>
        {inUse ? (
          <div className="row" style={{ gap: 20, fontSize: 12.5 }}>
            <span className="faint">Stored <b style={{ color: "var(--text)" }}>{bytes(data!.used_bytes)}</b></span>
            <span className="faint">Points <b style={{ color: "var(--text)" }}>{data!.recovery_points}</b></span>
            <span className="faint">Items <b style={{ color: "var(--text)" }}>{data!.object_count.toLocaleString()}</b></span>
            <span className="faint">Sources <b style={{ color: "var(--text)" }}>{data!.source_count}</b></span>
            <button className="btn sm" onClick={onOpen}><Icon name="search" size={13} /> View details</button>
          </div>
        ) : (
          <div className="faint" style={{ fontSize: 12.5, maxWidth: 320 }}>
            {data && !data.enabled
              ? <>Not enabled — turn on Arkive Cloud in <a href="/onboarding">Protection Setup</a> to store data with us.</>
              : "No data stored with Arkive Cloud yet."}
          </div>
        )}
      </div>
    </Card>
  );
}

// Read-only drill-in for Arkive Cloud: usage + the sources stored with us. No
// controls — Arkive manages this tier.
function ArkiveCloudDetail({ data, onBack }: { data: ArkiveCloud; onBack: () => void }) {
  return (
    <>
      <button className="btn ghost sm" onClick={onBack} style={{ marginBottom: 12 }}>← Cloud Storage</button>
      <div className="row" style={{ gap: 12, alignItems: "center", marginBottom: 16 }}>
        <ArkiveMark size={44} />
        <div className="stack" style={{ gap: 2 }}>
          <div className="row" style={{ gap: 8, alignItems: "center" }}>
            <h2 style={{ margin: 0 }}>Arkive Cloud</h2>
            <Pill tone="info">Managed by Arkive</Pill>
          </div>
          <div className="faint" style={{ fontSize: 12.5 }}>
            Hosted, multi-region cloud storage
            {data.last_backup_at ? ` · last backup ${fmtAgo(data.last_backup_at)}` : ""}
          </div>
        </div>
      </div>

      <div className="insights-stats" style={{ marginBottom: 16 }}>
        <MiniStat icon="cloud" label="Data stored" value={bytes(data.used_bytes)} tint="#0559c9" />
        <MiniStat icon="clock" label="Recovery points" value={String(data.recovery_points)} tint="#2dbe60" />
        <MiniStat icon="database" label="Items protected" value={data.object_count.toLocaleString()} tint="#c56cf0" />
        <MiniStat icon="shield" label="Encryption" value="Quantum-safe" tint="#35d0a5" />
      </div>

      <Card>
        <div className="row" style={{ gap: 8, marginBottom: 12, alignItems: "center" }}>
          <Icon name="database" size={15} />
          <h3 style={{ margin: 0, fontSize: 15 }}>Sources stored here</h3>
        </div>
        {data.sources.length === 0 ? (
          <div className="muted" style={{ padding: 8 }}>
            No data has landed in Arkive Cloud yet. Route a source to Arkive Cloud in the <a href="/mappings">Data Map</a>.
          </div>
        ) : (
          <table className="table">
            <thead><tr><th>Source</th><th>Recovery points</th><th>Items</th><th>Stored</th><th>Last backup</th></tr></thead>
            <tbody>
              {data.sources.map((s) => (
                <tr key={s.collection_id}>
                  <td>
                    <div className="row" style={{ gap: 8, alignItems: "center" }}>
                      <SourceIcon type={s.source_type} fallback="database" size={16} />
                      <div style={{ fontWeight: 600 }}>{s.name}</div>
                    </div>
                  </td>
                  <td>{s.recovery_points}{s.recoverable ? <span className="faint" style={{ fontSize: 11 }}> · {s.recoverable} recoverable</span> : ""}</td>
                  <td>{s.objects.toLocaleString()}</td>
                  <td>{bytes(s.bytes)}</td>
                  <td className="faint" style={{ fontSize: 12 }}>{fmtAgo(s.last_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </>
  );
}

function StorageCard({ inst, onOpen }: { inst: StorageInstance; onOpen: () => void }) {
  const h = health(inst);
  const provisioning = ["provisioning", "starting"].includes(inst.provision_state);
  return (
    <Card className="insight-card">
      <div className="row" style={{ gap: 10, alignItems: "center", marginBottom: 8, cursor: provisioning ? "default" : "pointer" }}
           onClick={() => { if (!provisioning) onOpen(); }}>
        <div className="insight-card-ic" style={{ background: `${inst.color}1e`, color: inst.color }}>
          <SourceIcon type={inst.provider} fallback="cloud" size={20} />
        </div>
        <div className="flex1">
          <div style={{ fontWeight: 700 }}>{inst.name}</div>
          <div className="faint" style={{ fontSize: 11 }}>{inst.provider_display}</div>
        </div>
        {provisioning
          ? <Pill tone="info"><span className="spinner-dot" /> Setting up</Pill>
          : <Pill tone={h.tone} dot>{h.label}</Pill>}
      </div>
      {provisioning ? (
        <div className="faint" style={{ fontSize: 12, marginBottom: 10 }}>{inst.provision_message || "Provisioning your cloud storage…"}</div>
      ) : (
        <>
          {inst.last_test_error && (
            <div style={{ color: "var(--danger-c,#f2545b)", fontSize: 12, marginBottom: 6 }}>{inst.last_test_error}</div>
          )}
          <div className="row" style={{ gap: 16, fontSize: 12.5, marginBottom: 6 }}>
            <span className="faint">Stored <b style={{ color: "var(--text)" }}>{bytes(inst.used_bytes)}</b></span>
            <span className="faint">Points <b style={{ color: "var(--text)" }}>{inst.recovery_points}</b></span>
            <span className="faint">Items <b style={{ color: "var(--text)" }}>{inst.object_count.toLocaleString()}</b></span>
          </div>
          <div className="faint" style={{ fontSize: 11, marginBottom: 10 }}>
            {inst.last_test_at ? `Last tested ${fmtAgo(inst.last_test_at)}` : "Not tested yet"}
            {!inst.enabled ? " · paused" : ""}
          </div>
        </>
      )}
      <div className="row" style={{ gap: 8, marginTop: "auto" }}>
        {!provisioning && (
          <button className="btn sm" onClick={onOpen}><Icon name="search" size={13} /> Details</button>
        )}
      </div>
    </Card>
  );
}

// Full-page detail — usage, health, the sources stored here, and controls.
function StorageDetail({ inst, providers, onBack, onChanged }: {
  inst: StorageInstance; providers: ProviderSpec[]; onBack: () => void; onChanged: () => void;
}) {
  const [data, setData] = useState<DataResp | null>(null);
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState(false);
  const cur = data?.storage || inst;
  const h = health(cur);
  const spec = useMemo(() => providers.find((p) => p.provider === inst.provider), [providers, inst.provider]);

  async function loadData() {
    try { setData(await api.get<DataResp>(`/storage/${inst.id}/data`)); } catch { /* ignore */ }
  }
  useEffect(() => { void loadData(); /* eslint-disable-next-line */ }, [inst.id]);

  async function test() {
    setBusy(true);
    try {
      const res = await api.post<StorageInstance>(`/storage/${inst.id}/test`, {});
      await loadData(); onChanged();
      if (res.last_test_ok) notify({ title: "Storage healthy", message: "Read & write access verified.", tone: "ok" });
      else notify({ title: "Storage test failed", message: res.last_test_error || "Could not verify access.", tone: "danger" });
    } catch (e) { notify({ message: (e as Error).message, tone: "danger" }); }
    finally { setBusy(false); }
  }
  async function toggle() {
    setBusy(true);
    try { await api.put(`/storage/${inst.id}`, { enabled: !cur.enabled }); await loadData(); onChanged(); }
    finally { setBusy(false); }
  }
  async function remove() {
    if (!(await confirmDialog({ title: `Remove ${cur.name}?`,
      message: "This removes the storage configuration. Your bucket and its objects are left untouched. Any mapping still routing here must be re-routed first.",
      confirmLabel: "Remove", tone: "danger" }))) return;
    try { await api.del(`/storage/${inst.id}`); onBack(); }
    catch (e) { notify({ message: (e as Error).message, tone: "danger" }); }
  }

  const cfg = cur.config || {};
  const cfgLabel = (spec?.config || []).filter((f) => cfg[f.name]);

  return (
    <>
      <button className="btn ghost sm" onClick={onBack} style={{ marginBottom: 12 }}>← Cloud Storage</button>
      <div className="spread" style={{ marginBottom: 16, alignItems: "flex-start", flexWrap: "wrap", gap: 12 }}>
        <div className="row" style={{ gap: 12, alignItems: "center" }}>
          <div className="insight-card-ic" style={{ background: `${cur.color}1e`, color: cur.color, width: 44, height: 44 }}>
            <SourceIcon type={cur.provider} fallback="cloud" size={24} />
          </div>
          <div className="stack" style={{ gap: 2 }}>
            <div className="row" style={{ gap: 8, alignItems: "center" }}>
              <h2 style={{ margin: 0 }}>{cur.name}</h2>
              <Pill tone={h.tone} dot>{h.label}</Pill>
            </div>
            <div className="faint" style={{ fontSize: 12.5 }}>
              {cur.provider_display}{cfg.bucket ? ` · ${cfg.bucket}` : cfg.container ? ` · ${cfg.container}` : ""}
              {cur.last_test_at ? ` · tested ${fmtAgo(cur.last_test_at)}` : ""}
            </div>
          </div>
        </div>
        <div className="row" style={{ gap: 8 }}>
          <button className="btn sm" disabled={busy} onClick={() => void test()}>
            {busy ? <><span className="spinner-dot" /> Testing…</> : <><Icon name="repeat" size={13} /> Test now</>}
          </button>
          <button className="btn ghost sm" onClick={() => setEditing(true)}><Icon name="edit" size={13} /> Edit</button>
          <button className="btn ghost sm" disabled={busy} onClick={() => void toggle()}>{cur.enabled ? "Pause" : "Resume"}</button>
          <button className="btn danger sm" onClick={() => void remove()}>Remove</button>
        </div>
      </div>

      {cur.last_test_error && (
        <div className="row" style={{ gap: 8, alignItems: "center", marginBottom: 12,
              border: "1px solid var(--danger-c,#f2545b)", borderRadius: 8, padding: "8px 12px" }}>
          <Icon name="alert" size={15} />
          <span style={{ color: "var(--danger-c,#f2545b)", fontSize: 12.5 }}>{cur.last_test_error}</span>
        </div>
      )}

      <div className="insights-stats" style={{ marginBottom: 16 }}>
        <MiniStat icon="cloud" label="Data stored" value={bytes(cur.used_bytes)} tint="#4f7cff" />
        <MiniStat icon="clock" label="Recovery points" value={String(cur.recovery_points)} tint="#2dbe60" />
        <MiniStat icon="database" label="Items protected" value={cur.object_count.toLocaleString()} tint="#c56cf0" />
        <MiniStat icon="shield" label="Encryption" value="Quantum-safe" tint="#35d0a5" />
      </div>

      <Card style={{ marginBottom: 16 }}>
        <div className="row" style={{ gap: 8, marginBottom: 8, alignItems: "center" }}>
          <Icon name="key" size={15} />
          <h3 style={{ margin: 0, fontSize: 15 }}>Access</h3>
        </div>
        <div className="faint" style={{ fontSize: 12.5, lineHeight: 1.5 }}>
          The <b>write</b> credential is held securely so automated backups keep running unattended.
          {" "}Restores use a separate <b>read</b> credential that is only unlocked by your passkey —{" "}
          {cur.has_read_credential
            ? "a read credential is configured."
            : "no read credential is configured yet, so restores fall back to the write credential."}
        </div>
        <div className="row" style={{ gap: 8, marginTop: 10, flexWrap: "wrap" }}>
          <Pill tone="ok"><Icon name="check" size={11} /> Write credential stored</Pill>
          <Pill tone={cur.has_read_credential ? "ok" : "warn"}>
            {cur.has_read_credential ? <><Icon name="check" size={11} /> Read credential (passkey-gated)</> : "No dedicated read credential"}
          </Pill>
          {cfgLabel.map((f) => (
            <Pill key={f.name} tone="info">{f.label.replace(/\s*\(.*\)/, "")}: {cfg[f.name]}</Pill>
          ))}
        </div>
      </Card>

      <Card>
        <div className="row" style={{ gap: 8, marginBottom: 12, alignItems: "center" }}>
          <Icon name="database" size={15} />
          <h3 style={{ margin: 0, fontSize: 15 }}>Sources stored here</h3>
        </div>
        {(data?.sources || []).length === 0 ? (
          <div className="muted" style={{ padding: 8 }}>
            No data has landed here yet. Route a source to this storage in the <a href="/mappings">Data Map</a>.
          </div>
        ) : (
          <table className="table">
            <thead><tr><th>Source</th><th>Recovery points</th><th>Items</th><th>Stored</th><th>Last backup</th></tr></thead>
            <tbody>
              {(data?.sources || []).map((s) => (
                <tr key={s.collection_id}>
                  <td>
                    <div className="row" style={{ gap: 8, alignItems: "center" }}>
                      <SourceIcon type={s.source_type} fallback="database" size={16} />
                      <div style={{ fontWeight: 600 }}>{s.name}</div>
                    </div>
                  </td>
                  <td>{s.recovery_points}{s.recoverable ? <span className="faint" style={{ fontSize: 11 }}> · {s.recoverable} recoverable</span> : ""}</td>
                  <td>{s.objects.toLocaleString()}</td>
                  <td>{bytes(s.bytes)}</td>
                  <td className="faint" style={{ fontSize: 12 }}>{fmtAgo(s.last_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      {editing && spec && (
        <SetupForm provider={spec} existing={cur} onClose={() => setEditing(false)}
                   onDone={() => { setEditing(false); void loadData(); onChanged(); }} />
      )}
    </>
  );
}

function MiniStat({ icon, label, value, tint }: { icon: IconName; label: string; value: string; tint: string }) {
  return (
    <div className="insights-stat">
      <div className="insights-stat-ic" style={{ background: `${tint}22`, color: tint }}>
        <Icon name={icon} size={16} />
      </div>
      <div className="stack" style={{ gap: 1 }}>
        <div style={{ fontSize: 17, fontWeight: 700 }}>{value}</div>
        <div className="faint" style={{ fontSize: 11.5 }}>{label}</div>
      </div>
    </div>
  );
}

// Add flow: pick provider → pick setup mode → the credential form.
export function AddStorageModal({ providers, onClose, onDone }: {
  providers: ProviderSpec[]; onClose: () => void; onDone: () => void;
}) {
  const [provider, setProvider] = useState<ProviderSpec | null>(null);
  const [mode, setMode] = useState<"pick" | "existing" | "provision">("pick");

  if (provider && mode === "existing") {
    return <SetupForm provider={provider} onClose={onClose} onDone={onDone} />;
  }
  if (provider && mode === "provision") {
    return <ProvisionForm provider={provider} onClose={onClose} onDone={onDone} />;
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-panel" style={{ width: "min(720px, 100%)" }} onClick={(e) => e.stopPropagation()}>
        <div className="spread">
          <div>
            <h3 style={{ margin: 0 }}>{provider ? `Set up ${provider.display_name}` : "Add cloud storage"}</h3>
            <div className="faint" style={{ fontSize: 12, maxWidth: 520 }}>
              {provider ? "Choose how you'd like to connect it." : "Pick your cloud provider."}
            </div>
          </div>
          <button className="btn ghost sm" onClick={onClose}>Close</button>
        </div>
        <div className="modal-body">
          {!provider ? (
            <div className="grid grid-3">
              {providers.map((p) => (
                <div key={p.provider} className="dest-card" onClick={() => setProvider(p)}>
                  <div className="row" style={{ gap: 10, alignItems: "center" }}>
                    <div className="insight-card-ic" style={{ background: `${p.color}1e`, color: p.color, width: 34, height: 34 }}>
                      <SourceIcon type={p.provider} fallback="cloud" size={19} />
                    </div>
                    <div style={{ fontWeight: 650 }}>{p.display_name}</div>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <div className="stack" style={{ gap: 12 }}>
              <div className="dest-card" onClick={() => setMode("provision")}>
                <div className="row" style={{ gap: 10, alignItems: "center" }}>
                  <Icon name="sparkle" size={18} />
                  <div className="flex1">
                    <div style={{ fontWeight: 650 }}>Set it up for me <Pill tone="ok">Recommended</Pill></div>
                    <div className="faint" style={{ fontSize: 12 }}>
                      We create the bucket, keys and permissions for you — a write-only key for backups
                      and a separate read-only key for restores.
                    </div>
                  </div>
                </div>
              </div>
              <div className="dest-card" onClick={() => setMode("existing")}>
                <div className="row" style={{ gap: 10, alignItems: "center" }}>
                  <Icon name="key" size={18} />
                  <div className="flex1">
                    <div style={{ fontWeight: 650 }}>I already have storage</div>
                    <div className="faint" style={{ fontSize: 12 }}>Enter your existing bucket + access keys.</div>
                  </div>
                </div>
              </div>
              <button className="btn ghost sm" style={{ alignSelf: "flex-start" }} onClick={() => setProvider(null)}>← Back</button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// Scenario-2: collect the org-level admin credential (used once, never stored),
// kick off provisioning, then watch it create the bucket + scoped keys live.
function ProvisionForm({ provider, onClose, onDone }: {
  provider: ProviderSpec; onClose: () => void; onDone: () => void;
}) {
  const [name, setName] = useState(`My ${provider.display_name}`);
  const [admin, setAdmin] = useState<Record<string, string>>(() => selectDefaults(provider.provision || []));
  const [starting, setStarting] = useState(false);
  const [err, setErr] = useState("");
  const [provId, setProvId] = useState<string | null>(null);
  const [showHelp, setShowHelp] = useState(true);
  const fields = provider.provision || [];

  async function start() {
    for (const f of fields) {
      if (f.required && !admin[f.name]) { setErr(`${f.label} is required.`); return; }
    }
    setStarting(true); setErr("");
    try {
      const res = await api.post<{ id: string }>("/storage/provision", {
        provider: provider.provider, name, admin,
      });
      setProvId(res.id);
    } catch (e) {
      setErr((e as Error)?.message || "Could not start provisioning"); setStarting(false);
    }
  }

  if (provId) {
    return <ProvisionProgress storageId={provId} label={name} onClose={onClose} onDone={onDone} />;
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-panel" style={{ maxWidth: 560 }} onClick={(e) => e.stopPropagation()}>
        <div className="spread">
          <div>
            <h3 style={{ margin: 0 }}>Provision {provider.display_name}</h3>
            <div className="faint" style={{ fontSize: 12, maxWidth: 480 }}>
              Sign in to your cloud console (with MFA), create an admin credential, and paste it below.
              We use it once to create everything, then discard it — only the two scoped keys are kept.
            </div>
          </div>
          <button className="btn ghost sm" onClick={onClose}><Icon name="logout" size={14} /></button>
        </div>
        <div className="modal-body" style={{ maxHeight: "68vh", overflow: "auto" }}>
          {err && <div style={{ color: "var(--danger-c,#f2545b)", fontSize: 12, marginBottom: 10 }}>{err}</div>}
          {(provider.help || []).length > 0 && (
            <div style={{ border: "1px solid var(--border-soft)", borderRadius: 10, padding: "10px 12px", marginBottom: 14, background: "var(--inset)" }}>
              <button className="btn ghost sm" style={{ padding: 0, marginBottom: showHelp ? 8 : 0 }}
                      onClick={() => setShowHelp((v) => !v)}>
                <Icon name="info" size={13} /> Where do I find these?
                <span style={{ display: "inline-block", marginLeft: 4, fontSize: 10, transform: showHelp ? "rotate(90deg)" : "none" }}>▶</span>
              </button>
              {showHelp && (
                <ol style={{ margin: 0, paddingLeft: 18, fontSize: 12, lineHeight: 1.6 }}>
                  {(provider.help || []).map((s, i) => <li key={i}>{s}</li>)}
                </ol>
              )}
            </div>
          )}
          <label className="stack" style={{ marginBottom: 12 }}>
            <span className="faint" style={{ fontSize: 11.5 }}>Display name</span>
            <input className="input" value={name} onChange={(e) => setName(e.target.value)} />
          </label>
          <div className="faint" style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: 0.5, margin: "6px 0" }}>
            Admin credential <span style={{ textTransform: "none" }}>· used once, never stored</span>
          </div>
          {fields.map((f) => (
            <FieldInput key={f.name} f={f} value={admin[f.name] || ""}
                        onChange={(v) => setAdmin((a) => ({ ...a, [f.name]: v }))} />
          ))}
          <div className="faint" style={{ fontSize: 11.5, marginTop: 4 }}>
            <Icon name="shield" size={12} /> We'll create a dedicated bucket, a write-only backup key,
            and a read-only restore key scoped to just that bucket.
          </div>
        </div>
        <div className="modal-foot">
          <div style={{ flex: 1 }} />
          <button className="btn sm" onClick={onClose}>Cancel</button>
          <button className="btn primary sm" disabled={starting} onClick={() => void start()}>
            {starting ? "Starting…" : "Provision storage"}
          </button>
        </div>
      </div>
    </div>
  );
}

interface ProvStatus { provision_state: string; message: string | null; done: boolean; error: boolean; status: string; }

function ProvisionProgress({ storageId, label, onClose, onDone }: {
  storageId: string; label: string; onClose: () => void; onDone: () => void;
}) {
  const [prov, setProv] = useState<ProvStatus | null>(null);
  useEffect(() => {
    let stop = false;
    async function poll() {
      try {
        const s = await api.get<ProvStatus>(`/storage/${storageId}/provision`);
        if (stop) return;
        setProv(s);
        if (!s.done && !s.error) setTimeout(poll, 2000);
      } catch { if (!stop) setTimeout(poll, 2500); }
    }
    void poll();
    return () => { stop = true; };
  }, [storageId]);

  const done = !!prov?.done;
  const error = !!prov?.error;
  return (
    <div className="modal-backdrop" onClick={done || error ? onClose : undefined}>
      <div className="modal-panel" style={{ maxWidth: 480 }} onClick={(e) => e.stopPropagation()}>
        <div className="spread">
          <div>
            <h3 style={{ margin: 0 }}>Setting up {label}</h3>
            <div className="faint" style={{ fontSize: 12 }}>Provisioning in your cloud account…</div>
          </div>
          <button className="btn ghost sm" onClick={onClose}><Icon name="logout" size={14} /></button>
        </div>
        <div className="modal-body">
          <div className="row" style={{ gap: 12, alignItems: "center", padding: "8px 0" }}>
            <div style={{ width: 30, height: 30, borderRadius: "50%", display: "grid", placeItems: "center",
                          flexShrink: 0, background: done ? "#2dbe60" : error ? "var(--danger-c,#f2545b)" : "#4f7cff", color: "#fff" }}>
              {done ? <Icon name="check" size={16} /> : error ? <Icon name="alert" size={16} /> : <span className="spinner-dot" />}
            </div>
            <div className="flex1">
              <div style={{ fontWeight: 600, fontSize: 13.5 }}>
                {done ? "Storage ready" : error ? "Provisioning failed" : (prov?.message || "Working…")}
              </div>
              {done && <div className="faint" style={{ fontSize: 12, marginTop: 2 }}>{prov?.message}</div>}
              {error && <div style={{ color: "var(--danger-c,#f2545b)", fontSize: 12, marginTop: 2 }}>{prov?.message}</div>}
            </div>
          </div>
          {!done && !error && (
            <div className="faint" style={{ fontSize: 12, marginTop: 6 }}>
              Creating the bucket and scoped keys can take up to a minute — cloud keys need a moment to activate.
            </div>
          )}
        </div>
        <div className="modal-foot">
          <div style={{ flex: 1 }} />
          {done ? <button className="btn primary sm" onClick={onDone}>Done</button>
            : error ? <button className="btn sm" onClick={onDone}>Close</button>
            : <button className="btn sm" onClick={onClose}>Run in background</button>}
        </div>
      </div>
    </div>
  );
}

// Scenario-1 credential form (create or edit). Renders config + write + read fields.
function SetupForm({ provider, existing, onClose, onDone }: {
  provider: ProviderSpec; existing?: StorageInstance; onClose: () => void; onDone: () => void;
}) {
  const editing = !!existing;
  const [name, setName] = useState(existing?.name || provider.display_name);
  const [cfg, setCfg] = useState<Record<string, string>>({ ...selectDefaults(provider.config), ...(existing?.config || {}) });
  const [write, setWrite] = useState<Record<string, string>>({});
  const [read, setRead] = useState<Record<string, string>>({});
  const [showRead, setShowRead] = useState(false);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState("");

  async function save() {
    for (const f of provider.config) {
      if (f.required && !cfg[f.name]) { setErr(`${f.label} is required.`); return; }
    }
    if (!editing) {
      const missingWrite = provider.write.some((f) => f.required && !write[f.name]);
      const anyWrite = provider.write.some((f) => write[f.name]);
      if (missingWrite || !anyWrite) { setErr("Enter the write credential."); return; }
    }
    setSaving(true); setErr("");
    try {
      const body = {
        provider: provider.provider, name,
        config: cfg,
        write: Object.fromEntries(Object.entries(write).filter(([, v]) => v)),
        read: Object.fromEntries(Object.entries(read).filter(([, v]) => v)),
      };
      if (editing) await api.put(`/storage/${existing!.id}`, body);
      else await api.post("/storage", body);
      notify({ message: editing ? "Storage updated." : `${provider.display_name} connected — testing…`, tone: "info" });
      onDone();
    } catch (e) {
      setErr((e as Error)?.message || "Could not save the storage"); setSaving(false);
    }
  }

  const field = (f: ProviderField, val: Record<string, string>, set: (u: Record<string, string>) => void, ph?: string) => (
    <FieldInput key={f.name} f={f} value={val[f.name] || ""} placeholder={ph}
                showRequired={!editing}
                onChange={(v) => set({ ...val, [f.name]: v })} />
  );

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-panel" style={{ maxWidth: 560 }} onClick={(e) => e.stopPropagation()}>
        <div className="spread">
          <div>
            <h3 style={{ margin: 0 }}>{editing ? `Edit ${existing!.name}` : `Connect ${provider.display_name}`}</h3>
            <div className="faint" style={{ fontSize: 12 }}>Data is encrypted before it reaches your bucket.</div>
          </div>
          <button className="btn ghost sm" onClick={onClose}><Icon name="logout" size={14} /></button>
        </div>
        <div className="modal-body" style={{ maxHeight: "68vh", overflow: "auto" }}>
          {err && <div style={{ color: "var(--danger-c,#f2545b)", fontSize: 12, marginBottom: 10 }}>{err}</div>}
          <label className="stack" style={{ marginBottom: 12 }}>
            <span className="faint" style={{ fontSize: 11.5 }}>Display name</span>
            <input className="input" value={name} onChange={(e) => setName(e.target.value)}
                   placeholder="e.g. My AWS backups" />
          </label>
          <div className="faint" style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: 0.5, margin: "6px 0" }}>Storage location</div>
          {provider.config.map((f) => field(f, cfg, setCfg))}
          <div className="faint" style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: 0.5, margin: "6px 0" }}>
            Write credential <span style={{ textTransform: "none" }}>· used for automated backups</span>
          </div>
          {provider.write.map((f) => field(f, write, setWrite, editing ? "leave blank to keep current" : f.placeholder))}
          <button className="btn ghost sm" style={{ marginBottom: 10 }} onClick={() => setShowRead((v) => !v)}>
            <Icon name={showRead ? "check" : "key"} size={12} /> {showRead ? "Hide" : "Add"} a separate read credential (recommended)
          </button>
          {showRead && (
            <>
              <div className="faint" style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: 0.5, margin: "6px 0" }}>
                Read credential <span style={{ textTransform: "none" }}>· unlocked by your passkey for restores</span>
              </div>
              {provider.read.map((f) => field(f, read, setRead, editing ? "leave blank to keep current" : f.placeholder))}
            </>
          )}
        </div>
        <div className="modal-foot">
          <div style={{ flex: 1 }} />
          <button className="btn sm" onClick={onClose}>Cancel</button>
          <button className="btn primary sm" disabled={saving} onClick={() => void save()}>
            {saving ? "Saving…" : editing ? "Save changes" : "Connect & test"}
          </button>
        </div>
      </div>
    </div>
  );
}
