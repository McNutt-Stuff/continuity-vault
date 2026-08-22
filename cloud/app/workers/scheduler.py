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
from ..models import Collection, ConnectorAccount, PendingAction, SyncJob, Tenant
from .sync_worker import run_backup

logger = logging.getLogger("cv.scheduler")

_thread: threading.Thread | None = None
_scope_logged = False
_last_cloud_purge: datetime | None = None


def _now() -> datetime:
    # Naive UTC to match the tz-naive DateTime columns.
    return datetime.now(timezone.utc).replace(tzinfo=None)


def start_scheduler() -> None:
    global _thread
    settings = get_settings()
    if not settings.sync_enabled:
        logger.warning("backup scheduler DISABLED (CV_SYNC_ENABLED is false) — "
                       "no automatic backups will run until it is re-enabled")
        return
    if _thread is not None:
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
            try:
                _purge_cloud_unsubscribed()
            except Exception:  # noqa: BLE001
                logger.exception("cloud-unsubscribe purge failed")
            time.sleep(tick)

    _thread = threading.Thread(target=loop, name="cv-sync-scheduler", daemon=True)
    _thread.start()
    logger.info("backup scheduler started (tick %ds, default cadence %d min, node=%s role=%s mode=%s)",
                tick, settings.sync_interval_minutes,
                settings.node_name or settings.domain,
                settings.node_role or "control-plane",
                "federated" if settings.node_sync_scope else "single")


def _purge_cloud_unsubscribed() -> None:
    """Permanently delete Arkive Cloud data for tenants/accounts whose 30-day
    unsubscribe grace has elapsed. Checked at most hourly."""
    global _last_cloud_purge
    now = datetime.utcnow()
    if _last_cloud_purge and (now - _last_cloud_purge) < timedelta(hours=1):
        return
    _last_cloud_purge = now
    from ..api.billing import purge_cloud_data
    from ..models import User, Vault
    with SessionLocal() as db:
        # Org tenants: delete across all tenant vaults.
        for t in (db.query(Tenant)
                  .filter(Tenant.cloud_delete_at.isnot(None),
                          Tenant.cloud_delete_at <= now).all()):
            vids = [v.id for v in db.query(Vault).filter(Vault.tenant_id == t.id).all()]
            res = purge_cloud_data(db, t, vids)
            t.cloud_delete_at = None
            db.commit()
            logger.warning("cloud unsubscribe purge: tenant=%s receipts=%d", t.id, res["receipts"])
        # Shared/personal accounts: delete across the user's own vaults.
        for u in (db.query(User)
                  .filter(User.cloud_delete_at.isnot(None),
                          User.cloud_delete_at <= now).all()):
            t = db.get(Tenant, u.tenant_id)
            if t is None:
                u.cloud_delete_at = None
                db.commit()
                continue
            vids = [v.id for v in db.query(Vault).filter(
                Vault.tenant_id == u.tenant_id, Vault.owner_user_id == u.id).all()]
            res = purge_cloud_data(db, t, vids)
            u.cloud_delete_at = None
            db.commit()
            logger.warning("cloud unsubscribe purge: user=%s receipts=%d", u.id, res["receipts"])


def _purge_recovered() -> None:
    """Destroy expired recovery windows (temporary decrypted items)."""
    from ..api.recovery import purge_expired
    with SessionLocal() as db:
        n = purge_expired(db)
        if n:
            logger.info("purged %d expired recovery window(s)", n)


def _effective_interval(c: Collection, default_minutes: int, caps=None) -> int:
    """Resolve a mapping's cadence: NULL → the global default; value as-is otherwise.
    A mapping that wants a slower cadence (e.g. a full-refetch source) sets its own
    ``backup_interval_minutes`` — "Default" always means the configured default."""
    if c.backup_interval_minutes is not None:
        return c.backup_interval_minutes
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


def _process_collection(db, c: Collection, now: datetime, default_minutes: int) -> tuple[int, int, int]:
    """Handle one mapping for this cycle. Returns (eligible, due, ran) as 0/1
    counters. Raising is fine — the caller isolates it so one broken source can
    never abort the whole scheduling cycle."""
    conn = get_connector(c.source_type)
    if conn is None:
        return (0, 0, 0)
    caps = conn.capabilities()
    # Picker sources (Google Photos) can't auto-pull — remind the user instead.
    if caps.picker:
        try:
            _maybe_photo_reminder(db, c, now)
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            logger.warning("photo reminder failed for %s: %s", c.id, exc)
        return (0, 0, 0)
    interval = _effective_interval(c, default_minutes, caps)
    # Agent-collected sources are PUSH (the endpoint owns its cadence); a source
    # with no linked account can't be pulled. Neither is cloud-schedulable.
    if caps.requires_agent or not c.connector_account_id:
        return (0, 0, 0)
    # Deactivated (unlinked) sources keep their data but don't sync.
    acct = db.get(ConnectorAccount, c.connector_account_id)
    if acct is not None and acct.active is False:
        return (0, 0, 0)
    if not _is_due(c, interval, now):
        return (1, 0, 0)
    # Track the scheduled run as a job so the admin worker view shows it (and that
    # it was schedule-triggered, vs a manual "Back up now").
    job = SyncJob(tenant_id=c.tenant_id, collection_id=c.id, kind="backup",
                  trigger="schedule", node_id=None, status="running",
                  message="Scheduled backup", started_at=now)
    db.add(job)
    db.commit()
    from .jobs import capture_job_log
    with capture_job_log() as cap:
        logger.info("scheduled backup starting: %s (%s) → %s interval=%dmin",
                    c.name, c.source_type, c.destinations or ["cv-cloud"], interval)
        try:
            run_backup(db, c)
            logger.info("scheduled backup complete: collection=%s (%s) source=%s "
                        "destinations=%s", c.id, c.name, c.source_type, c.destinations)
            c.last_backup_run_at = now
            acct = db.get(ConnectorAccount, c.connector_account_id)
            job.status = "done"
            job.processed = job.total = int((acct.last_object_count or 0) if acct else 0)
            job.message = "Scheduled backup complete"
            job.log = (cap.records or [])[-800:]
            job.finished_at = _now()
            db.commit()
            return (1, 1, 1)
        except Exception as exc:  # noqa: BLE001 - isolate per-source failures
            logger.exception("scheduled backup failed for collection %s (%s)",
                             c.id, c.source_type)
            records = list(cap.records or [])
            db.rollback()
            # run_backup already recorded the error on the source; still stamp the run
            # time so a persistently-failing source doesn't retry every tick.
            try:
                c.last_backup_run_at = now
                job = db.get(SyncJob, job.id)
                if job is not None:
                    job.status = "failed"
                    job.error = str(exc)[:500]
                    job.log = records[-800:]
                    job.finished_at = _now()
                db.commit()
            except Exception:
                db.rollback()
            return (1, 1, 0)


def run_due() -> int:
    """Run a backup for every mapping whose cadence is due. Returns count run.

    In federated mode the control plane SKIPS tenants assigned to a node (that
    node runs their sync against its own local database); a customer node only
    has its own tenants locally, so it processes them all."""
    global _scope_logged
    settings = get_settings()
    is_cp = (settings.node_role or "control-plane") == "control-plane"
    # Ownership is authoritative: the control plane NEVER runs a tenant that is
    # assigned to a node (node_id set) — that node owns its sync. This holds even
    # if the federation flag isn't set on this box, so an assigned tenant can't be
    # double-run. A customer node only has its own tenants locally, so it runs all.
    skip_assigned = is_cp
    default_minutes = max(1, settings.sync_interval_minutes)
    now = _now()
    total = eligible = due = ran = skipped_node = 0
    if skip_assigned and not _scope_logged:
        logger.info("scheduling scope: role=%s node=%s — skipping tenants assigned to a node",
                    settings.node_role or "control-plane",
                    settings.node_name or settings.domain)
        _scope_logged = True
    with SessionLocal() as db:
        assigned: set[str] = set()
        if skip_assigned:
            assigned = {tid for (tid,) in
                        db.query(Tenant.id).filter(Tenant.node_id.isnot(None)).all()}
        for c in db.query(Collection).all():
            total += 1
            if c.tenant_id in assigned:
                skipped_node += 1
                continue
            try:
                e, d, r = _process_collection(db, c, now, default_minutes)
                eligible += e
                due += d
                ran += r
            except Exception as exc:  # noqa: BLE001 - never abort the whole cycle
                db.rollback()
                logger.warning("scheduler: skipping collection %s (%s): %s",
                               c.id, c.source_type, exc)
    log = logger.info if ran else logger.debug
    log("scheduler cycle: mappings=%d cloud-eligible=%d due=%d ran=%d on-another-node=%d",
        total, eligible, due, ran, skipped_node)
    return ran
