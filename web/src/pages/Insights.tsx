import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { Card, bytes, Loading } from "../components/ui";
import { Icon, IconName } from "../components/Icon";

interface TimelineSeries { key: string; label: string; icon: string; color: string; values: number[]; }
interface Timeline {
  granularity: "month" | "year";
  points: string[];
  series: TimelineSeries[];
  bytes: number[];
  cumulative: number[];
  total_objects: number;
  total_bytes: number;
  span_start?: string;
  span_end?: string;
}
interface CardDetail { label: string; value: string; }
interface InsightCard {
  id: string;
  icon: string;
  tone: "info" | "warn" | "ok";
  title: string;
  headline: string;
  body: string;
  detail?: CardDetail[];
  action?: { label: string; to: string };
}
interface InsightsResp {
  status: "ready" | "insufficient_data";
  generated_at: string | null;
  stats: { object_count?: number; total_bytes?: number; source_count?: number; category_count?: number };
  timeline: Timeline;
  cards: InsightCard[];
}

const TONE_COLOR: Record<string, string> = {
  info: "#4f7cff", warn: "#f5a623", ok: "#2dbe60",
};
const asIcon = (n: string): IconName => (n || "database") as IconName;

export default function Insights() {
  const [data, setData] = useState<InsightsResp | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);

  async function load() {
    try { setData(await api.get<InsightsResp>("/insights")); }
    catch { setData(null); }
    finally { setLoading(false); }
  }
  useEffect(() => { void load(); }, []);

  async function refresh() {
    setRefreshing(true);
    try { setData(await api.post<InsightsResp>("/insights/refresh", {})); }
    catch { /* ignore */ }
    finally { setRefreshing(false); }
  }

  if (loading) return <Loading label="Analyzing your digital footprint…" />;

  const tl = data?.timeline;
  const hasTimeline = !!tl && (tl.points?.length || 0) > 0;
  const gen = data?.generated_at ? new Date(data.generated_at.endsWith("Z") ? data.generated_at : `${data.generated_at}Z`) : null;

  return (
    <>
      <div className="spread" style={{ marginBottom: 18 }}>
        <div className="stack">
          <h2 style={{ margin: 0 }}>Your digital footprint</h2>
          <div className="faint" style={{ fontSize: 12.5 }}>
            {gen ? `Refreshed ${gen.toLocaleString()}` : "A living view of everything Arkive protects for you"}
          </div>
        </div>
        <button className="btn ghost sm" disabled={refreshing} onClick={() => void refresh()}>
          {refreshing ? <><span className="spinner-dot" /> Refreshing…</> : <><Icon name="repeat" size={14} /> Refresh</>}
        </button>
      </div>

      {data && (
        <div className="insights-stats" style={{ marginBottom: 16 }}>
          <StatChip icon="database" label="Objects protected" value={(data.stats.object_count || 0).toLocaleString()} tint="#4f7cff" />
          <StatChip icon="cloud" label="Total volume" value={bytes(data.stats.total_bytes || 0)} tint="#2dbe60" />
          <StatChip icon="link" label="Sources woven in" value={String(data.stats.source_count || 0)} tint="#c56cf0" />
          <StatChip icon="clock" label="History spans"
                    value={spanLabel(tl?.span_start, tl?.span_end)} tint="#f5a623" />
        </div>
      )}

      <Card className="insights-hero" style={{ marginBottom: 22, padding: 0, overflow: "hidden" }}>
        <div style={{ padding: "16px 18px 6px" }}>
          <div className="row" style={{ gap: 8, alignItems: "center" }}>
            <Icon name="insights" size={16} />
            <h3 style={{ margin: 0, fontSize: 15 }}>Footprint over time</h3>
          </div>
          <div className="faint" style={{ fontSize: 12 }}>
            How your protected life has grown — woven by source and volume.
          </div>
        </div>
        {hasTimeline
          ? <FootprintTimeline tl={tl!} />
          : <div className="muted" style={{ padding: "40px 18px 48px", textAlign: "center" }}>
              As Arkive protects more of your data, your footprint timeline will appear here.
            </div>}
      </Card>

      <div className="spread" style={{ marginBottom: 10 }}>
        <h3 style={{ margin: 0, fontSize: 16 }}>What we found</h3>
        {data && <span className="faint" style={{ fontSize: 12 }}>
          {data.cards.length} insight{data.cards.length === 1 ? "" : "s"} for you
        </span>}
      </div>

      {data && data.cards.length > 0 ? (
        <div className="insights-cards">
          {data.cards.map((c) => <InsightCardView key={c.id} card={c} />)}
        </div>
      ) : (
        <Card>
          <div className="row" style={{ gap: 12, alignItems: "center", padding: "8px 4px" }}>
            <div className="result-icon" style={{ background: "var(--inset)" }}>
              <Icon name="sparkle" size={18} />
            </div>
            <div className="flex1">
              <div style={{ fontWeight: 600 }}>Your insights are still taking shape</div>
              <div className="faint" style={{ fontSize: 12.5 }}>
                Once you've protected a bit more of your digital life, we'll surface tailored findings
                about your footprint here. Connect more sources to unlock them faster.
              </div>
            </div>
          </div>
        </Card>
      )}
    </>
  );
}

function spanLabel(start?: string, end?: string): string {
  if (!start || !end) return "—";
  const s = new Date(start), e = new Date(end);
  if (isNaN(s.getTime()) || isNaN(e.getTime())) return "—";
  const yrs = (e.getTime() - s.getTime()) / (365.25 * 24 * 3600 * 1000);
  if (yrs >= 1.5) return `${Math.round(yrs)} years`;
  const months = Math.max(1, Math.round(yrs * 12));
  return `${months} month${months === 1 ? "" : "s"}`;
}

function StatChip({ icon, label, value, tint }: { icon: IconName; label: string; value: string; tint: string }) {
  return (
    <div className="insights-stat">
      <div className="insights-stat-ic" style={{ background: `${tint}22`, color: tint }}>
        <Icon name={icon} size={16} />
      </div>
      <div className="stack" style={{ gap: 1 }}>
        <div style={{ fontSize: 17, fontWeight: 700 }}>{value}</div>
        <div className="faint" style={{ fontSize: 11.5 }}>{label}</div>
      </div>
    </div>
  );
}

// --- The hero visualization: a stacked-area footprint woven by source, with a
// cumulative growth curve overlaid and an interactive per-period breakdown. ----
function FootprintTimeline({ tl }: { tl: Timeline }) {
  const n = tl.points.length;
  const [hover, setHover] = useState<number | null>(null);
  const wrapRef = useRef<HTMLDivElement | null>(null);

  const VBW = 1000, VBH = 340;
  const padL = 44, padR = 44, padT = 18, padB = 46;
  const plotW = VBW - padL - padR, plotH = VBH - padT - padB;

  const geom = useMemo(() => {
    const stackTotals = tl.points.map((_, i) => tl.series.reduce((s, sv) => s + (sv.values[i] || 0), 0));
    const maxStack = Math.max(1, ...stackTotals);
    const maxCum = Math.max(1, ...(tl.cumulative.length ? tl.cumulative : [1]));
    const x = (i: number) => n <= 1 ? padL + plotW / 2 : padL + (i * plotW) / (n - 1);
    const yStack = (v: number) => padT + plotH * (1 - v / maxStack);
    const yCum = (v: number) => padT + plotH * (1 - v / maxCum);

    // Build stacked bands bottom→top.
    const bands: { color: string; key: string; d: string }[] = [];
    const bottoms = tl.points.map(() => 0);
    for (const sv of tl.series) {
      const tops = tl.points.map((_, i) => bottoms[i] + (sv.values[i] || 0));
      const topPts = tops.map((v, i) => `${x(i).toFixed(1)},${yStack(v).toFixed(1)}`);
      const botPts = bottoms.map((v, i) => `${x(i).toFixed(1)},${yStack(v).toFixed(1)}`).reverse();
      bands.push({ color: sv.color, key: sv.key, d: `M${topPts.join(" L")} L${botPts.join(" L")} Z` });
      for (let i = 0; i < n; i++) bottoms[i] = tops[i];
    }
    const cumLine = tl.cumulative.map((v, i) => `${x(i).toFixed(1)},${yCum(v).toFixed(1)}`).join(" ");
    return { x, yStack, yCum, bands, cumLine, stackTotals, maxStack, maxCum };
  }, [tl, n]);

  // Sparse x labels: first, last, and evenly-spaced middles.
  const labelIdx = useMemo(() => {
    if (n <= 6) return tl.points.map((_, i) => i);
    const want = 6, step = (n - 1) / (want - 1);
    return Array.from({ length: want }, (_, k) => Math.round(k * step));
  }, [n, tl.points]);

  const active = hover ?? n - 1;
  const activeBreak = tl.series
    .map((s) => ({ label: s.label, color: s.color, icon: s.icon, value: s.values[active] || 0 }))
    .filter((s) => s.value > 0)
    .sort((a, b) => b.value - a.value);

  function onMove(e: React.MouseEvent) {
    const el = wrapRef.current;
    if (!el || n === 0) return;
    const r = el.getBoundingClientRect();
    const rel = (e.clientX - r.left) / r.width;   // 0..1 across the element
    const px = rel * VBW;
    const i = n <= 1 ? 0 : Math.round(((px - padL) / plotW) * (n - 1));
    setHover(Math.max(0, Math.min(n - 1, i)));
  }

  return (
    <div>
      <div ref={wrapRef} style={{ position: "relative", width: "100%" }}
           onMouseMove={onMove} onMouseLeave={() => setHover(null)}>
        <svg viewBox={`0 0 ${VBW} ${VBH}`} width="100%" style={{ display: "block" }}>
          <defs>
            <linearGradient id="cumFill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#ffffff" stopOpacity="0.10" />
              <stop offset="100%" stopColor="#ffffff" stopOpacity="0" />
            </linearGradient>
          </defs>
          {/* horizontal gridlines */}
          {[0, 0.25, 0.5, 0.75, 1].map((f) => (
            <line key={f} x1={padL} x2={VBW - padR} y1={padT + plotH * f} y2={padT + plotH * f}
                  stroke="var(--border-soft,#22304a)" strokeWidth={1} opacity={0.5} />
          ))}
          {/* stacked source bands */}
          {geom.bands.map((b) => (
            <path key={b.key} d={b.d} fill={b.color} fillOpacity={0.82} stroke={b.color}
                  strokeOpacity={0.9} strokeWidth={0.6} />
          ))}
          {/* cumulative growth curve */}
          <polyline points={geom.cumLine} fill="none" stroke="#ffffff" strokeOpacity={0.85}
                    strokeWidth={2.2} strokeLinejoin="round" strokeLinecap="round" />
          {/* active period guide */}
          {n > 0 && (
            <line x1={geom.x(active)} x2={geom.x(active)} y1={padT} y2={padT + plotH}
                  stroke="#fff" strokeOpacity={0.55} strokeWidth={1} strokeDasharray="3 3" />
          )}
          {n > 0 && (
            <circle cx={geom.x(active)} cy={geom.yCum(tl.cumulative[active] || 0)} r={4}
                    fill="#fff" />
          )}
          {/* x labels */}
          {labelIdx.map((i) => (
            <text key={i} x={geom.x(i)} y={VBH - 16} textAnchor="middle"
                  fontSize={12} fill="var(--text-faint,#8a93a6)">{fmtPoint(tl.points[i], tl.granularity)}</text>
          ))}
        </svg>
      </div>

      {/* per-period breakdown + legend */}
      <div style={{ padding: "6px 18px 16px" }}>
        <div className="row" style={{ gap: 8, alignItems: "baseline", flexWrap: "wrap", marginBottom: 6 }}>
          <span style={{ fontWeight: 700, fontSize: 14 }}>{fmtPoint(tl.points[active], tl.granularity, true)}</span>
          <span className="faint" style={{ fontSize: 12 }}>
            {geom.stackTotals[active].toLocaleString()} new · {(tl.cumulative[active] || 0).toLocaleString()} total by then · {bytes(tl.bytes[active] || 0)} added
          </span>
        </div>
        <div className="row" style={{ gap: 14, flexWrap: "wrap" }}>
          {activeBreak.length === 0 && <span className="faint" style={{ fontSize: 12 }}>No new items in this period.</span>}
          {activeBreak.map((s) => (
            <div key={s.label} className="row" style={{ gap: 6, alignItems: "center" }}>
              <span style={{ width: 10, height: 10, borderRadius: 3, background: s.color, display: "inline-block" }} />
              <span style={{ fontSize: 12.5 }}>{s.label}</span>
              <span className="faint" style={{ fontSize: 12 }}>{s.value.toLocaleString()}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function fmtPoint(p: string, gran: "month" | "year", long = false): string {
  if (!p) return "";
  if (gran === "year") return p;
  const [y, m] = p.split("-");
  const d = new Date(Number(y), Number(m) - 1, 1);
  if (isNaN(d.getTime())) return p;
  return d.toLocaleDateString(undefined, long ? { month: "long", year: "numeric" } : { month: "short", year: "2-digit" });
}

function InsightCardView({ card }: { card: InsightCard }) {
  const nav = useNavigate();
  const tint = TONE_COLOR[card.tone] || "#4f7cff";
  return (
    <Card className="insight-card" style={{ borderTop: `3px solid ${tint}` }}>
      <div className="row" style={{ gap: 10, alignItems: "center", marginBottom: 8 }}>
        <div className="insight-card-ic" style={{ background: `${tint}1e`, color: tint }}>
          <Icon name={asIcon(card.icon)} size={18} />
        </div>
        <div className="flex1">
          <div className="faint" style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: 0.4 }}>{card.title}</div>
          <div style={{ fontWeight: 700, fontSize: 16, lineHeight: 1.2 }}>{card.headline}</div>
        </div>
      </div>
      <div style={{ fontSize: 13.5, lineHeight: 1.5, color: "var(--text-2,#c3ccdd)" }}>{card.body}</div>
      {card.detail && card.detail.length > 0 && (
        <div className="insight-card-detail">
          {card.detail.map((d, i) => (
            <div key={i} className="stack" style={{ gap: 1 }}>
              <div style={{ fontWeight: 700, fontSize: 13.5 }}>{d.value}</div>
              <div className="faint" style={{ fontSize: 10.5 }}>{d.label}</div>
            </div>
          ))}
        </div>
      )}
      {card.action && (
        <div style={{ marginTop: 12 }}>
          <button className="btn sm" style={{ borderColor: tint, color: tint }}
                  onClick={() => nav(card.action!.to)}>
            {card.action.label} <Icon name="link" size={13} />
          </button>
        </div>
      )}
    </Card>
  );
}
