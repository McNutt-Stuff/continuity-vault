// Full-color brand marks for data sources. These are the synced Wikimedia
// Commons SVGs served from /public/source-icons/<type>.svg (refresh/extend with
// scripts/sync_source_icons.py). Falls back to a built-in glyph when missing.

import { SourceIcon } from "./SourceIcon";

export type BrandName = string;

// Source types that have a synced brand icon. Keep in sync with the keys in
// scripts/sync_source_icons.py.
const SYNCED = new Set([
  "gmail", "onepassword", "outlook", "onedrive", "dropbox", "icloud",
  "google_drive", "slack", "notion", "github",
  "reddit", "facebook", "instagram", "google_calendar", "google_contacts",
  "google_photos", "evernote",
]);

// Returns the source type when a dedicated brand icon exists, else null so the
// caller can render a generic glyph (and pick a neutral vs. colored background).
export function brandForSource(sourceType: string): string | null {
  return SYNCED.has(sourceType) ? sourceType : null;
}

export function BrandIcon({ name, size = 18 }: { name: BrandName; size?: number }) {
  return <SourceIcon type={name} size={size} />;
}
