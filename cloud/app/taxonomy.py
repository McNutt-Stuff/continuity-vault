"""
Canonical information model for imported data.

Every connector maps its provider-specific types into a two-level taxonomy —
a top-level ``Category`` and a specific ``kind`` — so the platform can index,
facet, classify sensitivity, and apply policy categorically regardless of source.

    Category (10)        Example kinds
    ------------------   ----------------------------------------------------
    credential           login, password, api_key, ssh_key, credit_card, ...
    message              email, chat, sms, voicemail, comment
    contact              person, organization, group
    document             pdf, text, spreadsheet, presentation, code, ebook
    media                image, photo, video, audio
    file                 archive, binary, generic
    calendar             event, task, reminder
    note                 note, journal, bookmark
    identity             passport, drivers_license, ssn, tax_document, ...
    record               database_row, form_submission, custom

Sensitivity drives handling: ``restricted`` categories (credentials, identity)
never place derived content in the search index — only the title and non-secret
metadata are indexed; the payload stays envelope-encrypted.
"""

from __future__ import annotations

from enum import Enum
from typing import Dict, List, Tuple


class Category(str, Enum):
    CREDENTIAL = "credential"
    MESSAGE = "message"
    CONTACT = "contact"
    DOCUMENT = "document"
    IMAGE = "image"
    MEDIA = "media"
    FILE = "file"
    CALENDAR = "calendar"
    NOTE = "note"
    IDENTITY = "identity"
    RECORD = "record"


class Sensitivity(str, Enum):
    STANDARD = "standard"
    SENSITIVE = "sensitive"
    RESTRICTED = "restricted"


# Per-category metadata. ``icon`` maps to a frontend Icon name.
CATEGORY_META: Dict[Category, dict] = {
    Category.CREDENTIAL: {"display": "Credentials", "icon": "key",
                          "sensitivity": Sensitivity.RESTRICTED, "index_preview": False},
    Category.MESSAGE: {"display": "Messages", "icon": "mail",
                       "sensitivity": Sensitivity.SENSITIVE, "index_preview": True},
    Category.CONTACT: {"display": "Contacts", "icon": "user",
                       "sensitivity": Sensitivity.SENSITIVE, "index_preview": True},
    Category.DOCUMENT: {"display": "Documents", "icon": "file",
                        "sensitivity": Sensitivity.STANDARD, "index_preview": True},
    Category.IMAGE: {"display": "Images", "icon": "image",
                     "sensitivity": Sensitivity.STANDARD, "index_preview": True},
    Category.MEDIA: {"display": "Video & Audio", "icon": "activity",
                     "sensitivity": Sensitivity.STANDARD, "index_preview": True},
    Category.FILE: {"display": "Files", "icon": "database",
                    "sensitivity": Sensitivity.STANDARD, "index_preview": True},
    Category.CALENDAR: {"display": "Calendar", "icon": "calendar",
                        "sensitivity": Sensitivity.STANDARD, "index_preview": True},
    Category.NOTE: {"display": "Notes", "icon": "note",
                    "sensitivity": Sensitivity.SENSITIVE, "index_preview": True},
    Category.IDENTITY: {"display": "Identity & Legal", "icon": "shield",
                        "sensitivity": Sensitivity.RESTRICTED, "index_preview": False},
    Category.RECORD: {"display": "Records", "icon": "database",
                      "sensitivity": Sensitivity.STANDARD, "index_preview": True},
}

# Canonical kinds per category.
KINDS: Dict[Category, List[str]] = {
    Category.CREDENTIAL: ["login", "password", "api_key", "ssh_key", "certificate",
                          "oauth_token", "secure_note", "software_license", "database",
                          "wifi", "server", "crypto_wallet", "credit_card",
                          "bank_account", "recovery_codes", "membership", "secret"],
    Category.MESSAGE: ["email", "chat", "sms", "voicemail", "comment"],
    Category.CONTACT: ["person", "organization", "group", "contact"],
    Category.DOCUMENT: ["pdf", "text", "spreadsheet", "presentation", "form",
                        "ebook", "code", "drawing"],
    Category.IMAGE: ["image", "photo"],
    Category.MEDIA: ["video", "audio"],
    Category.FILE: ["archive", "binary", "generic", "file"],
    Category.CALENDAR: ["event", "task", "reminder"],
    Category.NOTE: ["note", "journal", "bookmark"],
    Category.IDENTITY: ["passport", "drivers_license", "national_id", "ssn",
                        "birth_certificate", "visa", "tax_document", "legal_document",
                        "medical_record", "insurance_policy", "identity"],
    Category.RECORD: ["database_row", "form_submission", "custom", "record"],
}

KIND_TO_CATEGORY: Dict[str, str] = {
    kind: cat.value for cat, kinds in KINDS.items() for kind in kinds
}


def category_for_kind(kind: str) -> str:
    """Resolve a kind to its category value (unknown kinds fall back to file)."""
    return KIND_TO_CATEGORY.get((kind or "").lower(), Category.FILE.value)


def sensitivity_for(category: str) -> str:
    try:
        return CATEGORY_META[Category(category)]["sensitivity"].value
    except (ValueError, KeyError):
        return Sensitivity.STANDARD.value


def index_preview(category: str) -> bool:
    try:
        return CATEGORY_META[Category(category)]["index_preview"]
    except (ValueError, KeyError):
        return True


_CODE_EXT = {"js", "ts", "tsx", "py", "java", "go", "rb", "rs", "c", "cpp", "h",
             "json", "xml", "yaml", "yml", "sh", "html", "css", "sql", "php"}
_IMAGE_EXT = {"jpg", "jpeg", "png", "gif", "heic", "heif", "webp", "tiff", "bmp", "svg"}
_VIDEO_EXT = {"mp4", "mov", "avi", "mkv", "webm", "m4v"}
_AUDIO_EXT = {"mp3", "wav", "aac", "flac", "m4a", "ogg", "aiff"}
_ARCHIVE_EXT = {"zip", "tar", "gz", "tgz", "rar", "7z", "bz2", "xz"}


def classify_file(name: str, mime: str = "") -> Tuple[str, str]:
    """Map a file name/MIME to a (category, kind)."""
    m = (mime or "").lower()
    ext = name.rsplit(".", 1)[-1].lower() if "." in (name or "") else ""

    if m == "application/pdf" or ext == "pdf":
        return (Category.DOCUMENT.value, "pdf")
    if "wordprocessingml" in m or "msword" in m or ext in {"doc", "docx", "odt", "rtf", "pages"}:
        return (Category.DOCUMENT.value, "text")
    if "spreadsheet" in m or "ms-excel" in m or ext in {"xls", "xlsx", "csv", "tsv", "ods", "numbers"}:
        return (Category.DOCUMENT.value, "spreadsheet")
    if "presentation" in m or "powerpoint" in m or ext in {"ppt", "pptx", "key", "odp"}:
        return (Category.DOCUMENT.value, "presentation")
    if ext in {"epub", "mobi", "azw3"}:
        return (Category.DOCUMENT.value, "ebook")
    if ext in _CODE_EXT:
        return (Category.DOCUMENT.value, "code")
    if m.startswith("text/") or ext in {"txt", "md", "log"}:
        return (Category.DOCUMENT.value, "text")

    if m.startswith("image/") or ext in _IMAGE_EXT:
        return (Category.MEDIA.value, "image")
    if m.startswith("video/") or ext in _VIDEO_EXT:
        return (Category.MEDIA.value, "video")
    if m.startswith("audio/") or ext in _AUDIO_EXT:
        return (Category.MEDIA.value, "audio")

    if ext in _ARCHIVE_EXT or "zip" in m or "compressed" in m or "tar" in m:
        return (Category.FILE.value, "archive")
    return (Category.FILE.value, "generic")


# 1Password item category -> canonical (category, kind).
_OP_MAP: Dict[str, Tuple[str, str]] = {
    "LOGIN": (Category.CREDENTIAL.value, "login"),
    "PASSWORD": (Category.CREDENTIAL.value, "password"),
    "API_CREDENTIAL": (Category.CREDENTIAL.value, "api_key"),
    "SSH_KEY": (Category.CREDENTIAL.value, "ssh_key"),
    "DATABASE": (Category.CREDENTIAL.value, "database"),
    "SERVER": (Category.CREDENTIAL.value, "server"),
    "WIRELESS_ROUTER": (Category.CREDENTIAL.value, "wifi"),
    "CRYPTO_WALLET": (Category.CREDENTIAL.value, "crypto_wallet"),
    "SECURE_NOTE": (Category.CREDENTIAL.value, "secure_note"),
    "SOFTWARE_LICENSE": (Category.CREDENTIAL.value, "software_license"),
    "CREDIT_CARD": (Category.CREDENTIAL.value, "credit_card"),
    "BANK_ACCOUNT": (Category.CREDENTIAL.value, "bank_account"),
    "REWARD_PROGRAM": (Category.CREDENTIAL.value, "membership"),
    "MEMBERSHIP": (Category.CREDENTIAL.value, "membership"),
    "IDENTITY": (Category.IDENTITY.value, "identity"),
    "PASSPORT": (Category.IDENTITY.value, "passport"),
    "DRIVER_LICENSE": (Category.IDENTITY.value, "drivers_license"),
    "SOCIAL_SECURITY_NUMBER": (Category.IDENTITY.value, "ssn"),
    "MEDICAL_RECORD": (Category.IDENTITY.value, "medical_record"),
    "DOCUMENT": (Category.FILE.value, "generic"),
}


def map_1password(op_category: str) -> Tuple[str, str]:
    return _OP_MAP.get((op_category or "").upper(), (Category.CREDENTIAL.value, "secure_note"))


def describe() -> dict:
    """Full taxonomy for the API/UI."""
    return {
        "categories": [
            {
                "category": cat.value,
                "display": meta["display"],
                "icon": meta["icon"],
                "sensitivity": meta["sensitivity"].value,
                "indexPreview": meta["index_preview"],
                "kinds": KINDS[cat],
            }
            for cat, meta in CATEGORY_META.items()
        ]
    }
