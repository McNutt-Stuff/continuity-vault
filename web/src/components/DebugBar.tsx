// Live debug overlay — a per-account footer bar exposing behind-the-scenes
// diagnostics for live troubleshooting. Enabled via the debug_overlay_enabled
// feature flag (self-toggle from the account menu). Collapsed, it shows the active
// node, serving-index completeness and the last response time; expanded, it shows
// a rolling request/response/timing history plus the live server-side picture
// (heartbeats, replication freshness, recent tenant errors).
import { useEffect, useMemo, useRef, useState } from "react";
import { Icon } from "./Icon";
import { api, DebugCall, getDebugCalls, subscribeDebugCalls, clearDebugCalls } from "../api";

interface LiveDiag {
  server_time: string;
  db_ping_ms: number | null;
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

type Tab = "requests" | "server" | "errors";

export function DebugBar({ onDisable }: { onDisable?: () => void }) {
  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState<Tab>("requests");
  const [calls, setCalls] = useState<DebugCall[]>(getDebugCalls());
  const [live, setLive] = useState<LiveDiag | null>(null);
  const [liveErr, setLiveErr] = useState<string>("");
  const timer = useRef<number | null>(null);

  useEffect(() => subscribeDebugCalls((c) => setCalls(c.slice())), []);

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

  return (
    <div style={{ position: "fixed", left: 0, right: 0, bottom: 0, zIndex: 4000, pointerEvents: "none" }}>
      {open && (
        <div style={{ pointerEvents: "auto", maxHeight: "46vh", overflow: "hidden",
          background: "var(--surface)", borderTop: "1px solid var(--border)",
          boxShadow: "0 -8px 24px rgba(0,0,0,.28)", display: "flex", flexDirection: "column" }}>
          <div className="row" style={{ gap: 6, padding: "6px 10px", borderBottom: "1px solid var(--border-soft)" }}>
            {(["requests", "server", "errors"] as Tab[]).map((t) => (
              <button key={t} className={"btn sm " + (tab === t ? "primary" : "ghost")}
                onClick={() => setTab(t)} style={{ textTransform: "capitalize" }}>
                {t}{t === "requests" ? ` · ${calls.length}` : ""}{t === "errors" && live ? ` · ${live.recent_errors.length}` : ""}
              </button>
            ))}
            <div style={{ flex: 1 }} />
            <button className="btn ghost sm" onClick={() => clearDebugCalls()} title="Clear request history"><Icon name="trash" size={12} /> Clear</button>
            <button className="btn ghost sm" onClick={() => void loadLive()} title="Refresh diagnostics"><Icon name="repeat" size={12} /></button>
          </div>
          <div style={{ overflow: "auto", padding: "8px 10px", fontSize: 12, fontFamily: "var(--mono, ui-monospace, monospace)" }}>
            {tab === "requests" && (
              <table style={{ width: "100%", borderCollapse: "collapse" }}>
                <tbody>
                  {calls.slice().reverse().map((c) => (
                    <tr key={c.id} style={{ borderBottom: "1px solid var(--border-soft)" }}>
                      <td style={{ padding: "3px 6px", color: "var(--faint)", whiteSpace: "nowrap" }}>{new Date(c.ts).toLocaleTimeString()}</td>
                      <td style={{ padding: "3px 6px", fontWeight: 700, whiteSpace: "nowrap" }}>{c.method}</td>
                      <td style={{ padding: "3px 6px", color: statusTone(c.status, c.ok), fontWeight: 700, whiteSpace: "nowrap" }}>{c.status || "ERR"}</td>
                      <td style={{ padding: "3px 6px", textAlign: "right", whiteSpace: "nowrap", color: c.ms > 800 ? "var(--warn)" : "var(--muted)" }}>{c.ms}ms</td>
                      <td style={{ padding: "3px 6px", wordBreak: "break-all" }}>{c.path}{c.error ? <span style={{ color: "var(--danger)" }}> — {c.error}</span> : null}</td>
                    </tr>
                  ))}
                  {calls.length === 0 && <tr><td className="muted" style={{ padding: 8 }}>No requests captured yet.</td></tr>}
                </tbody>
              </table>
            )}
            {tab === "server" && (
              <div className="stack" style={{ gap: 8 }}>
                {liveErr && <div style={{ color: "var(--danger)" }}>{liveErr}</div>}
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
                {liveErr && <div style={{ color: "var(--danger)" }}>{liveErr}</div>}
                {live?.recent_errors.length === 0 && <div className="muted">No recent warnings or errors for this tenant.</div>}
                {live?.recent_errors.map((e, i) => (
                  <div key={i} style={{ borderBottom: "1px solid var(--border-soft)", padding: "3px 0" }}>
                    <span style={{ color: e.level === "critical" || e.level === "error" ? "var(--danger)" : "var(--warn)", fontWeight: 700 }}>{e.level.toUpperCase()}</span>
                    <span className="faint"> {e.ts ? new Date(e.ts + "Z").toLocaleTimeString() : ""} · {e.source}{e.logger ? ` · ${e.logger}` : ""}</span>
                    <div style={{ wordBreak: "break-word" }}>{e.message}</div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}
      {/* Collapsed status strip — always visible when the overlay is enabled. */}
      <div className="row" onClick={() => setOpen((o) => !o)} style={{ pointerEvents: "auto", cursor: "pointer",
        gap: 12, padding: "4px 12px", fontSize: 11.5, alignItems: "center",
        background: "var(--surface)", borderTop: "1px solid var(--border)",
        fontFamily: "var(--mono, ui-monospace, monospace)" }}>
        <span className="row" style={{ gap: 5, fontWeight: 700 }}><Icon name="activity" size={12} /> DEBUG</span>
        <span title="Node currently serving this tenant">
          <Icon name="server" size={11} /> {live?.active.node_name || "…"}
          {live && !live.active.online && <span style={{ color: "var(--danger)" }}> offline</span>}
        </span>
        <span title="Active serving-index completeness" style={{ color: indexComplete ? "var(--muted)" : "var(--warn)" }}>
          <Icon name="database" size={11} /> {idx ? `${idx.pct}%` : "…"} {idx && !idx.complete ? "seeding" : ""}
        </span>
        <span title="Last API response time" style={{ color: (lastMs ?? 0) > 800 ? "var(--warn)" : "var(--muted)" }}>
          <Icon name="clock" size={11} /> {lastMs == null ? "—" : `${lastMs}ms`}
        </span>
        {errorCount > 0 && <span style={{ color: "var(--danger)" }} title="Failed requests this session"><Icon name="alert" size={11} /> {errorCount}</span>}
        {live?.db_ping_ms != null && <span className="faint" title="DB round-trip">db {live.db_ping_ms}ms</span>}
        <div style={{ flex: 1 }} />
        <span className="faint">{open ? "click to collapse" : "click for history"}</span>
        {onDisable && <button className="btn ghost sm" title="Turn off the debug overlay" onClick={(e) => { e.stopPropagation(); onDisable(); }}><Icon name="x" size={11} /></button>}
        <span style={{ fontSize: 10 }}>{open ? "▾" : "▴"}</span>
      </div>
    </div>
  );
}

function DRow({ k, v, tone }: { k: string; v: string; tone?: "warn" | "danger" }) {
  return (
    <div className="row" style={{ gap: 8 }}>
      <span className="faint" style={{ minWidth: 130 }}>{k}</span>
      <span style={{ color: tone === "danger" ? "var(--danger)" : tone === "warn" ? "var(--warn)" : "inherit", wordBreak: "break-all" }}>{v}</span>
    </div>
  );
}
