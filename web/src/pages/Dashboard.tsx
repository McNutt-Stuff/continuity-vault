import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { Card, Pill, Stat, bytes, timeAgo } from "../components/ui";
import { Icon } from "../components/Icon";

interface Snapshot { snapshot_id: string; destination: string; object_count: number; total_bytes: number; recoverable: boolean; created_at: string; }
interface Appliance { id: string; name: string; model: string; state: string; isolation_state: string; attestation_ok: boolean; last_heartbeat_at: string | null; telemetry: any; }
interface Account { id: string; connector_type: string; account_label: string; last_sync_at: string | null; }
interface Tenant { name: string; plan: string; key_ownership_model: string; vaults: any[]; }

export default function Dashboard() {
  const nav = useNavigate();
  const [snaps, setSnaps] = useState<Snapshot[]>([]);
  const [apps, setApps] = useState<Appliance[]>([]);
  const [accts, setAccts] = useState<Account[]>([]);
  const [tenant, setTenant] = useState<Tenant | null>(null);
  const [health, setHealth] = useState<any>(null);

  useEffect(() => {
    api.get<Snapshot[]>("/snapshots").then(setSnaps).catch(() => {});
    api.get<Appliance[]>("/appliances").then(setApps).catch(() => {});
    api.get<Account[]>("/connectors/accounts").then(setAccts).catch(() => {});
    api.get<Tenant>("/tenant").then(setTenant).catch(() => {});
    api.get<any>("/health").then(setHealth).catch(() => {});
  }, []);

  const totalBytes = snaps.reduce((s, x) => s + x.total_bytes, 0);
  const recoverable = snaps.filter((s) => s.recoverable).length;

  return (
    <>
      <div className="spread" style={{ marginBottom: 18 }}>
        <div className="stack">
          <div className="muted">{tenant?.name ?? "—"} · {tenant?.plan}</div>
          <div className="row" style={{ gap: 8 }}>
            <Pill tone="info"><Icon name="key" size={12} /> Keys: {tenant?.key_ownership_model}</Pill>
            <Pill tone={health?.pq_available ? "ok" : "warn"}>
              {health?.pq_available ? "Post-quantum active" : "PQC fallback (dev)"}
            </Pill>
          </div>
        </div>
        <div className="row">
          <button className="btn" onClick={() => nav("/onboarding")}>
            <Icon name="grid" size={15} /> Protection setup
          </button>
          <button className="btn primary" onClick={() => nav("/connectors")}>
            <Icon name="link" size={15} /> Add a source
          </button>
        </div>
      </div>

      <div className="grid grid-4">
        <Stat label="Protected sources" value={accts.length} hint={`${new Set(accts.map(a => a.connector_type)).size} services`} />
        <Stat label="Recovery points" value={snaps.length} hint={`${recoverable} verified recoverable`} />
        <Stat label="Protected data" value={bytes(totalBytes)} hint="encrypted at rest" />
        <Stat label="Appliances" value={apps.length} hint={`${apps.filter(a => a.attestation_ok).length} attested`} />
      </div>

      <div className="grid grid-2" style={{ marginTop: 16 }}>
        <Card>
          <div className="spread" style={{ marginBottom: 12 }}>
            <h2>Appliance fleet</h2>
            <a onClick={() => nav("/appliances")} style={{ cursor: "pointer", fontSize: 13 }}>View all</a>
          </div>
          {apps.length === 0 && <div className="muted">No appliances linked. Add one from the Appliances page.</div>}
          {apps.map((a) => (
            <div key={a.id} className="result-row" onClick={() => nav("/appliances")}>
              <div className="result-icon" style={{ background: "linear-gradient(135deg,#4f7cff,#35d0a5)" }}>
                <Icon name="server" size={18} />
              </div>
              <div className="flex1">
                <div style={{ fontWeight: 600 }}>{a.name} <span className="faint">· {a.model}</span></div>
                <div className="faint" style={{ fontSize: 12 }}>Heartbeat {timeAgo(a.last_heartbeat_at)}</div>
              </div>
              <ApplianceStatePill state={a.state} isolation={a.isolation_state} ok={a.attestation_ok} />
            </div>
          ))}
        </Card>

        <Card>
          <div className="spread" style={{ marginBottom: 12 }}>
            <h2>Recent recovery points</h2>
            <a onClick={() => nav("/snapshots")} style={{ cursor: "pointer", fontSize: 13 }}>View all</a>
          </div>
          {snaps.slice(0, 6).map((s) => (
            <div key={s.id ?? s.snapshot_id} className="result-row">
              <div className="result-icon" style={{ background: "#1a2234" }}>
                <Icon name="clock" size={17} />
              </div>
              <div className="flex1">
                <div className="mono">{s.snapshot_id.slice(0, 12)}…</div>
                <div className="faint" style={{ fontSize: 12 }}>
                  {s.object_count} objects · {bytes(s.total_bytes)} · {s.destination}
                </div>
              </div>
              {s.recoverable ? <Pill tone="ok">Recoverable</Pill> : <Pill tone="warn">Pending seal</Pill>}
            </div>
          ))}
          {snaps.length === 0 && <div className="muted">Run a backup to create recovery points.</div>}
        </Card>
      </div>
    </>
  );
}

export function ApplianceStatePill({ state, isolation, ok }: { state: string; isolation: string; ok: boolean }) {
  if (!ok) return <Pill tone="danger">Attestation failed</Pill>;
  if (state === "SEALED" && isolation === "sealed") return <Pill tone="ok">Offline & sealed</Pill>;
  if (state.startsWith("UNSEALED")) return <Pill tone="warn">Recovery session</Pill>;
  if (state === "ONLINE_STAGING") return <Pill tone="info">Online staging</Pill>;
  if (state === "QUARANTINED") return <Pill tone="danger">Quarantined</Pill>;
  return <Pill tone="info">{state}</Pill>;
}
