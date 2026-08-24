"""
Apple iMessage / SMS collector (macOS).

Reads the local Messages database (``~/Library/Messages/chat.db``) and emits:

  * one object per message (kind ``message``) carrying its text, direction,
    the sender/recipients (phone number or email + resolved contact name when
    available), the chat/thread id (so a whole conversation can be reassembled),
    the service (iMessage/SMS) and a list of its attachments; and
  * one object per attachment (kind derived from its type — image/video/audio/
    file) carrying the actual file bytes, linked back to its message + chat.

Group conversations are included (participants are listed on every message).
Collection is incremental by message ROWID so later runs only push new messages.

The DB is copied to a temp file and opened read-only so a live Messages app
never blocks the read (and we never touch the original).
"""

from __future__ import annotations

import base64
import logging
import os
import re
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("arkive")

_CHAT_DB = Path.home() / "Library" / "Messages" / "chat.db"
_ADDRESSBOOK = Path.home() / "Library" / "Application Support" / "AddressBook" / "Sources"
# Apple's Core Data epoch (2001-01-01) in Unix seconds.
_APPLE_EPOCH = 978307200
_MAX_ATTACH_BYTES = 100 * 1024 * 1024  # skip pathologically large attachments
_MAX_MESSAGES = 50000                  # safety cap per run

_IMG = {"jpg", "jpeg", "png", "gif", "heic", "heif", "webp", "tiff", "bmp"}
_VID = {"mp4", "mov", "avi", "mkv", "webm", "m4v", "3gp"}
_AUD = {"mp3", "wav", "aac", "flac", "m4a", "caf", "amr"}


def available() -> bool:
    return _CHAT_DB.exists()


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #

def _apple_to_unix(ts) -> Optional[float]:
    """Convert a Messages timestamp (ns or s since 2001-01-01) to Unix seconds."""
    if not ts:
        return None
    ts = float(ts)
    if ts > 1e11:  # nanoseconds (modern macOS)
        ts /= 1e9
    return ts + _APPLE_EPOCH


def _iso(ts) -> Optional[str]:
    from datetime import datetime, timezone
    u = _apple_to_unix(ts)
    if u is None:
        return None
    try:
        return datetime.fromtimestamp(u, tz=timezone.utc).isoformat()
    except Exception:
        return None


def _kind_for(name: str) -> str:
    ext = Path(name or "").suffix.lstrip(".").lower()
    if ext in _IMG:
        return "image"
    if ext in _VID:
        return "video"
    if ext in _AUD:
        return "audio"
    if ext == "pdf":
        return "pdf"
    return "file"


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _attributed_text(blob: Optional[bytes]) -> str:
    """Best-effort extraction of message text from a macOS ``attributedBody``
    (an archived NSAttributedString) — modern Messages leaves ``text`` NULL and
    stores the body here. Returns "" when it can't be decoded."""
    if not blob:
        return ""
    try:
        i = blob.index(b"NSString")
    except ValueError:
        return ""
    j = i + len(b"NSString") + 1
    plus = blob.find(b"\x2b", j, j + 16)  # the '+' preceding the UTF-8 length
    if plus == -1:
        return ""
    j = plus + 1
    if j >= len(blob):
        return ""
    length = blob[j]
    j += 1
    if length == 0x81:            # 2-byte little-endian length follows
        length = int.from_bytes(blob[j:j + 2], "little"); j += 2
    elif length == 0x82:          # 4-byte length
        length = int.from_bytes(blob[j:j + 4], "little"); j += 4
    try:
        return blob[j:j + length].decode("utf-8", "replace").strip()
    except Exception:
        return ""


def _load_contacts() -> Dict[str, str]:
    """Map normalized phone/email → display name from the local AddressBook.
    Best-effort — returns {} if the AddressBook isn't readable."""
    out: Dict[str, str] = {}
    try:
        if not _ADDRESSBOOK.exists():
            return out
        for db in _ADDRESSBOOK.glob("*/AddressBook-v22.abcddb"):
            try:
                tmp = _copy_ro(db)
                con = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
                con.row_factory = sqlite3.Row
                names: Dict[int, str] = {}
                for r in con.execute(
                        "SELECT Z_PK, ZFIRSTNAME, ZLASTNAME, ZORGANIZATION FROM ZABCDRECORD"):
                    nm = " ".join(x for x in (r["ZFIRSTNAME"], r["ZLASTNAME"]) if x) \
                        or (r["ZORGANIZATION"] or "")
                    if nm.strip():
                        names[r["Z_PK"]] = nm.strip()
                for r in con.execute("SELECT ZOWNER, ZFULLNUMBER FROM ZABCDPHONENUMBER"):
                    nm = names.get(r["ZOWNER"])
                    d = _digits(r["ZFULLNUMBER"] or "")
                    if nm and len(d) >= 7:
                        out[d[-10:]] = nm
                for r in con.execute("SELECT ZOWNER, ZADDRESS FROM ZABCDEMAILADDRESS"):
                    nm = names.get(r["ZOWNER"])
                    addr = (r["ZADDRESS"] or "").strip().lower()
                    if nm and addr:
                        out[addr] = nm
                con.close()
                _rm(tmp)
            except Exception:
                continue
    except Exception:
        pass
    return out


def _name_for(handle: str, contacts: Dict[str, str]) -> Optional[str]:
    if not handle:
        return None
    h = handle.strip()
    if "@" in h:
        return contacts.get(h.lower())
    d = _digits(h)
    return contacts.get(d[-10:]) if len(d) >= 7 else None


def _party(handle: str, contacts: Dict[str, str]) -> dict:
    return {"handle": handle, "name": _name_for(handle, contacts)}


def _copy_ro(src: Path) -> str:
    """Copy a (possibly locked) SQLite DB to a temp file for a safe read-only open."""
    fd, tmp = tempfile.mkstemp(prefix="arkive-msg-", suffix=".db")
    os.close(fd)
    shutil.copy2(src, tmp)
    # Copy the -wal/-shm sidecars too so recent writes are visible.
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


# --------------------------------------------------------------------------- #
# Collection                                                                   #
# --------------------------------------------------------------------------- #

def collect(config: Optional[dict] = None,
            state: Optional[dict] = None) -> Tuple[List[dict], dict]:
    """Collect new iMessage/SMS messages + attachments since the last run.

    ``state`` = {"last_rowid": N}; returns (objects, new_state)."""
    config = config or {}
    state = state or {}
    last_rowid = int(state.get("last_rowid") or 0)
    if not available():
        return [], state

    tmp = _copy_ro(_CHAT_DB)
    objects: List[dict] = []
    max_rowid = last_rowid
    try:
        con = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        contacts = _load_contacts()
        log.info("imessage: reading chat.db (ro snapshot) for messages after ROWID %d "
                 "(%d contact(s) resolved)", last_rowid, len(contacts))

        # Chat (thread) metadata + participants.
        chats: Dict[int, dict] = {}
        for r in con.execute("SELECT ROWID, guid, chat_identifier, display_name, style "
                             "FROM chat"):
            chats[r["ROWID"]] = {
                "guid": r["guid"], "identifier": r["chat_identifier"],
                "name": r["display_name"] or "", "is_group": (r["style"] == 43),
                "participants": [],
            }
        log.debug("imessage: indexed %d chat(s)", len(chats))
        for r in con.execute(
                "SELECT chj.chat_id, h.id FROM chat_handle_join chj "
                "JOIN handle h ON h.ROWID = chj.handle_id"):
            c = chats.get(r["chat_id"])
            if c is not None and r["id"]:
                c["participants"].append(r["id"])
        # message ROWID -> its chat ROWID.
        msg_chat: Dict[int, int] = {}
        for r in con.execute("SELECT chat_id, message_id FROM chat_message_join"):
            msg_chat.setdefault(r["message_id"], r["chat_id"])

        # Attachments per message.
        msg_atts: Dict[int, List[sqlite3.Row]] = {}
        for r in con.execute(
                "SELECT maj.message_id, a.ROWID aid, a.guid, a.filename, a.mime_type, "
                "a.transfer_name, a.total_bytes "
                "FROM message_attachment_join maj JOIN attachment a ON a.ROWID = maj.attachment_id"):
            msg_atts.setdefault(r["message_id"], []).append(r)

        rows = con.execute(
            "SELECT m.ROWID, m.guid, m.text, m.attributedBody, m.date, m.is_from_me, "
            "m.service, h.id AS handle "
            "FROM message m LEFT JOIN handle h ON h.ROWID = m.handle_id "
            "WHERE m.ROWID > ? ORDER BY m.ROWID ASC LIMIT ?",
            (last_rowid, _MAX_MESSAGES))

        for m in rows:
            rowid = m["ROWID"]
            max_rowid = max(max_rowid, rowid)
            guid = m["guid"] or f"row{rowid}"
            text = (m["text"] or "").strip() or _attributed_text(m["attributedBody"])
            chat = chats.get(msg_chat.get(rowid))
            is_from_me = bool(m["is_from_me"])
            service = m["service"] or "iMessage"
            when = _iso(m["date"])
            peer = m["handle"] or (chat["identifier"] if chat else "")

            participants = list(chat["participants"]) if chat else ([peer] if peer else [])
            is_group = bool(chat and chat["is_group"])
            chat_id = chat["guid"] if chat else (peer or guid)
            chat_name = (chat["name"] if chat else "") or (
                ", ".join(_name_for(p, contacts) or p for p in participants[:4]))

            if is_from_me:
                sender = {"handle": "me", "name": "Me"}
                recipients = [_party(p, contacts) for p in participants]
            else:
                sender = _party(peer, contacts)
                # Recipients = me + the other participants (group) / just me (1:1).
                others = [p for p in participants if p != peer]
                recipients = [{"handle": "me", "name": "Me"}] + [_party(p, contacts) for p in others]

            # Attachment objects (actual bytes) + refs linked on the message.
            att_refs: List[dict] = []
            for a in msg_atts.get(rowid, []):
                fname = a["transfer_name"] or (a["filename"] or "").split("/")[-1] or "attachment"
                kind = _kind_for(fname)
                att_oid = f"imessage:att:{a['guid'] or a['aid']}"
                ref = {"object_id": att_oid, "filename": fname,
                       "mime": a["mime_type"], "kind": kind}
                att_refs.append(ref)
                path = a["filename"]
                if path and path.startswith("~"):
                    path = os.path.expanduser(path)
                if not path or not os.path.exists(path):
                    continue
                try:
                    size = os.path.getsize(path)
                    if size > _MAX_ATTACH_BYTES:
                        continue
                    with open(path, "rb") as fh:
                        raw = fh.read()
                except OSError:
                    continue
                objects.append({
                    "object_id": att_oid,
                    "kind": kind,
                    "title": fname,
                    "content_b64": base64.b64encode(raw).decode(),
                    "preview": f"{a['mime_type'] or kind} · in {chat_name or 'conversation'}",
                    "meta": {"kind": kind, "mime": a["mime_type"], "filename": fname,
                             "message_guid": guid, "chat_id": chat_id,
                             "chat_name": chat_name, "service": service,
                             "modified": when},
                    "labels": [chat_name or "Messages", "Attachment"],
                    "size_bytes": len(raw),
                })

            # The message object (full record as content; text/meta for search).
            record = {
                "guid": guid, "text": text, "service": service, "date": when,
                "from": sender, "to": recipients, "chat_id": chat_id,
                "chat_name": chat_name, "is_group": is_group,
                "is_from_me": is_from_me, "attachments": att_refs,
            }
            payload = _json(record)
            title = text[:80] if text else (
                f"[{len(att_refs)} attachment(s)]" if att_refs else "(message)")
            who = "Me" if is_from_me else (sender.get("name") or sender.get("handle") or "Unknown")
            preview = f"{who}: {text[:160]}" if text else f"{who} · {len(att_refs)} attachment(s)"
            objects.append({
                "object_id": f"imessage:msg:{guid}",
                "kind": "message",
                "title": title,
                "content_b64": base64.b64encode(payload).decode(),
                "preview": preview,
                "meta": {
                    "kind": "message", "service": service, "chat_id": chat_id,
                    "chat_name": chat_name, "is_group": is_group,
                    "message_guid": guid,
                    "from": sender.get("name") or sender.get("handle"),
                    "to": ", ".join(r.get("name") or r.get("handle") for r in recipients)[:200],
                    "direction": "sent" if is_from_me else "received",
                    "has_attachments": bool(att_refs), "modified": when,
                },
                "labels": [chat_name or "Messages", service],
                "size_bytes": len(payload),
            })
        con.close()
    finally:
        _rm(tmp)

    new_state = {"last_rowid": max_rowid}
    log.info("imessage: collected %d object(s) (through ROWID %d)", len(objects), max_rowid)
    return objects, new_state


def _json(obj) -> bytes:
    import json
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")
