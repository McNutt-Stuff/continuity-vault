"""Scheduled integrity validation of replicated search indexes.

Re-reads each index replica from its storage destination, decrypts it, and
verifies it opens and its row count matches what was written — proving the DR
copy is intact and restorable. Updates each replica's health/status and logs
verbosely. Runs on the server that owns the scope (same node-ownership rule as
index replication). Surfaces its live status to the Node → Processes tab via
``workers.status``.
"""

from __future__ import annotations

import logging
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .. import credstore
from ..db import SessionLocal
from ..models import IndexReplica, Tenant
from . import status as worker_status
from . import index_replication as repl

logger = logging.getLogger("cv.integrity")

VERIFY_INTERVAL_SECONDS = 12 * 3600      # re-check each replica at most this often
_RUN_EVERY_SECONDS = 3600                 # how often the loop looks for due replicas
_last_run: Optional[datetime] = None


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _verify_one(db, tenant: Tenant, r: IndexReplica) -> None:
    dest, prefix = repl._resolve_object_dest(db, tenant, r.destination)
    if dest is None:
        return  # appliance/unsupported target — verified when localized index lands
    logger.info("integrity: verifying index replica scope=%s:%s dest=%s",
                r.scope, r.scope_id, r.destination)
    try:
        cipher = dest.get_object(prefix, r.key or repl._INDEX_KEY)
        raw = credstore.decrypt_bytes(f"index:{tenant.id}", cipher)
    except Exception as exc:  # noqa: BLE001
        r.status = "error"
        r.error = f"unreadable/undecryptable: {exc}"[:400]
        r.last_verified_at = _now()
        logger.warning("integrity: replica FAILED (fetch/decrypt) scope=%s:%s dest=%s: %s",
                       r.scope, r.scope_id, r.destination, exc)
        return
    tmp = Path(tempfile.mkstemp(prefix="arkive-verify-", suffix=".sqlite")[1])
    try:
        tmp.write_bytes(raw)
        con = sqlite3.connect(str(tmp))
        try:
            rows = con.execute("SELECT count(*) FROM search_documents").fetchone()[0]
            meta = dict(con.execute("SELECT k, v FROM meta").fetchall())
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001
        r.status = "error"
        r.error = f"corrupt index file: {exc}"[:400]
        r.last_verified_at = _now()
        logger.warning("integrity: replica CORRUPT scope=%s:%s dest=%s: %s",
                       r.scope, r.scope_id, r.destination, exc)
        return
    finally:
        tmp.unlink(missing_ok=True)
    expected = int(meta.get("object_count") or r.object_count or 0)
    if rows != expected:
        r.status = "error"
        r.error = f"row-count mismatch: file has {rows}, expected {expected}"
        r.last_verified_at = _now()
        logger.warning("integrity: replica MISMATCH scope=%s:%s dest=%s (%d != %d)",
                       r.scope, r.scope_id, r.destination, rows, expected)
        return
    r.status = "ok"
    r.error = ""
    r.object_count = rows
    r.last_verified_at = _now()
    logger.info("integrity: replica OK scope=%s:%s dest=%s (%d rows verified)",
                r.scope, r.scope_id, r.destination, rows)


def verify_due(force: bool = False) -> None:
    """Verify every owned replica whose last check is older than the interval."""
    global _last_run
    now = _now()
    if not force and _last_run and (now - _last_run).total_seconds() < _RUN_EVERY_SECONDS:
        return
    _last_run = now
    worker_status.record("index-integrity", state="running", health="ok",
                         message="verifying index replicas")
    checked = failed = 0
    try:
        with SessionLocal() as db:
            owned = {(s["scope"], s["scope_id"]): s["tenant"] for s in repl._scopes(db)}
            cutoff = now - timedelta(seconds=VERIFY_INTERVAL_SECONDS)
            replicas = (db.query(IndexReplica)
                        .filter(IndexReplica.status != "pending").all())
            for r in replicas:
                tenant = owned.get((r.scope, r.scope_id))
                if tenant is None:
                    continue  # another server owns this scope
                if not force and r.last_verified_at and r.last_verified_at > cutoff:
                    continue
                try:
                    _verify_one(db, tenant, r)
                    checked += 1
                    if r.status == "error":
                        failed += 1
                except Exception:  # noqa: BLE001
                    db.rollback()
                    logger.exception("integrity: unexpected error verifying replica %s", r.id)
                    continue
                db.commit()
    except Exception as exc:  # noqa: BLE001
        worker_status.record("index-integrity", state="failed", health="error",
                             message=f"integrity run failed: {exc}")
        logger.exception("integrity run failed")
        return
    worker_status.record(
        "index-integrity", state="idle",
        health="error" if failed else "ok",
        message=f"verified {checked} replica(s), {failed} failed" if checked
                else "no replicas due for verification",
        checked=checked, failed=failed)
