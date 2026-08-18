"""Unified search across all data types, accounts, and objects (spec 15 / user
requirement). Passkey step-up is required because results expose object metadata
for the user's protected data."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import audit, fleet, security
from ..db import get_db
from ..models import Appliance, SearchDocument, SnapshotReceipt, Tenant
from ..storage import build_destination
from ..taxonomy import describe, sensitivity_for

router = APIRouter(prefix="/search", tags=["search"])


def _location_label(destination: str) -> str:
    base = destination.split(":", 1)[0]
    return {
        "cv-cloud": "Arkive Cloud",
        "customer-s3": "Customer S3",
        "local-fs": "Local store",
        "appliance": "Appliance",
    }.get(base, destination)


@router.get("/taxonomy")
def taxonomy(principal: security.Principal = Depends(security.get_principal)):
    """The canonical information model (categories + kinds) used across sources."""
    return describe()


@router.get("")
def search(q: str = "", source_type: str | None = None, doc_type: str | None = None,
           category: str | None = None, label: str | None = None, limit: int = 50,
           principal: security.Principal = Depends(security.require_passkey),
           tenant: Tenant = Depends(security.get_tenant),
           db: Session = Depends(get_db)):
    # Pull the whole tenant index newest-first, then de-duplicate: repeated
    # backups (and multi-destination stores) create a fresh index row per
    # snapshot for the same object. The UI must show each object once, so we keep
    # the newest row per (source, object) and remember every snapshot it appeared
    # in (for the "stored at" locations).
    all_docs = (db.query(SearchDocument)
                .filter(SearchDocument.tenant_id == tenant.id)
                .order_by(SearchDocument.created_at.desc()).all())

    unique: list[SearchDocument] = []
    seen: set[tuple] = set()
    object_snapshots: dict[tuple, set[str]] = {}
    for r in all_docs:
        key = (r.source_type, r.object_id)
        object_snapshots.setdefault(key, set())
        if r.snapshot_id:
            object_snapshots[key].add(r.snapshot_id)
        if key in seen:
            continue
        seen.add(key)
        unique.append(r)

    # Facets reflect the de-duplicated universe so counts match what's shown.
    by_source: dict[str, int] = {}
    by_type: dict[str, int] = {}
    by_category: dict[str, int] = {}
    by_label: dict[str, int] = {}
    for r in unique:
        by_source[r.source_type] = by_source.get(r.source_type, 0) + 1
        by_type[r.doc_type] = by_type.get(r.doc_type, 0) + 1
        if r.category:
            by_category[r.category] = by_category.get(r.category, 0) + 1
        for lbl in (r.labels or []):
            by_label[lbl] = by_label.get(lbl, 0) + 1

    # Apply filters to the de-duplicated set (all filters, before limiting).
    ql = q.lower().strip() if q else ""
    filtered = []
    for r in unique:
        if source_type and r.source_type != source_type:
            continue
        if doc_type and r.doc_type != doc_type:
            continue
        if category and r.category != category:
            continue
        if label and label not in (r.labels or []):
            continue
        if ql:
            hay = " ".join([r.title or "", r.preview or "", r.search_blob or ""]).lower()
            if ql not in hay:
                continue
        filtered.append(r)

    filtered.sort(key=lambda r: r.modified_at or r.created_at, reverse=True)
    rows = filtered[:limit]

    # Map each result's object to every destination it is stored in (across all
    # of its snapshots): cloud, appliance, customer S3.
    all_snap_ids: set[str] = set()
    for r in rows:
        all_snap_ids |= object_snapshots.get((r.source_type, r.object_id), set())
    receipts_by_snap: dict[str, list] = {}
    if all_snap_ids:
        for rc in (db.query(SnapshotReceipt)
                   .filter(SnapshotReceipt.tenant_id == tenant.id,
                           SnapshotReceipt.snapshot_id.in_(all_snap_ids)).all()):
            receipts_by_snap.setdefault(rc.snapshot_id, []).append(rc)

    def _locations_for(r: SearchDocument) -> list:
        out: dict[str, dict] = {}
        for snap in object_snapshots.get((r.source_type, r.object_id), set()):
            for rc in receipts_by_snap.get(snap, []):
                # Prefer the recoverable copy when the same destination recurs.
                existing = out.get(rc.destination)
                if existing and existing["recoverable"]:
                    continue
                out[rc.destination] = {
                    "destination": rc.destination,
                    "label": _location_label(rc.destination),
                    "recoverable": bool(rc.recoverable),
                }
        return list(out.values())

    results = [{
        "object_id": r.object_id,
        "snapshot_id": r.snapshot_id,
        "collection_id": r.collection_id,
        "source_type": r.source_type,
        "doc_type": r.doc_type,
        "category": r.category,
        "sensitivity": sensitivity_for(r.category or ""),
        "title": r.title,
        "preview": r.preview,
        "meta": r.meta,
        "labels": r.labels,
        "size_bytes": r.size_bytes,
        "modified_at": r.modified_at.isoformat() if r.modified_at else None,
        "locations": _locations_for(r),
    } for r in rows]

    return {
        "count": len(results),
        "total_indexed": len(unique),
        "results": results,
        "facets": {"source": by_source, "type": by_type,
                   "category": by_category, "label": by_label},
    }


class RetrieveRequest(BaseModel):
    snapshot_id: str
    object_id: str
    destination: str  # the chosen storage location for this item


@router.post("/retrieve")
def retrieve(body: RetrieveRequest,
             principal: security.Principal = Depends(security.require_passkey),
             tenant: Tenant = Depends(security.get_tenant),
             db: Session = Depends(get_db)):
    """Retrieve an item from wherever the customer stored it. Cloud/S3 objects are
    read directly (returned client-encrypted); appliance-stored objects are pulled
    via a signed recovery-window command to the offline appliance."""
    base = body.destination.split(":", 1)[0]
    label = _location_label(body.destination)

    if base == "appliance":
        appliance = None
        if ":" in body.destination:
            aid = body.destination.split(":", 1)[1]
            appliance = db.get(Appliance, aid)
        if not appliance or appliance.tenant_id != tenant.id:
            appliance = (db.query(Appliance)
                         .filter(Appliance.tenant_id == tenant.id,
                                 Appliance.state.in_(["SEALED", "ONLINE_STAGING"]))
                         .order_by(Appliance.last_heartbeat_at.desc()).first())
        if not appliance:
            raise HTTPException(404, "no appliance available to retrieve from")
        cmd = fleet.issue_command(
            db, appliance, "OPEN_RECOVERY_WINDOW", principal.user_id,
            {"snapshotId": body.snapshot_id, "objectIds": [body.object_id]})
        audit.record(db, actor=principal.user_id, action="search.retrieve",
                     tenant_id=tenant.id, resource=body.object_id,
                     detail={"location": label, "appliance": appliance.id})
        return {"status": "requested", "location": label, "async": True,
                "message": f"Recovery requested from {appliance.name}. "
                           "The appliance unseals, retrieves, and re-seals; "
                           "local approval may be required.",
                "command_id": cmd.id}

    # Cloud / customer-S3 / local: read the (client-encrypted) object directly.
    try:
        dest = build_destination(body.destination if base in ("cv-cloud", "customer-s3") else "cv-cloud")
        prefix = tenant.storage_prefix or tenant.id
        data = dest.get_object(prefix, f"{body.snapshot_id}/{body.object_id}")
    except Exception as exc:
        raise HTTPException(404, f"object not found at {label}: {exc}")
    audit.record(db, actor=principal.user_id, action="search.retrieve",
                 tenant_id=tenant.id, resource=body.object_id,
                 detail={"location": label})
    return {"status": "available", "location": label, "async": False,
            "size_bytes": len(data), "encrypted": True,
            "message": f"Object available at {label} ({len(data)} bytes, client-encrypted)."}
