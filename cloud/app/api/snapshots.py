"""Snapshot / recovery-point inventory."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import security
from ..db import get_db
from ..models import SnapshotReceipt, Tenant

router = APIRouter(prefix="/snapshots", tags=["snapshots"])


@router.get("")
def list_snapshots(tenant: Tenant = Depends(security.get_tenant),
                   db: Session = Depends(get_db)):
    rows = (db.query(SnapshotReceipt)
            .filter(SnapshotReceipt.tenant_id == tenant.id)
            .order_by(SnapshotReceipt.created_at.desc()).limit(200).all())
    return [{
        "id": r.id,
        "snapshot_id": r.snapshot_id,
        "vault_id": r.vault_id,
        "collection_id": r.collection_id,
        "destination": r.destination,
        "object_count": r.object_count,
        "total_bytes": r.total_bytes,
        "manifest_hash": r.manifest_hash,
        "recoverable": r.recoverable,
        "created_at": r.created_at.isoformat(),
    } for r in rows]
