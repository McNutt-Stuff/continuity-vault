"""Universal user-activity logger.

An ASGI middleware that records EVERY authenticated, state-changing request a user
makes to the audit ledger, so the admin always has a complete activity trail — even
for actions that don't have a hand-written ``audit.record`` call. Endpoints that DO
write a rich audit entry set a per-request flag (see ``audit._request_audited``), and
this middleware skips them to avoid duplicate rows. Read-only (GET/HEAD) requests and
non-user callers (fleet/agent bearer tokens) are ignored.
"""

from __future__ import annotations

import logging
import re

from starlette.requests import Request

from . import audit, security
from .db import SessionLocal

logger = logging.getLogger("cv.activity")

_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
_VERB = {"POST": "created", "PUT": "updated", "PATCH": "updated", "DELETE": "deleted"}
_ID_RE = re.compile(r"^[0-9a-f]{16,}$|^[0-9a-f-]{20,}$", re.I)

# Paths that are not user activity (health, fleet sync, the activity write itself).
_SKIP_PREFIXES = ("/api/nodes/", "/api/appliance/", "/api/agent/", "/api/c/o/")


def _norm_path(path: str) -> str:
    """Collapse id-like segments so distinct resources group by shape, not id."""
    parts = []
    for seg in path.split("/"):
        parts.append(":id" if (seg and _ID_RE.match(seg)) else seg)
    return "/".join(parts)


def _category(path: str) -> str:
    if path.startswith("/api/auth") or "passkey" in path:
        return "security"
    if path.startswith("/api/admin"):
        return "admin"
    return "activity"


async def middleware(request: Request, call_next):
    # Give this request a shared flag so a hand-written audit.record can mark it.
    token = audit._request_audited.set({"audited": False})
    try:
        response = await call_next(request)
    finally:
        flag = audit._request_audited.get() or {"audited": False}
        audit._request_audited.reset(token)
    try:
        if (request.method in _MUTATING and not flag.get("audited")
                and 200 <= response.status_code < 400):
            path = request.url.path
            if not any(path.startswith(p) for p in _SKIP_PREFIXES):
                _record(request, response.status_code)
    except Exception:  # noqa: BLE001 — logging must never break the response
        pass
    return response


def _record(request: Request, status: int) -> None:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return  # unauthenticated (e.g. sign-in) — those self-audit as actor=email
    try:
        principal = security._decode(auth.split(" ", 1)[1])
    except Exception:  # noqa: BLE001 — fleet/agent tokens aren't user sessions
        return
    path = request.url.path
    verb = _VERB.get(request.method, "changed")
    # Derive a readable noun from the last meaningful path segment so the feed
    # reads e.g. "notifications.updated" / "plan.updated".
    segs = [s for s in path.split("/") if s and s != "api" and not _ID_RE.match(s)]
    noun = segs[-1] if segs else "request"
    action = f"{noun}.{verb}"
    try:
        with SessionLocal() as db:
            audit.record(db, actor=principal.user_id, action=action,
                         tenant_id=principal.tenant_id, resource=_norm_path(path),
                         category=_category(path),
                         detail={"method": request.method, "path": path, "status": status})
    except Exception:  # noqa: BLE001
        logger.debug("activity log failed for %s %s", request.method, path)
