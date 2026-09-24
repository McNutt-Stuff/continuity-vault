"""Snapshot / recovery-point inventory + integrity-validation detail."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import nullslast
from sqlalchemy.orm import Session

from .. import security
from ..db import get_db
from ..models import SearchDocument, SnapshotReceipt, Tenant

router = APIRouter(prefix="/snapshots", tags=["snapshots"])


def _labels(db: Session, tenant_id: str):
    """(store_label_map, location_label) resolvers, reused from the search API so a
    destination shows the REAL storage name (e.g. "Rob's Appliance · Vault SSD")
    instead of the opaque ``store:<id>`` / ``byos:<id>`` id."""
    from .search import _location_label, _store_label_map
    return _store_label_map(db, tenant_id), _location_label


@router.get("")
def list_snapshots(principal: security.Principal = Depends(security.get_principal),
                   tenant: Tenant = Depends(security.get_tenant),
                   db: Session = Depends(get_db)):
    # Data partitioning: recovery points are limited to the user's own vaults.
    allowed = security.content_vault_ids(db, principal)
    rows = (db.query(SnapshotReceipt)
            .filter(SnapshotReceipt.tenant_id == tenant.id,
                    SnapshotReceipt.vault_id.in_(allowed))
            .order_by(SnapshotReceipt.created_at.desc()).limit(200).all()) if allowed else []
    store_labels, location_label = _labels(db, tenant.id)
    from ..models import Collection
    coll_names = dict(db.query(Collection.id, Collection.name)
                      .filter(Collection.tenant_id == tenant.id).all())
    coll_types = dict(db.query(Collection.id, Collection.source_type)
                      .filter(Collection.tenant_id == tenant.id).all())
    return [{
        "id": r.id,
        "snapshot_id": r.snapshot_id,
        "vault_id": r.vault_id,
        "collection_id": r.collection_id,
        "collection_name": coll_names.get(r.collection_id) or "",
        "source_type": coll_types.get(r.collection_id) or "",
        "destination": r.destination,
        "destination_label": location_label(r.destination, store_labels),
        "object_count": r.object_count,
        "total_bytes": r.total_bytes,
        "manifest_hash": r.manifest_hash,
        "recoverable": r.recoverable,
        "created_at": r.created_at.isoformat(),
    } for r in rows]


@router.get("/{receipt_id}")
def snapshot_detail(receipt_id: str,
                    principal: security.Principal = Depends(security.get_principal),
                    tenant: Tenant = Depends(security.get_tenant),
                    db: Session = Depends(get_db)):
    """Expanded recovery-point view: WHAT data this point secured (the objects) plus
    the integrity evidence (hybrid-signed manifest hash + algorithms, per-object
    hashes, and the appliance seal result when applicable)."""
    allowed = set(security.content_vault_ids(db, principal))
    r = db.get(SnapshotReceipt, receipt_id)
    if r is None or r.tenant_id != tenant.id or r.vault_id not in allowed:
        raise HTTPException(404, "recovery point not found")

    store_labels, location_label = _labels(db, tenant.id)
    from ..models import Collection
    coll = db.get(Collection, r.collection_id)

    receipt = r.receipt if isinstance(r.receipt, dict) else {}
    payload = receipt.get("payload") if isinstance(receipt.get("payload"), dict) else {}
    signature = receipt.get("signature") if isinstance(receipt.get("signature"), dict) else {}
    # The stored receipt is the signed manifest for cloud/BYOS destinations, or the
    # appliance seal receipt after a seal (which carries the integrity result but not
    # the per-object hashes). Handle both shapes.
    is_seal = "integrityResult" in payload or "isolationState" in payload
    algorithms = [s.get("algorithm") for s in (signature.get("signatures") or [])
                  if s.get("algorithm")]
    manifest_object_hashes = {h.get("objectId"): h.get("ciphertextHash")
                              for h in (payload.get("objectHashes") or [])
                              if isinstance(h, dict)}

    # The collection of data secured in this recovery point: the index rows written
    # for this snapshot. Authoritative object identity + per-object content hash.
    docs = (db.query(SearchDocument)
            .filter(SearchDocument.tenant_id == tenant.id,
                    SearchDocument.snapshot_id == r.snapshot_id,
                    SearchDocument.vault_id.in_(allowed))
            .order_by(nullslast(SearchDocument.size_bytes.desc()))
            .limit(1000).all())
    objects = [{
        "object_id": d.object_id,
        "title": d.title,
        "source_type": d.source_type,
        "doc_type": d.doc_type,
        "size_bytes": d.size_bytes,
        "content_hash": d.content_hash,
        "manifest_hash": manifest_object_hashes.get(d.object_id),
    } for d in docs]

    seal = None
    if is_seal:
        seal = {
            "isolation_state": payload.get("isolationState"),
            "integrity_result": payload.get("integrityResult"),
            "commit_timestamp": payload.get("commitTimestamp"),
            "appliance_id": payload.get("applianceId"),
        }

    return {
        "id": r.id,
        "snapshot_id": r.snapshot_id,
        "created_at": r.created_at.isoformat(),
        "recoverable": r.recoverable,
        "destination": r.destination,
        "destination_label": location_label(r.destination, store_labels),
        "collection_id": r.collection_id,
        "collection_name": coll.name if coll else "",
        "source_type": coll.source_type if coll else "",
        "object_count": r.object_count,
        "total_bytes": r.total_bytes,
        "manifest_hash": r.manifest_hash,
        "integrity": {
            "signed": bool(algorithms),
            "hash_alg": signature.get("hashAlg") or "",
            "algorithms": algorithms,
            "retention_class": payload.get("retentionClass") or "",
            "manifest_object_count": len(manifest_object_hashes) or None,
            "seal": seal,
            "verified": bool(r.recoverable),
        },
        "objects": objects,
        "objects_truncated": len(objects) >= 1000,
    }
