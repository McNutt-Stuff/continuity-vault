// Full-color brand marks for data sources. Resolution (which types have a synced
// icon + variant aliases like outlook_local -> outlook) lives in the formal
// registry in ./sourceIcons so every surface stays consistent. Refresh/extend the
// SVG assets with scripts/sync_source_icons.py.

import { SourceIcon } from "./SourceIcon";
import { brandForSource } from "./sourceIcons";

export type BrandName = string;

// Re-exported so existing imports (`import { brandForSource } from "./BrandIcon"`)
// keep working while the registry is the single source of truth.
export { brandForSource };

export function BrandIcon({ name, size = 18 }: { name: BrandName; size?: number }) {
  return <SourceIcon type={name} size={size} />;
}
