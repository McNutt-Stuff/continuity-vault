import { ReactNode } from "react";

export function Card({ children, style, className, onClick }: { children: ReactNode; style?: React.CSSProperties; className?: string; onClick?: () => void }) {
  return <div className={`card ${className ?? ""}`} style={style} onClick={onClick}>{children}</div>;
}

export function Stat({ label, value, hint }: { label: string; value: ReactNode; hint?: ReactNode }) {
  return (
    <div className="card stat">
      <div className="value">{value}</div>
      <div className="label">{label}</div>
      {hint && <div className="faint" style={{ fontSize: 12 }}>{hint}</div>}
    </div>
  );
}

type Tone = "ok" | "warn" | "danger" | "info";
export function Pill({ tone = "info", children }: { tone?: Tone; children: ReactNode }) {
  return (
    <span className={`pill ${tone}`}>
      <span className="dot" />
      {children}
    </span>
  );
}

// Standard loading state for pages/cards that fetch data on mount.
export function Loading({ label = "Loading…", card = true }: { label?: string; card?: boolean }) {
  const body = (
    <div className="loading-state">
      <span className="spinner" />
      <span className="muted">{label}</span>
    </div>
  );
  return card ? <Card>{body}</Card> : body;
}

export function bytes(n: number): string {
  if (!n) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB", "PB"];
  const i = Math.floor(Math.log(n) / Math.log(1024));
  return `${(n / Math.pow(1024, i)).toFixed(i ? 1 : 0)} ${u[i]}`;
}

export function timeAgo(iso: string | null): string {
  if (!iso) return "never";
  const d = (Date.now() - serverDate(iso).getTime()) / 1000;
  if (d < 60) return "just now";
  if (d < 3600) return `${Math.floor(d / 60)}m ago`;
  if (d < 86400) return `${Math.floor(d / 3600)}h ago`;
  return `${Math.floor(d / 86400)}d ago`;
}

// The API serializes naive UTC datetimes (no timezone suffix). Parse a
// timezone-less value as UTC — otherwise the browser reads it as local time and
// everything looks like it happened "just now"/in the future.
export function serverDate(iso: string): Date {
  const hasTz = /[zZ]$|[+-]\d{2}:?\d{2}$/.test(iso);
  return new Date(hasTz ? iso : iso + "Z");
}

// Softer, plan-appropriate language for the shared/group data scope: a Family
// plan says "family"; Business/Enterprise say "organization"; personal accounts
// have no group scope (just "Me"). Returns null when there's no group scope.
export type GroupScope = { value: "family" | "organization"; label: string; noun: string };
export function groupScope(plan?: string): GroupScope | null {
  const p = (plan || "").toLowerCase();
  if (p === "family") return { value: "family", label: "My family", noun: "family" };
  if (p === "business" || p === "enterprise")
    return { value: "organization", label: "My organization", noun: "organization" };
  return null;
}

// Absolute local date/time for tooltips — consistent across the app.
export function fmtAbsolute(iso: string | null): string {
  if (!iso) return "—";
  return serverDate(iso).toLocaleString([], {
    year: "numeric", month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit",
  });
}
