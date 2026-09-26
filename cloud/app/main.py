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
    compliance,
    costs,
    dashboard,
    debug,
    debug_panel,
    index_status,
    insights,
    integrations,
    notifications,
    org,
    node_sync,
    photos,
    provisioning,
    recovery,
    recovery_key,
    restore,
    rules,
    search,
    signup,
    site,
    snapshots,
    storage_instances,
    support,
    tenant,
    terminal,
    topology,
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

# Request-chain breadcrumbs for the live debug overlay: stamp every response with
# where it was served (this box) + server processing time. node_proxy already
# stamps the CP→node hop; we only fill defaults so a proxied response keeps its
# richer route. Registered LAST so it wraps the others (outermost).
import time as _time  # noqa: E402
_ROUTE_IS_CP = (settings.node_role or "control-plane") == "control-plane"
_ROUTE_LABEL = settings.node_name or settings.domain or ("control-plane" if _ROUTE_IS_CP else "node")


@app.middleware("http")
async def _route_stamp(request, call_next):
    _t0 = _time.perf_counter()
    response = await call_next(request)
    try:
        response.headers["X-Arkive-Server-Ms"] = str(round((_time.perf_counter() - _t0) * 1000, 1))
        response.headers.setdefault("X-Arkive-Route", "cp" if _ROUTE_IS_CP else "node")
        response.headers.setdefault("X-Arkive-Served-By", _ROUTE_LABEL)
    except Exception:  # noqa: BLE001 — never let stamping break a response
        pass
    return response

# Any UNHANDLED exception in an API route is logged to the unified log store
# (Platform Logs, via the cv.* logger) with a short reference + traceback, and the
# reference is returned to the client — so a 500 is never a silent "something went
# wrong". Intentional HTTPExceptions (4xx) keep their own detail and aren't caught.
import logging as _logging  # noqa: E402
import traceback as _traceback  # noqa: E402
import uuid as _uuid  # noqa: E402
from fastapi import Request as _Request  # noqa: E402
from fastapi.responses import JSONResponse as _JSONResponse  # noqa: E402

_api_logger = _logging.getLogger("cv.api")
# Stdout/journald copy on a NON-captured logger name (not under cv.*/arkive.*/app.)
# so the unified store gets exactly ONE, fully-attributed row (via logsink.emit)
# rather than a duplicate that carries no tenant/node context.
_api_stderr = _logging.getLogger("api.errors")

@app.exception_handler(Exception)
async def _log_unhandled(request: _Request, exc: Exception):  # noqa: ANN001
    ref = _uuid.uuid4().hex[:8]
    # Attribute the failure like every other log: derive the customer/tenant +
    # actor from the caller's session token, and the source from the path, so the
    # Platform Logs entry is scoped and drill-downable (never a context-less 500).
    tenant_id = None
    actor = ""
    path = request.url.path
    try:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            from . import security
            principal = security._decode(auth.split(" ", 1)[1])
            tenant_id = getattr(principal, "tenant_id", None)
            actor = getattr(principal, "user_id", "") or ""
    except Exception:  # noqa: BLE001 — fleet/agent/expired tokens aren't user sessions
        pass
    source = ("node" if path.startswith("/api/nodes/")
              else "appliance" if path.startswith("/api/appliance/")
              else "agent" if path.startswith("/api/agent/")
              else "auth" if path.startswith("/api/auth")
              else "cloud")
    msg = (f"unhandled API error [{ref}] {request.method} {path}: "
           f"{type(exc).__name__}: {str(exc)[:400]}")
    try:
        from . import logsink
        logsink.emit(level="error", source=source, logger_name="cv.api",
                     message=f"{msg}\n{_traceback.format_exc()}",
                     tenant_id=tenant_id, actor=actor, resource=path,
                     meta={"ref": ref, "method": request.method, "path": path,
                           "exception": type(exc).__name__})
    except Exception:  # noqa: BLE001 — logging must never mask the original error
        pass
    try:
        _api_stderr.error(msg)  # operational copy to journald (not re-captured)
    except Exception:  # noqa: BLE001
        pass
    return _JSONResponse(
        status_code=500,
        content={"detail": f"Something went wrong (ref {ref}). The error was logged.",
                 "ref": ref})

API = "/api"
app.include_router(auth.router, prefix=API)
app.include_router(signup.router, prefix=API)
app.include_router(tenant.router, prefix=API)
app.include_router(org.router, prefix=API)
app.include_router(dashboard.router, prefix=API)
app.include_router(insights.router, prefix=API)
app.include_router(notifications.router, prefix=API)
app.include_router(integrations.router, prefix=API)
app.include_router(integrations.advanced_router, prefix=API)
app.include_router(integrations.agent_router, prefix=API)
app.include_router(integrations.admin_router, prefix=API)
# Microsoft 365 managed integration — self-contained package router. Registered
# defensively: a packaged integration must never be able to crash the core
# control plane (PKG-001). A failure here is logged and skipped, not fatal.
try:
    from .integrations.microsoft365 import api as m365_api  # noqa: E402
    app.include_router(m365_api.router, prefix=API)
except Exception:  # noqa: BLE001
    import logging as _logging
    _logging.getLogger("cv.integrations").exception("microsoft365 router failed to load")
try:
    from .integrations.google_workspace import api as gw_api  # noqa: E402
    app.include_router(gw_api.router, prefix=API)
except Exception:  # noqa: BLE001
    import logging as _logging
    _logging.getLogger("cv.integrations").exception("google_workspace router failed to load")
app.include_router(billing.router, prefix=API)
app.include_router(billing.admin_router, prefix=API)
app.include_router(connectors.router, prefix=API)
app.include_router(collections.router, prefix=API)
app.include_router(rules.router, prefix=API)
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
app.include_router(topology.router, prefix=API)
app.include_router(provisioning.router, prefix=API)
app.include_router(costs.router, prefix=API)
app.include_router(terminal.admin_terminal_router, prefix=API)
app.include_router(terminal.agent_terminal_router, prefix=API)
app.include_router(debug.router, prefix=API)
app.include_router(debug_panel.router, prefix=API)
app.include_router(activity.router, prefix=API)
app.include_router(compliance.router, prefix=API)
app.include_router(recovery.router, prefix=API)
app.include_router(recovery_key.router, prefix=API)
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
    # Apply any fleet secrets this node previously adopted from the control plane
    # BEFORE any crypto/DB use, so credentials + vault keys decrypt with the shared
    # KEK instead of a per-node random one (InvalidTag). No-op on the control plane.
    try:
        from . import fleet_secrets
        fleet_secrets.load_persisted()
    except Exception:  # noqa: BLE001
        pass
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

    # Unified log sink — capture cv.*/arkive.* app logs into the platform log store
    # so the control plane is the one place to view logs (nodes push theirs up).
    try:
        from . import logsink
        logsink.install()
    except Exception:  # noqa: BLE001
        pass

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

    # Log forwarding is DECOUPLED from federation: every non-control-plane node
    # must push its unified logs to the CP so all logs are viewable in one place
    # (golden rule) — even a node with no assigned tenants or with node_sync_scope
    # off. A federated node already forwards logs inside its replication push, so
    # start the dedicated forwarder only when replication ISN'T running.
    if role != "control-plane" and not (settings.node_sync_scope):
        try:
            from .workers.node_replication import start_log_forwarder
            start_log_forwarder()
        except Exception:  # noqa: BLE001
            _logging.getLogger("cv.startup").exception("log forwarder start failed")

    # Every node drains its own durable activity queue (retries to offline
    # appliances / unreachable storage) so pending backups deliver on reconnect.
    from .workers.queue import start_queue_worker
    start_queue_worker()

    # Managed-integration reconcile (e.g. Microsoft 365 Entra discovery) runs on
    # whichever box owns the instances — CP for CP-hosted tenants, each node for
    # its federated ones. Guarded so a package issue can't break startup.
    try:
        from .integrations.microsoft365.worker import start_m365_worker
        start_m365_worker()
    except Exception:  # noqa: BLE001
        import logging
        logging.getLogger("cv.integrations.m365").exception("m365 worker start failed")

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
