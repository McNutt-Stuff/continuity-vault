"""SQLAlchemy engine/session setup for the cloud control plane."""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker, Session

from .config import get_settings

settings = get_settings()

_is_sqlite = settings.database_url.startswith("sqlite")
connect_args = {"check_same_thread": False} if _is_sqlite else {}

# Web-request engine/pool. Postgres gets an explicitly-sized pool with pre-ping
# (drop dead connections) and recycling (avoid stale server-side timeouts).
_pool_kw = {} if _is_sqlite else {
    "pool_size": settings.db_pool_size,
    "max_overflow": settings.db_max_overflow,
    "pool_timeout": settings.db_pool_timeout,
    "pool_recycle": 1800,
    "pool_pre_ping": True,
}
engine = create_engine(settings.database_url, connect_args=connect_args, future=True, **_pool_kw)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

# Dedicated engine/pool for background workers (sync, scheduler, replication,
# telemetry). Long-running backups hold a connection while they work, so routing
# them through a SEPARATE pool guarantees they can never exhaust the web pool and
# take the API down — worker overload only backs up other workers. SQLite (dev)
# shares the single engine since a second pool to one file adds no isolation.
if _is_sqlite:
    worker_engine = engine
    WorkerSessionLocal = SessionLocal
else:
    worker_engine = create_engine(
        settings.database_url, connect_args=connect_args, future=True,
        pool_size=settings.db_worker_pool_size,
        max_overflow=settings.db_worker_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        pool_recycle=1800, pool_pre_ping=True)
    WorkerSessionLocal = sessionmaker(bind=worker_engine, autoflush=False,
                                      autocommit=False, future=True)

Base = declarative_base()


def get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from . import models  # noqa: F401  ensure models are registered

    Base.metadata.create_all(bind=engine)
    _apply_additive_migrations()
    _backfill_appliance_storage()
    _clean_far_future_calendar()


def _clean_far_future_calendar() -> None:
    """Remove stale calendar index rows left by an earlier build that expanded
    recurring series into per-occurrence instances (object ids ``…_YYYYMMDD`` /
    ``…_YYYYMMDDTHHMMSSZ``). Those spread a yearly event across every year to its
    UNTIL (e.g. 2099); the current build stores one event per series dated at its
    next occurrence, so the instance rows are safe to drop."""
    import logging
    import re
    from .models import SearchDocument

    inst_re = re.compile(r"_\d{8}(t\d{6}z)?$", re.IGNORECASE)  # Google instance-id suffix
    try:
        with SessionLocal() as db:
            oids = [oid for (oid,) in
                    db.query(SearchDocument.object_id)
                    .filter(SearchDocument.source_type == "google_calendar")
                    .distinct().all()
                    if oid and inst_re.search(oid)]
            if not oids:
                return
            n = 0
            for i in range(0, len(oids), 400):  # bounded IN() for SQLite/PG limits
                n += (db.query(SearchDocument)
                      .filter(SearchDocument.source_type == "google_calendar",
                              SearchDocument.object_id.in_(oids[i:i + 400]))
                      .delete(synchronize_session=False))
            db.commit()
            logging.getLogger("cv.db").warning(
                "cleaned %d stale recurring-calendar instance row(s)", n)
    except Exception:
        pass  # best-effort cleanup; never block startup


def _backfill_appliance_storage() -> None:
    """Ensure every appliance has a built-in storage object and migrate legacy
    ``appliance:<id>`` / ``appliance`` mapping destinations to ``store:<id>`` so
    mappings reference the storage object (with its own id/name), not the device."""
    from .models import Appliance, ApplianceStorage, Collection

    try:
        with SessionLocal() as db:
            builtin: dict[str, str] = {}
            for a in db.query(Appliance).all():
                store = (db.query(ApplianceStorage)
                         .filter(ApplianceStorage.appliance_id == a.id,
                                 ApplianceStorage.kind == "builtin").first())
                if not store:
                    store = ApplianceStorage(tenant_id=a.tenant_id, appliance_id=a.id,
                                             name="Built-In Storage", kind="builtin")
                    db.add(store)
                    db.flush()
                builtin[a.id] = store.id
            any_store = next(iter(builtin.values()), None)
            for c in db.query(Collection).all():
                dests = c.destinations or []
                new, dirty = [], False
                for d in dests:
                    if isinstance(d, str) and d.startswith("appliance:"):
                        sid = builtin.get(d.split(":", 1)[1])
                        if sid:
                            new.append(f"store:{sid}"); dirty = True; continue
                    elif d == "appliance" and any_store:
                        new.append(f"store:{any_store}"); dirty = True; continue
                    new.append(d)
                if dirty:
                    c.destinations = new
            db.commit()
    except Exception:
        pass  # best-effort backfill; never block startup


def _apply_additive_migrations() -> None:
    """Add new columns to pre-existing tables (prototype-grade, additive only).

    ``create_all`` never alters existing tables, so newly-introduced columns are
    applied here. Each statement runs in its own transaction and is ignored if it
    has already been applied."""
    from sqlalchemy import text

    statements = [
        "ALTER TABLE users ADD COLUMN email_verified BOOLEAN DEFAULT false",
        "ALTER TABLE users ADD COLUMN first_name VARCHAR DEFAULT ''",
        "ALTER TABLE users ADD COLUMN last_name VARCHAR DEFAULT ''",
        "ALTER TABLE users ADD COLUMN phone VARCHAR DEFAULT ''",
        "ALTER TABLE users ADD COLUMN last_login_at TIMESTAMP",
        # One account per email address, platform-wide.
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_user_email ON users (email)",
        "ALTER TABLE users ADD COLUMN feature_flags JSON",
        "ALTER TABLE users ADD COLUMN protection_options JSON",
        "ALTER TABLE users ADD COLUMN cloud_delete_at TIMESTAMP",
        # Setup wizard completion. Existing accounts (created before this shipped)
        # are marked done so only genuinely-new accounts see the wizard; the
        # cutoff makes the backfill a one-time no-op for future users.
        "ALTER TABLE users ADD COLUMN setup_completed_at TIMESTAMP",
        "UPDATE users SET setup_completed_at = COALESCE(created_at, CURRENT_TIMESTAMP) "
        "WHERE setup_completed_at IS NULL AND created_at < '2026-08-28'",
        "ALTER TABLE tenants ADD COLUMN feature_flags JSON",
        "ALTER TABLE tenants ADD COLUMN cloud_delete_at TIMESTAMP",
        "ALTER TABLE recovered_items ADD COLUMN object_modified_at TIMESTAMP",
        "ALTER TABLE connector_accounts ADD COLUMN active BOOLEAN DEFAULT true",
        "ALTER TABLE connector_accounts ADD COLUMN account_username VARCHAR",
        "ALTER TABLE sync_jobs ADD COLUMN node_id VARCHAR",
        "ALTER TABLE sync_jobs ADD COLUMN trigger VARCHAR DEFAULT 'manual'",
        "ALTER TABLE sync_jobs ADD COLUMN log JSON",
        "ALTER TABLE search_documents ADD COLUMN category VARCHAR",
        "ALTER TABLE linking_codes ADD COLUMN kind VARCHAR DEFAULT 'appliance'",
        "ALTER TABLE desktop_agents ADD COLUMN agent_token_hash VARCHAR",
        "ALTER TABLE appliances ADD COLUMN agent_token_hash VARCHAR",
        "ALTER TABLE collections ADD COLUMN destinations JSON",
        "ALTER TABLE collections ADD COLUMN index_fields JSON",
        "ALTER TABLE collections ADD COLUMN agent_id VARCHAR",
        "ALTER TABLE collections ADD COLUMN backup_interval_minutes INTEGER",
        "ALTER TABLE collections ADD COLUMN last_backup_run_at TIMESTAMP",
        "ALTER TABLE collections ADD COLUMN config JSON",
        "ALTER TABLE desktop_agents ADD COLUMN last_scan JSON",
        "ALTER TABLE desktop_agents ADD COLUMN fs_expansions JSON",
        "ALTER TABLE desktop_agents ADD COLUMN pending_commands JSON",
        "ALTER TABLE recovered_items ADD COLUMN version INTEGER",
        "ALTER TABLE recovered_items ADD COLUMN version_created_at TIMESTAMP",
        "ALTER TABLE tenants ADD COLUMN licensed_bytes BIGINT DEFAULT 0",
        "ALTER TABLE tenants ADD COLUMN protection_options JSON",
        "ALTER TABLE tenants ADD COLUMN appliance_plan JSON",
        "ALTER TABLE source_configs ADD COLUMN family VARCHAR",
        "ALTER TABLE pricing_config ADD COLUMN license_plans JSON",
        "ALTER TABLE email_config ADD COLUMN aws_access_key_id VARCHAR",
        "ALTER TABLE email_config ADD COLUMN aws_secret_encrypted VARCHAR",
        "ALTER TABLE nodes ADD COLUMN storage_service_id VARCHAR",
        "ALTER TABLE nodes ADD COLUMN email_service_id VARCHAR",
        "ALTER TABLE nodes ADD COLUMN cloud JSON",
        "ALTER TABLE nodes ADD COLUMN backup_service_ids JSON",
        "ALTER TABLE nodes ADD COLUMN config_overrides JSON",
        "ALTER TABLE nodes ADD COLUMN config_profile_id VARCHAR",
        "ALTER TABLE appliances ADD COLUMN config_profile_id VARCHAR",
        "ALTER TABLE appliances ADD COLUMN backup_service_ids JSON",
        "ALTER TABLE service_objects ADD COLUMN capabilities JSON",
        "ALTER TABLE search_documents ADD COLUMN content_hash VARCHAR",
        "ALTER TABLE search_documents ADD COLUMN version INTEGER DEFAULT 1",
        # One current row per (tenant, source_type, object_id) so reads filter to
        # it instead of de-duplicating the whole index. Metadata-only (constant
        # default) on PG 11+, so it's safe at startup; the scheduler backfills the
        # correct value for legacy rows in the background.
        "ALTER TABLE search_documents ADD COLUMN IF NOT EXISTS is_current BOOLEAN DEFAULT true",
        "ALTER TABLE users ADD COLUMN notification_prefs JSON",
        "ALTER TABLE users ADD COLUMN notification_emails JSON",
        "ALTER TABLE users ADD COLUMN contact_linking_enabled BOOLEAN DEFAULT false",
        "ALTER TABLE users ADD COLUMN last_plan_change_at TIMESTAMP",
        "ALTER TABLE users ADD COLUMN timezone VARCHAR DEFAULT ''",
        "ALTER TABLE billing_profiles ADD COLUMN activated_at TIMESTAMP",
        "ALTER TABLE billing_profiles ADD COLUMN next_charge_at TIMESTAMP",
        "ALTER TABLE billing_profiles ADD COLUMN dunning_attempts INTEGER DEFAULT 0",
        "ALTER TABLE billing_charges ADD COLUMN description VARCHAR DEFAULT ''",
        "ALTER TABLE support_docs ADD COLUMN baseline_hash VARCHAR DEFAULT ''",
        "ALTER TABLE audit_events ADD COLUMN severity VARCHAR DEFAULT 'info'",
        "ALTER TABLE audit_events ADD COLUMN category VARCHAR DEFAULT 'activity'",
        "ALTER TABLE connector_accounts ADD COLUMN sync_cursor JSON",
        "ALTER TABLE connector_accounts ADD COLUMN last_object_count INTEGER",
        "ALTER TABLE connector_accounts ADD COLUMN last_error TEXT",
        "ALTER TABLE connector_accounts ADD COLUMN last_error_at TIMESTAMP",
        "ALTER TABLE connector_accounts ADD COLUMN backfill_cursor JSON",
        "ALTER TABLE connector_accounts ADD COLUMN backfill_done BOOLEAN DEFAULT false",
        "ALTER TABLE connector_accounts ADD COLUMN backfill_started_at TIMESTAMP",
        "ALTER TABLE connector_accounts ADD COLUMN backfill_completed_at TIMESTAMP",
        "ALTER TABLE connector_accounts ADD COLUMN backfill_count INTEGER DEFAULT 0",
        "ALTER TABLE connector_accounts ADD COLUMN fail_count INTEGER DEFAULT 0",
        "ALTER TABLE source_configs ADD COLUMN backfill_enabled BOOLEAN DEFAULT false",
        "ALTER TABLE integration_instances ADD COLUMN provision_state VARCHAR DEFAULT 'idle'",
        "ALTER TABLE integration_instances ADD COLUMN provision_message TEXT",
        "ALTER TABLE integration_instances ADD COLUMN provision_otp VARCHAR",
        "ALTER TABLE integration_instances ADD COLUMN repoll_requested BOOLEAN DEFAULT false",
        "ALTER TABLE network_clients ADD COLUMN nickname VARCHAR DEFAULT ''",
        "ALTER TABLE network_clients ADD COLUMN ownership VARCHAR DEFAULT ''",
        "ALTER TABLE network_clients ADD COLUMN owner_user_id VARCHAR",
        "ALTER TABLE backup_runs ADD COLUMN log JSON",
        # Composite indexes for the hot "whole tenant index, newest-first" scans
        # (unified search, Overview, Protection Setup usage) — the single-column
        # tenant_id/vault_id indexes still left Postgres sorting the whole match.
        "CREATE INDEX IF NOT EXISTS ix_search_documents_tenant_created "
        "ON search_documents(tenant_id, created_at)",
        "CREATE INDEX IF NOT EXISTS ix_search_documents_vault_created "
        "ON search_documents(vault_id, created_at)",
        # Support the per-object DISTINCT ON dedup used by Overview / Protection
        # Setup (newest row per source+object) so Postgres needn't sort the whole
        # tenant index each time.
        "CREATE INDEX IF NOT EXISTS ix_search_documents_tenant_obj "
        "ON search_documents(tenant_id, source_type, object_id, created_at)",
        "CREATE INDEX IF NOT EXISTS ix_search_documents_vault_obj "
        "ON search_documents(vault_id, source_type, object_id, created_at)",
        "CREATE INDEX IF NOT EXISTS ix_snapshot_receipts_tenant_vault "
        "ON snapshot_receipts(tenant_id, vault_id)",
        "ALTER TABLE appliance_storages ADD COLUMN used_bytes INTEGER DEFAULT 0",
        "ALTER TABLE appliance_storages ADD COLUMN health JSON",
        # External / mirror storage: lifecycle state, stable device identity, and
        # 1:1 mirror linkage so reconnected drives are recognized and mirrors shadow
        # their source store.
        "ALTER TABLE appliance_storages ADD COLUMN state VARCHAR DEFAULT 'ready'",
        "ALTER TABLE appliance_storages ADD COLUMN device_serial VARCHAR DEFAULT ''",
        "ALTER TABLE appliance_storages ADD COLUMN mirror_of_id VARCHAR",
        "ALTER TABLE appliance_storages ADD COLUMN last_seen_at TIMESTAMP",
        # Integrity worker records the last successful verify per replica — added
        # to the model after the table first shipped, so backfill the column.
        "ALTER TABLE index_replicas ADD COLUMN IF NOT EXISTS last_verified_at TIMESTAMP",
        "ALTER TABLE nodes ADD COLUMN version_updated_at TIMESTAMP",
        "ALTER TABLE appliances ADD COLUMN version_updated_at TIMESTAMP",
        "ALTER TABLE desktop_agents ADD COLUMN version_updated_at TIMESTAMP",
        # Per-user vault ownership (data partitioning). Backfill legacy/shared
        # vaults to the org owner so no vault is left unassigned.
        "ALTER TABLE vaults ADD COLUMN owner_user_id VARCHAR",
        "UPDATE vaults SET owner_user_id = ("
        "  SELECT u.id FROM users u WHERE u.tenant_id = vaults.tenant_id "
        "  AND u.role = 'owner' ORDER BY u.created_at LIMIT 1) "
        "WHERE owner_user_id IS NULL",
        # Per-user source ownership. Backfill from the vault a source is mapped
        # into, else the org owner.
        "ALTER TABLE connector_accounts ADD COLUMN owner_user_id VARCHAR",
        "UPDATE connector_accounts SET owner_user_id = ("
        "  SELECT v.owner_user_id FROM collections c JOIN vaults v ON v.id = c.vault_id "
        "  WHERE c.connector_account_id = connector_accounts.id "
        "  AND v.owner_user_id IS NOT NULL LIMIT 1) "
        "WHERE owner_user_id IS NULL",
        "UPDATE connector_accounts SET owner_user_id = ("
        "  SELECT u.id FROM users u WHERE u.tenant_id = connector_accounts.tenant_id "
        "  AND u.role = 'owner' ORDER BY u.created_at LIMIT 1) "
        "WHERE owner_user_id IS NULL",
        # Tenant type + customer-node assignment (scaling architecture). Existing
        # platform tenants become 'internal'; everyone else stays 'dedicated'.
        "ALTER TABLE tenants ADD COLUMN tenant_type VARCHAR DEFAULT 'dedicated'",
        "ALTER TABLE tenants ADD COLUMN node_id VARCHAR",
        "UPDATE tenants SET tenant_type = 'internal' WHERE plan = 'platform' "
        "AND (tenant_type IS NULL OR tenant_type = 'dedicated')",
        # Byte counts can exceed 32-bit INTEGER (>2.1GB) — widen to BIGINT so
        # storing appliance capacity / large content doesn't overflow on Postgres.
        "ALTER TABLE appliance_storages ALTER COLUMN capacity_bytes TYPE BIGINT",
        "ALTER TABLE appliance_storages ALTER COLUMN used_bytes TYPE BIGINT",
        "ALTER TABLE snapshot_receipts ALTER COLUMN total_bytes TYPE BIGINT",
        "ALTER TABLE search_documents ALTER COLUMN size_bytes TYPE BIGINT",
        # One-time: clear any oversized agent folder-index blobs (older agents
        # built multi-million-node trees) so reading them can't OOM the process.
        # Done in SQL so the giant JSON is never loaded into Python.
        "UPDATE desktop_agents SET last_scan = NULL, fs_expansions = NULL "
        "WHERE last_scan IS NOT NULL AND length(CAST(last_scan AS TEXT)) > 4000000",
        # NOTE: appliance_commands cleanup (its huge inline-ciphertext envelopes are
        # the #1 bloat) is deliberately NOT done here — a full-table UPDATE/DELETE on
        # a multi-GB table would stall startup and fail the deploy health check. It
        # runs in the BACKGROUND shortly after boot instead (workers/pruning.prune_all
        # + the concurrent index in workers/scheduler._ensure_perf_indexes).
    ]
    for statement in statements:
        try:
            with engine.begin() as conn:
                conn.execute(text(statement))
        except Exception:
            pass  # column already exists or dialect variance — safe to skip
