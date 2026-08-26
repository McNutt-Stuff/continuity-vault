#!/usr/bin/env python3
"""
Sync brand icons for data sources from Wikimedia Commons.

Downloads an SVG logo per source type into ``web/public/source-icons/<type>.svg``
so the portal can show a real brand mark for each source (Gmail, Dropbox, …)
with a graceful fallback to the built-in glyphs.

HOW TO ADD / RETRIEVE MORE ICONS
--------------------------------
1. Add an entry to ``SOURCE_ICONS`` below: the source ``type`` (must match the
   connector ``connector_type`` in cloud/app/connectors/registry.py) mapped to
   the exact Wikimedia Commons *File* title (without the "File:" prefix), e.g.
   "Gmail icon (2020).svg". Browse/search titles at
   https://commons.wikimedia.org/  (namespace "File:").
2. Run:  python3 scripts/sync_source_icons.py
3. If the exact title is unknown, put a best-guess title OR a plain search phrase
   in ``search`` and the script resolves it via the Commons API and downloads the
   first matching SVG.

Fetching mechanics (reusable by an agent):
  - Direct file bytes:   https://commons.wikimedia.org/wiki/Special:FilePath/<TITLE>
  - Resolve a title:     https://commons.wikimedia.org/w/api.php?action=query
                         &titles=File:<TITLE>&prop=imageinfo&iiprop=url&format=json
  - Search for a file:   .../w/api.php?action=query&list=search&srnamespace=6
                         &srsearch=<PHRASE>&format=json
  A descriptive User-Agent is REQUIRED by the Wikimedia API policy.

Note: brand logos are trademarks; used here nominatively to identify the service
a customer is connecting. Prefer PD-textlogo / freely-licensed files on Commons.
"""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

UA = "ArkiveIconSync/1.0 (https://arkive.life; icons@arkive.life)"
API = "https://commons.wikimedia.org/w/api.php"
FILEPATH = "https://commons.wikimedia.org/wiki/Special:FilePath/"
OUT_DIR = Path(__file__).resolve().parents[1] / "web" / "public" / "source-icons"

# source type -> {file: exact Commons File title, search: fallback phrase}
SOURCE_ICONS: dict[str, dict] = {
    "gmail":      {"file": "Gmail icon (2020).svg", "search": "Gmail logo"},
    "onepassword": {"file": "1Password favicon.svg", "search": "1Password favicon"},
    "outlook":    {"file": "Microsoft Office Outlook (2018–present).svg", "search": "Outlook logo"},
    "onedrive":   {"file": "Microsoft Office OneDrive (2019–present).svg", "search": "OneDrive logo"},
    "dropbox":    {"file": "Dropbox Icon.svg", "search": "Dropbox icon"},
    "icloud":     {"file": "ICloud logo.svg", "search": "iCloud logo"},
    "google_drive": {"file": "Google Drive icon (2020).svg", "search": "Google Drive logo"},
    "slack":      {"file": "Slack icon 2019.svg", "search": "Slack logo"},
    "notion":     {"file": "Notion-logo.svg", "search": "Notion app logo"},
    "github":     {"file": "Octicons-mark-github.svg", "search": "GitHub mark"},
    "reddit":     {"file": "Reddit Logo Icon.svg", "search": "Reddit logo icon"},
    "facebook":   {"file": "2023 Facebook icon.svg", "search": "2023 Facebook icon"},
    "instagram":  {"file": "Instagram icon.svg", "search": "Instagram logo icon"},
    "linkedin":   {"file": "LinkedIn icon.svg", "search": "LinkedIn logo icon"},
    "evernote":   {"file": "Evernote.svg", "search": "Evernote logo"},
    "google_calendar": {"file": "Google Calendar icon (2020).svg", "search": "Google Calendar icon"},
    "google_contacts": {"file": "Google Contacts icon.svg", "search": "Google Contacts icon"},
    "google_photos": {"file": "Google Photos icon (2020).svg", "search": "Google Photos logo"},
    "imessage":   {"file": "IMessage logo.svg", "search": "iMessage logo"},
    # Integrations (network intelligence) — matched by integration_type.
    "ubiquiti":   {"file": "Ubiquiti Logo 2023.svg", "search": "Ubiquiti logo"},
    # Bring-your-own cloud storage providers — matched by CustomerStorage.provider.
    "aws":        {"file": "Amazon Web Services Logo.svg", "search": "Amazon Web Services logo"},
    "azure":      {"file": "Microsoft Azure.svg", "search": "Microsoft Azure logo"},
    "gcp":        {"file": "Google Cloud logo.svg", "search": "Google Cloud Platform logo"},
}


def _get(url: str, timeout: int = 25) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _is_svg(data: bytes) -> bool:
    head = data[:512].lstrip().lower()
    return head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in data[:2048].lower())


def _search_title(phrase: str) -> str | None:
    q = urllib.parse.urlencode({
        "action": "query", "list": "search", "srnamespace": 6,
        "srsearch": f"{phrase} filetype:svg", "srlimit": 5, "format": "json",
    })
    try:
        data = json.loads(_get(f"{API}?{q}"))
        for hit in data.get("query", {}).get("search", []):
            title = hit.get("title", "")
            if title.lower().endswith(".svg"):
                return title[5:] if title.startswith("File:") else title
    except Exception:
        return None
    return None


def _download(title: str) -> bytes | None:
    url = FILEPATH + urllib.parse.quote(title)
    try:
        data = _get(url)
        return data if _is_svg(data) else None
    except Exception:
        return None


def sync() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ok, fail = 0, 0
    for stype, spec in SOURCE_ICONS.items():
        data = _download(spec["file"]) if spec.get("file") else None
        if data is None and spec.get("search"):
            title = _search_title(spec["search"])
            if title:
                data = _download(title)
        dest = OUT_DIR / f"{stype}.svg"
        if data:
            dest.write_bytes(data)
            print(f"  ✓ {stype:<14} {len(data):>6}B -> {dest.relative_to(OUT_DIR.parents[2])}")
            ok += 1
        else:
            print(f"  ✗ {stype:<14} could not resolve an SVG (check the title/search)")
            fail += 1
    print(f"\nSynced {ok} icon(s), {fail} missing → {OUT_DIR}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(sync())
