import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { Card, Pill, bytes, timeAgo, serverDate, fmtAbsolute } from "../components/ui";
import { Icon } from "../components/Icon";
import { BrandIcon, brandForSource } from "../components/BrandIcon";
import { confirmDialog, notify, promptDialog } from "../components/dialog";
import { ApplianceStatePill } from "./Dashboard";

interface StoreHealth {
  drive_health?: string; temperature_c?: number; power?: string;
  smart?: { enabled: boolean; status?: string };
  raid?: { enabled: boolean; status?: string };
}
interface Store {
  id: string; name: string; kind: string;
  capacity_bytes: number; used_bytes: number; free_bytes: number;
  path?: string | null; mount?: string | null; health: StoreHealth;
}
interface StoredItem {
  snapshot_id: string; source: string; storage: string; path?: string;
  object_count: number; total_bytes: number; recoverable: boolean; at: string;
}
interface SourceSummary {
  source: string; vault: string; source_type: string;
  recovery_points: number; objects: number; bytes: number;
  recoverable: number; storage: string; last_at: string;
}
interface StoredData {
  recovery_points: number; objects: number; bytes: number;
  sources?: SourceSummary[]; items: StoredItem[];
}
interface Command { type: string; status: string; sequence: number; created_at: string; }
interface Appliance {
  id: string; serial: string; model: string; name: string; location_label: string;
  state: string; isolation_state: string; software_version: string;
  attestation_ok: boolean; tamper_state: string;
  last_heartbeat_at: string | null; last_attestation_at: string | null;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  telemetry: any; stores?: Store[]; stored_data?: StoredData; recent_commands?: Command[];
}

// Online = a heartbeat within the last ~90s (3 missed 30s beats).
function isOnline(a: Appliance): boolean {
  if (!a.last_heartbeat_at) return false;
  return (Date.now() - serverDate(a.last_heartbeat_at).getTime()) / 1000 < 90;
}

type HealthLevel = "healthy" | "warning" | "critical";
function healthOf(a: Appliance): { level: HealthLevel; label: string } {
  if (!a.attestation_ok) return { level: "critical", label: "Attestation failed" };
  if (a.tamper_state && a.tamper_state !== "normal") return { level: "critical", label: "Tamper detected" };
  if (a.state === "QUARANTINED") return { level: "critical", label: "Quarantined" };
  if (!isOnline(a)) return { level: "warning", label: "Offline" };
  const stores = a.stores ?? [];
  for (const s of stores) {
    if (s.capacity_bytes > 0 && s.used_bytes / s.capacity_bytes >= 0.9)
      return { level: "warning", label: "Storage nearly full" };
    if (s.health?.drive_health && s.health.drive_health !== "healthy")
      return { level: "warning", label: "Drive health" };
  }
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

function OnlinePill({ a }: { a: Appliance }) {
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

function HealthPill({ a }: { a: Appliance }) {
  const h = healthOf(a);
  return (
    <span className="row" style={{ gap: 6, alignItems: "center" }}>
      <StatusDot color={HEALTH_COLOR[h.level]} />
      <span style={{ fontSize: 12, fontWeight: 600, color: HEALTH_COLOR[h.level] }}>{h.label}</span>
    </span>
  );
}

export default function Appliances() {
  const [apps, setApps] = useState<Appliance[]>([]);
  const [selected, setSelected] = useState<Appliance | null>(null);
  const [code, setCode] = useState<string | null>(null);
  const [installCmd, setInstallCmd] = useState<string | null>(null);
  const [toast, setToast] = useState("");

  async function load() {
    const list = await api.get<Appliance[]>("/appliances");
    setApps(list);
    // Refresh the open appliance's full detail (stores, capacity, stored data).
    if (selected) {
      try { setSelected(await api.get<Appliance>(`/appliances/${selected.id}`)); }
      catch { setSelected(list.find((a) => a.id === selected.id) ?? null); }
    }
  }
  useEffect(() => {
    void load();
    const t = setInterval(load, 8000);
    return () => clearInterval(t);
  }, [selected?.id]);

  async function select(a: Appliance) {
    setSelected(a);  // immediate
    try { setSelected(await api.get<Appliance>(`/appliances/${a.id}`)); } catch { /* keep list item */ }
  }

  async function newCode() {
    const res = await api.post<{ code: string }>("/appliances/linking-code", {
      model: "CV Edge 8",
      name: "Home Appliance",
    });
    setCode(res.code);
  }

  async function newInstaller() {
    const res = await api.post<{ code: string; command: string }>("/appliances/installer", {
      model: "CV Edge 8",
      name: "Home Appliance",
    });
    setCode(res.code);
    setInstallCmd(res.command);
  }

  async function command(a: Appliance, command_type: string, parameters: any = {}) {
    await api.post(`/appliances/${a.id}/command`, { command_type, parameters });
    setToast(`${command_type} issued (signed & sequenced)`);
    setTimeout(() => setToast(""), 2500);
    await load();
  }

  async function remove(a: Appliance) {
    const ok = await confirmDialog({
      title: "Remove appliance",
      message: `Remove "${a.name}" (${a.serial}) from the fleet? This deletes the fleet record and its pending commands. Existing recovery points are kept. If the unit is still running it will re-appear on its next activation.`,
      confirmLabel: "Remove appliance",
    });
    if (!ok) return;
    try {
      await api.del(`/appliances/${a.id}`);
      if (selected?.id === a.id) setSelected(null);
      setToast("Appliance removed");
      setTimeout(() => setToast(""), 2500);
      await load();
    } catch (e) {
      await notify({ title: "Couldn't remove appliance", message: (e as ApiError).message, tone: "danger" });
    }
  }

  return (
    <div className="grid grid-2" style={{ alignItems: "start" }}>
      <div style={{ minWidth: 0 }}>
        <Card style={{ marginBottom: 16 }}>
          <div className="spread" style={{ marginBottom: 8 }}>
            <h2>Turnkey activation</h2>
            <div className="row" style={{ gap: 8 }}>
              <button className="btn sm" onClick={newCode}>
                <Icon name="link" size={14} /> Linking code
              </button>
              <button className="btn primary sm" onClick={newInstaller}>
                <Icon name="server" size={14} /> Install command
              </button>
            </div>
          </div>
          <div className="muted" style={{ fontSize: 13 }}>
            On a clean Ubuntu host, paste the one-line install command below — it downloads,
            installs, registers, and enables headless self-updates from the cloud. Or generate
            just a linking code to enter on a pre-installed appliance console.
          </div>
          {code && (
            <div className="card" style={{ marginTop: 14, textAlign: "center", background: "var(--bg-elev)" }}>
              <div className="faint" style={{ fontSize: 12 }}>Linking code (valid 15 min)</div>
              <div className="mono" style={{ fontSize: 26, letterSpacing: 2, margin: "8px 0" }}>{code}</div>
            </div>
          )}
          {installCmd && (
            <div className="card" style={{ marginTop: 14, background: "var(--bg-elev)" }}>
              <div className="spread" style={{ marginBottom: 6 }}>
                <div className="faint" style={{ fontSize: 12 }}>One-line install (run as sudo on Ubuntu)</div>
                <button
                  className="btn sm"
                  onClick={() => {
                    void navigator.clipboard.writeText(installCmd);
                    setToast("Install command copied");
                    setTimeout(() => setToast(""), 2500);
                  }}
                >
                  <Icon name="link" size={13} /> Copy
                </button>
              </div>
              <pre className="mono" style={{ fontSize: 11, whiteSpace: "pre-wrap", wordBreak: "break-all", margin: 0 }}>
                {installCmd}
              </pre>
            </div>
          )}
        </Card>

        {apps.map((a) => (
          <div
            key={a.id}
            className={`dest-card ${selected?.id === a.id ? "selected" : ""}`}
            style={{ marginBottom: 12 }}
            onClick={() => select(a)}
          >
            <div className="spread">
              <div className="row">
                <div className="result-icon" style={{ background: "linear-gradient(135deg,#4f7cff,#35d0a5)", width: 36, height: 36 }}>
                  <Icon name="server" size={18} />
                </div>
                <div>
                  <div style={{ fontWeight: 650 }}>{a.name}</div>
                  <div className="faint mono" style={{ fontSize: 11 }}>{a.model} · {a.serial}</div>
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
        {apps.length === 0 && <Card><div className="muted">No appliances linked yet.</div></Card>}
      </div>

      <div style={{ minWidth: 0 }}>
        {selected ? (
          <ApplianceDetail a={selected} onCommand={command} onRemove={remove} reload={load} />
        ) : (
          <Card><div className="muted">Select an appliance to view its dashboard.</div></Card>
        )}
      </div>

      {toast && <div className="toast"><Icon name="check" size={15} /> {toast}</div>}
    </div>
  );
}

function ApplianceDetail({ a, onCommand, onRemove, reload }: { a: Appliance; onCommand: (a: Appliance, t: string, p?: any) => void; onRemove: (a: Appliance) => void; reload: () => Promise<void> }) {
  const t = a.telemetry ?? {};
  const [kv, setKv] = useState<{ title: string; rows: [string, string][] } | null>(null);

  async function renameAppliance() {
    const name = await promptDialog({ title: "Rename appliance", label: "Appliance name", defaultValue: a.name, confirmLabel: "Save" });
    if (name == null || !name.trim()) return;
    try { await api.put(`/appliances/${a.id}`, { name: name.trim() }); await reload(); }
    catch (e) { await notify({ title: "Couldn't rename", message: (e as ApiError).message, tone: "danger" }); }
  }
  async function addStorage() {
    const name = await promptDialog({ title: "Add storage", label: "Storage name", placeholder: "External Storage 1", confirmLabel: "Add" });
    if (name == null || !name.trim()) return;
    try { await api.post(`/appliances/${a.id}/storage`, { name: name.trim(), kind: "external" }); await reload(); }
    catch (e) { await notify({ title: "Couldn't add storage", message: (e as ApiError).message, tone: "danger" }); }
  }
  async function renameStorage(s: Store) {
    const name = await promptDialog({ title: "Rename storage", label: "Storage name", defaultValue: s.name, confirmLabel: "Save" });
    if (name == null || !name.trim()) return;
    try { await api.put(`/appliances/${a.id}/storage/${s.id}`, { name: name.trim() }); await reload(); }
    catch (e) { await notify({ title: "Couldn't rename storage", message: (e as ApiError).message, tone: "danger" }); }
  }
  async function deleteStorage(s: Store) {
    const ok = await confirmDialog({ title: "Remove storage", message: `Remove "${s.name}"? Mappings pointing at it will need re-routing.`, confirmLabel: "Remove" });
    if (!ok) return;
    try { await api.del(`/appliances/${a.id}/storage/${s.id}`); await reload(); }
    catch (e) { await notify({ title: "Couldn't remove storage", message: (e as ApiError).message, tone: "danger" }); }
  }

  function showAdvanced() {
    const rows: [string, string][] = [
      ["Name", a.name],
      ["Serial", a.serial],
      ["Model", a.model],
      ["Software version", a.software_version],
      ["State", a.state],
      ["Isolation", a.isolation_state],
      ["Attestation", a.attestation_ok ? "verified" : "failed"],
      ["Tamper", a.tamper_state],
      ["Type", t.model_kind === "vm" ? `VM (${t.virtualization || "?"})` : "Hardware"],
      ["Product", t.hardware_product || "—"],
      ["Vendor", t.hardware_vendor || "—"],
      ["OS", t.os || "—"],
      ["Kernel", t.kernel || "—"],
      ["Arch", t.arch || "—"],
      ["CPUs", String(t.cpu_count ?? "—")],
      ["Memory", t.mem_total_bytes ? `${bytes(t.mem_available_bytes ?? 0)} free / ${bytes(t.mem_total_bytes)}` : "—"],
      ["Uptime", t.uptime_seconds ? fmtUptime(t.uptime_seconds) : "—"],
      ["Local IP", t.local_ip || "—"],
      ["Public IP", t.public_ip || "—"],
      ["Cloud endpoint", t.cloud_url || "—"],
      ["Latency", t.cloud_latency_ms != null ? `${t.cloud_latency_ms} ms` : "—"],
      ["Channel", t.channel_encryption || "TLS 1.3"],
      ["Content encryption", t.content_alg || "AES-256-GCM"],
      ["Quantum-safe", t.quantum_safe ? "enabled (ML-KEM/ML-DSA)" : "classical fallback"],
      ["Signing", t.signing_alg || "—"],
      ["Data sent", t.net_bytes_sent != null ? bytes(t.net_bytes_sent) : "—"],
      ["Data received", t.net_bytes_recv != null ? bytes(t.net_bytes_recv) : "—"],
      ["Data path", t.data_path || "—"],
      ["Data mount", t.data_mount || "—"],
      ["Heartbeat", fmtAbsolute(a.last_heartbeat_at)],
      ["Last attestation", fmtAbsolute(a.last_attestation_at)],
      ["Appliance ID", a.id],
    ];
    setKv({ title: `${a.name} — details`, rows });
  }

  return (
    <>
      <Card style={{ marginBottom: 16 }}>
        <div className="spread" style={{ alignItems: "flex-start", gap: 12 }}>
          <div className="row" style={{ gap: 12, alignItems: "center" }}>
            <div className="result-icon" style={{ background: "linear-gradient(135deg,#4f7cff,#35d0a5)", width: 46, height: 46 }}>
              <Icon name="server" size={22} />
            </div>
            <div>
              <div className="row" style={{ gap: 8, alignItems: "center" }}>
                <h2 style={{ margin: 0 }}>{a.name}</h2>
                <button className="btn ghost sm" title="Rename appliance" onClick={renameAppliance}>
                  <Icon name="gear" size={13} />
                </button>
              </div>
              <div className="faint mono" style={{ fontSize: 11.5 }}>{a.model} · v{a.software_version} · {a.serial}</div>
            </div>
          </div>
          <div className="stack" style={{ alignItems: "flex-end", gap: 8 }}>
            <div className="row" style={{ gap: 16 }}>
              <OnlinePill a={a} />
              <HealthPill a={a} />
            </div>
            <div className="row" style={{ gap: 6 }}>
              {t.model_kind && (
                <Pill tone={t.model_kind === "hardware" ? "ok" : "info"}>
                  {t.model_kind === "hardware" ? "Hardware" : "Virtual"}
                </Pill>
              )}
              <ApplianceStatePill state={a.state} isolation={a.isolation_state} ok={a.attestation_ok} />
            </div>
          </div>
        </div>

        {/* Controls header bar — signed, sequenced, expiring commands. */}
        <div className="row" style={{ gap: 8, marginTop: 14, paddingTop: 12, borderTop: "1px solid var(--border-soft)", flexWrap: "wrap", alignItems: "center" }}>
          <button className="btn sm" onClick={() => onCommand(a, "REQUEST_VERIFICATION")}>
            <Icon name="shield" size={13} /> Verify integrity
          </button>
          {a.state === "QUARANTINED"
            ? <Pill tone="danger">Quarantined</Pill>
            : <button className="btn sm danger" onClick={() => onCommand(a, "QUARANTINE")}>
                <Icon name="lock" size={13} /> Quarantine
              </button>}
          <button className="btn sm ghost" onClick={showAdvanced}>
            <Icon name="search" size={13} /> Advanced
          </button>
          <div style={{ flex: 1 }} />
          <button className="btn sm ghost danger" onClick={() => onRemove(a)}>
            <Icon name="logout" size={13} /> Remove
          </button>
        </div>

        <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 12, marginTop: 14 }}>
          <Info label="Heartbeat" value={timeAgo(a.last_heartbeat_at)} title={fmtAbsolute(a.last_heartbeat_at)} />
          <Info label="Attestation" value={a.attestation_ok ? "Verified" : "Failed"} tone={a.attestation_ok ? "ok" : "danger"} />
          <Info label="Tamper" value={a.tamper_state} tone={a.tamper_state === "normal" ? "ok" : "danger"} />
          <Info label="Uptime" value={t.uptime_seconds ? fmtUptime(t.uptime_seconds) : "—"} />
        </div>
      </Card>

      <StorageCard a={a} onAdd={addStorage} onRename={renameStorage} onDelete={deleteStorage} onAdvanced={setKv} />

      <StoredDataCard a={a} />

      <NetworkCard a={a} />

      <Card style={{ marginBottom: 16 }}>
        <h3 style={{ marginBottom: 12 }}>System &amp; platform</h3>
        <div className="grid grid-3">
          <Info label="Type" value={t.model_kind === "vm" ? `VM (${t.virtualization || "?"})` : "Hardware"} />
          <Info label="Product" value={t.hardware_product ?? "—"} />
          <Info label="Vendor" value={t.hardware_vendor ?? "—"} />
          <Info label="OS" value={t.os ?? "—"} />
          <Info label="Arch" value={t.arch ?? "—"} />
          <Info label="CPUs" value={String(t.cpu_count ?? "—")} />
          <Info label="Load" value={Array.isArray(t.load_avg) ? t.load_avg.join(" ") : "—"} />
          <Info label="Memory" value={t.mem_total_bytes ? `${bytes(t.mem_available_bytes ?? 0)} free / ${bytes(t.mem_total_bytes)}` : "—"} />
          <Info label="Uptime" value={t.uptime_seconds ? fmtUptime(t.uptime_seconds) : "—"} />
        </div>
      </Card>

      {Array.isArray(t.recent_logs) && t.recent_logs.length > 0 && (
        <Card style={{ marginBottom: 16 }}>
          <h3 style={{ marginBottom: 10 }}>Recent activity</h3>
          <pre className="mono" style={{ fontSize: 11, maxHeight: 220, overflow: "auto",
               background: "rgba(0,0,0,0.28)", padding: 12, borderRadius: 10, margin: 0 }}>
            {t.recent_logs.join("\n")}
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

function fmtUptime(s: number): string {
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

function StorageCard({ a, onAdd, onRename, onDelete, onAdvanced }: {
  a: Appliance; onAdd: () => void; onRename: (s: Store) => void; onDelete: (s: Store) => void;
  onAdvanced: (m: { title: string; rows: [string, string][] }) => void;
}) {
  const stores = a.stores ?? [];
  const totalCap = stores.reduce((n, s) => n + (s.capacity_bytes || 0), 0);
  const totalUsed = stores.reduce((n, s) => n + (s.used_bytes || 0), 0);
  const pct = totalCap ? Math.min(100, (totalUsed / totalCap) * 100) : 0;
  const barTone = pct >= 90 ? "#f2545b" : pct >= 75 ? "#f5a623" : undefined;
  return (
    <Card style={{ marginBottom: 16 }}>
      <div className="spread" style={{ marginBottom: 12 }}>
        <h3 style={{ margin: 0 }}>Storage</h3>
        <button className="btn sm" onClick={onAdd}><Icon name="database" size={13} /> Add storage</button>
      </div>
      {totalCap > 0 && (
        <div style={{ marginBottom: 14 }}>
          <div className="spread faint" style={{ fontSize: 12, marginBottom: 6 }}>
            <span>{bytes(totalUsed)} used of {bytes(totalCap)} across {stores.length} volume{stores.length === 1 ? "" : "s"}</span>
            <span>{bytes(Math.max(totalCap - totalUsed, 0))} free · {Math.round(pct)}%</span>
          </div>
          <div className="progress"><span style={{ width: `${pct}%`, background: barTone }} /></div>
        </div>
      )}
      {stores.map((s) => <StorageItem key={s.id} s={s} onRename={onRename} onDelete={onDelete} onAdvanced={onAdvanced} />)}
      {stores.length === 0 && <div className="muted">No storage volumes reported yet.</div>}
    </Card>
  );
}

function StoredDataCard({ a }: { a: Appliance }) {
  const t = a.telemetry ?? {};
  const sd = a.stored_data;
  const sources = sd?.sources ?? [];
  return (
    <Card style={{ marginBottom: 16 }}>
      <div className="spread" style={{ marginBottom: 10 }}>
        <h3 style={{ margin: 0 }}>Stored data</h3>
        {sd && (
          <span className="faint" style={{ fontSize: 12 }}>
            {sd.recovery_points} recovery points · {sd.objects} objects · {bytes(sd.bytes)}
          </span>
        )}
      </div>
      {t.data_path && (
        <div className="faint" style={{ fontSize: 12, marginBottom: 12 }}>
          <Icon name="lock" size={12} /> Client-encrypted (AES-256-GCM), sealed at rest
        </div>
      )}
      {sources.length === 0 && <div className="muted">No recovery points stored on this appliance yet.</div>}
      {sources.map((s, i) => {
        const brand = brandForSource(s.source_type);
        return (
          <div key={i} className="result-row">
            <div className="result-icon" style={{ background: brand ? "var(--inset)" : "linear-gradient(135deg,#4f7cff,#35d0a5)", width: 34, height: 34 }}>
              {brand ? <BrandIcon name={brand} size={17} /> : <Icon name="database" size={16} />}
            </div>
            <div className="flex1">
              <div style={{ fontWeight: 600 }}>{s.source}</div>
              <div className="faint" style={{ fontSize: 11.5 }}>
                Vault: {s.vault} · {s.storage} · updated {timeAgo(s.last_at)}
              </div>
            </div>
            <div className="stack" style={{ alignItems: "flex-end", gap: 2 }}>
              <div style={{ fontWeight: 600, fontSize: 13 }}>{bytes(s.bytes)}</div>
              <div className="faint" style={{ fontSize: 11 }}>
                {s.objects} objects · {s.recovery_points} point{s.recovery_points === 1 ? "" : "s"}
              </div>
            </div>
          </div>
        );
      })}
    </Card>
  );
}

function NetworkCard({ a }: { a: Appliance }) {
  const t = a.telemetry ?? {};
  const online = isOnline(a);
  const lat: number | null = t.cloud_latency_ms ?? null;
  return (
    <Card style={{ marginBottom: 16 }}>
      <h3 style={{ marginBottom: 12 }}>Network</h3>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 12 }}>
        <Info label="Local IP" value={t.local_ip || "—"} />
        <Info label="Public IP" value={t.public_ip || "—"} />
        <div className="stack">
          <div className="faint" style={{ fontSize: 11.5 }}>Cloud connectivity</div>
          <div className="row" style={{ gap: 6, alignItems: "center" }}>
            <StatusDot color={online ? "#35d0a5" : "#6b7688"} pulse={online} />
            <span style={{ fontWeight: 600, color: online ? "#35d0a5" : undefined }}>
              {online ? "Connected" : "Disconnected"}
            </span>
          </div>
        </div>
        <Info label="Latency" value={lat != null ? `${lat} ms` : "—"} tone={lat != null && lat < 250 ? "ok" : undefined} />
        <Info label="Channel" value={t.channel_encryption || "TLS 1.3"} tone="ok" />
        <Info label="Quantum-safe" value={t.quantum_safe ? "Enabled" : "Classical"} tone={t.quantum_safe ? "ok" : "danger"} />
        <Info label="Data sent" value={t.net_bytes_sent != null ? bytes(t.net_bytes_sent) : "—"} />
        <Info label="Data received" value={t.net_bytes_recv != null ? bytes(t.net_bytes_recv) : "—"} />
        <Info label="Signing" value={t.signing_alg ?? "—"} />
      </div>
      <div className="faint" style={{ fontSize: 11.5, marginTop: 10 }}>
        Cloud endpoint: <span className="mono">{t.cloud_url ?? "—"}</span>
      </div>
    </Card>
  );
}

function StorageItem({ s, onRename, onDelete, onAdvanced }:
  { s: Store; onRename: (s: Store) => void; onDelete: (s: Store) => void;
    onAdvanced: (m: { title: string; rows: [string, string][] }) => void }) {
  const pct = s.capacity_bytes ? Math.min(100, (s.used_bytes / s.capacity_bytes) * 100) : 0;
  const h = s.health || {};
  const barTone = pct >= 90 ? "#f2545b" : pct >= 75 ? "#f5a623" : undefined;
  const chips: { label: string; value: string; tone: "ok" | "warn" | "danger" | "info" }[] = [];
  if (h.drive_health) chips.push({ label: "Drive", value: h.drive_health, tone: h.drive_health === "healthy" ? "ok" : "danger" });
  if (h.smart?.enabled) chips.push({ label: "SMART", value: h.smart.status ?? "—", tone: h.smart.status === "passed" ? "ok" : "danger" });
  if (h.raid?.enabled) chips.push({ label: "RAID", value: h.raid.status ?? "—", tone: h.raid.status === "optimal" ? "ok" : "danger" });
  if (h.temperature_c != null) chips.push({ label: "Temp", value: `${h.temperature_c}°C`, tone: h.temperature_c >= 60 ? "warn" : "info" });
  if (h.power) chips.push({ label: "Power", value: h.power, tone: h.power === "ok" ? "ok" : "danger" });

  function showAdvanced() {
    const rows: [string, string][] = [
      ["Name", s.name],
      ["Type", s.kind === "builtin" ? "Built-in storage" : "External storage"],
      ["Storage ID", `store:${s.id}`],
      ["Data path", s.path || "—"],
      ["Mount / device", s.mount || "—"],
      ["Capacity", s.capacity_bytes ? bytes(s.capacity_bytes) : "—"],
      ["Used", bytes(s.used_bytes)],
      ["Free", bytes(s.free_bytes)],
      ["Usage", s.capacity_bytes ? `${Math.round(pct)}%` : "—"],
      ["Drive health", h.drive_health || "—"],
      ["SMART", h.smart?.enabled ? (h.smart.status || "—") : "n/a"],
      ["RAID", h.raid?.enabled ? (h.raid.status || "—") : "n/a"],
      ["Temperature", h.temperature_c != null ? `${h.temperature_c}°C` : "—"],
      ["Power", h.power || "—"],
    ];
    onAdvanced({ title: `${s.name} — details`, rows });
  }

  return (
    <div className="store-item">
      <div className="spread">
        <div className="row" style={{ gap: 10 }}>
          <div className="result-icon" style={{ background: "linear-gradient(135deg,#4f7cff,#35d0a5)", width: 32, height: 32 }}>
            <Icon name="database" size={15} />
          </div>
          <div>
            <div style={{ fontWeight: 600 }}>{s.name}</div>
            <div className="faint" style={{ fontSize: 11.5 }}>
              {s.kind === "builtin" ? "Built-in storage" : "External storage"}
            </div>
          </div>
        </div>
        <div className="row" style={{ gap: 6 }}>
          <Pill tone={s.kind === "builtin" ? "info" : "ok"}>{s.kind === "builtin" ? "Built-in" : "External"}</Pill>
          <button className="btn sm ghost" onClick={showAdvanced} title="Advanced details">
            <Icon name="gear" size={13} />
          </button>
          <button className="btn sm ghost" onClick={() => onRename(s)}>Rename</button>
          {s.kind !== "builtin" && <button className="btn sm ghost" onClick={() => onDelete(s)}>Remove</button>}
        </div>
      </div>
      <div className="spread faint" style={{ fontSize: 12, margin: "10px 0 4px" }}>
        <span>{s.capacity_bytes ? `${bytes(s.used_bytes)} of ${bytes(s.capacity_bytes)}` : "Capacity not yet reported"}</span>
        {s.capacity_bytes > 0 && <span>{bytes(s.free_bytes)} free · {Math.round(pct)}%</span>}
      </div>
      <div className="progress"><span style={{ width: `${pct}%`, background: barTone }} /></div>
      {chips.length > 0 && (
        <div className="row" style={{ gap: 8, marginTop: 10, flexWrap: "wrap" }}>
          {chips.map((c) => (
            <span key={c.label} className="store-health">
              <span className="faint">{c.label}</span>
              <Pill tone={c.tone}>{c.value}</Pill>
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

function Info({ label, value, tone, title }: { label: string; value: string; tone?: "ok" | "danger"; title?: string }) {
  return (
    <div className="stack" title={title}>
      <div className="faint" style={{ fontSize: 11.5 }}>{label}</div>
      {tone ? <Pill tone={tone}>{value}</Pill> : <div style={{ fontWeight: 600 }}>{value}</div>}
    </div>
  );
}
