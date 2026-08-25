"""
Local Microsoft Outlook collector (macOS).

Reads the on-device Outlook profile (``Outlook.sqlite`` plus the per-message
``.olk15MsgSource`` MIME files) and emits emails, their attachments, contacts,
calendar events and notes — everything Outlook stores locally, so it's backed up
even for accounts that aren't reachable in the cloud.

Outlook's schema differs across versions, so the reader is deliberately
defensive: it introspects the table/column names and maps them heuristically,
degrading to "nothing collected" rather than failing if the layout is unfamiliar.

For each email we attach the raw RFC-822 source (``.olk15MsgSource``) when it can
be located — that gives full, searchable content and lets the unified-search
email viewer render headers, body and attachments.
"""

from __future__ import annotations

import base64
import gzip
import html
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("arkive")

_GROUP = Path.home() / "Library" / "Group Containers" / "UBF8T346G9.Office" / "Outlook"
_APPLE_EPOCH = 978307200  # Mac absolute time base (2001-01-01) in Unix seconds
_MAX_ROWS = 20000         # per record type, safety cap
_MAX_MIME_BYTES = 100 * 1024 * 1024


def _profiles() -> List[Path]:
    base = _GROUP / "Outlook 15 Profiles"
    if not base.exists():
        return []
    return [p / "Data" for p in base.iterdir() if (p / "Data" / "Outlook.sqlite").exists()]


def available() -> bool:
    return bool(_profiles())


def _copy_ro(src: Path) -> str:
    import shutil
    import tempfile
    fd, tmp = tempfile.mkstemp(prefix="arkive-olk-", suffix=".sqlite")
    os.close(fd)
    shutil.copy2(src, tmp)
    for ext in ("-wal", "-shm"):
        side = Path(str(src) + ext)
        if side.exists():
            try:
                shutil.copy2(side, tmp + ext)
            except Exception:
                pass
    return tmp


def _rm(path: str) -> None:
    for p in (path, path + "-wal", path + "-shm"):
        try:
            os.remove(p)
        except OSError:
            pass


def _iso(ts) -> Optional[str]:
    if ts in (None, "", 0):
        return None
    try:
        v = float(ts)
    except (TypeError, ValueError):
        return None
    # Outlook stores Mac absolute time (seconds since 2001); some builds use ms.
    if v > 1e12:
        v /= 1000.0
    unix = v + _APPLE_EPOCH if v < 1e9 else v
    try:
        return datetime.fromtimestamp(unix, tz=timezone.utc).isoformat()
    except Exception:
        return None


def _cols(con: sqlite3.Connection, table: str) -> List[str]:
    try:
        return [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]
    except sqlite3.Error:
        return []


def _first(row: sqlite3.Row, cols: List[str], *needles: str) -> Optional[str]:
    """Value of the first column whose name contains any needle (case-insensitive)."""
    low = {c.lower(): c for c in cols}
    for needle in needles:
        for lc, orig in low.items():
            if needle in lc:
                v = row[orig]
                if v not in (None, ""):
                    return v
    return None


def _tables(con: sqlite3.Connection) -> List[str]:
    try:
        return [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
    except sqlite3.Error:
        return []


def _mime_index(data_dir: Path) -> Dict[str, Path]:
    """Map an Outlook record id -> its .olk15MsgSource path (raw RFC-822)."""
    idx: Dict[str, Path] = {}
    msgs = data_dir / "Messages"
    if not msgs.exists():
        return idx
    try:
        for p in msgs.rglob("*.olk15MsgSource"):
            # Filenames look like Message_<recordId>.olk15MsgSource
            stem = p.stem
            rid = stem.split("_")[-1] if "_" in stem else stem
            idx[rid] = p
    except Exception:
        pass
    return idx


def _read_mime(path: Path) -> Optional[bytes]:
    try:
        if path.stat().st_size > _MAX_MIME_BYTES:
            return None
        return path.read_bytes()
    except OSError:
        return None


def _obj(object_id: str, kind: str, title: str, content: bytes, preview: str,
         meta: dict, labels: List[str]) -> dict:
    return {
        "object_id": object_id, "kind": kind, "title": title,
        "content_b64": base64.b64encode(content).decode(),
        "preview": preview[:200], "meta": meta, "labels": labels,
        "size_bytes": len(content),
    }


def _json(obj) -> bytes:
    import json
    return json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")


# --------------------------------------------------------------------------- #
# Per-record-type extractors (heuristic, schema-tolerant)                      #
# --------------------------------------------------------------------------- #

def _collect_mail(con, cols, table, mime_idx, out, seen):
    for row in con.execute(f'SELECT rowid AS _arkive_rid, * FROM "{table}" LIMIT {_MAX_ROWS}'):
        rid = str(_first(row, cols, "record_recordid", "recordid", "record_id") or row["_arkive_rid"])
        if f"mail:{rid}" in seen:
            continue
        seen.add(f"mail:{rid}")
        subject = _first(row, cols, "subject") or "(no subject)"
        sender = _first(row, cols, "senderlist", "sender", "from") or ""
        to = _first(row, cols, "torecipient", "recipientlist", "displayto", "to") or ""
        when = _iso(_first(row, cols, "timereceived", "timesent", "received", "sent", "time"))
        preview = _first(row, cols, "preview", "snippet") or ""
        mime = _read_mime(mime_idx[rid]) if rid in mime_idx else None
        content = mime if mime else _json({"subject": subject, "from": sender,
                                           "to": to, "preview": preview, "date": when})
        out.append(_obj(
            f"outlook_local:mail:{rid}", "email", subject, content,
            f"{sender} · {preview}".strip(" ·"),
            {"kind": "email", "from": sender, "to": to, "date": when,
             "folder": _first(row, cols, "folder"), "has_mime": bool(mime),
             "content_backed_up": bool(mime), "modified": when},
            ["Outlook", "Mail"]))


def _collect_contacts(con, cols, table, out, seen):
    for row in con.execute(f'SELECT rowid AS _arkive_rid, * FROM "{table}" LIMIT {_MAX_ROWS}'):
        rid = str(_first(row, cols, "record_recordid", "recordid", "record_id") or row["_arkive_rid"])
        if f"contact:{rid}" in seen:
            continue
        seen.add(f"contact:{rid}")
        name = (_first(row, cols, "displayname", "fullname", "title", "name")
                or _first(row, cols, "firstname") or "(contact)")
        email = _first(row, cols, "email") or ""
        phone = _first(row, cols, "phone", "mobile", "number") or ""
        org = _first(row, cols, "company", "organization", "org") or ""
        rec = {"name": name, "email": email, "phone": phone, "org": org}
        out.append(_obj(
            f"outlook_local:contact:{rid}", "contact", name, _json(rec),
            " · ".join(x for x in (email, phone, org) if x),
            {"kind": "contact", "email": email, "phone": phone, "org": org},
            ["Outlook", "Contacts"]))


def _collect_calendar(con, cols, table, out, seen):
    for row in con.execute(f'SELECT rowid AS _arkive_rid, * FROM "{table}" LIMIT {_MAX_ROWS}'):
        rid = str(_first(row, cols, "record_recordid", "recordid", "record_id") or row["_arkive_rid"])
        if f"event:{rid}" in seen:
            continue
        seen.add(f"event:{rid}")
        title = _first(row, cols, "subject", "title", "summary") or "(event)"
        start = _iso(_first(row, cols, "starttime", "start", "eventstart"))
        end = _iso(_first(row, cols, "endtime", "end", "eventend"))
        loc = _first(row, cols, "location") or ""
        rec = {"title": title, "start": start, "end": end, "location": loc}
        out.append(_obj(
            f"outlook_local:event:{rid}", "event", title, _json(rec),
            f"{start or ''} · {loc}".strip(" ·"),
            {"kind": "event", "start": start, "location": loc, "modified": start},
            ["Outlook", "Calendar"]))


def _collect_notes(con, cols, table, out, seen):
    for row in con.execute(f'SELECT rowid AS _arkive_rid, * FROM "{table}" LIMIT {_MAX_ROWS}'):
        rid = str(_first(row, cols, "record_recordid", "recordid", "record_id") or row["_arkive_rid"])
        if f"note:{rid}" in seen:
            continue
        seen.add(f"note:{rid}")
        title = _first(row, cols, "title", "subject") or "(note)"
        body = _first(row, cols, "preview", "body", "content", "plaintext") or ""
        rec = {"title": title, "body": body}
        out.append(_obj(
            f"outlook_local:note:{rid}", "note", title, _json(rec),
            body[:160], {"kind": "note"}, ["Outlook", "Notes"]))


# --------------------------------------------------------------------------- #
# New Outlook for Mac (EFM store)                                              #
# --------------------------------------------------------------------------- #
# "New Outlook" leaves the legacy Mail table empty and instead stores each
# message body as gzipped HTML under Files/S0/<n>/EFMData/<id>.dat. The envelope
# (From/To/Subject/Sent) is rendered inside that HTML as a header block, so we
# parse it back out; the raw block store that would hold it structurally is
# opaque. We back up the body + recovered headers as a viewable email.

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_HDR_RE = {
    "from": re.compile(r"<b>\s*From:\s*</b>\s*(.*?)\s*<", re.I | re.S),
    "to": re.compile(r"<b>\s*To:\s*</b>\s*(.*?)\s*<", re.I | re.S),
    "subject": re.compile(r"<b>\s*Subject:\s*</b>\s*(.*?)\s*<", re.I | re.S),
    "date": re.compile(r"<b>\s*(?:Sent|Date):\s*</b>\s*(.*?)\s*<", re.I | re.S),
}
_DATE_FORMATS = (
    "%A, %d %B %Y at %H:%M", "%A, %B %d, %Y at %H:%M",
    "%A, %d %B %Y %H:%M:%S", "%A, %B %d, %Y %H:%M:%S",
    "%A, %d %B %Y %H:%M", "%A, %B %d, %Y %H:%M",
    "%A, %B %d, %Y %I:%M %p", "%A, %d %B %Y %I:%M %p",
)


def _html_to_text(html_str: str) -> str:
    text = html.unescape(_TAG_RE.sub(" ", html_str))
    return _WS_RE.sub(" ", text).strip()


def _hdr(html_str: str, key: str) -> str:
    m = _HDR_RE[key].search(html_str)
    if not m:
        return ""
    return _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", m.group(1)))).strip()


def _parse_hdr_date(s: str) -> Optional[str]:
    s = (s or "").strip()
    if not s:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            continue
    return None


def _iso_mtime(p: Path) -> Optional[str]:
    try:
        return datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).isoformat()
    except OSError:
        return None


def _wrap_html_email(subject: str, frm: str, to: str, when: Optional[str],
                     date_text: str, html_body: str) -> bytes:
    hdr = []
    if frm:
        hdr.append(f"From: {frm}")
    if to:
        hdr.append(f"To: {to}")
    hdr.append(f"Subject: {subject}")
    if date_text or when:
        hdr.append(f"Date: {date_text or when}")
    hdr += ["MIME-Version: 1.0", "Content-Type: text/html; charset=utf-8"]
    return ("\r\n".join(hdr) + "\r\n\r\n" + html_body).encode("utf-8", "replace")


def _efm_dirs(profile_root: Path) -> List[Path]:
    files = profile_root / "Files"
    if not files.exists():
        return []
    return [d for d in files.rglob("EFMData") if d.is_dir()]


def _collect_new_outlook(profile_root: Path, out: List[dict], seen: set) -> Tuple[int, int]:
    """Returns (messages, with_headers)."""
    n = with_hdr = 0
    for efm in _efm_dirs(profile_root):
        for p in sorted(efm.glob("*.dat")):
            oid = f"outlook_local:mail:efm:{p.stem}"
            if oid in seen:
                continue
            try:
                if p.stat().st_size > _MAX_MIME_BYTES:
                    continue
                body = gzip.decompress(p.read_bytes()).decode("utf-8", "replace")
            except (OSError, EOFError, gzip.BadGzipFile):
                continue
            text = _html_to_text(body)
            if not text:
                continue
            seen.add(oid)
            subject = _hdr(body, "subject")
            frm = _hdr(body, "from")
            to = _hdr(body, "to")
            date_text = _hdr(body, "date")
            when = _parse_hdr_date(date_text) or _iso_mtime(p)
            if subject or frm:
                with_hdr += 1
            title = subject or (text[:80].strip() or "Outlook message")
            preview = (f"{frm} · " if frm else "") + text[:180]
            out.append(_obj(
                oid, "email", title,
                _wrap_html_email(title, frm, to, when, date_text, body), preview,
                {"kind": "email", "from": frm, "to": to, "date": when,
                 "date_text": date_text, "folder": "", "has_mime": True,
                 "content_backed_up": True, "source": "new-outlook",
                 "meta_source": "header" if subject else "body", "modified": when},
                ["Outlook", "Mail"]))
            n += 1
            if n >= _MAX_ROWS:
                return n, with_hdr
    return n, with_hdr


def collect(config: Optional[dict] = None,
            state: Optional[dict] = None) -> Tuple[List[dict], dict]:
    """Collect Outlook mail/contacts/calendar/notes from every local profile.
    Dedup + versioning are handled server-side by the content hash, so a full
    read each run is safe (state is reserved for future incremental support)."""
    config = config or {}
    inc = config.get("includeCategories") or []  # optional Data Map filter

    def _want(cat: str) -> bool:
        return (not inc) or (cat in inc)

    out: List[dict] = []
    seen: set = set()
    profiles = _profiles()
    log.info("outlook_local: scanning %d local profile(s) [filter=%s]",
             len(profiles), inc or "all")
    for data_dir in profiles:
        db = data_dir / "Outlook.sqlite"
        tmp = _copy_ro(db)
        log.info("outlook_local: reading profile %s", data_dir)
        counts = {"mail": 0, "contacts": 0, "calendar": 0, "notes": 0}
        try:
            con = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            mime_idx = _mime_index(data_dir) if _want("mail") else {}
            log.info("outlook_local: %d table(s); %d .olk15MsgSource file(s) indexed",
                     len(_tables(con)), len(mime_idx))
            inventory: List[str] = []
            for table in _tables(con):
                cols = _cols(con, table)
                if not cols:
                    continue
                t = table.lower()
                try:
                    nrows = con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                except sqlite3.Error:
                    nrows = -1
                if nrows:
                    inventory.append(f"{table}({nrows})")
                before = len(out)
                cat = None
                try:
                    if _want("mail") and ("mail" in t or "message" in t):
                        cat = "mail"
                        _collect_mail(con, cols, table, mime_idx, out, seen)
                    elif _want("contacts") and "contact" in t:
                        cat = "contacts"
                        _collect_contacts(con, cols, table, out, seen)
                    elif _want("calendar") and ("calendar" in t or "appointment" in t
                                                or "event" in t):
                        cat = "calendar"
                        _collect_calendar(con, cols, table, out, seen)
                    elif _want("notes") and ("note" in t):
                        cat = "notes"
                        _collect_notes(con, cols, table, out, seen)
                except sqlite3.Error as exc:
                    log.debug("outlook_local: table %s skipped: %s", table, exc)
                emitted = len(out) - before
                if cat:
                    counts[cat] += emitted
                    log.info("outlook_local: table %-30s → %-8s rows=%s emitted=%d",
                             table, cat, nrows, emitted)
            # One compact line lists every non-empty table so an unfamiliar schema
            # (why a category came back empty) is diagnosable without verbose mode.
            log.info("outlook_local: table inventory: %s", ", ".join(inventory) or "(none)")
            con.close()
            # New Outlook fallback: the legacy Mail table is empty, so pull the
            # readable message bodies (and headers parsed from them) from EFM.
            if _want("mail") and counts["mail"] == 0:
                efm, with_hdr = _collect_new_outlook(data_dir.parent, out, seen)
                counts["mail"] += efm
                if efm:
                    log.warning("outlook_local: legacy Mail table empty (New Outlook) — "
                                "recovered %d message(s) from EFM, %d with Subject/Sender/Date "
                                "parsed from the message header. Some are thread-derived; "
                                "switch Outlook to Legacy for authoritative envelopes.",
                                efm, with_hdr)
                else:
                    log.info("outlook_local: no EFM message bodies found either")
        except Exception as exc:  # noqa: BLE001
            log.warning("outlook_local: profile %s unreadable: %s", data_dir, exc)
        finally:
            _rm(tmp)
        log.info("outlook_local: profile summary — mail=%d contacts=%d calendar=%d notes=%d",
                 counts["mail"], counts["contacts"], counts["calendar"], counts["notes"])

    log.info("outlook_local: collected %d object(s)", len(out))
    return out, (state or {})
