"""Background backup/sync scheduler.

A daemon thread ticks on a short base interval and runs a backup for every
mapping whose per-mapping cadence is due. Each mapping can set its own
``backup_interval_minutes`` (NULL = the global default, 0 = manual only); the
scheduler tracks ``last_backup_run_at`` so it only runs a mapping when due.
Cloud-connector sources run an incremental (delta) backup; agent-collected
sources get a ``collect`` command queued to their bound desktop agent.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from ..config import get_settings
from ..connectors import get_connector
from ..db import SessionLocal
from ..models import Collection, DesktopAgent
from .sync_worker import run_backup

logger = logging.getLogger("cv.scheduler")

_thread: threading.Thread | None = None


def _now() -> datetime:
    # Naive UTC to match the tz-naive DateTime columns.
    return datetime.now(timezone.utc).replace(tzinfo=None)


def start_scheduler() -> None:
    global _thread
    settings = get_settings()
    if not settings.sync_enabled or _thread is not None:
        return
    tick = max(15, settings.scheduler_tick_seconds)

    def loop() -> None:
        # Small initial delay so startup/migrations settle first.
        time.sleep(min(tick, 30))
        while True:
            try:
                run_due()
            except Exception:  # noqa: BLE001 - never let the thread die
                logger.exception("scheduled backup cycle failed")
            try:
                _purge_recovered()
            except Exception:  # noqa: BLE001
                logger.exception("recovery purge failed")
            time.sleep(tick)

    _thread = threading.Thread(target=loop, name="cv-sync-scheduler", daemon=True)
    _thread.start()
    logger.info("backup scheduler started (tick %ds, default cadence %d min)",
                tick, settings.sync_interval_minutes)


def _purge_recovered() -> None:
    """Destroy expired recovery windows (temporary decrypted items)."""
    from ..api.recovery import purge_expired
    with SessionLocal() as db:
        n = purge_expired(db)
        if n:
            logger.info("purged %d expired recovery window(s)", n)


def _effective_interval(c: Collection, default_minutes: int) -> int:
    """Resolve a mapping's cadence: NULL → global default; value as-is otherwise."""
    return default_minutes if c.backup_interval_minutes is None else c.backup_interval_minutes


def _is_due(c: Collection, interval_minutes: int, now: datetime) -> bool:
    if interval_minutes <= 0:
        return False  # manual only / disabled
    if c.last_backup_run_at is None:
        return True
    last = c.last_backup_run_at
    if last.tzinfo is not None:
        last = last.replace(tzinfo=None)
    return (now - last) >= timedelta(minutes=interval_minutes)


def _queue_agent_collect(db, c: Collection) -> int:
    """Queue a collect command to the agent(s) bound to this mapping."""
    q = db.query(DesktopAgent).filter(DesktopAgent.tenant_id == c.tenant_id)
    if c.agent_id:
        agents = q.filter(DesktopAgent.id == c.agent_id).all()
    else:
        agents = [a for a in q.all() if c.source_type in (a.collectors or [])]
    queued = 0
    for a in agents:
        a.pending_command = {"type": "collect", "params": {}}
        queued += 1
    return queued


def run_due() -> int:
    """Run a backup for every mapping whose cadence is due. Returns count run."""
    settings = get_settings()
    default_minutes = max(1, settings.sync_interval_minutes)
    now = _now()
    ran = 0
    with SessionLocal() as db:
        for c in db.query(Collection).all():
            interval = _effective_interval(c, default_minutes)
            if not _is_due(c, interval, now):
                continue
            conn = get_connector(c.source_type)
            if conn is None:
                continue
            caps = conn.capabilities()
            try:
                logger.info("scheduled backup due: collection=%s (%s) source=%s "
                            "destinations=%s interval=%dmin", c.id, c.name,
                            c.source_type, c.destinations, interval)
                if caps.requires_agent:
                    # Agent sources are pushed: queue a collect for the bound agent.
                    if _queue_agent_collect(db, c) == 0:
                        continue  # no agent available — retry next tick, don't mark run
                else:
                    if not c.connector_account_id:
                        continue  # nothing to authenticate a pull with
                    # Delta sources sync incrementally; non-delta sources only run
                    # on a cadence the admin set explicitly (a full re-snapshot).
                    if not caps.delta and c.backup_interval_minutes is None:
                        continue
                    run_backup(db, c)
                c.last_backup_run_at = now
                db.commit()
                ran += 1
            except Exception as exc:  # noqa: BLE001 - isolate per-source failures
                db.rollback()
                logger.warning("scheduled backup failed for collection %s (%s): %s",
                               c.id, c.source_type, exc)
    return ran
