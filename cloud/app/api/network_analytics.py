"""Network Analytics — unified view over all network/traffic signals.

Aggregates the tenant's observed apps/services + devices across EVERY source
(Ubiquiti/UniFi DPI, the endpoint agents' browser web-usage, and anything else
that writes NetworkApp/NetworkUsage/NetworkClient) into one de-duplicated picture:
what apps are in use, which devices use them, where the data came from, what's
unprotected ("apps you aren't protecting"), what's AI, and what an admin has
flagged as risky. Summaries + drill-downs, sorting + filtering.

Gated by the ``network_analytics_enabled`` flag (ON by default). Writes (risk
flags) persist into NetworkApp.meta, which already replicates node→CP, so no new
table/migration is needed. For node-hosted tenants this router is proxied CP→node
(api/node_proxy) so it operates on the node that owns the live network data.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import audit, features, security
from ..db import get_db
from ..integrations.source_map import candidate_service, map_app_to_source
from ..models import (ConnectorAccount, IntegrationInstance, NetworkApp,
                      NetworkClient, NetworkUsage, Tenant, User)

router = APIRouter(prefix="/network-analytics", tags=["network-analytics"])
logger = logging.getLogger("cv.netanalytics")

RISK_LEVELS = ("concerning", "risky", "blocked")
_ENDPOINT_IID = "endpoint-web"


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _guard(principal: security.Principal, tenant: Tenant, db: Session) -> User | None:
    user = db.get(User, principal.user_id)
    if not features.resolve(user, tenant, "network_analytics_enabled", db):
        raise HTTPException(403, "Network Analytics is not enabled for this account.")
    return user


# --------------------------------------------------------------------------- #
# Source identity + icons                                                      #
# --------------------------------------------------------------------------- #
def _source_map(db: Session, tid: str) -> dict[str, str]:
    """integration_id → a source label (ubiquiti, endpoint, microsoft365, …)."""
    out = {_ENDPOINT_IID: "endpoint"}
    for i in db.query(IntegrationInstance).filter(
            IntegrationInstance.tenant_id == tid).all():
        out[i.id] = i.integration_type or "network"
    return out


# Source label → the brand/icon type the frontend renders.
SOURCE_ICON = {"ubiquiti": "ubiquiti", "endpoint": "macos",
               "microsoft365": "microsoft365", "network": "activity"}


def _app_ref(name: str, meta: dict) -> str:
    """A logical-app identity that de-dupes the same app seen from several sources:
    AI tools collapse by catalog id; everything else by normalized name."""
    if isinstance(meta, dict) and meta.get("ai") and meta.get("ai_tool_id"):
        return f"ai:{meta['ai_tool_id']}"
    return "name:" + (name or "").strip().lower()


def _device_ref(c: NetworkClient | None, client_key: str) -> str:
    if c is not None:
        if c.mac:
            return "mac:" + c.mac.strip().lower()
        label = (c.hostname or c.name or c.client_key or "").strip().lower()
        if label:
            return "host:" + label
    return "key:" + (client_key or "").strip().lower()


# --------------------------------------------------------------------------- #
# Aggregation                                                                  #
# --------------------------------------------------------------------------- #
def _load(db: Session, tid: str):
    apps = db.query(NetworkApp).filter(NetworkApp.tenant_id == tid).all()
    usage = db.query(NetworkUsage).filter(NetworkUsage.tenant_id == tid).all()
    clients = db.query(NetworkClient).filter(NetworkClient.tenant_id == tid).all()
    return apps, usage, clients


def _enabled_source_types(db: Session, tid: str) -> set[str]:
    return {r[0] for r in db.query(ConnectorAccount.connector_type)
            .filter(ConnectorAccount.tenant_id == tid).all()}


def _aggregate(db: Session, tid: str) -> dict:
    """Build the de-duplicated app + device aggregates once (reused by every view)."""
    apps, usage, clients = _load(db, tid)
    smap = _source_map(db, tid)
    enabled = _enabled_source_types(db, tid)
    client_by_key = {c.client_key: c for c in clients}
    # app_key → NetworkApp (first wins; rows for the same app_key across a source).
    app_by_key: dict[str, NetworkApp] = {}
    for a in apps:
        app_by_key.setdefault(a.app_key, a)

    # ---- Logical apps -----------------------------------------------------
    la: dict[str, dict] = {}
    appkey_to_ref: dict[str, str] = {}
    for a in apps:
        meta = a.meta if isinstance(a.meta, dict) else {}
        ref = _app_ref(a.name, meta)
        appkey_to_ref[a.app_key] = ref
        src = smap.get(a.integration_id, a.integration_id or "network")
        g = la.setdefault(ref, {
            "ref": ref, "name": a.name or a.app_key, "category": a.category or "",
            "sources": set(), "source_types": set(), "total_bytes": 0,
            "devices": set(), "is_ai": False, "ai": None, "source_type": "",
            "risk": "", "risk_reason": "", "last_seen": None})
        g["sources"].add(src)
        if a.source_type:
            g["source_type"] = a.source_type
            g["source_types"].add(a.source_type)
        g["total_bytes"] += int(a.total_bytes or 0)
        if a.client_count and not usage:
            g["devices"].update(f"_cc:{a.app_key}:{i}" for i in range(a.client_count))
        if a.last_seen and (g["last_seen"] is None or a.last_seen > g["last_seen"]):
            g["last_seen"] = a.last_seen
        if meta.get("ai"):
            g["is_ai"] = True
            g["name"] = meta.get("name") or g["name"]
            g["category"] = "AI"
            g["ai"] = {"vendor": meta.get("vendor", ""),
                       "tool": meta.get("ai_tool_id", ""),
                       "ai_category": meta.get("ai_category", ""),
                       "data_risk": meta.get("data_risk", ""),
                       "sanctioned": bool(meta.get("sanctioned"))}
        if meta.get("risk") in RISK_LEVELS:
            g["risk"] = meta["risk"]
            g["risk_reason"] = meta.get("risk_reason", "")

    # Attribute per-device usage to each logical app.
    for u in usage:
        ref = appkey_to_ref.get(u.app_key)
        if not ref or ref not in la:
            continue
        c = client_by_key.get(u.client_key)
        la[ref]["devices"].add(_device_ref(c, u.client_key))

    # ---- Logical devices --------------------------------------------------
    ld: dict[str, dict] = {}
    for c in clients:
        ref = _device_ref(c, c.client_key)
        src = smap.get(c.integration_id, c.integration_id or "network")
        g = ld.setdefault(ref, {
            "ref": ref, "name": c.nickname or c.name or c.hostname or c.mac or ref,
            "device_type": c.device_type or "device", "sources": set(),
            "total_bytes": 0, "apps": set(), "owner_user_id": c.owner_user_id or "",
            "ownership": c.ownership or "", "last_seen": None})
        g["sources"].add(src)
        g["total_bytes"] += int(c.total_bytes or 0)
        if c.owner_user_id and not g["owner_user_id"]:
            g["owner_user_id"] = c.owner_user_id
        if c.last_seen and (g["last_seen"] is None or c.last_seen > g["last_seen"]):
            g["last_seen"] = c.last_seen
    key_to_devref = {c.client_key: _device_ref(c, c.client_key) for c in clients}
    for u in usage:
        dref = key_to_devref.get(u.client_key, "key:" + (u.client_key or "").lower())
        ref = appkey_to_ref.get(u.app_key)
        if dref in ld and ref:
            ld[dref]["apps"].add(ref)

    # Finalize apps.
    app_views = []
    for g in la.values():
        protectable = bool(g["source_type"])
        protected = protectable and g["source_type"] in enabled
        app_views.append({
            "ref": g["ref"], "name": g["name"], "category": g["category"],
            "sources": sorted(g["sources"]),
            "source_type": g["source_type"],
            "total_bytes": g["total_bytes"],
            "device_count": len(g["devices"]),
            "protectable": protectable, "protected": protected,
            "is_ai": g["is_ai"], "ai": g["ai"],
            "risk": g["risk"], "risk_reason": g["risk_reason"],
            "last_seen": g["last_seen"].isoformat() if g["last_seen"] else None,
        })

    dev_views = []
    for g in ld.values():
        dev_views.append({
            "ref": g["ref"], "name": g["name"], "device_type": g["device_type"],
            "sources": sorted(g["sources"]), "total_bytes": g["total_bytes"],
            "app_count": len(g["apps"]), "owner_user_id": g["owner_user_id"],
            "ownership": g["ownership"],
            "last_seen": g["last_seen"].isoformat() if g["last_seen"] else None,
        })
    return {"apps": app_views, "devices": dev_views, "enabled": enabled}


# --------------------------------------------------------------------------- #
# Views                                                                        #
# --------------------------------------------------------------------------- #
@router.get("/overview")
def overview(principal: security.Principal = Depends(security.get_principal),
             tenant: Tenant = Depends(security.get_tenant),
             db: Session = Depends(get_db)):
    _guard(principal, tenant, db)
    agg = _aggregate(db, tenant.id)
    apps, devices = agg["apps"], agg["devices"]
    by_source: dict[str, dict] = {}
    for a in apps:
        for s in a["sources"]:
            e = by_source.setdefault(s, {"source": s, "icon": SOURCE_ICON.get(s, "activity"),
                                         "apps": 0, "bytes": 0})
            e["apps"] += 1
            e["bytes"] += a["total_bytes"]
    for d in devices:
        for s in d["sources"]:
            e = by_source.setdefault(s, {"source": s, "icon": SOURCE_ICON.get(s, "activity"),
                                         "apps": 0, "bytes": 0})
    unprotected = [a for a in apps if a["protectable"] and not a["protected"]]
    return {
        "totals": {
            "apps": len(apps), "devices": len(devices),
            "bytes": sum(a["total_bytes"] for a in apps),
            "ai_apps": sum(1 for a in apps if a["is_ai"]),
            "unprotected": len(unprotected),
            "risky": sum(1 for a in apps if a["risk"]),
        },
        "by_source": sorted(by_source.values(), key=lambda s: -s["bytes"]),
        "top_apps": sorted(apps, key=lambda a: -a["total_bytes"])[:8],
        "risk_levels": list(RISK_LEVELS),
    }


@router.get("/apps")
def list_apps(q: str | None = None, category: str | None = None,
              source: str | None = None, protection: str | None = None,
              ai: bool | None = None, risk: bool | None = None,
              sort: str = "bytes", limit: int = 500,
              principal: security.Principal = Depends(security.get_principal),
              tenant: Tenant = Depends(security.get_tenant),
              db: Session = Depends(get_db)):
    _guard(principal, tenant, db)
    apps = _aggregate(db, tenant.id)["apps"]
    if q:
        ql = q.lower()
        apps = [a for a in apps if ql in a["name"].lower() or ql in a["ref"].lower()]
    if category:
        apps = [a for a in apps if a["category"] == category]
    if source:
        apps = [a for a in apps if source in a["sources"]]
    if protection == "unprotected":
        apps = [a for a in apps if a["protectable"] and not a["protected"]]
    elif protection == "protected":
        apps = [a for a in apps if a["protected"]]
    if ai is True:
        apps = [a for a in apps if a["is_ai"]]
    if risk is True:
        apps = [a for a in apps if a["risk"]]
    keys = {"bytes": lambda a: -a["total_bytes"], "name": lambda a: a["name"].lower(),
            "devices": lambda a: -a["device_count"]}
    apps.sort(key=keys.get(sort, keys["bytes"]))
    facets = {
        "categories": sorted({a["category"] for a in apps if a["category"]}),
        "sources": sorted({s for a in apps for s in a["sources"]}),
    }
    return {"total": len(apps), "apps": apps[:max(1, min(2000, limit))], "facets": facets}


@router.get("/devices")
def list_devices(q: str | None = None, source: str | None = None, sort: str = "bytes",
                 limit: int = 500,
                 principal: security.Principal = Depends(security.get_principal),
                 tenant: Tenant = Depends(security.get_tenant),
                 db: Session = Depends(get_db)):
    _guard(principal, tenant, db)
    devices = _aggregate(db, tenant.id)["devices"]
    if q:
        ql = q.lower()
        devices = [d for d in devices if ql in d["name"].lower()]
    if source:
        devices = [d for d in devices if source in d["sources"]]
    keys = {"bytes": lambda d: -d["total_bytes"], "name": lambda d: d["name"].lower(),
            "apps": lambda d: -d["app_count"]}
    devices.sort(key=keys.get(sort, keys["bytes"]))
    return {"total": len(devices), "devices": devices[:max(1, min(2000, limit))]}


@router.get("/apps/{ref:path}")
def app_detail(ref: str,
               principal: security.Principal = Depends(security.get_principal),
               tenant: Tenant = Depends(security.get_tenant),
               db: Session = Depends(get_db)):
    """Drill-down: the devices that use a logical app + per-source breakdown."""
    _guard(principal, tenant, db)
    tid = tenant.id
    apps, usage, clients = _load(db, tid)
    smap = _source_map(db, tid)
    client_by_key = {c.client_key: c for c in clients}
    appkeys = {a.app_key for a in apps
               if _app_ref(a.name, a.meta if isinstance(a.meta, dict) else {}) == ref}
    if not appkeys:
        raise HTTPException(404, "app not found")
    head = next(a for a in apps if a.app_key in appkeys)
    meta = head.meta if isinstance(head.meta, dict) else {}
    by_source: dict[str, dict] = {}
    for a in apps:
        if a.app_key in appkeys:
            s = smap.get(a.integration_id, a.integration_id or "network")
            e = by_source.setdefault(s, {"source": s, "icon": SOURCE_ICON.get(s, "activity"),
                                         "bytes": 0})
            e["bytes"] += int(a.total_bytes or 0)
    devs: dict[str, dict] = {}
    for u in usage:
        if u.app_key not in appkeys:
            continue
        c = client_by_key.get(u.client_key)
        dref = _device_ref(c, u.client_key)
        e = devs.setdefault(dref, {
            "ref": dref, "name": (c.nickname or c.name or c.hostname or c.mac) if c else u.client_key,
            "device_type": (c.device_type or "device") if c else "device",
            "bytes": 0, "sessions": 0})
        e["bytes"] += int(u.total_bytes or 0)
        e["sessions"] += int(u.sessions or 0)
    return {
        "ref": ref, "name": meta.get("name") or head.name,
        "category": "AI" if meta.get("ai") else (head.category or ""),
        "source_type": head.source_type or "",
        "is_ai": bool(meta.get("ai")),
        "ai": ({"vendor": meta.get("vendor", ""), "data_risk": meta.get("data_risk", ""),
                "ai_category": meta.get("ai_category", ""),
                "sanctioned": bool(meta.get("sanctioned"))} if meta.get("ai") else None),
        "risk": meta.get("risk", "") if meta.get("risk") in RISK_LEVELS else "",
        "risk_reason": meta.get("risk_reason", ""),
        "by_source": sorted(by_source.values(), key=lambda s: -s["bytes"]),
        "devices": sorted(devs.values(), key=lambda d: -d["bytes"]),
    }


@router.get("/devices/{ref:path}")
def device_detail(ref: str,
                  principal: security.Principal = Depends(security.get_principal),
                  tenant: Tenant = Depends(security.get_tenant),
                  db: Session = Depends(get_db)):
    """Drill-down: the apps a logical device used."""
    _guard(principal, tenant, db)
    tid = tenant.id
    apps, usage, clients = _load(db, tid)
    smap = _source_map(db, tid)
    app_by_key = {}
    for a in apps:
        app_by_key.setdefault(a.app_key, a)
    member_keys = {c.client_key for c in clients if _device_ref(c, c.client_key) == ref}
    if not member_keys:
        member_keys = {ref.split(":", 1)[-1]}
    head = next((c for c in clients if c.client_key in member_keys), None)
    used: dict[str, dict] = {}
    for u in usage:
        if u.client_key not in member_keys:
            continue
        a = app_by_key.get(u.app_key)
        if a is None:
            continue
        meta = a.meta if isinstance(a.meta, dict) else {}
        aref = _app_ref(a.name, meta)
        e = used.setdefault(aref, {
            "ref": aref, "name": meta.get("name") or a.name or a.app_key,
            "category": "AI" if meta.get("ai") else (a.category or ""),
            "is_ai": bool(meta.get("ai")), "source_type": a.source_type or "",
            "risk": meta.get("risk", "") if meta.get("risk") in RISK_LEVELS else "",
            "bytes": 0})
        e["bytes"] += int(u.total_bytes or 0)
    return {
        "ref": ref,
        "name": (head.nickname or head.name or head.hostname or head.mac) if head else ref,
        "device_type": (head.device_type or "device") if head else "device",
        "sources": sorted({smap.get(c.integration_id, c.integration_id or "network")
                           for c in clients if c.client_key in member_keys}),
        "apps": sorted(used.values(), key=lambda a: -a["bytes"]),
    }


@router.get("/shadow")
def shadow(principal: security.Principal = Depends(security.get_principal),
           tenant: Tenant = Depends(security.get_tenant),
           db: Session = Depends(get_db)):
    """Cross-source 'apps you aren't protecting': services seen in traffic that map
    to an Arkive connector you haven't enabled, grouped by the connector to add."""
    _guard(principal, tenant, db)
    agg = _aggregate(db, tenant.id)
    groups: dict[str, dict] = {}
    recommend: dict[str, dict] = {}
    for a in agg["apps"]:
        if a["is_ai"]:
            continue
        if a["protectable"] and not a["protected"]:
            st = a["source_type"]
            g = groups.setdefault(st, {"source_type": st, "name": a["name"],
                                       "total_bytes": 0, "apps": 0, "devices": 0})
            g["total_bytes"] += a["total_bytes"]
            g["apps"] += 1
            g["devices"] = max(g["devices"], a["device_count"])
        elif not a["protectable"]:
            cand = candidate_service(a["name"], a["category"])
            if cand:
                r = recommend.setdefault(cand["name"], {
                    "name": cand["name"], "kind": cand.get("kind", ""),
                    "total_bytes": 0, "apps": 0})
                r["total_bytes"] += a["total_bytes"]
                r["apps"] += 1
    return {
        "unprotected": sorted(groups.values(), key=lambda g: -g["total_bytes"]),
        "recommended": sorted(recommend.values(), key=lambda r: -r["total_bytes"])[:20],
    }


class RiskFlag(BaseModel):
    ref: str
    risk: str = "concerning"   # concerning | risky | blocked | "" to clear
    reason: str = ""


@router.post("/flags")
def set_flag(body: RiskFlag,
             principal: security.Principal = Depends(security.get_principal),
             tenant: Tenant = Depends(security.get_tenant),
             db: Session = Depends(get_db)):
    """Flag (or clear) a logical app/protocol as concerning/risky/blocked. The flag
    is stored on every NetworkApp row for that app so it survives + replicates."""
    _guard(principal, tenant, db)
    if not security.is_org_admin(principal.role) and not principal.is_platform_admin:
        raise HTTPException(403, "Only an organization administrator can flag apps.")
    risk = (body.risk or "").strip().lower()
    if risk and risk not in RISK_LEVELS:
        raise HTTPException(400, f"invalid risk; allowed: {list(RISK_LEVELS)} or empty to clear")
    now = _now()
    rows = db.query(NetworkApp).filter(NetworkApp.tenant_id == tenant.id).all()
    n = 0
    for a in rows:
        meta = dict(a.meta or {})
        if _app_ref(a.name, meta) != body.ref:
            continue
        if risk:
            meta.update({"risk": risk, "risk_reason": (body.reason or "")[:300],
                         "flagged_by": principal.user_id, "flagged_at": now.isoformat()})
        else:
            for k in ("risk", "risk_reason", "flagged_by", "flagged_at"):
                meta.pop(k, None)
        a.meta = meta
        a.updated_at = now
        n += 1
    if n == 0:
        raise HTTPException(404, "app not found")
    audit.record(db, actor=principal.user_id, action="netanalytics.flag",
                 tenant_id=tenant.id, resource=body.ref, category="admin",
                 severity="warning" if risk else "info",
                 detail={"ref": body.ref, "risk": risk or "cleared",
                         "reason": (body.reason or "")[:200]})
    db.commit()
    return {"ok": True, "ref": body.ref, "risk": risk, "rows": n}
