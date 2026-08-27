import { Icon } from "./Icon";
import { Pill } from "./ui";

function short(v?: string | null): string {
  if (!v) return "";
  return v.length > 12 ? v.slice(0, 12) : v;
}

/** Inline "vX · Up to date / Update available" indicator for a device. */
export function VersionPill({ version, updateAvailable }: {
  version?: string | null; updateAvailable?: boolean;
}) {
  if (!version) return <span className="faint" style={{ fontSize: 12 }}>unknown</span>;
  return (
    <span className="row" style={{ gap: 6, alignItems: "center" }}>
      <span className="mono" style={{ fontSize: 11.5 }}>v{short(version)}</span>
      {updateAvailable
        ? <Pill tone="warn" dot>Update available</Pill>
        : <Pill tone="ok" dot>Up to date</Pill>}
    </span>
  );
}

/** Top-corner banner showing the production version the control plane serves. */
export function ProductionVersion({ label, version }: {
  label: string; version?: string | null;
}) {
  return (
    <div className="row" style={{ gap: 6, alignItems: "center", fontSize: 12 }}
         title="Current production version served by the control plane">
      <Icon name="server" size={12} />
      <span className="faint">{label}</span>
      <span className="mono" style={{ fontWeight: 600 }}>{version ? `v${short(version)}` : "—"}</span>
    </div>
  );
}
