import { SourceIcon } from "./SourceIcon";
import { Icon, IconName } from "./Icon";

/**
 * Icon for a backup storage destination / location. Renders the provider brand
 * mark (AWS / Azure / Google Cloud) for a customer's own bucket, an appliance
 * glyph for on-prem hardware, and the Arkive cloud glyph for our hosted tier.
 *
 * `provider` (aws|azure|gcp) is preferred when known; otherwise the destination
 * id is used to infer the kind ("cv-cloud", "store:<id>"/"appliance", "byos:<id>").
 */
const BRANDS = new Set(["aws", "azure", "gcp"]);

export function destBrand(dest?: string, provider?: string): string {
  if (provider && BRANDS.has(provider)) return provider;
  const d = dest || "";
  if (d.startsWith("appliance") || d.startsWith("store:")) return "appliance";
  if (d === "cv-cloud") return "arkive";
  return "cloud";
}

export function DestIcon({ dest, provider, size = 13, fallback = "cloud" }:
  { dest?: string; provider?: string; size?: number; fallback?: IconName }) {
  const b = destBrand(dest, provider);
  const inner = BRANDS.has(b)
    ? <SourceIcon type={b} fallback={fallback} size={size} />
    : b === "appliance"
      ? <Icon name="server" size={size} />
      : <Icon name="cloud" size={size} />;
  // Inline-flex wrapper so a block <img> brand mark still aligns with adjacent text.
  return (
    <span style={{ display: "inline-flex", alignItems: "center", flex: "none", verticalAlign: "middle" }}>
      {inner}
    </span>
  );
}
