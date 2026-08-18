"""Collections + backup execution (sync worker trigger)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import audit, security
from ..db import get_db
from ..models import Collection, ConnectorAccount, SearchDocument, SnapshotReceipt, Tenant, Vault
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
    db.commit()
    db.refresh(coll)
    audit.record(db, actor=principal.user_id, action="collection.updated",
                 tenant_id=tenant.id, resource=coll.id,
                 detail={"destinations": coll.destinations})
    return _collection_view(db, coll)


def _collection_view(db: Session, c: Collection) -> dict:
    vault = db.get(Vault, c.vault_id)
    account = db.get(ConnectorAccount, c.connector_account_id) if c.connector_account_id else None
    return {
        "id": c.id, "name": c.name, "source_type": c.source_type,
        "vault_id": c.vault_id, "vault_name": vault.name if vault else None,
        "connector_account_id": c.connector_account_id,
        "account_label": account.account_label if account else None,
        "sensitivity": c.sensitivity,
        "destinations": c.destinations or ["cv-cloud"],
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
