import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { useAuth } from "../auth";
import { Card, Pill, timeAgo, serverDate, fmtAbsolute, Loading } from "../components/ui";
import { Icon } from "../components/Icon";
import { BrandIcon } from "../components/BrandIcon";
import { notify } from "../components/dialog";

interface Agent {
  id: string; name: string; hostname: string; platform: string; version: string;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  state: string; collectors: string[]; enabled_collectors?: string[]; config: any; telemetry: any;
  node_name?: string | null; node_url?: string | null;
  last_heartbeat_at: string | null; last_collection_at: string | null;
}

// Per-collector display metadata for the Agents → Collectors toggles.
const COLLECTOR_META: Record<string, { label: string; desc: string; brand?: string }> = {
  onepassword: { label: "1Password", desc: "Passwords & secure items via the op CLI", brand: "onepassword" },
  endpoint_files: { label: "Endpoint files", desc: "Folders you select in the Data Map" },
  imessage: { label: "Apple Messages", desc: "iMessage/SMS, group threads & attachments" },
  outlook_local: { label: "Outlook (on this Mac)", desc: "Local email, attachments, contacts, calendar & notes" },
};

// Online = a heartbeat within the last ~90s.
function isOnline(a: Agent): boolean {
  if (!a.last_heartbeat_at) return false;
  return (Date.now() - serverDate(a.last_heartbeat_at).getTime()) / 1000 < 90;
}

type HealthLevel = "healthy" | "warning" | "critical";
function healthOf(a: Agent): { level: HealthLevel; label: string } {
  const t = a.telemetry || {};
  if (!isOnline(a)) return { level: "warning", label: "Offline" };
  if (t.op_available === false) return { level: "warning", label: "1Password CLI missing" };
  if (t.op_auth === "unauthenticated") return { level: "warning", label: "1Password locked" };
  return { level: "healthy", label: "Healthy" };
}

const HEALTH_COLOR: Record<HealthLevel, string> = {
  healthy: "#35d0a5", warning: "#f5a623", critical: "#f2545b",
};

function StatusDot({ color, pulse }: { color: string; pulse?: boolean }) {
  return (
    <span style={{
      width: 9, height: 9, borderRadius: "50%", background: color, flex: "none",
      boxShadow: `0 0 0 3px ${color}22`, display: "inline-block",
      animation: pulse ? "badge-pulse 1.6s ease-in-out infinite" : undefined,
    }} />
  );
}

function OnlinePill({ a }: { a: Agent }) {
  const online = isOnline(a);
  return (
    <span className="row" style={{ gap: 6, alignItems: "center" }}>
      <StatusDot color={online ? "#35d0a5" : "#6b7688"} pulse={online} />
      <span style={{ fontSize: 12, fontWeight: 600, color: online ? "#35d0a5" : "var(--faint,#8892a6)" }}>
        {online ? "Online" : "Offline"}
      </span>
    </span>
  );
}

function HealthPill({ a }: { a: Agent }) {
  const h = healthOf(a);
  return (
    <span className="row" style={{ gap: 6, alignItems: "center" }}>
      <StatusDot color={HEALTH_COLOR[h.level]} />
      <span style={{ fontSize: 12, fontWeight: 600, color: HEALTH_COLOR[h.level] }}>{h.label}</span>
    </span>
  );
}

export default function Agents() {
  const { me, stepUp } = useAuth();
  const [agents, setAgents] = useState<Agent[]>([]);
  const [code, setCode] = useState<string | null>(null);
  const [toast, setToast] = useState("");
  const [selected, setSelected] = useState<Agent | null>(null);
  const [loaded, setLoaded] = useState(false);

  async function load() {
    try {
      const list = await api.get<Agent[]>("/agents");
      setAgents(list);
      // Keep the current selection if it still exists; never auto-select on load.
      setSelected((d) => (d ? list.find((a) => a.id === d.id) ?? null : null));
    } finally {
      setLoaded(true);
    }
  }
  function select(a: Agent) { setSelected(a); }
  useEffect(() => {
    void load();
    const t = setInterval(load, 8000);
    return () => clearInterval(t);
  }, []);

  function flash(msg: string) {
    setToast(msg);
    setTimeout(() => setToast(""), 3000);
  }

  async function newCode() {
    const res = await api.post<{ code: string }>("/agents/linking-code", {
      name: "Mac Agent",
      collectors: ["onepassword"],
    });
    setCode(res.code);
  }

  async function downloadInstaller() {
    if (!me?.passkey_verified) {
      try { await stepUp(); } catch (e) { return notify({ message: (e as Error).message, tone: "danger" }); }
    }
    const res = await api.post<{ filename: string; script: string }>("/agents/installer", {
      name: "Mac Agent", collectors: ["onepassword"], destinations: ["cv-cloud"],
    });
    const blob = new Blob([res.script], { type: "text/x-shellscript" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = res.filename;
    a.click();
    URL.revokeObjectURL(url);
    flash("Installer downloaded — run it on your Mac to link this agent");
  }

  async function command(a: Agent, type: string) {
    await api.post(`/agents/${a.id}/command`, { type, params: {} });
    flash(`${type} queued for ${a.name}`);
  }

  if (!loaded && agents.length === 0) return <Loading label="Loading agents…" />;

  return (
    <div className="grid grid-2" style={{ alignItems: "start" }}>
      <div style={{ minWidth: 0 }}>
        <Card style={{ marginBottom: 16 }}>
          <div className="spread" style={{ marginBottom: 8 }}>
            <h2>Install a desktop agent</h2>
            <div className="row">
              <button className="btn sm" onClick={newCode}>
                <Icon name="link" size={14} /> Linking code
              </button>
              <button className="btn primary sm" onClick={downloadInstaller}>
                <Icon name="logout" size={14} /> Download Mac installer
              </button>
            </div>
          </div>
          <div className="muted" style={{ fontSize: 13 }}>
            Desktop agents collect locally with native tools (like the 1Password CLI) and push
            <b> client-side–encrypted</b> data to the platform — the cloud never sees plaintext.
            Download the installer (linking code baked in), run it on a Mac, and it installs a
            background menu-bar app bundled with the 1Password CLI.
          </div>
          {code && (
            <>
              <div className="card" style={{ marginTop: 14, textAlign: "center", background: "var(--bg-elev)" }}>
                <div className="faint" style={{ fontSize: 12 }}>Agent linking code (valid 15 min)</div>
                <div className="mono" style={{ fontSize: 26, letterSpacing: 2, margin: "8px 0" }}>{code}</div>
              </div>
              <div className="mono faint" style={{ fontSize: 11, marginTop: 10, wordBreak: "break-all" }}>
                ARKIVE_LINKING_CODE={code} ./installers/desktop-agent-install-macos.sh
              </div>
            </>
          )}
        </Card>

        {agents.map((a) => (
          <div
            key={a.id}
            className={`dest-card ${selected?.id === a.id ? "selected" : ""}`}
            style={{ marginBottom: 12 }}
            onClick={() => select(a)}
          >
            <div className="spread">
              <div className="row">
                <div className="result-icon" style={{ background: "linear-gradient(135deg,#7a5cff,#4f7cff)", width: 36, height: 36 }}>
                  <Icon name="user" size={18} />
                </div>
                <div>
                  <div style={{ fontWeight: 650 }}>{a.name}</div>
                  <div className="faint mono" style={{ fontSize: 11 }}>{a.hostname} · v{a.version}</div>
                </div>
              </div>
              <StatusDot color={isOnline(a) ? "#35d0a5" : "#6b7688"} pulse={isOnline(a)} />
            </div>
            <div className="row" style={{ gap: 12, marginTop: 10, paddingTop: 10, borderTop: "1px solid var(--border-soft)" }}>
              <OnlinePill a={a} />
              <HealthPill a={a} />
            </div>
          </div>
        ))}
        {agents.length === 0 && <Card><div className="muted">No desktop agents linked yet.</div></Card>}
      </div>

      <div style={{ minWidth: 0 }}>
        {selected ? (
          <AgentDetail a={selected} onCommand={(type) => command(selected, type)} reload={load} />
        ) : (
          <Card><div className="muted">Select an agent to view its dashboard.</div></Card>
        )}
      </div>

      {toast && <div className="toast"><Icon name="check" size={15} /> {toast}</div>}
    </div>
  );
}

function Info({ label, value, tone, title }: { label: string; value: string; tone?: "ok" | "warn" | "danger" | "info"; title?: string }) {
  return (
    <div className="stack" title={title}>
      <div className="faint" style={{ fontSize: 11.5 }}>{label}</div>
      {tone ? <Pill tone={tone}>{value}</Pill> : <div style={{ fontWeight: 600, fontSize: 13 }}>{value}</div>}
    </div>
  );
}

function AgentDetail({ a, onCommand, reload }:
  { a: Agent; onCommand: (type: string) => void; reload: () => Promise<void> }) {
  const t = a.telemetry || {};
  const crypto = t.crypto || {};
  const online = isOnline(a);
  const [kv, setKv] = useState<{ title: string; rows: [string, string][] } | null>(null);
  const [verbose, setVerbose] = useState<boolean>(!!a.config?.verbose_logging);
  const [dest, setDest] = useState<string>(() => {
    const arr = a.config?.destinations || ["cv-cloud"];
    if (arr.includes("appliance")) return arr.includes("cv-cloud") ? "both" : "appliance";
    return "cv-cloud";
  });
  const opState: "missing" | "interactive" | "ready" =
    t.op_available === false ? "missing" : t.op_auth === "unauthenticated" ? "interactive" : "ready";
  const logs: string[] = Array.isArray(t.recent_logs) ? t.recent_logs : [];

  function showAdvanced() {
    const rows: [string, string][] = [
      ["Name", a.name],
      ["Hostname", t.hostname || a.hostname],
      ["Agent ID", a.id],
      ["Platform / OS", t.os || a.platform || "—"],
      ["Agent version", a.version],
      ["State", a.state],
      ["Local user", t.local_user || "—"],
      ["Local IP", t.local_ip || "—"],
      ["Public IP", t.public_ip || "—"],
      ["Cloud endpoint", t.cloud_url || "—"],
      ["Collectors", (a.collectors || []).join(", ") || "—"],
      ["Schedule", `${a.config?.schedule_minutes ?? 360} min`],
      ["Content encryption", crypto.content_alg || "AES-256-GCM"],
      ["Quantum-safe", crypto.pq_available ? "enabled (ML-KEM/ML-DSA)" : "classical fallback"],
      ["Recovery escrow", crypto.recovery_escrow === "escrowed"
        ? `escrowed · ${crypto.recovery_kem_alg || "KEM"}` : "pending"],
      ["1Password auth", t.op_auth || "—"],
    ];
    setKv({ title: `${a.name} — details`, rows });
  }

  async function setConfig(patch: Record<string, unknown>) {
    try { await api.put(`/agents/${a.id}/config`, patch); await reload(); }
    catch (e) { await notify({ title: "Couldn't update agent", message: (e as ApiError).message, tone: "danger" }); }
  }

  function toggleCollector(name: string) {
    const s = new Set(a.enabled_collectors || []);
    if (s.has(name)) s.delete(name); else s.add(name);
    void setConfig({ enabled_collectors: [...s] });
  }

  return (
    <>
      <Card style={{ marginBottom: 16 }}>
        <div className="spread" style={{ alignItems: "flex-start", gap: 12 }}>
          <div className="row" style={{ gap: 12, alignItems: "center" }}>
            <div className="result-icon" style={{ background: "linear-gradient(135deg,#7a5cff,#4f7cff)", width: 46, height: 46 }}>
              <Icon name="user" size={22} />
            </div>
            <div>
              <h2 style={{ margin: 0 }}>{a.name}</h2>
              <div className="faint mono" style={{ fontSize: 11.5 }}>{a.hostname} · v{a.version} · {t.os || a.platform}</div>
            </div>
          </div>
          <div className="stack" style={{ alignItems: "flex-end", gap: 8 }}>
            <div className="row" style={{ gap: 16 }}>
              <OnlinePill a={a} />
              <HealthPill a={a} />
            </div>
            <div className="row" style={{ gap: 6 }}>
              <Pill tone="info">Desktop agent</Pill>
              <Pill tone="info"><Icon name="server" size={11} /> {a.node_name || "Control plane"}</Pill>
            </div>
          </div>
        </div>

        {/* Controls header bar */}
        <div className="row" style={{ gap: 8, marginTop: 14, paddingTop: 12, borderTop: "1px solid var(--border-soft)", flexWrap: "wrap", alignItems: "center" }}>
          <button className="btn sm primary" onClick={() => onCommand("collect")}>
            <Icon name="restore" size={13} /> Collect now
          </button>
          <button className="btn sm" onClick={() => onCommand("update")}>
            <Icon name="server" size={13} /> Update
          </button>
          <button className="btn sm" onClick={() => onCommand("reconfigure")}>
            <Icon name="gear" size={13} /> Reconfigure
          </button>
          <button className="btn sm ghost" onClick={showAdvanced}>
            <Icon name="search" size={13} /> Advanced
          </button>
        </div>

        <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 12, marginTop: 14 }}>
          <Info label="Heartbeat" value={timeAgo(a.last_heartbeat_at)} title={fmtAbsolute(a.last_heartbeat_at)} />
          <Info label="Last collection" value={timeAgo(a.last_collection_at)} title={fmtAbsolute(a.last_collection_at)} />
          <Info label="State" value={a.state} tone={online ? "ok" : "warn"} />
          <Info label="Schedule" value={`${a.config?.schedule_minutes ?? 360} min`} />
        </div>
      </Card>

      {/* Collectors */}
      <Card style={{ marginBottom: 16 }}>
        <div className="spread" style={{ marginBottom: 12 }}>
          <h3 style={{ margin: 0 }}>Collectors</h3>
          <span className="faint" style={{ fontSize: 11.5 }}>Toggle what this agent may collect. Off collectors can't be added as a source.</span>
        </div>
        {(a.collectors || []).length === 0 && (
          <div className="faint" style={{ fontSize: 12.5 }}>
            No collectors detected yet — the agent reports what it can collect on its next heartbeat.
          </div>
        )}
        {(a.collectors || []).map((name) => {
          const meta = COLLECTOR_META[name] || { label: name, desc: "" };
          const on = (a.enabled_collectors || []).includes(name);
          const isOp = name === "onepassword";
          const desc = isOp
            ? (opState === "missing" ? "1Password CLI not installed"
              : opState === "interactive" ? "installed · collects interactively"
              : `installed · ${t.op_auth || "ready"}`)
            : meta.desc;
          return (
            <div key={name} className="collector-row" style={{ marginTop: 10 }}>
              <div className="row" style={{ gap: 10 }}>
                {meta.brand ? <BrandIcon name={meta.brand} size={18} /> : <Icon name="database" size={16} />}
                <div>
                  <div style={{ fontWeight: 600, fontSize: 13 }}>{meta.label}</div>
                  <div className="faint" style={{ fontSize: 11.5 }}>{desc}</div>
                </div>
              </div>
              <button className={`btn sm ${on ? "primary" : "ghost"}`} style={{ minWidth: 58 }}
                      title={on ? "Enabled — click to disable" : "Disabled — click to enable"}
                      onClick={() => toggleCollector(name)}>
                {on ? "On" : "Off"}
              </button>
            </div>
          );
        })}
        {opState === "interactive" && (a.enabled_collectors || []).includes("onepassword") && (
          <div className="hint-box" style={{ marginTop: 10 }}>
            1Password collects <b>interactively</b>: open and unlock the 1Password app, then use
            <span className="mono"> Collect now</span>. Unattended background collection needs a
            1Password service account (Business plan).
          </div>
        )}
        <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 12, marginTop: 12 }}>
          <Info label="Last collection" value={timeAgo(a.last_collection_at)} />
          <Info label="Enabled" value={(a.enabled_collectors || []).map((c) => COLLECTOR_META[c]?.label || c).join(", ") || "None"} />
          <Info label="Data routes to" value={a.node_name || "Control plane"} />
        </div>
      </Card>

      {/* Security */}
      <Card style={{ marginBottom: 16 }}>
        <h3 style={{ marginBottom: 12 }}>Security</h3>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 12 }}>
          <Info label="Client encryption" value={crypto.content_alg || "AES-256-GCM"} tone="ok" />
          <Info label="Quantum-safe" value={crypto.pq_available ? "Enabled" : "Classical"} tone={crypto.pq_available ? "ok" : "warn"} />
          <Info label="Recovery escrow" value={crypto.recovery_escrow === "escrowed" ? "Escrowed" : "Pending"} tone={crypto.recovery_escrow === "escrowed" ? "ok" : "warn"} />
        </div>
        <div className="faint" style={{ fontSize: 11.5, marginTop: 10 }}>
          Data is encrypted on this Mac before upload — the cloud never sees plaintext.
        </div>
      </Card>

      {/* Network */}
      <Card style={{ marginBottom: 16 }}>
        <h3 style={{ marginBottom: 12 }}>Network</h3>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 12 }}>
          <Info label="Local IP" value={t.local_ip || "—"} />
          <Info label="Public IP" value={t.public_ip || "—"} />
          <div className="stack">
            <div className="faint" style={{ fontSize: 11.5 }}>Cloud connectivity</div>
            <div className="row" style={{ gap: 6, alignItems: "center" }}>
              <StatusDot color={online ? "#35d0a5" : "#6b7688"} pulse={online} />
              <span style={{ fontWeight: 600, color: online ? "#35d0a5" : undefined }}>{online ? "Connected" : "Disconnected"}</span>
            </div>
          </div>
          <Info label="Channel" value={t.channel_encryption || "TLS 1.3"} tone="ok" />
          <Info label="Local user" value={t.local_user || "—"} />
          <Info label="Reported" value={timeAgo(t.reported_at || a.last_heartbeat_at)} />
        </div>
        <div className="faint" style={{ fontSize: 11.5, marginTop: 10 }}>
          Cloud endpoint: <span className="mono">{t.cloud_url ?? "—"}</span>
        </div>
      </Card>

      {/* Configuration */}
      <Card style={{ marginBottom: 16 }}>
        <h3 style={{ marginBottom: 12 }}>Configuration</h3>
        <div className="collector-row">
          <div className="row" style={{ gap: 10 }}>
            <Icon name="server" size={16} />
            <div>
              <div style={{ fontWeight: 600, fontSize: 13 }}>Backup destination</div>
              <div className="faint" style={{ fontSize: 11.5 }}>Where this agent pushes collected data.</div>
            </div>
          </div>
          <select
            value={dest}
            style={{ padding: "5px 8px", borderRadius: 6 }}
            onChange={(e) => {
              const v = e.target.value;
              setDest(v);
              const destinations = v === "appliance" ? ["appliance"]
                : v === "both" ? ["cv-cloud", "appliance"] : ["cv-cloud"];
              void setConfig({ destinations });
            }}
          >
            <option value="cv-cloud">Cloud</option>
            <option value="appliance">Appliance</option>
            <option value="both">Cloud + Appliance</option>
          </select>
        </div>
        <div className="collector-row" style={{ marginTop: 10 }}>
          <div className="row" style={{ gap: 10 }}>
            <Icon name="search" size={16} />
            <div>
              <div style={{ fontWeight: 600, fontSize: 13 }}>Verbose logging (DEBUG)</div>
              <div className="faint" style={{ fontSize: 11.5 }}>Applies on the next heartbeat (~30s). For troubleshooting.</div>
            </div>
          </div>
          <button
            className={`btn sm ${verbose ? "primary" : ""}`}
            onClick={() => { const next = !verbose; setVerbose(next); void setConfig({ verbose_logging: next }); }}
          >
            {verbose ? "On" : "Off"}
          </button>
        </div>
      </Card>

      {/* Recent activity */}
      {logs.length > 0 && (
        <Card style={{ marginBottom: 16 }}>
          <h3 style={{ marginBottom: 10 }}>Recent activity</h3>
          <pre className="mono" style={{ fontSize: 11, maxHeight: 220, overflow: "auto",
               background: "rgba(0,0,0,0.28)", padding: 12, borderRadius: 10, margin: 0 }}>
            {logs.join("\n")}
          </pre>
        </Card>
      )}

      {kv && <KVModal title={kv.title} rows={kv.rows} onClose={() => setKv(null)} />}
    </>
  );
}

function KVModal({ title, rows, onClose }:
  { title: string; rows: [string, string][]; onClose: () => void }) {
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-panel" style={{ maxWidth: 560 }} onClick={(e) => e.stopPropagation()}>
        <div className="spread">
          <h3 style={{ margin: 0 }}>{title}</h3>
          <button className="btn ghost sm" onClick={onClose}><Icon name="logout" size={14} /></button>
        </div>
        <div className="modal-body">
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12.5 }}>
            <tbody>
              {rows.map(([k, v], i) => (
                <tr key={i} style={{ borderBottom: "1px solid var(--border-soft)" }}>
                  <td className="faint" style={{ padding: "7px 12px 7px 0", whiteSpace: "nowrap", verticalAlign: "top" }}>{k}</td>
                  <td className="mono" style={{ padding: "7px 0", overflowWrap: "anywhere" }}>{v}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="modal-foot">
          <div style={{ flex: 1 }} />
          <button className="btn ghost sm" onClick={onClose}>Close</button>
        </div>
      </div>
    </div>
  );
}
