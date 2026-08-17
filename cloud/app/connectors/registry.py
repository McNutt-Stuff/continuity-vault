"""Concrete connectors for the supported services + a customizable source.

Every connector self-registers via ``@register_connector`` and declares which
metadata fields are searchable, so adding a new puller is a single class.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Iterable, List

from .base import (
    Connector,
    ConnectorCapabilities,
    OAuthSpec,
    SourceObject,
    all_connectors,
    get_connector,
    register_connector,
)


def _dt(days_ago: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days_ago)


def _oid(connector: str, label: str, n: int) -> str:
    return hashlib.sha256(f"{connector}:{label}:{n}".encode()).hexdigest()[:24]


@register_connector
class OnePasswordConnector(Connector):
    connector_type = "onepassword"
    display_name = "1Password"

    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            incremental=True,
            searchable_fields=["url", "username", "kind", "vault", "tags"],
            facet_fields=["vault", "kind"],
        )

    def oauth_spec(self) -> OAuthSpec:
        return OAuthSpec(
            connector_type=self.connector_type,
            display_name=self.display_name,
            auth_type="api-token",
            authorize_url="https://my.1password.com/integrations",
            token_url="https://my.1password.com/api/v1/token",
            scopes=["vault.read", "item.read"],
            icon="key",
            color="#0364d3",
            doc_types=["secret", "note"],
        )

    def fetch_objects(self, account_label, since=None, config=None) -> Iterable[SourceObject]:
        items = [
            ("Chase Bank Login", "login", "chase.com", "rob@arkive.life", "Personal", ["finance"]),
            ("Gmail Recovery Codes", "note", "google.com", "", "Personal", ["recovery"]),
            ("AWS Root Credentials", "login", "aws.amazon.com", "root", "Work", ["cloud", "critical"]),
            ("Home Wi-Fi", "password", "network", "", "Home", ["network"]),
            ("Passport Number", "identity", "travel", "", "Family", ["identity", "travel"]),
        ]
        for i, (title, kind, host, user, vault, tags) in enumerate(items):
            body = json.dumps({"title": title, "kind": kind, "host": host,
                               "secret": "•••• (encrypted at source)"}).encode()
            yield SourceObject(
                object_id=_oid(self.connector_type, account_label, i),
                doc_type="secret",
                title=title,
                content=body,
                preview=f"{kind} · {host}",
                meta={"vault": vault, "kind": kind, "url": host, "username": user, "tags": tags},
                labels=[vault, *tags],
                modified_at=_dt(i * 3),
            )


@register_connector
class GmailConnector(Connector):
    connector_type = "gmail"
    display_name = "Gmail"

    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            incremental=True,
            supports_pagination=True,
            searchable_fields=["from", "to", "folder", "labels"],
            facet_fields=["folder"],
        )

    def oauth_spec(self) -> OAuthSpec:
        return OAuthSpec(
            connector_type=self.connector_type,
            display_name=self.display_name,
            auth_type="oauth2",
            authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
            token_url="https://oauth2.googleapis.com/token",
            scopes=["https://www.googleapis.com/auth/gmail.readonly"],
            icon="mail",
            color="#ea4335",
            doc_types=["email"],
        )

    def fetch_objects(self, account_label, since=None, config=None) -> Iterable[SourceObject]:
        emails = [
            ("Q3 Board Deck — final", "board@company.com", "Please find attached the final deck for Thursday.", "Inbox"),
            ("Your receipt from Apple", "no_reply@apple.com", "Thank you for your purchase of iCloud+ 2TB.", "Receipts"),
            ("Security alert", "no-reply@accounts.google.com", "New sign-in to your account from macOS.", "Inbox"),
            ("Re: Vacation plans", "sarah@family.net", "Booked the flights! Confirmation attached.", "Family"),
            ("Invoice #4821", "billing@vendor.io", "Your invoice is ready. Amount due: $2,400.", "Finance"),
        ]
        for i, (subj, sender, body, folder) in enumerate(emails):
            payload = json.dumps({"subject": subj, "from": sender, "body": body}).encode()
            yield SourceObject(
                object_id=_oid(self.connector_type, account_label, i),
                doc_type="email",
                title=subj,
                content=payload,
                preview=body[:120],
                meta={"from": sender, "to": account_label, "folder": folder,
                      "hasAttachment": i % 2 == 0},
                labels=[folder],
                modified_at=_dt(i),
            )


@register_connector
class OutlookConnector(Connector):
    connector_type = "outlook"
    display_name = "Outlook.com"

    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            incremental=True,
            searchable_fields=["from", "to", "folder"],
            facet_fields=["folder"],
        )

    def oauth_spec(self) -> OAuthSpec:
        return OAuthSpec(
            connector_type=self.connector_type,
            display_name=self.display_name,
            auth_type="oauth2",
            authorize_url="https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
            token_url="https://login.microsoftonline.com/common/oauth2/v2.0/token",
            scopes=["Mail.Read", "offline_access"],
            icon="mail",
            color="#0078d4",
            doc_types=["email"],
        )

    def fetch_objects(self, account_label, since=None, config=None) -> Iterable[SourceObject]:
        emails = [
            ("Contract renewal", "legal@partner.com", "The renewal terms are attached for signature.", "Legal"),
            ("Payroll confirmation", "hr@company.com", "Your payroll has been processed.", "Focused"),
            ("Meeting: Architecture review", "calendar@company.com", "Thursday 2pm, Room 4.", "Focused"),
        ]
        for i, (subj, sender, body, folder) in enumerate(emails):
            payload = json.dumps({"subject": subj, "from": sender, "body": body}).encode()
            yield SourceObject(
                object_id=_oid(self.connector_type, account_label, i),
                doc_type="email",
                title=subj,
                content=payload,
                preview=body[:120],
                meta={"from": sender, "to": account_label, "folder": folder},
                labels=[folder],
                modified_at=_dt(i + 1),
            )


@register_connector
class OneDriveConnector(Connector):
    connector_type = "onedrive"
    display_name = "OneDrive"

    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            incremental=True,
            searchable_fields=["path", "mime"],
            facet_fields=["mime"],
        )

    def oauth_spec(self) -> OAuthSpec:
        return OAuthSpec(
            connector_type=self.connector_type,
            display_name=self.display_name,
            auth_type="oauth2",
            authorize_url="https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
            token_url="https://login.microsoftonline.com/common/oauth2/v2.0/token",
            scopes=["Files.Read.All", "offline_access"],
            icon="cloud",
            color="#0078d4",
            doc_types=["file"],
        )

    def fetch_objects(self, account_label, since=None, config=None) -> Iterable[SourceObject]:
        files = [
            ("2025 Tax Return.pdf", 842_000, "application/pdf", "/Documents/Finance"),
            ("Family Trust.docx", 96_000, "application/vnd.openxmlformats", "/Documents/Legal"),
            ("Insurance Policy.pdf", 1_200_000, "application/pdf", "/Documents/Insurance"),
        ]
        for i, (name, size, mime, folder) in enumerate(files):
            yield SourceObject(
                object_id=_oid(self.connector_type, account_label, i),
                doc_type="file",
                title=name,
                content=f"[binary {name} {size} bytes]".encode(),
                preview=f"{mime} · {size // 1000} KB",
                meta={"mime": mime, "path": f"{folder}/{name}"},
                labels=[folder.strip("/").replace("/", " · ")],
                size_bytes=size,
                modified_at=_dt(i * 2),
            )


@register_connector
class DropboxConnector(Connector):
    connector_type = "dropbox"
    display_name = "Dropbox"

    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            incremental=True,
            searchable_fields=["path", "mime"],
            facet_fields=["mime"],
        )

    def oauth_spec(self) -> OAuthSpec:
        return OAuthSpec(
            connector_type=self.connector_type,
            display_name=self.display_name,
            auth_type="oauth2",
            authorize_url="https://www.dropbox.com/oauth2/authorize",
            token_url="https://api.dropboxapi.com/oauth2/token",
            scopes=["files.content.read", "files.metadata.read"],
            icon="cloud",
            color="#0061ff",
            doc_types=["file"],
        )

    def fetch_objects(self, account_label, since=None, config=None) -> Iterable[SourceObject]:
        files = [
            ("Wedding Photos.zip", 2_400_000_000, "application/zip", "/Media"),
            ("Business Plan.pdf", 540_000, "application/pdf", "/Work"),
            ("Recordings/interview.m4a", 88_000_000, "audio/mp4", "/Recordings"),
        ]
        for i, (name, size, mime, folder) in enumerate(files):
            yield SourceObject(
                object_id=_oid(self.connector_type, account_label, i),
                doc_type="file",
                title=name,
                content=f"[binary {name}]".encode(),
                preview=f"{mime} · {size // 1_000_000} MB",
                meta={"mime": mime, "path": f"{folder}/{name}"},
                labels=[folder.strip("/")],
                size_bytes=size,
                modified_at=_dt(i * 4),
            )


@register_connector
class ICloudConnector(Connector):
    connector_type = "icloud"
    display_name = "iCloud"

    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            searchable_fields=["album", "kind"],
            facet_fields=["album"],
        )

    def oauth_spec(self) -> OAuthSpec:
        return OAuthSpec(
            connector_type=self.connector_type,
            display_name=self.display_name,
            auth_type="app-password",
            authorize_url="https://appleid.apple.com/account/manage",
            token_url="https://setup.icloud.com/setup/ws/1/login",
            scopes=["photos.read", "drive.read", "contacts.read"],
            icon="cloud",
            color="#3693f3",
            doc_types=["photo", "file", "contact"],
        )

    def fetch_objects(self, account_label, since=None, config=None) -> Iterable[SourceObject]:
        items = [
            ("IMG_4821.HEIC", "photo", 3_800_000, "Recents"),
            ("Contacts Export.vcf", "contact", 42_000, "Contacts"),
            ("Notes — Passwords Hint.txt", "file", 1_200, "Notes"),
        ]
        for i, (name, dtype, size, album) in enumerate(items):
            yield SourceObject(
                object_id=_oid(self.connector_type, account_label, i),
                doc_type=dtype,
                title=name,
                content=f"[icloud {name}]".encode(),
                preview=f"{dtype} · {size // 1000} KB",
                meta={"album": album, "kind": dtype},
                labels=[album],
                size_bytes=size,
                modified_at=_dt(i),
            )


@register_connector
class CustomRecordsConnector(Connector):
    """A fully customizable puller for testing and bespoke sources.

    Accepts a ``config`` describing arbitrary records plus a field mapping, so any
    JSON-shaped source can be ingested and made searchable without writing code::

        config = {
          "records": [{"id": "1", "name": "Deed", "party": "Northwind", ...}],
          "mapping": {"id": "id", "title": "name", "preview": "summary",
                       "doc_type": "record", "searchable": ["party", "county"]}
        }

    All mapped metadata is searchable (``searchable_fields = ["*"]``). With no
    config it emits a sample estate/records dataset so it works out of the box.
    """

    connector_type = "custom"
    display_name = "Custom Source"

    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            incremental=False,
            searchable_fields=["*"],  # index every metadata value
            facet_fields=["category"],
        )

    def oauth_spec(self) -> OAuthSpec:
        return OAuthSpec(
            connector_type=self.connector_type,
            display_name=self.display_name,
            auth_type="custom",
            authorize_url="",
            token_url="",
            scopes=[],
            icon="database",
            color="#7a5cff",
            doc_types=["record"],
        )

    _SAMPLE = [
        {"id": "r1", "name": "Property Deed — 14 Elm St", "summary": "Recorded warranty deed",
         "category": "legal", "party": "Northwind Family Office", "county": "Marin", "year": 2019},
        {"id": "r2", "name": "Vehicle Title — Land Rover", "summary": "Certificate of title",
         "category": "asset", "party": "Alex Rivera", "vin": "SAL1234567890", "year": 2022},
        {"id": "r3", "name": "Life Insurance Policy", "summary": "Term policy, $2M",
         "category": "insurance", "party": "Northwind Family Office", "carrier": "Pacific Mutual"},
        {"id": "r4", "name": "Advance Healthcare Directive", "summary": "Signed & notarized",
         "category": "legal", "party": "Jordan Kim", "county": "Marin"},
    ]

    def fetch_objects(self, account_label, since=None, config=None) -> Iterable[SourceObject]:
        config = config or {}
        records = config.get("records") or self._SAMPLE
        mapping = config.get("mapping") or {
            "id": "id", "title": "name", "preview": "summary", "doc_type": "record",
        }
        for i, rec in enumerate(records):
            rid = str(rec.get(mapping.get("id", "id"), i))
            title = str(rec.get(mapping.get("title", "title"), f"Record {i}"))
            preview = str(rec.get(mapping.get("preview", "summary"), ""))
            doc_type = str(rec.get(mapping.get("doc_type", ""), "") or "record")
            # Everything not used as id/title/preview becomes searchable metadata.
            reserved = {mapping.get("id"), mapping.get("title"), mapping.get("preview")}
            meta = {k: v for k, v in rec.items() if k not in reserved}
            labels = [str(rec[k]) for k in ("category", "party") if rec.get(k)]
            yield SourceObject(
                object_id=_oid(self.connector_type, account_label, i) if rid == str(i) else rid,
                doc_type=doc_type,
                title=title,
                content=json.dumps(rec).encode(),
                preview=preview,
                meta=meta,
                labels=labels,
                modified_at=_dt(i),
            )


# Backward-compatible aliases (populated by the @register_connector decorators).
ALL_CONNECTORS: List[Connector] = all_connectors()
REGISTRY = {c.connector_type: c for c in ALL_CONNECTORS}

__all__ = ["ALL_CONNECTORS", "REGISTRY", "get_connector"]

