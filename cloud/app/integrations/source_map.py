"""Map observed network apps/services to Arkive source connectors, and catalog
the popular services worth building a source for.

Used by:
  * shadow-app detection (apps a user relies on but hasn't enabled a source for);
  * the "identify target new sources" analytics (popular services seen in traffic
    that Arkive doesn't yet have a connector for).
"""

from __future__ import annotations

import re

# Normalized app/service name -> Arkive connector type (a source we support).
# Keys are matched case-insensitively against the observed app + category names.
APP_SOURCE_MAP: dict[str, str] = {
    "gmail": "gmail",
    "google mail": "gmail",
    "outlook": "outlook",
    "hotmail": "outlook",
    "office 365": "outlook",
    "microsoft outlook": "outlook",
    "onedrive": "onedrive",
    "dropbox": "dropbox",
    "icloud": "icloud",
    "apple icloud": "icloud",
    "google drive": "google_drive",
    "google docs": "google_drive",
    "1password": "onepassword",
    "slack": "slack",
    "notion": "notion",
    "github": "github",
    "reddit": "reddit",
    "facebook": "facebook",
    "instagram": "instagram",
    "google calendar": "google_calendar",
    "google contacts": "google_contacts",
    "google photos": "google_photos",
    "evernote": "evernote",
    "linkedin": "linkedin",
    "imessage": "imessage",
    "apple messages": "imessage",
}

# Popular services worth a connector. ``source_type`` is set when we already have
# one; ``None`` marks a candidate the platform should consider building. ``kind``
# groups them for the "new sources to build" analytics.
POPULAR_SERVICES: list[dict] = [
    {"name": "Gmail", "source_type": "gmail", "kind": "email"},
    {"name": "Outlook", "source_type": "outlook", "kind": "email"},
    {"name": "iCloud", "source_type": "icloud", "kind": "cloud-storage"},
    {"name": "Google Drive", "source_type": "google_drive", "kind": "cloud-storage"},
    {"name": "Dropbox", "source_type": "dropbox", "kind": "cloud-storage"},
    {"name": "OneDrive", "source_type": "onedrive", "kind": "cloud-storage"},
    {"name": "1Password", "source_type": "onepassword", "kind": "credentials"},
    {"name": "Slack", "source_type": "slack", "kind": "messaging"},
    {"name": "Notion", "source_type": "notion", "kind": "productivity"},
    {"name": "GitHub", "source_type": "github", "kind": "developer"},
    {"name": "Evernote", "source_type": "evernote", "kind": "notes"},
    {"name": "LinkedIn", "source_type": "linkedin", "kind": "social"},
    {"name": "Instagram", "source_type": "instagram", "kind": "social"},
    {"name": "Facebook", "source_type": "facebook", "kind": "social"},
    {"name": "Reddit", "source_type": "reddit", "kind": "social"},
    # Candidates (no connector yet) — surfaced when seen in a customer's traffic.
    {"name": "Box", "source_type": None, "kind": "cloud-storage"},
    {"name": "Proton Drive", "source_type": None, "kind": "cloud-storage"},
    {"name": "WhatsApp", "source_type": None, "kind": "messaging"},
    {"name": "Signal", "source_type": None, "kind": "messaging"},
    {"name": "Telegram", "source_type": None, "kind": "messaging"},
    {"name": "Discord", "source_type": None, "kind": "messaging"},
    {"name": "Zoom", "source_type": None, "kind": "meetings"},
    {"name": "Trello", "source_type": None, "kind": "productivity"},
    {"name": "Asana", "source_type": None, "kind": "productivity"},
    {"name": "Todoist", "source_type": None, "kind": "productivity"},
    {"name": "Bitwarden", "source_type": None, "kind": "credentials"},
    {"name": "LastPass", "source_type": None, "kind": "credentials"},
    {"name": "Spotify", "source_type": None, "kind": "media"},
    {"name": "YouTube", "source_type": None, "kind": "media"},
]

_NAME_TO_SERVICE = {s["name"].lower(): s for s in POPULAR_SERVICES}


def map_app_to_source(name: str, category: str = "") -> str:
    """Best-effort map of an observed app/category to a connector source_type.
    Returns "" when no Arkive source matches."""
    hay = f"{name or ''} {category or ''}".lower()
    for needle, source in APP_SOURCE_MAP.items():
        if needle in hay:
            return source
    return ""


def candidate_service(name: str, category: str = "") -> dict | None:
    """If an observed app corresponds to a popular service we DON'T yet have a
    connector for, return its catalog entry (a build candidate), else None."""
    hay = f"{name or ''} {category or ''}".lower()
    for svc in POPULAR_SERVICES:
        if svc["source_type"]:
            continue
        if re.search(r"\b" + re.escape(svc["name"].lower()) + r"\b", hay):
            return svc
    return None
