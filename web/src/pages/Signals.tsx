import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { Card, Stat, Pill, Loading, timeAgo } from "../components/ui";
import { Icon } from "../components/Icon";

type Tab = "overview" | "signals" | "findings" | "providers";

interface Provider {
  provider: string; connection_status: string; last_success_at: string | null;
  objects_processed: number; signals_produced: number; errors: number;
  last_error: string; permissions_state: string; capabilities: string[]; coverage: Record<string, number>;
}
interface Overview {
  active_signals: number; stale_signals: number; open_findings: number;
  by_provider: Record<string, number>; providers: Provider[]; coverage: Record<string, number>;
}
interface Sig {
  id: string; signal_type: string; category: string; kind: string; provider: string;
  subject_type: string; subject_id: string; normalized_value: string; severity: string;
  confidence: string; freshness_state: string; last_seen: string | null; occurrence_count: number;
  value?: any; meta?: any; related_findings?: FindingT[];
}
interface FindingT {
  id: string; finding_type: string; category: string; severity: string; status: string;
  title: string; description: string; subject_type: string; subject_id: string;
  occurrence_count: number; last_seen: string | null; remediation?: any; meta?: any;
}

const SEV_TONE: Record<string, "ok" | "warn" | "danger" | "info"> = {
  info: "info", low: "info", medium: "warn", high: "danger", critical: "danger",
};
const FRESH_TONE: Record<string, "ok" | "warn" | "danger" | "info"> = {
  fresh: "ok", stale: "warn", expired: "danger", unknown: "info",
};
const PROVIDER_ICON: Record<string, string> = {
  arkive: "shield", endpoint: "user", m365: "mail", ubiquiti: "activity",
  node: "server", appliance: "server", ai: "sparkle",
};

function Bar({ pct, tone }: { pct: number; tone: string }) {
  const color = pct >= 95 ? "var(--ok)" : pct >= 80 ? "var(--warn)" : "var(--danger-c)";
  return (
    <div style={{ height: 6, borderRadius: 3, background: "var(--border-soft)", overflow: "hidden" }}>
      <div style={{ height: "100%", width: `${Math.max(0, Math.min(100, pct))}%`, background: tone || color, transition: "width .4s" }} />
    </div>
  );
}

export default function Signals() {
  const [tab, setTab] = useState<Tab>("overview");
  const [ov, setOv] = useState<Overview | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [err, setErr] = useState("");

  const load = async () => {
    try { setOv(await api.get<Overview>("/signals/overview")); setErr(""); }
    catch (e: any) { setErr(e?.message || "Failed to load signals"); }
    finally { setLoaded(true); }
  };
  useEffect(() => { void load(); }, []);

  if (!loaded) return <Loading label="Loading Signal Platform…" />;
  if (err) return <Card><div style={{ color: "var(--danger-c)" }}>{err}</div></Card>;

  return (
    <div className="stack" style={{ gap: 16 }}>
      <div className="spread" style={{ alignItems: "flex-end" }}>
        <div>
          <h1 style={{ margin: 0 }}>Signals</h1>
          <div className="faint" style={{ fontSize: 12.5 }}>
            Asset awareness, protection-gap detection and coverage — one normalized view of your environment.
          </div>
        </div>
        <div className="row" style={{ gap: 6 }}>
          {(["overview", "signals", "findings", "providers"] as Tab[]).map((t) => (
            <button key={t} className={"btn sm " + (tab === t ? "primary" : "ghost")}
              onClick={() => setTab(t)} style={{ textTransform: "capitalize" }}>{t}</button>
          ))}
        </div>
      </div>

      {tab === "overview" && ov && <OverviewTab ov={ov} onGoto={setTab} />}
      {tab === "signals" && <SignalsTab />}
      {tab === "findings" && <FindingsTab />}
      {tab === "providers" && <ProvidersTab providers={ov?.providers || []} />}
    </div>
  );
}

function OverviewTab({ ov, onGoto }: { ov: Overview; onGoto: (t: Tab) => void }) {
  return (
    <>
      <div className="row" style={{ gap: 12, flexWrap: "wrap" }}>
        <Stat label="Active signals" value={ov.active_signals.toLocaleString()} />
        <Stat label="Stale / expired" value={ov.stale_signals.toLocaleString()}
          hint={ov.stale_signals > 0 ? "telemetry aging out" : "all fresh"} />
        <Stat label="Open findings" value={ov.open_findings.toLocaleString()} />
        <Stat label="Providers" value={ov.providers.filter((p) => p.connection_status === "ok").length + " / " + ov.providers.length} hint="reporting" />
      </div>

      <Card>
        <div className="row" style={{ gap: 8, marginBottom: 10 }}><Icon name="grid" size={15} /><b>Coverage</b></div>
        <div className="stack" style={{ gap: 10 }}>
          {Object.entries(ov.coverage).map(([dim, pct]) => (
            <div key={dim}>
              <div className="spread" style={{ fontSize: 12.5, marginBottom: 3 }}>
                <span>{dim}</span><span className="faint">{pct}%</span>
              </div>
              <Bar pct={pct} tone="" />
            </div>
          ))}
        </div>
      </Card>

      <Card onClick={() => onGoto("providers")} style={{ cursor: "pointer" }}>
        <div className="row" style={{ gap: 8, marginBottom: 10 }}><Icon name="activity" size={15} /><b>Providers</b></div>
        <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
          {ov.providers.map((p) => (
            <span key={p.provider} className="row" style={{ gap: 6, alignItems: "center", padding: "6px 10px", borderRadius: 8, background: "var(--inset)" }}>
              <Icon name={(PROVIDER_ICON[p.provider] || "grid") as any} size={13} />
              <b style={{ textTransform: "capitalize" }}>{p.provider}</b>
              <Pill tone={p.connection_status === "ok" ? "ok" : p.connection_status === "not_configured" ? "info" : "warn"} dot>
                {p.connection_status === "ok" ? "healthy" : p.connection_status.replace("_", " ")}
              </Pill>
              <span className="faint" style={{ fontSize: 12 }}>{p.signals_produced.toLocaleString()} signals</span>
            </span>
          ))}
          {ov.providers.length === 0 && <span className="muted">No providers have reported yet.</span>}
        </div>
      </Card>
    </>
  );
}

function SignalsTab() {
  const [rows, setRows] = useState<Sig[]>([]);
  const [facets, setFacets] = useState<any>({});
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState<{ provider?: string; category?: string; severity?: string; freshness?: string; q?: string }>({});
  const [sel, setSel] = useState<Sig | null>(null);

  const load = async () => {
    setLoading(true);
    const qs = new URLSearchParams();
    Object.entries(filter).forEach(([k, v]) => { if (v) qs.set(k, String(v)); });
    try {
      const r = await api.get<{ total: number; signals: Sig[] }>(`/signals?${qs.toString()}`);
      setRows(r.signals); setTotal(r.total);
      setFacets(await api.get("/signals/facets"));
    } finally { setLoading(false); }
  };
  useEffect(() => { void load(); }, [JSON.stringify(filter)]);

  const openDetail = async (s: Sig) => { try { setSel(await api.get<Sig>(`/signals/${s.id}`)); } catch { setSel(s); } };

  return (
    <Card>
      <div className="row" style={{ gap: 8, flexWrap: "wrap", marginBottom: 10 }}>
        <input className="input sm" placeholder="Search signals…" style={{ width: 220 }}
          value={filter.q || ""} onChange={(e) => setFilter({ ...filter, q: e.target.value })} />
        <FacetSelect label="Provider" value={filter.provider} counts={facets.providers} onChange={(v) => setFilter({ ...filter, provider: v })} />
        <FacetSelect label="Category" value={filter.category} counts={facets.categories} onChange={(v) => setFilter({ ...filter, category: v })} />
        <FacetSelect label="Severity" value={filter.severity} counts={facets.severities} onChange={(v) => setFilter({ ...filter, severity: v })} />
        <FacetSelect label="Freshness" value={filter.freshness} counts={facets.freshness} onChange={(v) => setFilter({ ...filter, freshness: v })} />
        <div style={{ flex: 1 }} />
        <span className="faint" style={{ fontSize: 12, alignSelf: "center" }}>{total.toLocaleString()} signals</span>
      </div>
      {loading ? <Loading card={false} /> : rows.length === 0 ? (
        <div className="muted" style={{ padding: 12 }}>No signals match. Signals appear as providers report (Arkive protection, endpoints, M365, network).</div>
      ) : (
        <div className="stack" style={{ gap: 0 }}>
          {rows.map((s) => (
            <div key={s.id} className="row" onClick={() => openDetail(s)}
              style={{ gap: 10, alignItems: "center", padding: "8px 4px", borderTop: "1px solid var(--border-soft)", cursor: "pointer" }}>
              <Icon name={(PROVIDER_ICON[s.provider] || "grid") as any} size={14} />
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontFamily: "var(--mono)", fontSize: 12.5 }}>{s.signal_type}
                  <span className="faint"> = {s.normalized_value || "—"}</span></div>
                <div className="faint" style={{ fontSize: 11.5 }}>{s.subject_type}:{s.subject_id?.slice(0, 20)} · {s.category}</div>
              </div>
              <Pill tone={SEV_TONE[s.severity] || "info"}>{s.severity}</Pill>
              <Pill tone={FRESH_TONE[s.freshness_state] || "info"} dot>{s.freshness_state}</Pill>
              <span className="faint" style={{ fontSize: 11.5, whiteSpace: "nowrap" }}>{s.last_seen ? timeAgo(s.last_seen) : ""}</span>
            </div>
          ))}
        </div>
      )}
      {sel && <SignalDrawer sig={sel} onClose={() => setSel(null)} />}
    </Card>
  );
}

function FacetSelect({ label, value, counts, onChange }: { label: string; value?: string; counts?: Record<string, number>; onChange: (v: string) => void }) {
  const entries = Object.entries(counts || {}).sort((a, b) => b[1] - a[1]);
  return (
    <select className="input sm" value={value || ""} onChange={(e) => onChange(e.target.value)} style={{ width: 150 }}>
      <option value="">{label}: all</option>
      {entries.map(([k, n]) => <option key={k} value={k}>{k} ({n})</option>)}
    </select>
  );
}

function SignalDrawer({ sig, onClose }: { sig: Sig; onClose: () => void }) {
  return (
    <div onClick={onClose} style={{ position: "fixed", inset: 0, zIndex: 4000, background: "rgba(0,0,0,.4)", display: "flex", justifyContent: "flex-end" }}>
      <div onClick={(e) => e.stopPropagation()} style={{ width: "min(520px, 96vw)", height: "100%", overflow: "auto", background: "var(--panel)", borderLeft: "1px solid var(--border)", padding: 20 }}>
        <div className="spread" style={{ marginBottom: 12 }}>
          <b style={{ fontFamily: "var(--mono)", fontSize: 13 }}>{sig.signal_type}</b>
          <button className="btn ghost sm" onClick={onClose}><Icon name="x" size={14} /></button>
        </div>
        <div className="row" style={{ gap: 6, marginBottom: 12, flexWrap: "wrap" }}>
          <Pill tone="info">{sig.provider}</Pill>
          <Pill tone="info">{sig.category}</Pill>
          <Pill tone={SEV_TONE[sig.severity] || "info"}>{sig.severity}</Pill>
          <Pill tone={FRESH_TONE[sig.freshness_state] || "info"} dot>{sig.freshness_state}</Pill>
          <Pill tone="info">confidence: {sig.confidence}</Pill>
        </div>
        <Row k="Value" v={sig.normalized_value || "—"} />
        <Row k="Subject" v={`${sig.subject_type}: ${sig.subject_id}`} />
        <Row k="Kind" v={sig.kind} />
        <Row k="Occurrences" v={String(sig.occurrence_count)} />
        <Row k="Last seen" v={sig.last_seen ? timeAgo(sig.last_seen) : "—"} />
        {sig.value && Object.keys(sig.value).length > 0 && (
          <div style={{ marginTop: 12 }}>
            <div className="faint" style={{ fontSize: 12, marginBottom: 4 }}>Details</div>
            <pre style={{ margin: 0, padding: 10, background: "var(--code-bg)", borderRadius: 8, fontSize: 12, whiteSpace: "pre-wrap", wordBreak: "break-word" }}>{JSON.stringify(sig.value, null, 2)}</pre>
          </div>
        )}
        {sig.related_findings && sig.related_findings.length > 0 && (
          <div style={{ marginTop: 14 }}>
            <div className="faint" style={{ fontSize: 12, marginBottom: 6 }}>Related findings</div>
            {sig.related_findings.map((f) => (
              <div key={f.id} className="row" style={{ gap: 8, padding: "6px 0", borderTop: "1px solid var(--border-soft)" }}>
                <Pill tone={SEV_TONE[f.severity] || "info"} dot>{f.status}</Pill>
                <span style={{ fontSize: 12.5 }}>{f.title}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function Row({ k, v }: { k: string; v: string }) {
  return <div className="row" style={{ gap: 8, padding: "4px 0" }}><span className="faint" style={{ minWidth: 110, fontSize: 12.5 }}>{k}</span><span style={{ fontSize: 12.5, wordBreak: "break-all" }}>{v}</span></div>;
}

function FindingsTab() {
  const [rows, setRows] = useState<FindingT[]>([]);
  const [loading, setLoading] = useState(true);
  const [statusFilter, setStatusFilter] = useState<string>("");

  const load = async () => {
    setLoading(true);
    try {
      const qs = statusFilter ? `?status=${statusFilter}` : "";
      setRows((await api.get<{ findings: FindingT[] }>(`/signals/findings${qs}`)).findings);
    } finally { setLoading(false); }
  };
  useEffect(() => { void load(); }, [statusFilter]);

  const act = async (f: FindingT, status: string) => {
    let reason = "";
    if (status === "risk_accepted" || status === "exception") reason = window.prompt("Reason?") || "";
    try { await api.post(`/signals/findings/${f.id}/status`, { status, reason }); await load(); } catch { /* ignore */ }
  };

  return (
    <Card>
      <div className="row" style={{ gap: 8, marginBottom: 10 }}>
        <select className="input sm" value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)} style={{ width: 170 }}>
          <option value="">Open findings</option>
          <option value="resolved">Resolved</option>
          <option value="risk_accepted">Risk accepted</option>
          <option value="exception">Exception</option>
        </select>
        <div style={{ flex: 1 }} />
        <span className="faint" style={{ fontSize: 12, alignSelf: "center" }}>{rows.length} findings</span>
      </div>
      {loading ? <Loading card={false} /> : rows.length === 0 ? (
        <div className="muted" style={{ padding: 12 }}>No findings. Protection gaps, offline devices and unmanaged assets appear here.</div>
      ) : rows.map((f) => (
        <div key={f.id} className="row" style={{ gap: 10, alignItems: "center", padding: "10px 4px", borderTop: "1px solid var(--border-soft)" }}>
          <Pill tone={SEV_TONE[f.severity] || "info"} dot>{f.severity}</Pill>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontWeight: 600 }}>{f.title}</div>
            <div className="faint" style={{ fontSize: 12 }}>{f.description} {f.occurrence_count > 1 ? `· seen ${f.occurrence_count}×` : ""}</div>
          </div>
          {f.remediation?.route && <a className="btn ghost sm" href={f.remediation.route}>{f.remediation.label || "Fix"}</a>}
          {f.status !== "resolved" && <button className="btn ghost sm" onClick={() => act(f, "resolved")} title="Mark resolved"><Icon name="check" size={13} /></button>}
          {f.status !== "risk_accepted" && <button className="btn ghost sm" onClick={() => act(f, "risk_accepted")} title="Accept risk"><Icon name="shield" size={13} /></button>}
        </div>
      ))}
    </Card>
  );
}

function ProvidersTab({ providers }: { providers: Provider[] }) {
  return (
    <div className="row" style={{ gap: 12, flexWrap: "wrap" }}>
      {providers.length === 0 && <Card><div className="muted">No providers have reported yet.</div></Card>}
      {providers.map((p) => (
        <Card key={p.provider} style={{ minWidth: 280, flex: "1 1 280px" }}>
          <div className="spread" style={{ marginBottom: 8 }}>
            <span className="row" style={{ gap: 8 }}><Icon name={(PROVIDER_ICON[p.provider] || "grid") as any} size={16} /><b style={{ textTransform: "capitalize" }}>{p.provider}</b></span>
            <Pill tone={p.connection_status === "ok" ? "ok" : p.connection_status === "not_configured" ? "info" : "warn"} dot>
              {p.connection_status === "ok" ? "healthy" : p.connection_status.replace("_", " ")}
            </Pill>
          </div>
          <Row k="Signals" v={p.signals_produced.toLocaleString()} />
          <Row k="Objects" v={p.objects_processed.toLocaleString()} />
          <Row k="Last success" v={p.last_success_at ? timeAgo(p.last_success_at) : "—"} />
          {p.errors > 0 && <Row k="Errors" v={String(p.errors)} />}
          {p.last_error && <div style={{ color: "var(--danger-c)", fontSize: 12, marginTop: 4 }}>{p.last_error}</div>}
          {p.capabilities?.length > 0 && (
            <div className="row" style={{ gap: 4, flexWrap: "wrap", marginTop: 8 }}>
              {p.capabilities.map((c) => <Pill key={c} tone="info">{c}</Pill>)}
            </div>
          )}
        </Card>
      ))}
    </div>
  );
}
