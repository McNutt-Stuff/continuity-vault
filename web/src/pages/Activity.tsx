import { useEffect, useState } from "react";
import { api } from "../api";
import { Card, Pill, Stat, bytes, serverDate } from "../components/ui";
import { Icon, IconName } from "../components/Icon";
import { BrandIcon, brandForSource } from "../components/BrandIcon";

interface Event {
  kind: string; source: string; source_type?: string; destination?: string;
  destination_label?: string; object_count?: number; total_bytes?: number; status: string;
  snapshot_id?: string; at?: string; command?: string;
}
interface Job {
  id: string; source: string; source_type?: string; kind: string;
  status: string; processed: number; total: number; message: string;
}
interface Activity {
  in_flight: Event[]; events: Event[]; jobs: Job[];
  summary: { recent: number; pending: number; queued_agents: number; active_jobs: number };
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

  async function load() {
    try { setData(await api.get<Activity>("/activity?limit=80")); } catch { /* ignore */ }
  }
  useEffect(() => {
    void load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, []);

  const events = data?.events ?? [];
  const bytesTotal = events.reduce((s, e) => s + (e.total_bytes ?? 0), 0);
  const objectsTotal = events.reduce((s, e) => s + (e.object_count ?? 0), 0);

  return (
    <>
      <div className="grid grid-4" style={{ marginBottom: 16 }}>
        <Stat label="Recent events" value={data?.summary.recent ?? 0} />
        <Stat label="Running now" value={(data?.summary.active_jobs ?? 0) + (data?.summary.queued_agents ?? 0)}
          hint={(data?.summary.active_jobs || data?.summary.queued_agents) ? "backups in progress" : "idle"} />
        <Stat label="Sealing" value={data?.summary.pending ?? 0} hint="awaiting appliance seal" />
        <Stat label="Objects ingested" value={objectsTotal} hint={bytes(bytesTotal)} />
      </div>

      {data && (data.jobs.length > 0 || data.in_flight.length > 0) && (
        <Card style={{ marginBottom: 16 }}>
          <h3 style={{ marginBottom: 10 }}>In progress</h3>
          {data.jobs.map((j) => {
            const pct = j.total > 0 ? Math.min(100, (j.processed / j.total) * 100) : 0;
            return (
              <div key={j.id} className="result-row" style={{ alignItems: "flex-start" }}>
                <div className="result-icon" style={{ background: brandForSource(j.source_type || "") ? "#0e1524" : "linear-gradient(135deg,#4f7cff,#35d0a5)" }}>
                  <SourceGlyph type={j.source_type} size={18} />
                </div>
                <div className="flex1">
                  <div style={{ fontWeight: 600 }}>{j.source}</div>
                  <div className="spread faint" style={{ fontSize: 12, margin: "4px 0" }}>
                    <span>{j.message || "Working…"}</span>
                    {j.total > 0 && <span>{j.processed}/{j.total}</span>}
                  </div>
                  <div className="progress">
                    <span style={{ width: j.total > 0 ? `${pct}%` : "40%", opacity: j.total > 0 ? 1 : 0.5 }} />
                  </div>
                </div>
                <Pill tone="warn">{j.status}</Pill>
              </div>
            );
          })}
          {data.in_flight.map((e, i) => (
            <div key={i} className="result-row">
              <div className="result-icon" style={{ background: "#0e1524" }}>
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
            <div className="result-icon" style={{ background: brandForSource(e.source_type || "") ? "#0e1524" : "linear-gradient(135deg,#4f7cff,#35d0a5)" }}>
              <SourceGlyph type={e.source_type} size={18} />
            </div>
            <div className="flex1">
              <div style={{ fontWeight: 600 }}>{e.source}</div>
              <div className="faint" style={{ fontSize: 12.5 }}>
                <Icon name={isAppliance(e.destination) ? "server" : "cloud"} size={12} /> {destLabel(e)}
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
