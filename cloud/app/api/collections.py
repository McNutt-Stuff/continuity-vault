"""Collections + backup execution (sync worker trigger)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import audit, security
from ..db import get_db
from ..connectors import get_connector
from ..models import (
    Collection,
    ConnectorAccount,
    DesktopAgent,
    SearchDocument,
    SnapshotReceipt,
    Tenant,
    Vault,
)
from ..workers.sync_worker import run_backup

router = APIRouter(prefix="/collections", tags=["collections"])
logger = logging.getLogger("cv.collections")


class CreateCollectionRequest(BaseModel):
    vault_id: str
    name: str
    source_type: str
    connector_account_id: str | None = None
    sensitivity: str = "standard"
    destinations: list[str] = ["cv-cloud"]


@router.post("")
def create_collection(body: CreateCollectionRequest,
                      principal: security.Principal = Depends(security.get_principal),
                      tenant: Tenant = Depends(security.get_tenant),
                      db: Session = Depends(get_db)):
    vault = db.get(Vault, body.vault_id)
    if not vault or vault.tenant_id != tenant.id:
        raise HTTPException(404, "vault not found")
    coll = Collection(
        tenant_id=tenant.id,
        vault_id=vault.id,
        name=body.name,
        source_type=body.source_type,
        connector_account_id=body.connector_account_id,
        sensitivity=body.sensitivity,
        destinations=body.destinations or ["cv-cloud"],
    )
    db.add(coll)
    db.commit()
    db.refresh(coll)
    audit.record(db, actor=principal.user_id, action="collection.created",
                 tenant_id=tenant.id, resource=coll.id)
    return _collection_view(db, coll)


class UpdateCollectionRequest(BaseModel):
    name: str | None = None
    vault_id: str | None = None
    sensitivity: str | None = None
    destinations: list[str] | None = None
    index_fields: list[str] | None = None


@router.put("/{collection_id}")
def update_collection(collection_id: str, body: UpdateCollectionRequest,
                      principal: security.Principal = Depends(security.get_principal),
                      tenant: Tenant = Depends(security.get_tenant),
                      db: Session = Depends(get_db)):
    coll = db.get(Collection, collection_id)
    if not coll or coll.tenant_id != tenant.id:
        raise HTTPException(404, "collection not found")
    if body.vault_id is not None:
        vault = db.get(Vault, body.vault_id)
        if not vault or vault.tenant_id != tenant.id:
            raise HTTPException(404, "vault not found")
        coll.vault_id = body.vault_id
    if body.name is not None:
        coll.name = body.name
    if body.sensitivity is not None:
        coll.sensitivity = body.sensitivity
    if body.destinations is not None:
        coll.destinations = body.destinations or ["cv-cloud"]
    if body.index_fields is not None:
        coll.index_fields = body.index_fields
    db.commit()
    db.refresh(coll)
    audit.record(db, actor=principal.user_id, action="collection.updated",
                 tenant_id=tenant.id, resource=coll.id,
                 detail={"destinations": coll.destinations,
                         "index_fields": coll.index_fields})
    return _collection_view(db, coll)


def _available_fields(source_type: str) -> list[str]:
    conn = get_connector(source_type)
    if not conn:
        return []
    caps = conn.capabilities()
    out: list[str] = []
    for k in [*(caps.facet_fields or []), *(caps.searchable_fields or [])]:
        if k and k != "*" and k not in out:
            out.append(k)
    return out


def _collection_view(db: Session, c: Collection) -> dict:
    vault = db.get(Vault, c.vault_id)
    account = db.get(ConnectorAccount, c.connector_account_id) if c.connector_account_id else None
    conn = get_connector(c.source_type)
    available = _available_fields(c.source_type)
    # Last backup status across this mapping's snapshots.
    last = (db.query(SnapshotReceipt)
            .filter(SnapshotReceipt.collection_id == c.id)
            .order_by(SnapshotReceipt.created_at.desc()).first())
    is_agent = bool(conn and conn.capabilities().requires_agent)
    return {
        "id": c.id, "name": c.name, "source_type": c.source_type,
        "source_display": conn.display_name if conn else c.source_type,
        "source_label": account.account_label if account else c.name,
        "is_agent": is_agent,
        "vault_id": c.vault_id, "vault_name": vault.name if vault else None,
        "connector_account_id": c.connector_account_id,
        "account_label": account.account_label if account else None,
        "sensitivity": c.sensitivity,
        "destinations": c.destinations or ["cv-cloud"],
        "index_fields": list(c.index_fields or []),
        "available_fields": available,
        "last_backup_at": last.created_at.isoformat() if last else None,
        "last_object_count": last.object_count if last else 0,
        "last_recoverable": bool(last.recoverable) if last else False,
    }


@router.get("")
def list_collections(tenant: Tenant = Depends(security.get_tenant),
                     db: Session = Depends(get_db)):
    colls = db.query(Collection).filter(Collection.tenant_id == tenant.id).all()
    return [_collection_view(db, c) for c in colls]


@router.delete("/{collection_id}")
def delete_collection(collection_id: str,
                      principal: security.Principal = Depends(security.get_principal),
                      tenant: Tenant = Depends(security.get_tenant),
                      db: Session = Depends(get_db)):
    c = db.get(Collection, collection_id)
    if not c or c.tenant_id != tenant.id:
        raise HTTPException(404, "collection not found")
    # Remove dependent rows first — snapshot receipts and search-index entries
    # reference this collection (no DB cascade), so deleting the mapping while it
    # still has backup history would otherwise fail with a foreign-key error.
    db.query(SearchDocument).filter(SearchDocument.collection_id == collection_id).delete()
    db.query(SnapshotReceipt).filter(SnapshotReceipt.collection_id == collection_id).delete()
    db.delete(c)
    db.commit()
    audit.record(db, actor=principal.user_id, action="collection.deleted",
                 tenant_id=tenant.id, resource=collection_id)
    return {"ok": True}


class BackupRequest(BaseModel):
    destinations: list[str] | None = None  # falls back to the mapping's destinations


@router.post("/{collection_id}/backup")
def backup(collection_id: str, body: BackupRequest,
           principal: security.Principal = Depends(security.get_principal),
           tenant: Tenant = Depends(security.get_tenant),
           db: Session = Depends(get_db)):
    coll = db.get(Collection, collection_id)
    if not coll or coll.tenant_id != tenant.id:
        raise HTTPException(404, "collection not found")
    dests = body.destinations or coll.destinations or ["cv-cloud"]
    try:
        receipt = run_backup(db, coll, dests)
    except Exception as exc:
        logger.exception("backup failed for collection %s (%s)", coll.id, coll.source_type)
        raise HTTPException(502, f"backup failed: {exc}")
    return {
        "snapshot_id": receipt.snapshot_id,
        "object_count": receipt.object_count,
        "total_bytes": receipt.total_bytes,
        "destinations": dests,
        "recoverable": receipt.recoverable,
        "manifest_hash": receipt.manifest_hash,
    }


@router.post("/{collection_id}/sync")
def sync(collection_id: str,
         principal: security.Principal = Depends(security.get_principal),
         tenant: Tenant = Depends(security.get_tenant),
         db: Session = Depends(get_db)):
    """Trigger this source's natural sync, routing through the mapping.

    - Connector (cloud-pull) sources run a backup immediately.
    - Agent-collected sources (e.g. 1Password) queue a `collect` command to the
      matching desktop agent(s); the agent then collects locally and pushes
      through the same pipeline into the mapping's destinations.
    """
    coll = db.get(Collection, collection_id)
    if not coll or coll.tenant_id != tenant.id:
        raise HTTPException(404, "collection not found")

    conn = get_connector(coll.source_type)
    is_agent = bool(conn and conn.capabilities().requires_agent)

    if is_agent:
        agents = (db.query(DesktopAgent)
                  .filter(DesktopAgent.tenant_id == tenant.id).all())
        targeted = [a for a in agents if coll.source_type in (a.collectors or [])] or agents
        queued = 0
        for a in targeted:
            a.pending_command = {"type": "collect", "params": {}}
            queued += 1
        db.commit()
        audit.record(db, actor=principal.user_id, action="source.sync_requested",
                     tenant_id=tenant.id, resource=coll.id,
                     detail={"kind": "agent", "agents": queued})
        if queued == 0:
            raise HTTPException(409, "no desktop agent is linked to collect this source")
        return {"kind": "agent", "queued_agents": queued,
                "message": f"Queued collection on {queued} agent(s); data will arrive shortly."}

    dests = coll.destinations or ["cv-cloud"]
    try:
        receipt = run_backup(db, coll, dests)
    except Exception as exc:
        logger.exception("sync failed for collection %s (%s)", coll.id, coll.source_type)
        raise HTTPException(502, f"sync failed: {exc}")
    return {"kind": "connector", "snapshot_id": receipt.snapshot_id,
            "object_count": receipt.object_count, "destinations": dests,
            "recoverable": receipt.recoverable}
