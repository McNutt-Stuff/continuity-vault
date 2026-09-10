"""Cluster/Region topology helpers: default-topology seeding, geo→region→cluster
routing, and least-full node placement for new accounts.

A Cluster is the unit of horizontal scale — exactly one control-plane node plus N
customer-tenant / other nodes. Regions (customer-facing geographies) are served by
a cluster. A new account is routed to a region by the address it provides, then
placed on the least-full CUSTOMER node in that region's cluster (falling back to
the control plane when no customer node is available).
"""

from __future__ import annotations

import logging

from sqlalchemy import func
from sqlalchemy.orm import Session

from . import geo
from .models import Cluster, Node, Region, Tenant

logger = logging.getLogger("cv.topology")

# Soft cap on tenants per customer node, used to normalise the fullness score
# alongside disk utilisation. Nodes past this still accept placement (least-full
# wins) but score as "full".
_TENANT_SOFT_CAP = 500

DEFAULT_CLUSTER_CODE = "nam-1"


def ensure_default_topology(db: Session) -> None:
    """Idempotently seed the global region taxonomy + one default cluster, and
    attach existing nodes / activate the North-American regions. Safe to run on
    every startup (only creates what's missing; never clobbers admin edits)."""
    try:
        # 1) Region taxonomy — create any missing region (don't touch existing).
        existing = {r.code for r in db.query(Region.code).all()}
        for spec in geo.REGION_TAXONOMY:
            if spec["code"] in existing:
                continue
            db.add(Region(code=spec["code"], name=spec["name"], geo_zone=spec["geo_zone"],
                          sort_order=spec["sort_order"],
                          signup_enabled=bool(spec["signup_default"])))
        db.commit()

        # 2) One default cluster if none exists.
        cluster = db.query(Cluster).filter(Cluster.code == DEFAULT_CLUSTER_CODE).first()
        if cluster is None and db.query(Cluster).count() == 0:
            cluster = Cluster(code=DEFAULT_CLUSTER_CODE, name="North America Primary",
                              description="Serves North America East and West.",
                              home_region="nam-east", status="active")
            db.add(cluster)
            db.commit()

        cluster = cluster or db.query(Cluster).order_by(Cluster.created_at.asc()).first()
        if cluster is None:
            return

        # 3) Point the two North-American regions at the default cluster (only if
        #    unassigned) and make sure they accept signups.
        for code in ("nam-east", "nam-west"):
            r = db.query(Region).filter(Region.code == code).first()
            if r is not None and not r.cluster_id:
                r.cluster_id = cluster.id
                r.signup_enabled = True
        db.commit()

        # 4) Attach any clusterless nodes to the default cluster so the single
        #    cluster owns the whole current fleet.
        for n in db.query(Node).filter(Node.cluster_id.is_(None)).all():
            n.cluster_id = cluster.id
        db.commit()
    except Exception:  # noqa: BLE001 — never block startup on seeding
        db.rollback()
        logger.exception("ensure_default_topology failed")


def node_fullness(db: Session, node: Node) -> float:
    """A 0..1 fullness score (higher = fuller). Combines disk utilisation from the
    node's telemetry with its tenant load; the max of the two drives placement so
    a node that's tight on EITHER axis is deprioritised."""
    tel = node.telemetry or {}
    disk = 0.0
    try:
        disk = float(tel.get("disk_pct") or (tel.get("storage") or {}).get("pct") or 0) / 100.0
    except Exception:  # noqa: BLE001
        disk = 0.0
    tenants = (db.query(func.count(Tenant.id))
               .filter(Tenant.node_id == node.id).scalar() or 0)
    load = min(1.0, tenants / float(_TENANT_SOFT_CAP))
    return max(0.0, min(1.0, max(disk, load)))


def _cluster_for_region(db: Session, region_code: str) -> Cluster | None:
    r = db.query(Region).filter(Region.code == region_code).first()
    if r is not None and r.cluster_id:
        c = db.get(Cluster, r.cluster_id)
        if c is not None and (c.status or "active") == "active":
            return c
    # Fallback: the only/first active cluster (single-cluster deployments).
    return (db.query(Cluster).filter(Cluster.status == "active")
            .order_by(Cluster.created_at.asc()).first())


def pick_node_for_region(db: Session, region_code: str) -> Node | None:
    """The least-full active customer-tenant node in the region's cluster, or None
    (→ handled by the control plane) when the cluster has no customer node yet."""
    cluster = _cluster_for_region(db, region_code)
    if cluster is None:
        return None
    candidates = (db.query(Node)
                  .filter(Node.cluster_id == cluster.id,
                          Node.role == "customer-tenant",
                          Node.status == "active",
                          Node.is_self.is_(False),
                          Node.endpoint != "")
                  .all())
    if not candidates:
        return None
    return min(candidates, key=lambda n: node_fullness(db, n))


def route_new_tenant(db: Session, tenant: Tenant, country: str,
                     subdivision: str = "") -> tuple[str, Node | None]:
    """Resolve the region from the address, record it on the tenant, and place the
    tenant on the least-full customer node in that region's cluster (NULL node_id
    = the control plane handles it). Caller commits. Returns (region_code, node)."""
    region_code = geo.resolve_region(country, subdivision)
    tenant.region_code = region_code
    node = pick_node_for_region(db, region_code)
    tenant.node_id = node.id if node is not None else None
    logger.info("routed tenant %s → region=%s node=%s", tenant.id, region_code,
                node.name if node is not None else "control-plane")
    return region_code, node
