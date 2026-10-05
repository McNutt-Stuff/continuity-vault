import { useEffect, useState } from "react";
import { api } from "../api";
import { Card, Stat, Pill, Loading, bytes, timeAgo } from "../components/ui";
import { Icon, IconName } from "../components/Icon";
import { SourceIcon } from "../components/SourceIcon";
import { useAuth } from "../auth";
import { notify, promptDialog } from "../components/dialog";

type Tab = "overview" | "apps" | "devices" | "unprotected";

interface AiMeta { vendor: string; tool?: string; ai_category?: string; data_risk?: string; sanctioned: boolean }
interface AppRow {
  ref: string; name: string; category: string; sources: string[]; source_type: string;
  total_bytes: number; device_count: number; protectable: boolean; protected: boolean;
  is_ai: boolean; ai: AiMeta | null; risk: string; risk_reason: string; last_seen: string | null;
}
interface DeviceRow {
  ref: string; name: string; device_type: string; sources: string[]; total_bytes: number;
  app_count: number; owner_user_id: string; ownership: string; last_seen: string | null;
}
interface SourceAgg { source: string; icon: string; apps: number; bytes: number }
interface Overview {
  totals: { apps: number; devices: number; bytes: number; ai_apps: number; unprotected: number; risky: number };
  by_source: SourceAgg[]; top_apps: AppRow[]; risk_levels: string[];
}

const SEV_TONE: Record<string, "ok" | "warn" | "danger" | "info"> = {
  concerning: "warn", risky: "danger", blocked: "danger",
};
const RISK_LABEL: Record<string, string> = {
  concerning: "Concerning", risky: "Risky", blocked: "Blocked",
};
// Source label → brand/icon. UniFi + endpoint + managed integrations render real
// marks; anything else falls back to a generic activity glyph.
const SOURCE_BRAND: Record<string, string> = {
  ubiquiti: "ubiquiti", endpoint: "macos", microsoft365: "microsoft365",
  google_workspace: "google_workspace",
};
const SOURCE_LABEL: Record<string, string> = {
  ubiquiti: "UniFi network", endpoint: "Endpoint (browser)", microsoft365: "Microsoft 365",
  network: "Network",
};
const DEVICE_ICON: Record<string, IconName> = {
  computer: "user", laptop: "user", desktop: "user", server: "server",
  phone: "user", media: "activity", iot: "grid", device: "grid",
};

function SourceBadge({ s, size = 14 }: { s: string; size?: number }) {
  const brand = SOURCE_BRAND[s];
  return (
    <span className="row" title={SOURCE_LABEL[s] || s}
      style={{ gap: 5, alignItems: "center", padding: "2px 7px", borderRadius: 6, background: "var(--inset)", fontSize: 11 }}>
      {brand ? <SourceIcon type={brand} size={size} /> : <Icon name="activity" size={size} />}
      <span style={{ textTransform: "capitalize" }}>{SOURCE_LABEL[s] || s}</span>
    </span>
  );
}

export default function NetworkAnalytics() {
  const [tab, setTab] = useState<Tab>("overview");
  const [ov, setOv] = useState<Overview | null>(null);
  const [err, setErr] = useState("");

  const loadOv = async () => {
    try { setOv(await api.get<Overview>("/network-analytics/overview")); setErr(""); }
    catch (e: any) { setErr(e?.message || "Failed to load network analytics"); }
  };
  useEffect(() => { void loadOv(); }, []);

  return (
    <div className="stack" style={{ gap: 16 }}>
      <div className="spread" style={{ alignItems: "flex-end" }}>
        <div>
          <h1 style={{ margin: 0 }}>Network Analytics</h1>
          <div className="faint" style={{ fontSize: 13 }}>
            Every app, service and device across your network &amp; endpoints — de-duplicated, with where the data came from.
          </div>
        </div>
      </div>

      <div className="row" style={{ gap: 4, borderBottom: "1px solid var(--border)", flexWrap: "wrap" }}>
        {(["overview", "apps", "devices", "unprotected"] as Tab[]).map((t) => (
          <button key={t} onClick={() => setTab(t)}
            className="btn ghost sm"
            style={{
              borderRadius: 0, borderBottom: tab === t ? "2px solid var(--accent)" : "2px solid transparent",
              fontWeight: tab === t ? 700 : 500, textTransform: "capitalize",
              color: tab === t ? "var(--text)" : "var(--muted-c)",
            }}>
            {t === "unprotected" ? "Not protected" : t}
          </button>
        ))}
      </div>

      {err && <Card><div className="row" style={{ gap: 8, color: "var(--danger-c)" }}><Icon name="alert" size={15} />{err}</div></Card>}

      {tab === "overview" && (ov ? <OverviewTab ov={ov} onGoto={setTab} /> : <Loading />)}
      {tab === "apps" && <AppsTab onFlagChanged={loadOv} />}
      {tab === "devices" && <DevicesTab />}
      {tab === "unprotected" && <UnprotectedTab />}
    </div>
  );
}

function OverviewTab({ ov, onGoto }: { ov: Overview; onGoto: (t: Tab) => void }) {
  const t = ov.totals;
  return (
    <>
      <div className="row" style={{ gap: 12, flexWrap: "wrap" }}>
        <Stat label="Apps & services" value={t.apps.toLocaleString()} />
        <Stat label="Devices" value={t.devices.toLocaleString()} />
        <Stat label="Traffic observed" value={bytes(t.bytes)} />
        <Stat label="AI services" value={t.ai_apps.toLocaleString()} hint={t.ai_apps ? "in use" : "none seen"} />
        <Stat label="Not protected" value={t.unprotected.toLocaleString()}
          hint={t.unprotected ? "connect to protect" : "all covered"} />
        <Stat label="Risk-flagged" value={t.risky.toLocaleString()} hint={t.risky ? "needs review" : "none"} />
      </div>

      <Card>
        <div className="row" style={{ gap: 8, marginBottom: 10 }}><Icon name="grid" size={15} /><b>Where the data comes from</b></div>
        <div className="row" style={{ gap: 10, flexWrap: "wrap" }}>
          {ov.by_source.map((s) => (
            <div key={s.source} className="row" style={{ gap: 10, alignItems: "center", padding: "10px 14px", borderRadius: 10, background: "var(--inset)", minWidth: 190 }}>
              {SOURCE_BRAND[s.source] ? <SourceIcon type={SOURCE_BRAND[s.source]} size={22} /> : <Icon name={(s.icon as IconName) || "activity"} size={20} />}
              <div>
                <div style={{ fontWeight: 600, textTransform: "capitalize" }}>{SOURCE_LABEL[s.source] || s.source}</div>
                <div className="faint" style={{ fontSize: 12 }}>{s.apps.toLocaleString()} apps · {bytes(s.bytes)}</div>
              </div>
            </div>
          ))}
          {ov.by_source.length === 0 && <span className="muted">No network sources reporting yet — connect UniFi or enable endpoint web usage.</span>}
        </div>
      </Card>

      <Card onClick={() => onGoto("apps")} style={{ cursor: "pointer" }}>
        <div className="spread" style={{ marginBottom: 10 }}>
          <div className="row" style={{ gap: 8 }}><Icon name="activity" size={15} /><b>Top apps &amp; services</b></div>
          <span className="faint" style={{ fontSize: 12 }}>View all →</span>
        </div>
        <div className="stack" style={{ gap: 0 }}>
          {ov.top_apps.map((a) => <AppLine key={a.ref} a={a} />)}
          {ov.top_apps.length === 0 && <span className="muted">No apps observed yet.</span>}
        </div>
      </Card>
    </>
  );
}

function AppLine({ a, onClick }: { a: AppRow; onClick?: () => void }) {
  return (
    <div className="row" onClick={onClick}
      style={{ gap: 10, alignItems: "center", padding: "9px 4px", borderTop: "1px solid var(--border-soft)", cursor: onClick ? "pointer" : "default" }}>
      <AppGlyph a={a} />
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontWeight: 600, fontSize: 13, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{a.name}</div>
        <div className="faint" style={{ fontSize: 11.5 }}>{a.category || "—"} · {a.device_count} device{a.device_count === 1 ? "" : "s"}</div>
      </div>
      <div className="row" style={{ gap: 4 }}>{a.sources.map((s) => <SourceDot key={s} s={s} />)}</div>
      {a.is_ai && <Pill tone="info">AI</Pill>}
      {a.risk && <Pill tone={SEV_TONE[a.risk] || "warn"} dot>{RISK_LABEL[a.risk] || a.risk}</Pill>}
      {a.protectable && !a.protected && <Pill tone="warn">Unprotected</Pill>}
      <span className="faint" style={{ fontSize: 12, minWidth: 64, textAlign: "right" }}>{bytes(a.total_bytes)}</span>
    </div>
  );
}

function AppGlyph({ a }: { a: AppRow }) {
  if (a.is_ai) return <Icon name="sparkle" size={16} />;
  if (a.source_type) return <SourceIcon type={a.source_type} size={16} />;
  return <Icon name="grid" size={16} />;
}

function SourceDot({ s }: { s: string }) {
  const brand = SOURCE_BRAND[s];
  return <span title={SOURCE_LABEL[s] || s} style={{ display: "inline-flex" }}>
    {brand ? <SourceIcon type={brand} size={13} /> : <Icon name="activity" size={13} />}
  </span>;
}

function AppsTab({ onFlagChanged }: { onFlagChanged: () => void }) {
  const [rows, setRows] = useState<AppRow[]>([]);
  const [facets, setFacets] = useState<{ categories: string[]; sources: string[] }>({ categories: [], sources: [] });
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [sel, setSel] = useState<AppRow | null>(null);
  const [f, setF] = useState<{ q: string; category: string; source: string; protection: string; ai: boolean; risk: boolean; sort: string }>(
    { q: "", category: "", source: "", protection: "", ai: false, risk: false, sort: "bytes" });

  const load = async () => {
    setLoading(true);
    const qs = new URLSearchParams();
    if (f.q) qs.set("q", f.q);
    if (f.category) qs.set("category", f.category);
    if (f.source) qs.set("source", f.source);
    if (f.protection) qs.set("protection", f.protection);
    if (f.ai) qs.set("ai", "true");
    if (f.risk) qs.set("risk", "true");
    qs.set("sort", f.sort);
    try {
      const r = await api.get<{ total: number; apps: AppRow[]; facets: any }>(`/network-analytics/apps?${qs}`);
      setRows(r.apps); setTotal(r.total); setFacets(r.facets || { categories: [], sources: [] });
    } finally { setLoading(false); }
  };
  useEffect(() => { void load(); }, [JSON.stringify(f)]);

  return (
    <Card>
      <div className="row" style={{ gap: 8, flexWrap: "wrap", marginBottom: 10 }}>
        <input className="input sm" placeholder="Search apps…" style={{ width: 200 }}
          value={f.q} onChange={(e) => setF({ ...f, q: e.target.value })} />
        <select className="input sm" value={f.category} onChange={(e) => setF({ ...f, category: e.target.value })} style={{ width: 130 }}>
          <option value="">Category: all</option>
          {facets.categories.map((c) => <option key={c} value={c}>{c}</option>)}
        </select>
        <select className="input sm" value={f.source} onChange={(e) => setF({ ...f, source: e.target.value })} style={{ width: 150 }}>
          <option value="">Source: all</option>
          {facets.sources.map((s) => <option key={s} value={s}>{SOURCE_LABEL[s] || s}</option>)}
        </select>
        <select className="input sm" value={f.protection} onChange={(e) => setF({ ...f, protection: e.target.value })} style={{ width: 140 }}>
          <option value="">Protection: all</option>
          <option value="protected">Protected</option>
          <option value="unprotected">Not protected</option>
        </select>
        <button className={`btn sm ${f.ai ? "primary" : "ghost"}`} onClick={() => setF({ ...f, ai: !f.ai })}>AI only</button>
        <button className={`btn sm ${f.risk ? "primary" : "ghost"}`} onClick={() => setF({ ...f, risk: !f.risk })}>Flagged</button>
        <div style={{ flex: 1 }} />
        <select className="input sm" value={f.sort} onChange={(e) => setF({ ...f, sort: e.target.value })} style={{ width: 130 }}>
          <option value="bytes">Sort: traffic</option>
          <option value="devices">Sort: devices</option>
          <option value="name">Sort: name</option>
        </select>
      </div>
      <div className="faint" style={{ fontSize: 12, marginBottom: 4 }}>{total.toLocaleString()} app{total === 1 ? "" : "s"}</div>
      {loading ? <Loading card={false} /> : rows.length === 0 ? (
        <div className="muted" style={{ padding: 12 }}>No apps match. Apps appear from UniFi DPI and endpoint web usage.</div>
      ) : (
        <div className="stack" style={{ gap: 0 }}>
          {rows.map((a) => <AppLine key={a.ref} a={a} onClick={() => setSel(a)} />)}
        </div>
      )}
      {sel && <AppDrawer appRow={sel} onClose={() => setSel(null)} onFlagChanged={() => { void load(); onFlagChanged(); }} />}
    </Card>
  );
}

function AppDrawer({ appRow, onClose, onFlagChanged }: { appRow: AppRow; onClose: () => void; onFlagChanged: () => void }) {
  const { me } = useAuth();
  const [d, setD] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const load = async () => { try { setD(await api.get(`/network-analytics/apps/${encodeURIComponent(appRow.ref)}`)); } catch { setD({ ...appRow, devices: [], by_source: [] }); } };
  useEffect(() => { void load(); }, [appRow.ref]);

  const setRisk = async (risk: string) => {
    let reason = "";
    if (risk) {
      reason = (await promptDialog({ title: `Flag "${appRow.name}" as ${RISK_LABEL[risk] || risk}`, message: "Add a short reason (optional).", placeholder: "e.g. exfiltration risk / unsanctioned AI" })) || "";
    }
    setBusy(true);
    try {
      await api.post("/network-analytics/flags", { ref: appRow.ref, risk, reason });
      await load(); onFlagChanged();
      await notify({ message: risk ? `Flagged as ${RISK_LABEL[risk] || risk}` : "Flag cleared", tone: "success" });
    } catch (e: any) { await notify({ message: e?.message || "Could not update the flag", tone: "danger" }); }
    finally { setBusy(false); }
  };

  const risk = d?.risk || appRow.risk;
  return (
    <div onClick={onClose} style={{ position: "fixed", inset: 0, zIndex: 4000, background: "rgba(0,0,0,.4)", display: "flex", justifyContent: "flex-end" }}>
      <div onClick={(e) => e.stopPropagation()} style={{ width: "min(560px, 96vw)", height: "100%", overflow: "auto", background: "var(--panel)", borderLeft: "1px solid var(--border)", padding: 20 }}>
        <div className="spread" style={{ marginBottom: 12 }}>
          <div className="row" style={{ gap: 10 }}>
            <AppGlyph a={appRow} />
            <b style={{ fontSize: 15 }}>{appRow.name}</b>
          </div>
          <button className="btn ghost sm" onClick={onClose}><Icon name="x" size={14} /></button>
        </div>
        <div className="row" style={{ gap: 6, marginBottom: 12, flexWrap: "wrap" }}>
          <Pill tone="info">{d?.category || appRow.category || "—"}</Pill>
          {appRow.is_ai && <Pill tone="info">AI{d?.ai?.vendor ? ` · ${d.ai.vendor}` : ""}</Pill>}
          {d?.ai?.data_risk && <Pill tone={d.ai.data_risk === "high" ? "danger" : d.ai.data_risk === "medium" ? "warn" : "info"}>{d.ai.data_risk} data risk</Pill>}
          {appRow.protectable && !appRow.protected && <Pill tone="warn">Not protected</Pill>}
          {appRow.protected && <Pill tone="ok" dot>Protected</Pill>}
          {risk && <Pill tone={SEV_TONE[risk] || "warn"} dot>{RISK_LABEL[risk] || risk}</Pill>}
        </div>

        {me?.can_admin && (
          <Card style={{ marginBottom: 14, background: "var(--inset)" }}>
            <div className="row" style={{ gap: 8, marginBottom: 8 }}><Icon name="alert" size={14} /><b style={{ fontSize: 13 }}>Flag this app</b></div>
            <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
              {["concerning", "risky", "blocked"].map((lvl) => (
                <button key={lvl} disabled={busy} className={`btn sm ${risk === lvl ? "primary" : "ghost"}`}
                  onClick={() => setRisk(risk === lvl ? "" : lvl)}>{RISK_LABEL[lvl]}</button>
              ))}
              {risk && <button disabled={busy} className="btn ghost sm" onClick={() => setRisk("")}>Clear</button>}
            </div>
            {(d?.risk_reason || appRow.risk_reason) && <div className="faint" style={{ fontSize: 12, marginTop: 8 }}>Reason: {d?.risk_reason || appRow.risk_reason}</div>}
          </Card>
        )}

        <div className="faint" style={{ fontSize: 12, marginBottom: 6 }}>Seen via</div>
        <div className="row" style={{ gap: 8, flexWrap: "wrap", marginBottom: 14 }}>
          {(d?.by_source || []).map((s: any) => (
            <div key={s.source} className="row" style={{ gap: 6, alignItems: "center" }}>
              <SourceBadge s={s.source} /><span className="faint" style={{ fontSize: 12 }}>{bytes(s.bytes)}</span>
            </div>
          ))}
          {(d?.by_source || []).length === 0 && <span className="muted" style={{ fontSize: 12.5 }}>—</span>}
        </div>

        {appRow.protectable && !appRow.protected && appRow.source_type && (
          <a className="btn sm" href="/connectors" style={{ marginBottom: 14, display: "inline-flex" }}>
            <Icon name="link" size={13} /> Protect this source
          </a>
        )}

        <div className="faint" style={{ fontSize: 12, marginBottom: 6 }}>Devices using it</div>
        {!d ? <Loading card={false} /> : (d.devices || []).length === 0 ? <span className="muted" style={{ fontSize: 12.5 }}>No per-device detail.</span> : (
          <div className="stack" style={{ gap: 0 }}>
            {d.devices.map((dev: any) => (
              <div key={dev.ref} className="row" style={{ gap: 10, alignItems: "center", padding: "8px 0", borderTop: "1px solid var(--border-soft)" }}>
                <Icon name={DEVICE_ICON[dev.device_type] || "grid"} size={14} />
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 13, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{dev.name}</div>
                  <div className="faint" style={{ fontSize: 11.5 }}>{dev.device_type}</div>
                </div>
                <span className="faint" style={{ fontSize: 12 }}>{bytes(dev.bytes)}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function DevicesTab() {
  const [rows, setRows] = useState<DeviceRow[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [sel, setSel] = useState<DeviceRow | null>(null);
  const [f, setF] = useState<{ q: string; sort: string }>({ q: "", sort: "bytes" });

  const load = async () => {
    setLoading(true);
    const qs = new URLSearchParams();
    if (f.q) qs.set("q", f.q);
    qs.set("sort", f.sort);
    try {
      const r = await api.get<{ total: number; devices: DeviceRow[] }>(`/network-analytics/devices?${qs}`);
      setRows(r.devices); setTotal(r.total);
    } finally { setLoading(false); }
  };
  useEffect(() => { void load(); }, [JSON.stringify(f)]);

  return (
    <Card>
      <div className="row" style={{ gap: 8, flexWrap: "wrap", marginBottom: 10 }}>
        <input className="input sm" placeholder="Search devices…" style={{ width: 220 }}
          value={f.q} onChange={(e) => setF({ ...f, q: e.target.value })} />
        <div style={{ flex: 1 }} />
        <select className="input sm" value={f.sort} onChange={(e) => setF({ ...f, sort: e.target.value })} style={{ width: 130 }}>
          <option value="bytes">Sort: traffic</option>
          <option value="apps">Sort: apps</option>
          <option value="name">Sort: name</option>
        </select>
      </div>
      <div className="faint" style={{ fontSize: 12, marginBottom: 4 }}>{total.toLocaleString()} device{total === 1 ? "" : "s"} (de-duplicated across sources)</div>
      {loading ? <Loading card={false} /> : rows.length === 0 ? (
        <div className="muted" style={{ padding: 12 }}>No devices observed yet.</div>
      ) : (
        <div className="stack" style={{ gap: 0 }}>
          {rows.map((dv) => (
            <div key={dv.ref} className="row" onClick={() => setSel(dv)}
              style={{ gap: 10, alignItems: "center", padding: "9px 4px", borderTop: "1px solid var(--border-soft)", cursor: "pointer" }}>
              <Icon name={DEVICE_ICON[dv.device_type] || "grid"} size={15} />
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontWeight: 600, fontSize: 13, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{dv.name}</div>
                <div className="faint" style={{ fontSize: 11.5 }}>{dv.device_type} · {dv.app_count} app{dv.app_count === 1 ? "" : "s"}</div>
              </div>
              <div className="row" style={{ gap: 4 }}>{dv.sources.map((s) => <SourceDot key={s} s={s} />)}</div>
              <span className="faint" style={{ fontSize: 12, minWidth: 64, textAlign: "right" }}>{bytes(dv.total_bytes)}</span>
            </div>
          ))}
        </div>
      )}
      {sel && <DeviceDrawer dev={sel} onClose={() => setSel(null)} />}
    </Card>
  );
}

function DeviceDrawer({ dev, onClose }: { dev: DeviceRow; onClose: () => void }) {
  const [d, setD] = useState<any>(null);
  useEffect(() => { (async () => { try { setD(await api.get(`/network-analytics/devices/${encodeURIComponent(dev.ref)}`)); } catch { setD({ ...dev, apps: [] }); } })(); }, [dev.ref]);
  return (
    <div onClick={onClose} style={{ position: "fixed", inset: 0, zIndex: 4000, background: "rgba(0,0,0,.4)", display: "flex", justifyContent: "flex-end" }}>
      <div onClick={(e) => e.stopPropagation()} style={{ width: "min(540px, 96vw)", height: "100%", overflow: "auto", background: "var(--panel)", borderLeft: "1px solid var(--border)", padding: 20 }}>
        <div className="spread" style={{ marginBottom: 12 }}>
          <div className="row" style={{ gap: 10 }}><Icon name={DEVICE_ICON[dev.device_type] || "grid"} size={16} /><b style={{ fontSize: 15 }}>{dev.name}</b></div>
          <button className="btn ghost sm" onClick={onClose}><Icon name="x" size={14} /></button>
        </div>
        <div className="row" style={{ gap: 6, marginBottom: 14, flexWrap: "wrap" }}>
          <Pill tone="info">{dev.device_type}</Pill>
          {(d?.sources || dev.sources).map((s: string) => <SourceBadge key={s} s={s} />)}
        </div>
        <div className="faint" style={{ fontSize: 12, marginBottom: 6 }}>Apps &amp; services used</div>
        {!d ? <Loading card={false} /> : (d.apps || []).length === 0 ? <span className="muted" style={{ fontSize: 12.5 }}>No per-app detail.</span> : (
          <div className="stack" style={{ gap: 0 }}>
            {d.apps.map((a: any) => (
              <div key={a.ref} className="row" style={{ gap: 10, alignItems: "center", padding: "8px 0", borderTop: "1px solid var(--border-soft)" }}>
                {a.is_ai ? <Icon name="sparkle" size={14} /> : a.source_type ? <SourceIcon type={a.source_type} size={14} /> : <Icon name="grid" size={14} />}
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 13 }}>{a.name}</div>
                  <div className="faint" style={{ fontSize: 11.5 }}>{a.category || "—"}</div>
                </div>
                {a.risk && <Pill tone={SEV_TONE[a.risk] || "warn"} dot>{RISK_LABEL[a.risk] || a.risk}</Pill>}
                <span className="faint" style={{ fontSize: 12 }}>{bytes(a.bytes)}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function UnprotectedTab() {
  const [d, setD] = useState<{ unprotected: any[]; recommended: any[] } | null>(null);
  useEffect(() => { (async () => { try { setD(await api.get("/network-analytics/shadow")); } catch { setD({ unprotected: [], recommended: [] }); } })(); }, []);
  if (!d) return <Loading />;
  return (
    <>
      <Card>
        <div className="row" style={{ gap: 8, marginBottom: 4 }}><Icon name="alert" size={15} style={{ color: "var(--warn)" }} /><b>Apps you aren't protecting yet</b></div>
        <div className="faint" style={{ fontSize: 12.5, marginBottom: 12 }}>Services seen in your traffic that Arkive can back up — connect them to capture their data.</div>
        {d.unprotected.length === 0 ? <div className="muted" style={{ padding: 8 }}>Everything Arkive can protect is connected. Nice.</div> : (
          <div className="row" style={{ gap: 10, flexWrap: "wrap" }}>
            {d.unprotected.map((s) => (
              <div key={s.source_type} className="stack" style={{ gap: 6, padding: 12, borderRadius: 10, border: "1px solid var(--border)", minWidth: 210 }}>
                <div className="row" style={{ gap: 8, alignItems: "center" }}>
                  <SourceIcon type={s.source_type} size={18} />
                  <b style={{ textTransform: "capitalize" }}>{s.source_type.replace(/_/g, " ")}</b>
                </div>
                <div className="faint" style={{ fontSize: 12 }}>{s.apps} app{s.apps === 1 ? "" : "s"} · {bytes(s.total_bytes)}</div>
                <a className="btn sm primary" href="/connectors" style={{ alignSelf: "flex-start" }}><Icon name="link" size={13} /> Connect</a>
              </div>
            ))}
          </div>
        )}
      </Card>
      {d.recommended.length > 0 && (
        <Card>
          <div className="row" style={{ gap: 8, marginBottom: 4 }}><Icon name="star" size={15} /><b>Popular services in your traffic</b></div>
          <div className="faint" style={{ fontSize: 12.5, marginBottom: 12 }}>Seen on your network but Arkive doesn't have a connector yet — tell us what to build next.</div>
          <table className="tbl" style={{ width: "100%" }}>
            <thead><tr><th style={{ textAlign: "left" }}>Service</th><th style={{ textAlign: "left" }}>Kind</th><th style={{ textAlign: "right" }}>Traffic</th></tr></thead>
            <tbody>
              {d.recommended.map((r) => (
                <tr key={r.name}><td>{r.name}</td><td className="faint">{r.kind || "—"}</td><td style={{ textAlign: "right" }}>{bytes(r.total_bytes)}</td></tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}
    </>
  );
}
