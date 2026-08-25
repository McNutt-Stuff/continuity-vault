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
    FetchResult,
    OAuthSpec,
    SourceObject,
    all_connectors,
    get_connector,
    register_connector,
)
from . import live


def _dt(days_ago: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days_ago)


def _oid(connector: str, label: str, n: int) -> str:
    return hashlib.sha256(f"{connector}:{label}:{n}".encode()).hexdigest()[:24]


def _content_cap() -> int:
    from ..config import get_settings
    return get_settings().content_max_bytes


@register_connector
class EndpointFilesConnector(Connector):
    """Files collected from a desktop agent (local, external, and network drives).

    Agent-collected like 1Password: the operator picks folders in the Data Map,
    the agent walks them and pushes client-encrypted files. There is no cloud
    pull, so ``fetch_objects`` yields nothing."""

    connector_type = "endpoint_files"
    display_name = "Endpoint Files"

    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            requires_agent=True,
            searchable_fields=["path", "folder", "extension", "drive", "kind"],
            facet_fields=["extension", "drive", "kind"],
        )

    def oauth_spec(self) -> OAuthSpec:
        return OAuthSpec(
            connector_type=self.connector_type,
            display_name=self.display_name,
            auth_type="agent",
            authorize_url="",
            token_url="",
            scopes=[],
            icon="folder",
            color="#7a5cff",
            doc_types=["file", "pdf", "image", "video", "audio", "spreadsheet",
                       "presentation", "text", "archive"],
        )

    def fetch_objects(self, account_label, since=None, config=None) -> Iterable[SourceObject]:
        return iter(())  # agent-pushed; no cloud pull


@register_connector
class OnePasswordConnector(Connector):
    connector_type = "onepassword"
    display_name = "1Password"

    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            incremental=True,
            requires_agent=True,  # collected locally via the 1Password `op` CLI
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
        config = config or {}
        if config.get("token"):
            yield from live.fetch_1password(config)
            return
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
class ImessageConnector(Connector):
    """Apple iMessage / SMS, collected locally by the desktop agent from
    ``~/Library/Messages/chat.db``. Messages carry their thread (chat) id, the
    sender/recipients (number or email + contact name), group info and attachment
    references; attachments are backed up as their own file objects."""

    connector_type = "imessage"
    display_name = "Apple Messages"

    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            incremental=True,
            requires_agent=True,
            searchable_fields=["chat_id", "chat_name", "from", "to", "service",
                               "direction", "is_group", "message_guid", "filename", "kind"],
            facet_fields=["service", "chat_name", "is_group", "kind"],
            filter_categories=[
                {"id": "messages", "label": "Messages"},
                {"id": "attachments", "label": "Attachments"},
            ],
        )

    def oauth_spec(self) -> OAuthSpec:
        return OAuthSpec(
            connector_type=self.connector_type, display_name=self.display_name,
            auth_type="agent", authorize_url="", token_url="", scopes=[],
            icon="mail", color="#34da50",
            doc_types=["message", "image", "video", "audio", "file", "pdf"],
        )

    def fetch_objects(self, account_label, since=None, config=None) -> Iterable[SourceObject]:
        return iter(())  # agent-pushed; no cloud pull


@register_connector
class OutlookLocalConnector(Connector):
    """Local Microsoft Outlook store, collected by the desktop agent from the
    on-device Outlook profile (mail + attachments, contacts, calendar, notes) —
    so locally-cached data is backed up even for accounts not reachable in cloud."""

    connector_type = "outlook_local"
    display_name = "Outlook (local)"

    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            requires_agent=True,
            searchable_fields=["from", "to", "folder", "org", "email", "kind"],
            facet_fields=["kind", "folder"],
            filter_categories=[
                {"id": "mail", "label": "Email"},
                {"id": "contacts", "label": "Contacts"},
                {"id": "calendar", "label": "Calendar"},
                {"id": "notes", "label": "Notes"},
            ],
        )

    def oauth_spec(self) -> OAuthSpec:
        return OAuthSpec(
            connector_type=self.connector_type, display_name=self.display_name,
            auth_type="agent", authorize_url="", token_url="", scopes=[],
            icon="mail", color="#0a5bd3",
            doc_types=["email", "contact", "event", "note"],
        )

    def fetch_objects(self, account_label, since=None, config=None) -> Iterable[SourceObject]:
        return iter(())  # agent-pushed; no cloud pull


@register_connector
class GmailConnector(Connector):
    connector_type = "gmail"
    display_name = "Gmail"

    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            incremental=True,
            supports_pagination=True,
            delta=True,
            streaming=True,
            historical=True,
            dual_track=True,
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

    def fetch(self, account_label, cursor=None, config=None) -> FetchResult:
        # Live Gmail: full first backup (paginated), then history-based deltas.
        config = config or {}
        if config.get("access_token"):
            from ..config import get_settings  # avoid import cycle at module load
            s = get_settings()
            objects, new_cursor = live.fetch_gmail(
                config["access_token"], cursor=cursor,
                max_messages=s.sync_max_items, content_cap=s.content_max_bytes,
                options={"excludeFolders": config.get("excludeFolders"),
                         "includeSpamTrash": config.get("includeSpamTrash"),
                         "sinceDate": config.get("sinceDate")})
            return FetchResult(objects=objects, cursor=new_cursor, has_more=False)
        return FetchResult(objects=list(self.fetch_objects(account_label, config=config)))

    def fetch_stream(self, account_label, cursor=None, config=None, state=None, mode="recent"):
        # Two-track pull: mode="recent" history-deltas from the watermark; mode=
        # "backfill" pages the whole mailbox backwards in resumable chunks. The two
        # run concurrently with separate cursors so new mail is captured promptly
        # while a large history backfills in the background.
        config = config or {}
        if config.get("access_token"):
            from ..config import get_settings
            s = get_settings()
            yield from live.stream_gmail(
                config["access_token"], cursor=cursor,
                max_messages=s.sync_max_items, content_cap=s.content_max_bytes,
                options={"excludeFolders": config.get("excludeFolders"),
                         "includeSpamTrash": config.get("includeSpamTrash"),
                         "sinceDate": config.get("sinceDate")}, state=state, mode=mode)
        else:
            yield from self.fetch_objects(account_label, config=config)

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
            streaming=True,
            delta=True,
            historical=True,
            dual_track=True,
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

    def fetch_stream(self, account_label, cursor=None, config=None, state=None, mode="recent"):
        # Two-track: "recent" pulls new mail from a watermark; "backfill" pages
        # the whole mailbox backwards. Both resume via bounded chunks.
        config = config or {}
        if config.get("access_token"):
            from ..config import get_settings
            s = get_settings()
            yield from live.stream_outlook(
                config["access_token"], cursor=cursor,
                content_cap=s.content_max_bytes,
                options={"sinceDate": config.get("sinceDate")},
                state=state if state is not None else {}, mode=mode)
            return
        yield from self.fetch_objects(account_label, config=config)

    def fetch_objects(self, account_label, since=None, config=None) -> Iterable[SourceObject]:
        config = config or {}
        if config.get("access_token"):
            yield from live.fetch_graph_mail(config["access_token"], content_cap=_content_cap())
            return
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
            streaming=True,
            delta=True,
            historical=True,
            browsable=True,
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

    def list_folders(self, config, path=""):
        token = (config or {}).get("access_token")
        return live.onedrive_list_folders(token, path) if token else []

    def fetch_stream(self, account_label, cursor=None, config=None, state=None, mode="recent"):
        config = config or {}
        if config.get("access_token"):
            yield from live.stream_onedrive(config["access_token"], cursor, config,
                                            state if state is not None else {}, _content_cap())
            return
        yield from self.fetch_objects(account_label, config=config)

    def fetch_objects(self, account_label, since=None, config=None) -> Iterable[SourceObject]:
        config = config or {}
        if config.get("access_token"):
            yield from live.fetch_graph_files(config["access_token"], content_cap=_content_cap())
            return
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
            streaming=True,
            delta=True,
            historical=True,
            browsable=True,
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

    def list_folders(self, config, path=""):
        token = (config or {}).get("access_token")
        return live.dropbox_list_folders(token, path) if token else []

    def fetch_stream(self, account_label, cursor=None, config=None, state=None, mode="recent"):
        config = config or {}
        if config.get("access_token"):
            yield from live.stream_dropbox(config["access_token"], cursor, config,
                                           state if state is not None else {}, _content_cap())
            return
        yield from self.fetch_objects(account_label, config=config)

    def fetch_objects(self, account_label, since=None, config=None) -> Iterable[SourceObject]:
        config = config or {}
        if config.get("access_token"):
            yield from live.fetch_dropbox(config["access_token"], content_cap=_content_cap())
            return
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
class GoogleDriveConnector(Connector):
    connector_type = "google_drive"
    display_name = "Google Drive"

    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            incremental=True,
            streaming=True,
            delta=True,
            historical=True,
            browsable=True,
            searchable_fields=["name", "mime"],
            facet_fields=["mime"],
        )

    def oauth_spec(self) -> OAuthSpec:
        return OAuthSpec(
            connector_type=self.connector_type,
            display_name=self.display_name,
            auth_type="oauth2",
            authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
            token_url="https://oauth2.googleapis.com/token",
            scopes=["openid", "https://www.googleapis.com/auth/userinfo.email",
                    "https://www.googleapis.com/auth/drive.readonly"],
            icon="cloud",
            color="#1fa463",
            doc_types=["file", "document"],
        )

    def list_folders(self, config, path=""):
        token = (config or {}).get("access_token")
        return live.drive_list_folders(token, path) if token else []

    def fetch_stream(self, account_label, cursor=None, config=None, state=None, mode="recent"):
        config = config or {}
        if config.get("access_token"):
            yield from live.stream_drive(config["access_token"], cursor, config,
                                         state if state is not None else {}, _content_cap())
            return
        yield from self.fetch_objects(account_label, config=config)

    def fetch_objects(self, account_label, since=None, config=None) -> Iterable[SourceObject]:
        config = config or {}
        if config.get("access_token"):
            state: dict = {}
            yield from live.stream_drive(config["access_token"], None, config, state, _content_cap())
            return
        files = [
            ("Q3 Report.docx", 210_000, "application/vnd.google-apps.document", "Work"),
            ("Budget.xlsx", 88_000, "application/vnd.google-apps.spreadsheet", "Finance"),
            ("Vacation.jpg", 3_200_000, "image/jpeg", "Photos"),
        ]
        for i, (name, size, mime, folder) in enumerate(files):
            _cat, _kind = ("document", "document") if mime.startswith(
                "application/vnd.google-apps.") else ("document", "file")
            yield SourceObject(
                object_id=_oid(self.connector_type, account_label, i),
                doc_type=_kind, category=_cat, title=name,
                content=f"[binary {name}]".encode(),
                preview=f"{mime} · {size // 1000} KB",
                meta={"mime": mime, "name": name}, labels=[folder],
                size_bytes=size, modified_at=_dt(i * 3))


@register_connector
class ICloudConnector(Connector):
    connector_type = "icloud"
    display_name = "iCloud"

    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            streaming=True,
            searchable_fields=["album", "kind", "path"],
            facet_fields=["kind", "album"],
            filter_categories=[
                {"id": "photos", "label": "Photos & videos"},
                {"id": "files", "label": "iCloud Drive files"},
                {"id": "contacts", "label": "Contacts"},
            ],
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
            doc_types=["photo", "video", "image", "file", "person"],
        )

    def fetch_objects(self, account_label, since=None, config=None) -> Iterable[SourceObject]:
        config = config or {}
        if config.get("token"):
            yield from live.fetch_icloud(
                config.get("username", ""), config["token"], _content_cap(),
                options={"includeCategories": config.get("includeCategories")})
            return
        items = [
            ("IMG_4821.HEIC", "image", 3_800_000, "Photos"),
            ("Contacts Export.vcf", "person", 42_000, "Contacts"),
            ("Notes — Passwords Hint.txt", "file", 1_200, "iCloud Drive"),
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
class GoogleContactsConnector(Connector):
    connector_type = "google_contacts"
    display_name = "Google Contacts"

    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            searchable_fields=["emails", "phones", "org"],
            facet_fields=["org"],
        )

    def oauth_spec(self) -> OAuthSpec:
        return OAuthSpec(
            connector_type=self.connector_type, display_name=self.display_name,
            auth_type="oauth2",
            authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
            token_url="https://oauth2.googleapis.com/token",
            scopes=["https://www.googleapis.com/auth/contacts.readonly"],
            icon="user", color="#4285f4", doc_types=["person"],
        )

    def fetch_objects(self, account_label, since=None, config=None) -> Iterable[SourceObject]:
        config = config or {}
        if config.get("access_token"):
            yield from live.fetch_google_contacts(config["access_token"], _content_cap())
            return
        for i, (name, email) in enumerate([("Sarah Chen", "sarah@family.net"),
                                           ("Dr. Alvarez", "office@clinic.com")]):
            yield SourceObject(
                object_id=_oid(self.connector_type, account_label, i),
                doc_type="person", category="contact", title=name,
                content=json.dumps({"name": name, "email": email}).encode(),
                preview=email, meta={"emails": [email], "kind": "contact"},
                labels=["Contacts"], modified_at=_dt(i))


@register_connector
class GoogleCalendarConnector(Connector):
    connector_type = "google_calendar"
    display_name = "Google Calendar"

    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            searchable_fields=["calendar", "location", "organizer", "recurring"],
            facet_fields=["calendar"],
        )

    def oauth_spec(self) -> OAuthSpec:
        return OAuthSpec(
            connector_type=self.connector_type, display_name=self.display_name,
            auth_type="oauth2",
            authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
            token_url="https://oauth2.googleapis.com/token",
            scopes=["https://www.googleapis.com/auth/calendar.readonly"],
            icon="calendar", color="#4285f4", doc_types=["event"],
        )

    def fetch_objects(self, account_label, since=None, config=None) -> Iterable[SourceObject]:
        config = config or {}
        if config.get("access_token"):
            yield from live.fetch_google_calendar(config["access_token"], _content_cap())
            return
        for i, (title, when) in enumerate([("Board meeting", "2026-01-14T14:00"),
                                          ("Family dinner", "2026-01-20T18:30")]):
            yield SourceObject(
                object_id=_oid(self.connector_type, account_label, i),
                doc_type="event", category="calendar", title=title,
                content=json.dumps({"summary": title, "start": when}).encode(),
                preview=when, meta={"calendar": "Primary", "start": when, "kind": "event"},
                labels=["Primary"], modified_at=_dt(i))


@register_connector
class GooglePhotosConnector(Connector):
    connector_type = "google_photos"
    display_name = "Google Photos"

    def capabilities(self) -> ConnectorCapabilities:
        # Picker-based import: Google no longer allows unattended full-library
        # reads, so the user picks items/albums each session and Arkive imports
        # only what's new (deduped). Not auto-scheduled (picker, delta=False).
        return ConnectorCapabilities(
            picker=True,
            searchable_fields=["filename", "mime", "kind"],
            facet_fields=["kind"],
        )

    def oauth_spec(self) -> OAuthSpec:
        return OAuthSpec(
            connector_type=self.connector_type, display_name=self.display_name,
            auth_type="oauth2",
            authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
            token_url="https://oauth2.googleapis.com/token",
            scopes=["https://www.googleapis.com/auth/photospicker.mediaitems.readonly"],
            icon="image", color="#fbbc05", doc_types=["photo", "video"],
        )

    def fetch(self, account_label, cursor=None, config=None) -> FetchResult:
        # Import happens through the interactive picker flow, not an automatic
        # pull — a scheduled/manual run is a no-op.
        return FetchResult(objects=[], cursor=cursor, has_more=False)

    def fetch_objects(self, account_label, since=None, config=None) -> Iterable[SourceObject]:
        return iter(())


@register_connector
class RedditConnector(Connector):
    connector_type = "reddit"
    display_name = "Reddit"

    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            streaming=True,
            searchable_fields=["subreddit", "kind"],
            facet_fields=["subreddit", "kind"],
            filter_categories=[
                {"id": "posts", "label": "Posts"},
                {"id": "comments", "label": "Comments"},
                {"id": "saved", "label": "Saved"},
                {"id": "messages", "label": "Private messages"},
            ],
        )

    def oauth_spec(self) -> OAuthSpec:
        return OAuthSpec(
            connector_type=self.connector_type, display_name=self.display_name,
            auth_type="oauth2",
            authorize_url="https://www.reddit.com/api/v1/authorize",
            token_url="https://www.reddit.com/api/v1/access_token",
            scopes=["identity", "history", "read", "privatemessages"],
            icon="activity", color="#ff4500",
            doc_types=["post", "comment", "message"],
        )

    def fetch_objects(self, account_label, since=None, config=None) -> Iterable[SourceObject]:
        config = config or {}
        if config.get("access_token"):
            yield from live.fetch_reddit(
                config["access_token"], _content_cap(),
                options={"includeCategories": config.get("includeCategories")})
            return
        for i, (title, sub) in enumerate([("Ask me anything about backups", "selfhosted"),
                                         ("My homelab tour", "homelab")]):
            yield SourceObject(
                object_id=_oid(self.connector_type, account_label, i),
                doc_type="post", category="social", title=title,
                content=json.dumps({"title": title, "subreddit": sub}).encode(),
                preview=f"r/{sub}", meta={"subreddit": sub, "kind": "post"},
                labels=["Posts", f"r/{sub}"], modified_at=_dt(i))


@register_connector
class FacebookConnector(Connector):
    connector_type = "facebook"
    display_name = "Facebook"

    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            streaming=True,
            searchable_fields=["kind"],
            facet_fields=["kind"],
            filter_categories=[
                {"id": "posts", "label": "Posts"},
                {"id": "photos", "label": "Photos"},
            ],
        )

    def oauth_spec(self) -> OAuthSpec:
        return OAuthSpec(
            connector_type=self.connector_type, display_name=self.display_name,
            auth_type="oauth2",
            authorize_url="https://www.facebook.com/v19.0/dialog/oauth",
            token_url="https://graph.facebook.com/v19.0/oauth/access_token",
            scopes=["public_profile", "user_posts", "user_photos"],
            icon="user", color="#1877f2", doc_types=["post", "image"],
        )

    def fetch_objects(self, account_label, since=None, config=None) -> Iterable[SourceObject]:
        config = config or {}
        if config.get("access_token"):
            yield from live.fetch_facebook(
                config["access_token"], _content_cap(),
                options={"includeCategories": config.get("includeCategories")})
            return
        for i, msg in enumerate(["Great trip to the coast!", "Happy birthday to my sister"]):
            yield SourceObject(
                object_id=_oid(self.connector_type, account_label, i),
                doc_type="post", category="social", title=msg[:60],
                content=json.dumps({"message": msg}).encode(), preview=msg,
                meta={"kind": "post"}, labels=["Posts"], modified_at=_dt(i))


@register_connector
class InstagramConnector(Connector):
    connector_type = "instagram"
    display_name = "Instagram"

    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            streaming=True,
            searchable_fields=["media_type", "kind"],
            facet_fields=["media_type"],
            filter_categories=[{"id": "media", "label": "Photos & videos"}],
        )

    def oauth_spec(self) -> OAuthSpec:
        return OAuthSpec(
            connector_type=self.connector_type, display_name=self.display_name,
            auth_type="oauth2",
            authorize_url="https://api.instagram.com/oauth/authorize",
            token_url="https://api.instagram.com/oauth/access_token",
            scopes=["user_profile", "user_media"],
            icon="image", color="#e4405f", doc_types=["image", "video"],
        )

    def fetch_objects(self, account_label, since=None, config=None) -> Iterable[SourceObject]:
        config = config or {}
        if config.get("access_token"):
            yield from live.fetch_instagram(
                config["access_token"], _content_cap(),
                options={"includeCategories": config.get("includeCategories")})
            return
        for i, cap in enumerate(["Sunset at the lake", "New puppy"]):
            yield SourceObject(
                object_id=_oid(self.connector_type, account_label, i),
                doc_type="image", category="image", title=cap,
                content=json.dumps({"caption": cap}).encode(), preview=cap,
                meta={"media_type": "IMAGE", "kind": "image"},
                labels=["Instagram"], modified_at=_dt(i))


@register_connector
class LinkedInConnector(Connector):
    connector_type = "linkedin"
    display_name = "LinkedIn"

    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            streaming=True,
            searchable_fields=["kind", "headline"],
            facet_fields=["kind"],
            filter_categories=[
                {"id": "profile", "label": "Profile"},
                {"id": "resume", "label": "Résumé"},
                {"id": "posts", "label": "Posts & articles"},
                {"id": "messages", "label": "Messages"},
                {"id": "connections", "label": "Connections"},
            ],
        )

    def oauth_spec(self) -> OAuthSpec:
        return OAuthSpec(
            connector_type=self.connector_type, display_name=self.display_name,
            auth_type="oauth2",
            authorize_url="https://www.linkedin.com/oauth/v2/authorization",
            token_url="https://www.linkedin.com/oauth/v2/accessToken",
            scopes=["openid", "profile", "email"],
            icon="user", color="#0a66c2",
            doc_types=["profile", "resume", "post", "message", "contact"],
        )

    def fetch_objects(self, account_label, since=None, config=None) -> Iterable[SourceObject]:
        config = config or {}
        if config.get("access_token"):
            yield from live.fetch_linkedin(
                config["access_token"], _content_cap(),
                options={"includeCategories": config.get("includeCategories")})
            return
        yield SourceObject(
            object_id=_oid(self.connector_type, account_label, 0),
            doc_type="profile", category="social", title="LinkedIn profile",
            content=json.dumps({"name": account_label, "headline": "Sample profile"}).encode(),
            preview=account_label, meta={"kind": "profile"}, labels=["Profile"], modified_at=_dt(0))
        yield SourceObject(
            object_id=_oid(self.connector_type, account_label, 1),
            doc_type="resume", category="document", title=f"{account_label} — LinkedIn résumé",
            content=json.dumps({"name": account_label, "headline": "Sample profile"}).encode(),
            preview="Résumé", meta={"kind": "resume"}, labels=["Resume"], modified_at=_dt(1))


@register_connector
class GitHubConnector(Connector):
    connector_type = "github"
    display_name = "GitHub"

    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            incremental=True,
            streaming=True,
            delta=True,
            historical=True,
            browsable=True,  # the "folder" picker selects repositories
            searchable_fields=["repo", "path", "language", "state", "author"],
            facet_fields=["repo", "language", "state"],
            filter_categories=[
                {"id": "code", "label": "Repository files"},
                {"id": "issues", "label": "Issues & pull requests"},
            ],
        )

    def oauth_spec(self) -> OAuthSpec:
        return OAuthSpec(
            connector_type=self.connector_type, display_name=self.display_name,
            auth_type="oauth2",
            authorize_url="https://github.com/login/oauth/authorize",
            token_url="https://github.com/login/oauth/access_token",
            scopes=["read:user", "user:email", "repo"],
            icon="database", color="#1f2328",
            doc_types=["code", "text", "repository", "issue", "pull_request"],
        )

    def list_folders(self, config, path=""):
        token = (config or {}).get("access_token")
        return live.github_list_repos(token, path) if token else []

    def fetch_stream(self, account_label, cursor=None, config=None, state=None, mode="recent"):
        config = config or {}
        if config.get("access_token"):
            yield from live.stream_github(config["access_token"], cursor, config,
                                          state if state is not None else {}, _content_cap())
            return
        yield from self.fetch_objects(account_label, config=config)

    def fetch_objects(self, account_label, since=None, config=None) -> Iterable[SourceObject]:
        config = config or {}
        if config.get("access_token"):
            state: dict = {}
            yield from live.stream_github(config["access_token"], None, config, state, _content_cap())
            return
        # Simulated dataset (demo/local) — a repo with a file + an issue.
        repo = f"{account_label}/example"
        yield SourceObject(
            object_id=_oid(self.connector_type, account_label, 0),
            doc_type="repository", category="developer", title=repo,
            content=json.dumps({"full_name": repo, "language": "Python"}).encode(),
            preview="Example repository", meta={"repo": repo, "kind": "repository"},
            labels=[repo], modified_at=_dt(1))
        yield SourceObject(
            object_id=_oid(self.connector_type, account_label, 1),
            doc_type="code", category="developer", title="main.py",
            content=b"print('hello world')\n", preview=f"{repo} \u00b7 main.py",
            meta={"repo": repo, "path": "main.py"}, labels=[repo], modified_at=_dt(2))
        yield SourceObject(
            object_id=_oid(self.connector_type, account_label, 2),
            doc_type="issue", category="developer", title="#1 Sample issue",
            content=json.dumps({"number": 1, "title": "Sample issue"}).encode(),
            preview="A sample issue", meta={"repo": repo, "number": 1, "kind": "issue"},
            labels=[repo, "open"], modified_at=_dt(3))


@register_connector
class EvernoteConnector(Connector):
    connector_type = "evernote"
    display_name = "Evernote"

    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            incremental=True,   # Evernote sync is USN-based (updateCount)
            streaming=True,     # notes carry attachments (resources) → bounded ingest
            searchable_fields=["notebook", "tags", "author", "kind"],
            facet_fields=["notebook", "kind"],
            filter_categories=[
                {"id": "notes", "label": "Notes"},
                {"id": "attachments", "label": "Attachments"},
            ],
        )

    def oauth_spec(self) -> OAuthSpec:
        return OAuthSpec(
            connector_type=self.connector_type,
            display_name=self.display_name,
            auth_type="oauth2",
            authorize_url="https://accounts.evernote.com/oauth2/authorize",
            token_url="https://accounts.evernote.com/oauth2/token",
            scopes=[],
            icon="note",
            color="#2dbe60",
            doc_types=["note", "file"],
        )

    def fetch_objects(self, account_label, since=None, config=None) -> Iterable[SourceObject]:
        config = config or {}
        if config.get("access_token"):
            from . import evernote_mcp
            yield from evernote_mcp.fetch(
                config["access_token"], _content_cap(),
                options={"includeCategories": config.get("includeCategories")})
            return
        # Simulated dataset (demo/local) — notes across notebooks with tags.
        samples = [
            ("Home Renovation Plan", "Projects", ["home", "budget"], "Contractor quotes, permit checklist, and the phased timeline for the kitchen remodel."),
            ("Recipes to Try", "Personal", ["cooking"], "Weeknight pasta, no-knead sourdough, and a slow-roast brisket to attempt this fall."),
            ("Q3 Strategy Notes", "Work", ["strategy", "okr"], "Top priorities, hiring plan, and the product roadmap headed into the next quarter."),
            ("Travel — Japan", "Travel", ["itinerary"], "Flights, ryokan bookings, JR pass details, and a day-by-day plan for Kyoto and Tokyo."),
            ("Meeting Notes — June", "Work", ["meetings"], "Action items, owners, and decisions from the weekly staff sync."),
        ]
        for i, (title, notebook, tags, body) in enumerate(samples):
            yield SourceObject(
                object_id=_oid(self.connector_type, account_label, i),
                doc_type="note",
                title=title,
                content=json.dumps({"title": title, "notebook": notebook,
                                    "tags": tags, "content": body}).encode(),
                preview=body[:140],
                meta={"notebook": notebook, "tags": tags, "kind": "note"},
                labels=[notebook, *tags],
                modified_at=_dt(i * 5),
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

