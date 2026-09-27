// Live debug overlay — a per-account footer bar exposing behind-the-scenes
// diagnostics for live troubleshooting. Enabled via the debug_overlay_enabled
// feature flag (self-toggle from the account menu). Collapsed, it shows the active
// node, serving-index completeness and the last response time; expanded, it shows
// a rolling request/response/timing history plus the live server-side picture
// (heartbeats, replication freshness, recent tenant errors).
import { useEffect, useMemo, useRef, useState } from "react";
import { Icon } from "./Icon";
import { api, DebugCall, RouteHop, getDebugCalls, subscribeDebugCalls, clearDebugCalls } from "../api";

interface LiveDiag {
  server_time: string;
  db_ping_ms: number | null;
  is_admin?: boolean;
  tenant: { id: string; name: string; type: string };
  user: { id: string; email: string };
  active: {
    hosted: string; node_id: string | null; node_name: string; role: string;
    endpoint: string | null; online: boolean;
    last_heartbeat_at: string | null; last_sync_at: string | null; last_log_push_at: string | null;
  };
  serving_index: {
    active_index_count: number; cp_index_count: number;
    active_receipt_count: number; cp_receipt_count: number;
    pct: number; complete: boolean; checked_at: string | null;
  };
  placement: {
    state: string; standby_node_name: string | null; standby_online: boolean;
    standby_ready: boolean; standby_pending: number; standby_synced_at: string | null;
  };
  recent_errors: { ts: string | null; level: string; source: string; logger: string; message: string; resource: string }[];
}

function ago(iso: string | null): string {
  if (!iso) return "—";
  const s = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

function statusTone(status: number, ok: boolean): string {
  if (ok) return "var(--ok)";
  if (status === 0) return "var(--danger)";
  if (status >= 500) return "var(--danger)";
  if (status === 401 || status === 403) return "var(--warn)";
  return "var(--warn)";
}

// Label for a hop — the real hostname (control-plane shows its host too).
function hopLabel(h: RouteHop): string {
  if (h.name) return h.role === "control-plane" ? `CP (${h.name})` : h.name;
  return h.role === "control-plane" ? "CP" : "node";
}

// Compact end-to-end request chain from the server's per-hop breadcrumbs.
function chainLabel(c: DebugCall): string {
  if (c.status === 0) return "browser ✗ network";
  if (c.chain && c.chain.length) return "browser → " + c.chain.map(hopLabel).join(" → ");
  if (c.route === "cp->node") return `browser → CP → ${c.node || "node"}`;
  if (c.route === "node") return `browser → ${c.node || "node"}`;
  return "browser → CP";
}

// The compact route shown in the Requests row (hops joined by →).
function chainShort(c: DebugCall): string {
  if (c.chain && c.chain.length) return c.chain.map(hopLabel).join(" → ");
  if (c.route === "cp->node") return `CP → ${c.node || "node"}`;
  if (c.route === "node") return c.node || "node";
  return "CP";
}

type Tab = "requests" | "server" | "errors" | "fleet";

interface FleetNode {
  node_id: string; name: string; endpoint: string;
  reachable: boolean | null; ms: number | null; error: string | null;
  chain: RouteHop[];
}

export function DebugBar({ onDisable }: { onDisable?: () => void }) {
  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState<Tab>("requests");
  const [calls, setCalls] = useState<DebugCall[]>(getDebugCalls());
  const [live, setLive] = useState<LiveDiag | null>(null);
  const [liveErr, setLiveErr] = useState<string>("");
  const [selected, setSelected] = useState<DebugCall | null>(null);
  const [fleet, setFleet] = useState<FleetNode[] | null>(null);
  const [fleetErr, setFleetErr] = useState<string>("");
  const [fleetLoading, setFleetLoading] = useState(false);
  const timer = useRef<number | null>(null);

  useEffect(() => subscribeDebugCalls((c) => setCalls(c.slice())), []);

  const loadFleet = async () => {
    setFleetLoading(true); setFleetErr("");
    try { setFleet((await api.get<{ nodes: FleetNode[] }>("/debug-panel/fleet-path")).nodes); }
    catch (e: any) { setFleetErr(e?.message || "failed to probe the fleet"); }
    finally { setFleetLoading(false); }
  };
  useEffect(() => { if (tab === "fleet" && fleet === null && !fleetLoading) void loadFleet(); }, [tab]);

  const loadLive = async () => {
    try {
      setLive(await api.get<LiveDiag>("/debug-panel/live"));
      setLiveErr("");
    } catch (e: any) {
      setLiveErr(e?.message || "failed to load diagnostics");
    }
  };

  // Poll the server-side picture: often while expanded, gently while collapsed.
  useEffect(() => {
    void loadLive();
    const period = open ? 4000 : 15000;
    timer.current = window.setInterval(loadLive, period);
    return () => { if (timer.current) window.clearInterval(timer.current); };
  }, [open]);

  const lastMs = calls.length ? calls[calls.length - 1].ms : null;
  const errorCount = useMemo(() => calls.filter((c) => !c.ok).length, [calls]);
  const idx = live?.serving_index;
  const indexComplete = idx ? idx.complete : true;
  const mono = "var(--mono, ui-monospace, monospace)";

  return (
    <div className="debug-dock">
      {open && (
        <div style={{ maxHeight: "42vh", minHeight: 0, overflow: "hidden",
          display: "flex", flexDirection: "column", background: "var(--panel)",
          borderBottom: "1px solid var(--border-soft)" }}>
          <div className="row" style={{ gap: 6, padding: "6px 10px", borderBottom: "1px solid var(--border-soft)", background: "var(--bg-elev)" }}>
            {(["requests", "server", "errors", ...(live?.is_admin ? ["fleet"] : [])] as Tab[]).map((t) => (
              <button key={t} className={"btn sm " + (tab === t ? "primary" : "ghost")}
                onClick={() => setTab(t)} style={{ textTransform: "capitalize" }}>
                {t}{t === "requests" ? ` · ${calls.length}` : ""}{t === "errors" && live ? ` · ${live.recent_errors.length}` : ""}{t === "fleet" && fleet ? ` · ${fleet.length}` : ""}
              </button>
            ))}
            <div style={{ flex: 1 }} />
            {tab === "fleet" && <button className="btn ghost sm" onClick={() => void loadFleet()} title="Re-probe the fleet"><Icon name="repeat" size={12} /> Probe</button>}
            <button className="btn ghost sm" onClick={() => clearDebugCalls()} title="Clear request history"><Icon name="trash" size={12} /> Clear</button>
            <button className="btn ghost sm" onClick={() => void loadLive()} title="Refresh diagnostics"><Icon name="repeat" size={12} /></button>
          </div>
          <div style={{ overflow: "auto", padding: "8px 10px", fontSize: 12, fontFamily: mono, background: "var(--panel)" }}>
            {tab === "requests" && (
              <table style={{ width: "100%", borderCollapse: "collapse" }}>
                <tbody>
                  {calls.slice().reverse().map((c) => (
                    <tr key={c.id} onClick={() => setSelected(c)} style={{ borderBottom: "1px solid var(--border-soft)", cursor: "pointer" }} title="Click to inspect request & response">
                      <td style={{ padding: "3px 6px", color: "var(--text-faint)", whiteSpace: "nowrap" }}>{new Date(c.ts).toLocaleTimeString()}</td>
                      <td style={{ padding: "3px 6px", fontWeight: 700, whiteSpace: "nowrap" }}>{c.method}</td>
                      <td style={{ padding: "3px 6px", color: statusTone(c.status, c.ok), fontWeight: 700, whiteSpace: "nowrap" }}>{c.status || "ERR"}</td>
                      <td style={{ padding: "3px 6px", textAlign: "right", whiteSpace: "nowrap", color: c.ms > 800 ? "var(--warn)" : "var(--text-dim)" }}
                          title={`round-trip ${c.ms}ms${c.serverMs != null ? ` · server ${c.serverMs}ms` : ""}${c.upstreamMs != null ? ` · node hop ${c.upstreamMs}ms` : ""}`}>
                        {c.ms}ms{c.upstreamMs != null ? <span style={{ color: "var(--text-faint)" }}> ({c.upstreamMs})</span> : null}
                      </td>
                      <td style={{ padding: "3px 6px", whiteSpace: "nowrap", color: (c.chain && c.chain.length > 1) || c.route === "cp->node" ? "var(--warn)" : "var(--text-faint)" }} title={chainLabel(c)}>
                        {chainShort(c)}
                      </td>
                      <td style={{ padding: "3px 6px", wordBreak: "break-all" }}>{c.path}{c.error ? <span style={{ color: "var(--danger-c)" }}> — {c.error}</span> : null}</td>
                    </tr>
                  ))}
                  {calls.length === 0 && <tr><td className="muted" style={{ padding: 8 }}>No requests captured yet.</td></tr>}
                </tbody>
              </table>
            )}
            {tab === "server" && (
              <div className="stack" style={{ gap: 8 }}>
                {liveErr && <div style={{ color: "var(--danger-c)" }}>{liveErr}</div>}
                {live && (
                  <>
                    <DRow k="Tenant" v={`${live.tenant.name} · ${live.tenant.id}`} />
                    <DRow k="Serving node" v={`${live.active.node_name} (${live.active.role}) · ${live.active.hosted}${live.active.online ? "" : " · OFFLINE"}`} tone={live.active.online ? undefined : "danger"} />
                    {live.active.endpoint && <DRow k="Node endpoint" v={live.active.endpoint} />}
                    <DRow k="DB ping" v={live.db_ping_ms == null ? "—" : `${live.db_ping_ms}ms`} tone={(live.db_ping_ms ?? 0) > 200 ? "warn" : undefined} />
                    <DRow k="Heartbeat" v={ago(live.active.last_heartbeat_at)} />
                    <DRow k="Last config sync" v={ago(live.active.last_sync_at)} />
                    <DRow k="Last log push" v={ago(live.active.last_log_push_at)} />
                    <DRow k="Serving index" v={`${live.serving_index.active_index_count.toLocaleString()} / ${live.serving_index.cp_index_count.toLocaleString()} (${live.serving_index.pct}%)${live.serving_index.complete ? " · in sync" : " · seeding"}`} tone={live.serving_index.complete ? undefined : "warn"} />
                    <DRow k="Receipts" v={`${live.serving_index.active_receipt_count.toLocaleString()} / ${live.serving_index.cp_receipt_count.toLocaleString()}`} />
                    <DRow k="Index checked" v={ago(live.serving_index.checked_at)} />
                    {live.placement.standby_node_name && (
                      <DRow k="Standby" v={`${live.placement.standby_node_name}${live.placement.standby_online ? "" : " · offline"} · ${live.placement.standby_ready ? "in sync" : `syncing (${live.placement.standby_pending} pending)`}`} tone={live.placement.standby_ready ? undefined : "warn"} />
                    )}
                    {live.placement.state && <DRow k="Placement" v={live.placement.state} tone="warn" />}
                    <DRow k="Server time" v={new Date(live.server_time + "Z").toLocaleTimeString()} />
                  </>
                )}
              </div>
            )}
            {tab === "errors" && (
              <div className="stack" style={{ gap: 4 }}>
                {liveErr && <div style={{ color: "var(--danger-c)" }}>{liveErr}</div>}
                {live?.recent_errors.length === 0 && <div className="muted">No recent warnings or errors for this tenant.</div>}
                {live?.recent_errors.map((e, i) => (
                  <div key={i} style={{ borderBottom: "1px solid var(--border-soft)", padding: "3px 0" }}>
                    <span style={{ color: e.level === "critical" || e.level === "error" ? "var(--danger-c)" : "var(--warn)", fontWeight: 700 }}>{e.level.toUpperCase()}</span>
                    <span style={{ color: "var(--text-faint)" }}> {e.ts ? new Date(e.ts + "Z").toLocaleTimeString() : ""} · {e.source}{e.logger ? ` · ${e.logger}` : ""}</span>
                    <div style={{ wordBreak: "break-word" }}>{e.message}</div>
                  </div>
                ))}
              </div>
            )}
            {tab === "fleet" && (
              <div className="stack" style={{ gap: 6 }}>
                <div style={{ color: "var(--text-faint)", marginBottom: 2 }}>
                  Live CP→node round trip to every customer node (fleet-authed). Your own
                  requests only proxy when your tenant is node-hosted; this probes the path regardless.
                </div>
                {fleetLoading && <div className="muted">Probing the fleet…</div>}
                {fleetErr && <div style={{ color: "var(--danger-c)" }}>{fleetErr}</div>}
                {fleet && fleet.length === 0 && <div className="muted">No customer nodes in the fleet.</div>}
                {fleet?.map((f) => (
                  <div key={f.node_id} className="row" style={{ gap: 10, alignItems: "center", borderBottom: "1px solid var(--border-soft)", padding: "4px 0" }}>
                    <span style={{ color: f.reachable ? "var(--ok)" : "var(--danger-c)", fontWeight: 700, minWidth: 60 }}>
                      {f.reachable ? "OK" : "DOWN"}
                    </span>
                    <span style={{ flex: 1, wordBreak: "break-all" }}>
                      {f.chain.map(hopLabel).join(" → ")}
                      {f.error ? <span style={{ color: "var(--danger-c)" }}> — {f.error}</span> : null}
                    </span>
                    <span style={{ color: (f.ms ?? 0) > 500 ? "var(--warn)" : "var(--text-dim)", whiteSpace: "nowrap" }}>
                      {f.ms == null ? "—" : `${f.ms}ms`}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}
      {/* Collapsed status strip — always visible when the overlay is enabled. */}
      <div className="row" onClick={() => setOpen((o) => !o)} style={{ cursor: "pointer",
        gap: 12, padding: "5px 12px", fontSize: 11.5, alignItems: "center",
        background: "var(--bg-elev)", fontFamily: mono }}>
        <span className="row" style={{ gap: 5, fontWeight: 700 }}><Icon name="activity" size={12} /> DEBUG</span>
        <span title="Node currently serving this tenant">
          <Icon name="server" size={11} /> {live?.active.node_name || "…"}
          {live && !live.active.online && <span style={{ color: "var(--danger-c)" }}> offline</span>}
        </span>
        <span title="Active serving-index completeness" style={{ color: indexComplete ? "var(--text-dim)" : "var(--warn)" }}>
          <Icon name="database" size={11} /> {idx ? `${idx.pct}%` : "…"} {idx && !idx.complete ? "seeding" : ""}
        </span>
        <span title="Last API response time" style={{ color: (lastMs ?? 0) > 800 ? "var(--warn)" : "var(--text-dim)" }}>
          <Icon name="clock" size={11} /> {lastMs == null ? "—" : `${lastMs}ms`}
        </span>
        {errorCount > 0 && <span style={{ color: "var(--danger-c)" }} title="Failed requests this session"><Icon name="alert" size={11} /> {errorCount}</span>}
        {live?.db_ping_ms != null && <span style={{ color: "var(--text-faint)" }} title="DB round-trip">db {live.db_ping_ms}ms</span>}
        <div style={{ flex: 1 }} />
        <span style={{ color: "var(--text-faint)" }}>{open ? "click to collapse" : "click for history"}</span>
        {onDisable && <button className="btn ghost sm" title="Turn off the debug overlay" onClick={(e) => { e.stopPropagation(); onDisable(); }}><Icon name="x" size={11} /></button>}
        <span style={{ fontSize: 10 }}>{open ? "▾" : "▴"}</span>
      </div>
      {selected && <CallModal call={selected} onClose={() => setSelected(null)} />}
    </div>
  );
}

function CallModal({ call, onClose }: { call: DebugCall; onClose: () => void }) {
  const mono = "var(--mono, ui-monospace, monospace)";
  const copy = (s?: string) => { if (s) void navigator.clipboard?.writeText(s); };
  return (
    <div onClick={onClose} style={{ position: "fixed", inset: 0, zIndex: 5000,
      background: "rgba(0,0,0,.5)", display: "flex", alignItems: "center", justifyContent: "center", padding: 24 }}>
      <div onClick={(e) => e.stopPropagation()} style={{ width: "min(880px, 96vw)", maxHeight: "86vh",
        display: "flex", flexDirection: "column", background: "var(--panel)",
        border: "1px solid var(--border)", borderRadius: "var(--radius)", boxShadow: "var(--shadow)" }}>
        <div className="row" style={{ gap: 8, padding: "12px 16px", borderBottom: "1px solid var(--border-soft)", alignItems: "center" }}>
          <span style={{ fontWeight: 800, color: statusTone(call.status, call.ok) }}>{call.status || "ERR"}</span>
          <span style={{ fontWeight: 700 }}>{call.method}</span>
          <span style={{ fontFamily: mono, fontSize: 12.5, wordBreak: "break-all" }}>{call.path}</span>
          <div style={{ flex: 1 }} />
          <button className="btn ghost sm" onClick={onClose}><Icon name="x" size={14} /></button>
        </div>
        <div style={{ overflow: "auto", padding: 16 }}>
          <div className="row" style={{ gap: 16, flexWrap: "wrap", marginBottom: 12, fontSize: 12.5 }}>
            <span><b>Chain:</b> {chainLabel(call)}</span>
            <span><b>Round-trip:</b> {call.ms}ms</span>
            {call.serverMs != null && <span><b>Server:</b> {call.serverMs}ms</span>}
            {call.upstreamMs != null && <span><b>Node hop:</b> {call.upstreamMs}ms</span>}
            <span><b>At:</b> {new Date(call.ts).toLocaleString()}</span>
          </div>
          {call.chain && call.chain.length > 0 && (
            <div style={{ marginBottom: 12 }}>
              <div style={{ fontWeight: 700, fontSize: 12.5, marginBottom: 4 }}>Proxy path</div>
              <div className="stack" style={{ gap: 2, fontFamily: mono, fontSize: 12 }}>
                <div className="row" style={{ gap: 8 }}><span style={{ minWidth: 130, color: "var(--text-faint)" }}>browser</span><span /></div>
                {call.chain.map((h, i) => (
                  <div key={i} className="row" style={{ gap: 8 }}>
                    <span style={{ minWidth: 130 }}>{"→ ".repeat(1)}{h.role === "control-plane" ? "control plane" : "node"} · {h.name}</span>
                    <span style={{ color: h.error ? "var(--danger-c)" : "var(--text-dim)" }}>
                      {h.error ? h.error : `${h.ms != null ? `${h.ms}ms` : "—"}${h.upstream_ms != null ? ` · CP↔node ${h.upstream_ms}ms` : ""}`}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}
          {call.error && <div style={{ color: "var(--danger-c)", marginBottom: 12 }}>{call.error}</div>}
          <Section title={`Request${call.method === "GET" ? " (query only)" : ""}`} body={call.reqBody} mono={mono} onCopy={() => copy(call.reqBody)} empty="No request body (GET / no payload)." />
          <Section title="Response" body={call.respBody} mono={mono} onCopy={() => copy(call.respBody)} empty="No response body captured (binary, 204, or network failure)." />
        </div>
      </div>
    </div>
  );
}

function Section({ title, body, mono, onCopy, empty }: { title: string; body?: string; mono: string; onCopy: () => void; empty: string }) {
  const shown = body ? prettyJson(body) : undefined;
  return (
    <div style={{ marginBottom: 14 }}>
      <div className="row" style={{ gap: 8, marginBottom: 4 }}>
        <div style={{ fontWeight: 700, fontSize: 12.5 }}>{title}</div>
        {body && <button className="btn ghost sm" onClick={onCopy} title="Copy"><Icon name="file" size={12} /> Copy</button>}
      </div>
      {shown
        ? <pre style={{ margin: 0, padding: 10, background: "var(--code-bg)", borderRadius: "var(--radius-sm)",
            fontFamily: mono, fontSize: 12, whiteSpace: "pre-wrap", wordBreak: "break-word", maxHeight: "34vh", overflow: "auto" }}>{shown}</pre>
        : <div className="muted" style={{ fontSize: 12 }}>{empty}</div>}
    </div>
  );
}

function prettyJson(s: string): string {
  try { return JSON.stringify(JSON.parse(s), null, 2); } catch { return s; }
}

function DRow({ k, v, tone }: { k: string; v: string; tone?: "warn" | "danger" }) {
  return (
    <div className="row" style={{ gap: 8 }}>
      <span style={{ color: "var(--text-faint)", minWidth: 130 }}>{k}</span>
      <span style={{ color: tone === "danger" ? "var(--danger-c)" : tone === "warn" ? "var(--warn)" : "inherit", wordBreak: "break-all" }}>{v}</span>
    </div>
  );
}
