import { useEffect, useState } from "react";
import { useLocation } from "react-router-dom";
import { api } from "../api";
import { Card, Pill, timeAgo, Loading } from "../components/ui";
import { Icon, IconName } from "../components/Icon";
import { humanizeAction, prettyKey, formatValue } from "../components/format";

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
  info: "info", notice: "ok", warning: "warn", error: "danger", critical: "danger",
};
const CAT_ICON: Record<string, IconName> = {
  security: "shield", credential: "key", admin: "server", system: "database", activity: "user",
};
const CATEGORIES = ["", "activity", "security", "credential", "admin", "system"];
const SEVERITIES = ["", "info", "notice", "warning", "critical"];

export default function Audit() {
  const loc = useLocation();
  const [data, setData] = useState<AuditResp | null>(null);
  const [category, setCategory] = useState("");
  const [severity, setSeverity] = useState("");
  const [actor, setActor] = useState("");
  const [abnormal, setAbnormal] = useState(false);
  const [open, setOpen] = useState<number | null>(null);
  const [loaded, setLoaded] = useState(false);

  // Deep-link support: /audit?abnormal=1 or ?severity=critical from the alert bell.
  useEffect(() => {
    const p = new URLSearchParams(loc.search);
    if (p.get("abnormal")) setAbnormal(true);
    if (p.get("severity")) setSeverity(p.get("severity")!);
  }, [loc.search]);

  async function load() {
    const p = new URLSearchParams();
    if (category) p.set("category", category);
    if (severity) p.set("severity", severity);
    if (actor) p.set("actor", actor);
    try { setData(await api.get<AuditResp>(`/audit?${p.toString()}`)); }
    finally { setLoaded(true); }
  }
  useEffect(() => { void load(); }, [category, severity]);

  const rows = (data?.events ?? []).filter((e) =>
    !abnormal || ["warning", "error", "critical"].includes(e.severity));

  if (!loaded && !data) return <Loading label="Loading audit log…" />;

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
          <span className={`chip ${abnormal ? "active" : ""}`} onClick={() => setAbnormal((v) => !v)}>
            <Icon name="alert" size={12} /> Abnormal only
          </span>
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
        {rows.length === 0 && <div className="muted">No matching audit events.</div>}
        {rows.map((e, i) => {
          const detailKeys = Object.keys(e.detail || {});
          const expanded = open === i;
          return (
            <div key={i} className={`audit-row ${e.severity === "critical" ? "crit" : e.severity === "warning" ? "warn" : ""}`}>
              <div className="audit-main" onClick={() => setOpen(expanded ? null : i)}>
                <span className={`audit-sev ${e.severity}`}>
                  <Icon name={CAT_ICON[e.category] ?? "user"} size={14} />
                </span>
                <div className="flex1">
                  <div className="row" style={{ gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                    <span style={{ fontWeight: 600 }}>{humanizeAction(e.action)}</span>
                    <Pill tone={SEV_TONE[e.severity] ?? "info"}>{e.severity}</Pill>
                    <span className="faint" style={{ fontSize: 11 }}>{e.category}</span>
                  </div>
                  <div className="faint" style={{ fontSize: 12, marginTop: 2 }}>
                    <span className="mono">{e.actor}</span>
                    {e.resource && <> · <span className="mono">{e.resource.slice(0, 16)}</span></>}
                    {detailKeys.length > 0 && !expanded && <> · {detailKeys.length} detail field{detailKeys.length === 1 ? "" : "s"}</>}
                  </div>
                </div>
                <span className="faint" style={{ fontSize: 11, whiteSpace: "nowrap" }}>{timeAgo(e.created_at)}</span>
                {detailKeys.length > 0 && (
                  <span className="faint" style={{ fontSize: 11 }}>{expanded ? "▲" : "▼"}</span>
                )}
              </div>
              {expanded && (
                <div className="audit-detail">
                  {detailKeys.map((k) => (
                    <div key={k} className="audit-kv">
                      <span className="faint">{prettyKey(k)}</span>
                      <span className="mono">{formatValue(e.detail[k])}</span>
                    </div>
                  ))}
                  <div className="audit-kv">
                    <span className="faint">Chain hash</span>
                    <span className="mono">{e.entry_hash}…</span>
                  </div>
                </div>
              )}
            </div>
          );
        })}
      </Card>
    </>
  );
}
