"""Customer Overview (Home) aggregation.

One call returns everything the landing page shows: the sources we protect (with
their types), a breakdown of the objects we've captured, data protected vs the
licensed allowance, vaults + storage destinations, and the protection/retention
posture. Keeping the aggregation server-side keeps the dashboard a single fast
request instead of half a dozen.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import security
from ..connectors import get_connector
from ..db import get_db
from ..models import (
    ApplianceStorage,
    Collection,
    ConnectorAccount,
    DesktopAgent,
    ProtectionPolicy,
    SearchDocument,
    Tenant,
    Vault,
)

router = APIRouter(prefix="/overview", tags=["overview"])


# Group the connector doc-type taxonomy into the buckets a customer thinks in.
_OBJECT_BUCKETS: list[dict] = [
    {"key": "email", "label": "Emails", "icon": "mail", "color": "#ea4335",
     "types": {"email"}},
    {"key": "credential", "label": "Credentials", "icon": "key", "color": "#0364d3",
     "types": {"secret", "note", "password", "login"}},
    {"key": "document", "label": "Documents", "icon": "file", "color": "#4f7cff",
     "types": {"pdf", "text", "spreadsheet", "presentation", "record"}},
    {"key": "photo", "label": "Photos & images", "icon": "image", "color": "#35d0a5",
     "types": {"image", "photo"}},
    {"key": "media", "label": "Audio & video", "icon": "activity", "color": "#f5a623",
     "types": {"video", "audio"}},
    {"key": "file", "label": "Files & archives", "icon": "database", "color": "#7a5cff",
     "types": {"file", "archive"}},
    {"key": "contact", "label": "Contacts", "icon": "user", "color": "#c56cf0",
     "types": {"contact"}},
]
_ICON_MAP = {"folder": "file", "gear": "database"}  # connector icon → available UI icon


def _bucket_for(doc_type: str) -> dict:
    dt = (doc_type or "").lower()
    for b in _OBJECT_BUCKETS:
        if dt in b["types"]:
            return b
    return _OBJECT_BUCKETS[5]  # default: files & archives


def _source_meta(source_type: str) -> dict:
    conn = get_connector(source_type)
    if not conn:
        return {"type": source_type, "displayName": source_type,
                "icon": "database", "color": "#7a5cff"}
    spec = conn.oauth_spec()
    return {"type": source_type, "displayName": spec.display_name,
            "icon": _ICON_MAP.get(spec.icon, spec.icon), "color": spec.color}


def _storage_meta(dest: str, stores: dict) -> dict:
    if dest in ("cv-cloud",):
        return {"id": dest, "label": "Arkive Cloud", "kind": "cloud", "icon": "cloud"}
    if dest in ("customer-s3",):
        return {"id": dest, "label": "Your S3 bucket", "kind": "cloud", "icon": "cloud"}
    if dest.startswith("store:"):
        s = stores.get(dest.split(":", 1)[1])
        return {"id": dest, "label": s.name if s else "Appliance storage",
                "kind": "appliance", "icon": "server"}
    if dest == "appliance":
        return {"id": dest, "label": "Offline appliance", "kind": "appliance", "icon": "server"}
    return {"id": dest, "label": dest, "kind": "other", "icon": "database"}


@router.get("")
def overview(tenant: Tenant = Depends(security.get_tenant),
             db: Session = Depends(get_db)):
    accounts = db.query(ConnectorAccount).filter(
        ConnectorAccount.tenant_id == tenant.id).all()
    agents = db.query(DesktopAgent).filter(
        DesktopAgent.tenant_id == tenant.id,
        DesktopAgent.state != "retired").all()
    collections = db.query(Collection).filter(
        Collection.tenant_id == tenant.id).all()
    vaults = db.query(Vault).filter(Vault.tenant_id == tenant.id).all()

    # --- Protected sources + the mix of source types we cover ----------------
    type_counts: dict[str, int] = {}
    for c in collections:
        type_counts[c.source_type] = type_counts.get(c.source_type, 0) + 1
    if not type_counts:  # nothing mapped yet — seed from linked accounts/agents
        for a in accounts:
            type_counts[a.connector_type] = type_counts.get(a.connector_type, 0) + 1
        for ag in agents:
            for col in (ag.collectors or []):
                type_counts[col] = type_counts.get(col, 0) + 1
    source_types = [{**_source_meta(t), "count": n}
                    for t, n in sorted(type_counts.items(), key=lambda kv: -kv[1])]

    # --- Objects protected (deduped per logical object) + type breakdown -----
    docs = (db.query(SearchDocument)
            .filter(SearchDocument.tenant_id == tenant.id)
            .order_by(SearchDocument.created_at.desc()).all())
    seen: set[tuple] = set()
    bucket_counts: dict[str, int] = {}
    protected_bytes = 0
    object_total = 0
    for d in docs:
        key = (d.source_type, d.object_id)
        if key in seen:
            continue
        seen.add(key)
        object_total += 1
        protected_bytes += int(d.size_bytes or 0)
        bucket_counts[_bucket_for(d.doc_type)["key"]] = \
            bucket_counts.get(_bucket_for(d.doc_type)["key"], 0) + 1
    object_breakdown = [
        {"key": b["key"], "label": b["label"], "icon": b["icon"], "color": b["color"],
         "count": bucket_counts.get(b["key"], 0)}
        for b in _OBJECT_BUCKETS if bucket_counts.get(b["key"], 0) > 0
    ]

    # --- Data protected vs licensed allowance --------------------------------
    licensed = int(tenant.licensed_bytes or 0)
    percent = round(min(100.0, protected_bytes / licensed * 100), 1) if licensed else None

    # --- Vaults + storage destinations ---------------------------------------
    stores = {s.id: s for s in db.query(ApplianceStorage)
              .filter(ApplianceStorage.tenant_id == tenant.id).all()}
    dest_ids: list[str] = []
    for c in collections:
        for d in (c.destinations or ["cv-cloud"]):
            if d not in dest_ids:
                dest_ids.append(d)
    if not dest_ids:
        dest_ids = ["cv-cloud"]
    destinations = [_storage_meta(d, stores) for d in dest_ids]

    # --- Retention / protection posture --------------------------------------
    policies = db.query(ProtectionPolicy).filter(
        ProtectionPolicy.tenant_id == tenant.id).all()
    if policies:
        retention = {
            "cloud_days": max(p.cloud_retention_days for p in policies),
            "appliance_days": max(p.appliance_retention_days for p in policies),
            "immutability_days": max(p.immutability_days for p in policies),
            "rpo_minutes": min(p.rpo_minutes for p in policies),
        }
    else:
        # No policy configured — mirror the ProtectionPolicy column defaults
        # (SQLAlchemy column defaults only apply on insert, not on a bare instance).
        retention = {"cloud_days": 365, "appliance_days": 3650,
                     "immutability_days": 365, "rpo_minutes": 60}

    return {
        "sources": {"count": sum(type_counts.values()), "types": source_types},
        "objects": {"total": object_total, "breakdown": object_breakdown},
        "data": {"protected_bytes": protected_bytes, "licensed_bytes": licensed,
                 "percent": percent},
        "storage": {"vault_count": len(vaults), "destinations": destinations},
        "retention": retention,
        "protection": {
            "key_ownership_model": tenant.key_ownership_model,
            "encrypted": True,
        },
    }


# period -> (number of buckets, size of each bucket). "all" is handled dynamically.
_PERIODS = {
    "week": (7, timedelta(days=1)),
    "month": (30, timedelta(days=1)),
    "quarter": (13, timedelta(weeks=1)),
    "year": (12, timedelta(days=30)),
}


def _bucket_edges(period: str, now: datetime, earliest: datetime) -> list[datetime]:
    """End-of-bucket cutoff datetimes spanning the requested period, oldest→newest."""
    if period == "all":
        span = (now - earliest) or timedelta(days=1)
        step = span / 12
        return [earliest + step * (i + 1) for i in range(12)]
    n, step = _PERIODS.get(period, _PERIODS["week"])
    start = now - step * (n - 1)
    return [start + step * i for i in range(n)]


@router.get("/trends")
def trends(period: str = "week", tenant: Tenant = Depends(security.get_tenant),
           db: Session = Depends(get_db)):
    """Cumulative count of protected objects over time, per object type, so the
    dashboard can draw a growth trend line per category over a rolling window."""
    docs = (db.query(SearchDocument.source_type, SearchDocument.object_id,
                     SearchDocument.doc_type, SearchDocument.created_at)
            .filter(SearchDocument.tenant_id == tenant.id)
            .order_by(SearchDocument.created_at.asc()).all())
    # First time each logical object was protected + its bucket.
    first: dict[tuple, tuple] = {}
    earliest = None
    for st, oid, doc_type, created in docs:
        key = (st, oid)
        if key in first or created is None:
            continue
        first[key] = (created, _bucket_for(doc_type)["key"])
        earliest = created if earliest is None else min(earliest, created)

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if earliest is None:
        earliest = now - timedelta(days=7)
    edges = _bucket_edges(period if period in _PERIODS or period == "all" else "week", now, earliest)

    counts: dict[str, list[int]] = {}
    for created, bk in first.values():
        row = counts.setdefault(bk, [0] * len(edges))
        for i, edge in enumerate(edges):
            if created <= edge:
                row[i] += 1  # cumulative — present at/after first ingest
    series = []
    for b in _OBJECT_BUCKETS:
        vals = counts.get(b["key"])
        if not vals or vals[-1] == 0:
            continue
        series.append({"key": b["key"], "label": b["label"], "icon": b["icon"],
                       "color": b["color"], "values": vals, "current": vals[-1]})
    series.sort(key=lambda s: -s["current"])
    return {
        "period": period,
        "points": [e.isoformat() for e in edges],
        "series": series,
    }
