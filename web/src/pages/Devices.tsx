import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { useAuth } from "../auth";
import { Card, Pill, timeAgo, serverDate, fmtAbsolute, Loading } from "../components/ui";
import { Icon } from "../components/Icon";
import { BrandIcon } from "../components/BrandIcon";
import { SourceIcon } from "../components/SourceIcon";
import { notify, confirmDialog, promptDialog } from "../components/dialog";
import { VersionPill, ProductionVersion } from "../components/VersionBadge";

interface Agent {
  id: string; name: string; hostname: string; platform: string; version: string;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  state: string; collectors: string[]; enabled_collectors?: string[]; config: any; telemetry: any;
  node_name?: string | null; node_url?: string | null;
  production_version?: string | null; update_available?: boolean; version_updated_at?: string | null;
  last_heartbeat_at: string | null; last_collection_at: string | null;
}

// Per-collector display metadata for the collector toggles.
const COLLECTOR_META: Record<string, { label: string; desc: string; brand?: string }> = {
  onepassword: { label: "1Password", desc: "Passwords & secure items via the op CLI", brand: "onepassword" },
  endpoint_files: { label: "Endpoint files", desc: "Folders you select in the Data Map" },
  imessage: { label: "Apple Messages", desc: "iMessage/SMS, group threads & attachments", brand: "imessage" },
  outlook_local: { label: "Outlook (local)", desc: "Local email, attachments, contacts, calendar & notes", brand: "outlook" },
};

// Device platforms we present as one "Devices" family — desktop agents + mobile apps.
type DeviceKind = "agent" | "app";
interface DeviceTypeMeta { label: string; icon: string; kind: DeviceKind; available: boolean; note: string; }
const DEVICE_TYPES: Record<string, DeviceTypeMeta> = {
  macos: { label: "Mac", icon: "macos", kind: "agent", available: true, note: "macOS desktop agent" },
  windows: { label: "Windows", icon: "windows", kind: "agent", available: false, note: "Windows desktop agent" },
  ios: { label: "iPhone / iPad", icon: "ios", kind: "app", available: false, note: "iOS app" },
  android: { label: "Android", icon: "android", kind: "app", available: false, note: "Android app" },
};
function deviceTypeKey(platform?: string): string {
  const p = (platform || "macos").toLowerCase();
  if (p.includes("win")) return "windows";
  if (p.includes("android")) return "android";
  if (p.includes("ios") || p.includes("ipad") || p.includes("iphone")) return "ios";
  return "macos";
}

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

function DeviceGlyph({ typeKey, size = 20 }: { typeKey: string; size?: number }) {
  const meta = DEVICE_TYPES[typeKey] || DEVICE_TYPES.macos;
  return (
    <div className="result-icon" style={{ width: size + 16, height: size + 16, background: "var(--inset)" }}>
      <SourceIcon type={meta.icon} fallback="user" size={size} />
    </div>
  );
}

export default function Devices() {
  const { me, stepUp } = useAuth();
  const [agents, setAgents] = useState<Agent[]>([]);
  const [toast, setToast] = useState("");
  const [selected, setSelected] = useState<Agent | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [prodVersion, setProdVersion] = useState<string>("");
  const [addOpen, setAddOpen] = useState(false);

  async function load() {
    try {
      const list = await api.get<Agent[]>("/agents");
      setAgents(list);
      setSelected((d) => (d ? list.find((a) => a.id === d.id) ?? null : null));
    } finally {
      setLoaded(true);
    }
  }
  useEffect(() => {
    void load();
    api.get<{ production_version: string }>("/agents/versions").then((r) => setProdVersion(r.production_version)).catch(() => {});
    const t = setInterval(load, 8000);
    return () => clearInterval(t);
  }, []);

  function flash(msg: string) { setToast(msg); setTimeout(() => setToast(""), 3000); }

  async function command(a: Agent, type: string) {
    await api.post(`/agents/${a.id}/command`, { type, params: {} });
    flash(`${type} queued for ${a.name}`);
  }
  async function rename(a: Agent) {
    const name = await promptDialog({ title: "Rename device", label: "Device name",
      defaultValue: a.name, confirmLabel: "Save" });
    if (name == null) return;
    const trimmed = name.trim();
    if (!trimmed || trimmed === a.name) return;
    try { await api.put(`/agents/${a.id}`, { name: trimmed }); flash("Device renamed"); await load(); }
    catch (e) { await notify({ title: "Couldn't rename", message: (e as ApiError).message, tone: "danger" }); }
  }
  async function remove(a: Agent) {
    const ok = await confirmDialog({
      title: `Remove ${a.name}?`,
      message: "The device stops collecting and is removed from this list. Data it already backed up is kept — remove it from Sources to purge. This can't be undone.",
      confirmLabel: "Remove device", tone: "danger",
    });
    if (!ok) return;
    try { await api.del(`/agents/${a.id}`); setSelected(null); flash("Device removed"); await load(); }
    catch (e) { await notify({ title: "Couldn't remove", message: (e as ApiError).message, tone: "danger" }); }
  }

  if (!loaded && agents.length === 0) return <Loading label="Loading devices…" />;

  if (selected) {
    return (
      <DeviceDetail a={selected} onBack={() => setSelected(null)}
                    onCommand={(type) => command(selected, type)}
                    onRename={() => rename(selected)} onRemove={() => remove(selected)}
                    reload={load} />
    );
  }

  return (
    <>
      <div className="spread" style={{ marginBottom: 18, alignItems: "center", gap: 12, flexWrap: "wrap" }}>
        <div className="stack">
          <h2 style={{ margin: 0 }}>Devices</h2>
          <div className="faint" style={{ fontSize: 12.5, maxWidth: 620 }}>
            Your Macs, PCs and mobile apps — each collects locally with native tools and pushes
            client-side–encrypted data to the platform. The cloud never sees plaintext.
          </div>
        </div>
        <div className="row" style={{ gap: 12, alignItems: "center" }}>
          <ProductionVersion label="Current agent software" version={prodVersion} />
          <button className="btn primary" onClick={() => setAddOpen(true)}>
            <Icon name="plus" size={15} /> Add device
          </button>
        </div>
      </div>

      {agents.length === 0 ? (
        <Card>
          <div className="stack" style={{ alignItems: "center", gap: 10, padding: "26px 0", textAlign: "center" }}>
            <div className="row" style={{ gap: 10 }}>
              {Object.keys(DEVICE_TYPES).map((k) => <DeviceGlyph key={k} typeKey={k} size={18} />)}
            </div>
            <div style={{ fontWeight: 600 }}>No devices yet</div>
            <div className="faint" style={{ fontSize: 12.5, maxWidth: 420 }}>
              Add a Mac to start collecting locally. Windows, iOS and Android are coming soon.
            </div>
            <button className="btn primary sm" onClick={() => setAddOpen(true)}><Icon name="plus" size={14} /> Add device</button>
          </div>
        </Card>
      ) : (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(300px, 1fr))", gap: 14 }}>
          {agents.map((a) => {
            const tk = deviceTypeKey(a.platform);
            const meta = DEVICE_TYPES[tk] || DEVICE_TYPES.macos;
            return (
              <div key={a.id} className="dest-card" style={{ cursor: "pointer" }} onClick={() => setSelected(a)}>
                <div className="spread">
                  <div className="row" style={{ gap: 10, minWidth: 0 }}>
                    <DeviceGlyph typeKey={tk} size={18} />
                    <div style={{ minWidth: 0 }}>
                      <div style={{ fontWeight: 650, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{a.name}</div>
                      <div className="faint mono" style={{ fontSize: 11 }}>{a.hostname || meta.label} · v{a.version}</div>
                    </div>
                  </div>
                  <StatusDot color={isOnline(a) ? "#35d0a5" : "#6b7688"} pulse={isOnline(a)} />
                </div>
                <div className="row" style={{ gap: 12, marginTop: 10, paddingTop: 10, borderTop: "1px solid var(--border-soft)", flexWrap: "wrap" }}>
                  <Pill tone="info">{meta.label}</Pill>
                  <OnlinePill a={a} />
                  <HealthPill a={a} />
                  <VersionPill version={a.version} updateAvailable={a.update_available} />
                </div>
              </div>
            );
          })}
        </div>
      )}

      {addOpen && (
        <AddDeviceModal me={me} stepUp={stepUp} onClose={() => setAddOpen(false)} flash={flash} reload={load} />
      )}
      {toast && <div className="toast"><Icon name="check" size={15} /> {toast}</div>}
    </>
  );
}

function AddDeviceModal({ me, stepUp, onClose, flash, reload }:
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  { me: any; stepUp: () => Promise<void>; onClose: () => void; flash: (m: string) => void; reload: () => Promise<void> }) {
  const [code, setCode] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function downloadMac() {
    if (!me?.passkey_verified) {
      try { await stepUp(); } catch (e) { return notify({ message: (e as Error).message, tone: "danger" }); }
    }
    setBusy(true);
    try {
      const res = await api.post<{ filename: string; script: string }>("/agents/installer", {
        name: "Mac", collectors: ["onepassword"], destinations: ["cv-cloud"],
      });
      const blob = new Blob([res.script], { type: "text/x-shellscript" });
      const url = URL.createObjectURL(blob);
      const el = document.createElement("a");
      el.href = url; el.download = res.filename; el.click();
      URL.revokeObjectURL(url);
      flash("Installer downloaded — run it on your Mac to link this device");
      await reload();
    } catch (e) { await notify({ title: "Couldn't build installer", message: (e as ApiError).message, tone: "danger" }); }
    finally { setBusy(false); }
  }
  async function linkingCode() {
    try {
      const res = await api.post<{ code: string }>("/agents/linking-code", { name: "Mac", collectors: ["onepassword"] });
      setCode(res.code);
    } catch (e) { await notify({ title: "Couldn't create code", message: (e as ApiError).message, tone: "danger" }); }
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-panel" style={{ maxWidth: 640 }} onClick={(e) => e.stopPropagation()}>
        <div className="modal-head spread">
          <h3 style={{ margin: 0 }}><Icon name="plus" size={16} /> Add a device</h3>
          <button className="btn ghost sm" onClick={onClose}><Icon name="x" size={15} /></button>
        </div>
        <div className="modal-body">
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(260px, 1fr))", gap: 12 }}>
            {/* Mac — available */}
            <div style={{ border: "1px solid var(--border-soft)", borderRadius: 12, padding: 14 }}>
              <div className="row" style={{ gap: 10, marginBottom: 8 }}>
                <DeviceGlyph typeKey="macos" size={20} />
                <div>
                  <div style={{ fontWeight: 650 }}>Mac</div>
                  <div className="faint" style={{ fontSize: 11.5 }}>macOS desktop agent</div>
                </div>
              </div>
              <div className="faint" style={{ fontSize: 12, marginBottom: 10 }}>
                Installs a background agent bundled with the 1Password CLI. Run the installer on your Mac — the linking code is baked in.
              </div>
              <div className="row" style={{ gap: 8 }}>
                <button className="btn primary sm" disabled={busy} onClick={downloadMac}>
                  <Icon name="logout" size={14} /> Download installer
                </button>
                <button className="btn sm" onClick={linkingCode}><Icon name="link" size={14} /> Linking code</button>
              </div>
              {code && (
                <div className="card" style={{ marginTop: 12, textAlign: "center", background: "var(--bg-elev)" }}>
                  <div className="faint" style={{ fontSize: 11.5 }}>Linking code (valid 15 min)</div>
                  <div className="mono" style={{ fontSize: 24, letterSpacing: 2, margin: "6px 0" }}>{code}</div>
                </div>
              )}
            </div>

            {/* Coming-soon device types */}
            {(["windows", "ios", "android"] as const).map((k) => {
              const meta = DEVICE_TYPES[k];
              return (
                <div key={k} style={{ border: "1px solid var(--border-soft)", borderRadius: 12, padding: 14, opacity: 0.72 }}>
                  <div className="row" style={{ gap: 10, marginBottom: 8 }}>
                    <DeviceGlyph typeKey={k} size={20} />
                    <div>
                      <div style={{ fontWeight: 650 }}>{meta.label}</div>
                      <div className="faint" style={{ fontSize: 11.5 }}>{meta.note}</div>
                    </div>
                  </div>
                  <div className="faint" style={{ fontSize: 12, marginBottom: 10 }}>
                    {meta.kind === "app"
                      ? "A mobile app to back up photos, messages and files."
                      : "A background agent for Windows endpoints."}
                  </div>
                  <Pill tone="warn">Coming soon</Pill>
                </div>
              );
            })}
          </div>
        </div>
        <div className="modal-foot">
          <div style={{ flex: 1 }} />
          <button className="btn ghost sm" onClick={onClose}>Close</button>
        </div>
      </div>
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

function DeviceDetail({ a, onBack, onCommand, onRename, onRemove, reload }:
  { a: Agent; onBack: () => void; onCommand: (type: string) => void;
    onRename: () => void; onRemove: () => void; reload: () => Promise<void> }) {
  const t = a.telemetry || {};
  const crypto = t.crypto || {};
  const online = isOnline(a);
  const tk = deviceTypeKey(a.platform);
  const meta = DEVICE_TYPES[tk] || DEVICE_TYPES.macos;
  const [kv, setKv] = useState<{ title: string; rows: [string, string][] } | null>(null);
  const [verbose, setVerbose] = useState<boolean>(!!a.config?.verbose_logging);
  const [showTray, setShowTray] = useState<boolean>(a.config?.show_tray_icon !== false);
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
      ["Name", a.name], ["Hostname", t.hostname || a.hostname], ["Device ID", a.id],
      ["Platform / OS", t.os || a.platform || "—"], ["Agent version", a.version], ["State", a.state],
      ["Local user", t.local_user || "—"], ["Local IP", t.local_ip || "—"], ["Public IP", t.public_ip || "—"],
      ["Cloud endpoint", t.cloud_url || "—"], ["Channel", t.channel_encryption || "TLS 1.3"],
      ["Collectors", (a.collectors || []).join(", ") || "—"],
      ["Schedule", `${a.config?.schedule_minutes ?? 360} min`],
      ["Content encryption", crypto.content_alg || "AES-256-GCM"],
      ["Quantum-safe", crypto.pq_available ? "enabled (ML-KEM/ML-DSA)" : "classical fallback"],
      ["Recovery escrow", crypto.recovery_escrow === "escrowed" ? `escrowed · ${crypto.recovery_kem_alg || "KEM"}` : "pending"],
      ["1Password auth", t.op_auth || "—"],
    ];
    setKv({ title: `${a.name} — details`, rows });
  }

  async function setConfig(patch: Record<string, unknown>) {
    try { await api.put(`/agents/${a.id}/config`, patch); await reload(); }
    catch (e) { await notify({ title: "Couldn't update device", message: (e as ApiError).message, tone: "danger" }); }
  }
  function toggleCollector(name: string) {
    const s = new Set(a.enabled_collectors || []);
    if (s.has(name)) s.delete(name); else s.add(name);
    void setConfig({ enabled_collectors: [...s] });
  }

  return (
    <>
      <button className="btn ghost sm" onClick={onBack} style={{ marginBottom: 12 }}>← Devices</button>

      <Card style={{ marginBottom: 16 }}>
        <div className="spread" style={{ alignItems: "flex-start", gap: 12, flexWrap: "wrap" }}>
          <div className="row" style={{ gap: 14, alignItems: "center" }}>
            <div className="result-icon" style={{ width: 50, height: 50, background: "var(--inset)" }}>
              <SourceIcon type={meta.icon} fallback="user" size={26} />
            </div>
            <div>
              <h2 style={{ margin: 0 }}>{a.name}</h2>
              <div className="faint mono" style={{ fontSize: 11.5 }}>{a.hostname} · v{a.version} · {t.os || a.platform}</div>
            </div>
          </div>
          <div className="stack" style={{ alignItems: "flex-end", gap: 8 }}>
            <div className="row" style={{ gap: 16 }}><OnlinePill a={a} /><HealthPill a={a} /></div>
            <div className="row" style={{ gap: 6 }}>
              <Pill tone="info">{meta.label}</Pill>
              <Pill tone="info"><Icon name="server" size={11} /> {a.node_name || "Control plane"}</Pill>
            </div>
            <VersionPill version={a.version} updateAvailable={a.update_available} />
          </div>
        </div>

        {/* Actions */}
        <div className="row" style={{ gap: 8, marginTop: 14, paddingTop: 12, borderTop: "1px solid var(--border-soft)", flexWrap: "wrap", alignItems: "center" }}>
          <button className="btn sm primary" onClick={() => onCommand("collect")}><Icon name="restore" size={13} /> Collect now</button>
          {a.update_available && <button className="btn sm" onClick={() => onCommand("update")}><Icon name="server" size={13} /> Update</button>}
          <button className="btn sm" onClick={onRename}><Icon name="edit" size={13} /> Rename</button>
          <button className="btn sm ghost" onClick={showAdvanced}><Icon name="search" size={13} /> Advanced</button>
          <div style={{ flex: 1 }} />
          <button className="btn sm ghost" style={{ color: "var(--danger-c,#f2545b)" }} onClick={onRemove}>
            <Icon name="trash" size={13} /> Remove
          </button>
        </div>

        <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 12, marginTop: 14 }}>
          <Info label="Heartbeat" value={timeAgo(a.last_heartbeat_at)} title={fmtAbsolute(a.last_heartbeat_at)} />
          <Info label="Last collection" value={timeAgo(a.last_collection_at)} title={fmtAbsolute(a.last_collection_at)} />
          <Info label="State" value={a.state} tone={online ? "ok" : "warn"} />
          <Info label="Data routes to" value={a.node_name || "Control plane"} />
        </div>
      </Card>

      {/* Collectors */}
      <Card style={{ marginBottom: 16 }}>
        <div className="spread" style={{ marginBottom: 12 }}>
          <h3 style={{ margin: 0 }}>Collectors</h3>
          <span className="faint" style={{ fontSize: 11.5 }}>What this device may collect. Off collectors can't be added as a source.</span>
        </div>
        {(a.collectors || []).length === 0 && (
          <div className="faint" style={{ fontSize: 12.5 }}>
            No collectors detected yet — the device reports what it can collect on its next heartbeat.
          </div>
        )}
        {(a.collectors || []).map((name) => {
          const cm = COLLECTOR_META[name] || { label: name, desc: "" };
          const on = (a.enabled_collectors || []).includes(name);
          const isOp = name === "onepassword";
          const desc = isOp
            ? (opState === "missing" ? "1Password CLI not installed"
              : opState === "interactive" ? "installed · collects interactively"
              : `installed · ${t.op_auth || "ready"}`)
            : cm.desc;
          return (
            <div key={name} className="collector-row" style={{ marginTop: 10 }}>
              <div className="row" style={{ gap: 10 }}>
                {cm.brand ? <BrandIcon name={cm.brand} size={18} /> : <Icon name="database" size={16} />}
                <div>
                  <div style={{ fontWeight: 600, fontSize: 13 }}>{cm.label}</div>
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
      </Card>

      {/* Settings */}
      <Card style={{ marginBottom: 16 }}>
        <h3 style={{ marginBottom: 12 }}>Settings</h3>
        <div className="collector-row">
          <div className="row" style={{ gap: 10 }}>
            <Icon name="server" size={16} />
            <div>
              <div style={{ fontWeight: 600, fontSize: 13 }}>Backup destination</div>
              <div className="faint" style={{ fontSize: 11.5 }}>Where this device pushes collected data.</div>
            </div>
          </div>
          <select value={dest} style={{ padding: "5px 8px", borderRadius: 6 }}
                  onChange={(e) => {
                    const v = e.target.value; setDest(v);
                    const destinations = v === "appliance" ? ["appliance"]
                      : v === "both" ? ["cv-cloud", "appliance"] : ["cv-cloud"];
                    void setConfig({ destinations });
                  }}>
            <option value="cv-cloud">Cloud</option>
            <option value="appliance">Appliance</option>
            <option value="both">Cloud + Appliance</option>
          </select>
        </div>
        <div className="collector-row" style={{ marginTop: 10 }}>
          <div className="row" style={{ gap: 10 }}>
            <Icon name="server" size={16} />
            <div>
              <div style={{ fontWeight: 600, fontSize: 13 }}>Show menu bar icon</div>
              <div className="faint" style={{ fontSize: 11.5 }}>Run quietly in the background. Applies on the next heartbeat (it restarts to apply).</div>
            </div>
          </div>
          <button className={`btn sm ${showTray ? "primary" : ""}`}
                  onClick={() => { const next = !showTray; setShowTray(next); void setConfig({ show_tray_icon: next }); }}>
            {showTray ? "Shown" : "Hidden"}
          </button>
        </div>
        <div className="collector-row" style={{ marginTop: 10 }}>
          <div className="row" style={{ gap: 10 }}>
            <Icon name="search" size={16} />
            <div>
              <div style={{ fontWeight: 600, fontSize: 13 }}>Verbose logging (DEBUG)</div>
              <div className="faint" style={{ fontSize: 11.5 }}>For troubleshooting. Applies on the next heartbeat (~30s).</div>
            </div>
          </div>
          <button className={`btn sm ${verbose ? "primary" : ""}`}
                  onClick={() => { const next = !verbose; setVerbose(next); void setConfig({ verbose_logging: next }); }}>
            {verbose ? "On" : "Off"}
          </button>
        </div>
      </Card>

      {/* Security posture — compact */}
      <Card style={{ marginBottom: 16 }}>
        <h3 style={{ marginBottom: 12 }}>Security</h3>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 12 }}>
          <Info label="Client encryption" value={crypto.content_alg || "AES-256-GCM"} tone="ok" />
          <Info label="Quantum-safe" value={crypto.pq_available ? "Enabled" : "Classical"} tone={crypto.pq_available ? "ok" : "warn"} />
          <Info label="Recovery escrow" value={crypto.recovery_escrow === "escrowed" ? "Escrowed" : "Pending"} tone={crypto.recovery_escrow === "escrowed" ? "ok" : "warn"} />
        </div>
        <div className="faint" style={{ fontSize: 11.5, marginTop: 10 }}>
          Data is encrypted on this device before upload — the cloud never sees plaintext.
        </div>
      </Card>

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
        <div className="modal-head spread">
          <h3 style={{ margin: 0 }}>{title}</h3>
          <button className="btn ghost sm" onClick={onClose}><Icon name="x" size={15} /></button>
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
