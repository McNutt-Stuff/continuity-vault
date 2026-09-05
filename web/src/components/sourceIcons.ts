// Formal source-icon registry — the SINGLE source of truth for which data-source
// types have a synced brand mark and how local/variant types alias onto them.
//
// Every icon surface (SourceIcon, BrandIcon, Dashboard, Search, Activity, emails
// via the backend mirror in cloud/app/source_icons.py) resolves through here, so
// a source can never show the wrong icon on one page and the right one on another.
//
// The SVG assets live in web/public/source-icons/<type>.svg and are synced by
// scripts/sync_source_icons.py. When you add a source there, add its type here
// (and to cloud/app/source_icons.py, which mirrors this for notification emails).

// Types that have a synced brand SVG in /public/source-icons.
export const SYNCED_SOURCE_ICONS: ReadonlySet<string> = new Set([
  "gmail", "onepassword", "outlook", "onedrive", "dropbox", "icloud",
  "google_drive", "slack", "notion", "github", "reddit", "facebook",
  "instagram", "google_calendar", "google_contacts", "google_photos",
  "evernote", "linkedin", "imessage", "ubiquiti", "aws", "azure", "gcp",
]);

// Variant/local types that reuse another type's brand mark (e.g. the local
// Outlook store shows the Outlook logo). Keep in sync with the backend map.
export const SOURCE_ICON_ALIASES: Readonly<Record<string, string>> = {
  outlook_local: "outlook",
};

// Resolve a raw source type to the type whose SVG should render (applies aliases).
export function resolveIconType(type?: string): string | undefined {
  if (!type) return undefined;
  return SOURCE_ICON_ALIASES[type] ?? type;
}

// The source type whose brand icon to render, or null when none exists (so the
// caller can fall back to a generic glyph + neutral background).
export function brandForSource(sourceType: string): string | null {
  const t = resolveIconType(sourceType);
  return t && SYNCED_SOURCE_ICONS.has(t) ? t : null;
}
