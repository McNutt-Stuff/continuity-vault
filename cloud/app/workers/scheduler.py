"""Background delta-sync scheduler.

A daemon thread wakes every ``sync_interval_minutes`` and runs an incremental
backup for every cloud-connector source (agent-collected sources are pushed by
the agent, so they're skipped). The first run per source does a full paginated
backup; subsequent runs pull only deltas via the stored connector cursor.
"""

from __future__ import annotations

import logging
import threading
import time

from ..config import get_settings
from ..connectors import get_connector
from ..db import SessionLocal
from ..models import Collection
from .sync_worker import run_backup

logger = logging.getLogger("cv.scheduler")

_thread: threading.Thread | None = None


def start_scheduler() -> None:
    global _thread
    settings = get_settings()
    if not settings.sync_enabled or _thread is not None:
        return
    interval = max(1, settings.sync_interval_minutes) * 60

    def loop() -> None:
        # Small initial delay so startup/migrations settle first.
        time.sleep(min(interval, 120))
        while True:
            try:
                run_due()
            except Exception:  # noqa: BLE001 - never let the thread die
                logger.exception("scheduled sync cycle failed")
            time.sleep(interval)

    _thread = threading.Thread(target=loop, name="cv-sync-scheduler", daemon=True)
    _thread.start()
    logger.info("connector sync scheduler started (every %d min)",
                settings.sync_interval_minutes)


def run_due() -> int:
    """Run a delta backup for every cloud-connector source. Returns count synced."""
    synced = 0
    with SessionLocal() as db:
        collections = db.query(Collection).all()
        for c in collections:
            conn = get_connector(c.source_type)
            if conn is None or conn.capabilities().requires_agent:
                continue  # agent sources are pushed, not pulled
            if not conn.capabilities().delta:
                continue  # non-delta sources would re-snapshot everything each cycle
            if not c.connector_account_id:
                continue  # nothing to authenticate a pull with
            try:
                run_backup(db, c)
                synced += 1
            except Exception as exc:  # noqa: BLE001 - isolate per-source failures
                logger.warning("scheduled sync failed for collection %s (%s): %s",
                               c.id, c.source_type, exc)
    return synced
