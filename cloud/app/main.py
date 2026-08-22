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
    connectors,
    dashboard,
    org,
    node_sync,
    photos,
    recovery,
    restore,
    search,
    site,
    snapshots,
    tenant,
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

API = "/api"
app.include_router(auth.router, prefix=API)
app.include_router(tenant.router, prefix=API)
app.include_router(org.router, prefix=API)
app.include_router(dashboard.router, prefix=API)
app.include_router(billing.router, prefix=API)
app.include_router(connectors.router, prefix=API)
app.include_router(collections.router, prefix=API)
app.include_router(search.router, prefix=API)
app.include_router(snapshots.router, prefix=API)
app.include_router(restore.router, prefix=API)
app.include_router(appliances.fleet_router, prefix=API)
app.include_router(appliances.agent_router, prefix=API)
app.include_router(agents.fleet_router, prefix=API)
app.include_router(agents.agent_router, prefix=API)
app.include_router(admin.router, prefix=API)
app.include_router(activity.router, prefix=API)
app.include_router(recovery.router, prefix=API)
app.include_router(photos.router, prefix=API)
app.include_router(photos.actions_router, prefix=API)
app.include_router(site.public_router, prefix=API)
app.include_router(site.admin_router, prefix=API)
app.include_router(node_sync.router, prefix=API)
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
