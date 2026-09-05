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
})

# Variant/local types that reuse another type's brand mark (mirror the frontend).
SOURCE_ICON_ALIASES: dict[str, str] = {
    "outlook_local": "outlook",
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
