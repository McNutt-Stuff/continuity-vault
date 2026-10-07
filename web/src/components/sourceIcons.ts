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
  "salesforce", "crossbeam", "microsoft365", "sharepoint", "teams", "exchange",
  "copilot", "onenote",
  // Apple on-device (mobile) sources with real Commons app marks.
  "device_photos", "device_contacts", "device_calendar", "device_reminders",
  "device_notes",
  // Beyond-Microsoft compliance integrations (Phase 3).
  "google_workspace", "okta", "qualys", "tenable", "proofpoint",
  // Device/endpoint platform marks (Devices page).
  "macos", "windows", "ios", "android",
]);

// Variant/local types that reuse another type's brand mark (e.g. the local
// Outlook store shows the Outlook logo). Keep in sync with the backend map.
export const SOURCE_ICON_ALIASES: Readonly<Record<string, string>> = {
  outlook_local: "outlook",
  // Apple Passwords is iCloud Keychain — reuse the real Apple/iCloud brand mark.
  apple_passwords: "icloud",
  // Managed Exchange calendar/contacts are Outlook/Exchange data.
  calendar: "outlook",
  contacts: "outlook",
  darwin: "macos",
  mac: "macos",
  win: "windows",
  win32: "windows",
  ipados: "ios",
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

// Human-friendly display names for raw source types. The single place that turns
// "imessage" → "Apple Messages", "google_calendar" → "Google Calendar", etc.
export const SOURCE_LABELS: Readonly<Record<string, string>> = {
  gmail: "Gmail",
  google_calendar: "Google Calendar",
  google_contacts: "Google Contacts",
  google_drive: "Google Drive",
  google_photos: "Google Photos",
  google_workspace: "Google Workspace",
  outlook: "Outlook",
  outlook_local: "Outlook (desktop)",
  exchange: "Exchange Online",
  onedrive: "OneDrive",
  onenote: "OneNote",
  sharepoint: "SharePoint",
  teams: "Microsoft Teams",
  microsoft365: "Microsoft 365",
  copilot: "Microsoft Copilot",
  imessage: "Apple Messages",
  icloud: "iCloud",
  onepassword: "1Password",
  apple_passwords: "Apple Passwords",
  dropbox: "Dropbox",
  slack: "Slack",
  notion: "Notion",
  github: "GitHub",
  reddit: "Reddit",
  facebook: "Facebook",
  instagram: "Instagram",
  linkedin: "LinkedIn",
  evernote: "Evernote",
  salesforce: "Salesforce",
  crossbeam: "Crossbeam",
  ubiquiti: "Ubiquiti UniFi",
  okta: "Okta",
  qualys: "Qualys",
  tenable: "Tenable",
  proofpoint: "Proofpoint",
  aws: "Amazon S3",
  azure: "Azure Blob",
  gcp: "Google Cloud Storage",
  endpoint_files: "Device Files",
  device_photos: "Photos",
  device_contacts: "Contacts",
  device_calendar: "Calendar",
  device_reminders: "Reminders",
  device_files: "Files",
  manual: "Added manually",
  macos: "macOS",
  windows: "Windows",
  ios: "iOS",
  android: "Android",
};

// Pretty display name for a raw source type (falls back to a title-cased version
// of the key so a brand-new source still reads reasonably).
export function sourceLabel(type?: string): string {
  if (!type) return "";
  const hit = SOURCE_LABELS[type];
  if (hit) return hit;
  return type.replace(/[_-]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

