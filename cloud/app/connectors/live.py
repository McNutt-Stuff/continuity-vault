"""
Live data fetchers that pull real objects from provider APIs using an OAuth
access token. Returned as normalized ``SourceObject`` records for the sync
worker. Kept separate from the connector definitions so the API wiring stays
small and the simulated fallbacks remain for local/demo use.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from email import message_from_bytes
from email.header import decode_header, make_header
from typing import Iterable, List, Optional, Tuple

import httpx

from .base import SourceObject
from .ratelimit import RateLimiter, RateLimitExceeded
from ..taxonomy import classify_file, map_1password

logger = logging.getLogger("cv.connectors.live")


def _parse_dt(val: object) -> Optional[datetime]:
    """Best-effort parse of a provider timestamp into a NAIVE-UTC datetime: an
    existing datetime, ISO 8601 (with Z or ±HHMM offsets), or epoch seconds/ms.
    A date-only string ("2026-08-22") parses too. None when unknown. Naive-UTC so
    it stores + displays consistently (the API/frontend treat bare times as UTC)."""
    dt: Optional[datetime]
    if val is None or val == "":
        return None
    if isinstance(val, datetime):
        dt = val
    elif isinstance(val, (int, float)):
        ts = float(val)
        dt = datetime.fromtimestamp(ts / 1000.0 if ts > 1e12 else ts, tz=timezone.utc)
    else:
        s = str(val).strip()
        if s.isdigit():
            ts = float(s)
            dt = datetime.fromtimestamp(ts / 1000.0 if ts > 1e12 else ts, tz=timezone.utc)
        else:
            s = s.replace("Z", "+00:00")
            s = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", s)  # +0000 -> +00:00
            try:
                dt = datetime.fromisoformat(s)
            except ValueError:
                return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


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
        modified_at=_parse_dt(m.get("internalDate")),
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


def _gmail_since_query(since_date: str) -> str:
    """Gmail ``after:YYYY/MM/DD`` search filter from an ISO date (empty if unset)."""
    d = (since_date or "").strip()[:10]
    if not d:
        return ""
    try:
        y, m, day = d.split("-")
        return f"after:{int(y)}/{int(m)}/{int(day)}"
    except Exception:
        return ""


# Messages ingested per resumable full-backfill chunk (the job loop resumes the
# next chunk from the saved pageToken until the whole mailbox is captured).
_GMAIL_BACKFILL_CHUNK = 1000


def stream_gmail(access_token: str, cursor: Optional[dict] = None,
                 max_messages: int = 5000, content_cap: int = _DEFAULT_CAP,
                 options: Optional[dict] = None, state: Optional[dict] = None,
                 mode: str = "recent"):
    """Two-track Gmail pull (bounded memory — yields one message at a time):

      * ``mode="recent"`` — history-delta from the stored ``historyId`` (fast,
        runs on the schedule). The first recent run just records the mailbox head
        as a watermark; new mail flows from there forward.
      * ``mode="backfill"`` — pages *backwards* through the mailbox (bounded to
        messages older than enrollment via ``before:``) in resumable chunks so
        full history is captured concurrently with the recent track. Sets
        ``state['done']`` when the whole history has been ingested.

    An optional ``sinceDate`` sets a floor on how deep the backfill goes.
    """
    headers = {"Authorization": f"Bearer {access_token}"}
    cursor = cursor or {}
    options = options or {}
    exclude = {str(f).upper() for f in (options.get("excludeFolders") or [])}
    include_spam_trash = bool(options.get("includeSpamTrash")) and not (exclude & {"SPAM", "TRASH"})
    base_query_parts = [_GMAIL_EXCLUDE_QUERY[f] for f in exclude if f in _GMAIL_EXCLUDE_QUERY]

    def _emit(c, mid):
        o = _gmail_message(c, headers, mid, content_cap)
        if not o:
            return None
        if exclude and (set(o.meta.get("labelIds", [])) & exclude):
            return None
        return o

    with httpx.Client(timeout=60) as c:
        if mode == "backfill":
            yield from _stream_gmail_backfill(
                c, headers, cursor, state, base_query_parts, include_spam_trash,
                options, _emit)
            return

        # ---- recent / delta track ----
        history_id = cursor.get("history_id")
        if not history_id:
            # First recent run: record the mailbox head as a watermark. The
            # backfill track captures the current inbox + full history; new mail
            # is picked up from this point forward on subsequent runs.
            if state is not None:
                state["cursor"] = {"history_id": _gmail_history_id(c, headers)}
            return
        try:
            ids = _gmail_history_ids(c, headers, str(history_id), max_messages)
        except _HistoryGone:
            logger.info("gmail history %s expired; resetting the recent watermark "
                        "(older mail is covered by the backfill track)", history_id)
            ids = []
        for mid in ids:
            o = _emit(c, mid)
            if o:
                yield o
        if state is not None:
            state["cursor"] = {"history_id": _gmail_history_id(c, headers)}


def _stream_gmail_backfill(c, headers, cursor, state, base_query_parts,
                           include_spam_trash, options, emit):
    """Page the mailbox backwards (newest→oldest) in one resumable chunk. Runs
    independently of the recent track and covers the whole mailbox (content-hash
    dedup makes any overlap with recent a no-op); ``sinceDate`` sets a floor."""
    if cursor.get("done"):
        if state is not None:
            state["cursor"] = cursor
            state["done"] = True
        return
    query_parts = list(base_query_parts)
    since_q = _gmail_since_query(options.get("sinceDate") or "")
    if since_q:
        query_parts.append(since_q)
    query = " ".join(query_parts)

    next_token = cursor.get("page_token")
    emitted = 0
    exhausted = False
    while emitted < _GMAIL_BACKFILL_CHUNK:
        params: dict = {"maxResults": 500}
        if query:
            params["q"] = query
        if include_spam_trash:
            params["includeSpamTrash"] = "true"
        if next_token:
            params["pageToken"] = next_token
        r = c.get(f"{GMAIL}/messages", headers=headers, params=params)
        r.raise_for_status()
        data = r.json()
        for ref in data.get("messages", []):
            o = emit(c, ref["id"])
            if o:
                yield o
                emitted += 1
        next_token = data.get("nextPageToken")
        if not next_token:
            exhausted = True
            break
    if state is not None:
        if exhausted:
            state["cursor"] = {"done": True}
            state["done"] = True
        else:
            state["cursor"] = {"page_token": next_token, "has_more": True}


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
                    modified_at=_parse_dt(m.get("receivedDateTime")),
                )
            url, params = body.get("@odata.nextLink"), None


def _graph_iso(since_date: str) -> str:
    """ISO-8601 instant for a Graph ``receivedDateTime`` filter from an ISO date."""
    d = (since_date or "").strip()
    if not d:
        return ""
    return d if len(d) > 10 else f"{d[:10]}T00:00:00Z"


# Messages ingested per resumable Outlook chunk.
_OUTLOOK_CHUNK = 200


def _outlook_obj(c: "httpx.Client", headers: dict, m: dict, content_cap: int) -> SourceObject:
    sender = (m.get("from") or {}).get("emailAddress", {}).get("address", "")
    mime = c.get(f"https://graph.microsoft.com/v1.0/me/messages/{m['id']}/$value", headers=headers)
    raw = mime.content if mime.status_code < 400 else b""
    content, backed = _capped(raw, content_cap)
    return SourceObject(
        object_id=f"outlook:{m['id']}",
        doc_type="email",
        title=m.get("subject") or "(no subject)",
        content=content,
        preview=(m.get("bodyPreview") or "")[:200],
        meta={"from": sender, "folder": "Inbox", "webLink": m.get("webLink"),
              "content_backed_up": backed},
        labels=["Inbox"],
        size_bytes=len(raw) or None,  # type: ignore
        modified_at=_parse_dt(m.get("receivedDateTime")),
    )


def stream_outlook(access_token: str, cursor: Optional[dict] = None,
                   content_cap: int = _DEFAULT_CAP, options: Optional[dict] = None,
                   state: Optional[dict] = None, mode: str = "recent"):
    """Two-track Outlook/Graph mailbox pull:

      * ``mode="recent"`` — pulls mail newer than the stored watermark (fast,
        scheduled). The first recent run just records the watermark.
      * ``mode="backfill"`` — pages the whole mailbox *backwards* (newest-first)
        in resumable chunks so full history is captured concurrently. Honors an
        optional ``sinceDate`` floor and sets ``state['done']`` when exhausted.
    """
    headers = {"Authorization": f"Bearer {access_token}"}
    cursor = cursor or {}
    options = options or {}
    select = "id,subject,from,bodyPreview,receivedDateTime,parentFolderId,webLink"

    with httpx.Client(timeout=120) as c:
        if mode == "backfill":
            if cursor.get("done"):
                if state is not None:
                    state["cursor"], state["done"] = cursor, True
                return
            scan = cursor.get("scan") or {}
            if scan.get("next_link"):
                url: Optional[str] = scan["next_link"]
                params: Optional[dict] = None
            else:
                since_iso = _graph_iso(options.get("sinceDate") or "")
                url = "https://graph.microsoft.com/v1.0/me/messages"
                params = {"$top": 50, "$select": select, "$orderby": "receivedDateTime desc"}
                if since_iso:
                    params["$filter"] = f"receivedDateTime ge {since_iso}"
            emitted = 0
            exhausted = False
            while url and emitted < _OUTLOOK_CHUNK:
                r = c.get(url, headers=headers, params=params)
                r.raise_for_status()
                body = r.json()
                for m in body.get("value", []):
                    yield _outlook_obj(c, headers, m, content_cap)
                    emitted += 1
                url, params = body.get("@odata.nextLink"), None
                if not url:
                    exhausted = True
                    break
            if state is not None:
                if exhausted or not url:
                    state["cursor"], state["done"] = {"done": True}, True
                else:
                    state["cursor"] = {"scan": {"next_link": url}, "has_more": True}
            return

        # ---- recent / delta track ----
        last_seen = cursor.get("last_seen")
        if not last_seen:
            # First recent run: record a watermark; the backfill track captures
            # history and new mail flows from here forward.
            if state is not None:
                state["cursor"] = {"last_seen": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
            return
        url = "https://graph.microsoft.com/v1.0/me/messages"
        params = {"$top": 50, "$select": select, "$orderby": "receivedDateTime desc",
                  "$filter": f"receivedDateTime gt {last_seen}"}
        newest = last_seen
        emitted = 0
        while url and emitted < _OUTLOOK_CHUNK:
            r = c.get(url, headers=headers, params=params)
            r.raise_for_status()
            body = r.json()
            vals = body.get("value", [])
            if vals:
                newest = max(newest, vals[0].get("receivedDateTime") or newest)
            for m in vals:
                yield _outlook_obj(c, headers, m, content_cap)
                emitted += 1
            url, params = body.get("@odata.nextLink"), None
        if state is not None:
            state["cursor"] = {"last_seen": newest}


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
                    modified_at=_parse_dt(it.get("lastModifiedDateTime")),
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
                    modified_at=_parse_dt(it.get("server_modified") or it.get("client_modified")),
                )
            if not body.get("has_more"):
                break
            r = c.post("https://api.dropboxapi.com/2/files/list_folder/continue",
                       headers=headers, content=json.dumps({"cursor": body.get("cursor")}))
            r.raise_for_status()
            body = r.json()


# --------------------------------------------------------------------------- #
# Cloud file browsing + chunked/delta streaming (Dropbox, OneDrive)           #
# --------------------------------------------------------------------------- #

_FILE_CHUNK = 1200  # files ingested per resumable crawl chunk

# Selectable "root folder" token: back up the files directly in the account root
# only (NOT its subfolders — those are selected separately). Kept in sync with
# the frontend folder picker (web/src/pages/Mappings.tsx). Safe as a sentinel: it
# can't collide with a real Dropbox path_lower ("/…"), OneDrive relative path, or
# Google Drive folder id.
ROOT_SENTINEL = "__root__"
# A selected folder prefixed with this is backed up non-recursively (its top-level
# files only, not subfolders). ``ROOT_SENTINEL`` is the account-root shorthand.
FLAT_PREFIX = "flat:"


def dropbox_list_folders(access_token: str, path: str = "") -> List[dict]:
    """Immediate child folders of ``path`` (Dropbox path_lower, "" = root)."""
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    out: List[dict] = []
    with httpx.Client(timeout=30) as c:
        cursor: Optional[str] = None
        while True:
            if cursor is None:
                r = c.post("https://api.dropboxapi.com/2/files/list_folder", headers=headers,
                           content=json.dumps({"path": path or "", "recursive": False, "limit": 2000}))
            else:
                r = c.post("https://api.dropboxapi.com/2/files/list_folder/continue",
                           headers=headers, content=json.dumps({"cursor": cursor}))
            if r.status_code >= 400:
                break
            body = r.json()
            for it in body.get("entries", []):
                if it.get(".tag") == "folder":
                    out.append({"path": it.get("path_lower") or it.get("path_display"),
                                "name": it.get("name"), "hasMore": True})
            if not body.get("has_more"):
                break
            cursor = body.get("cursor")
    return sorted(out, key=lambda f: f["name"].lower())


def _dropbox_object(c: httpx.Client, auth: dict, it: dict, cap: int) -> SourceObject:
    _cat, _kind = classify_file(it.get("name", ""))
    size = int(it.get("size", 0))
    raw = b""
    if 0 < size <= cap:
        try:
            dr = c.post("https://content.dropboxapi.com/2/files/download",
                        headers={**auth, "Dropbox-API-Arg": json.dumps({"path": it.get("path_lower")})})
            raw = dr.content if dr.status_code < 400 else b""
        except Exception:
            raw = b""
    content, backed = _capped(raw, cap) if raw else (
        json.dumps({"_arkive": "content_exceeds_cap" if size > cap else "no_content",
                    "bytes": size}).encode(), False)
    return SourceObject(
        object_id=f"dropbox:{it.get('id', it['path_lower'])}",
        doc_type=_kind, category=_cat, title=it.get("name", "file"),
        content=content, preview=f"{size // 1000} KB · {it.get('path_display', '')}",
        meta={"path": it.get("path_display"), "rev": it.get("rev"),
              "content_backed_up": backed},
        labels=["/".join(it.get("path_display", "/").split("/")[:-1]) or "/"],
        size_bytes=size or None,  # type: ignore
        modified_at=_parse_dt(it.get("client_modified") or it.get("server_modified")),
        content_hash=it.get("content_hash"))


def stream_dropbox(access_token: str, cursor=None, config: Optional[dict] = None,
                   state: Optional[dict] = None, content_cap: int = _DEFAULT_CAP) -> Iterable[SourceObject]:
    """Chunked, resumable, delta Dropbox pull. Backs up only the selected folders
    (``config['roots']`` of path_lower; empty = whole Dropbox). Each call ingests
    up to ``_FILE_CHUNK`` files then persists a per-root cursor; later runs use the
    stored cursor to fetch only changes (Dropbox delta)."""
    config = config or {}
    state = state if state is not None else {}
    roots = config.get("roots") or [""]
    cur = cursor if isinstance(cursor, dict) else {}
    # Only keep cursors for the currently-selected folders so a narrowed selection
    # never keeps delta-pulling a previously-wider scope (e.g. the whole drive).
    rmap: dict = {r: c for r, c in (cur.get("roots") or {}).items() if r in roots}
    logger.info("dropbox stream: crawling %d folder(s): %s", len(roots),
                roots if roots != [""] else ["(whole Dropbox)"])
    emitted = 0
    stopped_early = False
    auth = {"Authorization": f"Bearer {access_token}"}
    hdr = {**auth, "Content-Type": "application/json"}
    with httpx.Client(timeout=120) as c:
        for root in roots:
            if emitted >= _FILE_CHUNK:
                stopped_early = True
                break
            # The "root files only" sentinel lists the account root non-recursively
            # so its top-level files are backed up without pulling every subfolder.
            # "flat:<path>" does the same for any specific folder.
            if root == ROOT_SENTINEL:
                dbx_path, recursive = "", False
            elif root.startswith(FLAT_PREFIX):
                dbx_path, recursive = root[len(FLAT_PREFIX):], False
            else:
                dbx_path, recursive = root, True
            dbx_cursor = rmap.get(root)
            while True:
                if dbx_cursor is None:
                    r = c.post("https://api.dropboxapi.com/2/files/list_folder", headers=hdr,
                               content=json.dumps({"path": dbx_path, "recursive": recursive,
                                                   "limit": 2000, "include_deleted": True}))
                else:
                    r = c.post("https://api.dropboxapi.com/2/files/list_folder/continue",
                               headers=hdr, content=json.dumps({"cursor": dbx_cursor}))
                if r.status_code >= 400:
                    # A reset/expired cursor: restart this root from scratch once.
                    if dbx_cursor is not None:
                        dbx_cursor = None
                        rmap[root] = None
                        continue
                    break
                body = r.json()
                for it in body.get("entries", []):
                    if it.get(".tag") != "file":
                        continue
                    yield _dropbox_object(c, auth, it, content_cap)
                    emitted += 1
                dbx_cursor = body.get("cursor")
                rmap[root] = dbx_cursor
                if not body.get("has_more"):
                    break
                if emitted >= _FILE_CHUNK:
                    stopped_early = True
                    break
    state["cursor"] = {"roots": rmap, "has_more": stopped_early}


def onedrive_list_folders(access_token: str, path: str = "") -> List[dict]:
    """Immediate child folders of ``path`` (relative to the drive root)."""
    headers = {"Authorization": f"Bearer {access_token}"}
    rel = (path or "").strip("/")
    url: Optional[str] = (f"https://graph.microsoft.com/v1.0/me/drive/root:/{rel}:/children"
                          if rel else "https://graph.microsoft.com/v1.0/me/drive/root/children")
    params: Optional[dict] = {"$select": "id,name,folder,parentReference", "$top": 200}
    out: List[dict] = []
    with httpx.Client(timeout=30) as c:
        while url:
            r = c.get(url, headers=headers, params=params)
            if r.status_code >= 400:
                break
            body = r.json()
            for it in body.get("value", []):
                if not it.get("folder"):
                    continue
                name = it.get("name", "")
                full = f"{rel}/{name}".strip("/") if rel else name
                out.append({"path": full, "name": name,
                            "hasMore": int((it.get("folder") or {}).get("childCount", 0)) > 0})
            url, params = body.get("@odata.nextLink"), None
    return sorted(out, key=lambda f: f["name"].lower())


def _onedrive_object(c: httpx.Client, headers: dict, it: dict, cap: int) -> SourceObject:
    mime = (it.get("file") or {}).get("mimeType", "application/octet-stream")
    path = (it.get("parentReference") or {}).get("path", "/drive/root:")
    size = int(it.get("size", 0))
    _cat, _kind = classify_file(it.get("name", ""), mime)
    dl = it.get("@microsoft.graph.downloadUrl")
    raw = b""
    if dl and 0 < size <= cap:
        try:
            raw = c.get(dl).content
        except Exception:
            raw = b""
    content, backed = _capped(raw, cap) if raw else (
        json.dumps({"_arkive": "content_exceeds_cap" if size > cap else "no_content",
                    "bytes": size}).encode(), False)
    return SourceObject(
        object_id=f"onedrive:{it['id']}", doc_type=_kind, category=_cat,
        title=it.get("name", "file"), content=content,
        preview=f"{mime} · {size // 1000} KB",
        meta={"mime": mime, "path": f"{path}/{it.get('name')}", "content_backed_up": backed},
        labels=[path.split(":")[-1] or "/"], size_bytes=size or None,  # type: ignore
        modified_at=_parse_dt(it.get("lastModifiedDateTime")))


def stream_onedrive(access_token: str, cursor=None, config: Optional[dict] = None,
                    state: Optional[dict] = None, content_cap: int = _DEFAULT_CAP) -> Iterable[SourceObject]:
    """Chunked, resumable, delta OneDrive pull via the Graph delta API. Backs up
    only the selected folders (``config['roots']`` relative to root; empty = whole
    drive). Persists a per-root delta link so later runs fetch only changes."""
    config = config or {}
    state = state if state is not None else {}
    roots = config.get("roots") or [""]
    cur = cursor if isinstance(cursor, dict) else {}
    # Only keep delta links for the currently-selected folders (see stream_dropbox).
    rmap: dict = {r: c for r, c in (cur.get("roots") or {}).items() if r in roots}
    logger.info("onedrive stream: crawling %d folder(s): %s", len(roots),
                roots if roots != [""] else ["(whole drive)"])
    emitted = 0
    stopped_early = False
    headers = {"Authorization": f"Bearer {access_token}"}
    select = ("id,name,size,file,folder,deleted,parentReference,"
              "lastModifiedDateTime,@microsoft.graph.downloadUrl")
    with httpx.Client(timeout=120) as c:
        for root in roots:
            if emitted >= _FILE_CHUNK:
                stopped_early = True
                break
            link = rmap.get(root)
            # "Root files only" sentinel / "flat:<path>": list a folder's immediate
            # children (non-recursive) and keep only files — no delta, so re-list
            # each run (cheap; content-hash dedup skips unchanged files).
            if root == ROOT_SENTINEL or root.startswith(FLAT_PREFIX):
                rel = "" if root == ROOT_SENTINEL else root[len(FLAT_PREFIX):].strip("/")
                base = (f"https://graph.microsoft.com/v1.0/me/drive/root:/{rel}:/children"
                        if rel else "https://graph.microsoft.com/v1.0/me/drive/root/children")
                url = link or base
                params = None if link else {"$select": select, "$top": 200}
                while url:
                    r = c.get(url, headers=headers, params=params)
                    if r.status_code >= 400:
                        rmap[root] = None
                        break
                    body = r.json()
                    for it in body.get("value", []):
                        if it.get("folder") or it.get("deleted") or not it.get("file"):
                            continue
                        yield _onedrive_object(c, headers, it, content_cap)
                        emitted += 1
                    nxt = body.get("@odata.nextLink")
                    if nxt:
                        rmap[root] = nxt
                        url, params = nxt, None
                        if emitted >= _FILE_CHUNK:
                            stopped_early = True
                            break
                        continue
                    rmap[root] = None  # children listing has no delta token
                    url = None
                continue
            rel = (root or "").strip("/")
            if link is None:
                url: Optional[str] = (f"https://graph.microsoft.com/v1.0/me/drive/root:/{rel}:/delta"
                                      if rel else "https://graph.microsoft.com/v1.0/me/drive/root/delta")
                params: Optional[dict] = {"$select": select, "$top": 200}
            else:
                url, params = link, None
            while url:
                r = c.get(url, headers=headers, params=params)
                if r.status_code >= 400:
                    if link is not None:  # stale delta link — restart this root
                        rmap[root] = None
                    break
                body = r.json()
                for it in body.get("value", []):
                    if it.get("folder") or it.get("deleted") or not it.get("file"):
                        continue
                    yield _onedrive_object(c, headers, it, content_cap)
                    emitted += 1
                nxt = body.get("@odata.nextLink")
                delta = body.get("@odata.deltaLink")
                if nxt:
                    rmap[root] = nxt
                    url, params = nxt, None
                    if emitted >= _FILE_CHUNK:
                        stopped_early = True
                        break
                    continue
                rmap[root] = delta or rmap.get(root)
                url = None
    state["cursor"] = {"roots": rmap, "has_more": stopped_early}


GDRIVE_API = "https://www.googleapis.com/drive/v3"
_GDRIVE_EXPORT = {
    "application/vnd.google-apps.document":
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.google-apps.spreadsheet":
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.google-apps.presentation":
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.google-apps.drawing": "image/png",
}


def drive_list_folders(access_token: str, path: str = "") -> List[dict]:
    """Immediate child folders of ``path`` (a Drive folder id; "" = My Drive root).
    Each returned ``path`` is the folder id the picker stores as a selected root."""
    headers = {"Authorization": f"Bearer {access_token}"}
    parent = path or "root"
    out: List[dict] = []
    with httpx.Client(timeout=30) as c:
        page_token: Optional[str] = None
        while True:
            params = {
                "q": (f"'{parent}' in parents and "
                      "mimeType='application/vnd.google-apps.folder' and trashed=false"),
                "fields": "nextPageToken,files(id,name)",
                "pageSize": 200, "orderBy": "name",
                "supportsAllDrives": "true", "includeItemsFromAllDrives": "true",
            }
            if page_token:
                params["pageToken"] = page_token
            r = c.get(f"{GDRIVE_API}/files", headers=headers, params=params)
            if r.status_code >= 400:
                logger.warning("drive folders %s: %s", r.status_code, r.text[:200])
                break
            body = r.json()
            for f in body.get("files", []):
                out.append({"path": f["id"], "name": f.get("name") or f["id"], "hasMore": True})
            page_token = body.get("nextPageToken")
            if not page_token:
                break
    return sorted(out, key=lambda f: f["name"].lower())


def _drive_object(c: httpx.Client, headers: dict, f: dict, cap: int) -> SourceObject:
    fid = f["id"]
    name = f.get("name", "file")
    mime = f.get("mimeType", "")
    size = int(f.get("size", 0) or 0)
    native = mime.startswith("application/vnd.google-apps.")
    raw = b""
    if native:
        export = _GDRIVE_EXPORT.get(mime)
        if export:
            try:
                er = c.get(f"{GDRIVE_API}/files/{fid}/export", headers=headers,
                           params={"mimeType": export})
                raw = er.content if er.status_code < 400 else b""
            except Exception:
                raw = b""
    elif 0 < size <= cap:
        try:
            dr = c.get(f"{GDRIVE_API}/files/{fid}", headers=headers,
                       params={"alt": "media", "supportsAllDrives": "true"})
            raw = dr.content if dr.status_code < 400 else b""
        except Exception:
            raw = b""
    content, backed = _capped(raw, cap) if raw else (
        json.dumps({"_arkive": "content_exceeds_cap" if size > cap else "no_content",
                    "bytes": size}).encode(), False)
    if native:
        _cat, _kind = "document", "document"
    else:
        _cat, _kind = classify_file(name, mime)
    return SourceObject(
        object_id=f"gdrive:{fid}", doc_type=_kind, category=_cat, title=name,
        content=content, preview=f"{mime} · {size // 1000} KB" if size else mime,
        meta={"mime": mime, "name": name, "native": native,
              "content_backed_up": backed},
        labels=[], size_bytes=size or None,  # type: ignore
        modified_at=_parse_dt(f.get("modifiedTime")),
        content_hash=f.get("md5Checksum"))


def stream_drive(access_token: str, cursor=None, config: Optional[dict] = None,
                 state: Optional[dict] = None, content_cap: int = _DEFAULT_CAP) -> Iterable[SourceObject]:
    """Chunked, resumable, delta Google Drive pull. Backs up only the selected
    folders (``config['roots']`` of folder ids; empty = whole My Drive). Walks the
    folder tree, persisting the pending frontier so large drives resume; a
    modifiedTime high-water mark makes later runs fetch only changed files."""
    config = config or {}
    state = state if state is not None else {}
    roots_cfg = config.get("roots") or ["root"]
    cur = cursor if isinstance(cursor, dict) else {}
    since = cur.get("since")  # ISO modifiedTime high-water from the last full pass
    # The "root files only" sentinel maps to My Drive root, walked non-recursively
    # (its subfolders are never queued) so only top-level files are captured;
    # "flat:<folderid>" does the same for any specific folder.
    norecurse = set(cur.get("norecurse") or [])
    roots: List[str] = []
    for r in roots_cfg:
        if r == ROOT_SENTINEL:
            roots.append("root")
            norecurse.add("root")
        elif r.startswith(FLAT_PREFIX):
            fid = r[len(FLAT_PREFIX):]
            roots.append(fid)
            norecurse.add(fid)
        else:
            roots.append(r)
    pending = cur.get("pending")
    if not isinstance(pending, list) or not pending:
        pending = list(roots)
    headers = {"Authorization": f"Bearer {access_token}"}
    logger.info("drive stream: crawling %d folder(s): %s (since=%s)",
                len(roots), roots if roots != ["root"] else ["(whole My Drive)"], since)
    emitted = 0
    stopped_early = False
    new_high = since
    with httpx.Client(timeout=120) as c:
        while pending:
            if emitted >= _FILE_CHUNK:
                stopped_early = True
                break
            folder_id = pending.pop(0)
            page_token: Optional[str] = None
            while True:
                params = {
                    "q": f"'{folder_id}' in parents and trashed=false",
                    "fields": ("nextPageToken,files(id,name,mimeType,size,"
                               "modifiedTime,md5Checksum)"),
                    "pageSize": 200,
                    "supportsAllDrives": "true", "includeItemsFromAllDrives": "true",
                }
                if page_token:
                    params["pageToken"] = page_token
                r = c.get(f"{GDRIVE_API}/files", headers=headers, params=params)
                if r.status_code >= 400:
                    logger.warning("drive list %s: %s", r.status_code, r.text[:200])
                    break
                body = r.json()
                for f in body.get("files", []):
                    if f.get("mimeType") == "application/vnd.google-apps.folder":
                        if folder_id not in norecurse:
                            pending.append(f["id"])
                        continue
                    mt = f.get("modifiedTime")
                    if since and mt and mt <= since:
                        continue  # delta: unchanged since the last full pass
                    yield _drive_object(c, headers, f, content_cap)
                    emitted += 1
                    if mt and (new_high is None or mt > new_high):
                        new_high = mt
                page_token = body.get("nextPageToken")
                if not page_token:
                    break
                if emitted >= _FILE_CHUNK:
                    stopped_early = True
                    break
    if stopped_early:
        # Mid-walk: keep the old high-water so the resumed pass sees one snapshot.
        state["cursor"] = {"since": since, "pending": pending,
                           "norecurse": list(norecurse), "has_more": True}
    else:
        # Full pass done — advance the high-water and clear the frontier.
        state["cursor"] = {"since": new_high or since, "pending": [],
                           "norecurse": list(norecurse), "has_more": False}



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
        # Surface as an auth failure so the sync worker flags the source
        # needs-reauth and notifies — not a silent empty (healthy-looking) pull.
        detail = (str(exc).strip() or exc.__class__.__name__)
        logger.warning("iCloud auth failed: %s", detail)
        raise PermissionError(
            f"iCloud authentication failed: {detail}. iCloud needs an app-specific "
            f"password (appleid.apple.com → Sign-In & Security) and can't sync an "
            f"account with interactive two-factor enabled.") from exc
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
                    labels=["Photos"], size_bytes=size or None,  # type: ignore
                    modified_at=_parse_dt(getattr(photo, "asset_date", None)
                                          or getattr(photo, "created", None)))
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
                        modified_at=_parse_dt(getattr(node, "date_modified", None)),
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
                    modified_at=_parse_dt(it.get("updated_at") or it.get("created_at")),
                )


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
    fields = "names,emailAddresses,phoneNumbers,organizations,addresses,birthdays,biographies,metadata"
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
                # People API: the contact's last-updated time (its closest "date").
                srcs = (p.get("metadata") or {}).get("sources") or []
                updated = next((s.get("updateTime") for s in srcs if s.get("updateTime")), None)
                yield SourceObject(
                    object_id=f"google_contacts:{rid}",
                    doc_type="person", category="contact", title=name,
                    content=json.dumps(p).encode(),
                    preview=", ".join(emails + phones)[:140] or org,
                    meta={"emails": emails, "phones": phones, "org": org, "kind": "contact"},
                    labels=["Contacts"],
                    modified_at=_parse_dt(updated))
            token = body.get("nextPageToken")
            if not token:
                break


def _event_object_date(ev: dict) -> Optional[datetime]:
    """Timeline date for a calendar event = its ORIGINAL / first-occurrence start
    (for a recurring series Google returns the master with start = first
    occurrence). Dating at the first occurrence — not the next one — keeps
    recurring events from dominating the top of search. Falls back to
    updated/created when there's no start."""
    start = ev.get("start") or {}
    dt = _parse_dt(start.get("dateTime") or start.get("date"))
    if dt is not None:
        return dt
    for cand in (ev.get("updated"), ev.get("created")):
        alt = _parse_dt(cand)
        if alt is not None:
            return alt
    return None


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
                # Don't expand recurring series into instances: a "repeats forever"
                # event would otherwise explode into thousands of objects (and get
                # dated at the expansion horizon). Store one event per series,
                # dated at its first occurrence.
                params = {"maxResults": 2500, "showDeleted": "false"}
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
                    recurring = bool(ev.get("recurrence"))
                    meta = {"calendar": cal_name, "start": when,
                            "location": ev.get("location"),
                            "organizer": (ev.get("organizer") or {}).get("email"),
                            "kind": "event"}
                    if recurring:  # only when true, so non-recurring events add no facet
                        meta["recurring"] = True
                    yield SourceObject(
                        object_id=f"google_calendar:{ev.get('id')}",
                        doc_type="event", category="calendar", title=summary,
                        content=json.dumps(ev).encode(),
                        preview=f"{when} · {ev.get('location', '')}".strip(" ·"),
                        meta=meta,
                        labels=[cal_name],
                        modified_at=_event_object_date(ev))
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
        labels=["Google Photos"], size_bytes=len(content) or 0,
        modified_at=_parse_dt(ct))


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
                    # Hash on durable fields only — score/comment counts mutate
                    # over time and would otherwise re-ingest the same item.
                    stable = f"{d.get('name') or d.get('id')}|{d.get('created_utc')}|" \
                             f"{d.get('title') or ''}|{d.get('selftext') or d.get('body') or ''}"
                    yield SourceObject(
                        object_id=f"reddit:{d.get('name') or d.get('id')}",
                        doc_type=kind, category="social", title=title,
                        content=json.dumps(d).encode(),
                        content_hash=hashlib.sha256(stable.encode()).hexdigest(),
                        preview=(d.get("selftext") or d.get("body") or d.get("url") or "")[:200],
                        meta={"subreddit": d.get("subreddit"), "score": d.get("score"),
                              "permalink": d.get("permalink"), "kind": kind},
                        labels=[label] + ([f"r/{d.get('subreddit')}"] if d.get("subreddit") else []),
                        modified_at=_parse_dt(d.get("created_utc")))
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
            for post in _page("/me/posts", {"fields": "id,message,created_time,permalink_url"}):
                msg = post.get("message") or "(post)"
                # Hash-stable content: only the post's own durable fields. (Graph
                # CDN fields like full_picture carry a rotating token, so including
                # them would re-create a "new version" of the same post every run.)
                stable = {"id": post.get("id"), "message": post.get("message"),
                          "created_time": post.get("created_time"),
                          "permalink_url": post.get("permalink_url")}
                yield SourceObject(
                    object_id=f"facebook:{post.get('id')}",
                    doc_type="post", category="social", title=msg[:80],
                    content=json.dumps(stable, sort_keys=True).encode(), preview=msg[:200],
                    meta={"created": post.get("created_time"),
                          "permalink": post.get("permalink_url"), "kind": "post"},
                    labels=["Posts"],
                    modified_at=_parse_dt(post.get("created_time")))
        if _want(options, "photos"):
            for photo in _page("/me/photos", {"type": "uploaded",
                                              "fields": "id,name,created_time,images,link"}):
                images = photo.get("images") or []
                src = images[0].get("source") if images else None
                # Downloaded image bytes are stable; the metadata fallback must be
                # too (images/link URLs rotate), so hash only durable fields.
                content, backed = _download(src, content_cap) if src else (
                    json.dumps({"id": photo.get("id"), "name": photo.get("name"),
                                "created_time": photo.get("created_time")}, sort_keys=True).encode(),
                    False)
                yield SourceObject(
                    object_id=f"facebook:photo:{photo.get('id')}",
                    doc_type="image", category="image",
                    title=(photo.get("name") or "Photo")[:80],
                    content=content, preview=photo.get("link") or "photo",
                    meta={"created": photo.get("created_time"), "link": photo.get("link"),
                          "kind": "image", "content_backed_up": backed},
                    labels=["Photos"],
                    modified_at=_parse_dt(photo.get("created_time")))


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
                    labels=["Instagram"],
                    modified_at=_parse_dt(m.get("timestamp")))
            url = (body.get("paging") or {}).get("next")
            params = None


def fetch_linkedin(access_token: str, content_cap: int = _DEFAULT_CAP,
                   options: Optional[dict] = None) -> Iterable[SourceObject]:
    """Back up as much of the member's LinkedIn account as the granted access
    allows: profile, a consolidated résumé, posts, messages and connections.

    LinkedIn's "Sign In" product only grants ``openid profile email`` (identity),
    so anything deeper (posts, messages, connection list, work history) needs
    LinkedIn partner access. Every section is therefore best-effort and silently
    skipped when its API/scope isn't available, without failing the backup."""
    headers = {"Authorization": f"Bearer {access_token}"}
    api_hdr = {**headers, "X-Restli-Protocol-Version": "2.0.0"}
    userinfo: dict = {}
    me: dict = {}
    sub = None
    with httpx.Client(timeout=60) as c:
        # OpenID Connect identity (always available with the base scopes).
        try:
            r = c.get("https://api.linkedin.com/v2/userinfo", headers=headers)
            if r.status_code < 400:
                userinfo = r.json()
                sub = userinfo.get("sub")
        except Exception:
            pass
        # Richer profile (localized name/headline/vanity) — needs r_liteprofile.
        try:
            rm = c.get("https://api.linkedin.com/v2/me", headers=api_hdr, params={
                "projection": "(id,localizedFirstName,localizedLastName,"
                              "localizedHeadline,vanityName)"})
            if rm.status_code < 400:
                me = rm.json()
                sub = sub or me.get("id")
        except Exception:
            pass

        person_urn = f"urn:li:person:{sub}" if sub else None
        name = (userinfo.get("name")
                or " ".join(filter(None, [me.get("localizedFirstName"),
                                          me.get("localizedLastName")]))
                or "LinkedIn member")

        # PROFILE — the member's identity card.
        if _want(options, "profile") and (userinfo or me):
            merged = {**me, **userinfo}
            yield SourceObject(
                object_id=f"linkedin:profile:{sub or name}",
                doc_type="profile", category="social", title=name,
                content=json.dumps(merged).encode(),
                preview=userinfo.get("email") or me.get("localizedHeadline") or name,
                meta={"email": userinfo.get("email"),
                      "headline": me.get("localizedHeadline"),
                      "vanity_name": me.get("vanityName"),
                      "locale": userinfo.get("locale"), "kind": "profile"},
                labels=["Profile"])

        # RÉSUMÉ — a consolidated professional document. Work history / education
        # / skills need r_basicprofile (partner); without it we still capture the
        # core identity, headline and public profile URL.
        if _want(options, "resume") and (userinfo or me):
            resume = {
                "name": name,
                "headline": me.get("localizedHeadline"),
                "email": userinfo.get("email"),
                "vanityName": me.get("vanityName"),
                "picture": userinfo.get("picture"),
                "locale": userinfo.get("locale"),
                "profileUrl": (f"https://www.linkedin.com/in/{me.get('vanityName')}"
                               if me.get("vanityName") else None),
            }
            yield SourceObject(
                object_id=f"linkedin:resume:{sub or name}",
                doc_type="resume", category="document",
                title=f"{name} — LinkedIn résumé",
                content=json.dumps(resume).encode(),
                preview=resume.get("headline") or name,
                meta={"headline": resume.get("headline"), "kind": "resume"},
                labels=["Resume"])

        # POSTS / ARTICLES — needs Community Management / member-social access.
        if person_urn and _want(options, "posts"):
            try:
                params = {"q": "authors", "authors": f"List({person_urn})", "count": 50}
                rp = c.get("https://api.linkedin.com/v2/ugcPosts", headers=api_hdr, params=params)
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
                                  "kind": "post"}, labels=["Posts"],
                            modified_at=_parse_dt((post.get("created") or {}).get("time")))
            except Exception:
                pass

        # MESSAGES — LinkedIn's Messaging API is partner-only; best-effort.
        if person_urn and _want(options, "messages"):
            try:
                r = c.get("https://api.linkedin.com/v2/messages", headers=api_hdr, params={
                    "q": "participants", "participants": f"List({person_urn})", "count": 50})
                if r.status_code < 400:
                    for msg in r.json().get("elements", []):
                        body = ((msg.get("body") or {}).get("text")
                                or (msg.get("eventContent") or {}).get("text") or "(message)")
                        yield SourceObject(
                            object_id=f"linkedin:msg:{msg.get('id') or msg.get('entityUrn')}",
                            doc_type="message", category="message",
                            title=(body[:80] or "LinkedIn message"),
                            content=json.dumps(msg).encode(), preview=body[:200],
                            meta={"created": msg.get("createdAt"), "kind": "message"},
                            labels=["Messages"],
                            modified_at=_parse_dt(msg.get("createdAt")))
            except Exception:
                pass

        # CONNECTIONS — count (r_1st_connections_size) + full list (r_network,
        # partner). Both best-effort.
        if person_urn and _want(options, "connections"):
            try:
                r = c.get(f"https://api.linkedin.com/v2/networkSizes/{person_urn}",
                          headers=api_hdr, params={"edgeType": "CONNECTION"})
                if r.status_code < 400:
                    d = r.json()
                    n = d.get("firstDegreeSize")
                    if n is not None:
                        yield SourceObject(
                            object_id=f"linkedin:connections:{sub}",
                            doc_type="contact", category="contact",
                            title=f"{n} LinkedIn connections",
                            content=json.dumps(d).encode(),
                            preview=f"{n} first-degree connections",
                            meta={"count": n, "kind": "connections"}, labels=["Connections"])
            except Exception:
                pass
            try:
                r = c.get("https://api.linkedin.com/v2/connections", headers=api_hdr,
                          params={"q": "viewer", "start": 0, "count": 100})
                if r.status_code < 400:
                    for conn in r.json().get("elements", []):
                        cname = " ".join(filter(None, [conn.get("localizedFirstName"),
                                                       conn.get("localizedLastName")])) or "Connection"
                        yield SourceObject(
                            object_id=f"linkedin:conn:{conn.get('id') or cname}",
                            doc_type="contact", category="contact", title=cname,
                            content=json.dumps(conn).encode(), preview=cname,
                            meta={"kind": "connection"}, labels=["Connections"])
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# GitHub (repositories: files + issues/PRs)                                    #
# --------------------------------------------------------------------------- #
GITHUB_API = "https://api.github.com"
# Stop making calls with this much quota left so we never fully exhaust the
# hourly budget (leaves room for the folder picker / other sources).
_GH_FLOOR = 50
# How long the crawl will sleep inline for a throttle before deferring the rest
# of the work to the next chunk/cycle. Sized to absorb GitHub's typical ~60s
# secondary-rate-limit Retry-After in-place instead of pausing the whole crawl.
_GH_INLINE_WAIT = 75


def _gh_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "Arkive-backup"}


def _gh_limiter(floor: int = _GH_FLOOR, max_wait: int = _GH_INLINE_WAIT) -> RateLimiter:
    return RateLimiter(name="GitHub", floor=floor, max_wait_seconds=max_wait)


def github_list_repos(access_token: str, path: str = "") -> List[dict]:
    """The repos a user can back up, returned as selectable 'folders' for the
    picker (each repo is a leaf — pick whole repos). Raises on a rate limit so
    the picker surfaces it instead of silently returning a short list."""
    headers = _gh_headers(access_token)
    # Interactive path: never sleep long — raise immediately if throttled so the
    # UI shows a clear "try again later" message.
    limiter = _gh_limiter(floor=0, max_wait=0)
    out: List[dict] = []
    with httpx.Client(timeout=30) as c:
        page = 1
        while page <= 20:  # safety cap (~2000 repos)
            r = limiter.get(c, f"{GITHUB_API}/user/repos", headers=headers, params={
                "per_page": 100, "page": page, "sort": "full_name",
                "affiliation": "owner,collaborator,organization_member"})
            if r.status_code == 401:
                raise RuntimeError("GitHub authorization expired (401) — reconnect this source")
            if r.status_code >= 400:
                logger.warning("GitHub repos %s: %s", r.status_code, r.text[:200])
                raise RuntimeError(f"GitHub API error {r.status_code}")
            repos = r.json()
            if not isinstance(repos, list) or not repos:
                break
            for repo in repos:
                fn = repo.get("full_name")
                if fn:
                    out.append({"path": fn, "name": fn, "hasMore": False})
            if len(repos) < 100:
                break
            page += 1
    return sorted(out, key=lambda f: f["name"].lower())


def _github_repos(c: httpx.Client, headers: dict, roots: List[str],
                  limiter: RateLimiter, status=None) -> Iterable[dict]:
    """Yield the repo objects to back up: the selected ones, or all accessible.
    Rate-limit aware (may raise RateLimitExceeded when the wait exceeds budget)."""
    if roots:
        for fn in roots:
            r = limiter.get(c, f"{GITHUB_API}/repos/{fn}", headers=headers, status=status)
            if r.status_code == 401:
                raise RuntimeError("GitHub authorization expired (401) — reconnect this source")
            if r.status_code < 400:
                yield r.json()
            else:
                logger.warning("GitHub repo %s: %s", fn, r.status_code)
        return
    page = 1
    while page <= 20:
        r = limiter.get(c, f"{GITHUB_API}/user/repos", headers=headers, status=status, params={
            "per_page": 100, "page": page,
            "affiliation": "owner,collaborator,organization_member"})
        if r.status_code == 401:
            raise RuntimeError("GitHub authorization expired (401) — reconnect this source")
        if r.status_code >= 400:
            logger.warning("GitHub repos list %s: %s", r.status_code, r.text[:200])
            break
        repos = r.json()
        if not isinstance(repos, list) or not repos:
            break
        yield from repos
        if len(repos) < 100:
            break
        page += 1


def stream_github(access_token: str, cursor=None, config: Optional[dict] = None,
                  state: Optional[dict] = None, content_cap: int = _DEFAULT_CAP) -> Iterable[SourceObject]:
    """Back up selected GitHub repos — their files (via the recursive git tree)
    plus issues/PRs. ``config['roots']`` = selected repo full_names (empty = all).
    A per-repo pushed_at cursor skips repos unchanged since the last sync.

    Rate-limit aware: paces against GitHub's hourly budget and, when the quota
    runs low, either waits briefly or stops the chunk early and records a
    ``resume_after`` so the background job loop waits for the reset and resumes —
    keeping calls under the limit without failing the backup."""
    config = config or {}
    state = state if state is not None else {}
    roots = config.get("roots") or []
    inc = config.get("includeCategories") or []
    want_code = (not inc) or ("code" in inc)
    want_issues = (not inc) or ("issues" in inc)
    status = config.get("_status")  # job status reporter (optional)
    cur = cursor if isinstance(cursor, dict) else {}
    seen_at: dict = dict(cur.get("repos") or {})
    # Per-repo resume progress: {full_name: {"files": <blobs done>, "issue_page": <next page>}}.
    # Lets a repo with more files than one chunk continue where it left off instead
    # of re-crawling (and re-throttling on) the same first files every chunk.
    progress: dict = dict(cur.get("progress") or {})
    headers = _gh_headers(access_token)
    limiter = _gh_limiter()
    emitted = 0
    stopped_early = False
    throttled_reset: Optional[float] = None
    logger.info("github stream: %s (code=%s issues=%s)",
                roots if roots else "(all repos)", want_code, want_issues)

    def gh_get(url: str, **kw):
        return limiter.get(c, url, status=status, **kw)

    with httpx.Client(timeout=120) as c:
        fn = None
        current_done = 0
        issue_page = 1
        try:
            for repo in _github_repos(c, headers, roots, limiter, status):
                if stopped_early:
                    break
                fn = repo.get("full_name")
                pushed = repo.get("pushed_at") or ""
                default_branch = repo.get("default_branch") or "main"
                repo_mod = _parse_dt(pushed)
                # Delta: skip a repo whose head hasn't moved since the last full sync.
                if fn and seen_at.get(fn) == pushed:
                    continue

                # Resume point within this repo (0 files / page 1 on a fresh crawl).
                rp = progress.get(fn) or {}
                files_done = int(rp.get("files", 0) or 0)
                issue_page = int(rp.get("issue_page", 1) or 1)
                current_done = files_done

                yield SourceObject(
                    object_id=f"github:repo:{fn}", doc_type="repository", category="developer",
                    title=fn or "repository",
                    content=json.dumps({
                        "full_name": fn, "description": repo.get("description"),
                        "private": repo.get("private"), "html_url": repo.get("html_url"),
                        "language": repo.get("language"), "default_branch": default_branch,
                        "pushed_at": pushed}).encode(),
                    preview=repo.get("description") or fn or "",
                    meta={"repo": fn, "language": repo.get("language"),
                          "private": repo.get("private"), "kind": "repository",
                          "url": repo.get("html_url")},
                    labels=[fn] if fn else [], modified_at=repo_mod)
                emitted += 1

                if want_code and fn:
                    tr = gh_get(f"{GITHUB_API}/repos/{fn}/git/trees/{default_branch}",
                                headers=headers, params={"recursive": "1"})
                    if tr.status_code < 400:
                        blobs = [n for n in (tr.json().get("tree") or [])
                                 if n.get("type") == "blob"]
                        # Skip files already ingested in a prior chunk (no re-fetch,
                        # no quota spent) and continue from there.
                        for idx, node in enumerate(blobs, start=1):
                            if idx <= files_done:
                                continue
                            if emitted >= _FILE_CHUNK:
                                stopped_early = True
                                break
                            fp = node.get("path", "")
                            size = int(node.get("size", 0) or 0)
                            raw = b""
                            if 0 < size <= content_cap:
                                br = gh_get(f"{GITHUB_API}/repos/{fn}/git/blobs/{node.get('sha')}",
                                            headers=headers)
                                if br.status_code < 400:
                                    b = br.json()
                                    try:
                                        raw = (base64.b64decode(b.get("content", ""))
                                               if b.get("encoding") == "base64"
                                               else (b.get("content") or "").encode())
                                    except Exception:
                                        raw = b""
                            content, backed = _capped(raw, content_cap) if raw else (
                                json.dumps({"_arkive": "content_exceeds_cap" if size > content_cap
                                            else "no_content", "bytes": size}).encode(), False)
                            _cat, _kind = classify_file(fp)
                            yield SourceObject(
                                object_id=f"github:{fn}:{fp}", doc_type=_kind, category=_cat,
                                title=fp.split("/")[-1] or fp, content=content,
                                preview=f"{fn} · {fp}",
                                meta={"repo": fn, "path": fp, "branch": default_branch,
                                      "content_backed_up": backed},
                                labels=[fn], size_bytes=size or None,  # type: ignore
                                content_hash=node.get("sha"), modified_at=repo_mod)
                            emitted += 1
                            current_done = idx

                if want_issues and fn and not stopped_early:
                    while issue_page <= 10:
                        if emitted >= _FILE_CHUNK:
                            stopped_early = True
                            break
                        ir = gh_get(f"{GITHUB_API}/repos/{fn}/issues", headers=headers,
                                    params={"state": "all", "per_page": 100, "page": issue_page})
                        if ir.status_code >= 400:
                            break
                        issues = ir.json()
                        if not isinstance(issues, list) or not issues:
                            break
                        for iss in issues:
                            is_pr = "pull_request" in iss
                            title = iss.get("title") or "(issue)"
                            yield SourceObject(
                                object_id=f"github:{'pr' if is_pr else 'issue'}:{fn}#{iss.get('number')}",
                                doc_type="pull_request" if is_pr else "issue", category="developer",
                                title=f"#{iss.get('number')} {title}",
                                content=json.dumps(iss).encode(),
                                preview=(iss.get("body") or "")[:200],
                                meta={"repo": fn, "number": iss.get("number"),
                                      "state": iss.get("state"),
                                      "author": (iss.get("user") or {}).get("login"),
                                      "kind": "pull_request" if is_pr else "issue",
                                      "url": iss.get("html_url")},
                                labels=[fn, iss.get("state") or "open"],
                                modified_at=_parse_dt(iss.get("updated_at") or iss.get("created_at")))
                            emitted += 1
                        if len(issues) < 100:
                            break
                        issue_page += 1

                # Persist per-repo progress: mark the repo fully done only when the
                # crawl reached the end (so deltas skip it next time); otherwise
                # remember where to resume so we advance instead of restarting.
                if fn:
                    if stopped_early:
                        progress[fn] = {"files": current_done, "issue_page": issue_page}
                    else:
                        seen_at[fn] = pushed
                        progress.pop(fn, None)
        except RateLimitExceeded as exc:
            # Hourly budget hit and the reset is further out than we'll wait inline:
            # stop this chunk, remember when the window resets, and let the job loop
            # wait + resume. Objects already yielded are ingested; not an error.
            throttled_reset = exc.reset_at or (datetime.now(timezone.utc).timestamp() + 60)
            stopped_early = True
            # Keep the in-flight repo's resume point so it advances (not restarts)
            # when the window reopens.
            if fn:
                progress[fn] = {"files": current_done, "issue_page": issue_page}
            logger.warning("github throttled — deferring rest of backup: %s", exc)
            if status:
                status("Paused for GitHub rate limit — will resume automatically")
    cursor_out = {"repos": seen_at, "progress": progress, "has_more": stopped_early}
    if throttled_reset:
        cursor_out["resume_after"] = throttled_reset
    state["cursor"] = cursor_out


