"""Control-plane → assigned-node proxy for file-level operations.

In the federated model NO file operation runs on the control plane: retrieval of
cloud/appliance backups, recovery windows, recovered-content downloads and
endpoint folder scans all execute on the tenant's assigned node, which holds the
local data, keys and storage credentials. The customer still drives everything
from the control-plane portal; this middleware transparently forwards those
requests to the node and streams the response back.

Requires a fleet-shared ``CV_SESSION_SECRET`` so the node validates the same
session token the portal issued (the node also has the user/tenant/vault rows via
replication).
"""

from __future__ import annotations

import logging
import time
from urllib.parse import urlparse

import httpx
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse

from .. import security, services
from ..config import get_settings
from ..db import SessionLocal

logger = logging.getLogger("cv.nodeproxy")
settings = get_settings()

_client: httpx.AsyncClient | None = None


def _cl() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=None)
    return _client


def _should_proxy(method: str, path: str) -> bool:
    """File-level + tenant-data operations that must run on the node, not here.

    Search (index + retrieval) is served from the node's live local index so the
    portal never shows a stale control-plane copy; recovery, recovered-content
    downloads, folder scans and source purges all execute on the node too."""
    if path == "/api/search" or path == "/api/search/taxonomy":
        return True
    if path == "/api/search/retrieve" and method == "POST":
        return True
    if path.startswith("/api/search/retrieve-status/"):
        return True
    # Cross-member recovery approvals are created + reviewed on the node that runs
    # the retrieval, so the whole dual-control flow stays consistent there.
    if path == "/api/search/access-approvals" or path.startswith("/api/search/access-approvals/"):
        return True
    # Integrations (setup/OTP handshake + network telemetry) run against the
    # node the appliance reports to, so the portal must operate on that same DB.
    # EXCEPT the Microsoft 365 managed integration, which is control-plane
    # authoritative: its connect/consent/scope/identity config is authored on the
    # CP and replicated down to the node (which only does app-only collection).
    # Proxying it would build the OAuth redirect from the node's domain and store
    # consent state on the node's DB, breaking the CP-hosted redirect handler.
    if path.startswith("/api/integrations/microsoft365"):
        return False
    if path == "/api/integrations" or path.startswith("/api/integrations/"):
        return True
    if path == "/api/recovered" or path.startswith("/api/recovered/"):
        return True
    if path == "/api/restore" or path.startswith("/api/restore/"):
        return True
    # Vault Recovery Key management touches the vault key store, which lives on
    # the tenant's node — create/rotate/status must run there.
    if path == "/api/recovery-key" or path.startswith("/api/recovery-key/"):
        return True
    if ("/fs-scan" in path or "/fs-expand" in path) and path.startswith("/api/agents/"):
        return True
    return False


def _node_for(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    try:
        principal = security._decode(auth.split(" ", 1)[1])
    except Exception:
        return None
    # Never let a DB hiccup (e.g. transient pool pressure) 500 the request — fall
    # back to handling it locally instead of taking the endpoint down.
    try:
        with SessionLocal() as db:
            return services.tenant_node_url(db, principal.tenant_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("node lookup failed (%s) — handling %s locally",
                       exc, request.url.path)
        return None


def _target_for(request: Request) -> tuple[str | None, bool]:
    """(assigned-node URL, is-switching) for the request's tenant. is-switching is
    True during the brief HA switchover window so file ops show a maintenance
    message instead of routing to a node mid-migration."""
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return None, False
    try:
        principal = security._decode(auth.split(" ", 1)[1])
    except Exception:
        return None, False
    try:
        from ..models import Tenant
        from .. import placement
        with SessionLocal() as db:
            t = db.get(Tenant, principal.tenant_id)
            if t is None:
                return None, False
            return services.tenant_node_url(db, t.id), placement.is_switching(t)
    except Exception as exc:  # noqa: BLE001
        logger.warning("node lookup failed (%s) — handling %s locally",
                       exc, request.url.path)
        return None, False


async def middleware(request: Request, call_next):
    # Only the control plane proxies; a node executes these locally.
    if not (settings.node_sync_scope
            and (settings.node_role or "control-plane") == "control-plane"):
        return await call_next(request)
    if not _should_proxy(request.method, request.url.path):
        return await call_next(request)
    # Run the (synchronous) DB lookup off the event loop so it never blocks other
    # requests while waiting on the connection pool.
    node_url, switching = await run_in_threadpool(_target_for, request)
    if switching:
        # Brief HA switchover — the tenant is moving to a healthier node. Return a
        # friendly maintenance signal the portal shows as a dialog (retry shortly).
        return JSONResponse({
            "detail": "Your data is briefly unavailable while we move it to a healthier "
                      "server. This usually takes under a minute — please try again shortly.",
            "maintenance": True, "retry_after": 15}, status_code=503,
            headers={"Retry-After": "15"})
    if not node_url:
        return await call_next(request)  # unassigned tenant → handled locally

    suffix = request.url.path[4:]  # strip leading /api (node_url already ends in /api)
    url = node_url + suffix
    if request.url.query:
        url += "?" + request.url.query
    body = await request.body()
    fwd = {k: v for k, v in request.headers.items()
           if k.lower() in ("authorization", "content-type", "accept")}
    _t0 = time.perf_counter()
    try:
        req = _cl().build_request(request.method, url, content=body, headers=fwd)
        resp = await _cl().send(req, stream=True)
    except Exception as exc:  # noqa: BLE001 - node offline / unreachable
        logger.warning("file op proxy to %s failed: %s", url, exc)
        return JSONResponse({"detail": "assigned node unavailable"}, status_code=503,
                            headers={"X-Arkive-Route": "cp->node",
                                     "X-Arkive-Node": urlparse(node_url).netloc or node_url})
    _upstream_ms = round((time.perf_counter() - _t0) * 1000, 1)

    relay = {}
    for h in ("content-type", "content-disposition", "cache-control"):
        if h in resp.headers:
            relay[h] = resp.headers[h]
    # Request-chain breadcrumbs for the debug overlay: this response was served
    # CP → the tenant's node, with the node's own round-trip time.
    relay["X-Arkive-Route"] = "cp->node"
    relay["X-Arkive-Node"] = urlparse(node_url).netloc or node_url
    relay["X-Arkive-Upstream-Ms"] = str(_upstream_ms)

    # The CP already authenticated this session to route it here, so a 401 from the
    # node means the node can't yet authenticate it — almost always because the
    # user/tenant hasn't finished replicating after a switchover, NOT a real session
    # expiry. Relaying the 401 would make the portal sign the customer out. Return a
    # 503 "node not ready" instead so the session survives and the client retries.
    if resp.status_code == 401:
        await resp.aclose()
        logger.warning("assigned node %s returned 401 for %s — node not ready "
                       "(user/tenant still replicating?); returning 503 not logout",
                       node_url, request.url.path)
        return JSONResponse(
            {"detail": "Your data is briefly unavailable while your account finishes "
                       "syncing to its server — please try again in a moment.",
             "node_not_ready": True, "retry_after": 10},
            status_code=503, headers={"Retry-After": "10"})

    async def _stream():
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await resp.aclose()

    return StreamingResponse(_stream(), status_code=resp.status_code, headers=relay)
