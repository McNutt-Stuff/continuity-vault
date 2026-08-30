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
from pathlib import Path

from sqlalchemy import text

from ..config import get_settings
from ..connectors import get_connector
from ..db import WorkerSessionLocal as SessionLocal
from ..models import Collection, ConnectorAccount, PendingAction, SyncJob, Tenant
from .sync_worker import run_backup
from .. import node_config

logger = logging.getLogger("cv.scheduler")

_thread: threading.Thread | None = None
_scope_logged = False
_last_cloud_purge: datetime | None = None
_last_insights: datetime | None = None


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

    def _current_tick() -> int:
        # Re-read each cycle so a configuration profile can retune the cadence live.
        try:
            with SessionLocal() as db:
                return max(15, node_config.get_int(db, "CV_SCHEDULER_TICK_SECONDS",
                                                   settings.scheduler_tick_seconds))
        except Exception:  # noqa: BLE001
            return tick

    def loop() -> None:
        # Small initial delay so startup/migrations settle first.
        time.sleep(min(tick, 30))
        while True:
            try:
                _apply_node_timezone()
            except Exception:  # noqa: BLE001
                logger.exception("timezone apply failed")
            try:
                run_due()
            except Exception:  # noqa: BLE001 - never let the thread die
                logger.exception("scheduled backup cycle failed")
            try:
                from .jobs import reap_stale_jobs
                reap_stale_jobs()
            except Exception:  # noqa: BLE001
                logger.exception("stale-job reap failed")
            try:
                _purge_recovered()
            except Exception:  # noqa: BLE001
                logger.exception("recovery purge failed")
            try:
                _purge_cloud_unsubscribed()
            except Exception:  # noqa: BLE001
                logger.exception("cloud-unsubscribe purge failed")
            try:
                _prune_db()
            except Exception:  # noqa: BLE001
                logger.exception("db prune failed")
            try:
                _run_daily_insights()
            except Exception:  # noqa: BLE001
                logger.exception("insight generation failed")
            try:
                _test_customer_storages()
            except Exception:  # noqa: BLE001
                logger.exception("customer storage health test failed")
            try:
                _run_notifications()
            except Exception:  # noqa: BLE001
                logger.exception("notification sweep failed")
            try:
                _run_contact_directory()
            except Exception:  # noqa: BLE001
                logger.exception("contact directory build failed")
            time.sleep(_current_tick())

    _thread = threading.Thread(target=loop, name="cv-sync-scheduler", daemon=True)
    _thread.start()
    logger.info("backup scheduler started (tick %ds, default cadence %d min, node=%s role=%s mode=%s)",
                tick, settings.sync_interval_minutes,
                settings.node_name or settings.domain,
                settings.node_role or "control-plane",
                "federated" if settings.node_sync_scope else "single")


_last_storage_test: datetime | None = None


def _apply_node_timezone() -> None:
    """Apply the node's configured CV_TIMEZONE to (a) this process — so Python
    local-time + log timestamps reflect it — and (b) best-effort the host system
    timezone so journalctl / OS logs match. The system change needs sudo rights on
    ``timedatectl`` (granted by the installer's sudoers); it is a no-op otherwise."""
    with SessionLocal() as db:
        name = node_config.apply_process_timezone(db)
    if not name:
        return
    global _applied_system_tz
    if name == _applied_system_tz:
        return
    try:
        current = ""
        try:
            current = Path("/etc/timezone").read_text().strip()
        except Exception:  # noqa: BLE001
            current = ""
        if current != name:
            import subprocess
            r = subprocess.run(["sudo", "-n", "timedatectl", "set-timezone", name],
                               capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                logger.info("system timezone set to %s", name)
            else:
                logger.debug("system timezone not changed (%s): %s",
                             r.returncode, (r.stderr or "").strip())
        _applied_system_tz = name
    except Exception:  # noqa: BLE001 — never let a tz change break the loop
        _applied_system_tz = name


_applied_system_tz: str | None = None


def _test_customer_storages() -> None:
    """Periodically round-trip a probe against each enabled customer storage so
    the Cloud Storage page reflects fresh health (at most every 30 minutes)."""
    global _last_storage_test
    now = datetime.utcnow()
    if _last_storage_test and (now - _last_storage_test) < timedelta(minutes=30):
        return
    _last_storage_test = now
    from .. import customer_storage as cs_mod
    from ..models import CustomerStorage
    with SessionLocal() as db:
        for cs in (db.query(CustomerStorage)
                   .filter(CustomerStorage.enabled == True).all()):  # noqa: E712
            try:
                ok, err = cs_mod.test_storage(db, cs)
                cs.last_test_at = now
                cs.last_test_ok = ok
                cs.last_test_error = err or None
                cs.status = "healthy" if ok else "error"
                db.commit()
            except Exception:  # noqa: BLE001
                logger.exception("storage health test failed for %s", cs.id)
                db.rollback()


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


_last_cmd_prune: datetime | None = None
_indexes_ensured = False


def _ensure_perf_indexes() -> None:
    """Build heavy indexes CONCURRENTLY (outside the startup path + no write lock)
    so a huge table can never stall boot / fail the deploy health check."""
    from ..db import worker_engine
    if worker_engine.dialect.name != "postgresql":
        return
    stmts = [
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_appliance_commands_appliance_status "
        "ON appliance_commands (appliance_id, status)",
        # audit.record() fetches the newest row (ORDER BY created_at DESC) on every
        # audited action — without this it full-scans + sorts the whole ledger each write.
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_audit_events_created_at "
        "ON audit_events (created_at)",
        # Unified search + billing/dashboard/org read one current row per object;
        # this partial index serves both the facet GROUP BYs and the newest-first page.
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_search_documents_current "
        "ON search_documents (tenant_id, vault_id, modified_at) WHERE is_current",
    ]
    for s in stmts:
        try:
            with worker_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
                conn.exec_driver_sql(s)
        except Exception:  # noqa: BLE001
            logger.exception("ensure perf index failed: %s", s)


def _backfill_search_current() -> None:
    """One-time: clear is_current on superseded search_documents rows so exactly one
    current row remains per (tenant, source_type, object_id). New writes maintain the
    flag going forward; this repairs legacy rows. Runs in the background (never at
    startup) and only once, guarded by a persisted flag."""
    from ..db import worker_engine
    from ..models import SystemSetting
    if worker_engine.dialect.name != "postgresql":
        return
    with SessionLocal() as db:
        flag = db.get(SystemSetting, "search_is_current_backfilled")
        if flag and flag.value == "1":
            return
        db.execute(text(
            "UPDATE search_documents s SET is_current = FALSE "
            "WHERE s.is_current AND EXISTS (SELECT 1 FROM search_documents n "
            "WHERE n.tenant_id = s.tenant_id AND n.source_type = s.source_type "
            "AND n.object_id = s.object_id AND (n.created_at > s.created_at "
            "OR (n.created_at = s.created_at AND n.id > s.id)))"))
        db.merge(SystemSetting(key="search_is_current_backfilled", value="1"))
        db.commit()
        logger.info("search is_current backfill complete")


def _prune_db() -> None:
    """Keep high-churn tables bounded (appliance_commands, sync_jobs, integration_runs,
    backup_runs, pending_actions, network_usage, node_metrics). At most once a day.
    Also builds perf indexes + the search is_current backfill once, in the background
    (never during startup)."""
    global _last_cmd_prune, _indexes_ensured
    now = datetime.utcnow()
    if _last_cmd_prune and (now - _last_cmd_prune) < timedelta(hours=24):
        return
    _last_cmd_prune = now
    from .pruning import prune_all
    with SessionLocal() as db:
        prune_all(db)
    if not _indexes_ensured:
        _indexes_ensured = True
        try:
            _backfill_search_current()
        except Exception:  # noqa: BLE001
            logger.exception("search is_current backfill failed")
        _ensure_perf_indexes()


def _run_daily_insights() -> None:
    """Refresh every enabled user's digital-footprint insights once per day so the
    Insights page always loads instantly. Checked at most every ~24h."""
    global _last_insights
    now = datetime.utcnow()
    if _last_insights and (now - _last_insights) < timedelta(hours=24):
        return
    _last_insights = now
    from .insights import generate_all
    generate_all()


_last_notif_sweep: datetime | None = None
_last_contact_build: datetime | None = None


def _run_contact_directory() -> None:
    """Rebuild the contact directory (phone/email → name) for users who opted in,
    for the tenants THIS node is responsible for. Rate-limited to a few hours —
    it's derived from the contact index which changes slowly."""
    global _last_contact_build
    now = datetime.utcnow()
    if _last_contact_build and (now - _last_contact_build) < timedelta(hours=6):
        return
    _last_contact_build = now
    from .. import contacts
    from ..models import User
    settings = get_settings()
    is_cp = (settings.node_role or "control-plane") == "control-plane"
    with SessionLocal() as db:
        assigned: set[str] = set()
        if is_cp:
            assigned = {tid for (tid,) in
                        db.query(Tenant.id).filter(Tenant.node_id.isnot(None)).all()}
        users = [u for u in db.query(User).filter(
                    User.status == "active",
                    User.contact_linking_enabled.is_(True)).all()
                 if u.tenant_id not in assigned]
        for u in users:
            try:
                contacts.build_directory(db, u)
            except Exception:  # noqa: BLE001
                db.rollback()
                logger.exception("contact directory build failed for %s", u.id)


def _run_notifications() -> None:
    """Send user email notifications for the tenants THIS node is responsible for,
    using this node's own assigned email service — a customer node emails its own
    tenants' daily/source-problem digests; the control plane emails the tenants it
    still handles (not assigned to a node). Neither routes mail through the other.
    Higher-level mail (password reset, welcome, plan change) is sent directly by the
    control-plane request handlers. Per-user dedupe keys make sends idempotent, so
    this can sweep often (~15 min) without duplicate mail."""
    global _last_notif_sweep
    now = _now()
    if _last_notif_sweep and (now - _last_notif_sweep) < timedelta(minutes=15):
        return
    _last_notif_sweep = now
    from .. import notifications as notif
    from ..models import User
    settings = get_settings()
    is_cp = (settings.node_role or "control-plane") == "control-plane"
    with SessionLocal() as db:
        # The control plane skips tenants assigned to a node (that node emails them);
        # a customer node's local DB only holds its own tenants, so it runs them all.
        assigned: set[str] = set()
        if is_cp:
            assigned = {tid for (tid,) in
                        db.query(Tenant.id).filter(Tenant.node_id.isnot(None)).all()}
        users = [u for u in db.query(User).filter(User.status == "active").all()
                 if u.tenant_id not in assigned]
        # The daily digest fires at notif.daily_hour in the NODE's timezone, once
        # per LOCAL day (dedupe key = local date).
        local = node_config.local_now(db)
        today = local.strftime("%Y-%m-%d")
        daily_hour = node_config.get_int(db, "notif.daily_hour", 0)
        send_daily = local.hour >= daily_hour
        for u in users:
            if send_daily:
                try:
                    notif.send_notification(db, u, "daily_summary", dedupe_key=f"daily:{today}")
                except Exception:  # noqa: BLE001
                    db.rollback()
                    logger.exception("daily summary failed for %s", u.id)
            try:
                _notify_source_problems(db, notif, u, now)
            except Exception:  # noqa: BLE001
                db.rollback()
                logger.exception("source-problem notify failed for %s", u.id)
        # Weekly org roll-up (Mondays in the node's timezone), to org admins.
        if local.weekday() == 0:
            try:
                _run_weekly_org(db, notif, assigned, now)
            except Exception:  # noqa: BLE001
                db.rollback()
                logger.exception("weekly org summary failed")


def _notify_source_problems(db, notif, user, now: datetime) -> None:
    issues = notif._source_issues(db, user)
    if not issues:
        return
    # Consolidated, repeated at most every N hours (admin-controlled). The dedupe
    # key is a time bucket, so the same issues re-notify only once per window.
    repeat_h = notif.source_repeat_hours(db)
    bucket = int(now.timestamp() // (repeat_h * 3600))
    notif.send_notification(db, user, "source_problem",
                            dedupe_key=f"srcprob:{bucket}", issues=issues)


def _run_weekly_org(db, notif, assigned: set[str], now: datetime) -> None:
    from ..models import User
    week = now.strftime("%Y-W%W")
    org_tenants = [t for t in db.query(Tenant)
                   .filter(Tenant.tenant_type != "shared").all()
                   if t.id not in assigned]
    for t in org_tenants:
        admins = (db.query(User)
                  .filter(User.tenant_id == t.id, User.status == "active",
                          User.role.in_(["owner", "security-admin"])).all())
        for u in admins:
            try:
                notif.send_notification(db, u, "weekly_org", dedupe_key=f"weekly:{week}")
            except Exception:  # noqa: BLE001
                db.rollback()
                logger.exception("weekly org summary failed for %s", u.id)


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
    # Dual-track sources: keep the independent deep-history backfill progressing in
    # its own background job while the fast recent delta below runs on schedule.
    if caps.dual_track:
        try:
            from .jobs import ensure_backfill_running
            ensure_backfill_running(db, c)
        except Exception as exc:  # noqa: BLE001 - never abort scheduling on this
            db.rollback()
            logger.warning("backfill ensure failed for %s: %s", c.id, exc)
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
        # A configuration profile can retune the default cadence live.
        default_minutes = max(1, node_config.get_int(
            db, "CV_SYNC_INTERVAL_MINUTES", settings.sync_interval_minutes))
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
