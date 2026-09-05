"""Unified-search index replication status (DR copies of the search index)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import security
from ..db import get_db
from ..models import IndexReplica, Tenant

router = APIRouter(prefix="/index", tags=["index"])


def _scope_for(tenant: Tenant, principal: security.Principal) -> tuple[str, str]:
    """Personal accounts replicate the USER's index; org tenants replicate the
    whole TENANT index (appliances are tenant-assigned)."""
    if (tenant.tenant_type or "dedicated") == "shared":
        return "user", principal.user_id
    return "tenant", tenant.id


def _replica_view(r: IndexReplica) -> dict:
    return {
        "id": r.id, "destination": r.destination,
        "destination_label": r.destination_label or r.destination,
        "status": r.status, "object_count": r.object_count or 0,
        "bytes": int(r.bytes or 0),
        "last_replicated_at": r.last_replicated_at.isoformat() if r.last_replicated_at else None,
        "error": r.error or "",
    }


@router.get("/status")
def index_status(principal: security.Principal = Depends(security.get_principal),
                 tenant: Tenant = Depends(security.get_tenant),
                 db: Session = Depends(get_db)):
    """Health of every replicated copy of the caller's search index (one per
    storage destination), for the Overview / Storage / Appliances surfaces."""
    scope, scope_id = _scope_for(tenant, principal)
    rows = (db.query(IndexReplica)
            .filter(IndexReplica.scope == scope, IndexReplica.scope_id == scope_id)
            .all())
    replicas = [_replica_view(r) for r in rows]
    ok = sum(1 for r in replicas if r["status"] == "ok")
    return {
        "scope": scope,
        "replicas": replicas,
        "protected": ok > 0,
        "healthy": ok, "total": len(replicas),
        "by_destination": {r["destination"]: r for r in replicas},
    }
