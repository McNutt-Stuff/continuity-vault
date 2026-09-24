import { Fragment, useCallback, useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api } from "../api";
import { Card, Pill, Loading } from "../components/ui";
import { Icon } from "../components/Icon";
import { SourceIcon } from "../components/SourceIcon";
import { notify } from "../components/dialog";

interface Evidence { capability: string; provider: string; status: string; summary: string; evidence_level?: string; stale?: boolean; }
interface Coverage { expected: number; covered: number; failed: number; unknown: number; evidence_levels?: string[]; }
interface Control {
  id: string; control_id: string; title: string; family: string; state: string; score: number;
  owner?: string | null; auto: boolean; guidance: string; capabilities: string[];
  last_evaluated_at?: string | null; evidence: Evidence[]; coverage?: Coverage;
  exception?: { reason: string; approved_by?: string; expires_at?: string | null } | null;
}
interface Driver {
  provider: string; signals: number; met: number; partial: number; unmet: number; unknown: number;
  capabilities: string[];
}
interface Issue { control_id: string; title: string; family: string; state: string; capabilities: string[]; reasons: string[]; }
interface Entity { kind: string; label: string; status: string; note: string; capability: string; provider: string; control_id: string; }
interface Detail {
  framework: string; label: string; version: string; authority?: string; url?: string;
  description?: string; enabled: boolean; score: number | null; met: number | null; total: number | null;
  delta: number; last_assessed_at?: string | null;
  trend: { score: number; met: number; total: number; at: string }[];
  controls: Control[]; drivers: Driver[]; open_issues: Issue[]; entities: Entity[];
  events: { kind: string; framework: string; control_id: string; actor: string; summary: string; at: string }[];
}

const STATES = ["not_assessed", "planned", "partially_implemented", "implemented", "operating", "not_applicable", "failed"];
const evTone = (s: string): "ok" | "warn" | "danger" | "info" =>
  s === "met" ? "ok" : s === "partial" ? "info" : s === "unmet" ? "danger" : "warn";
const prettyProvider = (p: string) => p === "arkive" ? "Arkive core"
  : p === "microsoft365" ? "Microsoft 365"
  : p === "integration_signals" ? "Integration signals" : p;

// Evidence provenance labels (spec: configuration vs observed vs verified_test vs manual).
const LEVEL_LABEL: Record<string, string> = {
  observed: "Observed", verified_test: "Verified", configuration: "Configured", manual: "Attested",
};

// Scoped coverage num/den with a mini bar — the honest "how much is actually covered".
function CoverageBar({ c }: { c?: Coverage }) {
  if (!c || !c.expected) return <span className="faint" style={{ fontSize: 11.5 }}>—</span>;
  const pct = Math.round((c.covered / c.expected) * 100);
  const color = pct >= 98 ? "var(--ok,#35d0a5)" : pct >= 50 ? "var(--warn,#f5a623)" : "var(--danger,#e5484d)";
  return (
    <div style={{ minWidth: 96 }}>
      <div style={{ fontSize: 12, fontWeight: 600 }}>{c.covered}/{c.expected}
        {c.failed ? <span style={{ color: "var(--danger,#e5484d)", fontWeight: 400 }}> · {c.failed} failed</span> : null}</div>
      <div style={{ height: 4, borderRadius: 3, background: "var(--inset)", marginTop: 3, overflow: "hidden" }}>
        <div style={{ width: `${pct}%`, height: "100%", background: color }} />
      </div>
    </div>
  );
}

function ScoreRing({ score, size = 76 }: { score: number | null; size?: number }) {
  const v = score ?? 0;
  const color = v >= 80 ? "var(--ok,#35d0a5)" : v >= 50 ? "var(--warn,#f5a623)" : "var(--danger,#e5484d)";
  return (
    <div style={{ width: size, height: size, borderRadius: "50%",
         background: `conic-gradient(${color} ${v * 3.6}deg, var(--inset) 0)`,
         display: "grid", placeItems: "center", flexShrink: 0 }}>
      <div style={{ width: size - 14, height: size - 14, borderRadius: "50%", background: "var(--bg)",
           display: "grid", placeItems: "center", fontWeight: 700, fontSize: size / 4 }}>
        {score == null ? "—" : `${v}%`}
      </div>
    </div>
  );
}

// Adherence-over-time line chart (score 0–100) rendered as a simple SVG.
function TrendChart({ points }: { points: { score: number; at: string }[] }) {
  const W = 640, H = 140, pad = 24;
  if (points.length === 0) return <div className="faint" style={{ fontSize: 12 }}>No history yet — re-assess to start the trend.</div>;
  const n = points.length;
  const x = (i: number) => n <= 1 ? pad : pad + (i * (W - 2 * pad)) / (n - 1);
  const y = (s: number) => H - pad - (s / 100) * (H - 2 * pad);
  const line = points.map((p, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(p.score).toFixed(1)}`).join(" ");
  const area = `${line} L${x(n - 1).toFixed(1)},${H - pad} L${x(0).toFixed(1)},${H - pad} Z`;
  return (
    <svg viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", maxWidth: W, height: "auto" }}>
      {[0, 50, 100].map((g) => (
        <g key={g}>
          <line x1={pad} x2={W - pad} y1={y(g)} y2={y(g)} stroke="var(--border-soft)" strokeWidth={1} />
          <text x={2} y={y(g) + 3} fontSize={9} fill="var(--faint,#889)">{g}</text>
        </g>
      ))}
      <path d={area} fill="var(--accent,#4f7cff)" opacity={0.12} />
      <path d={line} fill="none" stroke="var(--accent,#4f7cff)" strokeWidth={2} />
      {points.map((p, i) => (
        <circle key={i} cx={x(i)} cy={y(p.score)} r={2.5} fill="var(--accent,#4f7cff)">
          <title>{`${p.score}% · ${new Date(p.at + "Z").toLocaleString()}`}</title>
        </circle>
      ))}
    </svg>
  );
}

export default function ComplianceFramework() {
  const { framework = "" } = useParams();
  const navigate = useNavigate();
  const [detail, setDetail] = useState<Detail | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setDetail(await api.get<Detail>(`/compliance/framework/${framework}`));
    } catch (e) { notify({ message: (e as { message?: string }).message || "Couldn't load framework", tone: "danger" }); }
    finally { setLoaded(true); }
  }, [framework]);
  useEffect(() => { void load(); }, [load]);

  async function reassess() {
    setBusy(true);
    try { await api.post("/compliance/evaluate", {}); await load(); }
    catch (e) { notify({ message: (e as { message?: string }).message || "Couldn't re-assess", tone: "danger" }); }
    finally { setBusy(false); }
  }
  async function setState(c: Control, state: string) {
    try { await api.patch(`/compliance/controls/${c.id}`, { state }); await load(); }
    catch (e) { notify({ message: (e as { message?: string }).message || "Couldn't update", tone: "danger" }); }
  }
  async function addException(c: Control) {
    const reason = window.prompt(`Reason for the exception on ${c.control_id}:`);
    if (!reason) return;
    try { await api.post(`/compliance/controls/${c.id}/exception`, { reason }); await load(); }
    catch (e) { notify({ message: (e as { message?: string }).message || "Couldn't record exception", tone: "danger" }); }
  }

  if (!loaded) return <Loading label="Loading framework…" />;
  if (!detail) return <Card><div className="faint">Framework not found.</div></Card>;

  return (
    <>
      <button className="btn ghost sm" style={{ marginBottom: 12 }} onClick={() => navigate("/compliance")}>
        ← All frameworks
      </button>

      <Card style={{ marginBottom: 16 }}>
        <div className="spread" style={{ alignItems: "center", flexWrap: "wrap", gap: 12 }}>
          <div className="row" style={{ gap: 16, alignItems: "center" }}>
            <ScoreRing score={detail.score} />
            <div>
              <h2 style={{ margin: 0 }}>{detail.label}</h2>
              <div className="faint" style={{ fontSize: 12.5, maxWidth: 620 }}>{detail.description}</div>
              <div className="row" style={{ gap: 10, marginTop: 6, fontSize: 12 }}>
                {detail.total != null && <span className="faint">{detail.met ?? 0}/{detail.total} controls met</span>}
                {detail.delta !== 0 && (
                  <span style={{ color: detail.delta > 0 ? "var(--ok)" : "var(--danger)" }}>
                    {detail.delta > 0 ? "▲" : "▼"} {Math.abs(detail.delta)}%
                  </span>
                )}
                {detail.last_assessed_at && <span className="faint">Assessed {new Date(detail.last_assessed_at + "Z").toLocaleString()}</span>}
                {detail.url && <a className="faint" href={detail.url} target="_blank" rel="noreferrer">{detail.authority} reference ↗</a>}
              </div>
            </div>
          </div>
          <button className="btn sm primary" disabled={busy} onClick={reassess}>
            <Icon name="repeat" size={13} /> {busy ? "Assessing…" : "Re-assess"}
          </button>
        </div>
      </Card>

      {!detail.enabled ? (
        <Card><div className="faint">This framework isn't enabled yet. Enable it from the Compliance page to assess it.</div></Card>
      ) : (
        <>
          <Card style={{ marginBottom: 16 }}>
            <h3 style={{ marginTop: 0 }}>Adherence over time</h3>
            <TrendChart points={detail.trend.map((t) => ({ score: t.score, at: t.at }))} />
          </Card>

          <div style={{ display: "grid", gap: 16, gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))", marginBottom: 16 }}>
            <Card>
              <h3 style={{ marginTop: 0 }}>Drivers</h3>
              <div className="faint" style={{ fontSize: 12, marginBottom: 8 }}>What's feeding this framework's evidence.</div>
              <div className="stack" style={{ gap: 8 }}>
                {detail.drivers.length === 0 && <span className="faint" style={{ fontSize: 12 }}>No evidence yet — re-assess.</span>}
                {detail.drivers.map((d) => (
                  <div key={d.provider} className="row" style={{ gap: 8, alignItems: "center" }}>
                    <SourceIcon type={d.provider === "microsoft365" ? "microsoft365" : "shield"} fallback="shield" size={16} />
                    <span style={{ fontSize: 12.5, fontWeight: 600, minWidth: 120 }}>{prettyProvider(d.provider)}</span>
                    <div className="row" style={{ gap: 4 }}>
                      {d.met > 0 && <Pill tone="ok">{d.met} met</Pill>}
                      {d.partial > 0 && <Pill tone="info">{d.partial} partial</Pill>}
                      {d.unmet > 0 && <Pill tone="danger">{d.unmet} unmet</Pill>}
                      {d.unknown > 0 && <Pill tone="warn">{d.unknown} unknown</Pill>}
                    </div>
                  </div>
                ))}
              </div>
            </Card>

            <Card>
              <h3 style={{ marginTop: 0 }}>Open issues {detail.open_issues.length > 0 && <span className="faint" style={{ fontSize: 12 }}>({detail.open_issues.length})</span>}</h3>
              {detail.open_issues.length === 0 ? (
                <div className="faint" style={{ fontSize: 12.5 }}>No open issues — every assessed control is met.</div>
              ) : (
                <div className="stack" style={{ gap: 8 }}>
                  {detail.open_issues.map((iss) => (
                    <div key={iss.control_id} style={{ borderBottom: "1px solid var(--border-soft)", paddingBottom: 6 }}>
                      <div className="row" style={{ gap: 6, alignItems: "center" }}>
                        <Pill tone={iss.state === "failed" ? "danger" : "warn"}>{iss.state.replace(/_/g, " ")}</Pill>
                        <span style={{ fontSize: 12.5, fontWeight: 600 }}>{iss.control_id}</span>
                        <span className="faint" style={{ fontSize: 11.5 }}>{iss.title}</span>
                      </div>
                      {iss.reasons.slice(0, 4).map((r, i) => (
                        <div key={i} className="faint" style={{ fontSize: 11.5, marginLeft: 4 }}>• {r}</div>
                      ))}
                    </div>
                  ))}
                </div>
              )}
            </Card>
          </div>

          <Card style={{ marginBottom: 16 }}>
            <h3 style={{ marginTop: 0 }}>Troubling accounts & systems {detail.entities.length > 0 && <span className="faint" style={{ fontSize: 12 }}>({detail.entities.length})</span>}</h3>
            {detail.entities.length === 0 ? (
              <div className="faint" style={{ fontSize: 12.5 }}>Nothing flagged — no specific accounts or systems are dragging this framework down.</div>
            ) : (
              <div style={{ overflowX: "auto" }}>
                <table className="table">
                  <thead><tr><th>Item</th><th>Type</th><th>Issue</th><th>Capability</th></tr></thead>
                  <tbody>
                    {detail.entities.map((e, i) => (
                      <tr key={i}>
                        <td>
                          <div className="row" style={{ gap: 6, alignItems: "center" }}>
                            <SourceIcon type={e.provider === "microsoft365" ? "microsoft365" : (e.kind === "account" ? "user" : "shield")} fallback="shield" size={14} />
                            <span style={{ fontSize: 12.5, fontWeight: 600 }}>{e.label}</span>
                          </div>
                        </td>
                        <td><span className="faint" style={{ fontSize: 12 }}>{e.kind}</span></td>
                        <td><Pill tone={evTone(e.status)}>{e.status}</Pill> <span style={{ fontSize: 12 }}>{e.note}</span></td>
                        <td><span className="faint" style={{ fontSize: 11.5 }}>{e.capability}</span></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>

          <Card style={{ marginBottom: 16 }}>
            <h3 style={{ marginTop: 0 }}>Controls</h3>
            <div style={{ overflowX: "auto" }}>
              <table className="table">
                <thead><tr><th>Control</th><th>State</th><th>Coverage</th><th>Evidence</th><th></th></tr></thead>
                <tbody>
                  {detail.controls.map((c) => (
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
                        <td><CoverageBar c={c.coverage} /></td>
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
                          <td colSpan={5} style={{ background: "var(--inset)" }}>
                            <div className="faint" style={{ fontSize: 11.5, marginBottom: 6 }}>{c.guidance}</div>
                            {c.evidence.length === 0 ? <span className="faint" style={{ fontSize: 12 }}>No evidence collected yet — re-assess.</span> : (
                              <div className="stack" style={{ gap: 4 }}>
                                {c.evidence.map((e, i) => (
                                  <div key={i} className="row" style={{ gap: 8, alignItems: "center", fontSize: 12 }}>
                                    <SourceIcon type={e.provider === "microsoft365" ? "microsoft365" : "shield"} fallback="shield" size={14} />
                                    <Pill tone={evTone(e.status)}>{e.status}</Pill>
                                    <span className="faint">{e.capability}</span>
                                    <span className="flex1">{e.summary}</span>
                                    {e.evidence_level && <Pill tone="info">{LEVEL_LABEL[e.evidence_level] || e.evidence_level}</Pill>}
                                    {e.stale && <Pill tone="warn">stale</Pill>}
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

          {detail.events.length > 0 && (
            <Card>
              <h3 style={{ marginTop: 0 }}>Change history</h3>
              <div className="stack" style={{ gap: 0 }}>
                {detail.events.map((ev, i) => (
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
      )}
    </>
  );
}
