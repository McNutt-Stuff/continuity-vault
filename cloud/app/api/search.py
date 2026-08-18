"""Unified search across all data types, accounts, and objects (spec 15 / user
requirement). Passkey step-up is required because results expose object metadata
for the user's protected data."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import audit, fleet, security
from ..db import get_db
from ..connectors import get_connector
from ..models import Appliance, ApplianceStorage, Collection, ConnectorAccount, SearchDocument, SnapshotReceipt, Tenant
from ..storage import build_destination
from ..taxonomy import describe, sensitivity_for

router = APIRouter(prefix="/search", tags=["search"])


def _location_label(destination: str, store_labels: dict[str, str] | None = None) -> str:
    if destination.startswith("store:") and store_labels:
        return store_labels.get(destination, "Appliance storage")
    base = destination.split(":", 1)[0]
    return {
        "cv-cloud": "Arkive Cloud",
        "customer-s3": "Customer S3",
        "local-fs": "Local store",
        "appliance": "Appliance",
        "store": "Appliance storage",
    }.get(base, destination)


def _store_label_map(db: Session, tenant_id: str) -> dict[str, str]:
    """Map ``store:<id>`` → "<appliance> · <storage>" for the tenant."""
    out: dict[str, str] = {}
    appliances = {a.id: a for a in db.query(Appliance)
                  .filter(Appliance.tenant_id == tenant_id).all()}
    for s in (db.query(ApplianceStorage)
              .filter(ApplianceStorage.tenant_id == tenant_id).all()):
        a = appliances.get(s.appliance_id)
        out[f"store:{s.id}"] = f"{a.name} · {s.name}" if a else s.name
    return out


@router.get("/taxonomy")
def taxonomy(principal: security.Principal = Depends(security.get_principal)):
    """The canonical information model (categories + kinds) used across sources."""
    return describe()


@router.get("")
def search(q: str = "", source_type: str | None = None, doc_type: str | None = None,
           category: str | None = None, label: str | None = None,
           attr: str | None = None, limit: int = 50,
           principal: security.Principal = Depends(security.require_passkey),
           tenant: Tenant = Depends(security.get_tenant),
           db: Session = Depends(get_db)):
    # attr is an optional "key:value" attribute filter (e.g. "folder:Inbox"),
    # applied against the object's discrete indexed metadata.
    attr_key, attr_val = "", ""
    if attr and ":" in attr:
        attr_key, attr_val = attr.split(":", 1)
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

    # Resolve friendly source titles (the linked account label / mapping name)
    # and connector display names for every collection referenced by results.
    coll_ids = {r.collection_id for r in unique if r.collection_id}
    coll_label: dict[str, str] = {}
    coll_display: dict[str, str] = {}
    if coll_ids:
        for c in (db.query(Collection)
                  .filter(Collection.id.in_(coll_ids)).all()):
            account = (db.get(ConnectorAccount, c.connector_account_id)
                       if c.connector_account_id else None)
            coll_label[c.id] = account.account_label if account else c.name
            conn = get_connector(c.source_type)
            coll_display[c.id] = conn.display_name if conn else c.source_type
    source_display: dict[str, str] = {}
    for st in by_source:
        conn = get_connector(st)
        source_display[st] = conn.display_name if conn else st

    # Base filter: everything except the discrete-attribute filter (so the
    # attribute facets reflect what's available within the current source/type).
    ql = q.lower().strip() if q else ""
    base = []
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
        base.append(r)

    # Dynamic attribute facets from the discrete indexed metadata of the base set.
    # Each data type brings its own keys (folder, from, vault, kind, path…).
    meta_facets: dict[str, dict[str, int]] = {}
    for r in base:
        for k, v in (r.meta or {}).items():
            if k == "client_encrypted" or v in (None, "", [], {}):
                continue
            values = v if isinstance(v, (list, tuple)) else [v]
            for val in values:
                sval = str(val)
                if not sval or len(sval) > 60:
                    continue
                meta_facets.setdefault(k, {})
                meta_facets[k][sval] = meta_facets[k].get(sval, 0) + 1
    # Keep only keys with more than one distinct value (useful as filters), and
    # cap values per key.
    meta_facets = {
        k: dict(sorted(vals.items(), key=lambda kv: kv[1], reverse=True)[:8])
        for k, vals in meta_facets.items() if len(vals) > 1
    }

    # Final rows: apply the attribute filter on top of the base set.
    def _matches_attr(r: SearchDocument) -> bool:
        if not attr_key:
            return True
        v = (r.meta or {}).get(attr_key)
        if isinstance(v, (list, tuple)):
            return attr_val in [str(x) for x in v]
        return str(v) == attr_val

    filtered = [r for r in base if _matches_attr(r)]
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

    store_labels = _store_label_map(db, tenant.id)

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
                    "label": _location_label(rc.destination, store_labels),
                    "recoverable": bool(rc.recoverable),
                }
        return list(out.values())

    results = [{
        "object_id": r.object_id,
        "snapshot_id": r.snapshot_id,
        "collection_id": r.collection_id,
        "source_type": r.source_type,
        "source_label": coll_label.get(r.collection_id, source_display.get(r.source_type, r.source_type)),
        "source_display": coll_display.get(r.collection_id, source_display.get(r.source_type, r.source_type)),
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
                   "category": by_category, "label": by_label,
                   "attributes": meta_facets},
        "source_display": source_display,
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
    store_labels = _store_label_map(db, tenant.id)
    label = _location_label(body.destination, store_labels)

    if base in ("appliance", "store"):
        appliance = None
        if base == "store":
            store = db.get(ApplianceStorage, body.destination.split(":", 1)[1])
            if store and store.tenant_id == tenant.id:
                appliance = db.get(Appliance, store.appliance_id)
        elif ":" in body.destination:
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
