"""
Unified log sink — the plumbing that makes the control plane the ONE place to
view logs from everything.

Three inputs funnel into the ``log_entries`` table:
  1. In-process app loggers (cv.* / arkive.*) via a buffered logging.Handler +
     a background flusher (this runs on the CP AND on every customer-tenant node).
  2. Device logs — appliance/agent ``recent_logs`` lines ingested on heartbeat.
  3. The audit ledger — ``audit.record`` dual-writes a LogEntry so user actions /
     auth / audits show in the same view.

Customer-tenant nodes push their rows to the CP every ~30s (see
``workers/node_replication``); the cursor advances only on confirmed delivery, so
a batch that can't be delivered is retried whole and never dropped.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger("cv.logsink")  # our own logger — never self-captured

_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO,
           "warning": logging.WARNING, "error": logging.ERROR, "critical": logging.CRITICAL}
_LEVEL_NAME = {logging.DEBUG: "debug", logging.INFO: "info", logging.WARNING: "warning",
               logging.ERROR: "error", logging.CRITICAL: "critical"}

# Loggers we capture into the unified store (our own tree only — never third-party
# noise like uvicorn.access or botocore).
_CAPTURE_PREFIXES = ("cv.", "cv", "arkive.", "arkive", "app.")
# Loggers we NEVER capture (would recurse or is pure noise).
_SKIP_LOGGERS = {"cv.logsink"}

_BUF: deque = deque(maxlen=5000)   # bounded ring buffer of pending LogEntry dicts
_BUF_LOCK = threading.Lock()
_FLUSHING = threading.local()
_installed = False
_self_node: Optional[tuple[str, str]] = None  # (node_id, node_name), cached
_self_node_at = 0.0


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _source_for_logger(name: str) -> str:
    n = (name or "").lower()
    if "sync" in n or "connector" in n or n == "cv":
        return "connector" if ("sync" in n or "connector" in n) else "cloud"
    if "integration" in n:
        return "integration"
    if "nodeconfig" in n or "node" in n or "replication" in n:
        return "node"
    if "auth" in n:
        return "auth"
    return "cloud"


def _self_node_info(db) -> tuple[Optional[str], str]:
    """Cached (node_id, node_name) for the running node so captured rows are
    attributed. CP = is_self node; a customer node = its own row."""
    global _self_node, _self_node_at
    if _self_node and time.time() - _self_node_at < 120:
        return _self_node
    try:
        from .models import Node
        n = db.query(Node).filter(Node.is_self.is_(True)).first()
        _self_node = (n.id, n.name) if n else (None, "")
    except Exception:  # noqa: BLE001
        _self_node = (None, "")
    _self_node_at = time.time()
    return _self_node


class _SinkHandler(logging.Handler):
    """Buffers matching records; the flusher persists them. Never touches the DB
    itself (that would deadlock/recurse inside logging)."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.name in _SKIP_LOGGERS or getattr(_FLUSHING, "active", False):
                return
            if not record.name.startswith(_CAPTURE_PREFIXES):
                return
            msg = record.getMessage()
            if record.exc_info:
                msg = f"{msg}\n{logging.Formatter().formatException(record.exc_info)}"
            with _BUF_LOCK:
                _BUF.append({
                    "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).replace(tzinfo=None),
                    "level": _LEVEL_NAME.get(record.levelno, "info"),
                    "source": _source_for_logger(record.name),
                    "logger": record.name,
                    "message": msg[:8000],
                })
        except Exception:  # noqa: BLE001 — logging must never raise
            pass


def emit(*, level: str, source: str, message: str, logger_name: str = "",
         tenant_id: Optional[str] = None, node_id: Optional[str] = None,
         node_name: str = "", appliance_id: Optional[str] = None,
         agent_id: Optional[str] = None, actor: str = "", resource: str = "",
         meta: Optional[dict] = None, ts: Optional[datetime] = None) -> None:
    """Directly buffer a structured LogEntry (used by the audit dual-write and any
    caller that already has the tenant/actor context the logging module lacks)."""
    try:
        with _BUF_LOCK:
            _BUF.append({
                "ts": ts or _now(), "level": (level or "info").lower(),
                "source": source or "cloud", "logger": logger_name,
                "message": (message or "")[:8000], "tenant_id": tenant_id,
                "node_id": node_id, "node_name": node_name, "appliance_id": appliance_id,
                "agent_id": agent_id, "actor": actor, "resource": resource,
                "meta": meta or {},
            })
    except Exception:  # noqa: BLE001
        pass


_LINE_RE = None


def _parse_device_line(line: str) -> tuple[datetime, str, str]:
    """Parse an appliance/agent log line 'YYYY-MM-DDTHH:MM:SS(+ZZZZ) LEVEL message'.
    Falls back to (now, info, whole-line) when the shape doesn't match."""
    import re
    global _LINE_RE
    if _LINE_RE is None:
        _LINE_RE = re.compile(
            r"^(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[+-]\d{4}|Z)?)\s+"
            r"(?P<lvl>DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL)\s+(?P<msg>.*)$")
    m = _LINE_RE.match(line.strip())
    if not m:
        return _now(), "info", line.strip()[:8000]
    raw = m.group("ts").replace("Z", "+0000").replace(" ", "T")
    try:
        dt = datetime.strptime(raw[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        dt = _now()
    lvl = m.group("lvl").lower()
    return dt, ("warning" if lvl == "warn" else lvl), m.group("msg")[:8000]


def ingest_device_logs(db, *, source: str, lines: list[str], tenant_id: Optional[str] = None,
                       appliance_id: Optional[str] = None, agent_id: Optional[str] = None,
                       device_name: str = "") -> int:
    """Ingest an appliance/agent's ``recent_logs`` (rolling last-N lines) into the
    unified store, de-duplicated against what's already recorded for this device in
    the covered window (so repeated heartbeats don't duplicate lines)."""
    from .models import LogEntry
    if not lines:
        return 0
    parsed = [_parse_device_line(str(l)) for l in lines if str(l).strip()]
    if not parsed:
        return 0
    oldest = min(p[0] for p in parsed) - timedelta(seconds=1)
    q = db.query(LogEntry.ts, LogEntry.message).filter(LogEntry.source == source,
                                                        LogEntry.ts >= oldest)
    if appliance_id:
        q = q.filter(LogEntry.appliance_id == appliance_id)
    if agent_id:
        q = q.filter(LogEntry.agent_id == agent_id)
    seen = {(ts, _hash(msg)) for ts, msg in q.all()}
    added = 0
    for dt, lvl, msg in parsed:
        if (dt, _hash(msg)) in seen:
            continue
        seen.add((dt, _hash(msg)))
        db.add(LogEntry(ts=dt, level=lvl, source=source, logger=device_name,
                        message=msg, tenant_id=tenant_id, appliance_id=appliance_id,
                        agent_id=agent_id, node_name=device_name))
        added += 1
    if added:
        db.commit()
    return added


def _hash(msg: str) -> str:
    return hashlib.sha1((msg or "").encode("utf-8", "replace")).hexdigest()[:16]


def _flush_once() -> int:
    with _BUF_LOCK:
        if not _BUF:
            return 0
        batch = list(_BUF)
        _BUF.clear()
    _FLUSHING.active = True
    try:
        from .db import SessionLocal
        from .models import LogEntry
        with SessionLocal() as db:
            nid, nname = _self_node_info(db)
            for e in batch:
                db.add(LogEntry(
                    ts=e.get("ts") or _now(), level=e.get("level", "info"),
                    source=e.get("source", "cloud"), logger=e.get("logger", ""),
                    message=e.get("message", ""), tenant_id=e.get("tenant_id"),
                    node_id=e.get("node_id") or nid, node_name=e.get("node_name") or nname,
                    appliance_id=e.get("appliance_id"), agent_id=e.get("agent_id"),
                    actor=e.get("actor", ""), resource=e.get("resource", ""),
                    meta=e.get("meta") or {}))
            db.commit()
        return len(batch)
    except Exception as exc:  # noqa: BLE001 — never let logging kill the app
        logger.warning("log flush failed (%d dropped): %s", len(batch), exc)
        return 0
    finally:
        _FLUSHING.active = False


def _flusher_loop() -> None:
    while True:
        time.sleep(5)
        try:
            _flush_once()
        except Exception:  # noqa: BLE001
            pass


def install() -> None:
    """Attach the sink to our logger tree + start the flusher. Idempotent.
    Capture level from CV_LOG_CAPTURE_LEVEL (default info — warning/error always
    stored; info kept so the view can filter to it; debug only when configured)."""
    global _installed
    if _installed:
        return
    import os
    lvl = _LEVELS.get(os.environ.get("CV_LOG_CAPTURE_LEVEL", "info").lower(), logging.INFO)
    handler = _SinkHandler()
    handler.setLevel(lvl)
    for name in ("cv", "arkive", "app"):
        lg = logging.getLogger(name)
        lg.addHandler(handler)
        if lg.level == logging.NOTSET or lg.level > lvl:
            lg.setLevel(lvl)
    threading.Thread(target=_flusher_loop, daemon=True, name="cv-logsink").start()
    _installed = True
    logger.info("log sink installed (capture level=%s)", _LEVEL_NAME.get(lvl, "info"))
