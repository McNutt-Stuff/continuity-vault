"""
Live data fetchers that pull real objects from provider APIs using an OAuth
access token. Returned as normalized ``SourceObject`` records for the sync
worker. Kept separate from the connector definitions so the API wiring stays
small and the simulated fallbacks remain for local/demo use.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Iterable, List, Optional, Tuple

import httpx

from .base import SourceObject
from ..taxonomy import classify_file, map_1password

logger = logging.getLogger("cv.connectors.live")

GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _HistoryGone(Exception):
    """Gmail history is too old to page from; a full resync is required."""


def _gmail_message(c: httpx.Client, headers: dict, mid: str) -> Optional[SourceObject]:
    r = c.get(f"{GMAIL}/messages/{mid}", headers=headers,
              params={"format": "metadata",
                      "metadataHeaders": ["Subject", "From", "To", "Date"]})
    if r.status_code == 404:
        return None  # deleted between listing and fetch
    r.raise_for_status()
    m = r.json()
    hdrs = {h["name"]: h["value"] for h in m.get("payload", {}).get("headers", [])}
    return SourceObject(
        object_id=f"gmail:{mid}",
        doc_type="email",
        title=hdrs.get("Subject", "(no subject)"),
        content=json.dumps({"headers": hdrs, "snippet": m.get("snippet")}).encode(),
        preview=(m.get("snippet") or "")[:200],
        meta={"from": hdrs.get("From", ""), "to": hdrs.get("To", ""),
              "folder": _gmail_folder(m.get("labelIds", [])),
              "labelIds": m.get("labelIds", [])},
        labels=[l for l in m.get("labelIds", []) if not l.startswith("Label_")],
        size_bytes=int(m.get("sizeEstimate", 0)) or None,  # type: ignore
    )


def _gmail_folder(label_ids: List[str]) -> str:
    for l in ("INBOX", "SENT", "DRAFT", "SPAM", "TRASH"):
        if l in label_ids:
            return l.capitalize()
    return "Mail"


def _gmail_list_ids(c: httpx.Client, headers: dict, cap: int) -> List[str]:
    """All message ids (full sync), paging until exhausted or the safety cap."""
    ids: List[str] = []
    token: Optional[str] = None
    while len(ids) < cap:
        params = {"maxResults": 500}
        if token:
            params["pageToken"] = token
        r = c.get(f"{GMAIL}/messages", headers=headers, params=params)
        r.raise_for_status()
        data = r.json()
        ids.extend(ref["id"] for ref in data.get("messages", []))
        token = data.get("nextPageToken")
        if not token:
            break
    return ids[:cap]


def _gmail_history_ids(c: httpx.Client, headers: dict, start_history_id: str,
                       cap: int) -> List[str]:
    """Ids of messages added/changed since ``start_history_id`` (delta sync)."""
    ids: set[str] = set()
    token: Optional[str] = None
    while len(ids) < cap:
        params = {"startHistoryId": start_history_id,
                  "historyTypes": ["messageAdded", "labelAdded", "labelRemoved"]}
        if token:
            params["pageToken"] = token
        r = c.get(f"{GMAIL}/history", headers=headers, params=params)
        if r.status_code in (404, 410):
            raise _HistoryGone()
        r.raise_for_status()
        data = r.json()
        for h in data.get("history", []):
            for ma in h.get("messagesAdded", []):
                ids.add(ma["message"]["id"])
            for m in h.get("messages", []):
                ids.add(m["id"])
        token = data.get("nextPageToken")
        if not token:
            break
    return list(ids)


def _gmail_history_id(c: httpx.Client, headers: dict) -> str:
    p = c.get(f"{GMAIL}/profile", headers=headers).json()
    return str(p.get("historyId") or "")


def fetch_gmail(access_token: str, cursor: Optional[dict] = None,
                max_messages: int = 5000) -> Tuple[List[SourceObject], dict]:
    """Pull Gmail. First run does a full, paginated backup; subsequent runs pull
    only messages added/changed since the stored historyId. Returns the objects
    plus the new cursor to persist."""
    headers = {"Authorization": f"Bearer {access_token}"}
    cursor = cursor or {}
    with httpx.Client(timeout=60) as c:
        history_id = cursor.get("history_id")
        ids: List[str]
        if history_id:
            try:
                ids = _gmail_history_ids(c, headers, str(history_id), max_messages)
            except _HistoryGone:
                logger.info("gmail history %s expired; full resync", history_id)
                ids = _gmail_list_ids(c, headers, max_messages)
        else:
            ids = _gmail_list_ids(c, headers, max_messages)
        objects = [o for o in (_gmail_message(c, headers, mid) for mid in ids) if o]
        new_cursor = {"history_id": _gmail_history_id(c, headers)}
    return objects, new_cursor


def fetch_graph_mail(access_token: str, limit: int = 40) -> Iterable[SourceObject]:
    headers = {"Authorization": f"Bearer {access_token}"}
    with httpx.Client(timeout=30) as c:
        url: Optional[str] = "https://graph.microsoft.com/v1.0/me/messages"
        params: Optional[dict] = {"$top": 100,
                                  "$select": "subject,from,bodyPreview,receivedDateTime,webLink"}
        seen = 0
        while url and seen < limit:
            r = c.get(url, headers=headers, params=params)
            r.raise_for_status()
            body = r.json()
            for m in body.get("value", []):
                sender = (m.get("from") or {}).get("emailAddress", {}).get("address", "")
                seen += 1
                yield SourceObject(
                    object_id=f"outlook:{m['id']}",
                    doc_type="email",
                    title=m.get("subject") or "(no subject)",
                    content=json.dumps(m).encode(),
                    preview=(m.get("bodyPreview") or "")[:200],
                    meta={"from": sender, "webLink": m.get("webLink")},
                    labels=["Inbox"],
                )
            url, params = body.get("@odata.nextLink"), None


def fetch_graph_files(access_token: str, limit: int = 500) -> Iterable[SourceObject]:
    headers = {"Authorization": f"Bearer {access_token}"}
    with httpx.Client(timeout=30) as c:
        url: Optional[str] = "https://graph.microsoft.com/v1.0/me/drive/root/children"
        params: Optional[dict] = {"$top": 200,
                                  "$select": "name,size,file,folder,parentReference,lastModifiedDateTime"}
        seen = 0
        while url and seen < limit:
            r = c.get(url, headers=headers, params=params)
            r.raise_for_status()
            body = r.json()
            for it in body.get("value", []):
                if it.get("folder"):
                    continue
                mime = (it.get("file") or {}).get("mimeType", "application/octet-stream")
                path = (it.get("parentReference") or {}).get("path", "/drive/root:")
                _cat, _kind = classify_file(it.get("name", ""), mime)
                seen += 1
                yield SourceObject(
                    object_id=f"onedrive:{it['id']}",
                    doc_type=_kind,
                    category=_cat,
                    title=it.get("name", "file"),
                    content=json.dumps(it).encode(),
                    preview=f"{mime} · {int(it.get('size', 0)) // 1000} KB",
                    meta={"mime": mime, "path": f"{path}/{it.get('name')}"},
                    labels=[path.split(":")[-1] or "/"],
                    size_bytes=int(it.get("size", 0)) or None,  # type: ignore
                )
            url, params = body.get("@odata.nextLink"), None


def fetch_dropbox(access_token: str, limit: int = 1000) -> Iterable[SourceObject]:
    headers = {"Authorization": f"Bearer {access_token}",
               "Content-Type": "application/json"}
    with httpx.Client(timeout=30) as c:
        r = c.post("https://api.dropboxapi.com/2/files/list_folder", headers=headers,
                   content=json.dumps({"path": "", "recursive": True, "limit": 1000}))
        r.raise_for_status()
        body = r.json()
        seen = 0
        while seen < limit:
            for it in body.get("entries", []):
                if it.get(".tag") != "file":
                    continue
                _cat, _kind = classify_file(it.get("name", ""))
                seen += 1
                yield SourceObject(
                    object_id=f"dropbox:{it.get('id', it['path_lower'])}",
                    doc_type=_kind,
                    category=_cat,
                    title=it.get("name", "file"),
                    content=json.dumps(it).encode(),
                    preview=f"{int(it.get('size', 0)) // 1000} KB · {it.get('path_display', '')}",
                    meta={"path": it.get("path_display"), "rev": it.get("rev")},
                    labels=["/".join(it.get("path_display", "/").split("/")[:-1]) or "/"],
                    size_bytes=int(it.get("size", 0)) or None,  # type: ignore
                )
            if not body.get("has_more"):
                break
            r = c.post("https://api.dropboxapi.com/2/files/list_folder/continue",
                       headers=headers, content=json.dumps({"cursor": body.get("cursor")}))
            r.raise_for_status()
            body = r.json()


def _status(object_id: str, title: str, preview: str, label: str) -> SourceObject:
    return SourceObject(object_id=object_id, doc_type="note", title=title,
                        content=b"linked", preview=preview, meta={"status": "linked"},
                        labels=[label])


def fetch_1password(creds: dict) -> Iterable[SourceObject]:
    """Pull item metadata + encrypted item detail via a 1Password Connect server.

    Secret field values are stored only inside the (envelope-encrypted) object
    content; the search index carries only non-secret metadata.
    """
    token = creds.get("token")
    host = (creds.get("host") or "").rstrip("/")
    if not token or not host:
        yield _status("onepassword:status", "1Password connected",
                      "Add a 1Password Connect host to enable automatic item sync.",
                      "1Password")
        return
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    with httpx.Client(timeout=30) as c:
        vaults = c.get(f"{host}/v1/vaults", headers=headers)
        vaults.raise_for_status()
        for v in vaults.json():
            listing = c.get(f"{host}/v1/vaults/{v['id']}/items", headers=headers)
            if listing.status_code != 200:
                continue
            for it in listing.json():
                detail = c.get(f"{host}/v1/vaults/{v['id']}/items/{it['id']}",
                               headers=headers).json()
                username = ""
                for f in detail.get("fields", []):
                    if f.get("purpose") == "USERNAME":
                        username = f.get("value", "")
                urls = [u.get("href") for u in it.get("urls", []) if u.get("href")]
                _cat, _kind = map_1password(it.get("category"))
                yield SourceObject(
                    object_id=f"onepassword:{it['id']}",
                    doc_type=_kind,
                    category=_cat,
                    title=it.get("title", "(untitled)"),
                    content=json.dumps(detail).encode(),  # encrypted at rest downstream
                    preview=f"{it.get('category', '')} · {v.get('name', '')}",
                    meta={"vault": v.get("name"), "category": it.get("category"),
                          "kind": _kind, "tags": it.get("tags", []),
                          "url": urls[0] if urls else None, "username": username},
                    labels=[v.get("name"), *it.get("tags", [])],
                )


def fetch_icloud(creds: dict) -> Iterable[SourceObject]:
    """Best-effort iCloud pull (Contacts + Drive listing) via the pyicloud
    library and an app-specific password. iCloud has no official API and may
    require interactive 2FA, in which case a status note is returned."""
    apple_id = creds.get("username")
    password = creds.get("token")
    if not apple_id or not password:
        yield _status("icloud:status", "iCloud connected",
                      "Provide your Apple ID and app-specific password.", "iCloud")
        return
    try:
        from pyicloud import PyiCloudService  # optional dependency
    except Exception:
        yield _status("icloud:status", "iCloud connected",
                      "Install 'pyicloud' on the server to enable iCloud sync.", "iCloud")
        return
    try:
        api = PyiCloudService(apple_id, password)
    except Exception as exc:
        yield _status("icloud:status", "iCloud connected",
                      f"iCloud sign-in failed: {exc}", "iCloud")
        return
    if getattr(api, "requires_2fa", False) or getattr(api, "requires_2sa", False):
        yield _status("icloud:status", "iCloud connected",
                      "iCloud requires interactive two-factor auth; automated sync unavailable.",
                      "iCloud")
        return

    try:
        for ct in (api.contacts.all() or []):
            name = " ".join(filter(None, [ct.get("firstName"), ct.get("lastName")])) \
                or ct.get("companyName") or "Contact"
            emails = [e.get("field") for e in ct.get("emailAddresses", []) if e.get("field")]
            phones = [p.get("field") for p in ct.get("phones", []) if p.get("field")]
            yield SourceObject(
                object_id=f"icloud:contact:{ct.get('contactId')}",
                doc_type="contact", title=name,
                content=json.dumps(ct).encode(),
                preview=", ".join(emails + phones)[:140],
                meta={"emails": emails, "phones": phones}, labels=["Contacts"])
    except Exception:
        pass

    try:
        drive = api.drive
        for name in drive.dir():
            node = drive[name]
            _cat, _kind = classify_file(name)
            yield SourceObject(
                object_id=f"icloud:drive:{name}", doc_type=_kind, category=_cat, title=name,
                content=json.dumps({"name": name, "type": getattr(node, "type", None)}).encode(),
                preview=f"iCloud Drive · {getattr(node, 'type', 'item')}",
                meta={"path": f"/{name}"}, labels=["iCloud Drive"])
    except Exception:
        pass

