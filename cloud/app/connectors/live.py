"""
Live data fetchers that pull real objects from provider APIs using an OAuth
access token. Returned as normalized ``SourceObject`` records for the sync
worker. Kept separate from the connector definitions so the API wiring stays
small and the simulated fallbacks remain for local/demo use.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Iterable, List

import httpx

from .base import SourceObject


def _now() -> datetime:
    return datetime.now(timezone.utc)


def fetch_gmail(access_token: str, limit: int = 40) -> Iterable[SourceObject]:
    headers = {"Authorization": f"Bearer {access_token}"}
    with httpx.Client(timeout=30) as c:
        listing = c.get(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages",
            headers=headers, params={"maxResults": limit})
        listing.raise_for_status()
        for ref in listing.json().get("messages", []):
            m = c.get(
                f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{ref['id']}",
                headers=headers,
                params={"format": "metadata",
                        "metadataHeaders": ["Subject", "From", "To", "Date"]},
            ).json()
            hdrs = {h["name"]: h["value"] for h in m.get("payload", {}).get("headers", [])}
            yield SourceObject(
                object_id=f"gmail:{ref['id']}",
                doc_type="email",
                title=hdrs.get("Subject", "(no subject)"),
                content=json.dumps({"headers": hdrs, "snippet": m.get("snippet")}).encode(),
                preview=(m.get("snippet") or "")[:200],
                meta={"from": hdrs.get("From", ""), "to": hdrs.get("To", ""),
                      "labelIds": m.get("labelIds", [])},
                labels=[l for l in m.get("labelIds", []) if not l.startswith("Label_")],
                size_bytes=int(m.get("sizeEstimate", 0)) or None,  # type: ignore
            )


def fetch_graph_mail(access_token: str, limit: int = 40) -> Iterable[SourceObject]:
    headers = {"Authorization": f"Bearer {access_token}"}
    with httpx.Client(timeout=30) as c:
        r = c.get("https://graph.microsoft.com/v1.0/me/messages", headers=headers,
                  params={"$top": limit,
                          "$select": "subject,from,bodyPreview,receivedDateTime,webLink"})
        r.raise_for_status()
        for m in r.json().get("value", []):
            sender = (m.get("from") or {}).get("emailAddress", {}).get("address", "")
            yield SourceObject(
                object_id=f"outlook:{m['id']}",
                doc_type="email",
                title=m.get("subject") or "(no subject)",
                content=json.dumps(m).encode(),
                preview=(m.get("bodyPreview") or "")[:200],
                meta={"from": sender, "webLink": m.get("webLink")},
                labels=["Inbox"],
            )


def fetch_graph_files(access_token: str, limit: int = 60) -> Iterable[SourceObject]:
    headers = {"Authorization": f"Bearer {access_token}"}
    with httpx.Client(timeout=30) as c:
        r = c.get("https://graph.microsoft.com/v1.0/me/drive/root/children",
                  headers=headers,
                  params={"$top": limit,
                          "$select": "name,size,file,folder,parentReference,lastModifiedDateTime"})
        r.raise_for_status()
        for it in r.json().get("value", []):
            if it.get("folder"):
                continue
            mime = (it.get("file") or {}).get("mimeType", "application/octet-stream")
            path = (it.get("parentReference") or {}).get("path", "/drive/root:")
            yield SourceObject(
                object_id=f"onedrive:{it['id']}",
                doc_type="file",
                title=it.get("name", "file"),
                content=json.dumps(it).encode(),
                preview=f"{mime} · {int(it.get('size', 0)) // 1000} KB",
                meta={"mime": mime, "path": f"{path}/{it.get('name')}"},
                labels=[path.split(":")[-1] or "/"],
                size_bytes=int(it.get("size", 0)) or None,  # type: ignore
            )


def fetch_dropbox(access_token: str, limit: int = 100) -> Iterable[SourceObject]:
    headers = {"Authorization": f"Bearer {access_token}",
               "Content-Type": "application/json"}
    with httpx.Client(timeout=30) as c:
        r = c.post("https://api.dropboxapi.com/2/files/list_folder", headers=headers,
                   content=json.dumps({"path": "", "recursive": False, "limit": limit}))
        r.raise_for_status()
        for it in r.json().get("entries", []):
            if it.get(".tag") != "file":
                continue
            yield SourceObject(
                object_id=f"dropbox:{it.get('id', it['path_lower'])}",
                doc_type="file",
                title=it.get("name", "file"),
                content=json.dumps(it).encode(),
                preview=f"{int(it.get('size', 0)) // 1000} KB · {it.get('path_display', '')}",
                meta={"path": it.get("path_display"), "rev": it.get("rev")},
                labels=["/".join(it.get("path_display", "/").split("/")[:-1]) or "/"],
                size_bytes=int(it.get("size", 0)) or None,  # type: ignore
            )
