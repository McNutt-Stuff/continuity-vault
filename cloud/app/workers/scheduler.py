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
from ..models import Collection, ConnectorAccount, PendingAction
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


def _effective_interval(c: Collection, default_minutes: int, caps=None) -> int:
    """Resolve a mapping's cadence: NULL → global default; value as-is otherwise.
    Full-refetch sources (streaming, no delta) re-pull the whole library each run,
    so when the admin hasn't set a cadence they default to daily, not the global
    (usually hourly) default."""
    if c.backup_interval_minutes is not None:
        return c.backup_interval_minutes
    if caps is not None and caps.streaming and not caps.delta:
        return max(default_minutes, 1440)
    return default_minutes


def _is_due(c: Collection, interval_minutes: int, now: datetime) -> bool:
    if interval_minutes <= 0:
        return False  # manual only / disabled
    if c.last_backup_run_at is None:
        return True
    last = c.last_backup_run_at
    if last.tzinfo is not None:
        last = last.replace(tzinfo=None)
    return (now - last) >= timedelta(minutes=interval_minutes)


def _maybe_photo_reminder(db, collection, now: datetime) -> None:
    """Create a 'pick new photos' task when a picker source is overdue and no
    open action already exists (cadence from config.reminderDays, default 3)."""
    if not collection.connector_account_id:
        return
    days = int((collection.config or {}).get("reminderDays") or 3)
    if days <= 0:
        return  # reminders disabled for this mapping
    acct = db.get(ConnectorAccount, collection.connector_account_id)
    last = acct.last_sync_at if acct else None
    if last is not None and last.tzinfo is not None:
        last = last.replace(tzinfo=None)
    if last is not None and (now - last) < timedelta(days=days):
        return
    open_exists = (db.query(PendingAction)
                   .filter(PendingAction.collection_id == collection.id,
                           PendingAction.status == "open").first())
    if open_exists:
        return
    db.add(PendingAction(
        tenant_id=collection.tenant_id, kind="photos_pick",
        collection_id=collection.id, source_type=collection.source_type,
        title="Add new Google Photos",
        message="Pick recent photos or albums to back up. Items already saved are skipped automatically.",
        due_at=now))
    db.commit()


def run_due() -> int:
    """Run a backup for every mapping whose cadence is due. Returns count run."""
    settings = get_settings()
    default_minutes = max(1, settings.sync_interval_minutes)
    now = _now()
    ran = 0
    with SessionLocal() as db:
        for c in db.query(Collection).all():
            conn = get_connector(c.source_type)
            if conn is None:
                continue
            caps = conn.capabilities()
            # Picker sources (Google Photos) can't auto-pull — instead remind the
            # user on a cadence to pick new items.
            if caps.picker:
                try:
                    _maybe_photo_reminder(db, c, now)
                except Exception as exc:  # noqa: BLE001
                    db.rollback()
                    logger.warning("photo reminder failed for %s: %s", c.id, exc)
                continue
            interval = _effective_interval(c, default_minutes, caps)
            if not _is_due(c, interval, now):
                continue
            try:
                if caps.requires_agent:
                    # Agent-collected sources are PUSH: the endpoint agent owns the
                    # cadence (it knows when it's online and can reach the data) and
                    # runs its own timer from the mapping config it pulls on
                    # heartbeat. The cloud must NOT queue collects on a schedule —
                    # that fired blindly at offline/unreachable endpoints and could
                    # loop. (Manual "Sync now" still queues an explicit one-off.)
                    continue
                if not c.connector_account_id:
                    continue  # nothing to authenticate a pull with
                run_backup(db, c)
                logger.info("scheduled backup: collection=%s (%s) source=%s "
                            "destinations=%s interval=%dmin", c.id, c.name,
                            c.source_type, c.destinations, interval)
                c.last_backup_run_at = now
                db.commit()
                ran += 1
            except Exception as exc:  # noqa: BLE001 - isolate per-source failures
                db.rollback()
                # run_backup already recorded the error on the source; still stamp
                # the run time so a persistently-failing source doesn't retry every
                # tick (it retries on its normal cadence).
                try:
                    c.last_backup_run_at = now
                    db.commit()
                except Exception:
                    db.rollback()
                logger.warning("scheduled backup failed for collection %s (%s): %s",
                               c.id, c.source_type, exc)
    return ran
