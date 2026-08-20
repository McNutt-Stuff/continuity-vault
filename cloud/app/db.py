"""SQLAlchemy engine/session setup for the cloud control plane."""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker, Session

from .config import get_settings

settings = get_settings()

connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

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
        "ALTER TABLE pricing_config ADD COLUMN license_plans JSON",
        "ALTER TABLE email_config ADD COLUMN aws_access_key_id VARCHAR",
        "ALTER TABLE email_config ADD COLUMN aws_secret_encrypted VARCHAR",
        "ALTER TABLE nodes ADD COLUMN storage_service_id VARCHAR",
        "ALTER TABLE nodes ADD COLUMN email_service_id VARCHAR",
        "ALTER TABLE nodes ADD COLUMN cloud JSON",
        "ALTER TABLE search_documents ADD COLUMN content_hash VARCHAR",
        "ALTER TABLE search_documents ADD COLUMN version INTEGER DEFAULT 1",
        "ALTER TABLE audit_events ADD COLUMN severity VARCHAR DEFAULT 'info'",
        "ALTER TABLE audit_events ADD COLUMN category VARCHAR DEFAULT 'activity'",
        "ALTER TABLE connector_accounts ADD COLUMN sync_cursor JSON",
        "ALTER TABLE appliance_storages ADD COLUMN used_bytes INTEGER DEFAULT 0",
        "ALTER TABLE appliance_storages ADD COLUMN health JSON",
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
    ]
    for statement in statements:
        try:
            with engine.begin() as conn:
                conn.execute(text(statement))
        except Exception:
            pass  # column already exists or dialect variance — safe to skip
