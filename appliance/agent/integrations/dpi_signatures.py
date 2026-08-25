"""Compact fallback map for UniFi DPI category/application ids → names.

The controller resolves most names dynamically (see ubiquiti._load_dpi_names);
this is only a supplement for common services so shadow-app detection still works
if the controller doesn't return a name map. Keys are integers as returned by the
DPI stats API.
"""

from __future__ import annotations

# Category id -> human name (well-known UniFi DPI categories).
DPI_CATS: dict[int, str] = {
    1: "Instant Messaging",
    3: "Mail",
    4: "Web",
    5: "Streaming Media",
    6: "Social Networks",
    7: "File Transfer",
    8: "Cloud Storage",
    9: "VoIP",
    13: "Gaming",
    18: "Remote Access",
    19: "Business",
    20: "Network Management",
}

# Application id -> human name for services Arkive can map to a source. This is a
# best-effort subset; unknown ids fall back to their category name.
DPI_APPS: dict[int, str] = {
    5: "Google",
    56: "Gmail",
    106: "Google Drive",
    107: "Google Photos",
    108: "Google Calendar",
    112: "Dropbox",
    113: "Apple iCloud",
    116: "Microsoft OneDrive",
    118: "Microsoft Outlook",
    133: "Facebook",
    138: "Instagram",
    222: "Slack",
    247: "GitHub",
    263: "LinkedIn",
    281: "Reddit",
    301: "Evernote",
    322: "Notion",
    401: "WhatsApp",
    402: "Telegram",
    403: "Signal",
    404: "Discord",
    501: "Zoom",
    777: "1Password",
}
