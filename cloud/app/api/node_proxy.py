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
    # Integrations (setup/OTP handshake + network telemetry) run against the
    # node the appliance reports to, so the portal must operate on that same DB.
    if path == "/api/integrations" or path.startswith("/api/integrations/"):
        return True
    if path == "/api/recovered" or path.startswith("/api/recovered/"):
        return True
    if path == "/api/restore" or path.startswith("/api/restore/"):
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


async def middleware(request: Request, call_next):
    # Only the control plane proxies; a node executes these locally.
    if not (settings.node_sync_scope
            and (settings.node_role or "control-plane") == "control-plane"):
        return await call_next(request)
    if not _should_proxy(request.method, request.url.path):
        return await call_next(request)
    # Run the (synchronous) DB lookup off the event loop so it never blocks other
    # requests while waiting on the connection pool.
    node_url = await run_in_threadpool(_node_for, request)
    if not node_url:
        return await call_next(request)  # unassigned tenant → handled locally

    suffix = request.url.path[4:]  # strip leading /api (node_url already ends in /api)
    url = node_url + suffix
    if request.url.query:
        url += "?" + request.url.query
    body = await request.body()
    fwd = {k: v for k, v in request.headers.items()
           if k.lower() in ("authorization", "content-type", "accept")}
    try:
        req = _cl().build_request(request.method, url, content=body, headers=fwd)
        resp = await _cl().send(req, stream=True)
    except Exception as exc:  # noqa: BLE001 - node offline / unreachable
        logger.warning("file op proxy to %s failed: %s", url, exc)
        return JSONResponse({"detail": "assigned node unavailable"}, status_code=503)

    relay = {}
    for h in ("content-type", "content-disposition", "cache-control"):
        if h in resp.headers:
            relay[h] = resp.headers[h]

    async def _stream():
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await resp.aclose()

    return StreamingResponse(_stream(), status_code=resp.status_code, headers=relay)
