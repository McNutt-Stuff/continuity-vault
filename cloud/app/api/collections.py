"""Collections + backup execution (sync worker trigger)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import audit, security
from ..db import get_db
from ..models import Collection, ConnectorAccount, Tenant, Vault
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
    )
    db.add(coll)
    db.commit()
    db.refresh(coll)
    coll.destinations = body.destinations  # transient attr for response
    audit.record(db, actor=principal.user_id, action="collection.created",
                 tenant_id=tenant.id, resource=coll.id)
    return {"id": coll.id, "name": coll.name, "source_type": coll.source_type}


@router.get("")
def list_collections(tenant: Tenant = Depends(security.get_tenant),
                     db: Session = Depends(get_db)):
    colls = db.query(Collection).filter(Collection.tenant_id == tenant.id).all()
    return [{"id": c.id, "name": c.name, "source_type": c.source_type,
             "vault_id": c.vault_id, "sensitivity": c.sensitivity} for c in colls]


class BackupRequest(BaseModel):
    destinations: list[str] = ["cv-cloud"]


@router.post("/{collection_id}/backup")
def backup(collection_id: str, body: BackupRequest,
           principal: security.Principal = Depends(security.get_principal),
           tenant: Tenant = Depends(security.get_tenant),
           db: Session = Depends(get_db)):
    coll = db.get(Collection, collection_id)
    if not coll or coll.tenant_id != tenant.id:
        raise HTTPException(404, "collection not found")
    try:
        receipt = run_backup(db, coll, body.destinations)
    except Exception as exc:
        logger.exception("backup failed for collection %s (%s)", coll.id, coll.source_type)
        raise HTTPException(502, f"backup failed: {exc}")
    return {
        "snapshot_id": receipt.snapshot_id,
        "object_count": receipt.object_count,
        "total_bytes": receipt.total_bytes,
        "destinations": body.destinations,
        "recoverable": receipt.recoverable,
        "manifest_hash": receipt.manifest_hash,
    }
