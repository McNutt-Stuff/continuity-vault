import { useEffect, useState } from "react";
import { api } from "../api";
import { Card, Pill, timeAgo } from "../components/ui";
import { Icon } from "../components/Icon";

interface Event {
  actor: string; action: string; resource: string;
  category: string; severity: string;
  detail: Record<string, unknown>; entry_hash: string; created_at: string;
}
interface AuditResp {
  chain_valid: boolean;
  tallies: Record<string, number>;
  events: Event[];
}

const SEV_TONE: Record<string, "ok" | "info" | "warn" | "danger"> = {
  info: "info", notice: "ok", warning: "warn", critical: "danger",
};
const CATEGORIES = ["", "activity", "security", "credential", "admin", "system"];
const SEVERITIES = ["", "info", "notice", "warning", "critical"];

export default function Audit() {
  const [data, setData] = useState<AuditResp | null>(null);
  const [category, setCategory] = useState("");
  const [severity, setSeverity] = useState("");
  const [actor, setActor] = useState("");

  async function load() {
    const p = new URLSearchParams();
    if (category) p.set("category", category);
    if (severity) p.set("severity", severity);
    if (actor) p.set("actor", actor);
    setData(await api.get<AuditResp>(`/audit?${p.toString()}`));
  }
  useEffect(() => { void load(); }, [category, severity]);

  return (
    <>
      <Card style={{ marginBottom: 16 }}>
        <div className="spread" style={{ marginBottom: 12 }}>
          <div>
            <h2 style={{ margin: 0 }}>Audit log</h2>
            <div className="faint" style={{ fontSize: 12.5 }}>
              Tamper-evident, hash-chained record of all account activity, credential access, and
              security events.
            </div>
          </div>
          {data && (
            <Pill tone={data.chain_valid ? "ok" : "danger"}>
              <Icon name={data.chain_valid ? "lock" : "alert"} size={12} />
              {data.chain_valid ? "Chain verified" : "Chain broken"}
            </Pill>
          )}
        </div>

        <div className="chips" style={{ marginBottom: 10 }}>
          <span className="faint" style={{ fontSize: 11, alignSelf: "center", marginRight: 2 }}>Category</span>
          {CATEGORIES.map((c) => (
            <span key={c || "all"} className={`chip ${category === c ? "active" : ""}`} onClick={() => setCategory(c)}>
              {c || "All"}{data && c && data.tallies[c] ? ` · ${data.tallies[c]}` : ""}
            </span>
          ))}
        </div>
        <div className="chips" style={{ marginBottom: 10 }}>
          <span className="faint" style={{ fontSize: 11, alignSelf: "center", marginRight: 2 }}>Severity</span>
          {SEVERITIES.map((s) => (
            <span key={s || "all"} className={`chip ${severity === s ? "active" : ""}`} onClick={() => setSeverity(s)}>
              {s || "All"}
            </span>
          ))}
        </div>
        <div className="search-bar">
          <Icon name="search" />
          <input
            value={actor}
            placeholder="Filter by actor (user id, agent, sync-worker…)"
            onChange={(e) => setActor(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && load()}
          />
          <button className="btn primary sm" onClick={load}>Filter</button>
        </div>
      </Card>

      <Card>
        <table className="table">
          <thead>
            <tr><th>Time</th><th>Severity</th><th>Category</th><th>Actor</th><th>Action</th><th>Resource</th><th>Detail</th></tr>
          </thead>
          <tbody>
            {data?.events.map((e, i) => (
              <tr key={i}>
                <td className="faint" style={{ whiteSpace: "nowrap" }}>{timeAgo(e.created_at)}</td>
                <td><Pill tone={SEV_TONE[e.severity] ?? "info"}>{e.severity}</Pill></td>
                <td className="faint">{e.category}</td>
                <td className="mono" style={{ fontSize: 12 }}>{e.actor}</td>
                <td><Pill tone="info">{e.action}</Pill></td>
                <td className="mono faint">{(e.resource || "").slice(0, 12)}</td>
                <td className="faint" style={{ fontSize: 12, maxWidth: 280, overflow: "hidden", textOverflow: "ellipsis" }}>
                  {Object.keys(e.detail || {}).length ? JSON.stringify(e.detail) : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {data && data.events.length === 0 && <div className="muted">No matching audit events.</div>}
      </Card>
    </>
  );
}
