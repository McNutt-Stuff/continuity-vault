import { Fragment, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { Card, Pill, Loading } from "../components/ui";
import { Icon } from "../components/Icon";
import { SourceIcon } from "../components/SourceIcon";
import { notify } from "../components/dialog";

interface Framework {
  framework: string; label: string; version: string; authority?: string; url?: string;
  description?: string; controls: number; enabled: boolean;
  score: number | null; met: number | null; total: number | null;
}
interface Report {
  frameworks: { framework: string; label: string; score: number; met: number; total: number; delta: number }[];
  score: number | null; controls_total: number; controls_met: number; exceptions: number;
}
interface Evidence { capability: string; provider: string; status: string; summary: string; }
interface Control {
  id: string; control_id: string; title: string; family: string; state: string; score: number;
  owner?: string | null; auto: boolean; guidance: string; capabilities: string[];
  last_evaluated_at?: string | null; evidence: Evidence[];
  exception?: { reason: string; approved_by?: string; expires_at?: string | null } | null;
}
interface History {
  snapshots: { framework: string; score: number; met: number; total: number; at: string }[];
  events: { kind: string; framework: string; control_id: string; actor: string; summary: string; at: string }[];
}

const STATES = ["not_assessed", "planned", "partially_implemented", "implemented", "operating", "not_applicable", "failed"];
const stateTone = (s: string): "ok" | "warn" | "danger" | "info" =>
  s === "operating" || s === "implemented" ? "ok"
    : s === "failed" ? "danger" : s === "exception" ? "warn"
    : s === "partially_implemented" ? "info" : "warn";
const evTone = (s: string): "ok" | "warn" | "danger" | "info" =>
  s === "met" ? "ok" : s === "partial" ? "info" : s === "unmet" ? "danger" : "warn";

function ScoreRing({ score, size = 64 }: { score: number | null; size?: number }) {
  const v = score ?? 0;
  const color = v >= 80 ? "var(--ok,#35d0a5)" : v >= 50 ? "var(--warn,#f5a623)" : "var(--danger,#e5484d)";
  return (
    <div style={{ width: size, height: size, borderRadius: "50%",
         background: `conic-gradient(${color} ${v * 3.6}deg, var(--inset) 0)`,
         display: "grid", placeItems: "center", flexShrink: 0 }}>
      <div style={{ width: size - 12, height: size - 12, borderRadius: "50%", background: "var(--bg)",
           display: "grid", placeItems: "center", fontWeight: 700, fontSize: size / 4.2 }}>
        {score == null ? "—" : `${v}%`}
      </div>
    </div>
  );
}

export default function Compliance() {
  const navigate = useNavigate();
  const [frameworks, setFrameworks] = useState<Framework[]>([]);
  const [report, setReport] = useState<Report | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const [controls, setControls] = useState<Control[]>([]);
  const [history, setHistory] = useState<History | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);

  async function load() {
    try {
      const f = await api.get<{ frameworks: Framework[] }>("/compliance/frameworks");
      setFrameworks(f.frameworks || []);
      setReport(await api.get<Report>("/compliance/report"));
    } catch (e) { notify({ message: (e as { message?: string }).message || "Couldn't load compliance", tone: "danger" }); }
    finally { setLoaded(true); }
  }
  useEffect(() => { void load(); }, []);

  async function toggle(fw: string, enabled: boolean) {
    setBusy(fw);
    try { await api.post(`/compliance/frameworks/${fw}`, { enabled }); await load(); if (!enabled && open === fw) setOpen(null); }
    catch (e) { notify({ message: (e as { message?: string }).message || "Couldn't update", tone: "danger" }); }
    finally { setBusy(""); }
  }
  async function reassess() {
    setBusy("evaluate");
    try { await api.post("/compliance/evaluate", {}); await load(); if (open) await openFramework(open, true); }
    catch (e) { notify({ message: (e as { message?: string }).message || "Couldn't re-assess", tone: "danger" }); }
    finally { setBusy(""); }
  }
  async function openFramework(fw: string, keep = false) {
    if (!keep && open === fw) { setOpen(null); return; }
    setOpen(fw);
    try {
      const r = await api.get<{ controls: Control[] }>(`/compliance/controls?framework=${fw}`);
      setControls(r.controls || []);
      setHistory(await api.get<History>(`/compliance/history?framework=${fw}`));
    } catch { setControls([]); }
  }
  async function setState(c: Control, state: string) {
    try { await api.patch(`/compliance/controls/${c.id}`, { state }); setControls((cs) => cs.map((x) => x.id === c.id ? { ...x, state, auto: false } : x)); await load(); }
    catch (e) { notify({ message: (e as { message?: string }).message || "Couldn't update", tone: "danger" }); }
  }
  async function addException(c: Control) {
    const reason = window.prompt(`Reason for the exception on ${c.control_id}:`);
    if (!reason) return;
    try { await api.post(`/compliance/controls/${c.id}/exception`, { reason }); await openFramework(open!, true); await load(); }
    catch (e) { notify({ message: (e as { message?: string }).message || "Couldn't record exception", tone: "danger" }); }
  }

  if (!loaded) return <Loading label="Loading compliance…" />;

  const openSpec = frameworks.find((f) => f.framework === open);
  const trend = history?.snapshots?.filter((s) => s.framework === open) || [];

  return (
    <>
      <Card style={{ marginBottom: 16 }}>
        <div className="spread" style={{ alignItems: "center" }}>
          <div className="row" style={{ gap: 16, alignItems: "center" }}>
            <ScoreRing score={report?.score ?? null} />
            <div>
              <h2 style={{ margin: 0 }}>Compliance posture</h2>
              <div className="faint" style={{ fontSize: 12.5 }}>
                Framework scores computed from live evidence Arkive and your integrations provide —
                backup coverage, encryption, recovery, access governance, audit and retention.
                {report ? ` ${report.controls_met}/${report.controls_total} controls met · ${report.exceptions} exception(s).` : ""}
              </div>
            </div>
          </div>
          <button className="btn sm primary" disabled={busy === "evaluate"} onClick={reassess}>
            <Icon name="repeat" size={13} /> {busy === "evaluate" ? "Assessing…" : "Re-assess"}
          </button>
        </div>
      </Card>

      <Card style={{ marginBottom: 16 }}>
        <h3 style={{ marginTop: 0 }}>Frameworks</h3>
        <div className="faint" style={{ fontSize: 12, marginBottom: 12 }}>
          Enable the frameworks your organization is measured against. Arkive scores the data-protection,
          backup, recovery, access-governance and audit controls of each.
        </div>
        <div style={{ display: "grid", gap: 12, gridTemplateColumns: "repeat(auto-fill, minmax(240px, 1fr))",
             alignItems: "stretch" }}>
          {frameworks.map((f) => (
            <div key={f.framework} className="card" style={{ padding: "12px 14px", display: "flex", flexDirection: "column",
                 borderColor: f.enabled ? "var(--accent,#4f7cff)" : undefined }}>
              <div className="spread" style={{ alignItems: "flex-start" }}>
                <div>
                  <div style={{ fontSize: 14, fontWeight: 700 }}>{f.label}</div>
                  <div className="faint" style={{ fontSize: 11 }}>{f.authority} · {f.controls} controls</div>
                </div>
                <label><input type="checkbox" checked={f.enabled} disabled={busy === f.framework}
                              onChange={(e) => toggle(f.framework, e.target.checked)} /></label>
              </div>
              <div className="faint" style={{ fontSize: 11.5, marginTop: 6, minHeight: 32 }}>{f.description}</div>
              {f.enabled && (
                <div style={{ marginTop: 8 }}>
                  <div className="row" style={{ justifyContent: "space-between", fontSize: 12 }}>
                    <span className="faint">{f.met}/{f.total} met</span>
                    <span style={{ fontWeight: 700 }}>{f.score ?? "—"}{f.score != null ? "%" : ""}</span>
                  </div>
                  <div className="progress" style={{ marginTop: 4 }}><span style={{ width: `${f.score ?? 0}%` }} /></div>
                  <div className="row" style={{ gap: 6, marginTop: 8 }}>
                    <button className="btn ghost sm" onClick={() => openFramework(f.framework)}>
                      {open === f.framework ? "Hide controls" : "Quick view"}
                    </button>
                    <button className="btn sm" onClick={() => navigate(`/compliance/${f.framework}`)}>
                      Open dashboard →
                    </button>
                  </div>
                </div>
              )}
            </div>
          ))}
        </div>
      </Card>

      {open && openSpec && (
        <Card style={{ marginBottom: 16 }}>
          <div className="spread" style={{ alignItems: "center", marginBottom: 10 }}>
            <div>
              <h3 style={{ margin: 0 }}>{openSpec.label} — controls</h3>
              {openSpec.url && <a className="faint" style={{ fontSize: 11.5 }} href={openSpec.url} target="_blank" rel="noreferrer">{openSpec.authority} reference ↗</a>}
            </div>
            {trend.length > 1 && (
              <div className="row" style={{ gap: 3, alignItems: "flex-end", height: 34 }} title="Score trend">
                {trend.slice(-16).map((s, i) => (
                  <div key={i} style={{ width: 6, height: `${Math.max(4, s.score * 0.34)}px`,
                       background: "var(--accent,#4f7cff)", borderRadius: 2, opacity: 0.5 + i / 40 }}
                       title={`${s.score}% · ${new Date(s.at + "Z").toLocaleString()}`} />
                ))}
              </div>
            )}
          </div>
          <div style={{ overflowX: "auto" }}>
            <table className="table">
              <thead><tr><th>Control</th><th>State</th><th>Evidence</th><th></th></tr></thead>
              <tbody>
                {controls.map((c) => (
                  <Fragment key={c.id}>
                    <tr>
                      <td>
                        <div style={{ fontWeight: 600, fontSize: 12.5 }}>{c.control_id} <span className="faint" style={{ fontWeight: 400 }}>· {c.family}</span></div>
                        <div className="faint" style={{ fontSize: 11.5 }}>{c.title}</div>
                      </td>
                      <td>
                        {c.state === "exception"
                          ? <span title={c.exception?.reason || undefined}><Pill tone="warn">exception</Pill></span>
                          : <select className="input sm" value={c.state} onChange={(e) => setState(c, e.target.value)}>
                              {STATES.map((s) => <option key={s} value={s}>{s.replace(/_/g, " ")}</option>)}
                            </select>}
                        <span className="faint" style={{ fontSize: 10.5, marginLeft: 6 }}>{c.auto ? "auto" : "manual"}</span>
                      </td>
                      <td>
                        <button className="btn ghost sm" onClick={() => setExpanded(expanded === c.id ? null : c.id)}>
                          {c.evidence.length} signal(s) {expanded === c.id ? "▴" : "▾"}
                        </button>
                      </td>
                      <td style={{ textAlign: "right" }}>
                        {c.state !== "exception" && <button className="btn ghost sm" onClick={() => addException(c)}>Exception</button>}
                      </td>
                    </tr>
                    {expanded === c.id && (
                      <tr key={c.id + "-ev"}>
                        <td colSpan={4} style={{ background: "var(--inset)" }}>
                          <div className="faint" style={{ fontSize: 11.5, marginBottom: 6 }}>{c.guidance}</div>
                          {c.evidence.length === 0 ? <span className="faint" style={{ fontSize: 12 }}>No evidence collected yet — re-assess.</span> : (
                            <div className="stack" style={{ gap: 4 }}>
                              {c.evidence.map((e, i) => (
                                <div key={i} className="row" style={{ gap: 8, alignItems: "center", fontSize: 12 }}>
                                  <SourceIcon type={e.provider === "microsoft365" ? "microsoft365" : "shield"} fallback="shield" size={14} />
                                  <Pill tone={evTone(e.status)}>{e.status}</Pill>
                                  <span className="faint">{e.capability}</span>
                                  <span>{e.summary}</span>
                                </div>
                              ))}
                            </div>
                          )}
                        </td>
                      </tr>
                    )}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      {open && history && history.events.length > 0 && (
        <Card>
          <h3 style={{ marginTop: 0 }}>Change history</h3>
          <div className="stack" style={{ gap: 0 }}>
            {history.events.map((ev, i) => (
              <div key={i} className="row" style={{ gap: 10, alignItems: "center", padding: "6px 0", borderBottom: "1px solid var(--border-soft)" }}>
                <Pill tone="info">{ev.kind.replace(/_/g, " ")}</Pill>
                <span className="flex1" style={{ fontSize: 12.5 }}>{ev.summary}</span>
                <span className="faint" style={{ fontSize: 11 }}>{ev.actor} · {new Date(ev.at + "Z").toLocaleString()}</span>
              </div>
            ))}
          </div>
        </Card>
      )}
    </>
  );
}
