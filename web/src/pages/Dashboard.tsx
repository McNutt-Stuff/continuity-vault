import { ReactNode, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { Card, Pill, bytes } from "../components/ui";
import { Icon } from "../components/Icon";

interface SourceType { type: string; displayName: string; icon: string; color: string; count: number; }
interface ObjectBucket { key: string; label: string; icon: string; color: string; count: number; }
interface StorageDest { id: string; label: string; kind: string; icon: string; }
interface Overview {
  sources: { count: number; types: SourceType[] };
  objects: { total: number; breakdown: ObjectBucket[] };
  data: { protected_bytes: number; licensed_bytes: number; percent: number | null };
  storage: { vault_count: number; destinations: StorageDest[] };
  retention: { cloud_days: number; appliance_days: number; immutability_days: number; rpo_minutes: number };
  protection: { key_ownership_model: string; encrypted: boolean };
}
interface Tenant { name: string; plan: string; key_ownership_model: string; }

function fmtDuration(days: number): string {
  if (days >= 365) { const y = days / 365; return `${Number.isInteger(y) ? y : y.toFixed(1)} yr`; }
  if (days >= 30) return `${Math.round(days / 30)} mo`;
  return `${days} d`;
}
function fmtRpo(min: number): string {
  if (min >= 1440) return `${Math.round(min / 1440)}d`;
  if (min >= 60) return `${Math.round(min / 60)}h`;
  return `${min}m`;
}
const ICONS = ["mail", "key", "cloud", "file", "database", "image", "activity", "user", "server", "shield", "lock", "clock", "grid", "link"];
const iconName = (n: string) => (ICONS.includes(n) ? n : "database") as never;

export default function Dashboard() {
  const nav = useNavigate();
  const [ov, setOv] = useState<Overview | null>(null);
  const [tenant, setTenant] = useState<Tenant | null>(null);
  const [health, setHealth] = useState<{ pq_available?: boolean } | null>(null);

  useEffect(() => {
    api.get<Overview>("/overview").then(setOv).catch(() => {});
    api.get<Tenant>("/tenant").then(setTenant).catch(() => {});
    api.get<{ pq_available?: boolean }>("/health").then(setHealth).catch(() => {});
  }, []);

  const objTotal = ov?.objects.breakdown.reduce((s, b) => s + b.count, 0) || 0;

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

      {/* --- Top row: four headline cards --------------------------------- */}
      <div className="grid grid-4">
        {/* Protected sources + the mix of source types */}
        <Card style={{ minHeight: 134 }}>
          <div className="faint" style={{ fontSize: 12 }}>Protected sources</div>
          <div style={{ fontSize: 34, fontWeight: 700, lineHeight: 1.1, marginTop: 2 }}>{ov?.sources.count ?? "—"}</div>
          <div className="row" style={{ gap: 6, marginTop: 10, flexWrap: "wrap" }}>
            {(ov?.sources.types || []).map((t) => (
              <span key={t.type} title={`${t.displayName} · ${t.count}`}
                    style={{ display: "inline-flex", alignItems: "center", gap: 5, padding: "3px 8px",
                             borderRadius: 999, fontSize: 11, background: `${t.color}22`, color: t.color, border: `1px solid ${t.color}44` }}>
                <Icon name={iconName(t.icon)} size={12} /> {t.count}
              </span>
            ))}
            {ov && ov.sources.types.length === 0 && <span className="faint" style={{ fontSize: 12 }}>No sources yet</span>}
          </div>
        </Card>

        {/* Objects protected + type mix */}
        <Card style={{ minHeight: 134 }}>
          <div className="faint" style={{ fontSize: 12 }}>Objects protected</div>
          <div style={{ fontSize: 34, fontWeight: 700, lineHeight: 1.1, marginTop: 2 }}>{(ov?.objects.total ?? 0).toLocaleString()}</div>
          {objTotal > 0 ? (
            <>
              <div style={{ display: "flex", height: 8, borderRadius: 999, overflow: "hidden", marginTop: 10, background: "#0e1524" }}>
                {ov!.objects.breakdown.map((b) => (
                  <div key={b.key} title={`${b.label}: ${b.count}`} style={{ width: `${(b.count / objTotal) * 100}%`, background: b.color }} />
                ))}
              </div>
              <div className="row" style={{ gap: 10, marginTop: 8, flexWrap: "wrap" }}>
                {ov!.objects.breakdown.slice(0, 3).map((b) => (
                  <span key={b.key} className="faint" style={{ fontSize: 11, display: "inline-flex", alignItems: "center", gap: 4 }}>
                    <span style={{ width: 8, height: 8, borderRadius: 2, background: b.color }} /> {b.label} {b.count}
                  </span>
                ))}
              </div>
            </>
          ) : <div className="faint" style={{ fontSize: 12, marginTop: 10 }}>Nothing captured yet</div>}
        </Card>

        {/* Data protected vs licensed allowance */}
        <Card style={{ minHeight: 134 }}>
          <div className="faint" style={{ fontSize: 12 }}>Data protected</div>
          <div style={{ fontSize: 34, fontWeight: 700, lineHeight: 1.1, marginTop: 2 }}>{bytes(ov?.data.protected_bytes ?? 0)}</div>
          {ov && ov.data.licensed_bytes > 0 ? (
            <>
              <div style={{ height: 8, borderRadius: 999, marginTop: 12, background: "#0e1524", overflow: "hidden" }}>
                <div style={{ width: `${Math.min(100, ov.data.percent ?? 0)}%`, height: "100%",
                              background: (ov.data.percent ?? 0) > 90 ? "var(--danger-c,#f2545b)" : "linear-gradient(90deg,#4f7cff,#35d0a5)" }} />
              </div>
              <div className="faint" style={{ fontSize: 11.5, marginTop: 6 }}>
                {ov.data.percent}% of {bytes(ov.data.licensed_bytes)} licensed
              </div>
            </>
          ) : <div className="faint" style={{ fontSize: 12, marginTop: 12 }}>Encrypted at rest · unlimited plan</div>}
        </Card>

        {/* Vaults + storage destinations */}
        <Card style={{ minHeight: 134 }}>
          <div className="faint" style={{ fontSize: 12 }}>Vaults & storage</div>
          <div style={{ fontSize: 34, fontWeight: 700, lineHeight: 1.1, marginTop: 2 }}>
            {ov?.storage.vault_count ?? "—"} <span style={{ fontSize: 15, fontWeight: 500 }} className="faint">Vault{ov && ov.storage.vault_count === 1 ? "" : "s"}</span>
          </div>
          <div className="faint" style={{ fontSize: 11.5, marginTop: 2 }}>
            {ov?.storage.destinations.length ?? 0} storage destination{ov && ov.storage.destinations.length === 1 ? "" : "s"}
          </div>
          <div className="row" style={{ gap: 6, marginTop: 8, flexWrap: "wrap" }}>
            {(ov?.storage.destinations || []).map((d) => (
              <span key={d.id} title={d.label}
                    style={{ display: "inline-flex", alignItems: "center", gap: 5, padding: "3px 8px", borderRadius: 8,
                             fontSize: 11, background: "#0e1524", border: "1px solid var(--border-soft)" }}>
                <Icon name={iconName(d.icon)} size={12} /> {d.label}
              </span>
            ))}
          </div>
        </Card>
      </div>

      {/* --- Second row: what's protected + protection posture ------------ */}
      <div className="grid grid-2" style={{ marginTop: 16 }}>
        <Card>
          <div className="spread" style={{ marginBottom: 12 }}>
            <h2>What's protected</h2>
            <a onClick={() => nav("/search")} style={{ cursor: "pointer", fontSize: 13 }}>Explore</a>
          </div>
          {objTotal === 0 && <div className="muted">Run a backup to start protecting your data.</div>}
          <div className="stack" style={{ gap: 10 }}>
            {(ov?.objects.breakdown || []).map((b) => (
              <div key={b.key} className="row" style={{ gap: 10, alignItems: "center" }}>
                <div className="result-icon" style={{ width: 30, height: 30, background: `${b.color}22`, color: b.color }}>
                  <Icon name={iconName(b.icon)} size={15} />
                </div>
                <div className="flex1">
                  <div className="spread" style={{ marginBottom: 4 }}>
                    <span style={{ fontSize: 13, fontWeight: 600 }}>{b.label}</span>
                    <span className="faint" style={{ fontSize: 12 }}>{b.count.toLocaleString()}</span>
                  </div>
                  <div style={{ height: 6, borderRadius: 999, background: "#0e1524", overflow: "hidden" }}>
                    <div style={{ width: `${(b.count / objTotal) * 100}%`, height: "100%", background: b.color }} />
                  </div>
                </div>
              </div>
            ))}
          </div>
        </Card>

        <Card>
          <div className="spread" style={{ marginBottom: 12 }}>
            <h2>Protection & retention</h2>
            <a onClick={() => nav("/mappings")} style={{ cursor: "pointer", fontSize: 13 }}>Manage</a>
          </div>
          <div className="grid grid-2" style={{ gap: 12 }}>
            <Fact icon="cloud" label="Cloud retention" value={ov ? fmtDuration(ov.retention.cloud_days) : "—"} />
            <Fact icon="server" label="Appliance retention" value={ov ? fmtDuration(ov.retention.appliance_days) : "—"} />
            <Fact icon="lock" label="Immutability (WORM)" value={ov ? fmtDuration(ov.retention.immutability_days) : "—"} />
            <Fact icon="clock" label="Recovery point" value={ov ? `every ${fmtRpo(ov.retention.rpo_minutes)}` : "—"} />
            <Fact icon="shield" label="Encryption" value={health?.pq_available ? "Hybrid post-quantum" : "Hybrid (dev)"} />
            <Fact icon="key" label="Key ownership" value={ov?.protection.key_ownership_model ?? tenant?.key_ownership_model ?? "—"} />
          </div>
          {ov && ov.storage.destinations.length > 0 && (
            <div style={{ marginTop: 14, paddingTop: 12, borderTop: "1px solid var(--border-soft)" }}>
              <div className="faint" style={{ fontSize: 11.5, marginBottom: 6 }}>Stored across</div>
              <div className="row" style={{ gap: 12, flexWrap: "wrap" }}>
                {ov.storage.destinations.map((d) => (
                  <span key={d.id} className="row" style={{ gap: 5, fontSize: 12 }}>
                    <Icon name={iconName(d.icon)} size={13} /> {d.label}
                  </span>
                ))}
              </div>
            </div>
          )}
        </Card>
      </div>
    </>
  );
}

function Fact({ icon, label, value }: { icon: string; label: string; value: ReactNode }) {
  return (
    <div className="row" style={{ gap: 10, alignItems: "flex-start" }}>
      <div className="result-icon" style={{ width: 28, height: 28, background: "#0e1524" }}>
        <Icon name={iconName(icon)} size={14} />
      </div>
      <div>
        <div className="faint" style={{ fontSize: 11.5 }}>{label}</div>
        <div style={{ fontSize: 13.5, fontWeight: 600 }}>{value}</div>
      </div>
    </div>
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
