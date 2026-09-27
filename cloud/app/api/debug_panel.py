"""Live debug overlay — behind-the-scenes diagnostics for the current account.

Powers the per-account debug footer bar in the portal. Everything here is scoped
to the CALLER's own tenant (never cross-tenant), so a user may self-enable their
own overlay. It surfaces what's needed to troubleshoot live: which node currently
serves the tenant, the serving-index completeness, replication freshness
(heartbeat / sync / log-push), a quick DB ping, and the most recent warning+ log
lines for the tenant. The rolling request/response/timing history is captured
client-side (api.ts) — this endpoint provides the server-side picture.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from .. import features, placement, security
from ..db import get_db
from ..models import LogEntry, Node, Tenant, User

router = APIRouter(prefix="/debug-panel", tags=["debug-panel"])


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _online(node: Node | None) -> bool:
    return bool(node and node.last_heartbeat_at
               and (_now() - node.last_heartbeat_at).total_seconds() < 180)


class ToggleReq(BaseModel):
    enabled: bool


@router.post("/toggle")
def toggle(body: ToggleReq,
           principal: security.Principal = Depends(security.get_principal),
           db: Session = Depends(get_db)):
    """Self-serve enable/disable of the caller's OWN live debug overlay. Only ever
    flips a per-user flag (diagnostics stay scoped to the caller's tenant)."""
    user = db.get(User, principal.user_id)
    if not user:
        raise HTTPException(404, "user not found")
    ff = dict(user.feature_flags or {})
    ff["debug_overlay_enabled"] = bool(body.enabled)
    user.feature_flags = ff
    db.commit()
    return {"enabled": bool(body.enabled)}


@router.get("/live")
def live(principal: security.Principal = Depends(security.get_principal),
         tenant: Tenant = Depends(security.get_tenant),
         db: Session = Depends(get_db)):
    """Live server-side diagnostics for the caller's tenant. Requires the overlay
    flag to be on for this account (or a platform admin)."""
    user = db.get(User, principal.user_id)
    if not (principal.is_platform_admin
            or features.resolve(user, tenant, "debug_overlay_enabled", db)):
        raise HTTPException(403, "The debug overlay is not enabled for this account.")

    # Quick DB ping (round-trip to the store this request is served from).
    t0 = time.perf_counter()
    try:
        db.execute(text("SELECT 1"))
        db_ping_ms = round((time.perf_counter() - t0) * 1000, 1)
    except Exception:  # noqa: BLE001
        db_ping_ms = None

    node = db.get(Node, tenant.node_id) if tenant.node_id else None
    sb = db.get(Node, tenant.standby_node_id) if tenant.standby_node_id else None

    active = {
        "hosted": "node" if node else "control-plane",
        "node_id": node.id if node else None,
        "node_name": node.name if node else "Control plane",
        "role": node.role if node else "control-plane",
        "endpoint": (node.endpoint if node else None),
        "online": (_online(node) if node else True),
        "last_heartbeat_at": (node.last_heartbeat_at.isoformat()
                              if node and node.last_heartbeat_at else None),
        "last_sync_at": (node.last_sync_at.isoformat()
                         if node and node.last_sync_at else None),
        "last_log_push_at": (node.last_log_push_at.isoformat()
                             if node and node.last_log_push_at else None),
    }

    serving_index = {
        "active_index_count": int(tenant.active_index_count or 0),
        "cp_index_count": int(tenant.cp_index_count or 0),
        "active_receipt_count": int(tenant.active_receipt_count or 0),
        "cp_receipt_count": int(tenant.cp_receipt_count or 0),
        "pct": placement.active_index_pct(tenant),
        "complete": placement.is_active_index_complete(tenant),
        "checked_at": (tenant.active_counts_at.isoformat()
                       if tenant.active_counts_at else None),
    }

    placement_info = {
        "state": tenant.placement_state or "",
        "standby_node_name": (sb.name if sb else None),
        "standby_online": _online(sb),
        "standby_ready": placement.is_standby_ready(tenant),
        "standby_pending": int(tenant.standby_pending or 0),
        "standby_synced_at": (tenant.standby_synced_at.isoformat()
                              if tenant.standby_synced_at else None),
    }

    # Most recent warning+ log lines for THIS tenant (bounded, newest first).
    rows = (db.query(LogEntry)
            .filter(LogEntry.tenant_id == tenant.id,
                    LogEntry.level.in_(["warning", "error", "critical"]))
            .order_by(LogEntry.ts.desc()).limit(25).all())
    recent_errors = [{
        "ts": r.ts.isoformat() if r.ts else None,
        "level": r.level, "source": r.source, "logger": r.logger or "",
        "message": (r.message or "")[:400], "resource": r.resource or "",
    } for r in rows]

    return {
        "server_time": _now().isoformat(),
        "db_ping_ms": db_ping_ms,
        "is_admin": bool(principal.is_platform_admin),
        "tenant": {"id": tenant.id, "name": tenant.name,
                   "type": tenant.tenant_type or "dedicated"},
        "user": {"id": principal.user_id, "email": user.email if user else ""},
        "active": active,
        "serving_index": serving_index,
        "placement": placement_info,
        "recent_errors": recent_errors,
    }


@router.get("/fleet-path")
def fleet_path(principal: security.Principal = Depends(security.get_principal),
               db: Session = Depends(get_db)):
    """Platform-admin only: probe the CP→node path to EVERY customer-tenant node
    with a real fleet-authed round trip, so an admin (whose own tenant is CP-hosted
    and never proxies) can still SEE the end-to-end inter-node path + timing. Each
    entry returns the assembled chain [control-plane, node] using the node's own
    X-Arkive-Hop breadcrumb."""
    import httpx
    from ..config import get_settings
    from ..models import Node
    from . import site as _site

    if not principal.is_platform_admin:
        raise HTTPException(403, "Platform admin only.")
    s = get_settings()
    cp_label = s.node_name or s.domain or "control-plane"
    if (s.node_role or "control-plane") != "control-plane":
        return {"cp": cp_label, "nodes": [], "note": "not the control plane"}
    try:
        secret = _site._fleet_secret()
    except Exception:  # noqa: BLE001
        secret = None
    out = []
    for n in db.query(Node).filter(Node.role == "customer-tenant").all():
        if not n.endpoint:
            continue
        base = n.endpoint.rstrip("/")
        cp_hop = {"role": "control-plane", "name": cp_label, "ms": None}
        row = {"node_id": n.id, "name": n.name, "endpoint": n.endpoint,
               "reachable": None, "ms": None, "chain": [cp_hop], "error": None}
        t0 = time.perf_counter()
        try:
            r = httpx.get(f"{base}/nodes/sync/debug",
                          headers={"Authorization": f"Bearer {secret}"} if secret else {},
                          timeout=12.0)
            ms = round((time.perf_counter() - t0) * 1000, 1)
            row["ms"] = ms
            row["reachable"] = r.status_code < 500
            node_hop = None
            try:
                node_hop = json.loads(r.headers.get("X-Arkive-Hop") or "null")
            except Exception:  # noqa: BLE001
                node_hop = None
            if not isinstance(node_hop, dict):
                node_hop = {"role": "node", "name": n.name, "ms": None}
            node_hop["upstream_ms"] = ms
            row["chain"] = [cp_hop, node_hop]
        except Exception as exc:  # noqa: BLE001
            row["reachable"] = False
            row["error"] = str(exc)[:200]
            row["chain"] = [cp_hop, {"role": "node", "name": n.name,
                                     "error": "unreachable"}]
        out.append(row)
    return {"cp": cp_label, "nodes": out}
