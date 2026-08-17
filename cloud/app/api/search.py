"""Unified search across all data types, accounts, and objects (spec 15 / user
requirement). Passkey step-up is required because results expose object metadata
for the user's protected data."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import or_
from sqlalchemy.orm import Session

from .. import security
from ..db import get_db
from ..models import SearchDocument, Tenant

router = APIRouter(prefix="/search", tags=["search"])


@router.get("")
def search(q: str = "", source_type: str | None = None, doc_type: str | None = None,
           label: str | None = None, limit: int = 50,
           principal: security.Principal = Depends(security.require_passkey),
           tenant: Tenant = Depends(security.get_tenant),
           db: Session = Depends(get_db)):
    query = db.query(SearchDocument).filter(SearchDocument.tenant_id == tenant.id)
    if q:
        like = f"%{q.lower()}%"
        # Match title, preview, and the connector-declared searchable metadata
        # blob so metadata (sender, path, party, tags, …) is searchable too.
        query = query.filter(or_(
            SearchDocument.title.ilike(like),
            SearchDocument.preview.ilike(like),
            SearchDocument.search_blob.ilike(like),
        ))
    if source_type:
        query = query.filter(SearchDocument.source_type == source_type)
    if doc_type:
        query = query.filter(SearchDocument.doc_type == doc_type)

    rows = query.order_by(SearchDocument.modified_at.desc()).limit(limit).all()
    # Label filter is applied in Python since labels are stored as a JSON array.
    if label:
        rows = [r for r in rows if label in (r.labels or [])]
    results = [{
        "object_id": r.object_id,
        "snapshot_id": r.snapshot_id,
        "collection_id": r.collection_id,
        "source_type": r.source_type,
        "doc_type": r.doc_type,
        "title": r.title,
        "preview": r.preview,
        "meta": r.meta,
        "labels": r.labels,
        "size_bytes": r.size_bytes,
        "modified_at": r.modified_at.isoformat() if r.modified_at else None,
    } for r in rows]

    # Faceted counts for the search UI.
    all_rows = db.query(SearchDocument).filter(SearchDocument.tenant_id == tenant.id).all()
    by_source: dict[str, int] = {}
    by_type: dict[str, int] = {}
    by_label: dict[str, int] = {}
    for r in all_rows:
        by_source[r.source_type] = by_source.get(r.source_type, 0) + 1
        by_type[r.doc_type] = by_type.get(r.doc_type, 0) + 1
        for lbl in (r.labels or []):
            by_label[lbl] = by_label.get(lbl, 0) + 1

    return {
        "count": len(results),
        "total_indexed": len(all_rows),
        "results": results,
        "facets": {"source": by_source, "type": by_type, "label": by_label},
    }
