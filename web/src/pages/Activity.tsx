import { useEffect, useState } from "react";
import { api } from "../api";
import { Card, Pill, Stat, bytes, serverDate, Loading } from "../components/ui";
import { Icon, IconName } from "../components/Icon";
import { BrandIcon, brandForSource } from "../components/BrandIcon";
import { DestIcon } from "../components/DestIcon";
import { JobKindBadge } from "../components/JobKindBadge";

interface Event {
  kind: string; source: string; source_username?: string | null; source_type?: string; destination?: string;
  destination_label?: string; destination_provider?: string | null; object_count?: number; total_bytes?: number; status: string;
  snapshot_id?: string; at?: string; command?: string; owner?: string | null;
}
interface Job {
  id: string; source: string; source_username?: string | null; source_type?: string; kind: string;
  status: string; processed: number; total: number; message: string; error?: string | null;
  owner?: string | null; at?: string;
}
interface SourceError {
  account_id: string; source: string; source_username?: string | null; source_type?: string;
  needs_reauth?: boolean; error?: string; at?: string | null;
}
interface Activity {
  in_flight: Event[]; events: Event[]; jobs: Job[]; source_errors?: SourceError[];
  scope?: string; can_switch_scope?: boolean;
  summary: { recent: number; pending: number; queued_agents: number; active_jobs: number; source_errors?: number };
}

function ago(iso?: string | null): string {
  if (!iso) return "";
  const d = (Date.now() - serverDate(iso).getTime()) / 1000;
  if (d < 60) return "just now";
  if (d < 3600) return `${Math.floor(d / 60)}m ago`;
  if (d < 86400) return `${Math.floor(d / 3600)}h ago`;
  return `${Math.floor(d / 86400)}d ago`;
}

const isAppliance = (d?: string) => !!d && (/^appliance/.test(d) || d.startsWith("store:"));

function destLabel(e: Event): string {
  if (e.destination_label) return e.destination_label;
  const d = e.destination;
  if (!d || d === "cv-cloud") return "Arkive Cloud";
  if (d === "customer-s3") return "Customer S3";
  if (isAppliance(d)) return "Appliance";
  return d;
}

function SourceGlyph({ type, size = 16 }: { type?: string; size?: number }) {
  const brand = type ? brandForSource(type) : null;
  if (brand) return <BrandIcon name={brand} size={size} />;
  return <Icon name={"database" as IconName} size={size} />;
}

export default function ActivityPage() {
  const [data, setData] = useState<Activity | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [scope, setScope] = useState<"me" | "org">("me");

  async function load() {
    try { setData(await api.get<Activity>(`/activity?limit=80&scope=${scope}`)); } catch { /* ignore */ } finally { setLoaded(true); }
  }
  useEffect(() => {
    void load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, [scope]);

  const canSwitch = !!data?.can_switch_scope;
  const isOrg = scope === "org" && canSwitch;
  const events = data?.events ?? [];
  const bytesTotal = events.reduce((s, e) => s + (e.total_bytes ?? 0), 0);
  const objectsTotal = events.reduce((s, e) => s + (e.object_count ?? 0), 0);

  if (!loaded && !data) return <Loading label="Loading activity…" />;

  return (
    <>
      {canSwitch && (
        <div className="row" style={{ gap: 6, marginBottom: 14 }}>
          <button className={`btn sm ${scope === "me" ? "primary" : "ghost"}`} onClick={() => setScope("me")}>
            <Icon name="user" size={13} /> My activity
          </button>
          <button className={`btn sm ${scope === "org" ? "primary" : "ghost"}`} onClick={() => setScope("org")}>
            <Icon name="shield" size={13} /> Organization
          </button>
        </div>
      )}
      <div className="grid grid-4" style={{ marginBottom: 16 }}>
        <Stat label={isOrg ? "Org events" : "Recent events"} value={data?.summary.recent ?? 0} />
        <Stat label="Running now" value={(data?.summary.active_jobs ?? 0) + (data?.summary.queued_agents ?? 0)}
          hint={(data?.summary.active_jobs || data?.summary.queued_agents) ? "backups in progress" : "idle"} />
        <Stat label="Sealing" value={data?.summary.pending ?? 0} hint="awaiting appliance seal" />
        <Stat label="Objects ingested" value={objectsTotal} hint={bytes(bytesTotal)} />
      </div>

      {data && (data.source_errors?.length ?? 0) > 0 && (
        <Card style={{ marginBottom: 16, borderColor: "var(--warn)" }}>
          <h3 style={{ marginBottom: 10, color: "var(--warn)" }}>
            <Icon name="alert" size={15} /> Source issues
          </h3>
          {data.source_errors!.map((s) => (
            <div key={s.account_id} className="result-row">
              <div className="result-icon" style={{ background: "var(--inset)", color: "var(--warn)" }}>
                <SourceGlyph type={s.source_type} size={18} />
              </div>
              <div className="flex1">
                <div style={{ fontWeight: 600 }}>{s.source}{s.source_username && s.source_username !== s.source ? <span className="faint" style={{ fontWeight: 400 }}> ({s.source_username})</span> : null}</div>
                <div className="faint" style={{ fontSize: 12 }}>
                  {s.needs_reauth ? "Needs re-authorization" : "Last sync failed"}
                  {s.error ? ` — ${s.error.slice(0, 160)}` : ""} {s.at ? `· ${ago(s.at)}` : ""}
                </div>
              </div>
              <a className="btn sm primary" href="/connectors">{s.needs_reauth ? "Reconnect" : "Investigate"}</a>
            </div>
          ))}
        </Card>
      )}

      {data && (data.jobs.length > 0 || data.in_flight.length > 0) && (
        <Card style={{ marginBottom: 16 }}>
          <h3 style={{ marginBottom: 10 }}>Backup runs</h3>
          {data.jobs.map((j) => {
            const running = j.status === "running" || j.status === "queued";
            const pct = j.total > 0 ? Math.min(100, (j.processed / j.total) * 100) : 0;
            const tone = j.status === "done" ? "ok" : j.status === "failed" ? "danger" : running ? "warn" : "info";
            return (
              <div key={j.id} className="result-row" style={{ alignItems: "flex-start" }}>
                <div className="result-icon" style={{ background: brandForSource(j.source_type || "") ? "var(--inset)" : "linear-gradient(135deg,#4f7cff,#35d0a5)" }}>
                  <SourceGlyph type={j.source_type} size={18} />
                </div>
                <div className="flex1">
                  <div className="row" style={{ gap: 6, alignItems: "center" }}>
                    <span style={{ fontWeight: 600 }}>{j.source}{j.source_username && j.source_username !== j.source ? <span className="faint" style={{ fontWeight: 400 }}> ({j.source_username})</span> : null}</span>
                    <JobKindBadge kind={j.kind} />
                    {isOrg && j.owner && <Pill tone="info">{j.owner}</Pill>}
                  </div>
                  <div className="spread faint" style={{ fontSize: 12, margin: "4px 0" }}>
                    <span>{j.error || j.message || (j.kind === "backfill" ? "Crawling history…" : running ? "Working…" : "Completed")}</span>
                    {j.total > 0 && <span>{j.processed}/{j.total}</span>}
                  </div>
                  {running && (
                    <div className={`progress ${j.kind === "backfill" ? "backfill" : ""}`}>
                      <span style={{ width: j.total > 0 ? `${pct}%` : "40%", opacity: j.total > 0 ? 1 : 0.5 }} />
                    </div>
                  )}
                </div>
                <div className="stack" style={{ alignItems: "flex-end", gap: 6 }}>
                  <Pill tone={tone}>{j.status}</Pill>
                  {j.at && <span className="faint" style={{ fontSize: 11 }}>{ago(j.at)}</span>}
                </div>
              </div>
            );
          })}
          {data.in_flight.map((e, i) => (
            <div key={i} className="result-row">
              <div className="result-icon" style={{ background: "var(--inset)" }}>
                <SourceGlyph type={e.source_type} size={18} />
              </div>
              <div className="flex1">
                <div style={{ fontWeight: 600 }}>{e.source}</div>
                <div className="faint" style={{ fontSize: 12 }}>Desktop agent collecting locally, then pushing through the pipeline…</div>
              </div>
              <span className="row" style={{ gap: 8 }}>
                <span className="spinner-dot" />
                <Pill tone="warn">queued</Pill>
              </span>
            </div>
          ))}
        </Card>
      )}

      <Card>
        <div className="spread" style={{ marginBottom: 12 }}>
          <h3 style={{ margin: 0 }}>Ingestion timeline</h3>
          <span className="faint" style={{ fontSize: 12 }}>live · refreshes every 5s</span>
        </div>
        {events.length === 0 && <div className="muted">No ingestion activity yet. Trigger a sync from the Data Map.</div>}
        {events.map((e, i) => (
          <div key={i} className="result-row">
            <div className="result-icon" style={{ background: brandForSource(e.source_type || "") ? "var(--inset)" : "linear-gradient(135deg,#4f7cff,#35d0a5)" }}>
              <SourceGlyph type={e.source_type} size={18} />
            </div>
            <div className="flex1">
              <div className="row" style={{ gap: 6, alignItems: "center" }}>
                <span style={{ fontWeight: 600 }}>{e.source}{e.source_username && e.source_username !== e.source ? <span className="faint" style={{ fontWeight: 400 }}> ({e.source_username})</span> : null}</span>
                {isOrg && e.owner && <Pill tone="info">{e.owner}</Pill>}
              </div>
              <div className="faint" style={{ fontSize: 12.5 }}>
                <DestIcon dest={e.destination} provider={e.destination_provider || undefined} size={12} /> {destLabel(e)}
                {" · "}{e.object_count ?? 0} objects · {bytes(e.total_bytes ?? 0)}
              </div>
            </div>
            <div className="stack" style={{ alignItems: "flex-end", gap: 6 }}>
              <Pill tone={e.status === "recoverable" ? "ok" : "warn"}>
                {e.status === "recoverable" ? "recoverable" : "sealing"}
              </Pill>
              <span className="faint" style={{ fontSize: 11 }}>{ago(e.at)}</span>
            </div>
          </div>
        ))}
      </Card>
    </>
  );
}
