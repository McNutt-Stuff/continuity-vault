import { ReactNode, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { useAuth } from "../auth";
import { Card, Pill, bytes, Loading, groupScope } from "../components/ui";
import { Icon } from "../components/Icon";
import { SourceIcon } from "../components/SourceIcon";
import { DestIcon } from "../components/DestIcon";
import { PhotoPickerModal } from "../components/PhotoPicker";

interface SourceType { type: string; displayName: string; icon: string; color: string; count: number; }
interface ObjectBucket { key: string; label: string; icon: string; color: string; count: number; }
interface StorageDest { id: string; label: string; kind: string; icon: string; provider?: string; }
interface Overview {
  sources: { count: number; types: SourceType[] };
  objects: { total: number; breakdown: ObjectBucket[]; by_source: ObjectBucket[] };
  data: { protected_bytes: number; licensed_bytes: number; percent: number | null };
  storage: {
    vault_count: number; destinations: StorageDest[];
    usage: { cloud: number; appliance: number; customer: number };
    oldest_content_at: string | null;
  };
  protection: { key_ownership_model: string; encrypted: boolean };
  connector_health?: { issues: number; needs_reauth: number };
  cloud_deletion?: { pending: boolean; delete_at: string; days_left: number; object_count: number; bytes: number } | null;
  scope?: "me" | "org";
  can_switch_scope?: boolean;
}
interface Tenant { name: string; plan: string; key_ownership_model: string; tenant_type?: string; }
interface PendingAction {
  id: string; kind: string; title: string; message: string;
  source_type: string; collection_id?: string | null; account_id?: string | null;
}
interface TrendSeries { key: string; label: string; icon: string; color: string; values: number[]; current: number; }
interface Trends { period: string; points: string[]; series: TrendSeries[]; }

const PERIODS: { id: string; label: string }[] = [
  { id: "week", label: "1W" }, { id: "month", label: "1M" },
  { id: "quarter", label: "3M" }, { id: "year", label: "1Y" }, { id: "all", label: "All" },
];

function fmtOldest(iso: string | null): string {
  if (!iso) return "No data yet";
  const d = new Date(iso.endsWith("Z") ? iso : `${iso}Z`);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}
const ICONS = ["mail", "key", "cloud", "file", "database", "image", "activity", "user", "server", "shield", "lock", "clock", "grid", "link"];
const iconName = (n: string) => (ICONS.includes(n) ? n : "database") as never;

export default function Dashboard() {
  const nav = useNavigate();
  const { me } = useAuth();
  const [ov, setOv] = useState<Overview | null>(null);
  const [tenant, setTenant] = useState<Tenant | null>(null);
  const [health, setHealth] = useState<{ pq_available?: boolean } | null>(null);
  const [period, setPeriod] = useState("week");
  const [trends, setTrends] = useState<Trends | null>(null);
  const [actions, setActions] = useState<PendingAction[]>([]);
  const [pickerAccount, setPickerAccount] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [scope, setScope] = useState<"me" | "org">("me");

  function reloadActions() { api.get<PendingAction[]>("/actions").then(setActions).catch(() => {}); }
  async function dismissAction(id: string) {
    try { await api.post(`/actions/${id}/dismiss`, {}); } catch { /* ignore */ }
    reloadActions();
  }

  useEffect(() => {
    api.get<Overview>(`/overview?scope=${scope}`).then(setOv).catch(() => {}).finally(() => setLoaded(true));
  }, [scope]);

  useEffect(() => {
    api.get<Tenant>("/tenant").then(setTenant).catch(() => {});
    api.get<{ pq_available?: boolean }>("/health").then(setHealth).catch(() => {});
    reloadActions();
  }, []);

  useEffect(() => {
    api.get<Trends>(`/overview/trends?period=${period}&scope=${scope}`).then(setTrends).catch(() => {});
  }, [period, scope]);

  const objTotal = ov?.objects.breakdown.reduce((s, b) => s + b.count, 0) || 0;

  if (!loaded && !ov) return <Loading label="Loading your dashboard…" />;

  return (
    <>
      <div className="spread" style={{ marginBottom: 18 }}>
        <div className="stack">
          <h2 style={{ margin: 0 }}>
            Welcome {me?.display_name || ""}
            {tenant && ["family", "business", "enterprise"].includes(tenant.plan)
              ? <span className="faint" style={{ fontWeight: 400 }}> ({tenant.name})</span>
              : null}
          </h2>
          <div className="row" style={{ gap: 8 }}>
            <Pill tone="info"><Icon name="key" size={12} /> Keys: {tenant?.key_ownership_model}</Pill>
            <Pill tone={health?.pq_available ? "ok" : "warn"} dot>
              {health?.pq_available ? "Post-quantum active" : "PQC fallback (dev)"}
            </Pill>
          </div>
        </div>
        <div className="row">
          {ov?.can_switch_scope && (
            <div className="row" style={{ gap: 0, border: "1px solid var(--border-soft)", borderRadius: 8, overflow: "hidden" }}>
              <button className={`btn sm ${scope === "me" ? "primary" : "ghost"}`} style={{ borderRadius: 0 }} onClick={() => setScope("me")}>
                <Icon name="user" size={13} /> My account
              </button>
              <button className={`btn sm ${scope === "org" ? "primary" : "ghost"}`} style={{ borderRadius: 0 }} onClick={() => setScope("org")}>
                <Icon name="grid" size={13} /> {groupScope(tenant?.plan)?.label ?? "My organization"}
              </button>
            </div>
          )}
          <button className="btn" onClick={() => nav("/onboarding")}>
            <Icon name="grid" size={15} /> Protection setup
          </button>
          <button className="btn primary" onClick={() => nav("/connectors")}>
            <Icon name="link" size={15} /> Add a source
          </button>
        </div>
      </div>

      {ov?.cloud_deletion?.pending && (
        <Card style={{ marginBottom: 16, borderColor: "var(--danger)", cursor: "pointer" }} onClick={() => nav("/onboarding")}>
          <div className="row" style={{ gap: 12, alignItems: "center" }}>
            <div className="result-icon" style={{ background: "var(--inset)", color: "var(--danger)" }}>
              <Icon name="alert" size={18} />
            </div>
            <div className="flex1">
              <div style={{ fontWeight: 600 }}>
                Arkive Cloud data pending deletion — {ov.cloud_deletion.days_left} day{ov.cloud_deletion.days_left === 1 ? "" : "s"} left
              </div>
              <div className="faint" style={{ fontSize: 12.5 }}>
                {ov.cloud_deletion.object_count.toLocaleString()} object{ov.cloud_deletion.object_count === 1 ? "" : "s"} ({bytes(ov.cloud_deletion.bytes)}) will be permanently deleted from Arkive Cloud and cannot be recovered.
                Re-subscribe in Protection Setup to cancel.
              </div>
            </div>
            <Icon name="grid" size={16} />
          </div>
        </Card>
      )}

      {ov?.connector_health && ov.connector_health.issues > 0 && (
        <Card style={{ marginBottom: 16, borderColor: "var(--warn)", cursor: "pointer" }} onClick={() => nav("/connectors")}>
          <div className="row" style={{ gap: 12, alignItems: "center" }}>
            <div className="result-icon" style={{ background: "var(--inset)", color: "var(--warn)" }}>
              <Icon name="alert" size={18} />
            </div>
            <div className="flex1">
              <div style={{ fontWeight: 600 }}>Some of your sources are having issues</div>
              <div className="faint" style={{ fontSize: 12.5 }}>
                {ov.connector_health.issues} source{ov.connector_health.issues === 1 ? "" : "s"} need attention
                {ov.connector_health.needs_reauth > 0 ? ` · ${ov.connector_health.needs_reauth} need re-authorization` : ""} — click to investigate
              </div>
            </div>
            <Icon name="link" size={16} />
          </div>
        </Card>
      )}

      {actions.length > 0 && (
        <Card style={{ marginBottom: 16, borderColor: "var(--accent,#4f7cff)" }}>
          <div className="row" style={{ gap: 8, marginBottom: 4 }}>
            <Icon name="bell" size={16} />
            <h2 style={{ margin: 0, fontSize: 16 }}>Actions needed</h2>
          </div>
          <div className="stack" style={{ gap: 0 }}>
            {actions.map((a) => (
              <div key={a.id} className="row" style={{ gap: 10, alignItems: "center", padding: "10px 0", borderTop: "1px solid var(--border-soft)" }}>
                <div className="result-icon" style={{ width: 30, height: 30, background: "var(--inset)" }}>
                  {a.source_type ? <SourceIcon type={a.source_type} fallback="image" size={16} /> : <Icon name="bell" size={15} />}
                </div>
                <div className="flex1">
                  <div style={{ fontWeight: 600, fontSize: 13.5 }}>{a.title}</div>
                  <div className="faint" style={{ fontSize: 12 }}>{a.message}</div>
                </div>
                {a.kind === "photos_pick" && a.account_id && (
                  <button className="btn primary sm" onClick={() => setPickerAccount(a.account_id!)}>Add photos</button>
                )}
                <button className="btn ghost sm" onClick={() => dismissAction(a.id)}>Dismiss</button>
              </div>
            ))}
          </div>
        </Card>
      )}

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
                             borderRadius: 999, fontSize: 11, background: "var(--inset)", border: "1px solid var(--border-soft)" }}>
                <SourceIcon type={t.type} fallback={iconName(t.icon)} size={13} /> {t.count}
              </span>
            ))}
            {ov && ov.sources.types.length === 0 && <span className="faint" style={{ fontSize: 12 }}>No sources yet</span>}
          </div>
        </Card>

        {/* Objects protected — pie by source type, text top-left */}
        <Card style={{ minHeight: 134, position: "relative", overflow: "hidden" }}>
          <div className="faint" style={{ fontSize: 12 }}>Objects protected</div>
          <div style={{ fontSize: 34, fontWeight: 700, lineHeight: 1.1, marginTop: 2 }}>{(ov?.objects.total ?? 0).toLocaleString()}</div>
          {objTotal > 0 ? (
            <>
              <div className="stack" style={{ gap: 3, marginTop: 8, maxWidth: "62%" }}>
                {ov!.objects.by_source.slice(0, 3).map((b) => (
                  <span key={b.key} className="faint" style={{ fontSize: 11, display: "inline-flex", alignItems: "center", gap: 5 }}>
                    <span style={{ width: 8, height: 8, borderRadius: 2, background: b.color, flexShrink: 0 }} /> {b.label} {b.count.toLocaleString()}
                  </span>
                ))}
              </div>
              <div style={{ position: "absolute", right: 6, bottom: 2 }}>
                <Donut data={ov!.objects.by_source} size={96} />
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
              <div style={{ height: 8, borderRadius: 999, marginTop: 12, background: "var(--inset)", overflow: "hidden" }}>
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
                             fontSize: 11, background: "var(--inset)", border: "1px solid var(--border-soft)" }}>
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
            <div className="row" style={{ gap: 4 }}>
              {PERIODS.map((p) => (
                <span key={p.id} onClick={() => setPeriod(p.id)}
                      style={{ cursor: "pointer", fontSize: 11.5, padding: "2px 8px", borderRadius: 999,
                               fontWeight: 600, background: period === p.id ? "var(--accent,#4f7cff)" : "var(--inset)",
                               color: period === p.id ? "#fff" : "var(--muted,#8a94a7)" }}>
                  {p.label}
                </span>
              ))}
            </div>
          </div>
          {(!trends || trends.series.length === 0) && <div className="muted">Run a backup to start protecting your data.</div>}
          <div className="stack" style={{ gap: 4 }}>
            {(trends?.series || []).map((s) => (
              <div key={s.key} className="row" style={{ gap: 10, alignItems: "center", padding: "6px 0" }}>
                <div className="result-icon" style={{ width: 30, height: 30, background: `${s.color}22`, color: s.color }}>
                  <Icon name={iconName(s.icon)} size={15} />
                </div>
                <div style={{ width: 120 }}>
                  <div style={{ fontSize: 13, fontWeight: 600 }}>{s.label}</div>
                  <div className="faint" style={{ fontSize: 11 }}>{s.current.toLocaleString()} total</div>
                </div>
                <div className="flex1" style={{ display: "flex", justifyContent: "flex-end" }}>
                  <Sparkline values={s.values} color={s.color} />
                </div>
              </div>
            ))}
          </div>
          {trends && trends.series.length > 0 && (
            <div className="faint" style={{ fontSize: 11, marginTop: 8, textAlign: "right" }}>
              cumulative objects over the last {PERIODS.find((p) => p.id === period)?.label === "All" ? "period" : PERIODS.find((p) => p.id === period)?.label}
            </div>
          )}
        </Card>

        <Card>
          <div className="spread" style={{ marginBottom: 12 }}>
            <h2>Protection &amp; storage</h2>
            <a onClick={() => nav("/mappings")} style={{ cursor: "pointer", fontSize: 13 }}>Manage</a>
          </div>
          <div className="grid grid-2" style={{ gap: 12 }}>
            <Fact icon="cloud" label="Arkive Cloud" value={ov ? bytes(ov.storage.usage.cloud) : "—"} />
            <Fact icon="server" label="Secure hardware" value={ov ? bytes(ov.storage.usage.appliance) : "—"} />
            {ov && ov.storage.usage.customer > 0 && (
              <Fact icon="cloud" label="Your cloud storage" value={bytes(ov.storage.usage.customer)} />
            )}
            <Fact icon="clock" label="History reaches back to" value={ov ? fmtOldest(ov.storage.oldest_content_at) : "—"} />
            <Fact icon="shield" label="Encryption" value={health?.pq_available ? "Hybrid post-quantum" : "Hybrid (dev)"} />
            <Fact icon="key" label="Key ownership" value={ov?.protection.key_ownership_model ?? tenant?.key_ownership_model ?? "—"} />
          </div>
          <div className="faint" style={{ fontSize: 11, marginTop: 10 }}>Recovery points are retained indefinitely — your history isn't pruned.</div>
          {ov && ov.storage.destinations.length > 0 && (
            <div style={{ marginTop: 14, paddingTop: 12, borderTop: "1px solid var(--border-soft)" }}>
              <div className="faint" style={{ fontSize: 11.5, marginBottom: 6 }}>Stored across</div>
              <div className="row" style={{ gap: 12, flexWrap: "wrap" }}>
                {ov.storage.destinations.map((d) => (
                  <span key={d.id} className="row" style={{ gap: 5, fontSize: 12 }}>
                    <DestIcon dest={d.id} provider={d.provider} size={13} /> {d.label}
                  </span>
                ))}
              </div>
            </div>
          )}
        </Card>
      </div>
      {pickerAccount && (
        <PhotoPickerModal accountId={pickerAccount} onClose={() => setPickerAccount(null)}
                          onStarted={reloadActions} />
      )}
    </>
  );
}

function Fact({ icon, label, value }: { icon: string; label: string; value: ReactNode }) {
  return (
    <div className="row" style={{ gap: 10, alignItems: "flex-start" }}>
      <div className="result-icon" style={{ width: 28, height: 28, background: "var(--inset)" }}>
        <Icon name={iconName(icon)} size={14} />
      </div>
      <div>
        <div className="faint" style={{ fontSize: 11.5 }}>{label}</div>
        <div style={{ fontSize: 13.5, fontWeight: 600 }}>{value}</div>
      </div>
    </div>
  );
}

// Donut/pie of the object-type mix. Segments drawn as dasharray arcs on a ring.
function Donut({ data, size = 96 }: { data: { count: number; color: string; label: string }[]; size?: number }) {
  const total = data.reduce((s, d) => s + d.count, 0) || 1;
  const stroke = Math.round(size * 0.2);
  const r = (size - stroke) / 2;
  const cx = size / 2, cy = size / 2;
  const circ = 2 * Math.PI * r;
  let acc = 0;
  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
      <circle cx={cx} cy={cy} r={r} fill="none" stroke="var(--inset)" strokeWidth={stroke} />
      <g transform={`rotate(-90 ${cx} ${cy})`}>
        {data.map((d, i) => {
          const len = (d.count / total) * circ;
          const el = (
            <circle key={i} cx={cx} cy={cy} r={r} fill="none" stroke={d.color} strokeWidth={stroke}
                    strokeDasharray={`${len} ${circ - len}`} strokeDashoffset={-acc}>
              <title>{`${d.label}: ${d.count}`}</title>
            </circle>
          );
          acc += len;
          return el;
        })}
      </g>
    </svg>
  );
}

// Minimal area sparkline for a cumulative series.
function Sparkline({ values, color, width = 120, height = 30 }: { values: number[]; color: string; width?: number; height?: number }) {
  const vals = values.length >= 2 ? values : [...values, ...values, 0].slice(0, 2);
  const max = Math.max(...vals, 1);
  const min = Math.min(...vals, 0);
  const range = max - min || 1;
  const x = (i: number) => (i / (vals.length - 1)) * (width - 2) + 1;
  const y = (v: number) => height - 2 - ((v - min) / range) * (height - 4);
  const line = vals.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const area = `${x(0).toFixed(1)},${height} ${line} ${x(vals.length - 1).toFixed(1)},${height}`;
  return (
    <svg width={width} height={height} style={{ display: "block" }}>
      <polygon points={area} fill={color} opacity={0.12} />
      <polyline points={line} fill="none" stroke={color} strokeWidth="1.6" strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  );
}

export function ApplianceStatePill({ state, isolation, ok }: { state: string; isolation: string; ok: boolean }) {
  if (!ok) return <Pill tone="danger" dot>Attestation failed</Pill>;
  if (state === "SEALED" && isolation === "sealed") return <Pill tone="ok" dot>Offline & sealed</Pill>;
  if (state.startsWith("UNSEALED")) return <Pill tone="warn" dot>Recovery session</Pill>;
  if (state === "ONLINE_STAGING") return <Pill tone="info" dot>Online staging</Pill>;
  if (state === "QUARANTINED") return <Pill tone="danger" dot>Quarantined</Pill>;
  return <Pill tone="info" dot>{state}</Pill>;
}
