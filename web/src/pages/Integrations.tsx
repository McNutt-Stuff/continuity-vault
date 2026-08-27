import { Fragment, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { Card, Pill, bytes, Loading, groupScope } from "../components/ui";
import { Icon, IconName } from "../components/Icon";
import { SourceIcon } from "../components/SourceIcon";
import { notify, confirmDialog, promptDialog } from "../components/dialog";

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
  health: string;
  poll_interval_minutes: number; host: string; last_run_at: string | null;
  last_success_at: string | null; last_error: string | null;
  provision_state?: string; provision_message?: string | null;
  last_stats: { clients?: number; apps?: number; bytes_seen?: number; note?: string };
}
interface ApplianceRef { id: string; name: string; state: string; online: boolean; }
interface ListResp { available: Spec[]; instances: Instance[]; appliances: ApplianceRef[]; plan: string; }

interface NetClient {
  id: string; name: string; device_name: string; nickname: string;
  hostname: string; ip: string; mac: string;
  device_type: string; is_wired: boolean; is_guest: boolean;
  monitor_state: string; of_interest: boolean; ownership: string; owner_user_id: string | null;
  total_bytes: number; last_seen: string | null;
}
interface NetApp {
  app_key: string; name: string; category: string; source_type: string;
  of_interest: boolean; total_bytes: number; client_count: number; last_seen: string | null;
}
interface ShadowSource { source_type: string; name: string; total_bytes: number; apps: number; }
interface DataResp {
  clients: NetClient[]; apps: NetApp[]; shadow: ShadowSource[];
  stats: { clients?: number; monitored?: number; ignored?: number; apps?: number; bytes?: number;
           mine?: number; family?: number; organization?: number };
}
// Relationship drill-down rows (which clients use an app / which apps a client uses).
interface UsageClient { client_key: string; id: string | null; name: string; device_type: string; ip: string; mac: string; monitor_state: string; total_bytes: number; }
interface UsageApp { app_key: string; name: string; category: string; source_type: string; total_bytes: number; }

const asIcon = (n: string): IconName => (n || "puzzle") as IconName;
const HEALTH: Record<string, { tone: "ok" | "warn" | "info" | "danger"; label: string; dot: string }> = {
  ok: { tone: "ok", label: "Healthy", dot: "#2dbe60" },
  error: { tone: "danger", label: "Failing", dot: "#f2545b" },
  stale: { tone: "warn", label: "Stale", dot: "#f5a623" },
  pending: { tone: "info", label: "Waiting for first run", dot: "#4f7cff" },
  setup: { tone: "info", label: "Setting up", dot: "#4f7cff" },
  paused: { tone: "warn", label: "Paused", dot: "#8a94a6" },
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


export default function Integrations() {
  const [list, setList] = useState<ListResp | null>(null);
  const [loading, setLoading] = useState(true);
  const [setupSpec, setSetupSpec] = useState<Spec | null>(null);
  const [detailId, setDetailId] = useState<string | null>(null);
  const [showAdd, setShowAdd] = useState(false);

  async function load() {
    try { setList(await api.get<ListResp>("/integrations")); }
    catch { /* ignore */ }
    finally { setLoading(false); }
  }
  useEffect(() => { void load(); }, []);

  const specByType = useMemo(() => {
    const m: Record<string, Spec> = {};
    for (const s of (list?.available || [])) m[s.integration_type] = s;
    return m;
  }, [list]);

  if (loading) return <Loading label="Loading integrations…" />;

  const detailInst = detailId ? list?.instances.find((i) => i.id === detailId) : null;
  if (detailId && detailInst) {
    return <IntegrationDetail inst={detailInst} spec={specByType[detailInst.integration_type]}
                              plan={list?.plan || ""}
                              onBack={() => { setDetailId(null); void load(); }}
                              onChanged={load} />;
  }

  return (
    <>
      <div className="spread" style={{ marginBottom: 18, alignItems: "center", gap: 12, flexWrap: "wrap" }}>
        <div className="stack">
          <h2 style={{ margin: 0 }}>Integrations</h2>
          <div className="faint" style={{ fontSize: 12.5, maxWidth: 620 }}>
            Unlock intelligence about your environment — the apps and services in use, who's using
            them, and where your data really lives. Integrations don't back up data; they inform it.
          </div>
        </div>
        <button className="btn primary" onClick={() => setShowAdd(true)}>
          <Icon name="link" size={15} /> Add integration
        </button>
      </div>

      {/* Enabled integrations */}
      {list && list.instances.length > 0 ? (
        <div className="insights-cards" style={{ marginBottom: 20 }}>
          {list.instances.map((i) => (
            <InstanceCard key={i.id} inst={i} spec={specByType[i.integration_type]}
                          onOpen={() => setDetailId(i.id)} onChanged={load} />
          ))}
        </div>
      ) : (
        <Card>
          <div className="stack" style={{ alignItems: "center", gap: 10, padding: "28px 12px", textAlign: "center" }}>
            <div className="insight-card-ic" style={{ background: "#0559c91e", color: "#0559c9", width: 48, height: 48 }}>
              <Icon name="puzzle" size={22} />
            </div>
            <div style={{ fontWeight: 700, fontSize: 15 }}>No integrations yet</div>
            <div className="faint" style={{ fontSize: 12.5, maxWidth: 420 }}>
              Connect a device like your UniFi controller to see the apps and services on your
              network and where your data really lives.
            </div>
            <button className="btn primary sm" onClick={() => setShowAdd(true)}>
              <Icon name="link" size={13} /> Add your first integration
            </button>
          </div>
        </Card>
      )}

      {showAdd && (
        <AddIntegrationModal available={list?.available || []}
                             hasAppliance={(list?.appliances || []).length > 0}
                             onClose={() => setShowAdd(false)}
                             onPick={(s) => { setShowAdd(false); setSetupSpec(s); }} />
      )}

      {setupSpec && (
        <SetupModal spec={setupSpec} appliances={list?.appliances || []}
                    onClose={() => setSetupSpec(null)}
                    onDone={() => { setSetupSpec(null); void load(); }} />
      )}
    </>
  );
}

// Catalog modal (mirrors the Sources page): pick an integration to set up.
function AddIntegrationModal({ available, hasAppliance, onClose, onPick }: {
  available: Spec[]; hasAppliance: boolean; onClose: () => void; onPick: (s: Spec) => void;
}) {
  const [query, setQuery] = useState("");
  const q = query.trim().toLowerCase();
  const shown = available.filter((s) =>
    !q || s.display_name.toLowerCase().includes(q) || s.category.toLowerCase().includes(q)
    || s.description.toLowerCase().includes(q));
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-panel" style={{ width: "min(880px, 100%)" }} onClick={(e) => e.stopPropagation()}>
        <div className="spread">
          <div>
            <h3 style={{ margin: 0 }}>Add an integration</h3>
            <div className="faint" style={{ fontSize: 12, maxWidth: 520 }}>
              Integrations gather intelligence about your environment. They run on your appliance or
              in the cloud and never move your data.
            </div>
          </div>
          <button className="btn ghost sm" onClick={onClose}>Close</button>
        </div>
        <div className="modal-body" style={{ maxHeight: "72vh", overflow: "auto" }}>
          <input className="input sm" placeholder="Search integrations…" value={query}
                 onChange={(e) => setQuery(e.target.value)}
                 style={{ marginBottom: 14, width: "100%" }} />
          <div className="grid grid-3">
            {shown.map((s) => {
              const locked = s.needs_appliance && !hasAppliance;
              return (
              <div key={s.integration_type}
                   className="dest-card"
                   style={locked ? { opacity: 0.55, cursor: "not-allowed" } : undefined}
                   title={locked ? "Requires an appliance on your network" : undefined}
                   onClick={() => { if (!locked) onPick(s); }}>
                <div className="spread" style={{ marginBottom: 10 }}>
                  <div className="row" style={{ gap: 10, alignItems: "center" }}>
                    <div className="insight-card-ic" style={{ background: `${s.color}1e`, color: s.color, width: 34, height: 34 }}>
                      <SourceIcon type={s.integration_type} fallback={asIcon(s.icon)} size={19} />
                    </div>
                    <div>
                      <div style={{ fontWeight: 650 }}>{s.display_name}</div>
                      <div className="faint" style={{ fontSize: 11.5 }}>{s.category}</div>
                    </div>
                  </div>
                  <Pill tone="info">{s.runs_on === "appliance" ? "Appliance" : "Cloud"}</Pill>
                </div>
                <div className="faint" style={{ fontSize: 12, lineHeight: 1.45 }}>{s.description}</div>
                {locked && (
                  <div style={{ fontSize: 11.5, color: "var(--warn)", marginTop: 8, display: "flex", gap: 6, alignItems: "center" }}>
                    <Icon name="alert" size={12} /> Needs an appliance on your network
                  </div>
                )}
              </div>
              );
            })}
            {shown.length === 0 && <div className="muted">No integrations match “{query}”.</div>}
          </div>
        </div>
      </div>
    </div>
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

function ShadowChip({ s, onDetail }: { s: ShadowSource; onDetail: () => void }) {
  const nav = useNavigate();
  return (
    <div className="row" style={{ gap: 8, alignItems: "center", border: "1px solid var(--border-soft)",
          borderRadius: 10, padding: "8px 12px" }}>
      <div className="stack" style={{ gap: 0 }}>
        <div style={{ fontWeight: 600, fontSize: 13 }}>{s.name}</div>
        <div className="faint" style={{ fontSize: 11 }}>{bytes(s.total_bytes)} · {s.apps} app{s.apps === 1 ? "" : "s"}</div>
      </div>
      <button className="btn ghost sm" onClick={onDetail}>Who's using it</button>
      <button className="btn sm" onClick={() => nav("/connectors")}>Connect</button>
    </div>
  );
}

// Popup: which devices/users are driving an unprotected (shadow) service.
function ShadowDetailModal({ iid, s, onClose }: { iid: string; s: ShadowSource; onClose: () => void }) {
  const nav = useNavigate();
  const [clients, setClients] = useState<UsageClient[] | null>(null);
  useEffect(() => {
    api.get<{ clients: UsageClient[] }>(`/integrations/${iid}/usage?source_type=${encodeURIComponent(s.source_type)}`)
      .then((r) => setClients(r.clients || [])).catch(() => setClients([]));
  }, [iid, s.source_type]);
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-panel" style={{ maxWidth: 520 }} onClick={(e) => e.stopPropagation()}>
        <div className="spread">
          <div>
            <h3 style={{ margin: 0 }}>{s.name}</h3>
            <div className="faint" style={{ fontSize: 12 }}>
              Devices using this service — its data isn't protected until you connect it as a source.
            </div>
          </div>
          <button className="btn ghost sm" onClick={onClose}><Icon name="logout" size={14} /></button>
        </div>
        <div className="modal-body" style={{ maxHeight: "60vh", overflow: "auto" }}>
          {clients === null ? <Loading label="Loading…" />
            : clients.length === 0 ? <div className="muted" style={{ padding: 8 }}>No per-device detail available yet.</div>
            : (
              <table className="table">
                <thead><tr><th>Device</th><th>IP</th><th>Traffic</th></tr></thead>
                <tbody>
                  {clients.map((c) => (
                    <tr key={c.client_key} style={{ opacity: c.monitor_state === "ignored" ? 0.5 : 1 }}>
                      <td>
                        <div style={{ fontWeight: 600 }}>{c.name}</div>
                        <div className="faint" style={{ fontSize: 11 }}>{c.mac}</div>
                      </td>
                      <td className="faint" style={{ fontSize: 12 }}>{c.ip || "—"}</td>
                      <td>{bytes(c.total_bytes)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
        </div>
        <div className="modal-foot">
          <div style={{ flex: 1 }} />
          <button className="btn sm" onClick={onClose}>Close</button>
          <button className="btn primary sm" onClick={() => nav("/connectors")}>
            <Icon name="link" size={13} /> Connect source
          </button>
        </div>
      </div>
    </div>
  );
}


function InstanceCard({ inst, spec, onOpen, onChanged }: {
  inst: Instance; spec?: Spec; onOpen: () => void; onChanged: () => void;
}) {
  void spec;
  const [resuming, setResuming] = useState(false);
  const st = inst.last_stats || {};
  const provisioning = !!inst.provision_state && !["idle", "done"].includes(inst.provision_state);
  const h = HEALTH[inst.health] || HEALTH.pending;
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
        <div className="row" style={{ gap: 6, alignItems: "center" }} title={h.label}>
          <Pill tone={h.tone} dot>{h.label}</Pill>
        </div>
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
      <div className="row" style={{ gap: 16, fontSize: 12.5, marginBottom: 6 }}>
        <span className="faint">Clients <b style={{ color: "var(--text)" }}>{st.clients ?? "—"}</b></span>
        <span className="faint">Apps <b style={{ color: "var(--text)" }}>{st.apps ?? "—"}</b></span>
        <span className="faint">Seen <b style={{ color: "var(--text)" }}>{st.bytes_seen ? bytes(st.bytes_seen) : "—"}</b></span>
      </div>
      <div className="faint" style={{ fontSize: 11, marginBottom: 10 }}>
        {inst.last_run_at ? `Last checked ${fmtAgo(inst.last_run_at)}` : "Not collected yet"}
      </div>
      <div className="row" style={{ gap: 8, marginTop: "auto" }}>
        {!provisioning && (
          <button className="btn sm" onClick={onOpen}>
            <Icon name="search" size={13} /> Details
          </button>
        )}
      </div>
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
function IntegrationDetail({ inst, spec, plan, onBack, onChanged }: {
  inst: Instance; spec?: Spec; plan: string; onBack: () => void; onChanged: () => void;
}) {
  const [data, setData] = useState<DataResp | null>(null);
  const [tab, setTab] = useState<"apps" | "clients">("apps");
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState(false);
  const [repolling, setRepolling] = useState(false);
  const [shadowDetail, setShadowDetail] = useState<ShadowSource | null>(null);

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
              <Pill tone={(HEALTH[inst.health] || HEALTH.pending).tone} dot>
                {(HEALTH[inst.health] || HEALTH.pending).label}
              </Pill>
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
        <div className="row" style={{ gap: 8, alignItems: "center", marginBottom: 12,
              border: "1px solid var(--danger-c,#f2545b)", borderRadius: 8, padding: "8px 12px" }}>
          <Icon name="alert" size={15} />
          <div className="stack" style={{ gap: 1, flex: 1 }}>
            <span style={{ color: "var(--danger-c,#f2545b)", fontSize: 12.5 }}>{inst.last_error}</span>
            <span className="faint" style={{ fontSize: 11 }}>
              Last checked {fmtAgo(inst.last_run_at)}
              {inst.last_success_at ? ` · last succeeded ${fmtAgo(inst.last_success_at)}` : " · never succeeded"}
            </span>
          </div>
        </div>
      )}
      {!inst.last_error && inst.health === "stale" && (
        <div className="faint" style={{ fontSize: 12, marginBottom: 12 }}>
          <Icon name="alert" size={13} /> No fresh data — last successful collection {fmtAgo(inst.last_success_at)}.
        </div>
      )}

      <div className="insights-stats" style={{ marginBottom: 16 }}>
        <MiniStat icon="user" label="Clients seen" value={String(stats.clients || 0)} tint="#4f7cff" />
        <MiniStat icon="shield" label="My devices" value={String(stats.mine || 0)} tint="#2dbe60" />
        {groupScope(plan) && (
          <MiniStat icon="grid" label={groupScope(plan)!.label}
                    value={String(groupScope(plan)!.value === "family" ? (stats.family || 0) : (stats.organization || 0))}
                    tint="#35d0a5" />
        )}
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
            so anything living only there isn't recoverable. Open one to see which devices use it.
          </div>
          <div className="row" style={{ gap: 10, flexWrap: "wrap" }}>
            {data.shadow.map((s) => (
              <ShadowChip key={s.source_type} s={s} onDetail={() => setShadowDetail(s)} />
            ))}
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
          ? <AppsTable iid={inst.id} apps={data?.apps || []} onChanged={loadData} />
          : <ClientsTable iid={inst.id} plan={plan} clients={data?.clients || []} onChanged={loadData} />}
      </Card>

      {shadowDetail && (
        <ShadowDetailModal iid={inst.id} s={shadowDetail} onClose={() => setShadowDetail(null)} />
      )}

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

function AppsTable({ iid, apps, onChanged }: { iid: string; apps: NetApp[]; onChanged: () => void }) {
  const nav = useNavigate();
  const [open, setOpen] = useState<string | null>(null);
  const max = Math.max(1, ...apps.map((a) => a.total_bytes));
  async function toggleInterest(a: NetApp) {
    try { await api.post(`/integrations/apps/interest`, { app_key: a.app_key, of_interest: !a.of_interest }); onChanged(); }
    catch { /* ignore */ }
  }
  if (apps.length === 0) return <div className="muted" style={{ padding: 8 }}>No apps observed yet.</div>;
  return (
    <table className="table">
      <thead><tr><th style={{ width: 24 }}></th><th>App / service</th><th>Category</th><th>Traffic</th><th>Clients</th><th></th><th></th></tr></thead>
      <tbody>
        {apps.map((a) => {
          const isOpen = open === a.app_key;
          return (
            <Fragment key={a.app_key}>
              <tr>
                <td>
                  <button className="btn ghost sm" title="Show devices using this"
                          style={{ padding: "2px 8px" }}
                          onClick={() => setOpen(isOpen ? null : a.app_key)}>
                    <span style={{ display: "inline-block", fontSize: 10,
                          transform: isOpen ? "rotate(90deg)" : "none", transition: "transform .15s" }}>▶</span>
                  </button>
                </td>
                <td>
                  <div style={{ fontWeight: 600 }}>{a.name}</div>
                  <div style={{ height: 4, background: "var(--inset)", borderRadius: 3, marginTop: 3, width: 120 }}>
                    <div style={{ height: "100%", width: `${(a.total_bytes / max) * 100}%`,
                                  background: "#4f7cff", borderRadius: 3 }} />
                  </div>
                </td>
                <td className="faint" style={{ fontSize: 12 }}>{a.category || "—"}</td>
                <td>{bytes(a.total_bytes)}</td>
                <td>
                  <button className="btn ghost sm" style={{ padding: "2px 8px" }}
                          onClick={() => setOpen(isOpen ? null : a.app_key)}>{a.client_count} ▾</button>
                </td>
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
              {isOpen && (
                <tr className="drill-row">
                  <td></td>
                  <td colSpan={6}><UsageClients iid={iid} appKey={a.app_key} /></td>
                </tr>
              )}
            </Fragment>
          );
        })}
      </tbody>
    </table>
  );
}

// app → the devices driving it
function UsageClients({ iid, appKey }: { iid: string; appKey: string }) {
  const [rows, setRows] = useState<UsageClient[] | null>(null);
  useEffect(() => {
    api.get<{ clients: UsageClient[] }>(`/integrations/${iid}/usage?app_key=${encodeURIComponent(appKey)}`)
      .then((r) => setRows(r.clients || [])).catch(() => setRows([]));
  }, [iid, appKey]);
  if (rows === null) return <div className="faint" style={{ padding: 8, fontSize: 12 }}>Loading devices…</div>;
  if (rows.length === 0) return <div className="muted" style={{ padding: 8, fontSize: 12 }}>No per-device detail available.</div>;
  return (
    <div className="stack" style={{ gap: 2, padding: "6px 4px" }}>
      {rows.map((c) => (
        <div key={c.client_key} className="row" style={{ gap: 12, fontSize: 12.5, padding: "3px 0", alignItems: "center" }}>
          <span style={{ fontWeight: 600, minWidth: 200 }}>{c.name}
            {c.monitor_state === "ignored" && <span className="faint"> · ignored</span>}</span>
          <span className="faint" style={{ minWidth: 130 }}>{c.ip || c.mac}</span>
          <span className="faint">{bytes(c.total_bytes)}</span>
        </div>
      ))}
    </div>
  );
}

// client → the apps it uses
function UsageApps({ iid, clientKey }: { iid: string; clientKey: string }) {
  const [rows, setRows] = useState<UsageApp[] | null>(null);
  useEffect(() => {
    api.get<{ apps: UsageApp[] }>(`/integrations/${iid}/usage?client_key=${encodeURIComponent(clientKey)}`)
      .then((r) => setRows(r.apps || [])).catch(() => setRows([]));
  }, [iid, clientKey]);
  if (rows === null) return <div className="faint" style={{ padding: 8, fontSize: 12 }}>Loading apps…</div>;
  if (rows.length === 0) return <div className="muted" style={{ padding: 8, fontSize: 12 }}>No per-app detail available.</div>;
  return (
    <div className="stack" style={{ gap: 2, padding: "6px 4px" }}>
      {rows.map((a) => (
        <div key={a.app_key} className="row" style={{ gap: 12, fontSize: 12.5, padding: "3px 0", alignItems: "center" }}>
          <span style={{ fontWeight: 600, minWidth: 200 }}>{a.name}</span>
          <span className="faint" style={{ minWidth: 130 }}>{a.category || "—"}</span>
          <span className="faint">{bytes(a.total_bytes)}</span>
          {a.source_type && <Pill tone="ok"><Icon name="check" size={10} /> protected</Pill>}
        </div>
      ))}
    </div>
  );
}


function ClientsTable({ iid, plan, clients, onChanged }: { iid: string; plan: string; clients: NetClient[]; onChanged: () => void }) {
  const [open, setOpen] = useState<string | null>(null);
  const group = groupScope(plan);  // null for personal accounts → only "Me"
  async function setState_(c: NetClient, monitor_state: string) {
    try { await api.post(`/integrations/clients/${c.id}`, { monitor_state }); onChanged(); }
    catch (e) { notify({ message: (e as Error).message, tone: "danger" }); }
  }
  async function setOwnership(c: NetClient, ownership: string) {
    try { await api.post(`/integrations/clients/${c.id}`, { ownership }); onChanged(); }
    catch (e) { notify({ message: (e as Error).message, tone: "danger" }); }
  }
  async function renameDevice(c: NetClient) {
    const nickname = await promptDialog({
      title: "Nickname this device", label: "Nickname",
      message: `${c.device_name} · ${c.mac}`,
      defaultValue: c.nickname || "", confirmLabel: "Save",
    });
    if (nickname == null) return;
    try { await api.post(`/integrations/clients/${c.id}`, { nickname: nickname.trim() }); onChanged(); }
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
      <thead><tr><th style={{ width: 24 }}></th><th>Device</th><th>Belongs to</th><th>IP</th><th>Traffic</th><th>Monitoring</th><th></th></tr></thead>
      <tbody>
        {clients.map((c) => {
          const isOpen = open === (c.mac || c.id);
          const key = c.mac || c.id;
          return (
            <Fragment key={c.id}>
              <tr style={{ opacity: c.monitor_state === "ignored" ? 0.5 : 1 }}>
                <td>
                  <button className="btn ghost sm" title="Show apps this device uses"
                          style={{ padding: "2px 8px" }}
                          onClick={() => setOpen(isOpen ? null : key)}>
                    <span style={{ display: "inline-block", fontSize: 10,
                          transform: isOpen ? "rotate(90deg)" : "none", transition: "transform .15s" }}>▶</span>
                  </button>
                </td>
                <td>
                  <div className="row" style={{ gap: 8, alignItems: "center" }}>
                    <Icon name={dtIcon(c.device_type)} size={14} />
                    <div className="flex1">
                      <div className="row" style={{ gap: 6, alignItems: "center" }}>
                        <span style={{ fontWeight: 600 }}>{c.name}</span>
                        <button className="btn ghost sm" title="Set a nickname" style={{ padding: "1px 5px" }}
                                onClick={() => void renameDevice(c)}>
                          <Icon name="edit" size={11} />
                        </button>
                      </div>
                      <div className="faint" style={{ fontSize: 11 }}>
                        {c.nickname ? `${c.device_name} · ` : ""}{c.mac}{c.is_guest ? " · guest" : ""}
                      </div>
                    </div>
                  </div>
                </td>
                <td>
                  <select className="input sm" value={c.ownership || ""}
                          onChange={(e) => void setOwnership(c, e.target.value)} style={{ width: 130 }}>
                    <option value="">Unassigned</option>
                    <option value="personal">Me</option>
                    {group && <option value={group.value}>{group.label}</option>}
                    {/* keep a stored scope visible even if it doesn't match the plan */}
                    {c.ownership && c.ownership !== "personal" && c.ownership !== group?.value && (
                      <option value={c.ownership}>{c.ownership === "family" ? "My family" : "My organization"}</option>
                    )}
                  </select>
                </td>
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
              {isOpen && (
                <tr className="drill-row">
                  <td></td>
                  <td colSpan={6}><UsageApps iid={iid} clientKey={key} /></td>
                </tr>
              )}
            </Fragment>
          );
        })}
      </tbody>
    </table>
  );
}

