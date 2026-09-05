"""
Local Microsoft Outlook collector (macOS).

Backs up everything Outlook stores on the device so mail is protected even for
accounts that aren't reachable in the cloud. Two on-disk formats are handled:

  * **Legacy Outlook for Mac** — an ``Outlook.sqlite`` profile plus per-message
    ``.olk15MsgSource`` RFC-822 files. Read directly (schema-tolerant).

  * **New Outlook for Mac** — the opaque ``HxStore.hxd`` cache (the classic Mail
    table is empty). We decode it with the bundled ``hxprobe`` parser (an
    MIT-licensed reverse-engineering of the store) into a throwaway SQLite DB and
    emit the recovered emails, contacts and calendar events. Because HxStore is
    an undocumented cache, this path is flagged **experimental** so the cloud can
    surface a notice on the source (``capture.mode == "experimental-hxstore"``).

For every email we also emit its attachments as their own file objects (bytes +
kind), linked back to the message via ``meta.message_object_id`` so unified
search can categorise them as files while still tying them to the email.

Collection is incremental: a per-object content signature is persisted so a run
only pushes new or changed items. The first run captures the whole store
(backfill); later runs sync only the deltas.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("arkive")

_GROUP = Path.home() / "Library" / "Group Containers" / "UBF8T346G9.Office" / "Outlook"
_APPLE_EPOCH = 978307200  # Mac absolute time base (2001-01-01) in Unix seconds
_MAX_ROWS = 20000         # per record type, safety cap
_MAX_MESSAGES = 500000    # HxStore message fragments scanned per run (grouped after)
_MAX_MIME_BYTES = 100 * 1024 * 1024
_STATE_VERSION = 3

# New Outlook (HxStore) discovery roots.
_HXSTORE_ROOTS = (
    Path.home() / "Library" / "Group Containers" / "UBF8T346G9.Office" / "Outlook",
    Path.home() / "Library" / "Containers" / "com.microsoft.Outlook",
)
_HXSTORE_MAGIC = b"Nostromo"

_IMG = {"jpg", "jpeg", "png", "gif", "heic", "heif", "webp", "tiff", "bmp"}
_VID = {"mp4", "mov", "avi", "mkv", "webm", "m4v", "3gp"}
_AUD = {"mp3", "wav", "aac", "flac", "m4a", "caf", "amr"}


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


# --------------------------------------------------------------------------- #
# Discovery                                                                    #
# --------------------------------------------------------------------------- #

def _profiles() -> List[Path]:
    base = _GROUP / "Outlook 15 Profiles"
    if not base.exists():
        return []
    return [p / "Data" for p in base.iterdir() if (p / "Data" / "Outlook.sqlite").exists()]


def _discover_hxstores() -> List[Path]:
    """Every readable New-Outlook ``HxStore.hxd`` for the current user, newest
    first. Verifies the ``Nostromo`` magic so we never hand hxprobe a stray file."""
    seen: set = set()
    stores: List[Path] = []
    for root in _HXSTORE_ROOTS:
        if not root.exists():
            continue
        try:
            candidates = list(root.rglob("HxStore.hxd"))
        except (OSError, PermissionError):
            continue
        for path in candidates:
            try:
                st = path.stat()
                ident = (st.st_dev, st.st_ino)
                if not path.is_file() or st.st_size <= 8 or ident in seen:
                    continue
                with path.open("rb") as fh:
                    if fh.read(8) != _HXSTORE_MAGIC:
                        continue
                seen.add(ident)
                stores.append(path.resolve())
            except (OSError, PermissionError):
                continue
    return sorted(stores, key=lambda p: p.stat().st_mtime, reverse=True)


def available() -> bool:
    """True when there's anything to collect — a legacy profile OR a New Outlook
    HxStore (checked cheaply without decoding)."""
    if _profiles():
        return True
    for root in _HXSTORE_ROOTS:
        try:
            if root.exists() and next(root.rglob("HxStore.hxd"), None) is not None:
                return True
        except (OSError, PermissionError):
            continue
    return False


# --------------------------------------------------------------------------- #
# hxprobe (New Outlook / HxStore) integration                                 #
# --------------------------------------------------------------------------- #

def _hxprobe_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "hxprobe"


def locate_hxprobe() -> Optional[Path]:
    """Find a runnable ``hxprobe`` executable. Prefers the prebuilt binary shipped
    in the agent bundle, then a locally-built one, then ``PATH``."""
    hp = _hxprobe_dir()
    candidates = [
        hp / "bin" / "hxprobe",
        hp / "target" / "release" / "hxprobe",
    ]
    which = shutil.which("hxprobe")
    if which:
        candidates.append(Path(which))
    for c in candidates:
        try:
            if c.is_file() and os.access(c, os.X_OK):
                return c.resolve()
        except OSError:
            continue
    return None


def _build_hxprobe() -> Optional[Path]:
    """Best-effort build of hxprobe from the bundled Rust source when no prebuilt
    binary matches this machine (e.g. an Intel Mac + an arm64 bundle). Requires
    cargo; returns None (and logs) when it isn't available."""
    if not shutil.which("cargo"):
        return None
    hp = _hxprobe_dir()
    if not (hp / "Cargo.toml").exists():
        return None
    log.info("outlook_local: building hxprobe from source (%s)…", hp)
    try:
        subprocess.run(["cargo", "build", "--release"], cwd=str(hp),
                       check=True, capture_output=True, text=True, timeout=1800)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        log.warning("outlook_local: hxprobe build failed: %s", exc)
        return None
    built = hp / "target" / "release" / "hxprobe"
    return built.resolve() if built.exists() else None


def _hxprobe_runnable(hxprobe: Path) -> bool:
    """Verify the binary actually runs on this architecture (a bundled arm64
    binary won't exec on Intel) before we rely on it."""
    try:
        subprocess.run([str(hxprobe)], capture_output=True, timeout=30)
        return True
    except OSError:
        return False
    except subprocess.SubprocessError:
        return True  # ran but exited non-zero (e.g. usage) — still runnable


def _stable_snapshot(src: Path, dst: Path, retries: int = 5, stability_ms: int = 250) -> bool:
    """Copy a live HxStore only when its size/mtime stay stable across the copy,
    so an open Outlook doesn't hand us a torn file. Returns True on success."""
    delay = max(0, stability_ms) / 1000.0
    for _ in range(max(1, retries)):
        try:
            before = src.stat()
            if delay:
                time.sleep(delay)
            settled = src.stat()
            if (before.st_size, before.st_mtime_ns) != (settled.st_size, settled.st_mtime_ns):
                continue
            shutil.copyfile(src, dst)
            after = src.stat()
            if (settled.st_size, settled.st_mtime_ns) == (after.st_size, after.st_mtime_ns):
                return True
            try:
                dst.unlink()
            except OSError:
                pass
        except OSError as exc:
            log.debug("outlook_local: snapshot attempt failed: %s", exc)
        if delay:
            time.sleep(delay)
    return False


def _hx_choose(cols: List[str], *cands: str) -> Optional[str]:
    low = {c.lower(): c for c in cols}
    for cand in cands:
        if cand.lower() in low:
            return low[cand.lower()]
    for cand in cands:
        for c in cols:
            if cand.lower() in c.lower():
                return c
    return None


def _hx_unix(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _iso_from_unix(v) -> Optional[str]:
    f = _hx_unix(v)
    if f is None:
        return None
    try:
        return datetime.fromtimestamp(f, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _attachment_key(name: str) -> str:
    """Filename with Outlook's local ``[12345]`` record id stripped, lowercased."""
    base = name.replace("\\", "/").rsplit("/", 1)[-1].strip().lower()
    return re.sub(r"\[\d+\](?=\.[^.]+$|$)", "", base)


def _index_disk_attachments(store: Path) -> Tuple[dict, dict, dict, List[Path]]:
    """Index every file under the profile's ``Files/**/Attachments`` so message
    attachment candidates can be resolved to real bytes on disk."""
    exact: Dict[str, List[Path]] = {}
    normalized: Dict[str, List[Path]] = {}
    by_record: Dict[int, List[Path]] = {}
    disk: List[Path] = []
    files_root = store.parent / "Files"
    if not files_root.is_dir():
        return exact, normalized, by_record, disk
    try:
        for p in files_root.rglob("*"):
            if not p.is_file() or "Attachments" not in p.parts:
                continue
            disk.append(p)
            exact.setdefault(p.name.lower(), []).append(p)
            normalized.setdefault(_attachment_key(p.name), []).append(p)
            m = re.search(r"\[(\d+)\](?=\.[^.]+$|$)", p.name)
            if m:
                by_record.setdefault(int(m.group(1)), []).append(p)
    except (OSError, PermissionError):
        pass
    return exact, normalized, by_record, disk


def _resolve_attachment(candidate: str, att_ids: List[int],
                        exact: dict, normalized: dict, by_record: dict) -> Optional[Path]:
    basename = candidate.replace("\\", "/").rsplit("/", 1)[-1]
    matches = exact.get(basename.lower(), [])
    if len(matches) != 1:
        matches = normalized.get(_attachment_key(basename), [])
    if len(matches) != 1 and att_ids:
        id_matches = {p for rid in att_ids for p in by_record.get(rid, [])}
        name_matches = set(normalized.get(_attachment_key(basename), []))
        matches = sorted(id_matches & name_matches)
    return matches[0] if len(matches) == 1 else None


def _decode_json_list(raw) -> list:
    if not isinstance(raw, str):
        return []
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _emit_attachment(path: Path, message_oid: str, subject: str, frm: str,
                     to: str, when: Optional[str], out: List[dict],
                     sigs: Dict[str, str], old_sigs: Dict[str, str],
                     emitted: set) -> Optional[dict]:
    """Emit an email attachment as its own file object (bytes), linked to the
    email via ``meta.message_object_id``. Returns a lightweight ref for the email
    or None when the file can't be read. Deltas skip unchanged files by size+mtime
    without reading the bytes."""
    try:
        st = path.stat()
    except OSError:
        return None
    if st.st_size > _MAX_MIME_BYTES:
        return None
    oid = f"outlook_local:att:{hashlib.sha256(str(path).encode()).hexdigest()[:16]}"
    fname = path.name
    kind = _kind_for(fname)
    ref = {"object_id": oid, "filename": fname, "kind": kind, "size": st.st_size}
    if oid in emitted:
        return ref
    emitted.add(oid)
    signature = f"att:{st.st_size}:{st.st_mtime_ns}"
    sigs[oid] = signature
    if old_sigs.get(oid) == signature:
        return ref  # unchanged since last run — linked, but bytes already backed up
    try:
        raw = path.read_bytes()
    except OSError:
        return ref
    out.append(_obj(
        oid, kind, fname, raw,
        f"Attachment · {fname}" + (f" · from {frm}" if frm else ""),
        {"kind": kind, "filename": fname, "is_attachment": True,
         "message_object_id": message_oid, "subject": subject,
         "from": frm, "to": to, "date": when, "content_backed_up": True,
         "source": "new-outlook", "capture": "experimental-hxstore",
         "modified": when},
        ["Outlook", "Attachment"]))
    return ref


def _collect_hxstore(store: Path, hxprobe: Path, out: List[dict],
                     want, sigs: Dict[str, str], old_sigs: Dict[str, str],
                     tmpdir: Path) -> dict:
    """Decode one HxStore via hxprobe and append new/changed objects to ``out``.
    Populates ``sigs`` with the current per-object signatures for delta state."""
    counts = {"mail": 0, "contacts": 0, "calendar": 0, "attachments": 0}
    snapshot = tmpdir / (store.parent.name + "-HxStore.snapshot.hxd")
    parsed = tmpdir / (store.parent.name + "-hxstore.sqlite")
    if not _stable_snapshot(store, snapshot):
        log.warning("outlook_local: could not obtain a stable snapshot of %s", store)
        return counts
    try:
        res = subprocess.run([str(hxprobe), "db", str(snapshot), str(parsed)],
                             capture_output=True, text=True, timeout=1800)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("outlook_local: hxprobe failed on %s: %s", store, exc)
        return counts
    if res.returncode != 0 or not parsed.exists():
        log.warning("outlook_local: hxprobe could not parse %s: %s", store,
                    (res.stderr or "").strip()[:400])
        return counts

    exact, normalized, by_record, _disk = _index_disk_attachments(store)
    emitted_att: set = set()
    run_seen: set = set()  # object ids emitted this run (HxStore holds duplicate fragments)

    def _emit_if_changed(obj: dict, signature: str) -> bool:
        oid = obj["object_id"]
        if oid in run_seen:
            return False  # same logical item already captured this run (dup fragment)
        run_seen.add(oid)
        sigs[oid] = signature
        if old_sigs.get(oid) == signature:
            return False
        out.append(obj)
        return True

    con = sqlite3.connect(str(parsed))
    con.row_factory = sqlite3.Row
    try:
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}

        # -- Emails + their attachments ------------------------------------ #
        # New Outlook caches each email as SEVERAL fragments (a ~255-char preview
        # plus, often, a full copy) that share a message_id (or block). We group
        # by logical identity and keep the RICHEST fragment so the backed-up email
        # carries the full body/subject/date, not a truncated preview.
        if want("mail") and "messages" in tables:
            mcols = [r[1] for r in con.execute("PRAGMA table_info(messages)")]

            def mget(row, *names):
                col = _hx_choose(mcols, *names)
                return row[col] if col else None

            def _score(row) -> int:
                bk = (mget(row, "body_kind") or "").lower()
                subj = (mget(row, "subject") or "").strip()
                inherited = bool(mget(row, "subject_inherited"))
                return ((2 if bk == "full" else 1) * 10_000_000
                        + len(mget(row, "html") or "") * 2
                        + len(mget(row, "body") or "")
                        + (1_000_000 if subj and not inherited else 0))

            best: Dict[str, sqlite3.Row] = {}
            att_by_group: Dict[str, Tuple[set, set]] = {}
            subj_by_group: Dict[str, str] = {}   # a real (non-inherited) subject
            order = "ORDER BY sent_unix DESC" if "sent_unix" in mcols else ""
            for row in con.execute(f"SELECT rowid AS _rid, * FROM messages {order} LIMIT {_MAX_MESSAGES}"):
                mid = (mget(row, "message_id") or "").strip()
                block = mget(row, "block") or row["_rid"]
                key = mid or f"blk{block}"
                names, ids = att_by_group.setdefault(key, (set(), set()))
                for n in _decode_json_list(mget(row, "attachment_names_json")):
                    names.add(n)
                for i in _decode_json_list(mget(row, "attachment_ids_json")):
                    if str(i).isdigit():
                        ids.add(int(i))
                subj = (mget(row, "subject") or "").strip()
                if subj and not mget(row, "subject_inherited") and key not in subj_by_group:
                    subj_by_group[key] = subj
                cur = best.get(key)
                if cur is None or _score(row) > _score(cur):
                    best[key] = row

            for key, row in best.items():
                mid = (mget(row, "message_id") or "").strip()
                oid = "outlook_local:mail:hx:" + re.sub(r"[^A-Za-z0-9._@+-]", "_", key)
                subject = (subj_by_group.get(key)
                           or (mget(row, "subject") or "").strip() or "(no subject)")
                subject_inherited = key not in subj_by_group and bool(mget(row, "subject_inherited"))
                sender = mget(row, "sender") or ""
                sender_name = mget(row, "sender_name") or ""
                recipients = mget(row, "recipients") or ""
                when = _iso_from_unix(mget(row, "sent_unix"))
                date_text = mget(row, "sent_utc") or ""
                body = mget(row, "body") or ""
                html_body = mget(row, "html") or ""
                body_kind = (mget(row, "body_kind") or "").lower()
                frm = (f"{sender_name} <{sender}>".strip() if sender_name and sender
                       else (sender or sender_name))

                names, ids = att_by_group.get(key, (set(), set()))
                att_ids = sorted(ids)
                att_refs: List[dict] = []
                for cand in sorted(names):
                    path = _resolve_attachment(cand, att_ids, exact, normalized, by_record)
                    if not path:
                        continue
                    ref = _emit_attachment(path, oid, subject, frm, recipients, when,
                                           out, sigs, old_sigs, emitted_att)
                    if ref:
                        att_refs.append(ref)

                display_body = html_body or (
                    "<pre>" + html.escape(body) + "</pre>" if body else "")
                content = _wrap_html_email(subject, frm, recipients, when, date_text,
                                           display_body)
                signature = hashlib.sha256(content).hexdigest()
                obj = _obj(
                    oid, "email", subject, content,
                    (f"{frm} · " if frm else "") + (body[:180] or subject),
                    {"kind": "email", "from": frm, "to": recipients, "date": when,
                     "date_text": date_text, "message_id": mid or "",
                     "has_mime": True, "content_backed_up": True,
                     "source": "new-outlook", "capture": "experimental-hxstore",
                     "body_kind": body_kind or "preview",
                     "preview_only": body_kind != "full",
                     "subject_inherited": subject_inherited,
                     "has_attachments": bool(att_refs),
                     "attachments": att_refs, "modified": when},
                    ["Outlook", "Mail"])
                if _emit_if_changed(obj, signature):
                    counts["mail"] += 1

        # -- Contacts ------------------------------------------------------- #
        if want("contacts") and "contacts" in tables:
            ccols = [r[1] for r in con.execute("PRAGMA table_info(contacts)")]
            for row in con.execute(f"SELECT rowid AS _rid, * FROM contacts LIMIT {_MAX_ROWS}"):
                block = row["block"] if "block" in ccols else row["_rid"]
                oid = f"outlook_local:contact:hx:{block}"
                name = (row["display_name"] if "display_name" in ccols else None) or "(contact)"
                emails = (row["email_addresses"] if "email_addresses" in ccols else "") or ""
                phones = (row["phone_numbers"] if "phone_numbers" in ccols else "") or ""
                when = _iso_from_unix(row["modified_unix"] if "modified_unix" in ccols else None)
                rec = {"name": name, "email": emails, "phone": phones}
                content = _json(rec)
                signature = hashlib.sha256(content).hexdigest()
                obj = _obj(
                    oid, "contact", name, content,
                    " · ".join(x for x in (emails, phones) if x),
                    {"kind": "contact", "email": emails, "phone": phones,
                     "source": "new-outlook", "capture": "experimental-hxstore",
                     "modified": when}, ["Outlook", "Contacts"])
                if _emit_if_changed(obj, signature):
                    counts["contacts"] += 1

        # -- Calendar ------------------------------------------------------- #
        if want("calendar") and "calendar_events" in tables:
            ecols = [r[1] for r in con.execute("PRAGMA table_info(calendar_events)")]
            for row in con.execute(f"SELECT rowid AS _rid, * FROM calendar_events LIMIT {_MAX_ROWS}"):
                block = row["block"] if "block" in ecols else row["_rid"]
                oid = f"outlook_local:event:hx:{block}"
                title = (row["title"] if "title" in ecols else None) or "(event)"
                start = _iso_from_unix(row["start_unix"] if "start_unix" in ecols else None)
                end = _iso_from_unix(row["end_unix"] if "end_unix" in ecols else None)
                organizer = (row["organizer"] if "organizer" in ecols else "") or ""
                attendees = (row["attendees"] if "attendees" in ecols else "") or ""
                body = (row["body"] if "body" in ecols else "") or ""
                rec = {"title": title, "start": start, "end": end,
                       "organizer": organizer, "attendees": attendees, "body": body}
                content = _json(rec)
                signature = hashlib.sha256(content).hexdigest()
                obj = _obj(
                    oid, "event", title, content,
                    f"{start or ''} · {organizer}".strip(" ·"),
                    {"kind": "event", "start": start, "end": end,
                     "organizer": organizer, "source": "new-outlook",
                     "capture": "experimental-hxstore", "modified": start},
                    ["Outlook", "Calendar"])
                if _emit_if_changed(obj, signature):
                    counts["calendar"] += 1

        counts["attachments"] = len(emitted_att)
    finally:
        con.close()
    return counts


# --------------------------------------------------------------------------- #
# Shared helpers                                                               #
# --------------------------------------------------------------------------- #

def _copy_ro(src: Path) -> str:
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
    idx: Dict[str, Path] = {}
    msgs = data_dir / "Messages"
    if not msgs.exists():
        return idx
    try:
        for p in msgs.rglob("*.olk15MsgSource"):
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
    return json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")


# --------------------------------------------------------------------------- #
# Legacy Outlook.sqlite extractors (heuristic, schema-tolerant)               #
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
# Email envelope wrapper (shared by legacy + New Outlook)                      #
# --------------------------------------------------------------------------- #

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
    return ("\r\n".join(hdr) + "\r\n\r\n" + (html_body or "")).encode("utf-8", "replace")


# --------------------------------------------------------------------------- #
# Entry point                                                                 #
# --------------------------------------------------------------------------- #

def collect(config: Optional[dict] = None,
            state: Optional[dict] = None) -> Tuple[List[dict], dict]:
    """Collect Outlook mail/contacts/calendar (+ attachments) from every local
    profile and New-Outlook HxStore. Incremental: only new/changed objects are
    returned; ``state`` carries the per-object signatures so a run pushes just the
    deltas (first run = full backfill). The returned state also records the
    capture mode so the cloud can flag the experimental HxStore path on the
    source."""
    config = config or {}
    state = state or {}
    inc = config.get("includeCategories") or []  # optional Data Map filter

    def _want(cat: str) -> bool:
        return (not inc) or (cat in inc)

    old_sigs: Dict[str, str] = (state.get("sigs") or {}) if state.get("v") == _STATE_VERSION else {}
    sigs: Dict[str, str] = {}
    out: List[dict] = []
    seen: set = set()
    capture_mode = "legacy"
    hxstore_count = 0

    # -- Legacy profiles ---------------------------------------------------- #
    profiles = _profiles()
    log.info("outlook_local: scanning %d legacy profile(s) [filter=%s]",
             len(profiles), inc or "all")
    for data_dir in profiles:
        db = data_dir / "Outlook.sqlite"
        tmp = _copy_ro(db)
        counts = {"mail": 0, "contacts": 0, "calendar": 0, "notes": 0}
        try:
            con = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            mime_idx = _mime_index(data_dir) if _want("mail") else {}
            for table in _tables(con):
                cols = _cols(con, table)
                if not cols:
                    continue
                t = table.lower()
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
                if cat:
                    counts[cat] += len(out) - before
            con.close()
        except Exception as exc:  # noqa: BLE001
            log.warning("outlook_local: profile %s unreadable: %s", data_dir, exc)
        finally:
            _rm(tmp)
        log.info("outlook_local: legacy profile — mail=%d contacts=%d calendar=%d notes=%d",
                 counts["mail"], counts["contacts"], counts["calendar"], counts["notes"])

    # Track legacy object ids in the signature map so state stays authoritative
    # (their content hash is computed by the agent; the marker just records them).
    for o in out:
        sigs.setdefault(o["object_id"], "legacy")

    # -- New Outlook (HxStore) --------------------------------------------- #
    stores = _discover_hxstores()
    if stores:
        hxprobe = locate_hxprobe()
        if hxprobe and not _hxprobe_runnable(hxprobe):
            log.info("outlook_local: bundled hxprobe not runnable here — trying to build")
            hxprobe = _build_hxprobe() or hxprobe
        if not hxprobe:
            hxprobe = _build_hxprobe()
        if not hxprobe:
            log.warning("outlook_local: %d New Outlook store(s) found but hxprobe is "
                        "unavailable (no prebuilt binary and cargo missing) — skipping",
                        len(stores))
        else:
            log.info("outlook_local: decoding %d New Outlook store(s) via %s",
                     len(stores), hxprobe)
            with tempfile.TemporaryDirectory(prefix="arkive-hxstore-") as td:
                tmpdir = Path(td)
                for store in stores:
                    counts = _collect_hxstore(store, hxprobe, out, _want, sigs,
                                              old_sigs, tmpdir)
                    capture_mode = "experimental-hxstore"
                    hxstore_count += 1
                    log.info("outlook_local: HxStore %s — mail=%d contacts=%d "
                             "calendar=%d attachments=%d", store.parent.name,
                             counts["mail"], counts["contacts"], counts["calendar"],
                             counts["attachments"])

    new_state = {
        "v": _STATE_VERSION,
        "sigs": sigs,
        "capture": {
            "mode": capture_mode,
            "hxstore_count": hxstore_count,
            "notice": (
                "New Outlook detected. Its mail is stored in an undocumented local "
                "cache (HxStore); Arkive backs it up using an experimental decoder, "
                "so some messages may be preview-only or missing envelope details."
            ) if capture_mode == "experimental-hxstore" else "",
        },
    }
    log.info("outlook_local: %d new/changed object(s) to push (capture=%s, %d HxStore)",
             len(out), capture_mode, hxstore_count)
    return out, new_state
