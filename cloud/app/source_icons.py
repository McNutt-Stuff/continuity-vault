"""Formal source-icon registry (backend mirror of web/src/components/sourceIcons.ts).

Single source of truth for which data-source types have a synced brand SVG and how
variant/local types alias onto them, so notification emails render the same icons
as the portal. The SVG assets live in web/public/source-icons/<type>.svg and are
synced by scripts/sync_source_icons.py.
"""

from __future__ import annotations

# Types that have a synced brand SVG in web/public/source-icons.
BRAND_ICON_TYPES: frozenset[str] = frozenset({
    "gmail", "onepassword", "outlook", "onedrive", "dropbox", "icloud",
    "google_drive", "slack", "notion", "github", "reddit", "facebook",
    "instagram", "google_calendar", "google_contacts", "google_photos",
    "evernote", "linkedin", "imessage", "ubiquiti", "aws", "azure", "gcp",
    "salesforce", "crossbeam", "microsoft365", "sharepoint", "teams", "exchange",
    "copilot", "onenote",
    # Apple on-device app marks (device_* source types alias onto these).
    "apple_photos", "apple_contacts", "apple_calendar", "apple_reminders",
    "apple_notes", "apple_files", "apple_finder", "apple_health", "apple_wallet",
    # Beyond-Microsoft compliance integrations (Phase 3).
    "google_workspace", "okta", "qualys", "tenable", "proofpoint",
})

# Variant/local types that reuse another type's brand mark (mirror the frontend).
SOURCE_ICON_ALIASES: dict[str, str] = {
    "outlook_local": "outlook",
    # Apple Passwords is iCloud Keychain — reuse the real Apple/iCloud brand mark.
    "apple_passwords": "icloud",
    # Platform-neutral on-device sources → Apple app marks (swap per-platform later).
    "device_photos": "apple_photos",
    "device_contacts": "apple_contacts",
    "device_calendar": "apple_calendar",
    "device_reminders": "apple_reminders",
    "device_files": "apple_files",
    "endpoint_files": "apple_finder",
    "device_health": "apple_health",
    "device_wallet": "apple_wallet",
    # Managed Exchange calendar/contacts are Outlook/Exchange data.
    "calendar": "outlook",
    "contacts": "outlook",
}


def resolve_icon_type(source_type: str) -> str:
    """Apply the alias map (e.g. outlook_local -> outlook)."""
    return SOURCE_ICON_ALIASES.get(source_type or "", source_type or "")


def icon_source_type(source_type: str) -> str:
    """The type whose SVG should render for this source, or "" when none exists."""
    resolved = resolve_icon_type(source_type)
    return resolved if resolved in BRAND_ICON_TYPES else ""


def has_brand_icon(source_type: str) -> bool:
    return bool(icon_source_type(source_type))
