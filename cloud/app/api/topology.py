"""Platform topology admin: Clusters + Regions.

A Cluster groups the fleet's nodes (exactly one control-plane node + N others) and
serves one or more customer-facing Regions. New accounts are geo-routed to a region
→ its cluster → the least-full customer node. All endpoints are platform-admin only.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import audit, geo, routing, security
from ..db import get_db
from ..models import Cluster, Node, Region, Tenant

router = APIRouter(prefix="/admin", tags=["topology"],
                   dependencies=[Depends(security.require_platform_admin)])


def _norm_provider(cloud: dict | None) -> str:
    """Normalise a node's IMDS provider to aws|azure|gcp, else '' (on-prem/unknown)."""
    p = ((cloud or {}).get("provider", "") or "").lower()
    if "aws" in p or "amazon" in p:
        return "aws"
    if "azure" in p or "microsoft" in p:
        return "azure"
    if "gcp" in p or "google" in p:
        return "gcp"
    return ""


def _cluster_view(db: Session, c: Cluster) -> dict:
    from .admin import _node_view
    node_rows = db.query(Node).filter(Node.cluster_id == c.id).all()
    nodes = [_node_view(db, n) for n in node_rows]
    cp = next((n for n in nodes if n["role"] == "control-plane"), None)
    regions = db.query(Region).filter(Region.cluster_id == c.id).order_by(Region.sort_order).all()

    online = sum(1 for n in nodes if n.get("online"))
    tenants = sum(int(n.get("tenants") or 0) for n in nodes)

    def _worst(key: str):
        vals = [n["health"].get(key) for n in nodes
                if n.get("online") and n.get("health", {}).get(key) is not None]
        return round(max(vals), 1) if vals else None

    used = total = 0
    for n in nodes:
        stg = (n.get("telemetry") or {}).get("storage") or {}
        used += int(stg.get("used") or 0)
        total += int(stg.get("total") or 0)

    # Cloud platform of the cluster: single provider if consistent, else "mixed".
    providers = sorted({p for p in (_norm_provider(n.get("cloud")) for n in nodes) if p})
    platform = providers[0] if len(providers) == 1 else ("mixed" if len(providers) > 1 else "")

    # Health rollup + operator warnings.
    offline = len(nodes) - online
    warnings: list[str] = []
    if node_rows and cp is None:
        warnings.append("No control-plane node is assigned to this cluster.")
    if len(providers) > 1:
        warnings.append("Nodes span multiple cloud platforms (" +
                        ", ".join(p.upper() for p in providers) +
                        "). A cluster should run on a single platform.")
    if offline and nodes:
        warnings.append(f"{offline} node(s) offline.")
    for key, label in (("cpu_pct", "CPU"), ("mem_pct", "Memory"), ("disk_pct", "Disk")):
        w = _worst(key)
        if w is not None and w >= 90:
            warnings.append(f"{label} utilisation is high ({w:.0f}%).")
    if total and (used / total) >= 0.9:
        warnings.append("Cluster storage is over 90% full.")

    if node_rows and (cp is None or online == 0):
        health = "critical"
    elif warnings:
        health = "warn"
    else:
        health = "ok"

    compact = [{
        "id": n["id"], "name": n["name"], "role": n["role"], "status": n["status"],
        "is_self": n["is_self"], "endpoint": n["endpoint"], "online": n["online"],
        "version": n["version"], "tenants": n["tenants"], "health": n["health"],
        "cloud": n.get("cloud") or {}, "platform": _norm_provider(n.get("cloud")),
    } for n in nodes]

    return {
        "id": c.id, "code": c.code, "name": c.name, "description": c.description or "",
        "home_region": c.home_region or "", "status": c.status or "active",
        "global_sync_enabled": bool(c.global_sync_enabled),
        "control_plane": ({"id": cp["id"], "name": cp["name"]} if cp else None),
        "node_count": len(nodes),
        "customer_node_count": sum(1 for n in nodes if n["role"] == "customer-tenant"),
        "platform": platform, "platforms": providers,
        "health": health, "warnings": warnings,
        "nodes": compact,
        "regions": [{"code": r.code, "name": r.name} for r in regions],
        "summary": {
            "nodes_online": online, "nodes_total": len(nodes), "tenants": tenants,
            "cpu_pct": _worst("cpu_pct"), "mem_pct": _worst("mem_pct"),
            "disk_pct": _worst("disk_pct"),
            "storage_used": used, "storage_total": total,
            "health": health,
        },
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


def _region_view(db: Session, r: Region) -> dict:
    cl = db.get(Cluster, r.cluster_id) if r.cluster_id else None
    tenants = db.query(func.count(Tenant.id)).filter(Tenant.region_code == r.code).scalar() or 0
    return {
        "id": r.id, "code": r.code, "name": r.name, "geo_zone": r.geo_zone or "",
        "cluster_id": r.cluster_id, "cluster_name": (cl.name if cl else None),
        "signup_enabled": bool(r.signup_enabled), "sort_order": r.sort_order or 100,
        "tenant_count": int(tenants),
        "accepted_countries": sorted(geo.ACCEPTED_COUNTRIES) if r.geo_zone == "north-america" else [],
    }


@router.get("/topology")
def get_topology(db: Session = Depends(get_db)):
    clusters = db.query(Cluster).order_by(Cluster.created_at.asc()).all()
    regions = db.query(Region).order_by(Region.sort_order).all()
    all_nodes = db.query(Node).order_by(Node.role, Node.name).all()
    unassigned = [n for n in all_nodes if not n.cluster_id]
    return {
        "clusters": [_cluster_view(db, c) for c in clusters],
        "regions": [_region_view(db, r) for r in regions],
        "unassigned_nodes": [{"id": n.id, "name": n.name, "role": n.role,
                              "cloud": n.cloud or {}} for n in unassigned],
        # Every node + its current cluster, so the cluster page can offer an
        # "add existing node" picker (moving a node from one cluster to another).
        "all_nodes": [{"id": n.id, "name": n.name, "role": n.role,
                       "cluster_id": n.cluster_id, "cloud": n.cloud or {}}
                      for n in all_nodes],
        "geo_zones": sorted({r["geo_zone"] for r in geo.REGION_TAXONOMY}),
        "accepted_countries": sorted(geo.ACCEPTED_COUNTRIES),
    }


# ------------------------------- Clusters ---------------------------------- #

class ClusterBody(BaseModel):
    code: str
    name: str
    description: str = ""
    home_region: str = ""
    status: str = "active"
    global_sync_enabled: bool = False


class ClusterUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    home_region: str | None = None
    status: str | None = None
    global_sync_enabled: bool | None = None


@router.post("/clusters")
def create_cluster(body: ClusterBody,
                   principal: security.Principal = Depends(security.require_platform_admin),
                   db: Session = Depends(get_db)):
    code = body.code.strip().lower()
    if not code or not body.name.strip():
        raise HTTPException(400, "code and name are required")
    if db.query(Cluster).filter(Cluster.code == code).first():
        raise HTTPException(409, "a cluster with this code already exists")
    c = Cluster(code=code, name=body.name.strip(), description=body.description or "",
                home_region=body.home_region or "", status=body.status or "active",
                global_sync_enabled=bool(body.global_sync_enabled))
    db.add(c)
    db.commit()
    audit.record(db, actor=principal.user_id, action="cluster.created", resource=c.id,
                 detail={"code": code})
    return _cluster_view(db, c)


@router.put("/clusters/{cid}")
def update_cluster(cid: str, body: ClusterUpdate,
                   principal: security.Principal = Depends(security.require_platform_admin),
                   db: Session = Depends(get_db)):
    c = db.get(Cluster, cid)
    if not c:
        raise HTTPException(404, "cluster not found")
    for f in ("name", "description", "home_region", "status", "global_sync_enabled"):
        v = getattr(body, f)
        if v is not None:
            setattr(c, f, v)
    db.commit()
    audit.record(db, actor=principal.user_id, action="cluster.updated", resource=c.id)
    return _cluster_view(db, c)


@router.delete("/clusters/{cid}")
def delete_cluster(cid: str,
                   principal: security.Principal = Depends(security.require_platform_admin),
                   db: Session = Depends(get_db)):
    c = db.get(Cluster, cid)
    if not c:
        raise HTTPException(404, "cluster not found")
    # Detach nodes + regions so nothing dangles at the FK.
    for n in db.query(Node).filter(Node.cluster_id == cid).all():
        n.cluster_id = None
    for r in db.query(Region).filter(Region.cluster_id == cid).all():
        r.cluster_id = None
        r.signup_enabled = False
    db.delete(c)
    db.commit()
    audit.record(db, actor=principal.user_id, action="cluster.deleted", resource=cid,
                 severity="warning")
    return {"deleted": True}


class NodeClusterBody(BaseModel):
    cluster_id: str | None = None


@router.put("/nodes/{nid}/cluster")
def assign_node_cluster(nid: str, body: NodeClusterBody,
                        principal: security.Principal = Depends(security.require_platform_admin),
                        db: Session = Depends(get_db)):
    n = db.get(Node, nid)
    if not n:
        raise HTTPException(404, "node not found")
    if body.cluster_id:
        c = db.get(Cluster, body.cluster_id)
        if not c:
            raise HTTPException(404, "cluster not found")
        # Enforce exactly one control-plane node per cluster.
        if n.role == "control-plane":
            other = (db.query(Node)
                     .filter(Node.cluster_id == c.id, Node.role == "control-plane",
                             Node.id != n.id).first())
            if other is not None:
                raise HTTPException(409, f"cluster already has a control plane ({other.name})")
        n.cluster_id = c.id
    else:
        n.cluster_id = None
    db.commit()
    audit.record(db, actor=principal.user_id, action="node.cluster_assigned", resource=nid,
                 detail={"cluster_id": body.cluster_id})
    return {"id": n.id, "cluster_id": n.cluster_id}


# -------------------------------- Regions ---------------------------------- #

class RegionBody(BaseModel):
    code: str
    name: str
    geo_zone: str = ""
    cluster_id: str | None = None
    signup_enabled: bool = False
    sort_order: int = 100


class RegionUpdate(BaseModel):
    name: str | None = None
    geo_zone: str | None = None
    cluster_id: str | None = None
    signup_enabled: bool | None = None
    sort_order: int | None = None


@router.post("/regions")
def create_region(body: RegionBody,
                  principal: security.Principal = Depends(security.require_platform_admin),
                  db: Session = Depends(get_db)):
    code = body.code.strip().lower()
    if not code or not body.name.strip():
        raise HTTPException(400, "code and name are required")
    if db.query(Region).filter(Region.code == code).first():
        raise HTTPException(409, "a region with this code already exists")
    if body.cluster_id and not db.get(Cluster, body.cluster_id):
        raise HTTPException(404, "cluster not found")
    r = Region(code=code, name=body.name.strip(), geo_zone=body.geo_zone or "",
               cluster_id=body.cluster_id or None,
               signup_enabled=bool(body.signup_enabled), sort_order=body.sort_order or 100)
    db.add(r)
    db.commit()
    audit.record(db, actor=principal.user_id, action="region.created", resource=r.id,
                 detail={"code": code})
    return _region_view(db, r)


@router.put("/regions/{rid}")
def update_region(rid: str, body: RegionUpdate,
                  principal: security.Principal = Depends(security.require_platform_admin),
                  db: Session = Depends(get_db)):
    r = db.get(Region, rid)
    if not r:
        raise HTTPException(404, "region not found")
    if body.cluster_id is not None and body.cluster_id and not db.get(Cluster, body.cluster_id):
        raise HTTPException(404, "cluster not found")
    # A region can't accept signups without a cluster to serve it.
    if body.signup_enabled and not (body.cluster_id or r.cluster_id):
        raise HTTPException(400, "assign a cluster before enabling signups for this region")
    for f in ("name", "geo_zone", "cluster_id", "signup_enabled", "sort_order"):
        v = getattr(body, f)
        if v is not None:
            setattr(r, f, v or None if f == "cluster_id" else v)
    db.commit()
    audit.record(db, actor=principal.user_id, action="region.updated", resource=r.id)
    return _region_view(db, r)


@router.delete("/regions/{rid}")
def delete_region(rid: str,
                  principal: security.Principal = Depends(security.require_platform_admin),
                  db: Session = Depends(get_db)):
    r = db.get(Region, rid)
    if not r:
        raise HTTPException(404, "region not found")
    if r.code in geo.REGION_CODES:
        raise HTTPException(400, "built-in regions can't be deleted — disable signups instead")
    db.delete(r)
    db.commit()
    audit.record(db, actor=principal.user_id, action="region.deleted", resource=rid,
                 severity="warning")
    return {"deleted": True}
