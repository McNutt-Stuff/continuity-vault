"""Centralized database pruning — keeps high-churn tables bounded so the control
plane stays fast. Runs daily from the scheduler and on demand from the debug API.

Windows are conservative: keep recent history, drop stale/transient rows. NEVER
touches the audit log (hash-chained — pruning would break verification) or
snapshot receipts / search index (immutable protected data + recovery points).
Deletes create dead tuples; a periodic VACUUM (autovacuum, or the debug button)
reclaims the space on disk.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import text

logger = logging.getLogger("cv.prune")

# Retention windows (days).
R_CMD_TERMINAL = 7        # completed appliance commands
R_CMD_STALE = 1          # never-acked commands a dead appliance can't TTL-expire
R_SYNC_JOBS = 21         # per-run backup/sync trackers
R_INTEGRATION_RUNS = 30  # one row per integration poll
R_BACKUP_RUNS = 180      # infra-backup catalog
R_PENDING_ACTIONS = 30   # resolved reminders
R_NETWORK_USAGE = 60     # stale device×app usage edges
R_NODE_METRICS = 90      # telemetry time-series (also pruned by telemetry.py)


def prune_all(db) -> dict:
    """Prune every bounded-retention table. Each table is committed independently
    so one failure can't abort the rest. Returns per-table row counts."""
    from ..models import (ApplianceCommand, BackupRun, IntegrationRun, NetworkUsage,
                          NodeMetric, PendingAction, SyncJob)
    now = datetime.utcnow()
    counts: dict = {}

    def _do(name: str, fn):
        try:
            counts[name] = int(fn() or 0)
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception("prune %s failed", name)
            counts[name] = None

    # appliance_commands — the #1 bloat source: free inline-ciphertext payloads on
    # completed commands, expire never-acked stragglers, delete long-terminal rows.
    # The envelope-free uses a guarded raw UPDATE so it only touches rows that still
    # carry a payload (idempotent — no daily churn re-writing already-emptied rows).
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        _do("appliance_command_envelopes_freed", lambda: db.execute(text(
            "UPDATE appliance_commands SET envelope = '{}'::json "
            "WHERE status IN ('acked','rejected','expired') AND envelope::text <> '{}'")).rowcount)
    _do("appliance_commands_expired_stale", lambda: db.query(ApplianceCommand).filter(
        ApplianceCommand.status.in_(["pending", "delivered"]),
        ApplianceCommand.created_at < now - timedelta(days=R_CMD_STALE)).update(
            {ApplianceCommand.status: "expired", ApplianceCommand.envelope: {}},
            synchronize_session=False))
    _do("appliance_commands_deleted", lambda: db.query(ApplianceCommand).filter(
        ApplianceCommand.status.in_(["acked", "rejected", "expired"]),
        ApplianceCommand.created_at < now - timedelta(days=R_CMD_TERMINAL)).delete(
            synchronize_session=False))

    _do("sync_jobs_deleted", lambda: db.query(SyncJob).filter(
        SyncJob.status.in_(["done", "failed", "cancelled"]),
        SyncJob.created_at < now - timedelta(days=R_SYNC_JOBS)).delete(
            synchronize_session=False))

    _do("integration_runs_deleted", lambda: db.query(IntegrationRun).filter(
        IntegrationRun.created_at < now - timedelta(days=R_INTEGRATION_RUNS)).delete(
            synchronize_session=False))

    _do("backup_runs_deleted", lambda: db.query(BackupRun).filter(
        BackupRun.created_at < now - timedelta(days=R_BACKUP_RUNS)).delete(
            synchronize_session=False))

    _do("pending_actions_deleted", lambda: db.query(PendingAction).filter(
        PendingAction.status.in_(["done", "dismissed"]),
        PendingAction.created_at < now - timedelta(days=R_PENDING_ACTIONS)).delete(
            synchronize_session=False))

    _do("network_usage_deleted", lambda: db.query(NetworkUsage).filter(
        NetworkUsage.last_seen.isnot(None),
        NetworkUsage.last_seen < now - timedelta(days=R_NETWORK_USAGE)).delete(
            synchronize_session=False))

    _do("node_metrics_deleted", lambda: db.query(NodeMetric).filter(
        NodeMetric.ts < now - timedelta(days=R_NODE_METRICS)).delete(
            synchronize_session=False))

    nonzero = {k: v for k, v in counts.items() if v}
    if nonzero:
        logger.info("db prune: %s", nonzero)
    return counts
