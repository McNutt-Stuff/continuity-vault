"""
Live data fetchers that pull real objects from provider APIs using an OAuth
access token. Returned as normalized ``SourceObject`` records for the sync
worker. Kept separate from the connector definitions so the API wiring stays
small and the simulated fallbacks remain for local/demo use.
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timezone
from email import message_from_bytes
from email.header import decode_header, make_header
from typing import Iterable, List, Optional, Tuple

import httpx

from .base import SourceObject
from ..taxonomy import classify_file, map_1password

logger = logging.getLogger("cv.connectors.live")

GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
_DEFAULT_CAP = 268435456  # 256 MiB

# Gmail folder/label ids -> a Gmail search token so we can skip whole folders
# (e.g. Spam, Promotions) at list time; anything else is filtered by label id.
_GMAIL_EXCLUDE_QUERY = {
    "SPAM": "-in:spam",
    "TRASH": "-in:trash",
    "CATEGORY_PROMOTIONS": "-category:promotions",
    "CATEGORY_SOCIAL": "-category:social",
    "CATEGORY_UPDATES": "-category:updates",
    "CATEGORY_FORUMS": "-category:forums",
}


def _hdr(parsed, name: str) -> str:
    """Decode a possibly MIME-encoded email header (=?utf-8?..?=) to plain text.

    ``Message.get`` can return an ``email.header.Header`` for encoded values,
    which is not a str — decode it so indexing/joining never crashes."""
    if parsed is None:
        return ""
    raw = parsed.get(name)
    if raw is None:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return str(raw)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _capped(raw: bytes, cap: int) -> Tuple[bytes, bool]:
    """Store the full raw content, or an indexed-only marker when it exceeds the
    per-object cap. Returns (content, backed_up)."""
    if raw and len(raw) <= cap:
        return raw, True
    marker = json.dumps({"_arkive": "content_exceeds_cap", "bytes": len(raw)}).encode()
    return marker, False


class _HistoryGone(Exception):
    """Gmail history is too old to page from; a full resync is required."""


def _gmail_message(c: httpx.Client, headers: dict, mid: str,
                   cap: int = _DEFAULT_CAP) -> Optional[SourceObject]:
    # format=raw returns the full RFC822 message (body + attachments) plus
    # labelIds/snippet — the actual content we back up, not just metadata.
    r = c.get(f"{GMAIL}/messages/{mid}", headers=headers, params={"format": "raw"})
    if r.status_code == 404:
        return None  # deleted between listing and fetch
    r.raise_for_status()
    m = r.json()
    raw_b64 = m.get("raw", "")
    raw = base64.urlsafe_b64decode(raw_b64 + "===") if raw_b64 else b""
    parsed = message_from_bytes(raw) if raw else None
    subject = _hdr(parsed, "Subject") or "(no subject)"
    label_ids = m.get("labelIds", [])
    content, backed = _capped(raw, cap)
    return SourceObject(
        object_id=f"gmail:{mid}",
        doc_type="email",
        title=subject,
        content=content,  # full raw email (or indexed-only marker if oversized)
        preview=(m.get("snippet") or "")[:200],
        meta={"from": _hdr(parsed, "From"),
              "to": _hdr(parsed, "To"),
              "folder": _gmail_folder(label_ids),
              "labelIds": label_ids, "content_backed_up": backed},
        labels=[l for l in label_ids if not l.startswith("Label_")],
        size_bytes=len(raw) or int(m.get("sizeEstimate", 0)) or None,  # type: ignore
    )


def _gmail_folder(label_ids: List[str]) -> str:
    for l in ("INBOX", "SENT", "DRAFT", "SPAM", "TRASH"):
        if l in label_ids:
            return l.capitalize()
    return "Mail"


def _gmail_list_ids(c: httpx.Client, headers: dict, cap: int,
                    query: str = "", include_spam_trash: bool = False) -> List[str]:
    """All message ids (full sync), paging until exhausted or the safety cap.

    ``query`` is a Gmail search string used to skip whole folders (e.g.
    ``-in:spam -category:promotions``); ``include_spam_trash`` opts Spam/Trash in
    (Gmail lists exclude them by default)."""
    ids: List[str] = []
    token: Optional[str] = None
    while len(ids) < cap:
        params: dict = {"maxResults": 500}
        if query:
            params["q"] = query
        if include_spam_trash:
            params["includeSpamTrash"] = "true"
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
                max_messages: int = 5000,
                content_cap: int = _DEFAULT_CAP,
                options: Optional[dict] = None) -> Tuple[List[SourceObject], dict]:
    """Pull Gmail. First run does a full, paginated backup; subsequent runs pull
    only messages added/changed since the stored historyId. ``options`` may carry
    ``excludeFolders`` (label ids to skip, e.g. SPAM/CATEGORY_PROMOTIONS) and
    ``includeSpamTrash``. Returns the objects plus the new cursor to persist."""
    headers = {"Authorization": f"Bearer {access_token}"}
    cursor = cursor or {}
    options = options or {}
    exclude = {str(f).upper() for f in (options.get("excludeFolders") or [])}
    include_spam_trash = bool(options.get("includeSpamTrash")) and not (exclude & {"SPAM", "TRASH"})
    query = " ".join(_GMAIL_EXCLUDE_QUERY[f] for f in exclude if f in _GMAIL_EXCLUDE_QUERY)
    with httpx.Client(timeout=60) as c:
        history_id = cursor.get("history_id")
        ids: List[str]
        if history_id:
            try:
                ids = _gmail_history_ids(c, headers, str(history_id), max_messages)
            except _HistoryGone:
                logger.info("gmail history %s expired; full resync", history_id)
                ids = _gmail_list_ids(c, headers, max_messages, query, include_spam_trash)
        else:
            ids = _gmail_list_ids(c, headers, max_messages, query, include_spam_trash)
        objects = [o for o in (_gmail_message(c, headers, mid, content_cap) for mid in ids) if o]
        # Post-filter by label id so the delta (history) path and any custom
        # labels are also honoured, not just the list-time query.
        if exclude:
            objects = [o for o in objects
                       if not (set(o.meta.get("labelIds", [])) & exclude)]
        new_cursor = {"history_id": _gmail_history_id(c, headers)}
    return objects, new_cursor


def stream_gmail(access_token: str, cursor: Optional[dict] = None,
                 max_messages: int = 5000, content_cap: int = _DEFAULT_CAP,
                 options: Optional[dict] = None, state: Optional[dict] = None):
    """Lazy Gmail pull: yields one message at a time (so the caller can ingest in
    bounded batches instead of holding the whole mailbox in RAM) and records the
    new cursor in ``state['cursor']`` once done."""
    headers = {"Authorization": f"Bearer {access_token}"}
    cursor = cursor or {}
    options = options or {}
    exclude = {str(f).upper() for f in (options.get("excludeFolders") or [])}
    include_spam_trash = bool(options.get("includeSpamTrash")) and not (exclude & {"SPAM", "TRASH"})
    query = " ".join(_GMAIL_EXCLUDE_QUERY[f] for f in exclude if f in _GMAIL_EXCLUDE_QUERY)
    with httpx.Client(timeout=60) as c:
        history_id = cursor.get("history_id")
        if history_id:
            try:
                ids = _gmail_history_ids(c, headers, str(history_id), max_messages)
            except _HistoryGone:
                logger.info("gmail history %s expired; full resync", history_id)
                ids = _gmail_list_ids(c, headers, max_messages, query, include_spam_trash)
        else:
            ids = _gmail_list_ids(c, headers, max_messages, query, include_spam_trash)
        for mid in ids:
            o = _gmail_message(c, headers, mid, content_cap)
            if not o:
                continue
            if exclude and (set(o.meta.get("labelIds", [])) & exclude):
                continue
            yield o
        if state is not None:
            state["cursor"] = {"history_id": _gmail_history_id(c, headers)}


def fetch_graph_mail(access_token: str, limit: int = 40,
                     content_cap: int = _DEFAULT_CAP) -> Iterable[SourceObject]:
    headers = {"Authorization": f"Bearer {access_token}"}
    with httpx.Client(timeout=60) as c:
        url: Optional[str] = "https://graph.microsoft.com/v1.0/me/messages"
        params: Optional[dict] = {"$top": 100,
                                  "$select": "id,subject,from,bodyPreview,receivedDateTime,parentFolderId,webLink"}
        seen = 0
        while url and seen < limit:
            r = c.get(url, headers=headers, params=params)
            r.raise_for_status()
            body = r.json()
            for m in body.get("value", []):
                sender = (m.get("from") or {}).get("emailAddress", {}).get("address", "")
                # Full MIME (headers + body + attachments) = the backed-up content.
                mime = c.get(f"https://graph.microsoft.com/v1.0/me/messages/{m['id']}/$value",
                             headers=headers)
                raw = mime.content if mime.status_code < 400 else b""
                content, backed = _capped(raw, content_cap)
                seen += 1
                yield SourceObject(
                    object_id=f"outlook:{m['id']}",
                    doc_type="email",
                    title=m.get("subject") or "(no subject)",
                    content=content,
                    preview=(m.get("bodyPreview") or "")[:200],
                    meta={"from": sender, "folder": "Inbox", "webLink": m.get("webLink"),
                          "content_backed_up": backed},
                    labels=["Inbox"],
                    size_bytes=len(raw) or None,  # type: ignore
                )
            url, params = body.get("@odata.nextLink"), None


def fetch_graph_files(access_token: str, limit: int = 500,
                      content_cap: int = _DEFAULT_CAP) -> Iterable[SourceObject]:
    headers = {"Authorization": f"Bearer {access_token}"}
    with httpx.Client(timeout=120) as c:
        url: Optional[str] = "https://graph.microsoft.com/v1.0/me/drive/root/children"
        params: Optional[dict] = {"$top": 200,
                                  "$select": "id,name,size,file,folder,parentReference,lastModifiedDateTime,@microsoft.graph.downloadUrl"}
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
                size = int(it.get("size", 0))
                _cat, _kind = classify_file(it.get("name", ""), mime)
                # Download the actual file bytes (capped) as the backed-up content.
                dl = it.get("@microsoft.graph.downloadUrl")
                raw = b""
                if dl and size <= content_cap:
                    try:
                        raw = c.get(dl).content
                    except Exception:
                        raw = b""
                content, backed = _capped(raw, content_cap) if raw else (
                    json.dumps({"_arkive": "content_exceeds_cap" if size > content_cap else "no_content",
                                "bytes": size}).encode(), False)
                seen += 1
                yield SourceObject(
                    object_id=f"onedrive:{it['id']}",
                    doc_type=_kind,
                    category=_cat,
                    title=it.get("name", "file"),
                    content=content,
                    preview=f"{mime} · {size // 1000} KB",
                    meta={"mime": mime, "path": f"{path}/{it.get('name')}",
                          "content_backed_up": backed},
                    labels=[path.split(":")[-1] or "/"],
                    size_bytes=size or None,  # type: ignore
                )
            url, params = body.get("@odata.nextLink"), None


def fetch_dropbox(access_token: str, limit: int = 1000,
                  content_cap: int = _DEFAULT_CAP) -> Iterable[SourceObject]:
    headers = {"Authorization": f"Bearer {access_token}",
               "Content-Type": "application/json"}
    with httpx.Client(timeout=120) as c:
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
                size = int(it.get("size", 0))
                # Download the file bytes (capped) via the content endpoint.
                raw = b""
                if size <= content_cap:
                    try:
                        dr = c.post("https://content.dropboxapi.com/2/files/download",
                                    headers={"Authorization": headers["Authorization"],
                                             "Dropbox-API-Arg": json.dumps({"path": it.get("path_lower")})})
                        raw = dr.content if dr.status_code < 400 else b""
                    except Exception:
                        raw = b""
                content, backed = _capped(raw, content_cap) if raw else (
                    json.dumps({"_arkive": "content_exceeds_cap" if size > content_cap else "no_content",
                                "bytes": size}).encode(), False)
                seen += 1
                yield SourceObject(
                    object_id=f"dropbox:{it.get('id', it['path_lower'])}",
                    doc_type=_kind,
                    category=_cat,
                    title=it.get("name", "file"),
                    content=content,
                    preview=f"{size // 1000} KB · {it.get('path_display', '')}",
                    meta={"path": it.get("path_display"), "rev": it.get("rev"),
                          "content_backed_up": backed},
                    labels=["/".join(it.get("path_display", "/").split("/")[:-1]) or "/"],
                    size_bytes=size or None,  # type: ignore
                )
            if not body.get("has_more"):
                break
            r = c.post("https://api.dropboxapi.com/2/files/list_folder/continue",
                       headers=headers, content=json.dumps({"cursor": body.get("cursor")}))
            r.raise_for_status()
            body = r.json()


def fetch_icloud(username: str, password: str,
                 content_cap: int = _DEFAULT_CAP,
                 options: Optional[dict] = None) -> Iterable[SourceObject]:
    """Best-effort iCloud pull via pyicloud: Photos, Drive files, and Contacts.
    ``options.includeCategories`` (photos/files/contacts) filters what's captured.
    Interactive-2FA accounts can't be synced headlessly. Messages aren't exposed
    by any iCloud API and can't be captured."""
    options = options or {}

    def want(cat: str) -> bool:
        inc = options.get("includeCategories") or []
        return not inc or cat in inc

    try:
        from pyicloud import PyiCloudService  # optional dependency
    except Exception:
        logger.info("pyicloud not installed; skipping iCloud live pull")
        return
    try:
        api = PyiCloudService(username, password)
    except Exception as exc:
        logger.warning("iCloud auth failed: %s", exc)
        return
    if getattr(api, "requires_2fa", False) or getattr(api, "requires_2sa", False):
        logger.info("iCloud account requires interactive 2FA; cannot sync headlessly")
        return

    # Photos
    if want("photos"):
        try:
            for photo in api.photos.all:
                name = getattr(photo, "filename", None) or f"photo-{getattr(photo, 'id', '')}"
                size = int(getattr(photo, "size", 0) or 0)
                raw = b""
                if 0 < size <= content_cap:
                    try:
                        resp = photo.download()
                        raw = resp.raw.read(content_cap + 1) if resp else b""
                    except Exception:
                        raw = b""
                content, backed = _capped(raw, content_cap) if raw else (
                    json.dumps({"_arkive": "no_content", "bytes": size}).encode(), False)
                kind = "video" if str(name).lower().endswith((".mov", ".mp4")) else "image"
                yield SourceObject(
                    object_id=f"icloud:photo:{getattr(photo, 'id', name)}",
                    doc_type=kind, category=kind if kind == "video" else "image",
                    title=name, content=content, preview=f"{size // 1000} KB",
                    meta={"album": "Photos", "kind": kind, "content_backed_up": backed},
                    labels=["Photos"], size_bytes=size or None)  # type: ignore
        except Exception as exc:
            logger.info("iCloud Photos unavailable: %s", exc)

    # Contacts
    if want("contacts"):
        try:
            for person in (api.contacts.all() or []):
                cid = person.get("contactId") or person.get("phones", [{}])[0].get("field", "")
                name = " ".join(filter(None, [person.get("firstName"), person.get("lastName")])) or "Contact"
                content = json.dumps(person).encode()
                content, backed = _capped(content, content_cap)
                yield SourceObject(
                    object_id=f"icloud:contact:{cid}",
                    doc_type="person", category="contact", title=name,
                    content=content, preview=name,
                    meta={"album": "Contacts", "kind": "contact", "content_backed_up": backed},
                    labels=["Contacts"],
                )
        except Exception as exc:
            logger.info("iCloud contacts unavailable: %s", exc)

    # Drive files
    if want("files"):
        try:
            drive = api.drive
            for name in drive.dir():
                node = drive[name]
                if getattr(node, "type", "") == "file":
                    size = int(getattr(node, "size", 0) or 0)
                    raw = b""
                    if size <= content_cap:
                        try:
                            with node.open(stream=True) as resp:
                                raw = resp.raw.read(content_cap + 1)
                        except Exception:
                            raw = b""
                    _cat, _kind = classify_file(name)
                    content, backed = _capped(raw, content_cap) if raw else (
                        json.dumps({"_arkive": "no_content", "bytes": size}).encode(), False)
                    yield SourceObject(
                        object_id=f"icloud:drive:{name}",
                        doc_type=_kind, category=_cat, title=name,
                        content=content, preview=f"{size // 1000} KB",
                        meta={"path": f"/{name}", "album": "iCloud Drive",
                              "kind": _kind, "content_backed_up": backed},
                        labels=["iCloud Drive"], size_bytes=size or None,  # type: ignore
                    )
        except Exception as exc:
            logger.info("iCloud Drive unavailable: %s", exc)


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


# --------------------------------------------------------------------------- #
# Google Contacts + Calendar (People / Calendar API)                          #
# --------------------------------------------------------------------------- #

def _download(url: str, cap: int, headers: Optional[dict] = None) -> Tuple[bytes, bool]:
    """Download a media URL, honoring the per-object cap. Returns (content, backed)."""
    try:
        with httpx.Client(timeout=60, follow_redirects=True) as c:
            r = c.get(url, headers=headers or {})
            if r.status_code >= 400:
                return json.dumps({"_arkive": "no_content"}).encode(), False
            return _capped(r.content, cap)
    except Exception:
        return json.dumps({"_arkive": "no_content"}).encode(), False


def fetch_google_contacts(access_token: str,
                          content_cap: int = _DEFAULT_CAP) -> Iterable[SourceObject]:
    headers = {"Authorization": f"Bearer {access_token}"}
    fields = "names,emailAddresses,phoneNumbers,organizations,addresses,birthdays,biographies"
    url = "https://people.googleapis.com/v1/people/me/connections"
    with httpx.Client(timeout=60) as c:
        token: Optional[str] = None
        while True:
            params = {"personFields": fields, "pageSize": 1000}
            if token:
                params["pageToken"] = token
            r = c.get(url, headers=headers, params=params)
            if r.status_code >= 400:
                logger.warning("Google People API %s: %s (enable the People API in the "
                               "Google Cloud project and grant contacts.readonly)",
                               r.status_code, r.text[:300])
                return
            body = r.json()
            for p in body.get("connections", []):
                name = ((p.get("names") or [{}])[0]).get("displayName") or "Contact"
                emails = [e.get("value") for e in (p.get("emailAddresses") or []) if e.get("value")]
                phones = [ph.get("value") for ph in (p.get("phoneNumbers") or []) if ph.get("value")]
                org = ((p.get("organizations") or [{}])[0]).get("name", "")
                rid = (p.get("resourceName") or name).split("/")[-1]
                yield SourceObject(
                    object_id=f"google_contacts:{rid}",
                    doc_type="person", category="contact", title=name,
                    content=json.dumps(p).encode(),
                    preview=", ".join(emails + phones)[:140] or org,
                    meta={"emails": emails, "phones": phones, "org": org, "kind": "contact"},
                    labels=["Contacts"])
            token = body.get("nextPageToken")
            if not token:
                break


def fetch_google_calendar(access_token: str,
                          content_cap: int = _DEFAULT_CAP) -> Iterable[SourceObject]:
    headers = {"Authorization": f"Bearer {access_token}"}
    base = "https://www.googleapis.com/calendar/v3"
    with httpx.Client(timeout=60) as c:
        cals = c.get(f"{base}/users/me/calendarList", headers=headers)
        if cals.status_code >= 400:
            logger.warning("Google Calendar API %s: %s (enable the Calendar API in the "
                           "Google Cloud project and grant calendar.readonly)",
                           cals.status_code, cals.text[:300])
            return
        for cal in cals.json().get("items", []):
            cal_id = cal.get("id")
            cal_name = cal.get("summary", "Calendar")
            token: Optional[str] = None
            while True:
                params = {"singleEvents": "true", "maxResults": 2500, "orderBy": "startTime"}
                if token:
                    params["pageToken"] = token
                r = c.get(f"{base}/calendars/{cal_id}/events", headers=headers, params=params)
                if r.status_code >= 400:
                    break
                body = r.json()
                for ev in body.get("items", []):
                    start = (ev.get("start") or {})
                    when = start.get("dateTime") or start.get("date") or ""
                    summary = ev.get("summary") or "(no title)"
                    yield SourceObject(
                        object_id=f"google_calendar:{ev.get('id')}",
                        doc_type="event", category="calendar", title=summary,
                        content=json.dumps(ev).encode(),
                        preview=f"{when} · {ev.get('location', '')}".strip(" ·"),
                        meta={"calendar": cal_name, "start": when,
                              "location": ev.get("location"),
                              "organizer": (ev.get("organizer") or {}).get("email"),
                              "kind": "event"},
                        labels=[cal_name])
                token = body.get("nextPageToken")
                if not token:
                    break


_PICKER = "https://photospicker.googleapis.com/v1"


def create_picker_session(access_token: str) -> dict:
    """Create a Google Photos Picker session; returns the pickerUri the user opens."""
    with httpx.Client(timeout=30) as c:
        r = c.post(f"{_PICKER}/sessions",
                   headers={"Authorization": f"Bearer {access_token}"}, json={})
        if r.status_code >= 400:
            raise RuntimeError(f"{r.status_code} {r.text[:400]}")
        return r.json()


def get_picker_session(access_token: str, session_id: str) -> dict:
    """Poll a picker session; ``mediaItemsSet`` becomes true once the user picks."""
    with httpx.Client(timeout=30) as c:
        r = c.get(f"{_PICKER}/sessions/{session_id}",
                  headers={"Authorization": f"Bearer {access_token}"})
        if r.status_code >= 400:
            raise RuntimeError(f"{r.status_code} {r.text[:400]}")
        return r.json()


def delete_picker_session(access_token: str, session_id: str) -> None:
    try:
        with httpx.Client(timeout=15) as c:
            c.delete(f"{_PICKER}/sessions/{session_id}",
                     headers={"Authorization": f"Bearer {access_token}"})
    except Exception:
        pass


def iter_picker_media(access_token: str, session_id: str) -> Iterable[dict]:
    """Yield the raw media items the user picked in a session (paginated)."""
    headers = {"Authorization": f"Bearer {access_token}"}
    with httpx.Client(timeout=60) as c:
        page: Optional[str] = None
        while True:
            params = {"sessionId": session_id, "pageSize": 100}
            if page:
                params["pageToken"] = page
            r = c.get(f"{_PICKER}/mediaItems", headers=headers, params=params)
            if r.status_code >= 400:
                logger.warning("Picker mediaItems %s: %s", r.status_code, r.text[:300])
                return
            body = r.json()
            for it in body.get("mediaItems", []):
                yield it
            page = body.get("nextPageToken")
            if not page:
                break


def picker_item_to_object(item: dict, content_cap: int, access_token: str) -> SourceObject:
    """Download one picked media item's bytes and normalize it. Picker baseUrls
    require the bearer token (unlike the old Library API)."""
    mf = item.get("mediaFile") or {}
    mime = mf.get("mimeType", "")
    is_video = (item.get("type") == "VIDEO") or mime.startswith("video/")
    base_url = mf.get("baseUrl", "")
    dl = f"{base_url}={'dv' if is_video else 'd'}" if base_url else ""
    headers = {"Authorization": f"Bearer {access_token}"}
    content, backed = _download(dl, content_cap, headers=headers) if dl else (b"", False)
    fmeta = mf.get("mediaFileMetadata") or {}
    ct = item.get("createTime")
    return SourceObject(
        object_id=f"google_photos:{item.get('id')}",
        doc_type="video" if is_video else "photo", category="photo",
        title=mf.get("filename") or "Photo", content=content,
        preview=f"{mime} · {fmeta.get('width', '')}x{fmeta.get('height', '')}".strip(" ·"),
        meta={"filename": mf.get("filename"), "mime": mime,
              "kind": "video" if is_video else "photo", "created": ct,
              "content_backed_up": backed},
        labels=["Google Photos"], size_bytes=len(content) or 0)


# --------------------------------------------------------------------------- #
# Social: Reddit, Facebook, Instagram                                         #
# --------------------------------------------------------------------------- #
def _want(options: Optional[dict], category: str) -> bool:
    """True when a content category is selected (empty selection = include all)."""
    inc = (options or {}).get("includeCategories") or []
    return not inc or category in inc


def fetch_reddit(access_token: str, content_cap: int = _DEFAULT_CAP,
                 options: Optional[dict] = None) -> Iterable[SourceObject]:
    ua = "web:life.arkive:v1 (Arkive backup)"
    headers = {"Authorization": f"Bearer {access_token}", "User-Agent": ua}
    base = "https://oauth.reddit.com"
    with httpx.Client(timeout=60) as c:
        me = c.get(f"{base}/api/v1/me", headers=headers)
        if me.status_code >= 400:
            return
        user = me.json().get("name", "")

        def _listing(path: str, kind: str, label: str):
            after = None
            while True:
                params = {"limit": 100}
                if after:
                    params["after"] = after
                r = c.get(f"{base}{path}", headers=headers, params=params)
                if r.status_code >= 400:
                    break
                data = r.json().get("data", {})
                for child in data.get("children", []):
                    d = child.get("data", {})
                    title = d.get("title") or (d.get("body") or "")[:80] or "(reddit)"
                    yield SourceObject(
                        object_id=f"reddit:{d.get('name') or d.get('id')}",
                        doc_type=kind, category="social", title=title,
                        content=json.dumps(d).encode(),
                        preview=(d.get("selftext") or d.get("body") or d.get("url") or "")[:200],
                        meta={"subreddit": d.get("subreddit"), "score": d.get("score"),
                              "permalink": d.get("permalink"), "kind": kind},
                        labels=[label] + ([f"r/{d.get('subreddit')}"] if d.get("subreddit") else []))
                after = data.get("after")
                if not after:
                    break

        if _want(options, "posts"):
            yield from _listing(f"/user/{user}/submitted", "post", "Posts")
        if _want(options, "comments"):
            yield from _listing(f"/user/{user}/comments", "comment", "Comments")
        if _want(options, "saved"):
            yield from _listing(f"/user/{user}/saved", "post", "Saved")
        if _want(options, "messages"):
            r = c.get(f"{base}/message/inbox", headers=headers, params={"limit": 100})
            if r.status_code < 400:
                for child in r.json().get("data", {}).get("children", []):
                    d = child.get("data", {})
                    yield SourceObject(
                        object_id=f"reddit:msg:{d.get('name') or d.get('id')}",
                        doc_type="message", category="message",
                        title=d.get("subject") or "(message)",
                        content=json.dumps(d).encode(),
                        preview=(d.get("body") or "")[:200],
                        meta={"from": d.get("author"), "kind": "message"},
                        labels=["Messages"])


def fetch_facebook(access_token: str, content_cap: int = _DEFAULT_CAP,
                   options: Optional[dict] = None) -> Iterable[SourceObject]:
    base = "https://graph.facebook.com/v19.0"
    with httpx.Client(timeout=60) as c:
        def _page(path: str, params: dict):
            url: Optional[str] = f"{base}{path}"
            p: Optional[dict] = {**params, "access_token": access_token, "limit": 100}
            while url:
                r = c.get(url, params=p)
                if r.status_code >= 400:
                    return
                body = r.json()
                for item in body.get("data", []):
                    yield item
                url = (body.get("paging") or {}).get("next")
                p = None  # 'next' is a full URL

        if _want(options, "posts"):
            for post in _page("/me/posts", {"fields": "id,message,created_time,permalink_url,full_picture"}):
                msg = post.get("message") or "(post)"
                yield SourceObject(
                    object_id=f"facebook:{post.get('id')}",
                    doc_type="post", category="social", title=msg[:80],
                    content=json.dumps(post).encode(), preview=msg[:200],
                    meta={"created": post.get("created_time"),
                          "permalink": post.get("permalink_url"), "kind": "post"},
                    labels=["Posts"])
        if _want(options, "photos"):
            for photo in _page("/me/photos", {"type": "uploaded",
                                              "fields": "id,name,created_time,images,link"}):
                images = photo.get("images") or []
                src = images[0].get("source") if images else None
                content, backed = _download(src, content_cap) if src else (
                    json.dumps(photo).encode(), False)
                yield SourceObject(
                    object_id=f"facebook:photo:{photo.get('id')}",
                    doc_type="image", category="image",
                    title=(photo.get("name") or "Photo")[:80],
                    content=content, preview=photo.get("link") or "photo",
                    meta={"created": photo.get("created_time"), "link": photo.get("link"),
                          "kind": "image", "content_backed_up": backed},
                    labels=["Photos"])


def fetch_instagram(access_token: str, content_cap: int = _DEFAULT_CAP,
                    options: Optional[dict] = None) -> Iterable[SourceObject]:
    base = "https://graph.instagram.com"
    fields = "id,caption,media_type,media_url,thumbnail_url,permalink,timestamp"
    with httpx.Client(timeout=60) as c:
        url: Optional[str] = f"{base}/me/media"
        params: Optional[dict] = {"fields": fields, "access_token": access_token, "limit": 100}
        while url:
            r = c.get(url, params=params)
            if r.status_code >= 400:
                return
            body = r.json()
            for m in body.get("data", []):
                mtype = (m.get("media_type") or "IMAGE").upper()
                kind = "video" if mtype == "VIDEO" else "image"
                if not _want(options, "media"):
                    continue
                media_url = m.get("media_url") or m.get("thumbnail_url")
                content, backed = _download(media_url, content_cap) if media_url else (
                    json.dumps(m).encode(), False)
                yield SourceObject(
                    object_id=f"instagram:{m.get('id')}",
                    doc_type=kind, category=kind if kind == "video" else "image",
                    title=(m.get("caption") or "Instagram media")[:80],
                    content=content, preview=m.get("permalink") or "",
                    meta={"created": m.get("timestamp"), "permalink": m.get("permalink"),
                          "media_type": mtype, "kind": kind, "content_backed_up": backed},
                    labels=["Instagram"])
            url = (body.get("paging") or {}).get("next")
            params = None


def fetch_linkedin(access_token: str, content_cap: int = _DEFAULT_CAP,
                   options: Optional[dict] = None) -> Iterable[SourceObject]:
    """Back up the member's LinkedIn profile (OpenID Connect userinfo) and, when
    the app has Community Management / member-social access, their posts. Posts
    are best-effort and silently skipped when the scope isn't granted."""
    headers = {"Authorization": f"Bearer {access_token}"}
    sub = None
    with httpx.Client(timeout=60) as c:
        try:
            r = c.get("https://api.linkedin.com/v2/userinfo", headers=headers)
            if r.status_code < 400:
                d = r.json()
                sub = d.get("sub")
                if _want(options, "profile"):
                    name = d.get("name") or "LinkedIn profile"
                    yield SourceObject(
                        object_id=f"linkedin:profile:{sub or name}",
                        doc_type="profile", category="social", title=name,
                        content=json.dumps(d).encode(),
                        preview=d.get("email") or name,
                        meta={"email": d.get("email"),
                              "locale": d.get("locale"), "kind": "profile"},
                        labels=["Profile"])
        except Exception:
            pass
        if sub and _want(options, "posts"):
            try:
                hdr = {**headers, "X-Restli-Protocol-Version": "2.0.0"}
                params = {"q": "authors",
                          "authors": f"List(urn:li:person:{sub})", "count": 50}
                rp = c.get("https://api.linkedin.com/v2/ugcPosts", headers=hdr, params=params)
                if rp.status_code < 400:
                    for post in rp.json().get("elements", []):
                        share = (((post.get("specificContent") or {})
                                  .get("com.linkedin.ugc.ShareContent") or {})
                                 .get("shareCommentary") or {})
                        text = share.get("text") or "(post)"
                        yield SourceObject(
                            object_id=f"linkedin:{post.get('id')}",
                            doc_type="post", category="social", title=text[:80],
                            content=json.dumps(post).encode(), preview=text[:200],
                            meta={"created": (post.get("created") or {}).get("time"),
                                  "kind": "post"}, labels=["Posts"])
            except Exception:
                pass


