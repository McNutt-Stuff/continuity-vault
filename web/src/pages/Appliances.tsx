import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { Card, Pill, Stat, bytes, timeAgo } from "../components/ui";
import { Icon } from "../components/Icon";
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
interface StoredData {
  recovery_points: number; objects: number; bytes: number; items: StoredItem[];
}
interface Appliance {
  id: string; serial: string; model: string; name: string; location_label: string;
  state: string; isolation_state: string; software_version: string;
  attestation_ok: boolean; tamper_state: string;
  last_heartbeat_at: string | null; last_attestation_at: string | null;
  telemetry: any; stores?: Store[]; stored_data?: StoredData;
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
      <div>
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
            <div className="card" style={{ marginTop: 14, textAlign: "center", background: "#0e1421" }}>
              <div className="faint" style={{ fontSize: 12 }}>Linking code (valid 15 min)</div>
              <div className="mono" style={{ fontSize: 26, letterSpacing: 2, margin: "8px 0" }}>{code}</div>
            </div>
          )}
          {installCmd && (
            <div className="card" style={{ marginTop: 14, background: "#0e1421" }}>
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
                  <div className="faint mono" style={{ fontSize: 11 }}>{a.serial}</div>
                </div>
              </div>
              <ApplianceStatePill state={a.state} isolation={a.isolation_state} ok={a.attestation_ok} />
            </div>
          </div>
        ))}
        {apps.length === 0 && <Card><div className="muted">No appliances linked yet.</div></Card>}
      </div>

      <div>
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

  return (
    <>
      <Card style={{ marginBottom: 16 }}>
        <div className="spread" style={{ marginBottom: 14 }}>
          <div>
            <div className="row" style={{ gap: 8, alignItems: "center" }}>
              <h2 style={{ margin: 0 }}>{a.name}</h2>
              <button className="btn ghost sm" title="Rename appliance" onClick={renameAppliance}>
                <Icon name="gear" size={13} />
              </button>
            </div>
            <div className="faint" style={{ fontSize: 12 }}>{a.model} · v{a.software_version}</div>
          </div>
          <div className="row" style={{ gap: 8 }}>
            {t.model_kind && (
              <Pill tone={t.model_kind === "hardware" ? "ok" : "info"}>
                {t.model_kind === "hardware" ? "Hardware" : "Virtual"}
              </Pill>
            )}
            <ApplianceStatePill state={a.state} isolation={a.isolation_state} ok={a.attestation_ok} />
          </div>
        </div>
        <div className="grid grid-2">
          <Info label="Isolation" value={a.isolation_state === "sealed" ? "Sealed (offline)" : "Open"} />
          <Info label="Attestation" value={a.attestation_ok ? "Verified" : "Failed"} tone={a.attestation_ok ? "ok" : "danger"} />
          <Info label="Tamper" value={a.tamper_state} tone={a.tamper_state === "normal" ? "ok" : "danger"} />
          <Info label="Heartbeat" value={timeAgo(a.last_heartbeat_at)} />
        </div>
      </Card>

      <Card style={{ marginBottom: 16 }}>
        <div className="spread" style={{ marginBottom: 12 }}>
          <h3 style={{ margin: 0 }}>Storage</h3>
          <div className="row" style={{ gap: 10 }}>
            <span className="faint" style={{ fontSize: 12 }}>{t.snapshots ?? 0} snapshots · {t.objects ?? 0} objects</span>
            <button className="btn sm" onClick={addStorage}><Icon name="database" size={13} /> Add storage</button>
          </div>
        </div>
        <div className="muted" style={{ fontSize: 12.5, marginBottom: 12 }}>
          Mappings target a storage here (e.g. "{a.name} · Built-In Storage"), the same way they can
          target the Arkive cloud or your own S3 bucket. Each storage reports its own capacity & health.
        </div>
        {(a.stores ?? []).map((s) => <StorageItem key={s.id} a={a} s={s} onRename={renameStorage} onDelete={deleteStorage} />)}
        {(a.stores ?? []).length === 0 && <div className="muted">No storage objects yet.</div>}
      </Card>

      <Card style={{ marginBottom: 16 }}>
        <div className="spread" style={{ marginBottom: 10 }}>
          <h3 style={{ margin: 0 }}>Stored data</h3>
          {a.stored_data && (
            <span className="faint" style={{ fontSize: 12 }}>
              {a.stored_data.recovery_points} recovery points · {a.stored_data.objects} objects · {bytes(a.stored_data.bytes)}
            </span>
          )}
        </div>
        {t.data_path && (
          <div className="faint" style={{ fontSize: 12, marginBottom: 10 }}>
            Data location: <span className="mono">{t.data_path}</span>
            {t.data_mount ? <> on <span className="mono">{t.data_mount}</span></> : null} · client-encrypted (AES-256-GCM), sealed at rest
          </div>
        )}
        {(!a.stored_data || a.stored_data.items.length === 0) && (
          <div className="muted">No recovery points stored on this appliance yet.</div>
        )}
        {a.stored_data && a.stored_data.items.map((it) => (
          <div key={it.snapshot_id} className="result-row">
            <div className="result-icon" style={{ background: "linear-gradient(135deg,#4f7cff,#35d0a5)", width: 30, height: 30 }}>
              <Icon name="clock" size={14} />
            </div>
            <div className="flex1">
              <div style={{ fontWeight: 600 }}>{it.source}</div>
              <div className="faint" style={{ fontSize: 11.5 }}>
                {it.storage} · {it.object_count} objects · {bytes(it.total_bytes)} · {timeAgo(it.at)}
              </div>
              {it.path && <div className="faint mono" style={{ fontSize: 11, marginTop: 2 }}>{it.path}</div>}
            </div>
            <Pill tone={it.recoverable ? "ok" : "warn"}>{it.recoverable ? "recoverable" : "sealing"}</Pill>
          </div>
        ))}
      </Card>

      <Card style={{ marginBottom: 16 }}>
        <h3 style={{ marginBottom: 12 }}>System & platform</h3>
        <div className="grid grid-3">
          <Info label="Type" value={t.model_kind === "vm" ? `VM (${t.virtualization || "?"})` : "Hardware"} />
          <Info label="Product" value={t.hardware_product ?? "—"} />
          <Info label="Vendor" value={t.hardware_vendor ?? "—"} />
          <Info label="OS" value={t.os ?? "—"} />
          <Info label="Arch" value={t.arch ?? "—"} />
          <Info label="CPUs" value={String(t.cpu_count ?? "—")} />
          <Info label="Load" value={Array.isArray(t.load_avg) ? t.load_avg.join(" ") : "—"} />
          <Info label="Memory" value={t.mem_total_bytes ? `${bytes(t.mem_available_bytes ?? 0)} free / ${bytes(t.mem_total_bytes)}` : "—"} />
          <Info label="Uptime" value={t.uptime_seconds ? `${Math.floor(t.uptime_seconds / 3600)}h` : "—"} />
        </div>
      </Card>

      <Card style={{ marginBottom: 16 }}>
        <h3 style={{ marginBottom: 12 }}>Network & encryption</h3>
        <div className="grid grid-2">
          <Info label="Local IP" value={t.local_ip ?? "—"} />
          <Info label="Cloud" value={t.cloud_url ?? "—"} />
          <Info label="Client encryption" value={t.content_alg ?? "AES-256-GCM"} tone="ok" />
          <Info label="Quantum-safe" value={t.quantum_safe ? "Enabled" : "Classical"} tone={t.quantum_safe ? "ok" : "danger"} />
          <Info label="Signing" value={t.signing_alg ?? "—"} />
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

      <Card>
        <h3 style={{ marginBottom: 12 }}>Management commands</h3>
        <div className="muted" style={{ fontSize: 12.5, marginBottom: 12 }}>
          Every command is hybrid-signed (Ed25519 + ML-DSA), sequenced, and expiring. The
          appliance verifies and enforces local policy before acting.
        </div>
        <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
          <button className="btn sm" onClick={() => onCommand(a, "OPEN_INGEST_WINDOW", { maximumDurationSeconds: 1800 })}>
            Open ingest window
          </button>
          <button className="btn sm" onClick={() => onCommand(a, "REQUEST_VERIFICATION")}>
            Verify integrity
          </button>
          <button className="btn sm" onClick={() => onCommand(a, "SCHEDULE_BACKUP", { window: "02:00" })}>
            Schedule backup
          </button>
          <button className="btn sm danger" onClick={() => onCommand(a, "QUARANTINE")}>
            Quarantine
          </button>
        </div>
        <div className="spread" style={{ marginTop: 14, paddingTop: 12, borderTop: "1px solid var(--border-soft)" }}>
          <span className="faint" style={{ fontSize: 12 }}>Decommission or remove a stale / test unit from the fleet.</span>
          <button className="btn sm danger" onClick={() => onRemove(a)}>
            <Icon name="logout" size={13} /> Remove appliance
          </button>
        </div>
      </Card>
    </>
  );
}

function StorageItem({ a, s, onRename, onDelete }:
  { a: Appliance; s: Store; onRename: (s: Store) => void; onDelete: (s: Store) => void }) {
  const pct = s.capacity_bytes ? Math.min(100, (s.used_bytes / s.capacity_bytes) * 100) : 0;
  const h = s.health || {};
  const barTone = pct >= 90 ? "#f2545b" : pct >= 75 ? "#f5a623" : undefined;
  const chips: { label: string; value: string; tone: "ok" | "warn" | "danger" | "info" }[] = [];
  if (h.drive_health) chips.push({ label: "Drive", value: h.drive_health, tone: h.drive_health === "healthy" ? "ok" : "danger" });
  if (h.smart?.enabled) chips.push({ label: "SMART", value: h.smart.status ?? "—", tone: h.smart.status === "passed" ? "ok" : "danger" });
  if (h.raid?.enabled) chips.push({ label: "RAID", value: h.raid.status ?? "—", tone: h.raid.status === "optimal" ? "ok" : "danger" });
  if (h.temperature_c != null) chips.push({ label: "Temp", value: `${h.temperature_c}°C`, tone: h.temperature_c >= 60 ? "warn" : "info" });
  if (h.power) chips.push({ label: "Power", value: h.power, tone: h.power === "ok" ? "ok" : "danger" });

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
              {s.kind === "builtin" ? "Built-in storage" : "External storage"} · store:{s.id.slice(0, 8)}
              {s.path && <> · <span className="mono">{s.path}</span>{s.mount ? ` on ${s.mount}` : ""}</>}
            </div>
          </div>
        </div>
        <div className="row" style={{ gap: 6 }}>
          <Pill tone={s.kind === "builtin" ? "info" : "ok"}>{s.kind === "builtin" ? "Built-in" : "External"}</Pill>
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

function Info({ label, value, tone }: { label: string; value: string; tone?: "ok" | "danger" }) {
  return (
    <div className="stack">
      <div className="faint" style={{ fontSize: 11.5 }}>{label}</div>
      {tone ? <Pill tone={tone}>{value}</Pill> : <div style={{ fontWeight: 600 }}>{value}</div>}
    </div>
  );
}
