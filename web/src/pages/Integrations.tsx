import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { Card, Pill, bytes, Loading } from "../components/ui";
import { Icon, IconName } from "../components/Icon";
import { SourceIcon } from "../components/SourceIcon";
import { notify, confirmDialog } from "../components/dialog";

interface CredField { name: string; label: string; type: string; placeholder: string; required: boolean; help: string; }
interface Spec {
  integration_type: string; display_name: string; description: string; icon: string;
  color: string; category: string; runs_on: string; needs_appliance: boolean;
  default_interval_minutes: number; auto_provision_key: boolean; provides: string[];
  credential_fields: CredField[];
}
interface Instance {
  id: string; integration_type: string; display_name: string; label: string;
  enabled: boolean; runs_on: string; appliance_id: string | null; status: string;
  poll_interval_minutes: number; host: string; last_run_at: string | null;
  last_success_at: string | null; last_error: string | null;
  provision_state?: string; provision_message?: string | null;
  last_stats: { clients?: number; apps?: number; bytes_seen?: number; note?: string };
}
interface ApplianceRef { id: string; name: string; state: string; online: boolean; }
interface ListResp { available: Spec[]; instances: Instance[]; appliances: ApplianceRef[]; }

interface NetClient {
  id: string; name: string; hostname: string; ip: string; mac: string;
  device_type: string; is_wired: boolean; is_guest: boolean;
  monitor_state: string; of_interest: boolean; total_bytes: number; last_seen: string | null;
}
interface NetApp {
  app_key: string; name: string; category: string; source_type: string;
  of_interest: boolean; total_bytes: number; client_count: number; last_seen: string | null;
}
interface ShadowSource { source_type: string; name: string; total_bytes: number; apps: number; }
interface DataResp {
  clients: NetClient[]; apps: NetApp[]; shadow: ShadowSource[];
  stats: { clients?: number; monitored?: number; ignored?: number; apps?: number; bytes?: number };
}

const asIcon = (n: string): IconName => (n || "puzzle") as IconName;
const STATUS_TONE: Record<string, "ok" | "warn" | "info" | "danger"> = {
  active: "ok", pending: "info", error: "danger", disabled: "warn",
};

export default function Integrations() {
  const [list, setList] = useState<ListResp | null>(null);
  const [loading, setLoading] = useState(true);
  const [setupSpec, setSetupSpec] = useState<Spec | null>(null);
  const [detailId, setDetailId] = useState<string | null>(null);

  async function load() {
    try { setList(await api.get<ListResp>("/integrations")); }
    catch { /* ignore */ }
    finally { setLoading(false); }
  }
  useEffect(() => { void load(); }, []);

  const addable = useMemo(() => {
    if (!list) return [];
    const have = new Set(list.instances.map((i) => i.integration_type));
    return list.available.filter((s) => !have.has(s.integration_type) || s.needs_appliance);
  }, [list]);

  const specByType = useMemo(() => {
    const m: Record<string, Spec> = {};
    for (const s of (list?.available || [])) m[s.integration_type] = s;
    return m;
  }, [list]);

  if (loading) return <Loading label="Loading integrations…" />;

  const detailInst = detailId ? list?.instances.find((i) => i.id === detailId) : null;
  if (detailId && detailInst) {
    return <IntegrationDetail inst={detailInst} spec={specByType[detailInst.integration_type]}
                              onBack={() => { setDetailId(null); void load(); }}
                              onChanged={load} />;
  }

  return (
    <>
      <div className="spread" style={{ marginBottom: 18 }}>
        <div className="stack">
          <h2 style={{ margin: 0 }}>Integrations</h2>
          <div className="faint" style={{ fontSize: 12.5 }}>
            Unlock intelligence about your environment — the apps and services in use, who's using
            them, and where your data really lives. Integrations don't back up data; they inform it.
          </div>
        </div>
      </div>

      {/* Enabled integrations */}
      {list && list.instances.length > 0 && (
        <div className="insights-cards" style={{ marginBottom: 20 }}>
          {list.instances.map((i) => (
            <InstanceCard key={i.id} inst={i} spec={specByType[i.integration_type]}
                          onOpen={() => setDetailId(i.id)} onChanged={load} />
          ))}
        </div>
      )}

      {/* Add an integration */}
      <Card>
        <div className="row" style={{ gap: 8, marginBottom: 10, alignItems: "center" }}>
          <Icon name="puzzle" size={16} />
          <h3 style={{ margin: 0, fontSize: 15 }}>Available integrations</h3>
        </div>
        <div className="insights-cards">
          {(list?.available || []).map((s) => (
            <div key={s.integration_type} className="integration-tile">
              <div className="row" style={{ gap: 10, alignItems: "center", marginBottom: 6 }}>
                <div className="insight-card-ic" style={{ background: `${s.color}1e`, color: s.color }}>
                  <SourceIcon type={s.integration_type} fallback={asIcon(s.icon)} size={20} />
                </div>
                <div className="flex1">
                  <div style={{ fontWeight: 700 }}>{s.display_name}</div>
                  <div className="faint" style={{ fontSize: 11 }}>
                    Runs on {s.runs_on === "appliance" ? "your appliance" : "the cloud"}
                  </div>
                </div>
              </div>
              <div className="faint" style={{ fontSize: 12.5, lineHeight: 1.45, minHeight: 54 }}>
                {s.description}
              </div>
              <button className="btn primary sm" style={{ marginTop: 10 }}
                      onClick={() => setSetupSpec(s)}>
                <Icon name="link" size={13} /> Set up
              </button>
            </div>
          ))}
          {addable.length === 0 && (list?.available.length || 0) === 0 && (
            <div className="muted">No integrations are available yet.</div>
          )}
        </div>
      </Card>

      {setupSpec && (
        <SetupModal spec={setupSpec} appliances={list?.appliances || []}
                    onClose={() => setSetupSpec(null)}
                    onDone={() => { setSetupSpec(null); void load(); }} />
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

function ShadowChip({ s }: { s: ShadowSource }) {
  const nav = useNavigate();
  return (
    <div className="row" style={{ gap: 8, alignItems: "center", border: "1px solid var(--border-soft)",
          borderRadius: 10, padding: "8px 12px" }}>
      <div className="stack" style={{ gap: 0 }}>
        <div style={{ fontWeight: 600, fontSize: 13 }}>{s.name}</div>
        <div className="faint" style={{ fontSize: 11 }}>{bytes(s.total_bytes)} observed</div>
      </div>
      <button className="btn sm" onClick={() => nav("/connectors")}>Connect</button>
    </div>
  );
}

function InstanceCard({ inst, spec, onOpen, onChanged }: {
  inst: Instance; spec?: Spec; onOpen: () => void; onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState(false);
  const [resuming, setResuming] = useState(false);
  const st = inst.last_stats || {};
  const provisioning = !!inst.provision_state && !["idle", "done"].includes(inst.provision_state);
  async function toggle() {
    setBusy(true);
    try { await api.put(`/integrations/${inst.id}`, { enabled: !inst.enabled }); onChanged(); }
    finally { setBusy(false); }
  }
  async function repoll() {
    setBusy(true);
    try {
      await api.post(`/integrations/${inst.id}/repoll`, {});
      notify({ message: "Re-poll requested — new data will arrive shortly.", tone: "info" });
    } catch (e) { notify({ message: (e as Error).message, tone: "danger" }); }
    finally { setBusy(false); }
  }
  async function remove() {
    if (!(await confirmDialog({ title: `Remove ${inst.label}?`,
      message: "This stops the integration and deletes the network telemetry it collected.",
      confirmLabel: "Remove", tone: "danger" }))) return;
    setBusy(true);
    try { await api.del(`/integrations/${inst.id}`); onChanged(); }
    finally { setBusy(false); }
  }
  return (
    <Card className="insight-card">
      <div className="row" style={{ gap: 10, alignItems: "center", marginBottom: 8, cursor: provisioning ? "default" : "pointer" }}
           onClick={() => { if (!provisioning) onOpen(); }}>
        <div className="insight-card-ic" style={{ background: "#0559c91e", color: "#0559c9" }}>
          <SourceIcon type={inst.integration_type} fallback="activity" size={20} />
        </div>
        <div className="flex1">
          <div style={{ fontWeight: 700 }}>{inst.label}</div>
          <div className="faint" style={{ fontSize: 11 }}>{inst.host || inst.integration_type}</div>
        </div>
        <Pill tone={STATUS_TONE[inst.status] || "info"}>{inst.status}</Pill>
      </div>
      {provisioning && (
        <div className="row" style={{ gap: 8, alignItems: "center", marginBottom: 8,
              border: "1px solid var(--warn,#f5a623)", borderRadius: 8, padding: "6px 10px" }}>
          <Icon name="alert" size={14} />
          <span style={{ fontSize: 12.5, flex: 1 }}>
            {inst.provision_state === "error" ? "Setup didn't finish" : "Setup in progress"}
          </span>
          <button className="btn primary sm" onClick={() => setResuming(true)}>Continue setup</button>
        </div>
      )}
      {inst.last_error && !provisioning && (
        <div style={{ color: "var(--danger-c,#f2545b)", fontSize: 12, marginBottom: 6 }}>{inst.last_error}</div>
      )}
      <div className="row" style={{ gap: 16, fontSize: 12.5, marginBottom: 10 }}>
        <span className="faint">Clients <b style={{ color: "var(--text)" }}>{st.clients ?? "—"}</b></span>
        <span className="faint">Apps <b style={{ color: "var(--text)" }}>{st.apps ?? "—"}</b></span>
        <span className="faint">Seen <b style={{ color: "var(--text)" }}>{st.bytes_seen ? bytes(st.bytes_seen) : "—"}</b></span>
      </div>
      <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
        {!provisioning && (
          <>
            <button className="btn sm" disabled={busy} onClick={onOpen}>
              <Icon name="search" size={13} /> Details
            </button>
            <button className="btn ghost sm" disabled={busy} onClick={() => void repoll()}>
              <Icon name="repeat" size={13} /> Re-poll
            </button>
          </>
        )}
        <button className="btn ghost sm" disabled={busy} onClick={() => setEditing(true)}>
          <Icon name="edit" size={13} /> Edit
        </button>
        <button className="btn ghost sm" disabled={busy} onClick={() => void toggle()}>
          {inst.enabled ? "Pause" : "Resume"}
        </button>
        <button className="btn danger sm" disabled={busy} onClick={() => void remove()}>Remove</button>
      </div>
      {editing && (
        <EditModal inst={inst} spec={spec} onClose={() => setEditing(false)}
                   onDone={() => { setEditing(false); onChanged(); }} />
      )}
      {resuming && (
        <ProvisioningModal instanceId={inst.id} label={inst.label}
                           onClose={() => { setResuming(false); onChanged(); }}
                           onDone={() => { setResuming(false); onChanged(); }} />
      )}
    </Card>
  );
}

// Full-page detail for one integration instance — its own clients, apps, shadow
// sources and stats (scoped to this instance only).
function IntegrationDetail({ inst, spec, onBack, onChanged }: {
  inst: Instance; spec?: Spec; onBack: () => void; onChanged: () => void;
}) {
  const [data, setData] = useState<DataResp | null>(null);
  const [tab, setTab] = useState<"apps" | "clients">("apps");
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState(false);
  const [repolling, setRepolling] = useState(false);

  async function loadData() {
    try { setData(await api.get<DataResp>(`/integrations/${inst.id}/data`)); }
    catch { /* ignore */ }
  }
  useEffect(() => {
    void loadData();
    const t = setInterval(loadData, 8000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [inst.id]);

  async function repoll() {
    setRepolling(true);
    try {
      await api.post(`/integrations/${inst.id}/repoll`, {});
      notify({ message: "Re-poll requested — refreshing shortly.", tone: "info" });
      setTimeout(() => { void loadData(); onChanged(); setRepolling(false); }, 25000);
    } catch (e) { notify({ message: (e as Error).message, tone: "danger" }); setRepolling(false); }
  }
  async function toggle() {
    setBusy(true);
    try { await api.put(`/integrations/${inst.id}`, { enabled: !inst.enabled }); onChanged(); }
    finally { setBusy(false); }
  }
  async function remove() {
    if (!(await confirmDialog({ title: `Remove ${inst.label}?`,
      message: "This stops the integration and deletes the network telemetry it collected.",
      confirmLabel: "Remove", tone: "danger" }))) return;
    try { await api.del(`/integrations/${inst.id}`); onBack(); }
    catch (e) { notify({ message: (e as Error).message, tone: "danger" }); }
  }

  const stats = data?.stats || {};
  const lastRun = inst.last_run_at
    ? new Date(inst.last_run_at.endsWith("Z") ? inst.last_run_at : `${inst.last_run_at}Z`).toLocaleString()
    : null;
  const pollLbl = inst.poll_interval_minutes < 60
    ? `${inst.poll_interval_minutes}m` : `${inst.poll_interval_minutes / 60}h`;

  return (
    <>
      <button className="btn ghost sm" onClick={onBack} style={{ marginBottom: 12 }}>
        ← Integrations
      </button>
      <div className="spread" style={{ marginBottom: 16, alignItems: "flex-start", flexWrap: "wrap", gap: 12 }}>
        <div className="row" style={{ gap: 12, alignItems: "center" }}>
          <div className="insight-card-ic" style={{ background: "#0559c91e", color: "#0559c9", width: 44, height: 44 }}>
            <SourceIcon type={inst.integration_type} fallback="activity" size={24} />
          </div>
          <div className="stack" style={{ gap: 2 }}>
            <div className="row" style={{ gap: 8, alignItems: "center" }}>
              <h2 style={{ margin: 0 }}>{inst.label}</h2>
              <Pill tone={STATUS_TONE[inst.status] || "info"}>{inst.status}</Pill>
            </div>
            <div className="faint" style={{ fontSize: 12.5 }}>
              {inst.host || inst.integration_type} · polls every {pollLbl}
              {lastRun ? ` · last run ${lastRun}` : ""}
            </div>
          </div>
        </div>
        <div className="row" style={{ gap: 8 }}>
          <button className="btn sm" disabled={repolling} onClick={() => void repoll()}>
            {repolling ? <><span className="spinner-dot" /> Re-polling…</> : <><Icon name="repeat" size={13} /> Re-poll</>}
          </button>
          <button className="btn ghost sm" onClick={() => setEditing(true)}><Icon name="edit" size={13} /> Edit</button>
          <button className="btn ghost sm" disabled={busy} onClick={() => void toggle()}>{inst.enabled ? "Pause" : "Resume"}</button>
          <button className="btn danger sm" onClick={() => void remove()}>Remove</button>
        </div>
      </div>

      {inst.last_error && (
        <div style={{ color: "var(--danger-c,#f2545b)", fontSize: 12.5, marginBottom: 12 }}>{inst.last_error}</div>
      )}

      <div className="insights-stats" style={{ marginBottom: 16 }}>
        <MiniStat icon="user" label="Clients seen" value={String(stats.clients || 0)} tint="#4f7cff" />
        <MiniStat icon="shield" label="Monitored" value={String(stats.monitored || 0)} tint="#2dbe60" />
        <MiniStat icon="activity" label="Apps & services" value={String(stats.apps || 0)} tint="#c56cf0" />
        <MiniStat icon="cloud" label="Traffic seen" value={bytes(stats.bytes || 0)} tint="#f5a623" />
      </div>

      {data && data.apps.length === 0 && (
        <Card style={{ marginBottom: 16 }}>
          <div className="row" style={{ gap: 10, alignItems: "center" }}>
            <Icon name="info" size={16} />
            <span className="faint" style={{ fontSize: 12.5 }}>
              {inst.last_stats?.note
                || "No application data yet. If this persists, enable Deep Packet Inspection (Settings → Traffic Identification) on your UniFi controller."}
            </span>
          </div>
        </Card>
      )}

      {data && data.shadow.length > 0 && (
        <Card style={{ marginBottom: 16, borderColor: "var(--warn,#f5a623)" }}>
          <div className="row" style={{ gap: 8, marginBottom: 8, alignItems: "center" }}>
            <Icon name="alert" size={16} />
            <h3 style={{ margin: 0, fontSize: 15 }}>Cloud apps you're not protecting yet</h3>
          </div>
          <div className="faint" style={{ fontSize: 12.5, marginBottom: 10 }}>
            We see these services on your network, but you haven't connected them as sources —
            so anything living only there isn't recoverable.
          </div>
          <div className="row" style={{ gap: 10, flexWrap: "wrap" }}>
            {data.shadow.map((s) => <ShadowChip key={s.source_type} s={s} />)}
          </div>
        </Card>
      )}

      <Card>
        <div className="row" style={{ gap: 8, marginBottom: 12 }}>
          <button className={`chip ${tab === "apps" ? "active" : ""}`} onClick={() => setTab("apps")}>
            Apps & services
          </button>
          <button className={`chip ${tab === "clients" ? "active" : ""}`} onClick={() => setTab("clients")}>
            Clients & devices
          </button>
        </div>
        {tab === "apps"
          ? <AppsTable apps={data?.apps || []} onChanged={loadData} />
          : <ClientsTable clients={data?.clients || []} onChanged={loadData} />}
      </Card>

      {editing && (
        <EditModal inst={inst} spec={spec} onClose={() => setEditing(false)}
                   onDone={() => { setEditing(false); onChanged(); }} />
      )}
    </>
  );
}

function EditModal({ inst, spec, onClose, onDone }: {
  inst: Instance; spec?: Spec; onClose: () => void; onDone: () => void;
}) {
  const [label, setLabel] = useState(inst.label);
  const [host, setHost] = useState(inst.host || "");
  const [interval, setIntervalM] = useState(inst.poll_interval_minutes);
  const [secrets, setSecrets] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState("");
  const secretFields = (spec?.credential_fields || []).filter((f) => f.type !== "host");

  async function save() {
    setSaving(true); setErr("");
    try {
      const filled = Object.fromEntries(Object.entries(secrets).filter(([, v]) => v));
      const hostChanged = host.trim() !== (inst.host || "");
      const body: Record<string, unknown> = { label, poll_interval_minutes: interval };
      if (hostChanged || Object.keys(filled).length > 0) {
        body.credentials = { host: host.trim(), ...filled };
      }
      await api.put(`/integrations/${inst.id}`, body);
      notify({ message: "Integration updated.", tone: "info" });
      onDone();
    } catch (e) {
      setErr((e as Error)?.message || "Could not update the integration"); setSaving(false);
    }
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-panel" style={{ maxWidth: 480 }} onClick={(e) => e.stopPropagation()}>
        <div className="spread">
          <h3 style={{ margin: 0 }}>Edit {inst.label}</h3>
          <button className="btn ghost sm" onClick={onClose}><Icon name="logout" size={14} /></button>
        </div>
        <div className="modal-body">
          {err && <div style={{ color: "var(--danger-c,#f2545b)", fontSize: 12, marginBottom: 10 }}>{err}</div>}
          <label className="stack" style={{ marginBottom: 12 }}>
            <span className="faint" style={{ fontSize: 11.5 }}>Name</span>
            <input className="input" value={label} onChange={(e) => setLabel(e.target.value)} />
          </label>
          <label className="stack" style={{ marginBottom: 12 }}>
            <span className="faint" style={{ fontSize: 11.5 }}>Controller address</span>
            <input className="input" value={host} onChange={(e) => setHost(e.target.value)} />
          </label>
          {secretFields.map((f) => (
            <label key={f.name} className="stack" style={{ marginBottom: 12 }}>
              <span className="faint" style={{ fontSize: 11.5 }}>{f.label}</span>
              <input className="input" type={f.type === "password" ? "password" : "text"}
                     placeholder="leave blank to keep current" value={secrets[f.name] || ""}
                     onChange={(e) => setSecrets((s) => ({ ...s, [f.name]: e.target.value }))} />
            </label>
          ))}
          <label className="stack">
            <span className="faint" style={{ fontSize: 11.5 }}>Poll interval</span>
            <select className="input" value={interval} onChange={(e) => setIntervalM(Number(e.target.value))}>
              {[15, 30, 60, 180, 360, 720, 1440].map((m) => (
                <option key={m} value={m}>{m < 60 ? `${m} min` : m < 1440 ? `${m / 60} hours` : "1 day"}</option>
              ))}
            </select>
          </label>
        </div>
        <div className="modal-foot">
          <div style={{ flex: 1 }} />
          <button className="btn sm" onClick={onClose}>Cancel</button>
          <button className="btn primary sm" disabled={saving} onClick={() => void save()}>
            {saving ? "Saving…" : "Save changes"}
          </button>
        </div>
      </div>
    </div>
  );
}

function SetupModal({ spec, appliances, onClose, onDone }: {
  spec: Spec; appliances: ApplianceRef[]; onClose: () => void; onDone: () => void;
}) {
  const [vals, setVals] = useState<Record<string, string>>({});
  const [applianceId, setApplianceId] = useState(appliances[0]?.id || "");
  const [interval, setIntervalM] = useState(spec.default_interval_minutes);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState("");
  const [provInst, setProvInst] = useState<Instance | null>(null);

  async function save() {
    if (spec.needs_appliance && !applianceId) { setErr("Select an appliance to run this on."); return; }
    for (const f of spec.credential_fields) {
      if (f.required && !vals[f.name]) { setErr(`${f.label} is required.`); return; }
    }
    setSaving(true); setErr("");
    try {
      const inst = await api.post<Instance>("/integrations", {
        integration_type: spec.integration_type,
        appliance_id: spec.needs_appliance ? applianceId : null,
        credentials: vals,
        poll_interval_minutes: interval,
      });
      // Interactive integrations (MFA/OTP) hand off to the provisioning wizard.
      if (inst.provision_state && inst.provision_state !== "idle") {
        setProvInst(inst); setSaving(false);
      } else {
        notify({ message: `${spec.display_name} connected — it will start collecting shortly.`, tone: "info" });
        onDone();
      }
    } catch (e) {
      setErr((e as Error)?.message || "Could not set up the integration"); setSaving(false);
    }
  }

  if (provInst) {
    return <ProvisioningModal instanceId={provInst.id} label={provInst.label}
                              onClose={onClose} onDone={onDone} />;
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-panel" style={{ maxWidth: 520 }} onClick={(e) => e.stopPropagation()}>
        <div className="spread">
          <div>
            <h3 style={{ margin: 0 }}>Set up {spec.display_name}</h3>
            <div className="faint" style={{ fontSize: 12 }}>{spec.description}</div>
          </div>
          <button className="btn ghost sm" onClick={onClose}><Icon name="logout" size={14} /></button>
        </div>
        <div className="modal-body">
          {err && <div style={{ color: "var(--danger-c,#f2545b)", fontSize: 12, marginBottom: 10 }}>{err}</div>}
          {spec.needs_appliance && (
            <label className="stack" style={{ marginBottom: 12 }}>
              <span className="faint" style={{ fontSize: 11.5 }}>Run on appliance</span>
              {appliances.length === 0 ? (
                <div className="muted" style={{ fontSize: 12.5 }}>
                  No appliance available. This integration needs an appliance on your network to reach the device.
                </div>
              ) : (
                <select className="input" value={applianceId} onChange={(e) => setApplianceId(e.target.value)}>
                  {appliances.map((a) => (
                    <option key={a.id} value={a.id}>{a.name}{a.online ? "" : " (offline)"}</option>
                  ))}
                </select>
              )}
            </label>
          )}
          {spec.credential_fields.map((f) => (
            <label key={f.name} className="stack" style={{ marginBottom: 12 }}>
              <span className="faint" style={{ fontSize: 11.5 }}>{f.label}{f.required ? " *" : ""}</span>
              <input className="input" type={f.type === "password" ? "password" : "text"}
                     placeholder={f.placeholder} value={vals[f.name] || ""}
                     onChange={(e) => setVals((v) => ({ ...v, [f.name]: e.target.value }))} />
              {f.help && <span className="faint" style={{ fontSize: 11 }}>{f.help}</span>}
            </label>
          ))}
          <label className="stack" style={{ marginBottom: 4 }}>
            <span className="faint" style={{ fontSize: 11.5 }}>Poll interval</span>
            <select className="input" value={interval} onChange={(e) => setIntervalM(Number(e.target.value))}>
              {[15, 30, 60, 180, 360, 720, 1440].map((m) => (
                <option key={m} value={m}>{m < 60 ? `${m} min` : m < 1440 ? `${m / 60} hours` : "1 day"}</option>
              ))}
            </select>
          </label>
          {spec.auto_provision_key && (
            <div className="faint" style={{ fontSize: 11.5, marginTop: 8 }}>
              <Icon name="key" size={12} /> We'll use your login once to create a scoped API key, then
              discard the password. No further steps needed.
            </div>
          )}
        </div>
        <div className="modal-foot">
          <div style={{ flex: 1 }} />
          <button className="btn sm" onClick={onClose}>Cancel</button>
          <button className="btn primary sm" disabled={saving || (spec.needs_appliance && appliances.length === 0)}
                  onClick={() => void save()}>
            {saving ? "Connecting…" : "Connect"}
          </button>
        </div>
      </div>
    </div>
  );
}

interface ProvResp {
  provision_state: string; message: string | null; needs_otp: boolean;
  done: boolean; error: boolean; step: number; steps: string[];
}

// Interactive setup wizard: drives the login → email/OTP verification → API-key
// handshake on the appliance, showing live progress at each step.
function ProvisioningModal({ instanceId, label, onClose, onDone }: {
  instanceId: string; label: string; onClose: () => void; onDone: () => void;
}) {
  const [prov, setProv] = useState<ProvResp | null>(null);
  const [otp, setOtp] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState("");

  async function poll() {
    try { setProv(await api.get<ProvResp>(`/integrations/${instanceId}/provision`)); }
    catch { /* transient (e.g. still replicating to the node) — keep polling */ }
  }
  useEffect(() => {
    // Kick off (idempotent) then poll for progress until done/error.
    api.post(`/integrations/${instanceId}/provision`, {}).catch(() => {});
    void poll();
    const t = setInterval(poll, 2000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [instanceId]);

  async function submitOtp() {
    if (!otp.trim()) return;
    setSubmitting(true); setErr("");
    try {
      await api.post(`/integrations/${instanceId}/provision/otp`, { otp: otp.trim() });
      setOtp(""); await poll();
    } catch (e) { setErr((e as Error)?.message || "Could not submit the code"); }
    finally { setSubmitting(false); }
  }
  async function retry() {
    setErr("");
    await api.post(`/integrations/${instanceId}/provision`, {}).catch(() => {});
    await poll();
  }

  const state = prov?.provision_state || "starting";
  const steps = prov?.steps || ["Connecting to your controller", "Verify your identity", "Securing an API key"];
  const curStep = prov?.step ?? 0;
  const done = !!prov?.done;
  const error = !!prov?.error;
  const needsOtp = !!prov?.needs_otp;

  function stepStatus(i: number): "done" | "active" | "error" | "pending" {
    if (done) return "done";
    if (error && i === curStep) return "error";
    if (i < curStep) return "done";
    if (i === curStep) return "active";
    return "pending";
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-panel" style={{ maxWidth: 480 }} onClick={(e) => e.stopPropagation()}>
        <div className="spread">
          <div>
            <h3 style={{ margin: 0 }}>Setting up {label}</h3>
            <div className="faint" style={{ fontSize: 12 }}>Securely connecting through your appliance…</div>
          </div>
          <button className="btn ghost sm" onClick={onClose}><Icon name="logout" size={14} /></button>
        </div>
        <div className="modal-body">
          <div className="stack" style={{ gap: 0 }}>
            {steps.map((label2, i) => {
              const s = stepStatus(i);
              return (
                <div key={i} className="row" style={{ gap: 12, alignItems: "flex-start", padding: "10px 0" }}>
                  <div style={{ width: 26, height: 26, borderRadius: "50%", display: "grid", placeItems: "center",
                                flexShrink: 0, background:
                                  s === "done" ? "#2dbe60" : s === "error" ? "var(--danger-c,#f2545b)"
                                  : s === "active" ? "#4f7cff" : "var(--inset)",
                                color: s === "pending" ? "var(--text-faint)" : "#fff" }}>
                    {s === "done" ? <Icon name="check" size={14} />
                      : s === "error" ? <Icon name="alert" size={14} />
                      : s === "active" ? <span className="spinner-dot" />
                      : <span style={{ fontSize: 12 }}>{i + 1}</span>}
                  </div>
                  <div style={{ flex: 1, paddingTop: 3 }}>
                    <div style={{ fontWeight: 600, fontSize: 13.5,
                                  color: s === "pending" ? "var(--text-faint)" : "var(--text)" }}>{label2}</div>
                    {i === curStep && prov?.message && (
                      <div className="faint" style={{ fontSize: 12, marginTop: 2 }}>{prov.message}</div>
                    )}
                  </div>
                </div>
              );
            })}
          </div>

          {needsOtp && !done && (
            <div style={{ marginTop: 10, borderTop: "1px solid var(--border-soft)", paddingTop: 14 }}>
              <div style={{ fontWeight: 600, fontSize: 13, marginBottom: 6 }}>Enter your verification code</div>
              {err && <div style={{ color: "var(--danger-c,#f2545b)", fontSize: 12, marginBottom: 8 }}>{err}</div>}
              <div className="row" style={{ gap: 8 }}>
                <input className="input" autoFocus inputMode="numeric" placeholder="123456"
                       value={otp} onChange={(e) => setOtp(e.target.value)}
                       onKeyDown={(e) => { if (e.key === "Enter") void submitOtp(); }}
                       style={{ letterSpacing: 3, fontSize: 16, flex: 1 }} />
                <button className="btn primary sm" disabled={submitting || !otp.trim()} onClick={() => void submitOtp()}>
                  {submitting ? "Verifying…" : "Verify"}
                </button>
              </div>
            </div>
          )}

          {done && (
            <div className="row" style={{ gap: 8, alignItems: "center", marginTop: 10, color: "#2dbe60" }}>
              <Icon name="check" size={16} /> <b>Connected.</b>
              <span className="faint" style={{ fontSize: 12.5 }}>Data will start flowing on the next poll.</span>
            </div>
          )}
          {error && (
            <div style={{ marginTop: 10 }}>
              <div style={{ color: "var(--danger-c,#f2545b)", fontSize: 12.5, marginBottom: 8 }}>
                {prov?.message || "Setup didn't complete."}
              </div>
            </div>
          )}
        </div>
        <div className="modal-foot">
          <div style={{ flex: 1 }} />
          {done ? (
            <button className="btn primary sm" onClick={onDone}>Finish</button>
          ) : error ? (
            <>
              <button className="btn sm" onClick={onClose}>Close</button>
              <button className="btn primary sm" onClick={() => void retry()}>Try again</button>
            </>
          ) : (
            <button className="btn sm" onClick={onClose}>Cancel</button>
          )}
        </div>
      </div>
    </div>
  );
}

function AppsTable({ apps, onChanged }: { apps: NetApp[]; onChanged: () => void }) {
  const nav = useNavigate();
  const max = Math.max(1, ...apps.map((a) => a.total_bytes));
  async function toggleInterest(a: NetApp) {
    try { await api.post(`/integrations/apps/interest`, { app_key: a.app_key, of_interest: !a.of_interest }); onChanged(); }
    catch { /* ignore */ }
  }
  if (apps.length === 0) return <div className="muted" style={{ padding: 8 }}>No apps observed yet.</div>;
  return (
    <table className="table">
      <thead><tr><th>App / service</th><th>Category</th><th>Traffic</th><th>Clients</th><th></th><th></th></tr></thead>
      <tbody>
        {apps.map((a) => (
          <tr key={a.app_key}>
            <td>
              <div style={{ fontWeight: 600 }}>{a.name}</div>
              <div style={{ height: 4, background: "var(--inset)", borderRadius: 3, marginTop: 3, width: 120 }}>
                <div style={{ height: "100%", width: `${(a.total_bytes / max) * 100}%`,
                              background: "#4f7cff", borderRadius: 3 }} />
              </div>
            </td>
            <td className="faint" style={{ fontSize: 12 }}>{a.category || "—"}</td>
            <td>{bytes(a.total_bytes)}</td>
            <td className="faint">{a.client_count}</td>
            <td>
              {a.source_type
                ? <Pill tone="ok"><Icon name="check" size={11} /> Source</Pill>
                : <button className="btn ghost sm" onClick={() => nav("/connectors")}>No source</button>}
            </td>
            <td style={{ textAlign: "right" }}>
              <button className="btn ghost sm" title="Mark as an app of interest"
                      onClick={() => void toggleInterest(a)}>
                <Icon name={a.of_interest ? "check" : "sparkle"} size={13} /> {a.of_interest ? "Tracked" : "Track"}
              </button>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function ClientsTable({ clients, onChanged }: { clients: NetClient[]; onChanged: () => void }) {
  async function setState_(c: NetClient, monitor_state: string) {
    try { await api.post(`/integrations/clients/${c.id}`, { monitor_state }); onChanged(); }
    catch (e) { notify({ message: (e as Error).message, tone: "danger" }); }
  }
  async function toggleInterest(c: NetClient) {
    try { await api.post(`/integrations/clients/${c.id}`, { of_interest: !c.of_interest }); onChanged(); }
    catch { /* ignore */ }
  }
  if (clients.length === 0) return <div className="muted" style={{ padding: 8 }}>No clients observed yet.</div>;
  const dtIcon = (t: string): IconName =>
    t === "phone" ? "user" : t === "media" ? "activity" : t === "iot" ? "database" : "server";
  return (
    <table className="table">
      <thead><tr><th>Device</th><th>Type</th><th>IP</th><th>Traffic</th><th>Monitoring</th><th></th></tr></thead>
      <tbody>
        {clients.map((c) => (
          <tr key={c.id} style={{ opacity: c.monitor_state === "ignored" ? 0.5 : 1 }}>
            <td>
              <div className="row" style={{ gap: 8, alignItems: "center" }}>
                <Icon name={dtIcon(c.device_type)} size={14} />
                <div>
                  <div style={{ fontWeight: 600 }}>{c.name}</div>
                  <div className="faint" style={{ fontSize: 11 }}>{c.mac}{c.is_guest ? " · guest" : ""}</div>
                </div>
              </div>
            </td>
            <td className="faint" style={{ fontSize: 12 }}>{c.device_type || "—"}</td>
            <td className="faint" style={{ fontSize: 12 }}>{c.ip || "—"}</td>
            <td>{bytes(c.total_bytes)}</td>
            <td>
              <select className="input sm" value={c.monitor_state}
                      onChange={(e) => void setState_(c, e.target.value)} style={{ width: 130 }}>
                <option value="normal">Normal</option>
                <option value="monitored">Monitor (family)</option>
                <option value="ignored">Ignore</option>
              </select>
            </td>
            <td style={{ textAlign: "right" }}>
              <button className="btn ghost sm" title="Mark as a client of interest"
                      onClick={() => void toggleInterest(c)}>
                <Icon name={c.of_interest ? "check" : "sparkle"} size={13} />
              </button>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
