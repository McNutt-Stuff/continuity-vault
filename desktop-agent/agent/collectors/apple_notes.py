"""
Apple Notes collector — macOS.

Backs up the user's Apple Notes by reading the local Notes store
(``~/Library/Group Containers/group.com.apple.notes/NoteStore.sqlite``). Each note's
body is stored gzip-compressed as an Apple "note" protobuf inside ``ZICNOTEDATA.ZDATA``;
we snapshot the store (to dodge the live WAL lock), enumerate notes, decompress and
extract the note text with a tiny protobuf field reader (no external deps), and emit
one ``note`` object per note — flowing into the same Notes model + viewer as the
cloud note sources.

iOS sandboxes the Notes store (readable only by Apple's Notes app), so Notes are
collected here, on the Mac, like iMessage / local Outlook.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import logging
import platform
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("arkive")

_STATE_VERSION = 1
# Core Data epoch (2001-01-01) → Unix epoch offset.
_CORE_DATA_EPOCH = 978307200


def _store_path() -> Path:
    return (Path.home() / "Library" / "Group Containers"
            / "group.com.apple.notes" / "NoteStore.sqlite")


def available() -> bool:
    return platform.system() == "Darwin" and _store_path().exists()


# --------------------------------------------------------------------------- #
# Minimal protobuf reader — extract Apple Notes body text without a dependency #
# --------------------------------------------------------------------------- #

def _read_varint(data: bytes, i: int) -> Tuple[int, int]:
    shift = 0
    result = 0
    while i < len(data):
        b = data[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, i
        shift += 7
    return result, i


def _first_field(data: bytes, target: int) -> Optional[bytes]:
    """Return the bytes of the first length-delimited field ``target`` in ``data``."""
    i = 0
    n = len(data)
    while i < n:
        key, i = _read_varint(data, i)
        field = key >> 3
        wire = key & 7
        if wire == 0:        # varint
            _, i = _read_varint(data, i)
        elif wire == 2:      # length-delimited
            ln, i = _read_varint(data, i)
            val = data[i:i + ln]
            i += ln
            if field == target:
                return val
        elif wire == 1:      # 64-bit
            i += 8
        elif wire == 5:      # 32-bit
            i += 4
        else:
            break
    return None


def _note_text(zdata: bytes) -> str:
    """Decompress + extract the note's plaintext. Apple Notes proto layout:
    NoteStoreProto.document(2).note(3).note_text(2) = the attributed-string text."""
    try:
        blob = gzip.decompress(zdata)
    except OSError:
        return ""
    doc = _first_field(blob, 2)
    if doc is None:
        return ""
    note = _first_field(doc, 3)
    if note is None:
        return ""
    text = _first_field(note, 2)
    if text is None:
        return ""
    return text.decode("utf-8", "replace")


# --------------------------------------------------------------------------- #
# Snapshot + query                                                            #
# --------------------------------------------------------------------------- #

def _snapshot(src: Path) -> Optional[Path]:
    """Copy the store (+ WAL/SHM) to a temp dir so we read a consistent, unlocked
    view even while Notes is running."""
    try:
        tmp = Path(tempfile.mkdtemp(prefix="arkive-notes-"))
        for suffix in ("", "-wal", "-shm"):
            f = Path(str(src) + suffix)
            if f.exists():
                shutil.copy2(f, tmp / (src.name + suffix))
        dest = tmp / src.name
        return dest if dest.exists() else None
    except OSError as exc:  # noqa: BLE001
        log.warning("apple_notes: could not snapshot store: %s", exc)
        return None


def _cols(conn: sqlite3.Connection, table: str) -> set:
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.Error:
        return set()


def _pick(cols: set, *candidates: str) -> Optional[str]:
    for c in candidates:
        if c in cols:
            return c
    return None


def _iso(core_data_ts) -> Optional[str]:
    try:
        from datetime import datetime, timezone
        epoch = float(core_data_ts) + _CORE_DATA_EPOCH
        return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


# --------------------------------------------------------------------------- #
# Entry point                                                                 #
# --------------------------------------------------------------------------- #

def collect(data_dir: str, state: Optional[dict] = None) -> Tuple[List[dict], dict]:
    """Collect Apple Notes. ``state`` = {object_id: content_hash} so unchanged notes
    aren't re-uploaded."""
    state = state or {}
    prior = state.get("hashes", {}) if state.get("v") == _STATE_VERSION else {}
    if not available():
        log.info("apple_notes: NoteStore not available (macOS only) — skipping")
        return [], {"v": _STATE_VERSION, "hashes": prior}

    snap = _snapshot(_store_path())
    if snap is None:
        return [], {"v": _STATE_VERSION, "hashes": prior}

    objects: List[dict] = []
    current: Dict[str, str] = {}
    try:
        conn = sqlite3.connect(f"file:{snap}?mode=ro&immutable=1", uri=True)
        obj_cols = _cols(conn, "ZICCLOUDSYNCINGOBJECT")
        data_cols = _cols(conn, "ZICNOTEDATA")
        if not obj_cols or not data_cols:
            log.warning("apple_notes: unexpected schema (no notes tables)")
            return [], {"v": _STATE_VERSION, "hashes": prior}

        title_c = _pick(obj_cols, "ZTITLE1", "ZTITLE", "ZTITLE2") or "ZTITLE1"
        snippet_c = _pick(obj_cols, "ZSNIPPET")
        mod_c = _pick(obj_cols, "ZMODIFICATIONDATE1", "ZMODIFICATIONDATE")
        cre_c = _pick(obj_cols, "ZCREATIONDATE1", "ZCREATIONDATE", "ZCREATIONDATE3")
        ident_c = _pick(obj_cols, "ZIDENTIFIER")
        notedata_c = _pick(obj_cols, "ZNOTEDATA")
        folder_c = _pick(obj_cols, "ZFOLDER")
        data_pk = _pick(data_cols, "Z_PK") or "Z_PK"
        data_blob = _pick(data_cols, "ZDATA") or "ZDATA"

        # Folder id → folder title (folders are rows in the same table).
        folders: Dict[int, str] = {}
        if folder_c:
            try:
                for pk, name in conn.execute(
                        f"SELECT Z_PK, {title_c} FROM ZICCLOUDSYNCINGOBJECT "
                        f"WHERE {title_c} IS NOT NULL").fetchall():
                    folders[pk] = name
            except sqlite3.Error:
                pass

        sel = [f"o.Z_PK", f"o.{title_c}", f"d.{data_blob}"]
        sel.append(f"o.{snippet_c}" if snippet_c else "NULL")
        sel.append(f"o.{mod_c}" if mod_c else "NULL")
        sel.append(f"o.{cre_c}" if cre_c else "NULL")
        sel.append(f"o.{ident_c}" if ident_c else "NULL")
        sel.append(f"o.{folder_c}" if folder_c else "NULL")
        query = (f"SELECT {', '.join(sel)} FROM ZICCLOUDSYNCINGOBJECT o "
                 f"JOIN ZICNOTEDATA d ON o.{notedata_c} = d.{data_pk} "
                 f"WHERE d.{data_blob} IS NOT NULL")
        rows = conn.execute(query).fetchall()
        log.info("apple_notes: %d note(s) in store", len(rows))

        for pk, title, zdata, snippet, mod, cre, ident, folder in rows:
            identity = str(ident or pk)
            oid = "apple_notes:" + hashlib.sha256(identity.encode()).hexdigest()[:24]
            text = _note_text(zdata) if zdata else ""
            disp = (title or "").strip() or (text.strip().splitlines()[0][:80]
                                             if text.strip() else "Note")
            modified = _iso(mod)
            chash = hashlib.sha256(
                ("apple_notes:" + oid + "|" + disp + "|" + (modified or "") + "|"
                 + text).encode("utf-8", "replace")).hexdigest()
            current[oid] = chash
            if prior.get(oid) == chash:
                continue
            # JSON body ({title, notebook, content}) so it renders in the note viewer.
            body = json.dumps({"title": disp, "notebook": folders.get(folder, ""),
                               "content": text, "created": _iso(cre),
                               "modified": modified},
                              ensure_ascii=False).encode("utf-8", "replace")
            meta = {"kind": "note", "title": disp, "folder": folders.get(folder, ""),
                    "created": _iso(cre), "modified": modified, "device": "macos"}
            objects.append({
                "object_id": oid, "kind": "note", "title": disp,
                "content_b64": base64.b64encode(body).decode(),
                "content_hash": chash,
                "preview": "Note · " + disp[:60],
                "meta": meta, "labels": ["Notes", folders.get(folder, "") or "Notes"],
                "size_bytes": len(body),
            })
        conn.close()
    except sqlite3.Error as exc:
        log.warning("apple_notes: read failed: %s", exc)
        return [], {"v": _STATE_VERSION, "hashes": prior}
    finally:
        try:
            shutil.rmtree(snap.parent, ignore_errors=True)
        except OSError:
            pass

    log.info("apple_notes: %d note(s), %d new/changed to push", len(current), len(objects))
    return objects, {"v": _STATE_VERSION, "hashes": current}
