import { useState } from "react";
import { Icon, IconName } from "./Icon";

/**
 * Brand icon for a data source. Renders the synced Wikimedia SVG from
 * /public/source-icons/<type>.svg, falling back to a built-in glyph if the
 * brand icon is missing. Add more via scripts/sync_source_icons.py.
 */
export function SourceIcon({ type, fallback = "database", size = 16, style }:
  { type?: string; fallback?: IconName; size?: number; style?: React.CSSProperties }) {
  const [failed, setFailed] = useState(false);
  if (!type || failed) return <Icon name={fallback} size={size} />;
  return (
    <img src={`/source-icons/${type}.svg`} width={size} height={size} alt=""
         onError={() => setFailed(true)}
         style={{ display: "block", objectFit: "contain", ...style }} />
  );
}
