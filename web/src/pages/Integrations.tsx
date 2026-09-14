import { Fragment, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { useAuth } from "../auth";
import { Card, Pill, bytes, Loading, groupScope } from "../components/ui";
import { Icon, IconName } from "../components/Icon";
import { SourceIcon } from "../components/SourceIcon";
import { AreaChart, Sparkline } from "../components/charts";
import { notify, confirmDialog, promptDialog } from "../components/dialog";

interface CredField { name: string; label: string; type: string; placeholder: string; required: boolean; help: string; }
interface Spec {
  integration_type: string; display_name: string; description: string; icon: string;
  color: string; category: string; runs_on: string; needs_appliance: boolean;
  default_interval_minutes: number; auto_provision_key: boolean; provides: string[];
  credential_fields: CredField[];
  // Packaged-integration metadata + per-caller entitlement (server-authoritative).
  version?: string; status?: string; plans?: string[]; min_plan?: string;
  capabilities?: string[]; ownership_models?: string[]; managed?: boolean; workspace?: boolean;
  docs_slug?: string; entitled?: boolean; locked_reason?: string; available_to_setup?: boolean;
}
interface Instance {
  id: string; integration_type: string; display_name: string; label: string;
  enabled: boolean; runs_on: string; appliance_id: string | null; status: string;
  health: string;
  poll_interval_minutes: number; host: string; site?: string; last_run_at: string | null;
  last_success_at: string | null; last_error: string | null;
  provision_state?: string; provision_message?: string | null;
  last_stats: { clients?: number; apps?: number; bytes_seen?: number; note?: string;
    diag?: { site?: string; auth_mode?: string; devices_http?: number | string | null; traffic_http?: number | string | null } };
  m365?: { consent_state?: string; needs_consent?: boolean; identities_discovered?: number;
    identities_mapped?: number; managed_sources?: number; protected_objects?: number; collect_enabled?: boolean };
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
  empty: { tone: "warn", label: "No data", dot: "#f5a623" },
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
  const [m365Open, setM365Open] = useState<string | null>(null);

  async function load() {
    try {
      const base = await api.get<ListResp>("/integrations");
      // Managed integrations (Microsoft 365) are control-plane authoritative; the
      // generic list is proxied to the node and can lag replication, so a just-
      // added instance may be missing. Merge the CP's copy so it shows at once.
      const m365Spec = (base.available || []).find(
        (s) => s.integration_type === "microsoft365" && s.entitled !== false);
      if (m365Spec) {
        try {
          const r = await api.get<{ instances: Instance[] }>("/integrations/microsoft365/instances");
          const cpIds = new Set((r.instances || []).map((i) => i.id));
          // The CP is authoritative for Microsoft 365: drop node-replicated copies
          // it no longer reports (a removed instance lingers in the node's list
          // because replication is additive), then overlay the CP's own rows.
          const byId = new Map<string, Instance>();
          for (const i of base.instances || [])
            if (i.integration_type !== "microsoft365" || cpIds.has(i.id)) byId.set(i.id, i);
          for (const i of r.instances || []) byId.set(i.id, i);  // CP copy wins
          base.instances = Array.from(byId.values());
        } catch { /* not entitled / offline — fall back to the base list */ }
      }
      setList(base);
    }
    catch { /* ignore */ }
    finally { setLoading(false); }
  }
  useEffect(() => { void load(); }, []);

  // Surface the outcome of the Microsoft admin-consent redirect (Microsoft sends
  // the browser back to /integrations?m365=connected|error|denied). Without this
  // the redirect landed silently and the integration appeared to never connect.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const m = params.get("m365");
    if (!m) return;
    if (m === "connected") {
      notify({ message: "Microsoft 365 connected — administrator consent granted.", tone: "ok" });
      void load();
    } else if (m === "denied") {
      notify({ message: "Microsoft 365 consent was denied or cancelled in the Microsoft window.", tone: "warn" });
    } else if (m === "error") {
      notify({ message: "Microsoft 365 consent couldn't be recorded. Please open Microsoft 365 and try Re-authorize again — it re-opens the Microsoft consent screen.", tone: "danger" });
    }
    params.delete("m365");
    const qs = params.toString();
    window.history.replaceState({}, "", window.location.pathname + (qs ? `?${qs}` : ""));
  }, []);

  const specByType = useMemo(() => {
    const m: Record<string, Spec> = {};
    for (const s of (list?.available || [])) m[s.integration_type] = s;
    return m;
  }, [list]);

  if (loading) return <Loading label="Loading integrations…" />;

  if (m365Open) {
    return <M365Workspace spec={specByType["microsoft365"]} instanceId={m365Open}
                          onBack={() => { setM365Open(null); void load(); }} />;
  }

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
                          onOpen={() => { if (i.integration_type === "microsoft365") setM365Open(i.id); else setDetailId(i.id); }} onChanged={load} />
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
                             addedCounts={(list?.instances || []).reduce((acc, i) => { acc[i.integration_type] = (acc[i.integration_type] || 0) + 1; return acc; }, {} as Record<string, number>)}
                             onClose={() => setShowAdd(false)}
                             onPick={(s) => { setShowAdd(false); if (s.integration_type === "microsoft365") setM365Open("new"); else setSetupSpec(s); }} />
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
function AddIntegrationModal({ available, hasAppliance, addedCounts, onClose, onPick }: {
  available: Spec[]; hasAppliance: boolean; addedCounts: Record<string, number>; onClose: () => void; onPick: (s: Spec) => void;
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
              const comingSoon = s.status && s.status !== "ga";
              const notEntitled = s.entitled === false;
              const applianceLocked = s.needs_appliance && !hasAppliance;
              // Informational only — Add always starts a new configuration, even
              // for managed integrations (an org may connect several tenants).
              const connectedCount = addedCounts[s.integration_type] || 0;
              // A managed workspace integration (e.g. Microsoft 365) can be opened
              // in preview once entitled — the workspace itself gates each step.
              const openable = !!s.workspace && !notEntitled && !applianceLocked;
              const locked = applianceLocked || notEntitled || (!!comingSoon && !openable);
              const lockMsg = notEntitled ? (s.locked_reason || "Not available on your plan")
                : comingSoon ? (s.status === "preview" ? "Preview — coming soon" : "Coming soon")
                : applianceLocked ? "Needs an appliance on your network" : "";
              return (
              <div key={s.integration_type}
                   className="dest-card"
                   style={{ display: "flex", flexDirection: "column", minHeight: 150,
                            ...(locked ? { opacity: 0.62, cursor: "not-allowed" } : {}) }}
                   title={lockMsg || undefined}
                   onClick={() => { if (!locked) onPick(s); }}>
                <div className="spread" style={{ marginBottom: 10 }}>
                  <div className="row" style={{ gap: 10, alignItems: "center" }}>
                    <div className="insight-card-ic" style={{ background: `${s.color}1e`, color: s.color, width: 34, height: 34 }}>
                      <SourceIcon type={s.integration_type} fallback={asIcon(s.icon)} size={19} />
                    </div>
                    <div>
                      <div className="row" style={{ gap: 6, alignItems: "center" }}>
                        <div style={{ fontWeight: 650 }}>{s.display_name}</div>
                        {s.managed && <Pill tone="info">Managed</Pill>}
                        {connectedCount > 0 && <Pill tone="ok">{connectedCount} connected</Pill>}
                      </div>
                      <div className="faint" style={{ fontSize: 11.5 }}>{s.category}</div>
                    </div>
                  </div>
                  <div className="row" style={{ gap: 6, alignItems: "center" }}>
                    {s.min_plan && <Pill tone="warn">{s.min_plan[0].toUpperCase() + s.min_plan.slice(1)}</Pill>}
                    {comingSoon && openable && <Pill tone="info">Preview</Pill>}
                    <Pill tone="info">{s.runs_on === "appliance" ? "Appliance" : "Cloud"}</Pill>
                  </div>
                </div>
                <div className="faint" style={{ fontSize: 12, lineHeight: 1.45, flex: 1,
                     display: "-webkit-box", WebkitLineClamp: 3, WebkitBoxOrient: "vertical", overflow: "hidden" }}>{s.description}</div>
                {locked && (
                  <div style={{ fontSize: 11.5, color: "var(--warn)", marginTop: 8, display: "flex", gap: 6, alignItems: "center" }}>
                    <Icon name={comingSoon ? "clock" : "alert"} size={12} /> {lockMsg}
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

// ---- Time-series trends -------------------------------------------------- //
interface TrendPoint { day: string; bytes: number }
interface TrendEntity {
  key: string; name: string; total_bytes: number; prev_bytes: number;
  change_pct: number | null; series: TrendPoint[];
  category?: string; source_type?: string; device_type?: string;
}
interface NetAnalytics {
  window: string; days: number; series: TrendPoint[];
  top_apps: TrendEntity[]; top_clients: TrendEntity[];
  summary: { total_bytes: number; prev_total_bytes: number; change_pct: number | null;
    active_apps: number; active_clients: number; avg_daily_bytes: number };
}

const TREND_WINDOWS: { value: string; label: string }[] = [
  { value: "7d", label: "7 days" }, { value: "30d", label: "30 days" }, { value: "90d", label: "90 days" },
];

export function WindowSelect({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  return (
    <div className="row" style={{ gap: 4 }}>
      {TREND_WINDOWS.map((w) => (
        <button key={w.value} className={`chip ${value === w.value ? "active" : ""}`}
                style={{ fontSize: 12 }} onClick={() => onChange(w.value)}>{w.label}</button>
      ))}
    </div>
  );
}

export function TrendPill({ pct }: { pct: number | null }) {
  if (pct == null) return <span className="faint" style={{ fontSize: 11 }}>new</span>;
  const up = pct >= 0;
  return (
    <span style={{ fontSize: 11, fontWeight: 600, color: up ? "#4f7cff" : "var(--muted-c,#8a94a7)" }}>
      {up ? "▲" : "▼"} {Math.abs(pct)}%
    </span>
  );
}

function TrendList({ title, rows, kind }: { title: string; rows: TrendEntity[]; kind: "app" | "client" }) {
  return (
    <div>
      <h3 style={{ fontSize: 14, margin: "0 0 8px" }}>{title}</h3>
      {rows.length === 0 ? <div className="muted" style={{ fontSize: 12 }}>No data in this period yet.</div> : (
        <div className="stack" style={{ gap: 2 }}>
          {rows.map((r) => (
            <div key={r.key} className="row" style={{ gap: 10, alignItems: "center", padding: "6px 0",
                  borderBottom: "1px solid var(--border-soft)" }}>
              <div className="flex1" style={{ minWidth: 0 }}>
                <div style={{ fontWeight: 600, fontSize: 12.5, whiteSpace: "nowrap", overflow: "hidden",
                      textOverflow: "ellipsis" }}>{r.name}</div>
                <div className="faint" style={{ fontSize: 11 }}>
                  {(kind === "app" ? (r.category || "app") : (r.device_type || "device"))} · {bytes(r.total_bytes)}
                  {kind === "app" && r.source_type ? ` · ${r.source_type}` : ""}
                </div>
              </div>
              <div style={{ width: 88, flexShrink: 0 }}>
                <Sparkline data={r.series.map((p) => p.bytes)} color="#4f7cff" height={26} />
              </div>
              <div style={{ width: 52, textAlign: "right", flexShrink: 0 }}><TrendPill pct={r.change_pct} /></div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function TrendsPanel({ iid }: { iid: string }) {
  const [win, setWin] = useState("30d");
  const [d, setD] = useState<NetAnalytics | null>(null);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    let live = true;
    setLoading(true);
    api.get<NetAnalytics>(`/integrations/${iid}/analytics?window=${win}`)
      .then((r) => { if (live) setD(r); })
      .catch(() => { /* ignore */ })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [iid, win]);
  const labels = (d?.series || []).map((p) => p.day.slice(5));
  return (
    <div>
      <div className="spread" style={{ marginBottom: 12, alignItems: "center", gap: 10, flexWrap: "wrap" }}>
        <div className="faint" style={{ fontSize: 12.5 }}>
          Traffic over time — trailing-24h volume, sampled daily (kept 90 days).
        </div>
        <WindowSelect value={win} onChange={setWin} />
      </div>
      {loading && !d ? <div className="muted">Loading trends…</div>
        : !d || d.series.every((p) => !p.bytes) ? (
          <div className="muted" style={{ fontSize: 12.5 }}>
            No trend data yet — trends build up as the integration collects each day.
          </div>
        ) : (
        <>
          <div className="insights-stats" style={{ marginBottom: 14 }}>
            <div className="insights-stat">
              <div className="insights-stat-ic" style={{ background: "#f5a62322", color: "#f5a623" }}>
                <Icon name="cloud" size={16} />
              </div>
              <div className="stack" style={{ gap: 1 }}>
                <div className="row" style={{ gap: 6, alignItems: "baseline" }}>
                  <div style={{ fontSize: 17, fontWeight: 700 }}>{bytes(d.summary.total_bytes || 0)}</div>
                  <TrendPill pct={d.summary.change_pct} />
                </div>
                <div className="faint" style={{ fontSize: 11.5 }}>Traffic this period</div>
              </div>
            </div>
            <MiniStat icon="activity" label="Average per day" value={bytes(d.summary.avg_daily_bytes || 0)} tint="#4f7cff" />
            <MiniStat icon="grid" label="Active apps" value={String(d.summary.active_apps || 0)} tint="#c56cf0" />
            <MiniStat icon="user" label="Active devices" value={String(d.summary.active_clients || 0)} tint="#2dbe60" />
          </div>
          <div style={{ marginBottom: 16 }}>
            <AreaChart series={[{ name: "traffic", color: "#4f7cff", data: d.series.map((p) => p.bytes) }]}
                       labels={labels} unit="B" fmt={bytes} height={200} />
          </div>
          <div className="grid grid-2" style={{ gap: 20 }}>
            <TrendList title="Top apps & services" rows={d.top_apps} kind="app" />
            <TrendList title="Top devices" rows={d.top_clients} kind="client" />
          </div>
        </>
      )}
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
  const [resuming, setResuming] = useState(false);
  const [removing, setRemoving] = useState(false);
  const st = inst.last_stats || {};
  const provisioning = !!inst.provision_state && !["idle", "done"].includes(inst.provision_state);
  // Managed/workspace integrations (e.g. Microsoft 365) resume setup in their own
  // workspace — NOT the appliance ProvisioningModal.
  const isWorkspace = inst.integration_type === "microsoft365" || !!spec?.workspace;
  const h = HEALTH[inst.health] || HEALTH.pending;
  const clickable = !provisioning || isWorkspace;

  async function removeInst() {
    const ok = await confirmDialog({
      title: `Remove ${inst.label}?`,
      message: "This purges the integration configuration and stops collection. Any "
        + "protected recovery points are retained under your retention policy.",
      confirmLabel: "Remove", tone: "danger",
    });
    if (!ok) return;
    setRemoving(true);
    try {
      if (inst.integration_type === "microsoft365")
        await api.post(`/integrations/microsoft365/remove?instance_id=${encodeURIComponent(inst.id)}`, {});
      else
        await api.del(`/integrations/${inst.id}`);
      notify({ message: `${inst.label} removed`, tone: "ok" });
      onChanged();
    } catch (e) {
      notify({ message: (e as { message?: string }).message || "Couldn't remove the integration", tone: "danger" });
    } finally { setRemoving(false); }
  }

  return (
    <Card className="insight-card">
      <div className="row" style={{ gap: 10, alignItems: "center", marginBottom: 8, cursor: clickable ? "pointer" : "default" }}
           onClick={() => { if (clickable) onOpen(); }}>
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
          <button className="btn primary sm" onClick={() => { if (isWorkspace) onOpen(); else setResuming(true); }}>Continue setup</button>
        </div>
      )}
      {inst.last_error && !provisioning && (
        <div style={{ color: "var(--danger-c,#f2545b)", fontSize: 12, marginBottom: 6 }}>{inst.last_error}</div>
      )}
      {inst.integration_type === "microsoft365" ? (
        <>
          {inst.m365?.needs_consent && !inst.last_error && (
            <div style={{ color: "var(--warn,#f5a623)", fontSize: 12, marginBottom: 6 }}>
              More admin consent needed — open to re-grant.
            </div>
          )}
          <div className="row" style={{ gap: 16, fontSize: 12.5, marginBottom: 6 }}>
            <span className="faint">Identities <b style={{ color: "var(--text)" }}>{inst.m365?.identities_discovered ?? "—"}</b></span>
            <span className="faint">Mapped <b style={{ color: "var(--text)" }}>{inst.m365?.identities_mapped ?? "—"}</b></span>
            <span className="faint">Sources <b style={{ color: "var(--text)" }}>{inst.m365?.managed_sources ?? "—"}</b></span>
            <span className="faint">Protected <b style={{ color: "var(--text)" }}>{inst.m365?.protected_objects?.toLocaleString() ?? "—"}</b></span>
          </div>
        </>
      ) : (
        <div className="row" style={{ gap: 16, fontSize: 12.5, marginBottom: 6 }}>
          <span className="faint">Clients <b style={{ color: "var(--text)" }}>{st.clients ?? "—"}</b></span>
          <span className="faint">Apps <b style={{ color: "var(--text)" }}>{st.apps ?? "—"}</b></span>
          <span className="faint">Seen <b style={{ color: "var(--text)" }}>{st.bytes_seen ? bytes(st.bytes_seen) : "—"}</b></span>
        </div>
      )}
      <div className="faint" style={{ fontSize: 11, marginBottom: 10 }}>
        {inst.last_run_at ? `Last checked ${fmtAgo(inst.last_run_at)}` : "Not collected yet"}
      </div>
      <div className="row" style={{ gap: 8, marginTop: "auto" }}>
        {!provisioning && (
          <button className="btn sm" onClick={onOpen}>
            <Icon name={isWorkspace ? "link" : "search"} size={13} /> {isWorkspace ? "Open" : "Details"}
          </button>
        )}
        <button className="btn sm ghost" disabled={removing} onClick={removeInst}
                style={{ marginLeft: "auto", color: "var(--danger-c,#f2545b)" }}>
          <Icon name="trash" size={13} /> {removing ? "Removing…" : "Remove"}
        </button>
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
  const [tab, setTab] = useState<"trends" | "apps" | "clients" | "advanced">("trends");
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState(false);
  const [repolling, setRepolling] = useState(false);
  const [shadowDetail, setShadowDetail] = useState<ShadowSource | null>(null);
  const { me } = useAuth();
  const advanced = !!me?.features?.advanced_ubiquiti_analytics;
  const [reauth, setReauth] = useState(false);

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
              {inst.site ? ` · site ${inst.site}` : ""}
              {lastRun ? ` · last run ${lastRun}` : ""}
            </div>
          </div>
        </div>
        <div className="row" style={{ gap: 8 }}>
          <button className="btn sm" disabled={repolling} onClick={() => void repoll()}>
            {repolling ? <><span className="spinner-dot" /> Re-polling…</> : <><Icon name="repeat" size={13} /> Re-poll</>}
          </button>
          <button className="btn ghost sm" onClick={() => setEditing(true)}><Icon name="edit" size={13} /> Edit</button>
          {spec?.auto_provision_key && (
            <button className="btn ghost sm" onClick={() => setReauth(true)} title="Re-run sign-in (with 2-factor) to mint a fresh API key">
              <Icon name="key" size={13} /> Re-authenticate
            </button>
          )}
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
      {!inst.last_error && inst.health === "empty" && (
        <div className="row" style={{ gap: 8, alignItems: "flex-start", marginBottom: 12,
              border: "1px solid var(--warn,#f5a623)", borderRadius: 8, padding: "8px 12px" }}>
          <Icon name="alert" size={15} />
          <div className="stack" style={{ gap: 3, flex: 1 }}>
            <span style={{ fontSize: 12.5 }}>
              Connected, but the last runs collected <b>no devices</b>.
              {inst.last_stats?.note ? ` ${inst.last_stats.note}` : ""}
            </span>
            {inst.last_stats?.diag && (
              <span className="faint" style={{ fontSize: 11 }}>
                site <b>{inst.last_stats.diag.site || "?"}</b> · auth {inst.last_stats.diag.auth_mode || "?"}
                {inst.last_stats.diag.devices_http != null ? ` · devices API HTTP ${inst.last_stats.diag.devices_http}` : ""}
                {inst.last_stats.diag.traffic_http != null ? ` · traffic API HTTP ${inst.last_stats.diag.traffic_http}` : ""}
              </span>
            )}
            {spec?.auto_provision_key && (
              <div className="row" style={{ gap: 8, marginTop: 2 }}>
                <button className="btn sm" onClick={() => setReauth(true)}><Icon name="key" size={12} /> Re-authenticate (2FA)</button>
                <button className="btn ghost sm" disabled={repolling} onClick={() => void repoll()}>Re-poll</button>
              </div>
            )}
          </div>
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
          <button className={`chip ${tab === "trends" ? "active" : ""}`} onClick={() => setTab("trends")}>
            Trends
          </button>
          <button className={`chip ${tab === "apps" ? "active" : ""}`} onClick={() => setTab("apps")}>
            Apps & services
          </button>
          <button className={`chip ${tab === "clients" ? "active" : ""}`} onClick={() => setTab("clients")}>
            Clients & devices
          </button>
          {advanced && (
            <button className={`chip ${tab === "advanced" ? "active" : ""}`} onClick={() => setTab("advanced")}>
              <Icon name="grid" size={12} /> Advanced analytics
            </button>
          )}
        </div>
        {tab === "trends"
          ? <TrendsPanel iid={inst.id} />
          : tab === "apps"
          ? <AppsTable iid={inst.id} apps={data?.apps || []} onChanged={loadData} />
          : tab === "clients"
          ? <ClientsTable iid={inst.id} plan={plan} clients={data?.clients || []} onChanged={loadData} />
          : <AdvancedPanel />}
      </Card>

      {shadowDetail && (
        <ShadowDetailModal iid={inst.id} s={shadowDetail} onClose={() => setShadowDetail(null)} />
      )}
      {reauth && (
        <ProvisioningModal instanceId={inst.id} label={inst.label}
                           onClose={() => setReauth(false)}
                           onDone={() => { setReauth(false); void loadData(); onChanged(); }} />
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
  const [site, setSite] = useState(inst.site || "");
  const [interval, setIntervalM] = useState(inst.poll_interval_minutes);
  const [secrets, setSecrets] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState("");
  // Host + site are non-secret config we pre-fill; everything else is a secret
  // the user only re-enters to change (shown blank).
  const secretFields = (spec?.credential_fields || []).filter((f) => f.type !== "host" && f.name !== "site");
  const hasSite = (spec?.credential_fields || []).some((f) => f.name === "site");

  async function save() {
    setSaving(true); setErr("");
    try {
      const filled = Object.fromEntries(Object.entries(secrets).filter(([, v]) => v));
      const hostChanged = host.trim() !== (inst.host || "");
      const siteChanged = hasSite && site.trim() !== (inst.site || "");
      const body: Record<string, unknown> = { label, poll_interval_minutes: interval };
      if (hostChanged || siteChanged || Object.keys(filled).length > 0) {
        body.credentials = { host: host.trim(), ...(hasSite ? { site: site.trim() } : {}), ...filled };
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
          {hasSite && (
            <label className="stack" style={{ marginBottom: 12 }}>
              <span className="faint" style={{ fontSize: 11.5 }}>Site name</span>
              <input className="input" value={site} placeholder="default (auto-detected)"
                     onChange={(e) => setSite(e.target.value)} />
              <span className="faint" style={{ fontSize: 11 }}>Leave blank to auto-detect the UniFi site short-name.</span>
            </label>
          )}
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
  const [members, setMembers] = useState<{ id: string; name: string }[]>([]);
  const group = groupScope(plan);  // null for personal accounts → only "Me"
  useEffect(() => {
    // Org members to map a device to a specific person (evolution of Me / My Family).
    api.get<{ users: { id: string; display_name?: string; email: string }[] }>("/org/users")
      .then((r) => setMembers((r.users || []).map((u) => ({ id: u.id, name: u.display_name || u.email }))))
      .catch(() => setMembers([]));
  }, []);
  async function setState_(c: NetClient, monitor_state: string) {
    try { await api.post(`/integrations/clients/${c.id}`, { monitor_state }); onChanged(); }
    catch (e) { notify({ message: (e as Error).message, tone: "danger" }); }
  }
  async function setOwnership(c: NetClient, ownership: string) {
    try { await api.post(`/integrations/clients/${c.id}`, { ownership }); onChanged(); }
    catch (e) { notify({ message: (e as Error).message, tone: "danger" }); }
  }
  async function assignMember(c: NetClient, owner_user_id: string) {
    try { await api.post(`/integrations/clients/${c.id}`, { owner_user_id }); onChanged(); }
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
                  {members.length > 1 && (
                    <select className="input sm" value={c.owner_user_id || ""} title="Assign this device to a specific person"
                            onChange={(e) => void assignMember(c, e.target.value)} style={{ width: 130, marginTop: 4 }}>
                      <option value="">— person —</option>
                      {members.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
                    </select>
                  )}
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


// ---- Advanced Ubiquiti analytics (feature-flag gated) ----------------------
interface AdvGaps { days: number; collected_days: number; missing_days: string[]; missing_count: number; last_data_day: string | null; stale: boolean }
interface AdvUser { user_id: string; name: string; devices: number; stale_devices: number; window_bytes: number; last_seen: string | null; gap: boolean }
interface AdvOrg { window: string; days: number; users: AdvUser[]; unassigned: { devices: number; bytes: number }; assigned_devices: number; member_count: number; gaps: AdvGaps }
interface AdvDevice { id: string; name: string; device_type: string; ip: string; mac: string; total_bytes: number; last_seen: string | null; stale: boolean; monitor_state: string }
interface AdvApp { app_key: string; name: string; category: string; source_type: string; total_bytes: number }
interface AdvUserDetail { user_id: string; name: string; window: string; days: number; series: { day: string; bytes: number }[]; total_bytes: number; device_count: number; app_count: number; devices: AdvDevice[]; apps: AdvApp[]; categories: string[]; gaps: AdvGaps }

const ADV_WINDOWS = ["7d", "30d", "90d"];

function GapBanner({ gaps }: { gaps: AdvGaps }) {
  if (!gaps || (!gaps.stale && gaps.missing_count === 0)) {
    return (
      <div className="faint" style={{ fontSize: 12 }}>
        <Icon name="check" size={13} /> Continuous collection — {gaps?.collected_days}/{gaps?.days} days covered.
      </div>
    );
  }
  return (
    <div className="row" style={{ gap: 8, alignItems: "flex-start", border: "1px solid var(--warn,#f5a623)",
          borderRadius: 8, padding: "8px 12px", marginBottom: 12 }}>
      <Icon name="alert" size={15} />
      <div className="stack" style={{ gap: 2, fontSize: 12.5 }}>
        <span style={{ fontWeight: 600 }}>Collection gaps detected</span>
        <span className="faint">
          {gaps.collected_days}/{gaps.days} days have data
          {gaps.last_data_day ? ` · last data ${gaps.last_data_day}` : " · no data in window"}
          {gaps.missing_count ? ` · ${gaps.missing_count} missing day(s)` : ""}.
          Data may be missed for some people/devices — check the debug integrations diagnostic.
        </span>
      </div>
    </div>
  );
}

function AdvancedPanel() {
  const [window, setWindow] = useState("30d");
  const [org, setOrg] = useState<AdvOrg | null>(null);
  const [uid, setUid] = useState<string | null>(null);
  const [remapping, setRemapping] = useState(false);
  async function load() {
    try { setOrg(await api.get<AdvOrg>(`/integrations/advanced/org?window=${window}`)); }
    catch { /* ignore */ }
  }
  useEffect(() => { void load(); /* eslint-disable-next-line */ }, [window]);
  async function remap() {
    setRemapping(true);
    try {
      const r = await api.post<{ remapped: number; candidates: string[] }>("/integrations/remap-apps", {});
      notify({ message: `Re-mapped ${r.remapped} app(s) to sources.${r.candidates.length ? ` Candidates: ${r.candidates.slice(0, 5).join(", ")}` : ""}`, tone: "info" });
    } catch (e) { notify({ message: (e as Error).message, tone: "danger" }); }
    finally { setRemapping(false); }
  }
  if (uid) return <AdvancedUserPanel uid={uid} onBack={() => setUid(null)} />;
  if (!org) return <div className="muted" style={{ padding: 8 }}>Loading advanced analytics…</div>;
  return (
    <div className="stack" style={{ gap: 14 }}>
      <div className="spread" style={{ alignItems: "center", flexWrap: "wrap", gap: 8 }}>
        <div className="faint" style={{ fontSize: 12.5, maxWidth: 480 }}>
          Per-person network usage across all your integrations — map devices to people to close gaps.
        </div>
        <div className="row" style={{ gap: 6, alignItems: "center" }}>
          {ADV_WINDOWS.map((w) => (
            <button key={w} className={`chip ${window === w ? "active" : ""}`} onClick={() => setWindow(w)}>{w}</button>
          ))}
          <button className="btn ghost sm" disabled={remapping} onClick={() => void remap()} title="Re-map observed apps to Arkive sources">
            <Icon name="repeat" size={12} /> {remapping ? "Mapping…" : "Re-map apps"}
          </button>
        </div>
      </div>

      <GapBanner gaps={org.gaps} />

      <div className="insights-stats">
        <MiniStat icon="user" label="Members" value={String(org.member_count)} tint="#4f7cff" />
        <MiniStat icon="shield" label="Mapped devices" value={String(org.assigned_devices)} tint="#2dbe60" />
        <MiniStat icon="alert" label="Unmapped devices" value={String(org.unassigned.devices)} tint="#f5a623" />
        <MiniStat icon="cloud" label="Unmapped traffic" value={bytes(org.unassigned.bytes)} tint="#c56cf0" />
      </div>

      {org.unassigned.devices > 0 && (
        <div className="faint" style={{ fontSize: 12 }}>
          <Icon name="info" size={13} /> {org.unassigned.devices} device(s) aren't mapped to a person — assign them under
          <b> Clients &amp; devices</b> so their traffic is attributed and no one's usage is missed.
        </div>
      )}

      {org.users.length === 0 ? (
        <div className="muted" style={{ padding: 8 }}>No devices mapped to people yet. Assign devices to members to see per-person analytics.</div>
      ) : (
        <table className="table">
          <thead><tr><th>Member</th><th>Devices</th><th>Traffic ({org.window})</th><th>Last seen</th><th></th></tr></thead>
          <tbody>
            {org.users.map((u) => (
              <tr key={u.user_id} style={{ cursor: "pointer" }} onClick={() => setUid(u.user_id)}>
                <td style={{ fontWeight: 600 }}>{u.name}</td>
                <td>{u.devices}{u.stale_devices ? <span className="faint"> · {u.stale_devices} stale</span> : ""}</td>
                <td>{bytes(u.window_bytes)}</td>
                <td className="faint" style={{ fontSize: 12 }}>{u.last_seen ? fmtAgo(u.last_seen) : "—"}</td>
                <td style={{ textAlign: "right" }}>
                  {u.gap ? <Pill tone="warn" dot>gap</Pill> : <span className="faint" style={{ fontSize: 16 }}>›</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function AdvancedUserPanel({ uid, onBack }: { uid: string; onBack: () => void }) {
  const [window, setWindow] = useState("30d");
  const [cat, setCat] = useState("");
  const [d, setD] = useState<AdvUserDetail | null>(null);
  useEffect(() => {
    const qs = `window=${window}${cat ? `&category=${encodeURIComponent(cat)}` : ""}`;
    api.get<AdvUserDetail>(`/integrations/advanced/users/${uid}?${qs}`).then(setD).catch(() => setD(null));
  }, [uid, window, cat]);
  const dtIcon = (t: string): IconName =>
    t === "phone" ? "user" : t === "media" ? "activity" : t === "iot" ? "database" : "server";
  const series = d?.series || [];
  const chartData = series.map((p) => p.bytes || 0);
  const chartLabels = series.map((p) => { const dt = new Date(p.day.endsWith("Z") ? p.day : `${p.day}T00:00:00Z`); return `${dt.getMonth() + 1}/${dt.getDate()}`; });
  return (
    <div className="stack" style={{ gap: 14 }}>
      <div className="spread" style={{ alignItems: "center", flexWrap: "wrap", gap: 8 }}>
        <button className="btn ghost sm" onClick={onBack}>← All members</button>
        <div className="row" style={{ gap: 6 }}>
          {ADV_WINDOWS.map((w) => (
            <button key={w} className={`chip ${window === w ? "active" : ""}`} onClick={() => setWindow(w)}>{w}</button>
          ))}
        </div>
      </div>
      {!d ? <div className="muted" style={{ padding: 8 }}>Loading…</div> : (
        <>
          <h3 style={{ margin: 0 }}>{d.name}</h3>
          <GapBanner gaps={d.gaps} />
          <div className="insights-stats">
            <MiniStat icon="server" label="Devices" value={String(d.device_count)} tint="#4f7cff" />
            <MiniStat icon="activity" label="Apps" value={String(d.app_count)} tint="#c56cf0" />
            <MiniStat icon="cloud" label={`Traffic (${d.window})`} value={bytes(d.total_bytes)} tint="#f5a623" />
          </div>
          {chartData.some((x) => x > 0) && (
            <Card><AreaChart height={180} unit="" fmt={bytes} labels={chartLabels}
                             series={[{ name: "traffic", color: "#4f7cff", data: chartData }]} /></Card>
          )}

          <div className="stack" style={{ gap: 6 }}>
            <span style={{ fontWeight: 600, fontSize: 13 }}>Devices</span>
            {d.devices.length === 0 ? <div className="muted" style={{ fontSize: 12.5 }}>No devices mapped to this person.</div> : (
              <table className="table">
                <thead><tr><th>Device</th><th>Type</th><th>IP</th><th>Traffic</th><th>Last seen</th></tr></thead>
                <tbody>
                  {d.devices.map((dev) => (
                    <tr key={dev.id} style={{ opacity: dev.monitor_state === "ignored" ? 0.5 : 1 }}>
                      <td><div className="row" style={{ gap: 6, alignItems: "center" }}><Icon name={dtIcon(dev.device_type)} size={13} /> {dev.name}</div></td>
                      <td className="faint" style={{ fontSize: 12 }}>{dev.device_type || "—"}</td>
                      <td className="faint" style={{ fontSize: 12 }}>{dev.ip || "—"}</td>
                      <td>{bytes(dev.total_bytes)}</td>
                      <td className="faint" style={{ fontSize: 12 }}>
                        {dev.last_seen ? fmtAgo(dev.last_seen) : "—"} {dev.stale && <Pill tone="warn">stale</Pill>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>

          <div className="stack" style={{ gap: 6 }}>
            <div className="spread" style={{ alignItems: "center" }}>
              <span style={{ fontWeight: 600, fontSize: 13 }}>Apps &amp; services</span>
              {d.categories.length > 0 && (
                <select className="input sm" value={cat} onChange={(e) => setCat(e.target.value)} style={{ width: 170 }}>
                  <option value="">All categories</option>
                  {d.categories.map((c) => <option key={c} value={c}>{c}</option>)}
                </select>
              )}
            </div>
            {d.apps.length === 0 ? <div className="muted" style={{ fontSize: 12.5 }}>No app traffic for this person{cat ? " in this category" : ""}.</div> : (
              <table className="table">
                <thead><tr><th>App</th><th>Category</th><th>Source</th><th>Traffic</th></tr></thead>
                <tbody>
                  {d.apps.map((a) => (
                    <tr key={a.app_key}>
                      <td style={{ fontWeight: 600 }}>{a.name}</td>
                      <td className="faint" style={{ fontSize: 12 }}>{a.category || "—"}</td>
                      <td>{a.source_type
                        ? <Pill tone="ok"><SourceIcon type={a.source_type} size={12} /> {a.source_type}</Pill>
                        : <span className="faint" style={{ fontSize: 12 }}>—</span>}</td>
                      <td>{bytes(a.total_bytes)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Microsoft 365 managed workspace — connect, admin consent, Entra discovery    //
// and identity mapping. Content collection is a later phase.                   //
// --------------------------------------------------------------------------- //
interface M365Status {
  connected: boolean; state?: string; status?: string; consent_state?: string;
  instance_id?: string;
  microsoft_tenant_id?: string; scopes_granted?: string[];
  identities_discovered?: number; identities_mapped?: number; identities_suggested?: number;
  managed_sources?: number; auto_map?: boolean; auto_create?: boolean;
  last_run_at?: string | null;
  last_error?: string | null; permissions_ok?: boolean | null;
  needs_consent?: boolean; last_checked_at?: string | null;
}
interface M365ScopeRules { domains?: string[]; includes?: string[]; excludes?: string[]; include_guests?: boolean; }
interface M365Profile {
  workloads: string[]; destinations: string[]; backup_interval_minutes: number | null;
  workload_catalog?: { id: string; label: string; description?: string }[]; active_sources?: number;
}
interface StorageTarget { id: string; label: string; kind?: string; }
interface M365Identity {
  id: string; display_name: string; upn: string; email: string; entra_object_id: string;
  account_enabled: boolean; user_type: string; in_scope: boolean; state: string; scope_reason?: string;
  binding: { user_id: string | null; status: string; protected_only: boolean; mapping_method: string } | null;
}
interface M365Member { id: string; name: string; email: string; }
interface M365Compliance {
  enabled: boolean;
  managed_collections: { id: string; name: string; source_type: string; workload: string }[];
  rules: { id: string; name: string; enabled: boolean; priority: number; actions: { type: string; value?: string }[]; scoped: boolean }[];
}

function splitLines(v: string): string[] {
  return v.split(/[\n,]/).map((s) => s.trim()).filter(Boolean);
}

const WORKLOAD_LABELS: Record<string, string> = {
  exchange: "Exchange Online", onedrive: "OneDrive", sharepoint: "SharePoint",
  teams: "Teams channels", teams_chat: "Teams chats",
};

function ScopeField({ label, help, value, onChange }:
  { label: string; help: string; value: string; onChange: (v: string) => void }) {
  return (
    <label className="stack" style={{ gap: 4 }}>
      <span style={{ fontSize: 12, fontWeight: 600 }}>{label}</span>
      <textarea className="input" rows={2} value={value} onChange={(e) => onChange(e.target.value)}
                style={{ fontFamily: "var(--mono, monospace)", fontSize: 12 }} />
      <span className="faint" style={{ fontSize: 11 }}>{help}</span>
    </label>
  );
}

function M365Workspace({ spec, instanceId, onBack }: { spec?: Spec; instanceId: string; onBack: () => void }) {
  // "new" starts a fresh wizard; an id opens/manages that specific instance.
  const [activeId, setActiveId] = useState(instanceId === "new" ? "" : instanceId);
  const [status, setStatus] = useState<M365Status | null>(null);
  const [loading, setLoading] = useState(instanceId !== "new");
  const [busy, setBusy] = useState("");
  const [identities, setIdentities] = useState<M365Identity[]>([]);
  const [members, setMembers] = useState<M365Member[]>([]);
  const [sources, setSources] = useState<{ id: string; workload: string; name: string; ownership_type?: string; state: string; objects?: number; last_error?: string | null; last_collected_at: string | null }[]>([]);
  const [workloadRollup, setWorkloadRollup] = useState<{ workload: string; label: string; sources: number; active: number; objects: number; errors: number }[]>([]);
  const [totalObjects, setTotalObjects] = useState(0);
  const [collectEnabled, setCollectEnabled] = useState(false);
  const [consentState, setConsentState] = useState<{ url?: string; state?: string; configured?: boolean; message?: string } | null>(null);
  const [tenantInput, setTenantInput] = useState("");
  const [scope, setScope] = useState<M365ScopeRules>({});
  const [scopeOpen, setScopeOpen] = useState(false);
  const [profile, setProfile] = useState<M365Profile | null>(null);
  const [targets, setTargets] = useState<StorageTarget[]>([]);
  const [compliance, setCompliance] = useState<M365Compliance | null>(null);
  const nav = useNavigate();

  const iq = activeId ? `instance_id=${encodeURIComponent(activeId)}` : "";

  async function loadStatus() {
    if (!activeId) { setLoading(false); return; }  // fresh wizard — nothing to load yet
    try {
      const s = await api.get<M365Status>(`/integrations/microsoft365?${iq}`);
      setStatus(s);
      if (s.microsoft_tenant_id && !tenantInput) setTenantInput(s.microsoft_tenant_id);
    }
    catch (e) { notify({ message: (e as { message?: string }).message || "Couldn't load Microsoft 365", tone: "danger" }); }
    finally { setLoading(false); }
  }
  async function loadIdentities() {
    if (!activeId) return;
    try {
      const r = await api.get<{ identities: M365Identity[] }>(`/integrations/microsoft365/identities?limit=2000&${iq}`);
      setIdentities(r.identities || []);
      const m = await api.get<{ members: M365Member[] }>("/integrations/microsoft365/members");
      setMembers(m.members || []);
      const s = await api.get<{ collect_enabled: boolean; total_objects?: number; workloads?: { workload: string; label: string; sources: number; active: number; objects: number; errors: number }[]; sources: { id: string; workload: string; name: string; ownership_type?: string; state: string; objects?: number; last_error?: string | null; last_collected_at: string | null }[] }>(`/integrations/microsoft365/sources?${iq}`);
      setSources(s.sources || []); setCollectEnabled(!!s.collect_enabled);
      setWorkloadRollup(s.workloads || []); setTotalObjects(s.total_objects || 0);
      const sc = await api.get<{ rules: M365ScopeRules }>(`/integrations/microsoft365/scope?${iq}`);
      setScope(sc.rules || {});
      const pr = await api.get<M365Profile>(`/integrations/microsoft365/profile?${iq}`);
      setProfile(pr);
      try {
        const cr = await api.get<M365Compliance>(`/integrations/microsoft365/compliance-rules?${iq}`);
        setCompliance(cr);
      } catch { /* rules engine off / not entitled */ }
      try {
        const tg = await api.get<StorageTarget[]>("/tenant/storage-targets");
        setTargets(tg || []);
      } catch { /* targets optional */ }
    } catch { /* ignore */ }
  }
  useEffect(() => { void loadStatus(); }, [activeId]);
  useEffect(() => { if (status?.consent_state === "granted") void loadIdentities(); }, [status?.consent_state]);

  async function saveProfile(next: Partial<M365Profile>) {
    try {
      const r = await api.put<M365Profile>(`/integrations/microsoft365/profile?${iq}`, {
        workloads: next.workloads, destinations: next.destinations,
        backup_interval_minutes: next.backup_interval_minutes,
      });
      setProfile((p) => ({ ...(p || { workloads: [], destinations: [] }), ...r }));
      await loadIdentities();
      notify({ message: "Managed protection profile saved.", tone: "ok" });
    } catch (e) { notify({ message: (e as { message?: string }).message || "Couldn't save the profile", tone: "danger" }); }
  }

  async function saveScope(next: M365ScopeRules) {
    try {
      await api.put(`/integrations/microsoft365/scope?${iq}`, { rules: next });
      setScope(next);
      notify({ message: "Scope saved — re-running discovery to apply.", tone: "ok" });
      await discover();
    } catch (e) { notify({ message: (e as { message?: string }).message || "Couldn't save scope", tone: "danger" }); }
  }
  async function saveSettings(next: { auto_map?: boolean; auto_create?: boolean }) {
    try {
      const r = await api.put<{ auto_map: boolean; auto_create: boolean }>(`/integrations/microsoft365/settings?${iq}`, next);
      setStatus((s) => s ? { ...s, auto_map: r.auto_map, auto_create: r.auto_create } : s);
    } catch (e) { notify({ message: (e as { message?: string }).message || "Couldn't save settings", tone: "danger" }); }
  }
  async function acceptAllSuggestions() {
    setBusy("accept");
    try {
      const r = await api.post<{ accepted: number }>(`/integrations/microsoft365/identities/accept-suggestions?${iq}`, {});
      notify({ message: r.accepted ? `Accepted ${r.accepted} suggested mapping(s)` : "No suggestions to accept", tone: "ok" });
      await loadIdentities(); await loadStatus();
    } catch (e) { notify({ message: (e as { message?: string }).message || "Couldn't accept suggestions", tone: "danger" }); }
    finally { setBusy(""); }
  }

  async function connect() {
    setBusy("connect");
    try {
      const r = await api.post<M365Status>("/integrations/microsoft365/connect", { capabilities: ["entra_directory"] });
      if (r.instance_id) setActiveId(r.instance_id);
      setStatus(r);
    }
    catch (e) { notify({ message: (e as { message?: string }).message || "Connect failed", tone: "danger" }); }
    finally { setBusy(""); }
  }
  async function startConsent() {
    setBusy("consent");
    try {
      const r = await api.post<{ consent_configured: boolean; consent_url?: string; state?: string; message?: string }>(`/integrations/microsoft365/oauth/start?${iq}`, {});
      setConsentState({ url: r.consent_url, state: r.state, configured: r.consent_configured, message: r.message });
      if (r.consent_configured && r.consent_url) window.open(r.consent_url, "_blank", "noopener");
    } catch (e) { notify({ message: (e as { message?: string }).message || "Couldn't start consent", tone: "danger" }); }
    finally { setBusy(""); }
  }
  async function confirmConsent() {
    if (!consentState?.state || !tenantInput.trim()) return;
    setBusy("consent");
    try {
      const r = await api.post<M365Status>(`/integrations/microsoft365/oauth/callback?${iq}`,
        { state: consentState.state, microsoft_tenant_id: tenantInput.trim(), admin_consent: true });
      setStatus(r); setConsentState(null);
      notify({ message: "Microsoft 365 connected", tone: "ok" });
    } catch (e) { notify({ message: (e as { message?: string }).message || "Consent confirmation failed", tone: "danger" }); }
    finally { setBusy(""); }
  }
  async function discover() {
    setBusy("discover");
    try {
      const r = await api.post<M365Status & { discovered?: number; in_scope?: number; queued?: boolean; note?: string }>(`/integrations/microsoft365/discover?${iq}`, {});
      setStatus(r);
      if (r.queued) notify({ message: r.note || "Discovery queued on your node — results appear shortly.", tone: "ok" });
      else notify({ message: `Discovered ${r.discovered ?? 0} identities (${r.in_scope ?? 0} in scope)`, tone: "ok" });
      await loadIdentities();
    } catch (e) { notify({ message: (e as { message?: string }).message || "Discovery failed", tone: "danger" }); }
    finally { setBusy(""); }
  }
  async function decide(idn: M365Identity, value: string) {
    let decision: { external_identity_id: string; action: string; user_id?: string };
    if (value === "protected_only") decision = { external_identity_id: idn.id, action: "protected_only" };
    else if (value === "exclude") decision = { external_identity_id: idn.id, action: "exclude" };
    else if (value === "create_user") decision = { external_identity_id: idn.id, action: "create_user" };
    else if (value.startsWith("map:")) decision = { external_identity_id: idn.id, action: "map", user_id: value.slice(4) };
    else return;
    try {
      await api.post(`/integrations/microsoft365/identities/decisions?${iq}`, { decisions: [decision] });
      await loadIdentities(); await loadStatus();
    } catch (e) { notify({ message: (e as { message?: string }).message || "Couldn't apply mapping", tone: "danger" }); }
  }
  async function toggleCollection(next: boolean) {
    try {
      const r = await api.post<{ collect_enabled: boolean; sources_provisioned: number }>(`/integrations/microsoft365/collection?${iq}`, { enabled: next });
      setCollectEnabled(r.collect_enabled);
      await loadIdentities();
      notify({ message: next ? `Protection enabled — ${r.sources_provisioned} source(s) established` : "Protection paused", tone: "ok" });
    } catch (e) { notify({ message: (e as { message?: string }).message || "Couldn't update protection", tone: "danger" }); }
  }
  async function collectNow() {
    setBusy("collect");
    try {
      const r = await api.post<{ queued?: number; note?: string }>(`/integrations/microsoft365/collect-now?${iq}`, {});
      notify({ message: r.note || (r.queued ? "Backup started." : "Backup queued."), tone: "ok" });
      // Collection runs async (background thread on the CP, or the node worker for
      // node-hosted tenants + replication). Poll a few times so the sources table's
      // "Last collected" and counts update without a manual refresh.
      [4000, 10000, 20000, 35000].forEach((ms) => setTimeout(() => { void loadIdentities(); void loadStatus(); }, ms));
    } catch (e) { notify({ message: (e as { message?: string }).message || "Couldn't start the backup", tone: "danger" }); }
    finally { setBusy(""); }
  }
  async function removeSetup() {
    const ok = await confirmDialog({
      title: "Remove Microsoft 365 setup",
      message: "This deletes the half-finished connection so you can start over. It's only "
        + "available before any data is protected.",
      confirmLabel: "Remove setup",
    });
    if (!ok) return;
    setBusy("remove");
    try {
      await api.post(`/integrations/microsoft365/remove?${iq}`, {});
      notify({ message: "Microsoft 365 setup removed.", tone: "ok" });
      onBack();
    } catch (e) {
      notify({ message: (e as { message?: string }).message || "Couldn't remove the setup", tone: "danger" });
    } finally { setBusy(""); }
  }

  const connected = !!status?.connected;
  const consent = status?.consent_state || "pending";
  const inScope = identities.filter((i) => i.in_scope);

  return (
    <>
      <div className="spread" style={{ marginBottom: 14, alignItems: "center" }}>
        <button className="btn ghost sm" onClick={onBack}>← Integrations</button>
        <div className="row" style={{ gap: 8, alignItems: "center" }}>
          {consent === "granted" && (
            <button className="btn sm ghost" disabled={busy === "discover"} onClick={discover}>
              <Icon name="repeat" size={13} /> {busy === "discover" ? "Checking…" : "Re-check"}
            </button>
          )}
          {connected && (
            <button className="btn sm ghost" disabled={busy === "consent"} onClick={startConsent}
                    title="Re-open the Microsoft admin-consent screen to re-authorize or add permissions">
              <Icon name="key" size={13} /> {busy === "consent" ? "Preparing…" : "Re-authorize"}
            </button>
          )}
          {consent === "granted" && (
            <button className="btn sm primary" disabled={busy === "collect"} onClick={collectNow}>
              <Icon name="cloud" size={13} /> {busy === "collect" ? "Backing up…" : "Back up now"}
            </button>
          )}
          {spec?.status && spec.status !== "ga" && <Pill tone="info">Preview</Pill>}
          <Pill tone="info">Managed</Pill>
        </div>
      </div>

      {/* Re-authorization in progress (triggered from the header, independent of
          any error state) — confirm once the admin approves in the Microsoft tab. */}
      {consent === "granted" && consentState && !(status?.needs_consent || status?.last_error) && (
        <Card style={{ marginBottom: 14, borderColor: "var(--accent,#4f7cff)" }}>
          <div className="row" style={{ gap: 10, alignItems: "flex-start" }}>
            <Icon name="key" size={18} />
            <div className="flex1">
              <div style={{ fontWeight: 700, marginBottom: 2 }}>Re-authorize Microsoft 365</div>
              {consentState.configured === false ? (
                <div className="faint" style={{ fontSize: 12.5 }}>
                  {consentState.message || "The platform Microsoft 365 app isn't configured yet."}
                </div>
              ) : (
                <>
                  <div className="faint" style={{ fontSize: 12.5 }}>
                    A Microsoft consent window opened. Approve the permissions (or add new ones) there,
                    then confirm below. Consent uses <code>.default</code>, so it re-grants exactly the
                    Application permissions configured on the app.
                  </div>
                  <div className="row" style={{ gap: 8, marginTop: 10 }}>
                    <input className="input" style={{ maxWidth: 320 }} placeholder="Directory (tenant) ID"
                           value={tenantInput} onChange={(e) => setTenantInput(e.target.value)} />
                    <button className="btn primary sm" disabled={busy === "consent" || !tenantInput.trim()} onClick={confirmConsent}>
                      {busy === "consent" ? "Confirming…" : "Confirm consent granted"}
                    </button>
                    {consentState.url && <a className="btn ghost sm" href={consentState.url} target="_blank" rel="noreferrer">Reopen consent</a>}
                    <button className="btn ghost sm" onClick={() => setConsentState(null)}>Cancel</button>
                  </div>
                </>
              )}
            </div>
          </div>
        </Card>
      )}

      {/* Consent recorded but the app-only token lacks the granted Application
          roles (403) — surface the exact remediation and a re-consent action. */}
      {consent === "granted" && (status?.needs_consent || status?.last_error) && (
        <Card style={{ marginBottom: 14, borderColor: "var(--warn,#f5a623)" }}>
          <div className="row" style={{ gap: 10, alignItems: "flex-start" }}>
            <Icon name="alert" size={18} />
            <div className="flex1">
              <div style={{ fontWeight: 700, marginBottom: 2 }}>
                {status?.needs_consent ? "More Microsoft consent is needed" : "Microsoft 365 needs attention"}
              </div>
              <div className="faint" style={{ fontSize: 12.5 }}>
                {status?.last_error
                  || "The app-only token doesn't carry the granted Application permissions yet. "
                     + "Re-grant admin consent in Microsoft, then re-check."}
              </div>
              <div className="row" style={{ gap: 8, marginTop: 10 }}>
                <button className="btn sm primary" disabled={busy === "consent"} onClick={startConsent}>
                  <Icon name="link" size={13} /> {busy === "consent" ? "Preparing…" : "Re-request admin consent"}
                </button>
                <button className="btn sm" disabled={busy === "discover"} onClick={discover}>
                  <Icon name="repeat" size={13} /> {busy === "discover" ? "Checking…" : "Re-check permissions"}
                </button>
              </div>
              {consentState && consent === "granted" && (
                <div className="stack" style={{ gap: 8, marginTop: 12, paddingTop: 12, borderTop: "1px solid var(--border-soft)" }}>
                  <div className="faint" style={{ fontSize: 12 }}>
                    A Microsoft consent window opened. After the administrator approves, confirm below.
                  </div>
                  <div className="row" style={{ gap: 8 }}>
                    <input className="input" style={{ maxWidth: 320 }} placeholder="Directory (tenant) ID"
                           value={tenantInput} onChange={(e) => setTenantInput(e.target.value)} />
                    <button className="btn primary sm" disabled={busy === "consent" || !tenantInput.trim()} onClick={confirmConsent}>
                      {busy === "consent" ? "Confirming…" : "Confirm consent granted"}
                    </button>
                    {consentState.url && <a className="btn ghost sm" href={consentState.url} target="_blank" rel="noreferrer">Reopen consent</a>}
                  </div>
                </div>
              )}
            </div>
          </div>
        </Card>
      )}

      <Card style={{ marginBottom: 14 }}>
        <div className="row" style={{ gap: 12, alignItems: "center" }}>
          <div className="insight-card-ic" style={{ background: "#0364B81e", color: "#0364B8", width: 42, height: 42 }}>
            <SourceIcon type="microsoft365" fallback="cloud" size={22} />
          </div>
          <div style={{ flex: 1 }}>
            <h3 style={{ margin: 0 }}>Microsoft 365</h3>
            <div className="faint" style={{ fontSize: 12 }}>
              Turn Microsoft Entra ID into the source for your Arkive organization users —
              administrator-governed, no per-employee sign-in.
            </div>
          </div>
          <Pill tone={consent === "granted" ? "ok" : connected ? "warn" : "info"} dot>
            {consent === "granted" ? "Connected" : connected ? "Consent pending" : "Not connected"}
          </Pill>
        </div>
        {connected && (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(150px,1fr))", gap: 12, marginTop: 14, paddingTop: 14, borderTop: "1px solid var(--border-soft)" }}>
            <MiniStat icon="cloud" label="Microsoft tenant" value={status?.microsoft_tenant_id || "—"} tint="#0364B8" />
            <MiniStat icon="user" label="Identities discovered" value={String(status?.identities_discovered ?? 0)} tint="#3a6df0" />
            <MiniStat icon="check" label="Mapped" value={String(status?.identities_mapped ?? 0)} tint="#35d0a5" />
            <MiniStat icon="link" label="Suggested" value={String(status?.identities_suggested ?? 0)} tint="#f5a623" />
            <MiniStat icon="shield" label="In scope" value={String(inScope.length)} tint="#c56cf0" />
          </div>
        )}
      </Card>

      {loading ? <Loading label="Loading…" /> : !connected ? (
        <Card>
          <h3 style={{ marginTop: 0 }}>Connect Microsoft 365</h3>
          <div className="faint" style={{ fontSize: 12.5, marginBottom: 12, maxWidth: 560 }}>
            Create the organization connection. A Microsoft administrator then grants Arkive
            read access to your directory so we can discover users to protect.
          </div>
          <button className="btn primary" disabled={busy === "connect"} onClick={connect}>
            <Icon name="link" size={14} /> {busy === "connect" ? "Connecting…" : "Connect organization"}
          </button>
        </Card>
      ) : consent !== "granted" ? (
        <Card>
          <h3 style={{ marginTop: 0 }}>Microsoft administrator consent</h3>
          <div className="faint" style={{ fontSize: 12.5, marginBottom: 12, maxWidth: 560 }}>
            A Global Administrator grants Arkive app-only read access to your Microsoft 365
            directory. No employee passwords are ever used.
          </div>
          {!consentState ? (
            <div className="row" style={{ gap: 8, alignItems: "center" }}>
              <button className="btn primary" disabled={busy === "consent"} onClick={startConsent}>
                <Icon name="link" size={14} /> {busy === "consent" ? "Preparing…" : "Get admin consent"}
              </button>
              <button className="btn ghost sm" disabled={busy === "remove"} onClick={removeSetup}>
                {busy === "remove" ? "Removing…" : "Remove setup"}
              </button>
            </div>
          ) : consentState.configured === false ? (
            <div style={{ fontSize: 12.5, color: "var(--warn)" }}>
              <Icon name="alert" size={13} /> {consentState.message || "The platform Microsoft 365 app isn't configured yet."}
            </div>
          ) : (
            <div className="stack" style={{ gap: 10, maxWidth: 460 }}>
              <div className="faint" style={{ fontSize: 12 }}>
                A Microsoft consent window opened in a new tab. After the administrator approves,
                paste your Microsoft <b>Directory (tenant) ID</b> to finish connecting.
              </div>
              <input className="input" placeholder="Directory (tenant) ID" value={tenantInput}
                     onChange={(e) => setTenantInput(e.target.value)} />
              <div className="row" style={{ gap: 8 }}>
                <button className="btn primary sm" disabled={busy === "consent" || !tenantInput.trim()} onClick={confirmConsent}>
                  {busy === "consent" ? "Confirming…" : "Confirm consent granted"}
                </button>
                {consentState.url && <a className="btn ghost sm" href={consentState.url} target="_blank" rel="noreferrer">Reopen consent</a>}
              </div>
            </div>
          )}
        </Card>
      ) : (
        <>
        <Card>
          <div className="spread" style={{ marginBottom: 12, alignItems: "center" }}>
            <div>
              <h3 style={{ margin: 0 }}>Entra identities</h3>
              <div className="faint" style={{ fontSize: 12 }}>
                Map discovered Microsoft users to Arkive members. Mapping doesn't grant portal access.
              </div>
            </div>
            <div className="row" style={{ gap: 8, alignItems: "center" }}>
              <button className="btn ghost sm" onClick={() => setScopeOpen((v) => !v)}>
                <Icon name="shield" size={13} /> Scope
              </button>
              <button className="btn sm" disabled={busy === "discover"} onClick={discover}>
                <Icon name="activity" size={13} /> {busy === "discover" ? "Discovering…" : "Discover users"}
              </button>
            </div>
          </div>

          {scopeOpen && (
            <div className="stack" style={{ gap: 10, marginBottom: 14, padding: 12,
                 border: "1px solid var(--border-soft)", borderRadius: 8 }}>
              <div className="faint" style={{ fontSize: 12 }}>
                Choose which Entra users are in scope. Precedence: exclude &gt; guests &gt; include list &gt;
                domains &gt; default (enabled members). Leave all blank to protect every enabled member.
              </div>
              <ScopeField label="Domains (one per line)" help="Only users in these email domains"
                          value={(scope.domains || []).join("\n")}
                          onChange={(v) => setScope({ ...scope, domains: splitLines(v) })} />
              <ScopeField label="Include (UPN/email or object id)" help="Only these users (overrides domains)"
                          value={(scope.includes || []).join("\n")}
                          onChange={(v) => setScope({ ...scope, includes: splitLines(v) })} />
              <ScopeField label="Exclude (UPN/email or object id)" help="Never in scope"
                          value={(scope.excludes || []).join("\n")}
                          onChange={(v) => setScope({ ...scope, excludes: splitLines(v) })} />
              <label className="row" style={{ gap: 8, alignItems: "center", fontSize: 12.5 }}>
                <input type="checkbox" checked={!!scope.include_guests}
                       onChange={(e) => setScope({ ...scope, include_guests: e.target.checked })} />
                Include guest accounts
              </label>
              <div className="row" style={{ gap: 8 }}>
                <button className="btn primary sm" onClick={() => saveScope(scope)}>Save scope &amp; re-discover</button>
                <button className="btn ghost sm" onClick={() => setScopeOpen(false)}>Close</button>
              </div>
            </div>
          )}

          <div className="row" style={{ gap: 14, flexWrap: "wrap", alignItems: "center", marginBottom: 12,
               padding: "8px 12px", border: "1px solid var(--border-soft)", borderRadius: 8 }}>
            <label className="row" style={{ gap: 6, alignItems: "center", fontSize: 12.5 }} title="Automatically map any newly discovered user that matches an existing Arkive member by email">
              <input type="checkbox" checked={!!status?.auto_map}
                     onChange={(e) => saveSettings({ auto_map: e.target.checked })} />
              Auto-map matched users
            </label>
            <label className="row" style={{ gap: 6, alignItems: "center", fontSize: 12.5 }} title="Automatically create a new Arkive member for any in-scope user with no existing account">
              <input type="checkbox" checked={!!status?.auto_create}
                     onChange={(e) => saveSettings({ auto_create: e.target.checked })} />
              Auto-create accounts for new users
            </label>
            <div style={{ flex: 1 }} />
            {(status?.identities_suggested ?? 0) > 0 && (
              <button className="btn primary sm" disabled={busy === "accept"} onClick={acceptAllSuggestions}>
                <Icon name="check" size={13} /> {busy === "accept" ? "Accepting…" : `Accept ${status?.identities_suggested} suggestion(s)`}
              </button>
            )}
          </div>

          {identities.length === 0 ? (
            <div className="muted" style={{ padding: "16px 4px" }}>
              No identities yet. Click <b>Discover users</b> to pull your Entra directory.
            </div>
          ) : (
            <div style={{ overflowX: "auto" }}>
              <table className="table">
                <thead><tr><th>Name</th><th>User principal name</th><th>Type</th><th>Scope</th><th>Maps to</th></tr></thead>
                <tbody>
                  {identities.map((idn) => {
                    const cur = idn.binding?.status === "mapped" && idn.binding.user_id ? `map:${idn.binding.user_id}`
                      : idn.binding?.protected_only ? "protected_only"
                      : idn.state === "excluded" ? "exclude" : "";
                    const suggested = idn.binding?.status === "suggested" && idn.binding.user_id;
                    return (
                      <tr key={idn.id} style={idn.in_scope ? undefined : { opacity: 0.55 }}>
                        <td style={{ fontWeight: 600 }}>{idn.display_name}{!idn.account_enabled && <span className="faint" style={{ fontWeight: 400 }}> · disabled</span>}</td>
                        <td className="faint" style={{ fontSize: 12 }}>{idn.upn || idn.email || "—"}</td>
                        <td><Pill tone={idn.user_type === "guest" ? "warn" : "info"}>{idn.user_type}</Pill></td>
                        <td><Pill tone={idn.in_scope ? "ok" : "warn"}>{idn.in_scope ? "in scope" : (idn.scope_reason || "out")}</Pill></td>
                        <td>
                          <div className="row" style={{ gap: 6, alignItems: "center" }}>
                            <select className="input sm" value={suggested ? "" : cur} onChange={(e) => decide(idn, e.target.value)}>
                              <option value="">{suggested ? "Suggested…" : "Unassigned"}</option>
                              <option value="protected_only">Protected only</option>
                              <option value="exclude">Exclude</option>
                              <option value="create_user">Create new account</option>
                              <optgroup label="Map to member">
                                {members.map((mem) => <option key={mem.id} value={`map:${mem.id}`}>{mem.name}</option>)}
                              </optgroup>
                            </select>
                            {suggested && (
                              <button className="btn primary sm" title="Accept the suggested match"
                                      onClick={() => decide(idn, `map:${idn.binding!.user_id}`)}>
                                Accept
                              </button>
                            )}
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </Card>
        <Card style={{ marginTop: 14 }}>
          <div className="spread" style={{ alignItems: "center" }}>
            <div>
              <h3 style={{ margin: 0 }}>Protect mapped users</h3>
              <div className="faint" style={{ fontSize: 12, maxWidth: 560 }}>
                Collect your organization's <b>Exchange Online</b>, <b>OneDrive</b>, <b>SharePoint</b> and
                <b> Teams</b> using admin access — no per-employee sign-in. User data lands in each
                user's vault; org data (SharePoint sites, Teams channels) in the organization vault.
              </div>
            </div>
            <label className="row" style={{ gap: 8, alignItems: "center" }}>
              <input type="checkbox" checked={collectEnabled} onChange={(e) => toggleCollection(e.target.checked)} />
              <span style={{ fontSize: 12.5 }}>{collectEnabled ? "On" : "Off"}</span>
            </label>
          </div>

          {profile && (
            <div className="stack" style={{ gap: 12, marginTop: 14, padding: 12,
                 border: "1px solid var(--border-soft)", borderRadius: 8 }}>
              <div style={{ fontWeight: 600, fontSize: 13 }}>Managed protection profile</div>
              <div className="faint" style={{ fontSize: 11.5, marginTop: -6 }}>
                Applies to every protected user — Arkive establishes a Data Map profile per user that
                collects on their behalf.
              </div>
              <div className="stack" style={{ gap: 6 }}>
                <span style={{ fontSize: 12, fontWeight: 600 }}>What to protect</span>
                <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
                  {(profile.workload_catalog || []).map((w) => {
                    const on = profile.workloads.includes(w.id);
                    return (
                      <span key={w.id} className={`chip ${on ? "active" : ""}`} title={w.description}
                            onClick={() => saveProfile({ workloads: on ? profile.workloads.filter((x) => x !== w.id) : [...profile.workloads, w.id] })}>
                        {on && <Icon name="check" size={12} />} {w.label}
                      </span>
                    );
                  })}
                </div>
              </div>
              <div className="stack" style={{ gap: 6 }}>
                <span style={{ fontSize: 12, fontWeight: 600 }}>Store to</span>
                <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
                  {targets.length === 0 && <span className="faint" style={{ fontSize: 12 }}>No destinations enabled — set them in Protection Setup.</span>}
                  {targets.map((t) => {
                    const on = profile.destinations.includes(t.id);
                    return (
                      <span key={t.id} className={`chip ${on ? "active" : ""}`}
                            onClick={() => saveProfile({ destinations: on ? profile.destinations.filter((x) => x !== t.id) : [...profile.destinations, t.id] })}>
                        {on && <Icon name="check" size={12} />} {t.label}
                      </span>
                    );
                  })}
                </div>
              </div>
              <div className="row" style={{ gap: 8, alignItems: "center" }}>
                <span style={{ fontSize: 12, fontWeight: 600 }}>Back up automatically</span>
                <select className="input sm" style={{ width: 180 }}
                        value={profile.backup_interval_minutes ?? -1}
                        onChange={(e) => saveProfile({ backup_interval_minutes: Number(e.target.value) })}>
                  <option value={-1}>Default cadence</option>
                  <option value={360}>Every 6 hours</option>
                  <option value={720}>Every 12 hours</option>
                  <option value={1440}>Daily</option>
                  <option value={10080}>Weekly</option>
                </select>
                {typeof profile.active_sources === "number" && (
                  <span className="faint" style={{ fontSize: 11.5, marginLeft: "auto" }}>
                    {profile.active_sources} managed source(s) active
                  </span>
                )}
              </div>
            </div>
          )}
          {workloadRollup.length > 0 && (
            <div className="row" style={{ gap: 8, flexWrap: "wrap", marginTop: 12 }}>
              {workloadRollup.map((w) => (
                <div key={w.workload} className="card" style={{ padding: "8px 12px", minWidth: 150 }}>
                  <div className="row" style={{ gap: 6, alignItems: "center" }}>
                    <SourceIcon type={w.workload === "teams_chat" ? "teams" : w.workload} size={16} />
                    <span style={{ fontSize: 12.5, fontWeight: 600 }}>{w.label}</span>
                  </div>
                  <div style={{ fontSize: 20, fontWeight: 700, marginTop: 2 }}>{w.objects.toLocaleString()}</div>
                  <div className="faint" style={{ fontSize: 11 }}>
                    objects · {w.active}/{w.sources} active{w.errors ? ` · ${w.errors} need attention` : ""}
                  </div>
                </div>
              ))}
              <div className="card" style={{ padding: "8px 12px", minWidth: 130, marginLeft: "auto" }}>
                <div className="faint" style={{ fontSize: 11.5, fontWeight: 600 }}>Total protected</div>
                <div style={{ fontSize: 20, fontWeight: 700, marginTop: 2 }}>{totalObjects.toLocaleString()}</div>
                <div className="faint" style={{ fontSize: 11 }}>objects</div>
              </div>
            </div>
          )}
          {sources.length > 0 && (
            <div style={{ overflowX: "auto", marginTop: 12 }}>
              <table className="table">
                <thead><tr><th>Source</th><th>Workload</th><th>State</th><th style={{ textAlign: "right" }}>Objects</th><th>Last collected</th></tr></thead>
                <tbody>
                  {sources.map((s) => (
                    <tr key={s.id}>
                      <td style={{ fontWeight: 600 }}>{s.name}</td>
                      <td><Pill tone="info">{WORKLOAD_LABELS[s.workload] || s.workload}</Pill></td>
                      <td title={s.last_error || undefined}><Pill tone={s.state === "active" ? "ok" : (s.state === "credential_error" || s.state === "permission_required") ? "danger" : s.state === "empty" ? "warn" : "warn"}>{s.state}</Pill></td>
                      <td style={{ textAlign: "right", fontVariantNumeric: "tabular-nums" }}>{(s.objects ?? 0).toLocaleString()}</td>
                      <td className="faint" style={{ fontSize: 12 }}>{s.last_collected_at ? new Date(s.last_collected_at.endsWith("Z") ? s.last_collected_at : s.last_collected_at + "Z").toLocaleString() : "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
        {compliance?.enabled && (
          <Card style={{ marginTop: 14 }}>
            <div className="spread" style={{ marginBottom: 8 }}>
              <div className="stack" style={{ gap: 2 }}>
                <h3 style={{ margin: 0 }}><Icon name="shield" size={15} /> Compliance rules</h3>
                <span className="faint" style={{ fontSize: 12 }}>
                  Governance rules applied to these managed sources on ingest (label, restrict,
                  obfuscate, don't-index or discard).
                </span>
              </div>
              <button className="btn sm" onClick={() => nav("/rules")}>
                <Icon name="link" size={13} /> Manage rules
              </button>
            </div>
            {compliance.rules.length === 0 ? (
              <div className="faint" style={{ fontSize: 12.5 }}>
                No rules apply to these managed sources yet.{" "}
                <a onClick={() => nav("/rules")} style={{ cursor: "pointer", color: "var(--accent,#4f7cff)" }}>
                  Create a rule
                </a>{" "}
                and scope it to a Microsoft 365 managed source.
              </div>
            ) : (
              <div className="stack" style={{ gap: 0 }}>
                {compliance.rules.map((r) => (
                  <div key={r.id} className="row" style={{ gap: 10, alignItems: "center", padding: "6px 0", borderBottom: "1px solid var(--border-soft)" }}>
                    <div className="flex1">
                      <div style={{ fontSize: 13, fontWeight: 600 }}>{r.name}</div>
                      <div className="faint" style={{ fontSize: 11 }}>
                        {r.scoped ? "scoped to managed source(s)" : "applies to all sources"}
                        {" · "}{(r.actions || []).map((a) => a.type).join(", ") || "no actions"}
                      </div>
                    </div>
                    <Pill tone={r.enabled ? "ok" : "warn"}>{r.enabled ? "on" : "off"}</Pill>
                  </div>
                ))}
              </div>
            )}
          </Card>
        )}
        </>
      )}
    </>
  );
}

