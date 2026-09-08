import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { Card, bytes, Loading } from "../components/ui";
import { Icon, IconName } from "../components/Icon";
import { SourceIcon } from "../components/SourceIcon";

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
  status: "ready" | "insufficient_data" | "pending";
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

  // While a node-hosted report is being generated remotely, poll until it lands.
  useEffect(() => {
    if (data?.status !== "pending") return;
    const t = setTimeout(() => { void load(); }, 8000);
    return () => clearTimeout(t);
  }, [data]);

  async function refresh() {
    setRefreshing(true);
    try { setData(await api.post<InsightsResp>("/insights/refresh", {})); }
    catch { /* ignore */ }
    finally { setRefreshing(false); }
  }

  if (loading) return <Loading label="Analyzing your digital footprint…" />;

  if (data?.status === "pending") {
    return (
      <>
        <div className="spread" style={{ marginBottom: 18 }}>
          <div className="stack">
            <h2 style={{ margin: 0 }}>Your digital footprint</h2>
            <div className="faint" style={{ fontSize: 12.5 }}>A living view of everything Arkive protects for you</div>
          </div>
        </div>
        <Card className="insights-hero">
          <div className="row" style={{ gap: 14, alignItems: "center", padding: "20px 8px" }}>
            <span className="spinner" />
            <div className="flex1">
              <div style={{ fontWeight: 600, fontSize: 15 }}>Building your insights…</div>
              <div className="faint" style={{ fontSize: 12.5 }}>
                We're mining your protected data to surface your footprint timeline and tailored findings.
                This page will update automatically in a few moments.
              </div>
            </div>
          </div>
        </Card>
      </>
    );
  }

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
        {hasTimeline
          ? <FootprintTimeline tl={tl!} />
          : <>
              <div style={{ padding: "16px 18px 6px" }}>
                <div className="row" style={{ gap: 8, alignItems: "center" }}>
                  <Icon name="insights" size={16} />
                  <h3 style={{ margin: 0, fontSize: 15 }}>Footprint over time</h3>
                </div>
                <div className="faint" style={{ fontSize: 12 }}>
                  How your protected life has grown — woven by source and volume.
                </div>
              </div>
              <div className="muted" style={{ padding: "40px 18px 48px", textAlign: "center" }}>
                As Arkive protects more of your data, your footprint timeline will appear here.
              </div>
            </>}
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

// --- The hero visualization: a per-source bar chart across time. Each source
// gets its own bar per period, crowned with its brand icon; a smoothed trend
// line rides over the per-period totals. The chart scrolls horizontally and a
// time-range dropdown (top-right) reframes the window. -----------------------
type RangeOpt = { label: string; n: number };

function smoothPath(pts: [number, number][]): string {
  if (pts.length === 0) return "";
  if (pts.length === 1) return `M${pts[0][0].toFixed(1)},${pts[0][1].toFixed(1)}`;
  let d = `M${pts[0][0].toFixed(1)},${pts[0][1].toFixed(1)}`;
  for (let i = 0; i < pts.length - 1; i++) {
    const [x0, y0] = pts[i], [x1, y1] = pts[i + 1];
    const cx = (x0 + x1) / 2;
    d += ` C${cx.toFixed(1)},${y0.toFixed(1)} ${cx.toFixed(1)},${y1.toFixed(1)} ${x1.toFixed(1)},${y1.toFixed(1)}`;
  }
  return d;
}

function FootprintTimeline({ tl }: { tl: Timeline }) {
  const n = tl.points.length;

  const rangeOpts: RangeOpt[] = useMemo(() => {
    const base: RangeOpt[] = tl.granularity === "month"
      ? [{ label: "6 months", n: 6 }, { label: "1 year", n: 12 }, { label: "2 years", n: 24 }]
      : [{ label: "5 years", n: 5 }, { label: "10 years", n: 10 }];
    const opts = base.filter((o) => o.n < n);
    opts.push({ label: "All time", n: Infinity });
    return opts;
  }, [tl.granularity, n]);

  const [range, setRange] = useState<number>(() => rangeOpts[0]?.n ?? Infinity);
  const [hover, setHover] = useState<number | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  const count = range === Infinity ? n : Math.min(range, n);
  const start = Math.max(0, n - count);
  const vIdx = useMemo(() => Array.from({ length: count }, (_, k) => start + k), [count, start]);

  // Snap to the most recent period whenever the window changes.
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollLeft = el.scrollWidth;
  }, [range, n]);

  // Geometry.
  const numS = Math.max(1, tl.series.length);
  const BAR_W = 15, BAR_GAP = 4, GROUP_GAP = 26, PAD_L = 16, PAD_R = 22;
  const ICON_H = 22, BARS_H = 230, TOP_PAD = 16, AXIS_H = 36;
  const plotH = TOP_PAD + ICON_H + BARS_H;
  const groupInnerW = numS * BAR_W + Math.max(0, numS - 1) * BAR_GAP;
  const groupW = groupInnerW + GROUP_GAP;
  const totalW = PAD_L + PAD_R + count * groupW;

  const maxVal = Math.max(1, ...tl.series.flatMap((s) => vIdx.map((i) => s.values[i] || 0)));
  const totals = vIdx.map((i) => tl.series.reduce((a, s) => a + (s.values[i] || 0), 0));
  const maxTotal = Math.max(1, ...totals);

  const baseY = plotH;                              // bottom of the bars (svg coords)
  const groupCenter = (k: number) => PAD_L + k * groupW + groupW / 2;
  const trendY = (v: number) => baseY - (v / maxTotal) * BARS_H;
  const trendPath = useMemo(
    () => smoothPath(totals.map((v, k) => [groupCenter(k), trendY(v)] as [number, number])),
    [totals, groupW, maxTotal, count]);

  // Only thin out labels when groups are too narrow to fit them.
  const labelEvery = groupW < 46 ? Math.ceil(46 / groupW) : 1;

  const active = hover ?? (n - 1);
  const activeTotal = tl.series.reduce((a, s) => a + (s.values[active] || 0), 0);
  const activeBreak = tl.series
    .map((s) => ({ label: s.label, color: s.color, key: s.key, icon: s.icon, value: s.values[active] || 0 }))
    .filter((s) => s.value > 0)
    .sort((a, b) => b.value - a.value);

  return (
    <div>
      <div className="fp-head">
        <div className="stack" style={{ gap: 2 }}>
          <div className="row" style={{ gap: 8, alignItems: "center" }}>
            <Icon name="insights" size={16} />
            <h3 style={{ margin: 0, fontSize: 15 }}>Footprint over time</h3>
          </div>
          <div className="faint" style={{ fontSize: 12 }}>
            Objects protected each period, by source — with the overall trend.
          </div>
        </div>
        <select className="fp-range" value={String(range)}
                onChange={(e) => setRange(Number(e.target.value))} aria-label="Time range">
          {rangeOpts.map((o) => (
            <option key={o.label} value={String(o.n)}>{o.label}</option>
          ))}
        </select>
      </div>

      <div className="fp-scroll" ref={scrollRef} onMouseLeave={() => setHover(null)}>
        <div className="fp-track" style={{ width: totalW, height: plotH + AXIS_H, position: "relative" }}>
          {/* gridlines (behind bars) */}
          <svg className="fp-layer" width={totalW} height={plotH}
               viewBox={`0 0 ${totalW} ${plotH}`} style={{ zIndex: 0 }}>
            {[0, 0.25, 0.5, 0.75, 1].map((f) => (
              <line key={f} x1={PAD_L} x2={totalW - PAD_R} y1={baseY - BARS_H * f} y2={baseY - BARS_H * f}
                    stroke="var(--border-soft,#22304a)" strokeWidth={1} opacity={0.4} />
            ))}
          </svg>

          {/* per-source bars + icons + labels */}
          <div className="fp-groups" style={{ paddingLeft: PAD_L, paddingRight: PAD_R, position: "relative", zIndex: 1 }}>
            {vIdx.map((gi, k) => {
              const showLabel = k % labelEvery === 0 || k === count - 1;
              return (
                <div key={gi} className="fp-group"
                     style={{ width: groupW, opacity: hover == null || hover === gi ? 1 : 0.4 }}
                     onMouseEnter={() => setHover(gi)}>
                  <div className="fp-group-plot" style={{ height: plotH, gap: BAR_GAP }}>
                    {tl.series.map((s) => {
                      const v = s.values[gi] || 0;
                      const h = v > 0 ? Math.max(3, (v / maxVal) * BARS_H) : 0;
                      return (
                        <div key={s.key} className="fp-col" style={{ width: BAR_W }}
                             title={`${s.label}: ${v.toLocaleString()}`}>
                          {v > 0 && (
                            <SourceIcon type={s.key} fallback={asIcon(s.icon)} size={15}
                                        style={{ marginBottom: 3, opacity: 0.95 }} />
                          )}
                          <div className="fp-bar" style={{
                            height: h, width: BAR_W, background: s.color,
                            boxShadow: gi === active && v > 0 ? "0 0 0 1.5px rgba(255,255,255,.5)" : undefined,
                          }} />
                        </div>
                      );
                    })}
                  </div>
                  <div className="fp-xlabel" style={{ height: AXIS_H, width: groupW }}>
                    {showLabel ? fmtPoint(tl.points[gi], tl.granularity) : ""}
                  </div>
                </div>
              );
            })}
          </div>

          {/* trend line over per-period totals (in front) */}
          <svg className="fp-layer" width={totalW} height={plotH}
               viewBox={`0 0 ${totalW} ${plotH}`} style={{ zIndex: 2, pointerEvents: "none" }}>
            <path d={trendPath} fill="none" stroke="#ffffff" strokeOpacity={0.92} strokeWidth={2.2}
                  strokeLinejoin="round" strokeLinecap="round" />
            {totals.map((v, k) => {
              const on = vIdx[k] === active;
              return (
                <circle key={k} cx={groupCenter(k)} cy={trendY(v)} r={on ? 4.5 : 2.6}
                        fill="#ffffff" fillOpacity={on ? 1 : 0.75} />
              );
            })}
          </svg>
        </div>
      </div>

      {/* per-period breakdown + legend */}
      <div style={{ padding: "10px 18px 16px" }}>
        <div className="row" style={{ gap: 8, alignItems: "baseline", flexWrap: "wrap", marginBottom: 6 }}>
          <span style={{ fontWeight: 700, fontSize: 14 }}>{fmtPoint(tl.points[active], tl.granularity, true)}</span>
          <span className="faint" style={{ fontSize: 12 }}>
            {activeTotal.toLocaleString()} new · {(tl.cumulative[active] || 0).toLocaleString()} total by then · {bytes(tl.bytes[active] || 0)} added
          </span>
        </div>
        <div className="row" style={{ gap: 14, flexWrap: "wrap" }}>
          {activeBreak.length === 0 && <span className="faint" style={{ fontSize: 12 }}>No new items in this period.</span>}
          {activeBreak.map((s) => (
            <div key={s.label} className="row" style={{ gap: 6, alignItems: "center" }}>
              <SourceIcon type={s.key} fallback={asIcon(s.icon)} size={14} />
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
