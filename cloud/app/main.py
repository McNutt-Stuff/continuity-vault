"""Arkive cloud control-plane API (spec 14)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import get_settings
from .db import init_db
from .api import (
    activity,
    admin,
    agents,
    appliances,
    auth,
    billing,
    collections,
    comms,
    connectors,
    dashboard,
    debug,
    index_status,
    insights,
    integrations,
    notifications,
    org,
    node_sync,
    photos,
    recovery,
    restore,
    search,
    site,
    snapshots,
    storage_instances,
    support,
    tenant,
    terminal,
    updates,
)

settings = get_settings()

app = FastAPI(
    title="Arkive Control Plane",
    version="0.1.0",
    description="Cloud-managed digital continuity and cyber-recovery platform.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=(["*"] if settings.environment == "development"
                   else [settings.rp_origin, "https://arkive.life", "https://www.arkive.life"]),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Federated file-op proxy: on the control plane, forward retrieval / recovery /
# folder-scan requests to the tenant's assigned node so no file operation runs
# here. No-op when federation is off or on a node.
from .api import node_proxy  # noqa: E402
app.middleware("http")(node_proxy.middleware)

# Universal activity logger: record every authenticated state-changing request to
# the audit ledger so the admin has a complete per-user activity trail.
from . import activity_logger  # noqa: E402
app.middleware("http")(activity_logger.middleware)

API = "/api"
app.include_router(auth.router, prefix=API)
app.include_router(tenant.router, prefix=API)
app.include_router(org.router, prefix=API)
app.include_router(dashboard.router, prefix=API)
app.include_router(insights.router, prefix=API)
app.include_router(notifications.router, prefix=API)
app.include_router(integrations.router, prefix=API)
app.include_router(integrations.agent_router, prefix=API)
app.include_router(integrations.admin_router, prefix=API)
app.include_router(billing.router, prefix=API)
app.include_router(billing.admin_router, prefix=API)
app.include_router(connectors.router, prefix=API)
app.include_router(collections.router, prefix=API)
app.include_router(search.router, prefix=API)
app.include_router(snapshots.router, prefix=API)
app.include_router(storage_instances.router, prefix=API)
app.include_router(index_status.router, prefix=API)
app.include_router(restore.router, prefix=API)
app.include_router(appliances.fleet_router, prefix=API)
app.include_router(appliances.agent_router, prefix=API)
app.include_router(agents.fleet_router, prefix=API)
app.include_router(agents.agent_router, prefix=API)
app.include_router(admin.router, prefix=API)
app.include_router(terminal.admin_terminal_router, prefix=API)
app.include_router(terminal.agent_terminal_router, prefix=API)
app.include_router(debug.router, prefix=API)
app.include_router(activity.router, prefix=API)
app.include_router(recovery.router, prefix=API)
app.include_router(photos.router, prefix=API)
app.include_router(photos.actions_router, prefix=API)
app.include_router(site.public_router, prefix=API)
app.include_router(site.admin_router, prefix=API)
app.include_router(support.public_router, prefix=API)
app.include_router(support.tickets_router, prefix=API)
app.include_router(support.admin_router, prefix=API)
app.include_router(node_sync.router, prefix=API)
app.include_router(comms.public_router, prefix=API)
app.include_router(updates.router, prefix=API)
app.include_router(updates.public_router, prefix=API)


@app.on_event("startup")
def startup() -> None:
    # The database (Postgres) may still be accepting connections a moment after
    # the service starts; retry briefly so the worker doesn't crash-loop.
    import time
    from sqlalchemy.exc import OperationalError

    last_err: Exception | None = None
    for attempt in range(30):
        try:
            init_db()
            last_err = None
            break
        except OperationalError as exc:
            last_err = exc
            time.sleep(2)
    if last_err is not None:
        raise last_err

    if settings.seed_demo_data:
        from .seed import seed

        seed()

    # Apply this node's configured timezone to the process so API log timestamps
    # and server-side time formatting reflect CV_TIMEZONE (the scheduler keeps it
    # in sync as the config changes).
    try:
        from . import node_config
        from .db import SessionLocal
        with SessionLocal() as db:
            node_config.apply_process_timezone(db)
    except Exception:  # noqa: BLE001
        pass

    # Inject the configured Google Analytics tag into the served index.html(s).
    try:
        from . import analytics
        from .db import SessionLocal
        with SessionLocal() as db:
            analytics.apply(db)
    except Exception:  # noqa: BLE001
        pass

    # Clear jobs left "running" by a previous process — their worker threads died
    # with the old process, so they can never complete on their own.
    try:
        from .workers.jobs import reap_stale_jobs
        reap_stale_jobs(on_startup=True)
    except Exception:  # noqa: BLE001
        pass

    # Background sync. Federated mode (per-node data planes): a customer node
    # replicates its assigned tenants' config from the control plane into its
    # LOCAL database, runs its own scheduler over that data, and pushes results
    # back. The control plane schedules only the tenants NOT assigned to a node.
    from .workers.scheduler import start_scheduler
    role = settings.node_role or "control-plane"
    if settings.node_sync_scope and role != "control-plane":
        from .workers.node_replication import start_replication
        start_replication()
        start_scheduler()
    else:
        start_scheduler()

    # Every node drains its own durable activity queue (retries to offline
    # appliances / unreachable storage) so pending backups deliver on reconnect.
    from .workers.queue import start_queue_worker
    start_queue_worker()

    # The control plane samples the whole fleet's health into 90-day history.
    if role == "control-plane":
        from .workers.telemetry import start_telemetry_sampler
        start_telemetry_sampler()
        from .workers.billing import start_billing_worker
        start_billing_worker()

    # Verbose sync diagnostics when enabled (per-source fetch/ingest/errors).
    if settings.sync_debug:
        import logging
        for name in ("cv.sync", "cv.scheduler", "cv.connectors", "cv.connectors.evernote_mcp"):
            logging.getLogger(name).setLevel(logging.DEBUG)


@app.get("/api/health")
def health():
    from cv_crypto.provider import get_provider

    return {
        "status": "ok",
        "domain": settings.domain,
        "environment": settings.environment,
        "pq_available": get_provider().pq_available,
    }
