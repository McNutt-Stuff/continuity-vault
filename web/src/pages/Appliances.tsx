import { useEffect, useState } from "react";
import { api } from "../api";
import { Card, Pill, Stat, bytes, timeAgo } from "../components/ui";
import { Icon } from "../components/Icon";
import { ApplianceStatePill } from "./Dashboard";

interface Appliance {
  id: string; serial: string; model: string; name: string; location_label: string;
  state: string; isolation_state: string; software_version: string;
  attestation_ok: boolean; tamper_state: string;
  last_heartbeat_at: string | null; last_attestation_at: string | null;
  telemetry: any;
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
    if (selected) setSelected(list.find((a) => a.id === selected.id) ?? null);
  }
  useEffect(() => {
    void load();
    const t = setInterval(load, 8000);
    return () => clearInterval(t);
  }, []);

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
            onClick={() => setSelected(a)}
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
          <ApplianceDetail a={selected} onCommand={command} />
        ) : (
          <Card><div className="muted">Select an appliance to view its dashboard.</div></Card>
        )}
      </div>

      {toast && <div className="toast"><Icon name="check" size={15} /> {toast}</div>}
    </div>
  );
}

function ApplianceDetail({ a, onCommand }: { a: Appliance; onCommand: (a: Appliance, t: string, p?: any) => void }) {
  const t = a.telemetry ?? {};
  const usedPct = t.capacity_total_bytes ? (t.capacity_used_bytes / t.capacity_total_bytes) * 100 : 0;
  return (
    <>
      <Card style={{ marginBottom: 16 }}>
        <div className="spread" style={{ marginBottom: 14 }}>
          <div>
            <h2>{a.name}</h2>
            <div className="faint" style={{ fontSize: 12 }}>{a.model} · v{a.software_version}</div>
          </div>
          <ApplianceStatePill state={a.state} isolation={a.isolation_state} ok={a.attestation_ok} />
        </div>
        <div className="grid grid-2">
          <Info label="Isolation" value={a.isolation_state === "sealed" ? "Sealed (offline)" : "Open"} />
          <Info label="Attestation" value={a.attestation_ok ? "Verified" : "Failed"} tone={a.attestation_ok ? "ok" : "danger"} />
          <Info label="Tamper" value={a.tamper_state} tone={a.tamper_state === "normal" ? "ok" : "danger"} />
          <Info label="Heartbeat" value={timeAgo(a.last_heartbeat_at)} />
        </div>
      </Card>

      <Card style={{ marginBottom: 16 }}>
        <h3 style={{ marginBottom: 12 }}>Capacity & health</h3>
        <div className="spread" style={{ marginBottom: 6, fontSize: 13 }}>
          <span className="muted">{bytes(t.capacity_used_bytes ?? 0)} of {bytes(t.capacity_total_bytes ?? 0)}</span>
          <span className="muted">{t.snapshots ?? 0} snapshots</span>
        </div>
        <div className="progress"><span style={{ width: `${usedPct}%` }} /></div>
        <div className="grid grid-3" style={{ marginTop: 14 }}>
          <Info label="Drives" value={t.drive_health ?? "—"} tone="ok" />
          <Info label="Power" value={t.power ?? "—"} tone="ok" />
          <Info label="Temp" value={`${t.temperature_c ?? "—"}°C`} />
        </div>
      </Card>

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
      </Card>
    </>
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
