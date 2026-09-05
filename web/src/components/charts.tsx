import { useId } from "react";

// Lightweight dependency-free SVG charts for the node telemetry views.

export function Ring({ value, label, sub, color = "#4f7cff", size = 96 }: {
  value: number; label: string; sub?: string; color?: string; size?: number;
}) {
  const v = Math.max(0, Math.min(100, value || 0));
  const stroke = 8;
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const off = c * (1 - v / 100);
  const tone = v >= 90 ? "#f2545b" : v >= 75 ? "#f5a623" : color;
  return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 6 }}>
      <div style={{ position: "relative", width: size, height: size }}>
        <svg width={size} height={size} style={{ transform: "rotate(-90deg)" }}>
          <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="var(--inset)" strokeWidth={stroke} />
          <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke={tone} strokeWidth={stroke}
                  strokeDasharray={c} strokeDashoffset={off} strokeLinecap="round"
                  style={{ transition: "stroke-dashoffset .5s ease, stroke .3s" }} />
        </svg>
        <div style={{ position: "absolute", inset: 0, display: "flex", flexDirection: "column",
                      alignItems: "center", justifyContent: "center" }}>
          <div style={{ fontSize: 20, fontWeight: 700 }}>{Math.round(v)}<span style={{ fontSize: 11, fontWeight: 500 }}>%</span></div>
          {sub && <div className="faint" style={{ fontSize: 10 }}>{sub}</div>}
        </div>
      </div>
      <div className="faint" style={{ fontSize: 11.5, fontWeight: 600 }}>{label}</div>
    </div>
  );
}

export function Sparkline({ data, color = "#4f7cff", width = 120, height = 32, max }: {
  data: number[]; color?: string; width?: number; height?: number; max?: number;
}) {
  const hi = max ?? Math.max(1, ...(data.length ? data : [1]));
  const step = data.length > 1 ? width / (data.length - 1) : width;
  const pts = data.map((d, i) => `${(i * step).toFixed(1)},${(height - (Math.max(0, d) / hi) * height).toFixed(1)}`);
  return (
    <svg viewBox={`0 0 ${width} ${height}`} width="100%" height={height}
         preserveAspectRatio="none" style={{ display: "block" }}>
      {pts.length > 0 && (
        <polyline points={pts.join(" ")} fill="none" stroke={color} strokeWidth={1.6}
                  strokeLinejoin="round" strokeLinecap="round" vectorEffect="non-scaling-stroke" />
      )}
    </svg>
  );
}

export interface AreaSeries { name: string; color: string; data: number[]; }

export function AreaChart({ series, labels, height = 220, unit = "%", max, fmt }: {
  series: AreaSeries[]; labels?: string[]; height?: number; unit?: string; max?: number;
  fmt?: (v: number) => string;
}) {
  const gid = useId().replace(/:/g, "");
  const W = 900, H = height, padL = 44, padB = 22, padT = 10, padR = 8;
  const n = Math.max(1, ...series.map((s) => s.data.length));
  const hi = max ?? Math.max(1, ...series.flatMap((s) => s.data));
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const x = (i: number) => padL + (n > 1 ? (i / (n - 1)) * plotW : 0);
  const y = (v: number) => padT + plotH - (Math.max(0, v) / hi) * plotH;
  const ticks = 4;
  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} preserveAspectRatio="none"
         style={{ display: "block" }}>
      {Array.from({ length: ticks + 1 }).map((_, i) => {
        const gy = padT + (i / ticks) * plotH;
        const val = hi * (1 - i / ticks);
        return (
          <g key={i}>
            <line x1={padL} y1={gy} x2={W - padR} y2={gy} stroke="var(--border-soft)" strokeWidth={1} />
            <text x={padL - 6} y={gy + 3} textAnchor="end" fontSize={9} fill="var(--muted-c,#8a94a7)">
              {fmt ? fmt(val) : `${Math.round(val)}${unit === "%" ? "" : ""}`}
            </text>
          </g>
        );
      })}
      {series.map((s) => {
        if (!s.data.length) return null;
        const line = s.data.map((d, i) => `${x(i).toFixed(1)},${y(d).toFixed(1)}`).join(" L ");
        const area = `M ${x(0).toFixed(1)},${(padT + plotH).toFixed(1)} L ${line} L ${x(s.data.length - 1).toFixed(1)},${(padT + plotH).toFixed(1)} Z`;
        return (
          <g key={s.name}>
            <defs>
              <linearGradient id={`${gid}-${s.name}`} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={s.color} stopOpacity={0.22} />
                <stop offset="100%" stopColor={s.color} stopOpacity={0} />
              </linearGradient>
            </defs>
            <path d={area} fill={`url(#${gid}-${s.name})`} />
            <path d={`M ${line}`} fill="none" stroke={s.color} strokeWidth={1.8}
                  strokeLinejoin="round" strokeLinecap="round" />
          </g>
        );
      })}
      {labels && labels.length > 1 && [0, Math.floor(n / 2), n - 1].map((i) => (
        <text key={i} x={x(i)} y={H - 6} textAnchor="middle" fontSize={9} fill="var(--muted-c,#8a94a7)">
          {labels[i] || ""}
        </text>
      ))}
    </svg>
  );
}
